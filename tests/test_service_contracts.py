from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock

try:
    from XController import (
        AccountStats,
        ActionFailureInfo,
        ActionPreflight,
        ActionResult,
        ControllerSettings,
        LoginState,
        MediaCaptureData,
        MediaPreflight,
        TimelineReadResult,
        XTextAdapter,
    )
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
    from XController import (
        AccountStats,
        ActionFailureInfo,
        ActionPreflight,
        ActionResult,
        ControllerSettings,
        LoginState,
        MediaCaptureData,
        MediaPreflight,
        TimelineReadResult,
        XTextAdapter,
    )


class ServiceContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _adapter(self, settings: ControllerSettings | dict | None = None) -> XTextAdapter:
        adapter = XTextAdapter(profile_path=str(self.tmp_path / "profile"), settings=settings)
        adapter._mutation_guard = AsyncMock(return_value=(True, "", {}))  # type: ignore[method-assign]
        adapter._mutation_commit_guard = AsyncMock(return_value=(True, "", {}))  # type: ignore[method-assign]
        adapter._snapshot_recent_owned_ids = AsyncMock(return_value=set())  # type: ignore[method-assign]
        self.addCleanup(adapter._shutdown_executor)
        return adapter

    def test_action_models_serialize_to_plain_dicts(self) -> None:
        result = ActionResult(ok=False, action="reply", target_post_id="123", failure_reason="reply_limited")
        preflight = ActionPreflight(ok=False, action="reply", target_post_id="123", reason="reply_limited")
        timeline = TimelineReadResult(posts=[], requested_tab="for_you", active_tab="following", source_url="", current_state="home", raw_count=0, article_count=0)
        media = MediaPreflight(ok=False, errors=[{"path": "x.bmp", "reason": "unsupported_extension"}])
        capture = MediaCaptureData(kind="image", path="artifact.png", target_post_id="123", source_url="https://x.com/media")
        login = LoginState(logged_in=True, page_state="home", url="https://x.com/home", browser_started=True)
        account = AccountStats(handle="example", followers=10, raw={"source": "test"})

        self.assertEqual(result.to_dict()["failure_reason"], "reply_limited")
        self.assertEqual(preflight.to_dict()["reason"], "reply_limited")
        self.assertEqual(timeline.to_dict()["active_tab"], "following")
        self.assertEqual(media.to_dict()["errors"][0]["reason"], "unsupported_extension")
        self.assertEqual(capture.to_dict()["target_post_id"], "123")
        self.assertTrue(login.to_dict()["logged_in"])
        self.assertEqual(account.to_dict()["followers"], 10)
        self.assertEqual(account.to_dict()["raw"]["source"], "test")

    def test_account_stats_payload_normalizes_profile_counts(self) -> None:
        adapter = self._adapter()
        payload = {
            "current_url": "https://x.com/example",
            "title": "Example User (@example) / X",
            "user_name_blocks": ["Example User\n@example\n1.2K posts"],
            "bio": "Building things",
            "location": "Chicago, IL",
            "joined_at": "Joined January 2024",
            "verified": True,
            "links": [
                {"href": "https://x.com/example/following", "text": "1,234 Following"},
                {"href": "https://x.com/example/verified_followers", "text": "5.6K Followers"},
            ],
        }

        stats = adapter._account_stats_from_payload("example", payload, "2026-04-24T00:00:00+00:00", {"warnings": []})

        self.assertEqual(stats.handle, "example")
        self.assertEqual(stats.display_name, "Example User")
        self.assertEqual(stats.followers, 5600)
        self.assertEqual(stats.following, 1234)
        self.assertEqual(stats.posts, 1200)
        self.assertTrue(stats.verified)
        self.assertEqual(stats.bio, "Building things")
        self.assertIn("likes_count_unavailable", stats.raw["warnings"])

    async def test_account_stats_invalid_explicit_handle_returns_structured_error(self) -> None:
        adapter = self._adapter()

        stats = await adapter.account_stats("bad handle")

        self.assertEqual(stats.handle, "")
        self.assertEqual(stats.raw["error"], "invalid_handle")

    def test_playwright_mode_overrides_sync_preference(self) -> None:
        self.assertFalse(self._adapter({"playwright_mode": "async"})._prefer_sync_playwright())
        self.assertTrue(self._adapter({"playwright_mode": "sync"})._prefer_sync_playwright())
        self.assertTrue(self._adapter({"playwright_mode": "async", "prefer_sync_playwright": True})._prefer_sync_playwright())

    async def test_attach_images_preflight_reports_file_errors(self) -> None:
        adapter = self._adapter()
        good = self.tmp_path / "ok.png"
        bad = self.tmp_path / "bad.bmp"
        good.write_bytes(b"\x89PNG\r\n\x1a\nimage-data")
        bad.write_bytes(b"image")

        result = await adapter.attach_images_preflight([good, bad, self.tmp_path / "missing.jpg"])

        self.assertFalse(result.ok)
        self.assertEqual(result.normalized_paths, [str(good.resolve())])
        self.assertEqual([item["reason"] for item in result.errors], ["unsupported_extension", "file_not_found"])

    async def test_detailed_action_methods_return_structured_not_started_failures(self) -> None:
        adapter = self._adapter()

        reply = await adapter.reply_to_post_detailed("123", "hello")
        like = await adapter.like_post_detailed("123")
        post = await adapter.post_text_detailed("hello")

        self.assertFalse(reply.ok)
        self.assertEqual(reply.failure_reason, "page_not_started")
        self.assertFalse(like.ok)
        self.assertEqual(like.failure_stage, "not_started")
        self.assertFalse(post.ok)
        self.assertEqual(post.action, "post")

    async def test_like_post_detailed_does_not_replay_after_unconfirmed_attempt(self) -> None:
        class FakePage:
            url = "https://x.com/home"

        adapter = self._adapter()
        adapter.page = FakePage()  # type: ignore[assignment]
        adapter._like_in_current_context = AsyncMock(return_value=False)  # type: ignore[method-assign]
        adapter.engage_post = AsyncMock(return_value={"opened": True, "viewed": False, "liked": True})  # type: ignore[method-assign]
        adapter.preflight_action = AsyncMock(  # type: ignore[method-assign]
            return_value=ActionPreflight(ok=True, action="like", target_post_id="123")
        )

        result = await adapter.like_post_detailed("123")

        self.assertFalse(result.ok)
        self.assertEqual(result.failure_reason, "like_state_not_confirmed")
        self.assertFalse(result.confirmation_observed)
        adapter.engage_post.assert_not_awaited()
        adapter.preflight_action.assert_awaited_once_with("123", action="like")

    async def test_follow_user_detailed_includes_soft_error_diagnostic_on_failure(self) -> None:
        class FakePage:
            url = "https://x.com/example"

        adapter = self._adapter()
        adapter.page = FakePage()  # type: ignore[assignment]
        adapter.follow_user = AsyncMock(return_value=False)  # type: ignore[method-assign]
        adapter.last_action_error = ActionFailureInfo(
            action="follow_user",
            error_type="RuntimeError",
            message="follow button vanished",
            url="https://x.com/example",
            selector="button:has-text('Follow')",
        )

        result = await adapter.follow_user_detailed("example")

        self.assertFalse(result.ok)
        self.assertEqual(result.failure_stage, "target_lookup")
        self.assertEqual(result.failure_reason, "follow button vanished")
        self.assertIn("last_action_error", result.diagnostic)
        self.assertEqual(result.diagnostic["last_action_error"]["action"], "follow_user")

    async def test_reply_to_post_detailed_unknown_id_includes_confirmation_signal(self) -> None:
        class FakePage:
            url = "https://x.com/i/web/status/123"

        class NoopHuman:
            async def jitter(self, *_args, **_kwargs) -> None:
                return None

        adapter = self._adapter()
        adapter.page = FakePage()  # type: ignore[assignment]
        adapter.human = NoopHuman()
        box = object()
        send = object()
        send_after = object()
        adapter.attach_images_preflight = AsyncMock(return_value=MediaPreflight(ok=True, normalized_paths=[]))  # type: ignore[method-assign]
        adapter.preflight_action = AsyncMock(return_value=ActionPreflight(ok=True, action="reply", target_post_id="123"))  # type: ignore[method-assign]
        adapter._open_reply_box_in_current_context = AsyncMock(return_value=box)  # type: ignore[method-assign]
        adapter._clear_textbox = AsyncMock()  # type: ignore[method-assign]
        adapter._type_text = AsyncMock()  # type: ignore[method-assign]
        adapter._attach_images_to_composer = AsyncMock(return_value=True)  # type: ignore[method-assign]
        adapter._find_reply_submit_button = AsyncMock(side_effect=[send, send_after])  # type: ignore[method-assign]
        adapter._click = AsyncMock()  # type: ignore[method-assign]
        adapter._keyboard_press = AsyncMock()  # type: ignore[method-assign]
        adapter._has_reply_audience_modal = AsyncMock(return_value=False)  # type: ignore[method-assign]
        adapter._find_recent_own_created_post_id = AsyncMock(return_value=None)  # type: ignore[method-assign]
        adapter._guess_recent_post_id = AsyncMock(return_value=None)  # type: ignore[method-assign]
        adapter._find_first = AsyncMock(return_value=send_after)  # type: ignore[method-assign]
        adapter._is_button_enabled = AsyncMock(return_value=False)  # type: ignore[method-assign]
        adapter._submit_composer_once = AsyncMock(  # type: ignore[method-assign]
            return_value=(True, True, "send_button_disabled")
        )

        result = await adapter.reply_to_post_detailed("123", "hello")

        self.assertTrue(result.ok)
        self.assertEqual(result.created_post_id, "unknown_reply_id")
        self.assertEqual(result.diagnostic["confirmation_signal"]["action"], "reply")
        self.assertEqual(result.diagnostic["confirmation_signal"]["signal"], "send_button_disabled")

    async def test_quote_post_detailed_unknown_id_includes_confirmation_signal(self) -> None:
        class FakePage:
            url = "https://x.com/i/web/status/123"

        adapter = self._adapter()
        adapter.page = FakePage()  # type: ignore[assignment]
        adapter.attach_images_preflight = AsyncMock(return_value=MediaPreflight(ok=True, normalized_paths=[]))  # type: ignore[method-assign]
        adapter.preflight_action = AsyncMock(return_value=ActionPreflight(ok=True, action="quote", target_post_id="123"))  # type: ignore[method-assign]
        adapter.quote_post = AsyncMock(return_value="unknown_quote_id")  # type: ignore[method-assign]
        adapter._last_unknown_id_confirmation_signal = {"action": "quote", "signal": "box_empty"}

        result = await adapter.quote_post_detailed("123", text="hello")

        self.assertTrue(result.ok)
        self.assertEqual(result.created_post_id, "unknown_quote_id")
        self.assertEqual(result.diagnostic["confirmation_signal"]["action"], "quote")
        self.assertEqual(result.diagnostic["confirmation_signal"]["signal"], "box_empty")

    async def test_reply_to_post_detailed_without_composer_has_stage_and_reason(self) -> None:
        class FakePage:
            url = "https://x.com/i/web/status/123"

        class NoopHuman:
            async def jitter(self, *_args, **_kwargs) -> None:
                return None

        adapter = self._adapter()
        adapter.page = FakePage()  # type: ignore[assignment]
        adapter.human = NoopHuman()
        adapter.attach_images_preflight = AsyncMock(return_value=MediaPreflight(ok=True, normalized_paths=[]))  # type: ignore[method-assign]
        adapter.preflight_action = AsyncMock(return_value=ActionPreflight(ok=True, action="reply", target_post_id="123"))  # type: ignore[method-assign]
        adapter._open_reply_box_in_current_context = AsyncMock(return_value=None)  # type: ignore[method-assign]
        adapter._dismiss_reply_ui = AsyncMock()  # type: ignore[method-assign]
        adapter._random_scroll = AsyncMock()  # type: ignore[method-assign]

        result = await adapter.reply_to_post_detailed("123", "hello")

        self.assertFalse(result.ok)
        self.assertEqual(result.failure_stage, "post_submit")
        self.assertEqual(result.failure_reason, "submit_not_confirmed")

    async def test_created_post_id_resolver_matches_recent_owned_text(self) -> None:
        class FakePage:
            url = "https://x.com/home"

        adapter = self._adapter()
        adapter.page = FakePage()  # type: ignore[assignment]
        adapter._get_authenticated_handle = AsyncMock(return_value="adam_smasha")  # type: ignore[method-assign]
        adapter._collect_recent_owned_created_candidates = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                {"post_id": "123", "text": "unrelated post"},
                {"post_id": "2054320737582805502", "text": "xbox 360 Netflix Party Mode: actual avatars, fake couch, felt like hanging out"},
            ]
        )

        post_id = await adapter._find_recent_own_created_post_id(
            "post",
            "xbox 360 Netflix Party Mode: actual avatars, fake couch, felt like hanging out.",
        )

        self.assertEqual(post_id, "2054320737582805502")

    async def test_created_post_id_resolver_excludes_target_post(self) -> None:
        class FakePage:
            url = "https://x.com/i/web/status/123"

        adapter = self._adapter()
        adapter.page = FakePage()  # type: ignore[assignment]
        adapter._get_authenticated_handle = AsyncMock(return_value="adam_smasha")  # type: ignore[method-assign]
        adapter._collect_recent_owned_created_candidates = AsyncMock(return_value=[{"post_id": "123", "text": "same text"}])  # type: ignore[method-assign]

        post_id = await adapter._find_recent_own_created_post_id("reply", "same text", target_post_id="123")

        self.assertIsNone(post_id)

    async def test_created_reply_id_resolver_accepts_owned_text_when_reply_flag_is_missing(self) -> None:
        class FakePage:
            url = "https://x.com/home"

        adapter = self._adapter()
        adapter.page = FakePage()  # type: ignore[assignment]
        adapter._get_authenticated_handle = AsyncMock(return_value="adam_smasha")  # type: ignore[method-assign]
        adapter._collect_recent_owned_created_candidates = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                {
                    "post_id": "2054320737582805502",
                    "author": "adam_smasha",
                    "text": "Feels like trying is generous, most folks just want a quick headline to dunk on",
                    "is_reply": False,
                    "is_quote": False,
                    "source_surface": "with_replies",
                }
            ]
        )

        post_id = await adapter._find_recent_own_created_post_id(
            "reply",
            "Feels like trying is generous, most folks just want a quick headline to dunk on, not read credits",
            target_post_id="2054323038070485101",
        )

        self.assertEqual(post_id, "2054320737582805502")

    async def test_recent_post_guess_does_not_scan_page_content_by_default(self) -> None:
        class FakePage:
            url = "https://x.com/home"

        adapter = self._adapter()
        adapter.page = FakePage()  # type: ignore[assignment]
        adapter._page_content = AsyncMock(return_value='href="https://x.com/someone/status/999"')  # type: ignore[method-assign]

        self.assertIsNone(await adapter._guess_recent_post_id())
        self.assertEqual(await adapter._guess_recent_post_id(allow_page_content=True), "999")

    async def test_return_home_is_public_navigation_entrypoint(self) -> None:
        adapter = self._adapter()

        self.assertFalse(await adapter.return_home())

    async def test_status_page_sidebar_home_link_does_not_count_as_home(self) -> None:
        class FakeLocator:
            @property
            def first(self) -> "FakeLocator":
                return self

        class FakePage:
            url = "https://x.com/i/web/status/123"

            def locator(self, _selector: str) -> FakeLocator:
                return FakeLocator()

        adapter = self._adapter()
        adapter.page = FakePage()  # type: ignore[assignment]

        async def fake_any_selector(selectors: list[str]) -> bool:
            return 'a[href="/home"]' in selectors or 'a[data-testid="AppTabBar_Home_Link"]' in selectors

        adapter._any_selector = AsyncMock(side_effect=fake_any_selector)
        adapter._count_locator = AsyncMock(return_value=0)

        self.assertFalse(await adapter._looks_like_home_timeline())

    async def test_current_surface_includes_legacy_and_explicit_state_keys(self) -> None:
        adapter = self._adapter()
        adapter.current_state = AsyncMock(return_value={"state": "status", "url": "https://x.com/i/web/status/123"})  # type: ignore[method-assign]
        adapter._active_home_tab = AsyncMock(return_value="")

        surface = await adapter.current_surface()

        self.assertEqual(surface["state"], "status")
        self.assertEqual(surface["current_state"], "status")
        self.assertEqual(surface["url"], "https://x.com/i/web/status/123")
        self.assertEqual(surface["active_tab"], "")

    async def test_timeline_force_refresh_uses_return_home_reload_path(self) -> None:
        class FakePage:
            url = "https://x.com/home"

            def locator(self, _selector: str) -> object:
                return object()

        class NoopHuman:
            async def jitter(self, *_args, **_kwargs) -> None:
                return None

        adapter = self._adapter()
        adapter.page = FakePage()  # type: ignore[assignment]
        adapter.human = NoopHuman()
        adapter.return_home = AsyncMock(return_value=True)  # type: ignore[method-assign]
        adapter._select_home_tab = AsyncMock(return_value=True)
        adapter._active_home_tab = AsyncMock(return_value="for_you")
        adapter._count_locator = AsyncMock(return_value=0)
        adapter._collect_posts_from_current_page = AsyncMock(return_value=[])
        adapter._goto = AsyncMock()
        adapter._current_state_name = AsyncMock(return_value="home")

        result = await adapter.read_timeline_detailed(limit=1, force_refresh=True)

        adapter.return_home.assert_awaited_once_with(force_refresh=True)
        self.assertTrue(result.force_refreshed)

    async def test_timeline_reset_scroll_presses_home_without_force_refresh(self) -> None:
        class FakePage:
            url = "https://x.com/home"

            def locator(self, _selector: str) -> object:
                return object()

        class NoopHuman:
            async def jitter(self, *_args, **_kwargs) -> None:
                return None

        adapter = self._adapter()
        adapter.page = FakePage()  # type: ignore[assignment]
        adapter.human = NoopHuman()
        adapter.settle_home = AsyncMock(return_value=True)  # type: ignore[method-assign]
        adapter.return_home = AsyncMock(return_value=True)  # type: ignore[method-assign]
        adapter._keyboard_press = AsyncMock()
        adapter._active_home_tab = AsyncMock(return_value="for_you")
        adapter._count_locator = AsyncMock(return_value=0)
        adapter._collect_posts_from_current_page = AsyncMock(return_value=[])
        adapter._goto = AsyncMock()
        adapter._select_home_tab = AsyncMock(return_value=True)
        adapter._current_state_name = AsyncMock(return_value="home")

        result = await adapter.read_timeline_detailed(limit=1, reset_scroll=True)

        adapter.settle_home.assert_awaited_once_with("for_you")
        adapter.return_home.assert_not_awaited()
        adapter._keyboard_press.assert_awaited_once_with("Home")
        self.assertTrue(result.reset_scroll)

    async def test_settle_after_action_can_refresh_select_tab_and_reset_scroll(self) -> None:
        class FakePage:
            url = "https://x.com/status/123"

        class NoopHuman:
            async def jitter(self, *_args, **_kwargs) -> None:
                return None

        adapter = self._adapter()
        adapter.page = FakePage()  # type: ignore[assignment]
        adapter.human = NoopHuman()
        adapter.return_home = AsyncMock(return_value=True)  # type: ignore[method-assign]
        adapter._select_home_tab = AsyncMock(return_value=True)
        adapter._keyboard_press = AsyncMock()

        settled = await adapter.settle_after_action(tab="For You", force_refresh=True, reset_scroll=True)

        self.assertTrue(settled)
        adapter.return_home.assert_awaited_once_with(force_refresh=True)
        adapter._select_home_tab.assert_awaited_once_with("for_you")
        adapter._keyboard_press.assert_awaited_once_with("Home")

    async def test_login_state_shapes_for_not_started_login_and_logged_in(self) -> None:
        class FakePage:
            def __init__(self, url: str) -> None:
                self.url = url

            def locator(self, _selector: str) -> object:
                return object()

        adapter = self._adapter()

        not_started = await adapter.login_state()
        self.assertFalse(not_started.logged_in)
        self.assertEqual(not_started.page_state, "not_started")
        self.assertTrue(not_started.login_required)

        adapter.page = FakePage("https://x.com/i/flow/login")  # type: ignore[assignment]
        adapter.current_state = AsyncMock(return_value={"state": "login", "url": "https://x.com/i/flow/login"})  # type: ignore[method-assign]
        adapter._any_selector = AsyncMock(side_effect=[True, False])
        adapter._count_locator = AsyncMock(return_value=0)
        adapter._active_home_tab = AsyncMock(return_value="")

        login = await adapter.login_state()
        self.assertFalse(login.logged_in)
        self.assertEqual(login.page_state, "login")
        self.assertEqual(login.url, "https://x.com/i/flow/login")

        adapter.current_state = AsyncMock(return_value={"state": "home", "url": "https://x.com/home"})  # type: ignore[method-assign]
        adapter._any_selector = AsyncMock(side_effect=[False, True])
        adapter._active_home_tab = AsyncMock(return_value="for_you")

        logged_in = await adapter.login_state()
        self.assertTrue(logged_in.logged_in)
        self.assertEqual(logged_in.page_state, "home")
        self.assertEqual(logged_in.active_home_tab, "for_you")

        adapter.current_state = AsyncMock(
            return_value={"state": "unknown", "url": "https://x.com/i/jf/onboarding/web?mode=login"}
        )  # type: ignore[method-assign]
        adapter._any_selector = AsyncMock(side_effect=[False, True])
        adapter._count_locator = AsyncMock(return_value=0)
        adapter._active_home_tab = AsyncMock(return_value="")

        onboarding_login = await adapter.login_state()
        self.assertFalse(onboarding_login.logged_in)
        self.assertTrue(onboarding_login.login_required)

    async def test_capture_post_media_returns_normalized_captures(self) -> None:
        class FakePage:
            url = "https://x.com/example/status/123"

        article = object()
        adapter = self._adapter()
        adapter.page = FakePage()  # type: ignore[assignment]
        adapter._open_post_page = AsyncMock(return_value=True)
        adapter._resolve_target_article = AsyncMock(return_value=article)
        capture_path = str((self.tmp_path / "captures" / "123_image_1.png").resolve())
        adapter._capture_article_media_nodes = AsyncMock(
            return_value=[
                MediaCaptureData(
                    kind="image",
                    path=capture_path,
                    target_post_id="123",
                    source_url="https://pbs.twimg.com/media/example.jpg",
                    alt_text="example",
                )
            ]
        )

        captures = await adapter.capture_post_media("https://x.com/example/status/123", self.tmp_path / "captures")

        self.assertEqual(len(captures), 1)
        self.assertEqual(captures[0].to_dict()["kind"], "image")
        self.assertEqual(captures[0].target_post_id, "123")
        adapter._capture_article_media_nodes.assert_awaited_once()

    def test_removed_alias_methods_are_not_public(self) -> None:
        adapter = self._adapter()

        for name in ("read_unread_notifications", "recover_home", "refresh_home", "comment_post"):
            self.assertFalse(hasattr(adapter, name), name)


if __name__ == "__main__":
    unittest.main()
