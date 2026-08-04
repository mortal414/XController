from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, MagicMock

try:
    from XController import ControllerSettings, XTextAdapter
except ModuleNotFoundError as exc:
    if exc.name != "XController":
        raise
    repo_root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "XController",
        repo_root / "__init__.py",
        submodule_search_locations=[str(repo_root)],
    )
    if spec is None or spec.loader is None:
        raise
    module = importlib.util.module_from_spec(spec)
    sys.modules["XController"] = module
    spec.loader.exec_module(module)
    from XController import ControllerSettings, XTextAdapter


class NoopHuman:
    async def jitter(self, *_args, **_kwargs) -> None:
        return None


class FakeSyncContext:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeSyncPlaywright:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class FakeLocator:
    def __init__(self, count: int) -> None:
        self._count = count

    async def count(self) -> int:
        return self._count

    async def is_visible(self) -> bool:
        return self._count > 0


class FakeStatusLink(FakeLocator):
    def __init__(self, href: str) -> None:
        super().__init__(1)
        self.href = href

    async def get_attribute(self, name: str) -> str | None:
        return self.href if name == "href" else None


class FakeStatusLinks(FakeLocator):
    def __init__(self, status_ids: set[str]) -> None:
        self._links = [FakeStatusLink(f"/owner/status/{post_id}") for post_id in sorted(status_ids)]
        super().__init__(len(self._links))

    def nth(self, index: int) -> FakeStatusLink:
        return self._links[index]


class FakeArticle:
    def __init__(self, name: str, status_ids: set[str]) -> None:
        self.name = name
        self.status_ids = status_ids

    async def count(self) -> int:
        return 1

    async def is_visible(self) -> bool:
        return True

    def locator(self, selector: str) -> FakeLocator:
        if selector == 'a[href*="/status/"]':
            return FakeStatusLinks(self.status_ids)
        matches = any(f"/status/{post_id}" in selector for post_id in self.status_ids)
        return FakeLocator(1 if matches else 0)


class FakeArticleList:
    def __init__(self, articles: list[FakeArticle]) -> None:
        self._articles = articles
        self.first = articles[0] if articles else FakeMissingArticle()

    async def count(self) -> int:
        return len(self._articles)

    def nth(self, index: int) -> FakeArticle:
        return self._articles[index]


class FakeArticlePage:
    url = "https://x.com/home"

    def __init__(self, articles: list[FakeArticle]) -> None:
        self._articles = FakeArticleList(articles)

    def locator(self, selector: str) -> FakeArticleList:
        assert selector == "article"
        return self._articles


class FakeMissingArticle:
    async def count(self) -> int:
        return 0

    def locator(self, _selector: str) -> FakeLocator:
        return FakeLocator(0)


class RuntimeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.adapter = XTextAdapter(profile_path=str(Path(self.tmp.name) / "profile"))
        self.adapter.human = NoopHuman()

    def tearDown(self) -> None:
        self.adapter._shutdown_executor()
        self.tmp.cleanup()

    async def test_start_uses_sync_fallback_when_preferred(self) -> None:
        async def fake_start_sync_fallback() -> None:
            self.adapter._sync_context = FakeSyncContext()
            self.adapter._sync_page = object()
            self.adapter._sync_mode = True

        self.adapter._prefer_sync_playwright = MagicMock(return_value=True)
        self.adapter._start_sync_fallback = AsyncMock(side_effect=fake_start_sync_fallback)
        self.adapter._wait_network_idle = AsyncMock()

        await self.adapter.start()

        self.adapter._start_sync_fallback.assert_awaited_once()
        self.adapter._wait_network_idle.assert_awaited_once()
        self.assertTrue(self.adapter._sync_mode)
        self.assertIs(self.adapter.context, self.adapter._sync_context)
        self.assertIs(self.adapter.page, self.adapter._sync_page)

    async def test_sync_fallback_uses_headless_setting(self) -> None:
        self.adapter.settings = ControllerSettings(headless=True, playwright_mode="sync", prefer_sync_playwright=True)
        captured: dict[str, object] = {}

        async def run_sync(func, context_kwargs):
            captured.update(context_kwargs)
            return None

        self.adapter._run_sync = AsyncMock(side_effect=run_sync)

        await self.adapter._start_sync_fallback()

        self.assertTrue(captured["headless"])
        self.assertTrue(self.adapter._sync_mode)

    async def test_close_cleans_up_sync_mode_resources(self) -> None:
        sync_context = FakeSyncContext()
        sync_playwright = FakeSyncPlaywright()
        self.adapter.context = sync_context  # type: ignore[assignment]
        self.adapter.page = object()  # type: ignore[assignment]
        self.adapter._sync_context = sync_context
        self.adapter._sync_page = object()
        self.adapter._sync_playwright = sync_playwright
        self.adapter._sync_mode = True

        async def run_sync(func, *args, **kwargs):
            return func(*args, **kwargs)

        self.adapter._run_sync = AsyncMock(side_effect=run_sync)

        await self.adapter.close()

        self.assertTrue(sync_context.closed)
        self.assertTrue(sync_playwright.stopped)
        self.assertIsNone(self.adapter.context)
        self.assertIsNone(self.adapter.page)
        self.assertFalse(self.adapter._sync_mode)
        self.assertIsNone(self.adapter._executor)

    async def test_post_metrics_selects_article_matching_target_status(self) -> None:
        parent = FakeArticle("parent", {"111"})
        reply = FakeArticle("reply", {"222"})
        self.adapter.page = FakeArticlePage([parent, reply])  # type: ignore[assignment]
        self.adapter._bind_locator = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda article, _purpose: (article, "stable")
        )

        selected = await self.adapter._post_metrics_article("222")

        self.assertIs(selected, reply)

    async def test_post_metrics_does_not_fall_back_to_parent_article(self) -> None:
        parent = FakeArticle("parent", {"111"})
        self.adapter.page = FakeArticlePage([parent])  # type: ignore[assignment]

        selected = await self.adapter._post_metrics_article("333")

        self.assertIsNone(selected)

    async def test_post_metrics_returns_empty_when_only_parent_article_is_present(self) -> None:
        parent = FakeArticle("parent", {"111"})
        self.adapter.page = FakeArticlePage([parent])  # type: ignore[assignment]
        self.adapter._open_post_page = AsyncMock(return_value=True)  # type: ignore[method-assign]

        metrics = await self.adapter.post_metrics("333")

        self.assertEqual(metrics, {})

    async def test_post_metrics_returns_empty_when_status_page_has_no_article(self) -> None:
        self.adapter.page = FakeArticlePage([])  # type: ignore[assignment]
        self.adapter._open_post_page = AsyncMock(return_value=True)  # type: ignore[method-assign]

        metrics = await self.adapter.post_metrics("333")

        self.assertEqual(metrics, {})

    async def test_submit_post_uses_one_platform_shortcut_and_does_not_retry(self) -> None:
        self.adapter.page = object()  # type: ignore[assignment]
        self.adapter._find_first = AsyncMock(return_value=None)  # type: ignore[method-assign]
        self.adapter._wait_for_post_submission = AsyncMock(return_value=False)  # type: ignore[method-assign]

        async def fake_keyboard_press(_key: str) -> None:
            raise RuntimeError("shortcut boom")

        self.adapter._keyboard_press = AsyncMock(side_effect=fake_keyboard_press)  # type: ignore[method-assign]

        with self.assertLogs("XController._adapter_runtime", level="WARNING") as logs:
            submitted = await self.adapter._submit_post()

        self.assertFalse(submitted)
        expected = "Meta+Enter" if sys.platform == "darwin" else "Control+Enter"
        self.adapter._keyboard_press.assert_awaited_once_with(expected)
        self.adapter._wait_for_post_submission.assert_not_awaited()
        self.assertTrue(any("submit_post_shortcut_failed" in line and expected in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
