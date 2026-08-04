from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import asyncio
import unittest
from unittest.mock import AsyncMock

from XController import XTextAdapter


class NoopHuman:
    async def jitter(self, *_args, **_kwargs) -> None:
        return None

    async def wait_for_network_idle(self, *_args, **_kwargs) -> None:
        return None


class PageStub:
    def __init__(self, url: str, *, closed: bool = False, redirect_to: str = "") -> None:
        self.url = url
        self.closed = closed
        self.redirect_to = redirect_to
        self.goto_calls: list[str] = []

    def is_closed(self) -> bool:
        return self.closed

    async def goto(self, url: str, **_kwargs) -> None:
        self.goto_calls.append(url)
        self.url = self.redirect_to or url

    async def wait_for_load_state(self, *_args, **_kwargs) -> None:
        return None


class Candidate:
    def __init__(self, *, visible: bool, enabled: bool = True, post_id: str = "") -> None:
        self.visible = visible
        self.enabled = enabled
        self.post_id = post_id

    async def is_visible(self) -> bool:
        return self.visible

    async def is_enabled(self) -> bool:
        return self.enabled

    async def get_attribute(self, _name: str):
        return None


class CandidateList:
    def __init__(self, candidates: list[Candidate]) -> None:
        self.candidates = candidates

    async def count(self) -> int:
        return len(self.candidates)

    def nth(self, index: int) -> Candidate:
        return self.candidates[index]


class LocatorPage(PageStub):
    def __init__(self, candidates: list[Candidate]) -> None:
        super().__init__("https://x.com/home")
        self.candidates = CandidateList(candidates)

    def locator(self, _selector: str) -> CandidateList:
        return self.candidates


class FailIfQueriedPage(PageStub):
    def locator(self, _selector: str):
        raise AssertionError("untrusted page must not be queried")


class RuntimeHardeningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.adapter = XTextAdapter(profile_path=str(Path(self.tmp.name) / "profile"))
        self.adapter.human = NoopHuman()

    def tearDown(self) -> None:
        self.adapter._shutdown_executor()
        self.tmp.cleanup()

    def test_rejects_empty_profile_path(self) -> None:
        for value in ("", "   "):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "profile_path"):
                XTextAdapter(profile_path=value)

        profile = Path(self.adapter.profile_path)
        self.assertTrue(profile.is_absolute())
        self.assertTrue(profile.is_dir())

    def test_post_id_normalization_accepts_only_plain_ids_or_trusted_status_urls(self) -> None:
        accepted = {
            "123": "123",
            " https://x.com/user/status/456 ": "456",
            "https://www.x.com/i/web/status/789/photo/1?ref=ok": "789",
        }
        for value, expected in accepted.items():
            self.assertEqual(self.adapter._normalize_post_id(value), expected)

        for value in (
            "prefix123",
            "https://evil.example/user/status/123",
            "http://x.com/user/status/123",
            "https://x.com.evil.example/user/status/123",
            "https://x.com/user/status/123suffix",
            "/user/status/123",
        ):
            self.assertEqual(self.adapter._normalize_post_id(value), "", value)

    async def test_navigation_rejects_external_origin_before_goto(self) -> None:
        page = PageStub("about:blank")
        with self.assertRaisesRegex(ValueError, "trusted_x_origin"):
            await self.adapter._page_goto(page, "https://example.com/status/123")
        self.assertEqual(page.goto_calls, [])

    async def test_navigation_detaches_page_after_external_redirect(self) -> None:
        page = PageStub("about:blank", redirect_to="https://example.com/phish")
        self.adapter.page = page  # type: ignore[assignment]
        with self.assertRaisesRegex(RuntimeError, "trusted_x_origin"):
            await self.adapter._page_goto(page, "https://x.com/home")
        self.assertIsNone(self.adapter.page)

    async def test_refresh_skips_closed_and_external_pages(self) -> None:
        closed = PageStub("https://x.com/home", closed=True)
        external = PageStub("https://example.com/")
        trusted = PageStub("https://www.x.com/notifications")

        class Context:
            pages = [closed, external, trusted]

            async def new_page(self):
                raise AssertionError("a trusted existing page should be selected")

        self.adapter.context = Context()  # type: ignore[assignment]
        await self.adapter._refresh_page_handle()
        self.assertIs(self.adapter.page, trusted)

    async def test_find_helpers_skip_hidden_and_disabled_candidates(self) -> None:
        hidden = Candidate(visible=False)
        disabled = Candidate(visible=True, enabled=False)
        enabled = Candidate(visible=True, enabled=True)
        self.adapter.page = LocatorPage([hidden, disabled, enabled])  # type: ignore[assignment]

        self.assertIs(await self.adapter._find_first(["button"]), disabled)
        self.assertIs(await self.adapter._find_first_enabled(["button"]), enabled)
        self.assertIs(
            await self.adapter._find_first_in_scope(self.adapter.page, ["button"], require_enabled=True),
            enabled,
        )
        self.assertEqual(await self.adapter._visible_scope_count('[role="dialog"]'), 2)

    async def test_click_is_native_and_at_most_once(self) -> None:
        locator = unittest.mock.MagicMock()
        locator.click = AsyncMock(side_effect=RuntimeError("blocked"))
        locator.evaluate = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, "blocked"):
            await self.adapter._click(locator)
        locator.click.assert_awaited_once_with(timeout=3500)
        locator.evaluate.assert_not_awaited()

    async def test_exact_article_selection_rejects_id_prefix_and_hidden_match(self) -> None:
        prefix = Candidate(visible=True, post_id="1234")
        hidden = Candidate(visible=False, post_id="123")
        exact = Candidate(visible=True, post_id="123")
        stable_exact = Candidate(visible=True, post_id="123")
        self.adapter.page = LocatorPage([prefix, hidden, exact])  # type: ignore[assignment]
        self.adapter._article_primary_post_id = AsyncMock(side_effect=lambda item: item.post_id)
        self.adapter._bind_locator = AsyncMock(return_value=(stable_exact, "stable-123"))  # type: ignore[method-assign]

        self.assertIs(await self.adapter._find_exact_article("123"), stable_exact)
        self.adapter._bind_locator.assert_awaited_once_with(exact, "article-123")  # type: ignore[attr-defined]
        self.assertIsNone(await self.adapter._resolve_target_article("999"))

    async def test_exact_article_revalidates_stable_binding_before_return(self) -> None:
        exact = Candidate(visible=True, post_id="123")
        detached = Candidate(visible=False, post_id="123")
        self.adapter.page = LocatorPage([exact])  # type: ignore[assignment]
        self.adapter._article_primary_post_id = AsyncMock(side_effect=lambda item: item.post_id)
        self.adapter._bind_locator = AsyncMock(return_value=(detached, "stable-123"))  # type: ignore[method-assign]

        self.assertIsNone(await self.adapter._find_exact_article("123"))

    async def test_owned_lookup_does_not_coerce_caller_kind(self) -> None:
        article = object()
        self.adapter.page = PageStub("https://x.com/home")  # type: ignore[assignment]
        self.adapter._goto = AsyncMock()
        self.adapter._find_post_article_in_context = AsyncMock(return_value=article)
        self.adapter._classify_profile_article = AsyncMock(
            return_value={
                "post_id": "123",
                "author": "owner",
                "is_reply": False,
                "is_repost": False,
                "is_quote": False,
            }
        )
        self.assertIsNone(await self.adapter._find_owned_status_article_by_post_id("owner", "123", "reply"))

    async def test_close_clears_every_reference_even_when_async_close_fails(self) -> None:
        context = unittest.mock.MagicMock()
        context.close = AsyncMock(side_effect=RuntimeError("close failed"))
        playwright = unittest.mock.MagicMock()
        playwright.stop = AsyncMock()
        self.adapter.context = context
        self.adapter.page = object()  # type: ignore[assignment]
        self.adapter.playwright = playwright

        with self.assertRaisesRegex(RuntimeError, "close failed"):
            await self.adapter.close()
        playwright.stop.assert_awaited_once()
        self.assertIsNone(self.adapter.context)
        self.assertIsNone(self.adapter.page)
        self.assertIsNone(self.adapter.playwright)
        self.assertIsNone(self.adapter._executor)

    async def test_successful_action_result_has_no_failure_stage(self) -> None:
        result = await self.adapter._action_result("like", ok=True, failure_stage="unknown")
        self.assertEqual(result.failure_stage, "")

    async def test_state_and_action_lookup_do_not_use_external_page(self) -> None:
        page = unittest.mock.MagicMock()
        page.url = "https://example.com/status/123"
        page.is_closed.return_value = False
        page.locator.side_effect = AssertionError("external page must not be queried")
        self.adapter.page = page

        self.assertEqual((await self.adapter.current_state())["state"], "unknown")
        self.assertIsNone(await self.adapter._find_post_article_in_context("123"))

    async def test_click_navigation_helpers_never_query_untrusted_page(self) -> None:
        self.adapter.page = FailIfQueriedPage("https://example.com/phish")  # type: ignore[assignment]

        self.assertFalse(await self.adapter._open_home_via_click())
        self.assertFalse(await self.adapter._select_home_tab("for_you"))
        self.assertFalse(await self.adapter._open_notifications_via_click())
        self.assertFalse(await self.adapter._open_notifications_mentions_tab())
        self.assertFalse(await self.adapter._set_search_mode("live"))

    async def test_return_home_never_navigates_back_from_untrusted_page(self) -> None:
        self.adapter.page = FailIfQueriedPage("https://example.com/phish")  # type: ignore[assignment]
        self.adapter._go_back = AsyncMock(side_effect=AssertionError("external history must not be used"))  # type: ignore[method-assign]
        self.adapter._open_home_via_click = AsyncMock(side_effect=AssertionError("external page must not be clicked"))  # type: ignore[method-assign]

        self.assertFalse(await self.adapter._return_home(force_nav=True))
        self.adapter._go_back.assert_not_awaited()  # type: ignore[attr-defined]
        self.adapter._open_home_via_click.assert_not_awaited()  # type: ignore[attr-defined]

    async def test_media_ownership_uses_outer_article_timestamp_fallback(self) -> None:
        locator = unittest.mock.MagicMock()
        locator.evaluate = AsyncMock(return_value="https://x.com/owner/status/123")

        self.assertTrue(await self.adapter._media_node_belongs_to_post(locator, "123"))
        script = locator.evaluate.await_args.args[0]
        self.assertIn("article.querySelector('time')", script)
        self.assertIn('data-testid="quoteTweet"', script)

        locator.evaluate = AsyncMock(return_value="https://x.com/owner/status/456")
        self.assertFalse(await self.adapter._media_node_belongs_to_post(locator, "123"))

    async def test_capture_image_loop_propagates_existing_target_error(self) -> None:
        article = unittest.mock.MagicMock()
        images = unittest.mock.MagicMock()
        image = object()
        article.locator.return_value = images
        images.nth.return_value = image
        self.adapter._count_locator = AsyncMock(return_value=1)
        self.adapter._get_attribute = AsyncMock(side_effect=["https://pbs.twimg.com/media/a", "alt"])
        self.adapter._media_node_belongs_to_post = AsyncMock(return_value=True)
        self.adapter._screenshot_locator = AsyncMock(side_effect=FileExistsError("collision"))

        with self.assertRaisesRegex(FileExistsError, "collision"):
            await self.adapter._capture_article_media_nodes(
                article,
                "123",
                Path(self.tmp.name),
            )

    async def test_capture_video_loop_propagates_symlink_target_error(self) -> None:
        article = unittest.mock.MagicMock()
        images = unittest.mock.MagicMock()
        videos = unittest.mock.MagicMock()
        video = object()
        article.locator.side_effect = [images, videos]
        videos.nth.return_value = video
        self.adapter._count_locator = AsyncMock(side_effect=[0, 1])
        self.adapter._get_attribute = AsyncMock(side_effect=["https://video.twimg.com/a", "poster"])
        self.adapter._media_node_belongs_to_post = AsyncMock(return_value=True)
        self.adapter._play_video_locator = AsyncMock()
        self.adapter._screenshot_locator = AsyncMock(side_effect=ValueError("symlink"))

        with self.assertRaisesRegex(ValueError, "symlink"):
            await self.adapter._capture_article_media_nodes(
                article,
                "123",
                Path(self.tmp.name),
                frame_count=1,
            )

    def test_sync_network_idle_propagates_closed_page(self) -> None:
        page = unittest.mock.MagicMock()
        page.wait_for_load_state.side_effect = RuntimeError("Target page has been closed")
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.adapter._wait_network_idle_sync(page, 100)

    async def test_public_async_workflows_are_serialized_per_adapter(self) -> None:
        active = 0
        maximum = 0

        async def delayed_state() -> dict[str, str]:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.02)
            active -= 1
            return {"state": "home", "url": "https://x.com/home"}

        self.adapter.current_state = delayed_state  # type: ignore[method-assign]
        self.adapter._active_home_tab = AsyncMock(return_value="for_you")  # type: ignore[method-assign]

        await asyncio.gather(self.adapter.current_surface(), self.adapter.current_surface())

        self.assertEqual(maximum, 1)

    def test_sync_facade_rejects_an_already_started_adapter(self) -> None:
        self.adapter.context = object()  # type: ignore[assignment]

        with self.assertRaisesRegex(RuntimeError, "sync_facade_requires_unstarted_adapter"):
            self.adapter.sync()

    async def test_reply_composer_is_scoped_to_target_or_new_dialog(self) -> None:
        self.adapter.page = PageStub("https://x.com/owner/status/123")  # type: ignore[assignment]
        article, reply_button, expected_box = object(), object(), object()
        self.adapter._find_post_article_in_context = AsyncMock(return_value=article)  # type: ignore[method-assign]
        self.adapter._article_primary_post_id = AsyncMock(return_value="123")  # type: ignore[method-assign]
        self.adapter._snapshot_visible_bindings = AsyncMock(side_effect=[{"old-box"}, {"old-dialog"}])  # type: ignore[method-assign]
        self.adapter._find_new_target_composer = AsyncMock(return_value=expected_box)  # type: ignore[method-assign]
        self.adapter._confirm_reply_audience_if_needed = AsyncMock(return_value=False)  # type: ignore[method-assign]
        self.adapter._click = AsyncMock()  # type: ignore[method-assign]

        async def scoped(scope, selectors, **_kwargs):
            if scope is article and selectors == list(__import__("XController._ui_selectors", fromlist=["COMMENT_BUTTONS"]).COMMENT_BUTTONS):
                return reply_button
            return None

        self.adapter._find_first_in_scope = AsyncMock(side_effect=scoped)  # type: ignore[method-assign]
        self.adapter._find_first = AsyncMock(side_effect=AssertionError("global composer lookup forbidden"))  # type: ignore[method-assign]

        box = await self.adapter._open_reply_box_in_current_context("123")

        self.assertIs(box, expected_box)
        self.adapter._find_new_target_composer.assert_awaited_once_with(  # type: ignore[attr-defined]
            "123", {"old-box"}, {"old-dialog"}, timeout_ms=2600
        )
        self.adapter._find_first.assert_not_awaited()  # type: ignore[attr-defined]

    async def test_quote_menu_and_composer_are_new_scoped_surfaces(self) -> None:
        from XController import _ui_selectors as ui

        self.adapter.page = PageStub("https://x.com/owner/status/123")  # type: ignore[assignment]
        article, repost_button = object(), object()
        new_menu, quote_item, expected_box = object(), object(), object()
        self.adapter._find_post_article_in_context = AsyncMock(return_value=article)  # type: ignore[method-assign]
        self.adapter._article_primary_post_id = AsyncMock(return_value="123")  # type: ignore[method-assign]
        self.adapter._snapshot_visible_bindings = AsyncMock(
            side_effect=[{"old-box"}, {"old-dialog"}, {"old-menu"}]
        )  # type: ignore[method-assign]
        self.adapter._find_new_bound_locator = AsyncMock(return_value=new_menu)  # type: ignore[method-assign]
        self.adapter._find_new_target_composer = AsyncMock(return_value=expected_box)  # type: ignore[method-assign]
        self.adapter._click = AsyncMock()  # type: ignore[method-assign]

        async def scoped(scope, selectors, **_kwargs):
            if scope is article and selectors == ui.REPOST_BUTTONS:
                return repost_button
            if scope is new_menu and selectors == ui.QUOTE_MENU_ITEMS:
                return quote_item
            return None

        self.adapter._find_first_in_scope = AsyncMock(side_effect=scoped)  # type: ignore[method-assign]
        self.adapter._find_first_enabled = AsyncMock(side_effect=AssertionError("global menu lookup forbidden"))  # type: ignore[method-assign]
        self.adapter._find_first = AsyncMock(side_effect=AssertionError("global composer lookup forbidden"))  # type: ignore[method-assign]

        box = await self.adapter._open_quote_box_in_current_context("123")

        self.assertIs(box, expected_box)
        self.adapter._find_new_bound_locator.assert_awaited_once_with(  # type: ignore[attr-defined]
            self.adapter.page,
            ['[role="menu"]'],
            {"old-menu"},
            timeout_ms=2600,
            purpose="quote-menu",
        )
        self.adapter._find_new_target_composer.assert_awaited_once_with(  # type: ignore[attr-defined]
            "123", {"old-box"}, {"old-dialog"}, timeout_ms=3200
        )
        self.adapter._find_first_enabled.assert_not_awaited()  # type: ignore[attr-defined]
        self.adapter._find_first.assert_not_awaited()  # type: ignore[attr-defined]

    async def test_composer_relation_verification_fails_without_exact_target_link(self) -> None:
        textbox = unittest.mock.MagicMock()
        dialog_query = unittest.mock.MagicMock()
        dialog = unittest.mock.MagicMock()
        stable_dialog = unittest.mock.MagicMock()
        dialog_query.first = dialog
        textbox.locator.return_value = dialog_query
        self.adapter._count_locator = AsyncMock(return_value=1)  # type: ignore[method-assign]
        self.adapter._is_visible = AsyncMock(return_value=True)  # type: ignore[method-assign]
        self.adapter._bind_locator = AsyncMock(return_value=(stable_dialog, "new-dialog"))  # type: ignore[method-assign]
        self.adapter._find_exact_status_link = AsyncMock(return_value=None)  # type: ignore[method-assign]

        self.assertFalse(
            await self.adapter._composer_dialog_matches_target(textbox, "123", {"old-dialog"})
        )


if __name__ == "__main__":
    unittest.main()
