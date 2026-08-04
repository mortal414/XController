from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest

try:
    from XController import ActionResult, LoginState, SyncXController, TimelineReadResult
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
    from XController import ActionResult, LoginState, SyncXController, TimelineReadResult


class FakeAsyncAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.closed = False

    def _record(self, name: str, *args, **kwargs) -> None:
        self.calls.append((name, args, kwargs))

    async def start(self) -> None:
        self._record("start")

    async def close(self) -> None:
        self._record("close")
        self.closed = True

    async def login_state(self) -> LoginState:
        self._record("login_state")
        return LoginState(logged_in=True, page_state="home", url="https://x.com/home", browser_started=True)

    async def read_timeline_detailed(
        self,
        limit: int = 20,
        tab: str = "for_you",
        force_refresh: bool = False,
        reset_scroll: bool = False,
    ) -> TimelineReadResult:
        self._record(
            "read_timeline_detailed",
            limit=limit,
            tab=tab,
            force_refresh=force_refresh,
            reset_scroll=reset_scroll,
        )
        return TimelineReadResult(
            posts=[],
            requested_tab=tab,
            active_tab=tab,
            source_url="https://x.com/home",
            current_state="home",
            raw_count=0,
            article_count=0,
            force_refreshed=force_refresh,
            reset_scroll=reset_scroll,
        )

    async def like_post_detailed(self, platform_post_id: str) -> ActionResult:
        self._record("like_post_detailed", platform_post_id)
        return ActionResult(ok=True, action="like", target_post_id=platform_post_id)

    async def delete_post_detailed(self, platform_post_id: str, kind: str = "post") -> ActionResult:
        self._record("delete_post_detailed", platform_post_id, kind=kind)
        return ActionResult(ok=True, action="delete", target_post_id=platform_post_id, raw={"kind": kind})

    async def delete_quote(self, platform_post_id: str) -> bool:
        self._record("delete_quote", platform_post_id)
        return True

    async def delete_all_posts(self) -> list[str]:
        self._record("delete_all_posts")
        return ["https://x.com/i/web/status/456"]

    async def delete_all_quotes(self) -> list[str]:
        self._record("delete_all_quotes")
        return ["https://x.com/i/web/status/789"]

    async def read_notifications(self, limit: int = 20, unread_only: bool = False) -> list:
        self._record("read_notifications", limit=limit, unread_only=unread_only)
        return []


class SyncFacadeTests(unittest.TestCase):
    def test_sync_facade_lifecycle_and_forwarding_without_browser(self) -> None:
        adapter = FakeAsyncAdapter()
        service = SyncXController(adapter=adapter)  # type: ignore[arg-type]

        service.start()
        login = service.login_state()
        timeline = service.read_timeline_detailed(limit=2, tab="following", force_refresh=True, reset_scroll=True)
        like = service.like_post_detailed("123")
        delete = service.delete_post_detailed("456", kind="reply")
        quote_deleted = service.delete_quote("789")
        deleted_posts = service.delete_all_posts()
        deleted_quotes = service.delete_all_quotes()
        notifications = service.read_notifications(limit=3, unread_only=True)
        service.close()
        service.close()

        self.assertTrue(adapter.closed)
        self.assertTrue(login.logged_in)
        self.assertEqual(timeline.requested_tab, "following")
        self.assertTrue(timeline.force_refreshed)
        self.assertTrue(like.ok)
        self.assertTrue(delete.ok)
        self.assertTrue(quote_deleted)
        self.assertEqual(deleted_posts, ["https://x.com/i/web/status/456"])
        self.assertEqual(deleted_quotes, ["https://x.com/i/web/status/789"])
        self.assertEqual(notifications, [])
        self.assertIn(("start", (), {}), adapter.calls)
        self.assertIn(("like_post_detailed", ("123",), {}), adapter.calls)
        self.assertIn(("delete_post_detailed", ("456",), {"kind": "reply"}), adapter.calls)
        self.assertIn(("delete_quote", ("789",), {}), adapter.calls)
        self.assertIn(("delete_all_posts", (), {}), adapter.calls)
        self.assertIn(("delete_all_quotes", (), {}), adapter.calls)
        self.assertIn(("read_notifications", (), {"limit": 3, "unread_only": True}), adapter.calls)
        self.assertEqual([name for name, _args, _kwargs in adapter.calls].count("close"), 1)


if __name__ == "__main__":
    unittest.main()

