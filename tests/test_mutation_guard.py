from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock

from XController import ControllerHealth, ControllerSettings, LoginState, XTextAdapter


class _Page:
    url = "https://x.com/home"


class _OneLocator:
    def __init__(self, value: object) -> None:
        self.first = value


class _DialogScope:
    def __init__(self, dialog: object) -> None:
        self.dialog = dialog

    def locator(self, _selector: str) -> _OneLocator:
        return _OneLocator(self.dialog)


def _health(**overrides) -> ControllerHealth:
    values = {
        "browser_started": True,
        "logged_in": True,
        "current_url": "https://x.com/home",
        "current_state": "home",
        "login_required": False,
    }
    values.update(overrides)
    return ControllerHealth(**values)


class MutationGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _adapter(self, settings: ControllerSettings | None = None) -> XTextAdapter:
        adapter = XTextAdapter(
            profile_path=str(Path(self.tmp.name) / "profile"),
            settings=settings,
        )
        adapter.page = _Page()  # type: ignore[assignment]
        self.addCleanup(adapter._shutdown_executor)
        return adapter

    async def test_rate_limit_starts_cooldown_and_blocks_followup_probe(self) -> None:
        adapter = self._adapter(ControllerSettings(rate_limit_cooldown_seconds=30))
        adapter.health_check = AsyncMock(return_value=_health(rate_limited=True))  # type: ignore[method-assign]

        ready, reason, raw = await adapter._mutation_guard()
        self.assertFalse(ready)
        self.assertEqual(reason, "rate_limited")
        self.assertEqual(raw["cooldown_seconds"], 30)

        ready, reason, raw = await adapter._mutation_guard()
        self.assertFalse(ready)
        self.assertEqual(reason, "rate_limit_cooldown")
        self.assertGreater(raw["cooldown_remaining_seconds"], 0)
        adapter.health_check.assert_awaited_once()

    async def test_expected_account_mismatch_fails_closed(self) -> None:
        adapter = self._adapter(ControllerSettings(expected_account_handle="expected"))
        adapter.health_check = AsyncMock(return_value=_health())  # type: ignore[method-assign]
        adapter._get_authenticated_handle = AsyncMock(return_value="different")  # type: ignore[method-assign]

        ready, reason, raw = await adapter._mutation_guard()

        self.assertFalse(ready)
        self.assertEqual(reason, "account_identity_mismatch")
        self.assertEqual(raw["expected_account_handle"], "expected")
        self.assertEqual(raw["authenticated_account_handle"], "different")
        adapter._get_authenticated_handle.assert_awaited_once_with(force_refresh=True)

    async def test_expected_account_unavailable_fails_closed(self) -> None:
        adapter = self._adapter(ControllerSettings(expected_account_handle="expected"))
        adapter.health_check = AsyncMock(return_value=_health())  # type: ignore[method-assign]
        adapter._get_authenticated_handle = AsyncMock(return_value=None)  # type: ignore[method-assign]

        ready, reason, _ = await adapter._mutation_guard()

        self.assertFalse(ready)
        self.assertEqual(reason, "account_identity_unavailable")

    async def test_repost_does_not_navigate_when_guard_rejects(self) -> None:
        adapter = self._adapter()
        adapter._mutation_guard = AsyncMock(  # type: ignore[method-assign]
            return_value=(False, "account_locked", {"account_locked": True})
        )
        adapter._open_post_page = AsyncMock()  # type: ignore[method-assign]

        result = await adapter.repost_post_detailed("123")

        self.assertFalse(result.ok)
        self.assertEqual(result.failure_reason, "account_locked")
        self.assertEqual(result.failure_stage, "preflight")
        adapter._open_post_page.assert_not_awaited()

    async def test_commit_guard_rejects_login_loss_and_unexpected_dialogs(self) -> None:
        adapter = self._adapter()
        adapter._page_is_trusted = AsyncMock(return_value=True)  # type: ignore[method-assign]
        adapter.login_state = AsyncMock(  # type: ignore[method-assign]
            return_value=LoginState(logged_in=False, page_state="login", browser_started=True, login_required=True)
        )
        adapter._visible_system_messages = AsyncMock(return_value=[])  # type: ignore[method-assign]
        ready, reason, _ = await adapter._mutation_commit_guard()
        self.assertFalse(ready)
        self.assertEqual(reason, "login_required")

        adapter.login_state = AsyncMock(  # type: ignore[method-assign]
            return_value=LoginState(logged_in=True, page_state="home", browser_started=True, login_required=False)
        )
        adapter._visible_scope_count = AsyncMock(return_value=1)  # type: ignore[method-assign]
        ready, reason, _ = await adapter._mutation_commit_guard()
        self.assertFalse(ready)
        self.assertEqual(reason, "blocking_modal_present")

    async def test_commit_guard_accepts_only_supplied_dialog_and_rejects_challenge(self) -> None:
        adapter = self._adapter()
        dialog = object()
        scope = _DialogScope(dialog)
        adapter._page_is_trusted = AsyncMock(return_value=True)  # type: ignore[method-assign]
        adapter.login_state = AsyncMock(  # type: ignore[method-assign]
            return_value=LoginState(logged_in=True, page_state="compose", browser_started=True, login_required=False)
        )
        adapter._visible_system_messages = AsyncMock(return_value=[])  # type: ignore[method-assign]
        adapter._count_locator = AsyncMock(return_value=1)  # type: ignore[method-assign]
        adapter._is_visible = AsyncMock(return_value=True)  # type: ignore[method-assign]
        adapter._visible_scope_count = AsyncMock(return_value=1)  # type: ignore[method-assign]
        adapter._inner_text = AsyncMock(return_value="Confirm action")  # type: ignore[method-assign]
        ready, reason, _ = await adapter._mutation_commit_guard(scope)
        self.assertTrue(ready)
        self.assertEqual(reason, "")

        adapter._inner_text = AsyncMock(return_value="Confirm your identity with CAPTCHA")  # type: ignore[method-assign]
        ready, reason, _ = await adapter._mutation_commit_guard(scope)
        self.assertFalse(ready)
        self.assertEqual(reason, "authentication_challenge")


if __name__ == "__main__":
    unittest.main()
