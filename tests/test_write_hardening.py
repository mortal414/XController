from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock

try:
    from XController import ActionPreflight, MediaPreflight, XTextAdapter
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
    from XController import ActionPreflight, MediaPreflight, XTextAdapter


class _NoopHuman:
    async def jitter(self, *_args, **_kwargs) -> None:
        return None


class _Page:
    def __init__(self, url: str = "https://x.com/home") -> None:
        self.url = url


class _Textbox:
    def __init__(self, value: str, *, fill_clears: bool = True) -> None:
        self.value = value
        self.fill_clears = fill_clears
        self.clicks = 0
        self.fills: list[str] = []

    async def click(self, **_kwargs) -> None:
        self.clicks += 1

    async def input_value(self, **_kwargs) -> str:
        return self.value

    async def fill(self, value: str, **_kwargs) -> None:
        self.fills.append(value)
        if self.fill_clears:
            self.value = value

    async def is_visible(self) -> bool:
        return True


class _AmbiguousControl:
    def __init__(self) -> None:
        self.clicks = 0

    async def click(self, **_kwargs) -> None:
        self.clicks += 1
        raise TimeoutError("click result unknown")


class WriteHardeningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.adapter = XTextAdapter(profile_path=str(Path(self.tmp.name) / "profile"))
        self.adapter.human = _NoopHuman()
        self.adapter._mutation_guard = AsyncMock(return_value=(True, "", {}))  # type: ignore[method-assign]
        self.adapter._mutation_commit_guard = AsyncMock(return_value=(True, "", {}))  # type: ignore[method-assign]
        self.adapter._snapshot_recent_owned_ids = AsyncMock(return_value=set())  # type: ignore[method-assign]

    def tearDown(self) -> None:
        self.adapter._shutdown_executor()
        self.tmp.cleanup()

    def _spoofed_png(self) -> Path:
        path = Path(self.tmp.name) / "spoofed.png"
        path.write_bytes(b"this is not a PNG")
        return path

    async def test_post_image_rejects_spoofed_content_before_composer(self) -> None:
        path = self._spoofed_png()
        self.adapter._post_from_compose = AsyncMock()  # type: ignore[method-assign]

        with self.assertWarns(DeprecationWarning):
            with self.assertRaisesRegex(ValueError, "invalid_image_signature"):
                await self.adapter.post_image(path, text="hello")

        self.adapter._post_from_compose.assert_not_awaited()

    async def test_direct_quote_rejects_spoofed_content_before_mutation(self) -> None:
        path = self._spoofed_png()
        self.adapter.page = _Page()  # type: ignore[assignment]
        self.adapter._open_post_page = AsyncMock()  # type: ignore[method-assign]

        with self.assertRaisesRegex(ValueError, "invalid_image_signature"):
            await self.adapter.quote_post("123", text="hello", image_paths=path)

        self.adapter._mutation_guard.assert_not_awaited()
        self.adapter._open_post_page.assert_not_awaited()

    async def test_reply_image_helper_rejects_spoofed_content_before_reply_flow(self) -> None:
        path = self._spoofed_png()
        self.adapter._reply_to_post_impl = AsyncMock()  # type: ignore[method-assign]

        with self.assertWarns(DeprecationWarning):
            with self.assertRaisesRegex(ValueError, "invalid_image_signature"):
                await self.adapter.reply_with_image("123", path, text="hello")

        self.adapter._reply_to_post_impl.assert_not_awaited()

    async def test_clear_textbox_requires_observed_empty_state(self) -> None:
        box = _Textbox("stale draft")
        self.adapter._keyboard_press = AsyncMock()  # type: ignore[method-assign]

        self.assertTrue(await self.adapter._clear_textbox(box))
        self.assertEqual(box.fills, [""])
        self.assertEqual(box.value, "")

        uncleared = _Textbox("stale draft", fill_clears=False)
        self.assertFalse(await self.adapter._clear_textbox(uncleared))

    async def test_ambiguous_submit_click_is_never_replayed(self) -> None:
        control = _AmbiguousControl()
        self.adapter._find_composer_submit_control = AsyncMock(return_value=control)  # type: ignore[method-assign]
        self.adapter._wait_for_composer_confirmation = AsyncMock()  # type: ignore[method-assign]

        triggered, confirmed, signal = await self.adapter._submit_composer_once(
            object(),
            "hello",
            selectors=["button"],
        )

        self.assertTrue(triggered)
        self.assertFalse(confirmed)
        self.assertEqual(signal, "click_outcome_ambiguous")
        self.assertEqual(control.clicks, 1)
        self.adapter._wait_for_composer_confirmation.assert_not_awaited()

    async def test_targeted_submit_revalidates_relation_before_click(self) -> None:
        control = _AmbiguousControl()
        self.adapter._find_composer_submit_control = AsyncMock(return_value=control)  # type: ignore[method-assign]
        self.adapter._composer_still_targets_post = AsyncMock(return_value=False)  # type: ignore[method-assign]

        triggered, confirmed, signal = await self.adapter._submit_composer_once(
            object(),
            "hello",
            selectors=["button"],
            target_post_id="123",
        )

        self.assertFalse(triggered)
        self.assertFalse(confirmed)
        self.assertEqual(signal, "target_relation_lost")
        self.assertEqual(control.clicks, 0)
        self.adapter._mutation_commit_guard.assert_not_awaited()

    async def test_post_reports_unknown_id_only_after_confirmed_submission(self) -> None:
        self.adapter.page = _Page()  # type: ignore[assignment]
        box = object()
        self.adapter._open_compose_box = AsyncMock(return_value=box)  # type: ignore[method-assign]
        self.adapter._move_mouse = AsyncMock()  # type: ignore[method-assign]
        self.adapter._clear_textbox = AsyncMock(return_value=True)  # type: ignore[method-assign]
        self.adapter._type_text = AsyncMock()  # type: ignore[method-assign]
        self.adapter._attach_images_to_composer = AsyncMock(return_value=True)  # type: ignore[method-assign]
        self.adapter._submit_composer_once = AsyncMock(return_value=(True, True, "box_empty"))  # type: ignore[method-assign]
        self.adapter._find_recent_own_created_post_id = AsyncMock(return_value=None)  # type: ignore[method-assign]

        created_id = await self.adapter._post_from_compose("short", [])

        self.assertEqual(created_id, "unknown_post_id")
        diagnostic = self.adapter._consume_unknown_id_confirmation_signal("post")
        self.assertEqual(diagnostic["confirmation_signal"]["signal"], "box_empty")

    async def test_post_detailed_preserves_ambiguous_trigger_state(self) -> None:
        self.adapter.page = _Page()  # type: ignore[assignment]
        self.adapter.attach_images_preflight = AsyncMock(  # type: ignore[method-assign]
            return_value=MediaPreflight(ok=True, normalized_paths=[])
        )
        self.adapter._post_from_compose = AsyncMock(return_value=None)  # type: ignore[method-assign]
        self.adapter._last_post_submit_outcome = {
            "composer_opened": True,
            "triggered": True,
            "confirmed": False,
            "signal": "click_outcome_ambiguous",
        }

        result = await self.adapter.post_text_detailed("hello")

        self.assertFalse(result.ok)
        self.assertEqual(result.failure_reason, "submit_outcome_unknown")
        self.assertTrue(result.submit_clicked)
        self.assertFalse(result.confirmation_observed)

    async def test_reply_stops_after_one_unconfirmed_trigger(self) -> None:
        self.adapter.page = _Page("https://x.com/i/web/status/123")  # type: ignore[assignment]
        self.adapter.attach_images_preflight = AsyncMock(  # type: ignore[method-assign]
            return_value=MediaPreflight(ok=True, normalized_paths=[])
        )
        self.adapter.preflight_action = AsyncMock(  # type: ignore[method-assign]
            return_value=ActionPreflight(ok=True, action="reply", target_post_id="123")
        )
        self.adapter._open_reply_box_in_current_context = AsyncMock(return_value=object())  # type: ignore[method-assign]
        self.adapter._has_reply_audience_modal = AsyncMock(return_value=False)  # type: ignore[method-assign]
        self.adapter._clear_textbox = AsyncMock(return_value=True)  # type: ignore[method-assign]
        self.adapter._type_text = AsyncMock()  # type: ignore[method-assign]
        self.adapter._attach_images_to_composer = AsyncMock(return_value=True)  # type: ignore[method-assign]
        self.adapter._submit_composer_once = AsyncMock(  # type: ignore[method-assign]
            return_value=(True, False, "trigger_unconfirmed")
        )

        result = await self.adapter.reply_to_post_detailed("123", "hello")

        self.assertFalse(result.ok)
        self.assertEqual(result.failure_reason, "submit_outcome_unknown")
        self.assertTrue(result.submit_clicked)
        self.adapter._submit_composer_once.assert_awaited_once()
        self.adapter._open_reply_box_in_current_context.assert_awaited_once()

    async def test_quote_missing_composer_does_not_create_plain_post(self) -> None:
        self.adapter.page = _Page("https://x.com/i/web/status/123")  # type: ignore[assignment]
        self.adapter._open_post_page = AsyncMock(return_value=True)  # type: ignore[method-assign]
        self.adapter._open_quote_box_in_current_context = AsyncMock(return_value=None)  # type: ignore[method-assign]
        self.adapter._post_from_compose = AsyncMock()  # type: ignore[method-assign]

        self.assertIsNone(await self.adapter.quote_post("123", text="hello"))
        self.adapter._post_from_compose.assert_not_awaited()

    async def test_created_id_requires_recency_and_exact_short_text(self) -> None:
        now = datetime.now(timezone.utc)
        self.adapter.page = _Page()  # type: ignore[assignment]
        self.adapter._get_authenticated_handle = AsyncMock(return_value="owner")  # type: ignore[method-assign]
        self.adapter._collect_recent_owned_created_candidates = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                {"post_id": "100", "text": "ok", "created_at": (now - timedelta(minutes=2)).isoformat()},
                {"post_id": "101", "text": "ok then", "created_at": (now + timedelta(seconds=1)).isoformat()},
                {"post_id": "102", "text": "ok", "created_at": (now + timedelta(seconds=2)).isoformat()},
            ]
        )

        created_id = await self.adapter._find_recent_own_created_post_id("post", "ok", not_before=now)

        self.assertEqual(created_id, "102")
        self.assertTrue(self.adapter._created_text_matches("ok", "OK"))
        self.assertFalse(self.adapter._created_text_matches("ok", "ok then"))

    async def test_created_id_target_relation_outranks_raw_recency(self) -> None:
        now = datetime.now(timezone.utc)
        self.adapter.page = _Page()  # type: ignore[assignment]
        self.adapter._get_authenticated_handle = AsyncMock(return_value="owner")  # type: ignore[method-assign]
        self.adapter._collect_recent_owned_created_candidates = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                {
                    "post_id": "201",
                    "text": "same reply",
                    "created_at": (now + timedelta(seconds=4)).isoformat(),
                    "is_reply": True,
                },
                {
                    "post_id": "202",
                    "text": "same reply",
                    "created_at": (now + timedelta(seconds=1)).isoformat(),
                    "is_reply": True,
                    "reply_to_post_id": "123",
                },
            ]
        )

        created_id = await self.adapter._find_recent_own_created_post_id(
            "reply",
            "same reply",
            target_post_id="123",
            not_before=now,
        )

        self.assertEqual(created_id, "202")

    async def test_delete_never_falls_back_to_global_menu_controls(self) -> None:
        article = object()
        self.adapter._article_has_exact_primary_post_id = AsyncMock(return_value=True)  # type: ignore[method-assign]
        self.adapter._open_article_menu = AsyncMock(return_value=None)  # type: ignore[method-assign]
        self.adapter._find_first = AsyncMock(return_value=object())  # type: ignore[method-assign]
        self.adapter._safe_click_once = AsyncMock(return_value=True)  # type: ignore[method-assign]

        deleted = await self.adapter._delete_owned_article(
            article,
            {"post_id": "123", "author": "owner", "is_reply": False, "is_quote": False},
            "post",
            "owner",
        )

        self.assertFalse(deleted)
        self.adapter._find_first.assert_not_awaited()
        self.adapter._safe_click_once.assert_not_awaited()

    async def test_repost_requires_exact_target_and_observed_active_state(self) -> None:
        self.adapter.page = _Page("https://x.com/i/web/status/123")  # type: ignore[assignment]
        article, button, menu, confirm = object(), object(), object(), object()
        self.adapter._open_post_page = AsyncMock(return_value=True)  # type: ignore[method-assign]
        self.adapter._find_post_article_in_context = AsyncMock(return_value=article)  # type: ignore[method-assign]
        self.adapter._article_has_exact_primary_post_id = AsyncMock(return_value=True)  # type: ignore[method-assign]
        self.adapter._find_visible_in_scope = AsyncMock(side_effect=[None, button, None])  # type: ignore[method-assign]
        self.adapter._safe_click_once = AsyncMock(return_value=True)  # type: ignore[method-assign]
        self.adapter._visible_overlay_scope = AsyncMock(return_value=menu)  # type: ignore[method-assign]
        self.adapter._find_exact_control_in_scope = AsyncMock(return_value=confirm)  # type: ignore[method-assign]

        result = await self.adapter.repost_post_detailed("123")

        self.assertFalse(result.ok)
        self.assertEqual(result.failure_reason, "repost_state_not_confirmed")
        self.assertEqual(self.adapter._safe_click_once.await_count, 2)

    async def test_follow_requires_verified_handle_and_resulting_exact_state(self) -> None:
        self.adapter.page = _Page("https://x.com/owner")  # type: ignore[assignment]
        self.adapter._return_home = AsyncMock()  # type: ignore[method-assign]
        self.adapter._open_verified_profile = AsyncMock(return_value="different")  # type: ignore[method-assign]
        self.adapter._find_profile_follow_state = AsyncMock()  # type: ignore[method-assign]
        self.adapter._safe_click_once = AsyncMock(return_value=True)  # type: ignore[method-assign]

        self.assertFalse(await self.adapter.follow_user("owner"))
        self.adapter._find_profile_follow_state.assert_not_awaited()
        self.adapter._safe_click_once.assert_not_awaited()

        button, following = object(), object()
        self.adapter._open_verified_profile = AsyncMock(return_value="owner")  # type: ignore[method-assign]
        self.adapter._find_profile_follow_state = AsyncMock(  # type: ignore[method-assign]
            side_effect=[None, button, following]
        )
        self.assertTrue(await self.adapter.follow_user("@Owner"))
        self.adapter._safe_click_once.assert_awaited_once_with(button)

    async def test_unfollow_requires_dialog_and_observed_follow_state(self) -> None:
        self.adapter.page = _Page("https://x.com/owner")  # type: ignore[assignment]
        following, dialog, confirm = object(), object(), object()
        self.adapter._return_home = AsyncMock()  # type: ignore[method-assign]
        self.adapter._open_verified_profile = AsyncMock(return_value="owner")  # type: ignore[method-assign]
        self.adapter._find_profile_follow_state = AsyncMock(side_effect=[following, None])  # type: ignore[method-assign]
        self.adapter._safe_click_once = AsyncMock(return_value=True)  # type: ignore[method-assign]
        self.adapter._visible_overlay_scope = AsyncMock(return_value=dialog)  # type: ignore[method-assign]
        self.adapter._find_exact_control_in_scope = AsyncMock(return_value=confirm)  # type: ignore[method-assign]

        self.assertFalse(await self.adapter.unfollow_user("owner"))
        self.assertEqual(self.adapter._safe_click_once.await_count, 2)

    async def test_like_guard_runs_before_navigating_preflight_and_never_replays(self) -> None:
        self.adapter.page = _Page("https://example.com/external")  # type: ignore[assignment]
        self.adapter._mutation_guard = AsyncMock(  # type: ignore[method-assign]
            return_value=(False, "untrusted_origin", {})
        )
        self.adapter.preflight_action = AsyncMock()  # type: ignore[method-assign]

        rejected = await self.adapter.like_post_detailed("123")

        self.assertFalse(rejected.ok)
        self.assertEqual(rejected.failure_reason, "untrusted_origin")
        self.adapter.preflight_action.assert_not_awaited()

        self.adapter.page = _Page("https://x.com/home")  # type: ignore[assignment]
        self.adapter._mutation_guard = AsyncMock(return_value=(True, "", {}))  # type: ignore[method-assign]
        self.adapter._mutation_commit_guard = AsyncMock(return_value=(True, "", {}))  # type: ignore[method-assign]
        self.adapter.preflight_action = AsyncMock(  # type: ignore[method-assign]
            return_value=ActionPreflight(ok=True, action="like", target_post_id="123")
        )
        self.adapter._like_in_current_context = AsyncMock(return_value=False)  # type: ignore[method-assign]
        self.adapter.engage_post = AsyncMock()  # type: ignore[method-assign]

        unconfirmed = await self.adapter.like_post_detailed("123")

        self.assertFalse(unconfirmed.ok)
        self.assertEqual(unconfirmed.failure_reason, "like_state_not_confirmed")
        self.adapter._like_in_current_context.assert_awaited_once_with("123")
        self.adapter.engage_post.assert_not_awaited()

    async def test_quote_guard_runs_before_navigating_preflight(self) -> None:
        self.adapter.page = _Page("https://example.com/external")  # type: ignore[assignment]
        self.adapter._mutation_guard = AsyncMock(  # type: ignore[method-assign]
            return_value=(False, "untrusted_origin", {})
        )
        self.adapter.attach_images_preflight = AsyncMock()  # type: ignore[method-assign]
        self.adapter.preflight_action = AsyncMock()  # type: ignore[method-assign]

        result = await self.adapter.quote_post_detailed("123", text="quote")

        self.assertFalse(result.ok)
        self.assertEqual(result.failure_reason, "untrusted_origin")
        self.adapter.attach_images_preflight.assert_not_awaited()
        self.adapter.preflight_action.assert_not_awaited()

    async def test_triggered_delete_never_falls_back_to_another_surface(self) -> None:
        self.adapter.page = _Page()  # type: ignore[assignment]
        article = object()
        item = {"post_id": "123", "author": "owner"}
        self.adapter._get_authenticated_handle = AsyncMock(return_value="owner")  # type: ignore[method-assign]
        self.adapter._find_owned_status_article_by_post_id = AsyncMock(  # type: ignore[method-assign]
            return_value=(article, item)
        )

        async def ambiguous_delete(*_args, **_kwargs) -> bool:
            self.adapter._last_delete_outcome = {
                "triggered": True,
                "confirmed": False,
                "signal": "click_outcome_ambiguous",
            }
            return False

        self.adapter._delete_owned_article = AsyncMock(side_effect=ambiguous_delete)  # type: ignore[method-assign]
        self.adapter._find_profile_article_by_post_id = AsyncMock()  # type: ignore[method-assign]

        self.assertFalse(await self.adapter._delete_owned_status_item("123", "post"))
        self.adapter._delete_owned_article.assert_awaited_once()  # type: ignore[attr-defined]
        self.adapter._find_profile_article_by_post_id.assert_not_awaited()

    async def test_bulk_delete_rechecks_guard_before_every_candidate(self) -> None:
        self.adapter.page = _Page()  # type: ignore[assignment]
        self.adapter._mutation_guard = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                (True, "", {}),
                (True, "", {}),
                (False, "rate_limited", {}),
            ]
        )
        self.adapter._get_authenticated_handle = AsyncMock(return_value="owner")  # type: ignore[method-assign]
        self.adapter._open_profile_surface = AsyncMock(return_value=True)  # type: ignore[method-assign]
        self.adapter._collect_visible_profile_candidates = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                [(object(), {"post_id": "101", "url": "https://x.com/owner/status/101"})],
                [(object(), {"post_id": "102", "url": "https://x.com/owner/status/102"})],
            ]
        )
        self.adapter._delete_owned_article = AsyncMock(return_value=True)  # type: ignore[method-assign]

        deleted = await self.adapter._delete_all_profile_items("post")

        self.assertEqual(deleted, ["https://x.com/owner/status/101"])
        self.assertEqual(self.adapter._delete_owned_article.await_count, 1)  # type: ignore[attr-defined]
        self.assertEqual(self.adapter._mutation_guard.await_count, 3)  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
