from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock

try:
    from XController import LoginState, XTextAdapter
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
    from XController import LoginState, XTextAdapter


class _NoopHuman:
    async def jitter(self, *_args, **_kwargs) -> None:
        return None


class _Locator:
    def __init__(self, items: list[_Node]):
        self.items = items

    @property
    def first(self) -> _Node:
        return self.items[0] if self.items else _MissingNode()

    def nth(self, index: int) -> _Node:
        return self.items[index]

    async def count(self) -> int:
        return len(self.items)


class _Node:
    def __init__(self, *, text: str = "", attrs: dict[str, str] | None = None):
        self.text = text
        self.attrs = attrs or {}

    @property
    def first(self) -> _Node:
        return self

    async def count(self) -> int:
        return 1

    async def get_attribute(self, name: str) -> str | None:
        return self.attrs.get(name)

    async def inner_text(self, timeout: int | None = None) -> str:
        return self.text


class _MissingNode(_Node):
    async def count(self) -> int:
        return 0


class _Article(_Node):
    def __init__(self, *, post_id: str, author: str, text: str, created_at: datetime):
        super().__init__(text=text)
        self.post_id = post_id
        self.author = author
        self.created_at = created_at

    def locator(self, selector: str) -> _Locator:
        if selector == 'a[href*="/status/"]':
            return _Locator([_Node(attrs={"href": f"/{self.author}/status/{self.post_id}"})])
        if selector == 'div[data-testid="User-Name"] a[href^="/"]':
            return _Locator([_Node(attrs={"href": f"/{self.author}"})])
        if selector == 'div[data-testid="tweetText"]':
            return _Locator([_Node(text=self.text)])
        if selector == "time":
            return _Locator([_Node(attrs={"datetime": self.created_at.isoformat()})])
        return _Locator([])


class _TimestampNode(_Node):
    def __init__(self, outer_post_id: str):
        super().__init__(attrs={"datetime": datetime.now(timezone.utc).isoformat()})
        self.outer_post_id = outer_post_id

    def locator(self, selector: str) -> _Locator:
        if "ancestor::a" in selector:
            return _Locator([_Node(attrs={"href": f"/owner/status/{self.outer_post_id}"})])
        return _Locator([])


class _QuotedArticle(_Article):
    def __init__(self, *, outer_post_id: str, quote_post_id: str):
        super().__init__(
            post_id=outer_post_id,
            author="owner",
            text="outer post with nested quote",
            created_at=datetime.now(timezone.utc),
        )
        self.quote_post_id = quote_post_id

    def locator(self, selector: str) -> _Locator:
        if selector == 'a[href*="/status/"]':
            return _Locator(
                [
                    _Node(attrs={"href": f"/quoted/status/{self.quote_post_id}"}),
                    _Node(attrs={"href": f"/owner/status/{self.post_id}"}),
                ]
            )
        if selector == "time":
            return _Locator([_TimestampNode(self.post_id)])
        return super().locator(selector)


class _Page:
    url = "https://x.com/notifications/mentions"

    def __init__(self, articles: list[_Article]):
        self.articles = articles

    def locator(self, selector: str) -> _Locator:
        if selector in {'article[data-testid="tweet"], article', "article"}:
            return _Locator(self.articles)
        return _Locator([])


class _FailIfScannedPage:
    url = "https://x.com/notifications"

    def locator(self, _selector: str) -> _Locator:
        raise AssertionError("mentions should not scan articles when tab selection fails")


class ReadContractHardeningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.adapter = XTextAdapter(profile_path=str(Path(self.tmp.name) / "profile"))
        self.adapter.human = _NoopHuman()
        self.adapter._random_scroll = AsyncMock()

    def tearDown(self) -> None:
        self.adapter._shutdown_executor()
        self.tmp.cleanup()

    async def test_search_rejects_empty_query_and_nonpositive_limit(self) -> None:
        for query in ("", "   ", None):
            with self.subTest(query=query), self.assertRaisesRegex(ValueError, "query must not be empty"):
                await self.adapter.search_posts(query, limit=1)  # type: ignore[arg-type]

        for limit in (0, -1):
            with self.subTest(limit=limit), self.assertRaisesRegex(ValueError, "limit must be a positive integer"):
                await self.adapter.search_posts("valid query", limit=limit)

    async def test_mentions_requires_confirmed_mentions_tab(self) -> None:
        self.adapter.page = _FailIfScannedPage()  # type: ignore[assignment]
        self.adapter._looks_like_notifications_page = AsyncMock(return_value=True)  # type: ignore[method-assign]
        self.adapter._open_notifications_mentions_tab = AsyncMock(return_value=False)  # type: ignore[method-assign]

        rows = await self.adapter.read_mentions(
            "target",
            hours_back=2,
            limit=1,
            min_scroll_rounds=1,
            max_scroll_rounds=1,
        )

        self.assertEqual(rows, [])
        self.adapter._open_notifications_mentions_tab.assert_awaited_once()  # type: ignore[attr-defined]

    async def test_mentions_filters_old_rows_and_honors_exact_limit(self) -> None:
        now = datetime.now(timezone.utc)
        self.adapter.page = _Page(
            [
                _Article(post_id="100", author="old_author", text="old @target", created_at=now - timedelta(hours=3)),
                _Article(post_id="101", author="alice", text="new one @target", created_at=now - timedelta(minutes=20)),
                _Article(post_id="102", author="bob", text="new two @target", created_at=now - timedelta(minutes=10)),
                _Article(post_id="103", author="carol", text="new three @target", created_at=now - timedelta(minutes=5)),
            ]
        )  # type: ignore[assignment]
        self.adapter._looks_like_notifications_page = AsyncMock(return_value=True)  # type: ignore[method-assign]
        self.adapter._open_notifications_mentions_tab = AsyncMock(return_value=True)  # type: ignore[method-assign]
        self.adapter._extract_article_metrics = AsyncMock(return_value={})  # type: ignore[method-assign]

        rows = await self.adapter.read_mentions(
            "@target",
            hours_back=2,
            limit=2,
            min_scroll_rounds=1,
            max_scroll_rounds=1,
        )

        self.assertEqual([row.platform_post_id for row in rows], ["101", "102"])
        self.assertEqual(len(rows), 2)

    async def test_mentions_validates_handle_limit_and_time_window(self) -> None:
        with self.assertRaisesRegex(ValueError, "account_handle must be a valid X handle"):
            await self.adapter.read_mentions("bad/name")
        with self.assertRaisesRegex(ValueError, "limit must be a positive integer"):
            await self.adapter.read_mentions("target", limit=0)
        with self.assertRaisesRegex(ValueError, "hours_back must be a positive integer"):
            await self.adapter.read_mentions("target", hours_back=0)
        with self.assertRaisesRegex(ValueError, "max_scroll_rounds"):
            await self.adapter.read_mentions("target", min_scroll_rounds=3, max_scroll_rounds=2)

    async def test_profile_recent_metrics_validates_handle_and_limit(self) -> None:
        for username in ("", "bad/name", "home"):
            with self.subTest(username=username), self.assertRaisesRegex(ValueError, "username must be a valid X handle"):
                await self.adapter.profile_recent_metrics(username)
        self.adapter._goto = AsyncMock()  # type: ignore[method-assign]
        self.assertEqual(await self.adapter.profile_recent_metrics("valid_handle", limit=0), [])
        self.adapter._goto.assert_not_awaited()  # type: ignore[attr-defined]
        with self.assertRaisesRegex(ValueError, "limit must be a non-negative integer"):
            await self.adapter.profile_recent_metrics("valid_handle", limit=-1)

    async def test_profile_recent_metrics_honors_exact_limit(self) -> None:
        now = datetime.now(timezone.utc)
        articles = [
            _Article(post_id=str(post_id), author="valid_handle", text=f"post {post_id}", created_at=now)
            for post_id in (201, 202, 203)
        ]
        self.adapter.page = _Page(articles)  # type: ignore[assignment]
        self.adapter._goto = AsyncMock()  # type: ignore[method-assign]
        self.adapter._return_home = AsyncMock()  # type: ignore[method-assign]
        self.adapter._classify_profile_article = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda article, _handle: {
                "post_id": article.post_id,
                "url": f"https://x.com/valid_handle/status/{article.post_id}",
                "author": article.author,
                "text": article.text,
                "is_reply": False,
                "is_repost": False,
                "is_quote": False,
            }
        )
        self.adapter._extract_article_metrics = AsyncMock(return_value={})  # type: ignore[method-assign]

        rows = await self.adapter.profile_recent_metrics("@valid_handle", limit=1, source="posts")

        self.assertEqual([row["post_id"] for row in rows], ["201"])
        self.adapter._goto.assert_awaited_once_with("https://x.com/valid_handle")  # type: ignore[attr-defined]
        self.adapter._return_home.assert_awaited_once()  # type: ignore[attr-defined]

    async def test_media_preflight_rejects_extension_only_spoof(self) -> None:
        fake_png = Path(self.tmp.name) / "not-really-an-image.png"
        fake_png.write_bytes(b"plain text with a png extension")

        result = await self.adapter.attach_images_preflight(fake_png)

        self.assertFalse(result.ok)
        self.assertEqual(result.errors[0]["reason"], "invalid_image_signature")
        self.assertEqual(result.normalized_paths, [])

    async def test_article_summary_uses_outer_post_when_nested_quote_link_comes_first(self) -> None:
        article = _QuotedArticle(outer_post_id="222", quote_post_id="111")
        self.adapter._extract_article_metrics = AsyncMock(return_value={})  # type: ignore[method-assign]
        self.adapter._extract_article_media = AsyncMock(return_value=[])  # type: ignore[method-assign]

        summary = await self.adapter._article_summary(article)

        self.assertEqual(summary["post_id"], "222")
        self.assertEqual(summary["url"], "https://x.com/owner/status/222")

    async def test_thread_context_distinguishes_parent_target_and_replies(self) -> None:
        now = datetime.now(timezone.utc)
        articles = [
            _Article(post_id=post_id, author="owner", text=post_id, created_at=now)
            for post_id in ("10", "20", "30", "40")
        ]
        self.adapter.page = _Page(articles)  # type: ignore[assignment]
        self.adapter._open_post_page = AsyncMock(return_value=True)  # type: ignore[method-assign]
        self.adapter._article_summary = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda article, **_kwargs: {
                "post_id": article.post_id,
                "author": article.author,
                "text": article.text,
            }
        )

        rows = await self.adapter.read_post_thread_context(
            "20",
            limit=3,
            include_parent=False,
            include_target=True,
            include_replies=True,
        )

        self.assertEqual([row.platform_post_id for row in rows], ["20", "30", "40"])
        self.assertEqual([row.raw["thread_role"] for row in rows], ["target", "reply", "reply"])

    async def test_debug_snapshot_defaults_to_redacted_and_refuses_collision(self) -> None:
        output_dir = Path(self.tmp.name) / "snapshot"

        result = await self.adapter.debug_snapshot(output_dir)
        manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))

        self.assertFalse(manifest["artifact_security"]["contains_page_content"])
        self.assertEqual(manifest["articles"], [])
        self.assertEqual(manifest["visible_dialogs"], [])
        self.assertFalse((output_dir / "page.html").exists())
        self.assertFalse((output_dir / "page.png").exists())
        self.assertEqual(result["manifest_path"], str(output_dir / "manifest.json"))
        with self.assertRaisesRegex(FileExistsError, "debug_snapshot_target_exists:manifest.json"):
            await self.adapter.debug_snapshot(output_dir)

    async def test_debug_snapshot_redacts_parser_warning_urls_by_default(self) -> None:
        raw_url = "https://x.com/search?q=private+term#fragment"
        self.adapter._recent_parser_warnings = [
            {
                "category": "test",
                "reason": "parse",
                "context": "query",
                "url": raw_url,
            }
        ]
        output_dir = Path(self.tmp.name) / "snapshot-warning"

        result = await self.adapter.debug_snapshot(output_dir)
        manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(result["recent_parser_warnings"][0]["url"], "https://x.com/search")
        self.assertEqual(manifest["recent_parser_warnings"][0]["url"], "https://x.com/search")
        self.assertEqual(self.adapter._recent_parser_warnings[0]["url"], raw_url)

    async def test_health_check_scopes_restrictions_to_system_messages(self) -> None:
        class _HealthPage:
            url = "https://x.com/home"

        self.adapter.page = _HealthPage()  # type: ignore[assignment]
        self.adapter.login_state = AsyncMock(  # type: ignore[method-assign]
            return_value=LoginState(
                logged_in=True,
                page_state="home",
                url="https://x.com/home",
                browser_started=True,
            )
        )
        self.adapter._visible_system_messages = AsyncMock(  # type: ignore[method-assign]
            return_value=["Rate limit reached. Try again later."]
        )
        self.adapter._visible_dialogs = AsyncMock(return_value=[])  # type: ignore[method-assign]

        health = await self.adapter.health_check()

        self.assertTrue(health.rate_limited)
        self.assertFalse(health.account_locked)
        self.assertFalse(health.blocking_modal_present)
        self.assertEqual(health.raw["visible_system_messages"], ["Rate limit reached. Try again later."])

    async def test_health_check_degrades_closed_target_without_raising(self) -> None:
        class _ClosedPage:
            url = "https://x.com/home"

        self.adapter.page = _ClosedPage()  # type: ignore[assignment]
        self.adapter.login_state = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("Target page, context or browser has been closed")
        )

        health = await self.adapter.health_check()

        self.assertFalse(health.logged_in)
        self.assertTrue(health.login_required)
        self.assertEqual(health.current_state, "unavailable")
        self.assertEqual(health.raw["degraded_reason"], "target_closed")

    async def test_selector_wait_uses_active_playwright_mode(self) -> None:
        async_page = type("AsyncPage", (), {})()
        async_page.wait_for_selector = AsyncMock(return_value="async-result")
        self.adapter.page = async_page  # type: ignore[assignment]

        self.assertEqual(await self.adapter._wait_for_selector_compat("article", 250), "async-result")
        async_page.wait_for_selector.assert_awaited_once_with("article", timeout=250)

        class _SyncPage:
            def wait_for_selector(self, selector: str, *, timeout: int) -> tuple[str, int]:
                return selector, timeout

        sync_page = _SyncPage()
        self.adapter._sync_mode = True
        self.adapter._sync_page = sync_page  # type: ignore[assignment]
        self.adapter._run_sync = AsyncMock(return_value="sync-result")  # type: ignore[method-assign]

        self.assertEqual(await self.adapter._wait_for_selector_compat("article", 300), "sync-result")
        self.adapter._run_sync.assert_awaited_once_with(  # type: ignore[attr-defined]
            sync_page.wait_for_selector,
            "article",
            timeout=300,
        )


if __name__ == "__main__":
    unittest.main()
