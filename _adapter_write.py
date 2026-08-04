"""Posting, engagement, and content-mutation flows for XTextAdapter."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
import logging
import random
import re
import time
import warnings
from typing import Any

from . import _ui_selectors as ui
from ._adapter_runtime import ImagePathInput
from ._diagnostics import ActionFailureStage
from .base import ActionResult

logger = logging.getLogger(__name__)


class _AdapterWriteMixin:
    """State-mutating write and cleanup flows for X posts, replies, quotes, and follows."""

    async def _mutation_guard(self) -> tuple[bool, str, dict[str, Any]]:
        """Fail closed unless the active X session is healthy for a mutation."""
        self._last_mutation_guard_reason = ""
        if not self.page:
            self._last_mutation_guard_reason = "page_not_started"
            return False, "page_not_started", {}
        if not await self._page_is_trusted(self.page):
            self._last_mutation_guard_reason = "untrusted_origin"
            return False, "untrusted_origin", {}
        cooldown_until = float(getattr(self, "_rate_limit_cooldown_until", 0.0) or 0.0)
        cooldown_remaining = max(0.0, cooldown_until - time.monotonic())
        if cooldown_remaining > 0:
            self._last_mutation_guard_reason = "rate_limit_cooldown"
            return False, "rate_limit_cooldown", {
                "cooldown_remaining_seconds": round(cooldown_remaining, 3)
            }
        try:
            health = await self.health_check()
        except Exception as exc:
            self._last_mutation_guard_reason = "health_check_failed"
            return False, "health_check_failed", {"error_type": type(exc).__name__}
        raw = health.to_dict()
        reason = ""
        if not health.logged_in or health.login_required:
            reason = "login_required"
        elif health.account_locked:
            reason = "account_locked"
        elif health.rate_limited:
            reason = "rate_limited"
        elif health.blocking_modal_present:
            reason = "blocking_modal_present"
        if reason:
            if reason == "rate_limited":
                cooldown_seconds = int(self.settings.rate_limit_cooldown_seconds)
                self._rate_limit_cooldown_until = time.monotonic() + cooldown_seconds
                raw["cooldown_seconds"] = cooldown_seconds
            self._last_mutation_guard_reason = reason
            return False, reason, raw
        expected_handle = str(self.settings.expected_account_handle or "").strip().lstrip("@")
        if expected_handle:
            actual_handle = await self._get_authenticated_handle(force_refresh=True)
            raw["expected_account_handle"] = expected_handle
            raw["authenticated_account_handle"] = actual_handle or ""
            if not actual_handle:
                self._last_mutation_guard_reason = "account_identity_unavailable"
                return False, "account_identity_unavailable", raw
            if actual_handle.casefold() != expected_handle.casefold():
                self._last_mutation_guard_reason = "account_identity_mismatch"
                return False, "account_identity_mismatch", raw
        return True, "", raw

    async def _authenticated_handle_on_current_page(self) -> str:
        """Read account identity evidence without navigating away from a draft."""
        if not self.page or not await self._page_is_trusted(self.page):
            return ""
        for selector in (
            'a[data-testid="AppTabBar_Profile_Link"]',
            '[data-testid="SideNav_AccountSwitcher_Button"]',
        ):
            with contextlib.suppress(Exception):
                locator = self.page.locator(selector)
                for index in range(await self._count_locator(locator)):
                    candidate = locator.nth(index)
                    if not await self._is_visible(candidate):
                        continue
                    href = (await self._get_attribute(candidate, "href")) or ""
                    handle = self._extract_profile_handle_from_href(href)
                    if not handle:
                        handle = self._extract_handle_from_text(
                            await self._inner_text(candidate, timeout_ms=700)
                        )
                    if handle:
                        return handle
        return ""

    async def _mutation_commit_guard(
        self,
        expected_scope: Any | None = None,
    ) -> tuple[bool, str, dict[str, Any]]:
        """Revalidate mutation invariants immediately before one irreversible click.

        A composer or confirmation dialog is accepted only when it is the sole
        visible dialog and is the supplied control's own ancestor. Any other
        overlay, login challenge, restriction, origin change, or account change
        fails closed.
        """
        self._last_mutation_guard_reason = ""
        if not self.page:
            self._last_mutation_guard_reason = "page_not_started"
            return False, "page_not_started", {}
        if not await self._page_is_trusted(self.page):
            self._last_mutation_guard_reason = "untrusted_origin"
            return False, "untrusted_origin", {}

        cooldown_until = float(getattr(self, "_rate_limit_cooldown_until", 0.0) or 0.0)
        cooldown_remaining = max(0.0, cooldown_until - time.monotonic())
        if cooldown_remaining > 0:
            self._last_mutation_guard_reason = "rate_limit_cooldown"
            return False, "rate_limit_cooldown", {
                "cooldown_remaining_seconds": round(cooldown_remaining, 3)
            }

        try:
            login = await self.login_state()
            messages = await self._visible_system_messages()
        except Exception as exc:
            self._last_mutation_guard_reason = "health_check_failed"
            return False, "health_check_failed", {"error_type": type(exc).__name__}
        raw: dict[str, Any] = {
            "login_state": login.to_dict(),
            "visible_system_messages": list(messages),
        }
        if not login.logged_in or login.login_required:
            self._last_mutation_guard_reason = "login_required"
            return False, "login_required", raw

        system_text = " ".join(messages).casefold()
        if any(
            marker in system_text
            for marker in (
                "account is locked",
                "account has been locked",
                "temporarily restricted",
                "account is suspended",
                "account suspended",
            )
        ):
            self._last_mutation_guard_reason = "account_locked"
            return False, "account_locked", raw
        if any(
            marker in system_text
            for marker in (
                "rate limit",
                "rate-limit",
                "too many requests",
                "try again later",
                "reached your limit",
            )
        ):
            cooldown_seconds = int(self.settings.rate_limit_cooldown_seconds)
            self._rate_limit_cooldown_until = time.monotonic() + cooldown_seconds
            raw["cooldown_seconds"] = cooldown_seconds
            self._last_mutation_guard_reason = "rate_limited"
            return False, "rate_limited", raw

        expected_dialog = None
        if expected_scope is not None and hasattr(expected_scope, "locator"):
            if not await self._is_visible(expected_scope):
                self._last_mutation_guard_reason = "mutation_scope_not_visible"
                return False, "mutation_scope_not_visible", raw
            with contextlib.suppress(Exception):
                dialog = expected_scope.locator("xpath=ancestor-or-self::*[@role='dialog'][1]").first
                if await self._count_locator(dialog) and await self._is_visible(dialog):
                    expected_dialog = dialog
        visible_dialog_count = await self._visible_scope_count('[role="dialog"]')
        expected_dialog_count = 1 if expected_dialog is not None else 0
        raw["visible_dialog_count"] = visible_dialog_count
        raw["expected_dialog_bound"] = expected_dialog is not None
        if visible_dialog_count != expected_dialog_count:
            self._last_mutation_guard_reason = "blocking_modal_present"
            return False, "blocking_modal_present", raw
        if expected_dialog is not None:
            dialog_text = ""
            with contextlib.suppress(Exception):
                dialog_text = (await self._inner_text(expected_dialog, timeout_ms=700)).casefold()
            if any(
                marker in dialog_text
                for marker in (
                    "captcha",
                    "confirm your identity",
                    "verify your identity",
                    "enter your password",
                    "security key",
                )
            ):
                self._last_mutation_guard_reason = "authentication_challenge"
                return False, "authentication_challenge", raw

        expected_handle = str(self.settings.expected_account_handle or "").strip().lstrip("@")
        if expected_handle:
            actual_handle = await self._authenticated_handle_on_current_page()
            raw["expected_account_handle"] = expected_handle
            raw["authenticated_account_handle"] = actual_handle
            if not actual_handle:
                self._last_mutation_guard_reason = "account_identity_unavailable"
                return False, "account_identity_unavailable", raw
            if actual_handle.casefold() != expected_handle.casefold():
                self._last_mutation_guard_reason = "account_identity_mismatch"
                return False, "account_identity_mismatch", raw

        if not await self._page_is_trusted(self.page):
            self._last_mutation_guard_reason = "untrusted_origin"
            return False, "untrusted_origin", raw
        return True, "", raw

    def _mutation_guard_reason(self, fallback: str) -> str:
        return str(getattr(self, "_last_mutation_guard_reason", "") or fallback)

    async def _validated_image_paths_for_upload(
        self,
        image_paths: ImagePathInput | None,
    ) -> list[str]:
        """Resolve image paths and reject files whose content does not match their extension."""
        normalized = self._normalize_image_paths(image_paths)
        if not normalized:
            return []
        preflight = await self.attach_images_preflight(normalized)
        if preflight.ok:
            return preflight.normalized_paths

        error = preflight.errors[0] if preflight.errors else {}
        reason = str(error.get("reason") or "media_preflight_failed")
        path = str(error.get("path") or normalized[0])
        if reason == "file_not_found":
            raise FileNotFoundError(f"Image file does not exist: {path}")
        if reason == "file_unreadable":
            raise OSError(f"Image file is not readable: {path}")
        raise ValueError(f"Image upload preflight failed ({reason}): {path}")

    def _unknown_id_confirmation_diagnostic(self, action: str, signal: str) -> dict[str, Any]:
        return {
            "confirmation_signal": {
                "action": action,
                "signal": str(signal or "unknown")[:120],
            }
        }

    def _set_unknown_id_confirmation_signal(self, action: str, signal: str) -> None:
        self._last_unknown_id_confirmation_signal = {"action": action, "signal": str(signal or "unknown")[:120]}

    def _consume_unknown_id_confirmation_signal(self, action: str) -> dict[str, Any]:
        payload = getattr(self, "_last_unknown_id_confirmation_signal", None)
        if isinstance(payload, dict) and payload.get("action") == action:
            self._last_unknown_id_confirmation_signal = None
            return self._unknown_id_confirmation_diagnostic(action, str(payload.get("signal") or "unknown"))
        return self._unknown_id_confirmation_diagnostic(action, "signal_not_observed")

    async def _safe_click_once(self, locator: Any, *, timeout_ms: int = 3500) -> bool:
        """Issue one ordinary Playwright click without force/DOM replay fallbacks."""
        if locator is None:
            return False
        try:
            click = getattr(locator, "click", None)
            if callable(click):
                if self._sync_mode:
                    await self._run_sync(click, timeout=timeout_ms)
                else:
                    await click(timeout=timeout_ms)
            else:
                # Compatibility for test doubles and alternate locator shims. Real
                # Playwright locators always take the single-click path above.
                await self._click(locator)
            return True
        except Exception as exc:
            logger.warning("safe_click_once_failed error=%s", str(exc)[:260])
            return False

    async def _textbox_value(self, box: Any) -> str | None:
        """Read a text input/contenteditable value, or ``None`` when unreadable."""
        if box is None:
            return None
        input_value = getattr(box, "input_value", None)
        if callable(input_value):
            try:
                if self._sync_mode:
                    value = await self._run_sync(input_value, timeout=900)
                else:
                    value = await input_value(timeout=900)
                return str(value or "")
            except Exception:
                pass
        try:
            return str(await self._inner_text(box, timeout_ms=900) or "")
        except Exception:
            return None

    def _textbox_is_empty(self, value: str | None) -> bool:
        if value is None:
            return False
        return not str(value).replace("\u200b", "").strip()

    async def _fill_textbox(self, box: Any, text: str) -> bool:
        fill = getattr(box, "fill", None)
        if not callable(fill):
            return False
        try:
            if self._sync_mode:
                await self._run_sync(fill, text, timeout=1800)
            else:
                await fill(text, timeout=1800)
            return True
        except Exception:
            return False

    async def _find_visible_in_scope(
        self,
        scope: Any,
        selectors: list[str],
        *,
        timeout_ms: int = 0,
        require_enabled: bool = False,
    ) -> Any | None:
        if scope is None:
            return None
        deadline = time.monotonic() + (max(0, timeout_ms) / 1000.0)
        while True:
            for selector in selectors:
                with contextlib.suppress(Exception):
                    locator = scope.locator(selector)
                    count = await self._count_locator(locator)
                    for idx in range(count):
                        candidate = locator.nth(idx)
                        if not await self._is_visible(candidate):
                            continue
                        if require_enabled and not await self._is_button_enabled(candidate):
                            continue
                        return candidate
            if timeout_ms <= 0 or time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.12)

    async def _control_labels(self, locator: Any) -> set[str]:
        values: list[str] = []
        with contextlib.suppress(Exception):
            values.append(await self._inner_text(locator, timeout_ms=700))
        with contextlib.suppress(Exception):
            values.append((await self._get_attribute(locator, "aria-label")) or "")
        labels: set[str] = set()
        for value in values:
            normalized = re.sub(r"\s+", " ", str(value or "")).strip().casefold()
            if normalized:
                labels.add(normalized)
        return labels

    async def _find_exact_control_in_scope(
        self,
        scope: Any,
        selectors: list[str],
        labels: set[str],
        *,
        timeout_ms: int = 0,
        accepted_testids: set[str] | None = None,
    ) -> Any | None:
        expected = {re.sub(r"\s+", " ", label).strip().casefold() for label in labels}
        testids = {value.casefold() for value in (accepted_testids or set())}
        deadline = time.monotonic() + (max(0, timeout_ms) / 1000.0)
        while True:
            for selector in selectors:
                with contextlib.suppress(Exception):
                    locator = scope.locator(selector)
                    count = await self._count_locator(locator)
                    for idx in range(count):
                        candidate = locator.nth(idx)
                        if not await self._is_visible(candidate) or not await self._is_button_enabled(candidate):
                            continue
                        testid = ((await self._get_attribute(candidate, "data-testid")) or "").casefold()
                        if testid and testid in testids:
                            return candidate
                        if await self._control_labels(candidate) & expected:
                            return candidate
            if timeout_ms <= 0 or time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.12)

    async def _visible_overlay_scope(self, role: str, *, timeout_ms: int) -> Any | None:
        if not self.page:
            return None
        return await self._find_visible_in_scope(
            self.page,
            [f'[role="{role}"]'],
            timeout_ms=timeout_ms,
        )

    async def _composer_submit_scopes(self, box: Any) -> list[Any]:
        scopes: list[Any] = []
        with contextlib.suppress(Exception):
            scopes.extend(await self._composer_scopes_for_textbox(box, include_page=False))
        if hasattr(box, "locator"):
            with contextlib.suppress(Exception):
                closest = box.locator(
                    "xpath=ancestor::*[.//*[@data-testid='tweetButton' or "
                    "@data-testid='tweetButtonInline']][1]"
                ).first
                if await self._count_locator(closest) and await self._is_visible(closest):
                    scopes.append(closest)
        unique: list[Any] = []
        for scope in scopes:
            if all(scope is not known for known in unique):
                unique.append(scope)
        return unique

    async def _find_composer_submit_control(
        self,
        box: Any,
        selectors: list[str],
        *,
        timeout_ms: int,
        reply: bool,
    ) -> Any | None:
        scopes = await self._composer_submit_scopes(box)
        for scope in scopes:
            control = await self._find_visible_in_scope(
                scope,
                selectors,
                timeout_ms=timeout_ms,
                require_enabled=True,
            )
            if control is not None:
                return control
        # Test doubles and alternate locator adapters may not expose ancestor
        # traversal. Keep the legacy lookup only for those non-Playwright objects.
        if reply and not hasattr(box, "locator"):
            return await self._find_reply_submit_button(box, timeout_ms=timeout_ms)
        return None

    async def _wait_for_composer_confirmation(
        self,
        box: Any,
        submit_control: Any,
        initial_text: str,
        *,
        timeout_seconds: float = 5.0,
    ) -> tuple[bool, str]:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        initial_had_text = bool(self._compact_created_text(initial_text))
        while True:
            with contextlib.suppress(Exception):
                if await self._count_locator(box) == 0:
                    return True, "composer_detached"
            value = await self._textbox_value(box)
            if initial_had_text and self._textbox_is_empty(value):
                return True, "box_empty"
            if hasattr(box, "is_visible") and not await self._is_visible(box):
                return True, "composer_closed"
            # Unreadable test doubles can still expose the prior public signal.
            # Real locators must show the stronger cleared/closed transition.
            if value is None and not hasattr(box, "locator"):
                with contextlib.suppress(Exception):
                    if not await self._is_button_enabled(submit_control):
                        return True, "send_button_disabled"
            if time.monotonic() >= deadline:
                return False, "trigger_unconfirmed"
            await asyncio.sleep(0.15)

    async def _composer_still_targets_post(self, box: Any, post_id: str) -> bool:
        """Revalidate reply/quote target linkage immediately before submit."""
        normalized = self._normalize_post_id(post_id)
        if box is None or not normalized or not hasattr(box, "locator"):
            return False
        try:
            if not await self._is_visible(box):
                return False
            dialog = box.locator("xpath=ancestor::*[@role='dialog'][1]").first
            if not await self._count_locator(dialog) or not await self._is_visible(dialog):
                return False
            target_link = await self._find_exact_status_link(dialog, normalized)
            return target_link is not None and await self._is_visible(target_link)
        except Exception:
            return False

    async def _submit_composer_once(
        self,
        box: Any,
        initial_text: str,
        *,
        selectors: list[str],
        reply: bool = False,
        target_post_id: str = "",
    ) -> tuple[bool, bool, str]:
        """Locate a scoped submit control, trigger it once, then observe state."""
        control = await self._find_composer_submit_control(
            box,
            selectors,
            timeout_ms=1800,
            reply=reply,
        )
        if control is None:
            return False, False, "submit_control_not_found"
        if target_post_id and not await self._composer_still_targets_post(box, target_post_id):
            return False, False, "target_relation_lost"
        ready, reason, _ = await self._mutation_commit_guard(box)
        if not ready:
            return False, False, f"mutation_guard:{reason}"
        # Once click() is invoked its outcome may be ambiguous. Never replay it.
        clicked = await self._safe_click_once(control)
        if not clicked:
            return True, False, "click_outcome_ambiguous"
        confirmed, signal = await self._wait_for_composer_confirmation(box, control, initial_text)
        return True, confirmed, signal

    async def _open_compose_box(self):
        # Prefer click-driven compose from home/sidebar.
        if not await self._looks_like_home_timeline():
            if not await self._open_home_via_click():
                with contextlib.suppress(Exception):
                    await self._goto(f"{self.BASE_URL}/home")
        compose_btn = await self._find_first(ui.COMPOSE_BUTTONS, timeout_ms=2000)
        if compose_btn:
            try:
                await self._click(compose_btn)
                await self.human.jitter(350, 1000)
                box = await self._find_first(ui.COMPOSE_TEXTBOXES, timeout_ms=2600)
                if box:
                    return box
            except Exception:
                pass

        # URL fallback only if click flow fails.
        compose_urls = [f"{self.BASE_URL}/compose/tweet", f"{self.BASE_URL}/compose/post"]
        for url in compose_urls:
            try:
                await self._goto(url)
            except Exception as exc:
                logger.warning("open_compose_navigation_failed url=%s error=%s", url, str(exc)[:260])
                continue
            box = await self._find_first(ui.COMPOSE_TEXTBOXES, timeout_ms=2200)
            if box:
                return box
        return None

    async def _clear_textbox(self, box: Any) -> bool:
        """Clear only the supplied composer and verify the draft is empty."""
        if box is None or (hasattr(box, "is_visible") and not await self._is_visible(box)):
            return False
        try:
            if not await self._safe_click_once(box, timeout_ms=2200):
                return False
            await self._keyboard_press("Control+A")
            await self._keyboard_press("Backspace")
            await self.human.jitter(70, 220)
        except Exception as exc:
            logger.warning("textbox_clear_keyboard_failed error=%s", str(exc)[:260])
        value = await self._textbox_value(box)
        if self._textbox_is_empty(value):
            return True
        if await self._fill_textbox(box, ""):
            await self.human.jitter(50, 160)
            value = await self._textbox_value(box)
            if self._textbox_is_empty(value):
                return True
        logger.warning("textbox_clear_not_verified")
        return False

    async def _post_from_compose(self, text: str, image_paths: list[str]) -> str | None:
        self._last_post_submit_outcome = {
            "composer_opened": False,
            "triggered": False,
            "confirmed": False,
            "signal": "not_started",
        }
        if not self.page:
            return None
        if not str(text or "").strip() and not image_paths:
            self._last_mutation_guard_reason = "content_required"
            return None
        ready, _, _ = await self._mutation_guard()
        if not ready:
            return None
        baseline_ids = await self._snapshot_recent_owned_ids("post")
        box = await self._open_compose_box()
        if not box:
            logger.warning("post_text_compose_box_not_found")
            return None
        self._last_post_submit_outcome["composer_opened"] = True
        self._last_unknown_id_confirmation_signal = None
        await self._move_mouse()
        cleared = await self._clear_textbox(box)
        if cleared is False:
            logger.warning("post_text_compose_clear_not_verified")
            return None
        await self._type_text(box, text)
        if not await self._attach_images_to_composer(box, image_paths):
            logger.warning("post_text_media_attach_failed count=%s", len(image_paths))
            return None
        await self.human.jitter(280, 820)
        submitted_at = datetime.now(timezone.utc)
        triggered, confirmed, signal = await self._submit_composer_once(
            box,
            text,
            selectors=ui.POST_BUTTONS,
        )
        self._last_post_submit_outcome.update(
            {"triggered": triggered, "confirmed": confirmed, "signal": signal}
        )
        if not triggered:
            logger.warning("post_text_submit_control_not_found")
            return None
        if not confirmed:
            logger.warning("post_text_submit_not_confirmed signal=%s", signal)
            return None
        await self.human.jitter(900, 1700)
        post_id = await self._find_recent_own_created_post_id(
            "post",
            text,
            not_before=submitted_at,
            excluded_post_ids=baseline_ids,
        )
        if not post_id:
            logger.info("post_text_submitted_unknown_post_id")
            self._set_unknown_id_confirmation_signal("post", signal)
            return "unknown_post_id"
        return post_id

    async def post_text_detailed(self, text: str, image_paths: ImagePathInput | None = None) -> ActionResult:
        """Create a post and return structured action diagnostics.

        Args:
            text: Post body text to submit.
            image_paths: Optional image path(s) to attach before submit.
        """
        if not self.page:
            return await self._action_result("post", failure_reason="page_not_started", failure_stage=ActionFailureStage.NOT_STARTED)
        ready, reason, guard_raw = await self._mutation_guard()
        if not ready:
            return await self._action_result(
                "post",
                failure_reason=reason,
                failure_stage=ActionFailureStage.PREFLIGHT,
                raw={"health": guard_raw},
            )
        media_preflight = await self.attach_images_preflight(image_paths)
        if not media_preflight.ok:
            return await self._action_result(
                "post",
                failure_reason=str(media_preflight.errors[0].get("reason") or "media_preflight_failed"),
                failure_stage=ActionFailureStage.MEDIA_ATTACH,
                media_paths=media_preflight.normalized_paths,
                diagnostic={"media_preflight": media_preflight.to_dict()},
            )
        if not str(text or "").strip() and not media_preflight.normalized_paths:
            return await self._action_result(
                "post",
                failure_reason="content_required",
                failure_stage=ActionFailureStage.PREFLIGHT,
            )
        post_id = await self._post_from_compose(text, media_preflight.normalized_paths)
        if post_id:
            diagnostic = {}
            if post_id == "unknown_post_id":
                diagnostic = self._consume_unknown_id_confirmation_signal("post")
            result = await self._action_result(
                "post",
                ok=True,
                created_post_id=post_id,
                media_paths=media_preflight.normalized_paths,
                diagnostic=diagnostic,
            )
            result.composer_opened = True
            result.submit_clicked = True
            result.confirmation_observed = True
            result.media_attached = bool(media_preflight.normalized_paths)
            return result
        outcome = getattr(self, "_last_post_submit_outcome", {})
        if isinstance(outcome, dict) and bool(outcome.get("triggered")):
            result = await self._action_result(
                "post",
                failure_reason="submit_outcome_unknown",
                failure_stage=ActionFailureStage.POST_SUBMIT,
                media_paths=media_preflight.normalized_paths,
                diagnostic={
                    "confirmation_signal": {
                        "action": "post",
                        "signal": str(outcome.get("signal") or "trigger_unconfirmed")[:120],
                    }
                },
            )
            result.composer_opened = bool(outcome.get("composer_opened"))
            result.submit_clicked = True
            result.confirmation_observed = False
            result.media_attached = bool(media_preflight.normalized_paths)
            return result
        return await self._action_result(
            "post",
            failure_reason=self._mutation_guard_reason("submit_not_confirmed"),
            failure_stage=(
                ActionFailureStage.PREFLIGHT
                if getattr(self, "_last_mutation_guard_reason", "")
                else ActionFailureStage.POST_SUBMIT
            ),
            media_paths=media_preflight.normalized_paths,
        )

    async def post_text(self, text: str, image_paths: ImagePathInput | None = None) -> str | None:
        """Create a post and return the created post id when available.

        Args:
            text: Post body text to submit.
            image_paths: Optional image path(s) to attach before submit.
        """
        result = await self.post_text_detailed(text, image_paths=image_paths)
        return result.created_post_id if result.ok else None

    async def post_image(self, image_paths: ImagePathInput, text: str = "") -> str | None:
        """Create a post with one or more images (deprecated compatibility helper).

        Args:
            image_paths: Image path(s) to attach.
            text: Optional text content to include with the media post.
        """
        warnings.warn(
            "post_image() is deprecated; use post_text(text, image_paths=...) instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        image_files = await self._validated_image_paths_for_upload(image_paths)
        if not image_files:
            raise ValueError("At least one image path is required")
        return await self._post_from_compose(text, image_files)

    async def engage_post(
        self,
        platform_post_id: str,
        do_view: bool = True,
        do_like: bool = False,
        dwell_seconds: tuple[int, int] = (3, 8),
        return_to_previous: bool = True,
    ) -> dict[str, bool]:
        """Run the `engage_post` interaction workflow against a target post."""
        result = {"opened": False, "viewed": False, "liked": False}
        post_id = self._normalize_post_id(platform_post_id)
        if not post_id:
            return result
        if do_like:
            ready, _, _ = await self._mutation_guard()
            if not ready:
                return result
        if not await self._open_post_page(post_id):
            return result
        result["opened"] = True
        try:
            if do_view:
                min_s = max(1, int(dwell_seconds[0]))
                max_s = max(min_s, int(dwell_seconds[1]))
                await self.human.jitter(min_s * 1000, max_s * 1000)
                await self._random_scroll(random.randint(110, 360), random.randint(-70, 160))
                await self.human.jitter(550, 1500)
                result["viewed"] = True
            if do_like:
                ready, _, _ = await self._mutation_commit_guard()
                if ready:
                    result["liked"] = await self._like_in_current_context(post_id)
            return result
        except Exception as exc:
            logger.warning("engage_post_failed target=%s error=%s", platform_post_id, str(exc)[:260])
            return result
        finally:
            if return_to_previous:
                back_ok = False
                before_url = self.page.url if self.page else ""
                with contextlib.suppress(Exception):
                    await self._go_back()
                    after_url = self.page.url if self.page else ""
                    back_ok = bool(after_url and after_url != before_url)
                    if not back_ok and await self._looks_like_home_timeline():
                        back_ok = True
                if not back_ok:
                    await self._return_home()

    async def like_post_detailed(self, platform_post_id: str) -> ActionResult:
        """Run the `like_post_detailed` interaction workflow against a target post."""
        if not self.page:
            return await self._action_result("like", target_post_id=str(platform_post_id), failure_reason="page_not_started", failure_stage=ActionFailureStage.NOT_STARTED)
        post_id = self._normalize_post_id(platform_post_id)
        if not post_id:
            return await self._action_result("like", target_post_id=str(platform_post_id), failure_reason="target_post_not_found", failure_stage=ActionFailureStage.TARGET_LOOKUP)
        try:
            ready, reason, guard_raw = await self._mutation_guard()
            if not ready:
                return await self._action_result(
                    "like",
                    target_post_id=post_id,
                    failure_reason=reason,
                    failure_stage=ActionFailureStage.PREFLIGHT,
                    raw={"health": guard_raw},
                )
            preflight = await self.preflight_action(post_id, action="like")
            if not preflight.ok:
                return await self._action_result(
                    "like",
                    target_post_id=post_id,
                    failure_reason=preflight.reason or "preflight_failed",
                    failure_stage=ActionFailureStage.PREFLIGHT,
                    raw={"preflight": preflight.to_dict()},
                )
            ready, reason, guard_raw = await self._mutation_commit_guard()
            if not ready:
                return await self._action_result(
                    "like",
                    target_post_id=post_id,
                    failure_reason=reason,
                    failure_stage=ActionFailureStage.PREFLIGHT,
                    raw={"preflight": preflight.to_dict(), "health": guard_raw},
                )
            if await self._like_in_current_context(platform_post_id):
                result = await self._action_result("like", ok=True, target_post_id=post_id)
                result.confirmation_observed = True
                return result
            return await self._action_result(
                "like",
                target_post_id=post_id,
                failure_reason="like_state_not_confirmed",
                failure_stage=ActionFailureStage.CONFIRMATION,
                raw={"preflight": preflight.to_dict()},
            )
        except Exception as exc:
            if self._is_driver_connection_closed(exc):
                raise RuntimeError("playwright_driver_connection_closed") from exc
            if self._is_target_closed_error(exc):
                raise RuntimeError("target_page_or_context_closed") from exc
            logger.warning("like_post_failed target=%s error=%s", platform_post_id, str(exc)[:260])
            return await self._action_result(
                "like",
                target_post_id=post_id,
                failure_reason="unknown_exception",
                failure_stage=ActionFailureStage.UNKNOWN,
                diagnostic={"exception": type(exc).__name__, "message": str(exc)[:260]},
            )

    async def like_post(self, platform_post_id: str) -> bool:
        """Run the `like_post` interaction workflow against a target post."""
        result = await self.like_post_detailed(platform_post_id)
        return result.ok

    async def view_post(self, platform_post_id: str, dwell_seconds: tuple[int, int] = (3, 8)) -> bool:
        """Run the `view_post` interaction workflow against a target post."""
        result = await self.engage_post(platform_post_id, do_view=True, do_like=False, dwell_seconds=dwell_seconds)
        return bool(result.get("viewed"))

    async def view_post_detailed(self, platform_post_id: str, dwell_seconds: tuple[int, int] = (3, 8)) -> ActionResult:
        """Run the `view_post_detailed` interaction workflow against a target post."""
        post_id = self._normalize_post_id(platform_post_id)
        if not self.page:
            return await self._action_result("view", target_post_id=post_id, failure_reason="page_not_started", failure_stage=ActionFailureStage.NOT_STARTED)
        result = await self.engage_post(post_id, do_view=True, do_like=False, dwell_seconds=dwell_seconds)
        if result.get("viewed"):
            return await self._action_result("view", ok=True, target_post_id=post_id)
        return await self._action_result("view", target_post_id=post_id, failure_reason="target_post_not_found", failure_stage=ActionFailureStage.TARGET_LOOKUP)

    async def quote_post(
        self,
        platform_post_id: str,
        text: str = "",
        image_paths: ImagePathInput | None = None,
    ) -> str | None:
        """Run the `quote_post` interaction workflow against a target post."""
        if not self.page:
            return None
        self._last_quote_submit_outcome = {
            "composer_opened": False,
            "triggered": False,
            "confirmed": False,
            "signal": "not_started",
        }
        post_id = self._normalize_post_id(platform_post_id)
        if not post_id:
            return None
        image_files = await self._validated_image_paths_for_upload(image_paths)
        if not str(text or "").strip() and not image_files:
            self._last_mutation_guard_reason = "content_required"
            return None
        ready, _, _ = await self._mutation_guard()
        if not ready:
            return None
        baseline_ids = await self._snapshot_recent_owned_ids("quote")
        self._last_unknown_id_confirmation_signal = None
        try:
            if not await self._open_post_page(post_id):
                return None
            box = await self._open_quote_box_in_current_context(post_id)
            if not box:
                logger.warning("quote_composer_not_found target=%s", post_id)
                return None
            self._last_quote_submit_outcome["composer_opened"] = True

            await self._move_mouse()
            cleared = await self._clear_textbox(box)
            if cleared is False:
                logger.warning("quote_compose_clear_not_verified target=%s", post_id)
                return None
            await self._type_text(box, text)
            if not await self._attach_images_to_composer(box, image_files):
                logger.warning("quote_media_attach_failed target=%s count=%s", post_id, len(image_files))
                return None
            await self.human.jitter(280, 820)
            submitted_at = datetime.now(timezone.utc)
            triggered, confirmed, signal = await self._submit_composer_once(
                box,
                text,
                selectors=list(dict.fromkeys([*ui.POST_BUTTONS, *ui.REPLY_SEND_BUTTONS])),
                target_post_id=post_id,
            )
            self._last_quote_submit_outcome.update(
                {"triggered": triggered, "confirmed": confirmed, "signal": signal}
            )
            if not triggered:
                logger.warning("quote_submit_control_not_found target=%s", post_id)
                return None
            if not confirmed:
                logger.warning("quote_submit_not_confirmed target=%s signal=%s", post_id, signal)
                return None
            await self.human.jitter(900, 1700)
            quote_id = await self._find_recent_own_created_post_id(
                "quote",
                text,
                target_post_id=post_id,
                not_before=submitted_at,
                excluded_post_ids=baseline_ids,
            )
            if quote_id and quote_id != post_id:
                return quote_id
            self._set_unknown_id_confirmation_signal("quote", signal)
            return "unknown_quote_id"
        except Exception as exc:
            if self._is_driver_connection_closed(exc):
                raise RuntimeError("playwright_driver_connection_closed") from exc
            if self._is_target_closed_error(exc):
                raise RuntimeError("target_page_or_context_closed") from exc
            logger.warning("quote_post_failed target=%s error=%s", post_id, str(exc)[:260])
            return None

    async def quote_post_detailed(
        self,
        platform_post_id: str,
        text: str = "",
        image_paths: ImagePathInput | None = None,
    ) -> ActionResult:
        """Run the `quote_post_detailed` interaction workflow against a target post."""
        post_id = self._normalize_post_id(platform_post_id)
        if not self.page:
            return await self._action_result("quote", target_post_id=post_id, failure_reason="page_not_started", failure_stage=ActionFailureStage.NOT_STARTED)
        ready, reason, guard_raw = await self._mutation_guard()
        if not ready:
            return await self._action_result(
                "quote",
                target_post_id=post_id,
                failure_reason=reason,
                failure_stage=ActionFailureStage.PREFLIGHT,
                raw={"health": guard_raw},
            )
        media_preflight = await self.attach_images_preflight(image_paths)
        if not media_preflight.ok:
            return await self._action_result(
                "quote",
                target_post_id=post_id,
                failure_reason=str(media_preflight.errors[0].get("reason") or "media_preflight_failed"),
                failure_stage=ActionFailureStage.MEDIA_ATTACH,
                media_paths=media_preflight.normalized_paths,
                diagnostic={"media_preflight": media_preflight.to_dict()},
            )
        if not str(text or "").strip() and not media_preflight.normalized_paths:
            return await self._action_result(
                "quote",
                target_post_id=post_id,
                failure_reason="content_required",
                failure_stage=ActionFailureStage.PREFLIGHT,
            )
        preflight = await self.preflight_action(post_id, action="quote")
        if not preflight.ok:
            return await self._action_result(
                "quote",
                target_post_id=post_id,
                failure_reason=preflight.reason or "preflight_failed",
                failure_stage=ActionFailureStage.PREFLIGHT,
                media_paths=media_preflight.normalized_paths,
                raw={"preflight": preflight.to_dict()},
            )
        created_id = await self.quote_post(post_id, text=text, image_paths=media_preflight.normalized_paths)
        if created_id:
            diagnostic = {}
            if created_id == "unknown_quote_id":
                diagnostic = self._consume_unknown_id_confirmation_signal("quote")
            result = await self._action_result(
                "quote",
                ok=True,
                target_post_id=post_id,
                created_post_id=created_id,
                media_paths=media_preflight.normalized_paths,
                diagnostic=diagnostic,
                raw={"preflight": preflight.to_dict()},
            )
            result.composer_opened = True
            result.submit_clicked = True
            result.confirmation_observed = True
            result.media_attached = bool(media_preflight.normalized_paths)
            return result
        outcome = getattr(self, "_last_quote_submit_outcome", {})
        if isinstance(outcome, dict) and bool(outcome.get("triggered")):
            result = await self._action_result(
                "quote",
                target_post_id=post_id,
                failure_reason="submit_outcome_unknown",
                failure_stage=ActionFailureStage.POST_SUBMIT,
                media_paths=media_preflight.normalized_paths,
                diagnostic={
                    "confirmation_signal": {
                        "action": "quote",
                        "signal": str(outcome.get("signal") or "trigger_unconfirmed")[:120],
                    }
                },
                raw={"preflight": preflight.to_dict()},
            )
            result.composer_opened = bool(outcome.get("composer_opened"))
            result.submit_clicked = True
            result.confirmation_observed = False
            result.media_attached = bool(media_preflight.normalized_paths)
            return result
        return await self._action_result(
            "quote",
            target_post_id=post_id,
            failure_reason=self._mutation_guard_reason(
                preflight.reason or "quote_button_or_box_not_found"
            ),
            failure_stage=(
                ActionFailureStage.PREFLIGHT
                if getattr(self, "_last_mutation_guard_reason", "")
                else ActionFailureStage.COMPOSER_OPEN
            ),
            media_paths=media_preflight.normalized_paths,
            raw={"preflight": preflight.to_dict()},
        )

    async def quote_post_with_image(
        self,
        platform_post_id: str,
        image_paths: ImagePathInput,
        text: str = "",
    ) -> str | None:
        """Run the `quote_post_with_image` interaction workflow against a target post."""
        warnings.warn(
            "quote_post_with_image() is deprecated; use quote_post(..., image_paths=...) instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        image_files = await self._validated_image_paths_for_upload(image_paths)
        if not image_files:
            raise ValueError("At least one image path is required")
        return await self.quote_post(platform_post_id, text=text, image_paths=image_files)

    async def reply_to_post_detailed(
        self,
        platform_post_id: str,
        text: str,
        image_paths: ImagePathInput | None = None,
    ) -> ActionResult:
        """Run the `reply_to_post_detailed` interaction workflow against a target post."""
        if not self.page:
            return await self._action_result("reply", target_post_id=str(platform_post_id), failure_reason="page_not_started", failure_stage=ActionFailureStage.NOT_STARTED)
        post_id = self._normalize_post_id(platform_post_id)
        if not post_id:
            return await self._action_result("reply", target_post_id=str(platform_post_id), failure_reason="target_post_not_found", failure_stage=ActionFailureStage.TARGET_LOOKUP)
        ready, reason, guard_raw = await self._mutation_guard()
        if not ready:
            return await self._action_result(
                "reply",
                target_post_id=post_id,
                failure_reason=reason,
                failure_stage=ActionFailureStage.PREFLIGHT,
                raw={"health": guard_raw},
            )
        media_preflight = await self.attach_images_preflight(image_paths)
        if not media_preflight.ok:
            return await self._action_result(
                "reply",
                target_post_id=post_id,
                failure_reason=str(media_preflight.errors[0].get("reason") or "media_preflight_failed"),
                failure_stage=ActionFailureStage.MEDIA_ATTACH,
                media_paths=media_preflight.normalized_paths,
                diagnostic={"media_preflight": media_preflight.to_dict()},
            )
        image_files = media_preflight.normalized_paths
        if not str(text or "").strip() and not image_files:
            return await self._action_result(
                "reply",
                target_post_id=post_id,
                failure_reason="content_required",
                failure_stage=ActionFailureStage.PREFLIGHT,
            )
        ready, reason, guard_raw = await self._mutation_guard()
        if not ready:
            return await self._action_result(
                "reply",
                target_post_id=post_id,
                failure_reason=reason,
                failure_stage=ActionFailureStage.PREFLIGHT,
                media_paths=image_files,
                raw={"health": guard_raw},
            )
        baseline_ids = await self._snapshot_recent_owned_ids("reply")
        try:
            for attempt in range(1, 4):
                preflight = await self.preflight_action(post_id, action="reply")
                if not preflight.ok:
                    return await self._action_result(
                        "reply",
                        target_post_id=post_id,
                        failure_reason=preflight.reason or "preflight_failed",
                        failure_stage=ActionFailureStage.PREFLIGHT,
                        attempts=attempt,
                        media_paths=image_files,
                        raw={"preflight": preflight.to_dict()},
                    )
                box = await self._open_reply_box_in_current_context(post_id, scan_rounds=min(4, attempt + 1))
                if not box:
                    logger.warning("reply_button_or_box_not_found target=%s attempt=%s", post_id, attempt)
                    await self._dismiss_reply_ui()
                    await self._random_scroll(random.randint(-120, 260))
                    await self.human.jitter(160, 460)
                    continue

                if await self._has_reply_audience_modal():
                    if not await self._confirm_reply_audience_if_needed():
                        await self._dismiss_reply_ui()
                        return await self._action_result(
                            "reply",
                            target_post_id=post_id,
                            failure_reason="reply_audience_not_confirmed",
                            failure_stage=ActionFailureStage.CONFIRMATION,
                            attempts=attempt,
                            media_paths=image_files,
                        )
                try:
                    cleared = await self._clear_textbox(box)
                    if cleared is False:
                        raise RuntimeError("textbox_clear_not_verified")
                    await self._type_text(box, text)
                except Exception as exc:
                    logger.warning("reply_text_entry_failed target=%s attempt=%s error=%s", post_id, attempt, str(exc)[:260])
                    await self._dismiss_reply_ui()
                    return await self._action_result(
                        "reply",
                        target_post_id=post_id,
                        failure_reason="text_entry_failed",
                        failure_stage=ActionFailureStage.TEXT_ENTRY,
                        attempts=attempt,
                        media_paths=image_files,
                    )
                if not await self._attach_images_to_composer(box, image_files):
                    logger.warning(
                        "reply_media_attach_failed target=%s attempt=%s count=%s",
                        post_id,
                        attempt,
                        len(image_files),
                    )
                    await self._dismiss_reply_ui()
                    return await self._action_result(
                        "reply",
                        target_post_id=post_id,
                        failure_reason="media_upload_failed",
                        failure_stage=ActionFailureStage.MEDIA_ATTACH,
                        attempts=attempt,
                        media_paths=image_files,
                    )
                if await self._has_reply_audience_modal():
                    if not await self._confirm_reply_audience_if_needed():
                        await self._dismiss_reply_ui()
                        return await self._action_result(
                            "reply",
                            target_post_id=post_id,
                            failure_reason="reply_audience_not_confirmed",
                            failure_stage=ActionFailureStage.CONFIRMATION,
                            attempts=attempt,
                            media_paths=image_files,
                        )
                submitted_at = datetime.now(timezone.utc)
                triggered, confirmed, signal = await self._submit_composer_once(
                    box,
                    text,
                    selectors=ui.REPLY_SEND_BUTTONS,
                    reply=True,
                    target_post_id=post_id,
                )
                if not triggered:
                    logger.warning("reply_submit_trigger_not_found target=%s attempt=%s", post_id, attempt)
                    await self._dismiss_reply_ui()
                    return await self._action_result(
                        "reply",
                        target_post_id=post_id,
                        failure_reason=self._mutation_guard_reason(
                            "reply_submit_trigger_not_found"
                        ),
                        failure_stage=(
                            ActionFailureStage.PREFLIGHT
                            if getattr(self, "_last_mutation_guard_reason", "")
                            else ActionFailureStage.SUBMIT_LOOKUP
                        ),
                        attempts=attempt,
                        media_paths=image_files,
                    )
                if not confirmed:
                    logger.warning(
                        "reply_submit_not_confirmed target=%s attempt=%s signal=%s",
                        post_id,
                        attempt,
                        signal,
                    )
                    result = await self._action_result(
                        "reply",
                        target_post_id=post_id,
                        failure_reason="submit_outcome_unknown",
                        failure_stage=ActionFailureStage.POST_SUBMIT,
                        attempts=attempt,
                        media_paths=image_files,
                        diagnostic={"confirmation_signal": {"action": "reply", "signal": signal}},
                    )
                    result.composer_opened = True
                    result.submit_clicked = True
                    return result

                await self.human.jitter(700, 1500)
                reply_id = await self._find_recent_own_created_post_id(
                    "reply",
                    text,
                    target_post_id=post_id,
                    not_before=submitted_at,
                    excluded_post_ids=baseline_ids,
                )
                created_id = reply_id if reply_id and reply_id != post_id else "unknown_reply_id"
                diagnostic = (
                    {}
                    if created_id != "unknown_reply_id"
                    else self._unknown_id_confirmation_diagnostic("reply", signal)
                )
                result = await self._action_result(
                    "reply",
                    ok=True,
                    target_post_id=post_id,
                    created_post_id=created_id,
                    attempts=attempt,
                    media_paths=image_files,
                    diagnostic=diagnostic,
                )
                result.composer_opened = True
                result.submit_clicked = True
                result.confirmation_observed = True
                result.media_attached = bool(image_files)
                return result
            return await self._action_result(
                "reply",
                target_post_id=post_id,
                failure_reason="submit_not_confirmed",
                failure_stage=ActionFailureStage.POST_SUBMIT,
                attempts=3,
                media_paths=image_files,
            )
        except Exception as exc:
            if self._is_driver_connection_closed(exc):
                raise RuntimeError("playwright_driver_connection_closed") from exc
            if self._is_target_closed_error(exc):
                raise RuntimeError("target_page_or_context_closed") from exc
            logger.warning("reply_action_failed target=%s error=%s", post_id, str(exc)[:260])
            return await self._action_result(
                "reply",
                target_post_id=post_id,
                failure_reason="unknown_exception",
                failure_stage=ActionFailureStage.UNKNOWN,
                media_paths=image_files,
                diagnostic={"exception": type(exc).__name__, "message": str(exc)[:260]},
            )

    async def _reply_to_post_impl(
        self,
        platform_post_id: str,
        text: str,
        image_paths: ImagePathInput | None = None,
    ) -> str | None:
        result = await self.reply_to_post_detailed(platform_post_id, text, image_paths=image_paths)
        return result.created_post_id if result.ok else None

    async def reply_to_post(
        self,
        platform_post_id: str,
        text: str,
        image_paths: ImagePathInput | None = None,
    ) -> str | None:
        """Run the `reply_to_post` interaction workflow against a target post."""
        return await self._reply_to_post_impl(platform_post_id, text, image_paths=image_paths)

    async def reply_with_image(self, platform_post_id: str, image_paths: ImagePathInput, text: str = "") -> str | None:
        """Run the `reply_with_image` interaction workflow against a target post."""
        warnings.warn(
            "reply_with_image() is deprecated; use reply_to_post(..., image_paths=...) instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        image_files = await self._validated_image_paths_for_upload(image_paths)
        if not image_files:
            raise ValueError("At least one image path is required")
        return await self._reply_to_post_impl(platform_post_id, text, image_paths=image_files)

    async def _article_has_exact_primary_post_id(self, article: Any, post_id: str) -> bool:
        if article is None or not post_id:
            return False
        with contextlib.suppress(Exception):
            return await self._article_primary_post_id(article) == post_id
        return False

    async def _open_article_menu(
        self,
        article: Any | None,
        expected_post_id: str = "",
    ) -> Any | None:
        if article is None:
            return None
        target = await self._find_visible_in_scope(
            article,
            ui.POST_MENU_BUTTONS,
            timeout_ms=1400,
            require_enabled=True,
        )
        if target is None:
            logger.warning("open_article_menu_target_not_found")
            return None
        if expected_post_id and not await self._article_has_exact_primary_post_id(
            article,
            expected_post_id,
        ):
            logger.warning("open_article_menu_target_changed target=%s", expected_post_id)
            return None
        clicked = await self._safe_click_once(target)
        await self.human.jitter(240, 680)
        menu = await self._visible_overlay_scope("menu", timeout_ms=1800)
        if menu is None:
            logger.warning(
                "open_article_menu_overlay_not_found click_outcome=%s",
                "completed" if clicked else "ambiguous",
            )
        return menu

    async def _delete_owned_article(
        self,
        article: Any,
        item: dict[str, Any],
        expected_kind: str,
        own_handle: str,
    ) -> bool:
        self._last_delete_outcome = {
            "triggered": False,
            "confirmed": False,
            "signal": "not_started",
        }
        post_id = str(item.get("post_id") or "")
        if not post_id:
            return False
        ready, _, _ = await self._mutation_guard()
        if not ready:
            self._last_delete_outcome["signal"] = "mutation_guard_rejected"
            return False
        if not await self._article_has_exact_primary_post_id(article, post_id):
            logger.warning("delete_owned_item_article_id_mismatch target=%s", post_id)
            return False
        author = str(item.get("author") or "").lower()
        if author != own_handle.lower():
            logger.warning(
                "delete_owned_item_author_mismatch target=%s expected=%s actual=%s",
                post_id,
                own_handle,
                author or "-",
            )
            return False
        if expected_kind == "reply" and not bool(item.get("is_reply")):
            logger.warning("delete_reply_target_not_reply target=%s", post_id)
            return False
        if expected_kind == "quote" and not bool(item.get("is_quote")):
            logger.warning("delete_quote_target_not_quote target=%s", post_id)
            return False
        if expected_kind == "post" and (
            bool(item.get("is_reply")) or bool(item.get("is_repost")) or bool(item.get("is_quote"))
        ):
            logger.warning("delete_post_target_not_plain_post target=%s", post_id)
            return False
        menu = await self._open_article_menu(article, post_id)
        if menu is None:
            logger.warning("delete_owned_item_menu_open_failed target=%s", post_id)
            return False
        delete_item = await self._find_exact_control_in_scope(
            menu,
            ui.DELETE_MENU_ITEMS,
            {"delete", "delete post", "delete reply"},
            timeout_ms=1800,
        )
        if delete_item is None:
            logger.warning("delete_menu_item_not_found target=%s", post_id)
            return False
        ready, _, _ = await self._mutation_commit_guard(menu)
        if not ready:
            self._last_delete_outcome["signal"] = "mutation_guard_rejected_before_confirmation"
            return False
        # Treat the first Delete control as potentially irreversible. X can
        # change whether a separate confirmation sheet is shown.
        self._last_delete_outcome["triggered"] = True
        menu_item_clicked = await self._safe_click_once(delete_item)
        await self.human.jitter(260, 760)
        dialog = await self._visible_overlay_scope("dialog", timeout_ms=2200)
        if dialog is None:
            self._last_delete_outcome["signal"] = (
                "confirmation_dialog_not_found"
                if menu_item_clicked
                else "click_outcome_ambiguous"
            )
            logger.warning(
                "delete_confirm_dialog_not_found target=%s click_outcome=%s",
                post_id,
                "completed" if menu_item_clicked else "ambiguous",
            )
            return False
        confirm = await self._find_exact_control_in_scope(
            dialog,
            ui.DELETE_CONFIRM_BUTTONS,
            {"delete"},
            timeout_ms=1800,
            accepted_testids={"confirmationSheetConfirm"},
        )
        if confirm is None:
            logger.warning("delete_confirm_button_not_found target=%s", post_id)
            return False
        ready, _, _ = await self._mutation_commit_guard(dialog)
        if not ready:
            self._last_delete_outcome["signal"] = "mutation_guard_rejected_before_confirm"
            return False
        self._last_delete_outcome["triggered"] = True
        confirm_clicked = await self._safe_click_once(confirm)
        await self.human.jitter(500, 1300)
        await self._wait_network_idle(1200)
        confirmed = await self._wait_for_profile_article_disappearance(own_handle, post_id, expected_kind)
        self._last_delete_outcome.update(
            {
                "confirmed": confirmed,
                "signal": (
                    "article_disappeared"
                    if confirmed
                    else "state_not_confirmed"
                    if confirm_clicked
                    else "click_outcome_ambiguous"
                ),
            }
        )
        return confirmed

    async def _delete_owned_status_item(self, platform_post_id: str, expected_kind: str) -> bool:
        if not self.page:
            return False
        ready, _, _ = await self._mutation_guard()
        if not ready:
            return False
        post_id = self._normalize_post_id(platform_post_id)
        if not post_id:
            return False
        own_handle = await self._get_authenticated_handle()
        if not own_handle:
            logger.warning("delete_owned_item_missing_authenticated_handle target=%s", post_id)
            return False
        direct = await self._find_owned_status_article_by_post_id(own_handle, post_id, expected_kind)
        if direct is not None:
            article, item = direct
            if await self._delete_owned_article(article, item, expected_kind, own_handle):
                return True
            if bool(getattr(self, "_last_delete_outcome", {}).get("triggered")):
                logger.warning("direct_delete_status_outcome_ambiguous target=%s kind=%s", post_id, expected_kind)
                return False
            if getattr(self, "_last_mutation_guard_reason", ""):
                return False
            logger.warning("direct_delete_status_failed target=%s kind=%s", post_id, expected_kind)
        article = await self._find_profile_article_by_post_id(own_handle, post_id, expected_kind)
        if article is None:
            logger.warning(
                "delete_owned_item_article_not_found target=%s kind=%s handle=%s",
                post_id,
                expected_kind,
                own_handle,
            )
            return False
        item = await self._classify_profile_article(article, own_handle)
        return await self._delete_owned_article(article, item, expected_kind, own_handle)

    async def _undo_repost_article(self, article: Any, post_id: str) -> bool:
        self._last_repost_delete_outcome = {
            "triggered": False,
            "confirmed": False,
            "signal": "not_started",
        }
        if not self.page or article is None:
            return False
        ready, _, _ = await self._mutation_guard()
        if not ready:
            self._last_repost_delete_outcome["signal"] = "mutation_guard_rejected"
            return False
        if not await self._article_has_exact_primary_post_id(article, post_id):
            logger.warning("undo_repost_article_id_mismatch target=%s", post_id)
            return False
        scope = article
        active_btn = await self._find_first_in_scope(
            scope,
            ui.REPOST_ACTIVE_BUTTONS,
            timeout_ms=1600,
            require_enabled=True,
        )
        if not active_btn:
            logger.warning("undo_repost_active_button_not_found target=%s", post_id)
            return False
        ready, _, _ = await self._mutation_commit_guard()
        if not ready:
            self._last_repost_delete_outcome["signal"] = "mutation_guard_rejected_before_confirmation"
            return False
        if not await self._article_has_exact_primary_post_id(article, post_id):
            self._last_repost_delete_outcome["signal"] = "target_article_changed"
            return False
        # The first active-repost control may itself toggle state on a changed
        # X surface, so no fallback is safe after it is invoked.
        self._last_repost_delete_outcome["triggered"] = True
        active_clicked = await self._safe_click_once(active_btn)
        await self.human.jitter(220, 680)
        menu = await self._visible_overlay_scope("menu", timeout_ms=1800)
        if menu is None:
            if not active_clicked:
                logger.warning("undo_repost_menu_outcome_ambiguous target=%s", post_id)
                self._last_repost_delete_outcome["signal"] = "click_outcome_ambiguous"
            else:
                self._last_repost_delete_outcome["signal"] = "confirmation_menu_not_found"
            return False
        confirm = await self._find_exact_control_in_scope(
            menu,
            ui.UNDO_REPOST_BUTTONS,
            {"undo repost"},
            timeout_ms=1800,
            accepted_testids={"unretweetConfirm"},
        )
        if confirm is None:
            return False
        ready, _, _ = await self._mutation_commit_guard(menu)
        if not ready:
            self._last_repost_delete_outcome["signal"] = "mutation_guard_rejected_before_confirm"
            return False
        self._last_repost_delete_outcome["triggered"] = True
        confirm_clicked = await self._safe_click_once(confirm)
        await self.human.jitter(300, 900)
        await self._wait_network_idle(900)
        refreshed = await self._find_exact_article(post_id)
        if refreshed is None:
            return False
        still_active = await self._find_first_in_scope(refreshed, ui.REPOST_ACTIVE_BUTTONS, timeout_ms=700)
        inactive_btn = await self._find_first_in_scope(
            refreshed,
            ui.REPOST_BUTTONS,
            timeout_ms=900,
            require_enabled=True,
        )
        confirmed = inactive_btn is not None and still_active is None
        self._last_repost_delete_outcome.update(
            {
                "confirmed": confirmed,
                "signal": (
                    "inactive_state_observed"
                    if confirmed
                    else "state_not_confirmed"
                    if confirm_clicked
                    else "click_outcome_ambiguous"
                ),
            }
        )
        return confirmed

    async def _undo_repost(self, platform_post_id: str) -> bool:
        if not self.page:
            return False
        ready, _, _ = await self._mutation_guard()
        if not ready:
            return False
        post_id = self._normalize_post_id(platform_post_id)
        if not post_id:
            return False
        own_handle = await self._get_authenticated_handle()
        if not own_handle:
            logger.warning("undo_repost_missing_authenticated_handle target=%s", post_id)
            return False
        direct = await self._find_post_article_in_context(post_id, scan_rounds=2)
        if direct is None:
            with contextlib.suppress(Exception):
                if await self._open_post_page(post_id):
                    direct = await self._find_post_article_in_context(post_id, scan_rounds=2)
        if direct is not None:
            if await self._undo_repost_article(direct, post_id):
                return True
            if bool(getattr(self, "_last_repost_delete_outcome", {}).get("triggered")):
                logger.warning("direct_undo_repost_outcome_ambiguous target=%s", post_id)
                return False
            if getattr(self, "_last_mutation_guard_reason", ""):
                return False
        article = await self._find_profile_article_by_post_id(own_handle, post_id, "repost")
        if article is None:
            logger.warning("undo_repost_article_not_found target=%s handle=%s", post_id, own_handle)
            return False
        return await self._undo_repost_article(article, post_id)

    async def _delete_all_profile_items(self, kind: str) -> list[str]:
        if not self.page:
            return []
        ready, _, _ = await self._mutation_guard()
        if not ready:
            return []
        own_handle = await self._get_authenticated_handle()
        if not own_handle:
            logger.warning("delete_all_profile_items_missing_authenticated_handle kind=%s", kind)
            return []
        deleted_urls: list[str] = []
        attempted_ids: set[str] = set()
        visible_batch_size = 6
        while True:
            if not await self._open_profile_surface(own_handle, kind):
                return deleted_urls
            scan_limit = visible_batch_size
            candidates = await self._collect_visible_profile_candidates(
                own_handle,
                kind,
                limit=scan_limit,
                excluded_post_ids=attempted_ids,
            )
            if not candidates:
                snapshot = await self._scroll_snapshot()
                at_bottom_before_scroll = self._is_snapshot_at_bottom(snapshot)
                logger.info(
                    "delete_all_profile_items_no_visible_candidate kind=%s bottom=%s deleted=%s attempted=%s",
                    kind,
                    at_bottom_before_scroll,
                    len(deleted_urls),
                    len(attempted_ids),
                )
                if at_bottom_before_scroll:
                    return deleted_urls
                reached_bottom = await self._scroll_profile_surface_forward(kind)
                if reached_bottom:
                    logger.info(
                        "delete_all_profile_items_bottom_reached kind=%s deleted=%s attempted=%s",
                        kind,
                        len(deleted_urls),
                        len(attempted_ids),
                    )
                    return deleted_urls
                continue
            progress = False
            for article, item in candidates:
                post_id = str(item.get("post_id") or "")
                if not post_id:
                    continue
                ready, _, _ = await self._mutation_guard()
                if not ready:
                    return deleted_urls
                attempted_ids.add(post_id)
                if kind == "repost":
                    ok = await self._undo_repost_article(article, post_id)
                else:
                    ok = await self._delete_owned_article(article, item, kind, own_handle)
                if ok:
                    deleted_urls.append(str(item.get("url") or f"{self.BASE_URL}/i/web/status/{post_id}"))
                    progress = True
                    break
                outcome = (
                    getattr(self, "_last_repost_delete_outcome", {})
                    if kind == "repost"
                    else getattr(self, "_last_delete_outcome", {})
                )
                if bool(outcome.get("triggered")) or getattr(
                    self, "_last_mutation_guard_reason", ""
                ):
                    return deleted_urls
                await self.human.jitter(280, 720)
            if not progress:
                return deleted_urls
            await self.human.jitter(420, 980)

    async def delete_post(self, platform_post_id: str) -> bool:
        """Delete owned X content using the `delete_post` flow."""
        if not self.page:
            return False
        try:
            return await self._delete_owned_status_item(platform_post_id, expected_kind="post")
        except Exception as exc:
            if self._is_driver_connection_closed(exc):
                raise RuntimeError("playwright_driver_connection_closed") from exc
            if self._is_target_closed_error(exc):
                raise RuntimeError("target_page_or_context_closed") from exc
            logger.warning("delete_post_failed target=%s error=%s", platform_post_id, str(exc)[:260])
            return False
        finally:
            with contextlib.suppress(Exception):
                await self._return_home()

    async def delete_reply(self, platform_post_id: str) -> bool:
        """Delete owned X content using the `delete_reply` flow."""
        if not self.page:
            return False
        try:
            return await self._delete_owned_status_item(platform_post_id, expected_kind="reply")
        except Exception as exc:
            if self._is_driver_connection_closed(exc):
                raise RuntimeError("playwright_driver_connection_closed") from exc
            if self._is_target_closed_error(exc):
                raise RuntimeError("target_page_or_context_closed") from exc
            logger.warning("delete_reply_failed target=%s error=%s", platform_post_id, str(exc)[:260])
            return False
        finally:
            with contextlib.suppress(Exception):
                await self._return_home()

    async def delete_repost(self, platform_post_id: str) -> bool:
        """Delete owned X content using the `delete_repost` flow."""
        if not self.page:
            return False
        try:
            return await self._undo_repost(platform_post_id)
        except Exception as exc:
            if self._is_driver_connection_closed(exc):
                raise RuntimeError("playwright_driver_connection_closed") from exc
            if self._is_target_closed_error(exc):
                raise RuntimeError("target_page_or_context_closed") from exc
            logger.warning("delete_repost_failed target=%s error=%s", platform_post_id, str(exc)[:260])
            return False
        finally:
            with contextlib.suppress(Exception):
                await self._return_home()

    async def delete_quote(self, platform_post_id: str) -> bool:
        """Delete owned X content using the `delete_quote` flow."""
        if not self.page:
            return False
        try:
            return await self._delete_owned_status_item(platform_post_id, expected_kind="quote")
        except Exception as exc:
            if self._is_driver_connection_closed(exc):
                raise RuntimeError("playwright_driver_connection_closed") from exc
            if self._is_target_closed_error(exc):
                raise RuntimeError("target_page_or_context_closed") from exc
            logger.warning("delete_quote_failed target=%s error=%s", platform_post_id, str(exc)[:260])
            return False
        finally:
            with contextlib.suppress(Exception):
                await self._return_home()

    async def delete_all_posts(self) -> list[str]:
        """Delete owned X content using the `delete_all_posts` flow."""
        return await self._delete_all_profile_items("post")

    async def delete_all_replies(self) -> list[str]:
        """Delete owned X content using the `delete_all_replies` flow."""
        return await self._delete_all_profile_items("reply")

    async def delete_all_reposts(self) -> list[str]:
        """Delete owned X content using the `delete_all_reposts` flow."""
        return await self._delete_all_profile_items("repost")

    async def delete_all_quotes(self) -> list[str]:
        """Delete owned X content using the `delete_all_quotes` flow."""
        return await self._delete_all_profile_items("quote")

    async def delete_all_content(self) -> dict[str, list[str]]:
        """Delete owned X content using the `delete_all_content` flow."""
        return {
            "reposts": await self.delete_all_reposts(),
            "quotes": await self.delete_all_quotes(),
            "replies": await self.delete_all_replies(),
            "posts": await self.delete_all_posts(),
        }

    async def delete_post_detailed(self, platform_post_id: str, kind: str = "post") -> ActionResult:
        """Delete owned X content using the `delete_post_detailed` flow."""
        post_id = self._normalize_post_id(platform_post_id)
        if not self.page:
            return await self._action_result("delete", target_post_id=post_id, failure_reason="page_not_started", failure_stage=ActionFailureStage.NOT_STARTED)
        if kind not in {"post", "reply", "repost", "quote"}:
            return await self._action_result(
                "delete",
                target_post_id=post_id,
                failure_reason="invalid_delete_kind",
                failure_stage=ActionFailureStage.PREFLIGHT,
                raw={"kind": kind},
            )
        self._last_delete_outcome = {"triggered": False, "confirmed": False, "signal": "not_started"}
        self._last_repost_delete_outcome = {
            "triggered": False,
            "confirmed": False,
            "signal": "not_started",
        }
        if kind == "reply":
            ok = await self.delete_reply(post_id)
        elif kind == "repost":
            ok = await self.delete_repost(post_id)
        elif kind == "quote":
            ok = await self.delete_quote(post_id)
        else:
            ok = await self.delete_post(post_id)
        outcome = (
            getattr(self, "_last_repost_delete_outcome", {})
            if kind == "repost"
            else getattr(self, "_last_delete_outcome", {})
        )
        triggered = bool(outcome.get("triggered"))
        confirmed = bool(outcome.get("confirmed"))
        signal = str(outcome.get("signal") or "")
        failure_reason = ""
        if not ok:
            if triggered:
                failure_reason = (
                    "delete_outcome_ambiguous"
                    if signal == "click_outcome_ambiguous"
                    else "delete_state_not_confirmed"
                )
            else:
                failure_reason = self._mutation_guard_reason("target_post_not_found")
        result = await self._action_result(
            "delete",
            ok=ok,
            target_post_id=post_id,
            failure_reason=failure_reason,
            failure_stage=(
                ActionFailureStage.CONFIRMATION
                if ok or triggered
                else ActionFailureStage.PREFLIGHT
                if getattr(self, "_last_mutation_guard_reason", "")
                else ActionFailureStage.TARGET_LOOKUP
            ),
            raw={"kind": kind, "confirmation_signal": signal},
        )
        result.submit_clicked = triggered
        result.confirmation_observed = confirmed
        return result

    async def repost_post_detailed(self, platform_post_id: str) -> ActionResult:
        """Execute the `repost_post_detailed` controller operation."""
        post_id = self._normalize_post_id(platform_post_id)
        if not self.page:
            return await self._action_result("repost", target_post_id=post_id, failure_reason="page_not_started", failure_stage=ActionFailureStage.NOT_STARTED)
        ready, reason, guard_raw = await self._mutation_guard()
        if not ready:
            return await self._action_result(
                "repost",
                target_post_id=post_id,
                failure_reason=reason,
                failure_stage=ActionFailureStage.PREFLIGHT,
                raw={"health": guard_raw},
            )
        if not post_id or not await self._open_post_page(post_id):
            return await self._action_result("repost", target_post_id=post_id, failure_reason="target_post_not_found", failure_stage=ActionFailureStage.TARGET_LOOKUP)
        try:
            article = await self._find_post_article_in_context(post_id, scan_rounds=1)
            if article is None or not await self._article_has_exact_primary_post_id(article, post_id):
                return await self._action_result(
                    "repost",
                    target_post_id=post_id,
                    failure_reason="target_article_mismatch",
                    failure_stage=ActionFailureStage.TARGET_LOOKUP,
                )
            active = await self._find_visible_in_scope(
                article,
                ui.REPOST_ACTIVE_BUTTONS,
                timeout_ms=500,
                require_enabled=True,
            )
            if active is not None:
                result = await self._action_result(
                    "repost",
                    ok=True,
                    target_post_id=post_id,
                    raw={"state": "already_reposted"},
                )
                result.confirmation_observed = True
                return result
            repost_btn = await self._find_visible_in_scope(
                article,
                ui.REPOST_BUTTONS,
                timeout_ms=1400,
                require_enabled=True,
            )
            if repost_btn is None:
                return await self._action_result("repost", target_post_id=post_id, failure_reason="repost_button_not_found", failure_stage=ActionFailureStage.TARGET_LOOKUP)
            ready, reason, guard_raw = await self._mutation_commit_guard()
            if not ready:
                return await self._action_result(
                    "repost",
                    target_post_id=post_id,
                    failure_reason=reason,
                    failure_stage=ActionFailureStage.PREFLIGHT,
                    raw={"health": guard_raw},
                )
            if not await self._article_has_exact_primary_post_id(article, post_id):
                return await self._action_result(
                    "repost",
                    target_post_id=post_id,
                    failure_reason="target_article_changed",
                    failure_stage=ActionFailureStage.TARGET_LOOKUP,
                )
            repost_clicked = await self._safe_click_once(repost_btn)
            await self.human.jitter(220, 680)
            menu = await self._visible_overlay_scope("menu", timeout_ms=1800)
            if menu is None:
                active_after_click = await self._find_visible_in_scope(
                    article,
                    ui.REPOST_ACTIVE_BUTTONS,
                    timeout_ms=900,
                    require_enabled=True,
                )
                if active_after_click is not None:
                    result = await self._action_result(
                        "repost",
                        ok=True,
                        target_post_id=post_id,
                        raw={"state": "reposted_without_confirmation_menu"},
                    )
                    result.submit_clicked = True
                    result.confirmation_observed = True
                    return result
                result = await self._action_result(
                    "repost",
                    target_post_id=post_id,
                    failure_reason=(
                        "repost_confirmation_menu_not_found"
                        if repost_clicked
                        else "repost_trigger_outcome_ambiguous"
                    ),
                    failure_stage=ActionFailureStage.CONFIRMATION,
                )
                result.submit_clicked = True
                return result
            confirm = await self._find_exact_control_in_scope(
                menu,
                ['[data-testid="retweetConfirm"]', '[role="menuitem"]', 'button', '[role="button"]'],
                {"repost"},
                timeout_ms=1800,
                accepted_testids={"retweetConfirm"},
            )
            if confirm is None:
                result = await self._action_result(
                    "repost",
                    target_post_id=post_id,
                    failure_reason="repost_confirmation_not_found",
                    failure_stage=ActionFailureStage.CONFIRMATION,
                )
                result.submit_clicked = True
                return result
            ready, reason, guard_raw = await self._mutation_commit_guard(menu)
            if not ready:
                result = await self._action_result(
                    "repost",
                    target_post_id=post_id,
                    failure_reason=reason,
                    failure_stage=ActionFailureStage.PREFLIGHT,
                    raw={"health": guard_raw},
                )
                result.submit_clicked = True
                return result
            confirm_clicked = await self._safe_click_once(confirm)
            await self.human.jitter(400, 1100)
            active = await self._find_visible_in_scope(
                article,
                ui.REPOST_ACTIVE_BUTTONS,
                timeout_ms=3200,
                require_enabled=True,
            )
            if active is None:
                result = await self._action_result(
                    "repost",
                    target_post_id=post_id,
                    failure_reason=(
                        "repost_state_not_confirmed"
                        if confirm_clicked
                        else "repost_confirmation_outcome_ambiguous"
                    ),
                    failure_stage=ActionFailureStage.CONFIRMATION,
                )
                result.submit_clicked = True
                return result
            result = await self._action_result(
                "repost",
                ok=True,
                target_post_id=post_id,
                raw={"state": "reposted"},
            )
            result.submit_clicked = True
            result.confirmation_observed = True
            return result
        except Exception as exc:
            if self._is_driver_connection_closed(exc):
                raise RuntimeError("playwright_driver_connection_closed") from exc
            if self._is_target_closed_error(exc):
                raise RuntimeError("target_page_or_context_closed") from exc
            return await self._action_result(
                "repost",
                target_post_id=post_id,
                failure_reason="unknown_exception",
                failure_stage=ActionFailureStage.UNKNOWN,
                diagnostic={"exception": type(exc).__name__, "message": str(exc)[:260]},
            )

    async def _profile_identity_matches(self, handle: str, *, timeout_ms: int = 3200) -> bool:
        if not self.page:
            return False
        expected = handle.casefold()
        self._verified_profile_action_scope = None
        self._verified_profile_handle = ""
        deadline = time.monotonic() + (max(0, timeout_ms) / 1000.0)
        while True:
            current = self._profile_handle_from_url(self.page.url or "")
            if current.casefold() != expected:
                return False
            with contextlib.suppress(Exception):
                blocks = self.page.locator('[data-testid="primaryColumn"] [data-testid="UserName"]')
                count = await self._count_locator(blocks)
                for idx in range(count):
                    block = blocks.nth(idx)
                    if not await self._is_visible(block):
                        continue
                    body = await self._inner_text(block, timeout_ms=800)
                    visible_handles = {
                        match.casefold()
                        for match in re.findall(r"(?<![\w@])@([A-Za-z0-9_]{1,15})(?![A-Za-z0-9_])", body or "")
                    }
                    if expected in visible_handles:
                        with contextlib.suppress(Exception):
                            root = block.locator(
                                "xpath=ancestor::*[.//*[@data-testid='userActions']][1]"
                            ).first
                            if await self._count_locator(root) and await self._is_visible(root):
                                self._verified_profile_action_scope = root
                                self._verified_profile_handle = handle
                                return True
            if timeout_ms <= 0 or time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.15)

    async def _open_verified_profile(self, username: str) -> str:
        handle = self._normalize_account_handle(username)
        if not self.page or not handle:
            return ""
        self._verified_profile_action_scope = None
        self._verified_profile_handle = ""
        await self._goto(f"{self.BASE_URL}/{handle}")
        await self._wait_network_idle(1400)
        if not await self._profile_identity_matches(handle):
            logger.warning("profile_identity_mismatch expected=%s url=%s", handle, self.page.url or "")
            return ""
        return handle

    async def _find_profile_follow_state(self, state: str, *, timeout_ms: int = 0) -> Any | None:
        if not self.page or state not in {"follow", "following"}:
            return None
        scope = getattr(self, "_verified_profile_action_scope", None)
        verified_handle = str(getattr(self, "_verified_profile_handle", "") or "")
        if scope is None or not verified_handle:
            return None
        if self._profile_handle_from_url(self.page.url or "").casefold() != verified_handle.casefold():
            return None
        return await self._find_exact_control_in_scope(
            scope,
            [
                '[data-testid="userActions"] button',
                '[data-testid="userActions"] [role="button"]',
            ],
            {state},
            timeout_ms=timeout_ms,
        )

    async def follow_user_detailed(self, username: str) -> ActionResult:
        """Run the `follow_user_detailed` follow-state action on the target account."""
        handle = self._normalize_account_handle(username)
        if not self.page:
            return await self._action_result("follow", failure_reason="page_not_started", failure_stage=ActionFailureStage.NOT_STARTED, raw={"username": handle})
        ok = await self.follow_user(handle)
        outcome = getattr(self, "_last_follow_outcome", {})
        triggered = bool(outcome.get("triggered"))
        confirmed = bool(outcome.get("confirmed"))
        signal = str(outcome.get("signal") or "")
        fallback = (self.last_action_error.message if self.last_action_error else "").strip() or "target_user_not_found"
        failure_reason = ""
        if not ok:
            failure_reason = (
                "follow_outcome_ambiguous"
                if triggered and signal == "click_outcome_ambiguous"
                else "follow_state_not_confirmed"
                if triggered
                else self._mutation_guard_reason(fallback)
            )
        result = await self._action_result(
            "follow",
            ok=ok,
            failure_reason=failure_reason,
            failure_stage=(
                ActionFailureStage.CONFIRMATION
                if ok or triggered
                else ActionFailureStage.PREFLIGHT
                if getattr(self, "_last_mutation_guard_reason", "")
                else ActionFailureStage.TARGET_LOOKUP
            ),
            raw={"username": handle, "confirmation_signal": signal},
        )
        result.submit_clicked = triggered
        result.confirmation_observed = confirmed
        return result

    async def unfollow_user_detailed(self, username: str) -> ActionResult:
        """Run the `unfollow_user_detailed` follow-state action on the target account."""
        handle = self._normalize_account_handle(username)
        if not self.page:
            return await self._action_result("unfollow", failure_reason="page_not_started", failure_stage=ActionFailureStage.NOT_STARTED, raw={"username": handle})
        ok = await self.unfollow_user(handle)
        outcome = getattr(self, "_last_unfollow_outcome", {})
        triggered = bool(outcome.get("triggered"))
        confirmed = bool(outcome.get("confirmed"))
        signal = str(outcome.get("signal") or "")
        fallback = (self.last_action_error.message if self.last_action_error else "").strip() or "target_user_not_found"
        failure_reason = ""
        if not ok:
            failure_reason = (
                "unfollow_outcome_ambiguous"
                if triggered and signal == "click_outcome_ambiguous"
                else "unfollow_state_not_confirmed"
                if triggered
                else self._mutation_guard_reason(fallback)
            )
        result = await self._action_result(
            "unfollow",
            ok=ok,
            failure_reason=failure_reason,
            failure_stage=(
                ActionFailureStage.CONFIRMATION
                if ok or triggered
                else ActionFailureStage.PREFLIGHT
                if getattr(self, "_last_mutation_guard_reason", "")
                else ActionFailureStage.TARGET_LOOKUP
            ),
            raw={"username": handle, "confirmation_signal": signal},
        )
        result.submit_clicked = triggered
        result.confirmation_observed = confirmed
        return result

    async def follow_user(self, username: str) -> bool:
        """Run the `follow_user` follow-state action on the target account."""
        self._clear_action_error()
        self._last_follow_outcome = {"triggered": False, "confirmed": False, "signal": "not_started"}
        handle = self._normalize_account_handle(username)
        if not handle:
            return False
        if not self.page:
            return False
        ready, _, _ = await self._mutation_guard()
        if not ready:
            return False
        try:
            verified_handle = await self._open_verified_profile(handle)
            if verified_handle.casefold() != handle.casefold():
                return False
            if await self._find_profile_follow_state("following", timeout_ms=500) is not None:
                self._last_follow_outcome.update({"confirmed": True, "signal": "already_following"})
                return True
            btn = await self._find_profile_follow_state("follow", timeout_ms=1800)
            if btn is None:
                return False
            ready, _, _ = await self._mutation_commit_guard()
            if not ready:
                return False
            self._last_follow_outcome["triggered"] = True
            clicked = await self._safe_click_once(btn)
            confirmed = await self._find_profile_follow_state("following", timeout_ms=3600)
            observed = confirmed is not None
            self._last_follow_outcome.update({
                "confirmed": observed,
                "signal": "following_state_observed" if observed else "state_not_confirmed" if clicked else "click_outcome_ambiguous",
            })
            return observed
        except Exception as exc:
            self._handle_soft_ui_error(
                "follow_user",
                exc,
                selector='[data-testid="primaryColumn"] [data-testid="userActions"]',
            )
            return False
        finally:
            with contextlib.suppress(Exception):
                await self._return_home()

    async def unfollow_user(self, username: str) -> bool:
        """Run the `unfollow_user` follow-state action on the target account."""
        self._clear_action_error()
        self._last_unfollow_outcome = {"triggered": False, "confirmed": False, "signal": "not_started"}
        handle = self._normalize_account_handle(username)
        if not handle:
            return False
        if not self.page:
            return False
        ready, _, _ = await self._mutation_guard()
        if not ready:
            return False
        try:
            verified_handle = await self._open_verified_profile(handle)
            if verified_handle.casefold() != handle.casefold():
                return False
            following = await self._find_profile_follow_state("following", timeout_ms=1800)
            if following is None:
                # Exact Follow state means the requested outcome already holds.
                already_unfollowed = await self._find_profile_follow_state("follow", timeout_ms=500) is not None
                if already_unfollowed:
                    self._last_unfollow_outcome.update({"confirmed": True, "signal": "already_unfollowed"})
                return already_unfollowed
            ready, _, _ = await self._mutation_commit_guard()
            if not ready:
                return False
            following_clicked = await self._safe_click_once(following)
            dialog = await self._visible_overlay_scope("dialog", timeout_ms=2200)
            if dialog is None:
                observed = await self._find_profile_follow_state("follow", timeout_ms=700) is not None
                self._last_unfollow_outcome.update({
                    "triggered": observed,
                    "confirmed": observed,
                    "signal": "follow_state_observed" if observed else "confirmation_dialog_not_found" if following_clicked else "click_outcome_ambiguous",
                })
                return observed
            confirm = await self._find_exact_control_in_scope(
                dialog,
                ['button', '[role="button"]'],
                {"unfollow"},
                timeout_ms=1800,
            )
            if confirm is None:
                return False
            ready, _, _ = await self._mutation_commit_guard(dialog)
            if not ready:
                return False
            self._last_unfollow_outcome["triggered"] = True
            confirm_clicked = await self._safe_click_once(confirm)
            observed = await self._find_profile_follow_state("follow", timeout_ms=3600) is not None
            self._last_unfollow_outcome.update({
                "confirmed": observed,
                "signal": "follow_state_observed" if observed else "state_not_confirmed" if confirm_clicked else "click_outcome_ambiguous",
            })
            return observed
        except Exception as exc:
            self._handle_soft_ui_error(
                "unfollow_user",
                exc,
                selector='[role="dialog"] button (exact Unfollow)',
            )
            return False
        finally:
            with contextlib.suppress(Exception):
                await self._return_home()

    async def post_metrics(self, platform_post_id: str) -> dict[str, int]:
        """Create content on X using the `post_metrics` flow."""
        if not await self._open_post_page(platform_post_id):
            return {}
        if not self.page:
            return {}
        try:
            article = await self._post_metrics_article(platform_post_id)
            if not article or not await self._count_locator(article):
                return {}
            metrics = self._zero_metrics()
            article_metrics = await self._extract_article_metrics(article)
            for key in metrics:
                metrics[key] = max(int(metrics.get(key) or 0), int(article_metrics.get(key) or 0))
            return metrics
        except Exception as exc:
            logger.warning("post_metrics_parse_failed target=%s error=%s", platform_post_id, str(exc)[:260])
        return {}

    async def _post_metrics_article(self, platform_post_id: str) -> Any | None:
        if not self.page:
            return None
        post_id = self._normalize_post_id(platform_post_id)
        if not post_id:
            return None
        article = await self._find_exact_article(post_id, max_articles=12)
        if article is None:
            logger.warning("post_metrics_target_article_missing target=%s", post_id)
        return article

    async def _status_page_has_rendered_article(self, post_id: str) -> bool:
        if not self.page or not await self._page_is_trusted(self.page):
            return False
        return await self._find_exact_article(post_id, max_articles=12) is not None

    async def _status_page_missing(self) -> bool:
        if not self.page:
            return False
        try:
            body = (await self._inner_text(self.page.locator("body"), timeout_ms=1000)).lower()
        except Exception:
            return False
        missing_markers = (
            "this page doesn't exist",
            "this page doesn\u2019t exist",
            "post is unavailable",
            "something went wrong",
        )
        return any(marker in body for marker in missing_markers)

    async def _wait_for_status_article(self, post_id: str, *, timeout_ms: int = 4500) -> bool:
        deadline = time.monotonic() + (max(500, timeout_ms) / 1000.0)
        while time.monotonic() < deadline:
            if await self._status_page_has_rendered_article(post_id):
                return True
            if await self._status_page_missing():
                return False
            await self.human.jitter(180, 420)
        return await self._status_page_has_rendered_article(post_id)

    async def _open_post_page(self, platform_post_id: str) -> bool:
        if not self.page:
            return False
        if not platform_post_id:
            return False
        post_id = self._normalize_post_id(platform_post_id)
        if not post_id:
            return False

        # Click-first only on an already trusted page.
        if await self._page_is_trusted(self.page):
            for _ in range(3):
                link = await self._find_exact_status_link(self.page, post_id)
                if link is not None:
                    try:
                        await self._click(link)
                        await self.human.jitter(280, 820)
                        await self._wait_network_idle(1300)
                        current_url = self.page.url or ""
                        if self._normalize_post_id(current_url) == post_id and await self._wait_for_status_article(post_id):
                            return True
                    except Exception as ex:
                        if self._is_driver_connection_closed(ex):
                            raise RuntimeError("playwright_driver_connection_closed") from ex
                        if self._is_target_closed_error(ex):
                            raise RuntimeError("target_page_or_context_closed") from ex
                        logger.warning("open_post_click_failed target=%s error=%s", post_id, str(ex)[:260])
                await self._random_scroll(random.randint(250, 760))
                await self.human.jitter(160, 440)

        # URL fallback only if clickable path fails.
        candidate_urls = [
            f"{self.BASE_URL}/i/web/status/{post_id}",
            f"{self.BASE_URL}/i/status/{post_id}",
        ]
        for candidate in candidate_urls:
            try:
                await self._goto(candidate)
                current_url = self.page.url or ""
                if self._normalize_post_id(current_url) == post_id and await self._wait_for_status_article(post_id):
                    return True
            except Exception as ex:
                if self._is_driver_connection_closed(ex):
                    raise RuntimeError("playwright_driver_connection_closed") from ex
                if self._is_target_closed_error(ex):
                    raise RuntimeError("target_page_or_context_closed") from ex
                logger.warning("open_post_navigation_failed target=%s url=%s error=%s", post_id, candidate, str(ex)[:260])
        logger.warning("open_post_failed target=%s", post_id)
        return False

    def _extract_post_id(self, href: str) -> str:
        return self._normalize_post_id(self._normalize_trusted_x_url(href))

    def _compact_created_text(self, value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip().lower()

    def _created_text_matches(self, expected: str, candidate: str) -> bool:
        expected_text = self._compact_created_text(expected)
        candidate_text = self._compact_created_text(candidate)
        if not expected_text or not candidate_text:
            return False
        if expected_text == candidate_text:
            return True
        if len(expected_text) < 12 or len(candidate_text) < 12:
            return False
        if expected_text in candidate_text or candidate_text in expected_text:
            return True
        prefix_len = min(90, len(expected_text), len(candidate_text))
        return prefix_len >= 28 and expected_text[:prefix_len] == candidate_text[:prefix_len]

    def _coerce_created_datetime(self, value: Any) -> datetime | None:
        if isinstance(value, datetime):
            parsed = value
        else:
            raw = str(value or "").strip()
            if not raw:
                return None
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _created_candidate_datetime(self, item: dict[str, Any]) -> datetime | None:
        parsed = self._coerce_created_datetime(item.get("created_at"))
        if parsed is not None:
            return parsed
        post_id = str(item.get("post_id") or "")
        if not post_id.isdigit():
            return None
        try:
            milliseconds = (int(post_id) >> 22) + 1_288_834_974_657
            return datetime.fromtimestamp(milliseconds / 1000.0, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    def _created_candidate_references_target(self, item: dict[str, Any], target_post_id: str) -> bool:
        if not target_post_id:
            return False
        direct = {
            str(item.get("reply_to_post_id") or ""),
            str(item.get("quoted_post_id") or ""),
            str(item.get("target_post_id") or ""),
        }
        nested: set[str] = set()
        for key in ("status_ids", "related_post_ids"):
            value = item.get(key)
            if isinstance(value, (list, tuple, set)):
                nested.update(str(candidate or "") for candidate in value)
        return target_post_id in direct or target_post_id in nested

    async def _snapshot_recent_owned_ids(self, kind: str, *, limit: int = 48) -> set[str]:
        """Capture a pre-submit ownership baseline so older rows cannot win resolution."""
        if not self.page:
            return set()
        own_handle = await self._get_authenticated_handle()
        if not own_handle:
            return set()
        previous_url = self.page.url or ""
        try:
            items = await self._collect_recent_owned_created_candidates(own_handle, kind, limit=limit)
            return {
                str(item.get("post_id") or "")
                for item in items
                if str(item.get("post_id") or "").isdigit()
            }
        except Exception as exc:
            logger.warning("created_post_baseline_failed kind=%s error=%s", kind, str(exc)[:260])
            return set()
        finally:
            if (
                previous_url
                and self.page
                and (self.page.url or "") != previous_url
                and self._is_trusted_x_url(previous_url)
            ):
                with contextlib.suppress(Exception):
                    await self._goto(previous_url)

    async def _find_recent_own_created_post_id(
        self,
        kind: str,
        text: str,
        *,
        target_post_id: str = "",
        not_before: datetime | str | None = None,
        excluded_post_ids: set[str] | None = None,
    ) -> str | None:
        if not self.page:
            return None
        own_handle = await self._get_authenticated_handle()
        if not own_handle:
            return None
        expected_kind = kind if kind in {"post", "reply", "quote"} else "post"
        submitted_at = self._coerce_created_datetime(not_before)
        recency_cutoff = submitted_at - timedelta(seconds=5) if submitted_at else None
        recency_ceiling = datetime.now(timezone.utc) + timedelta(seconds=60) if submitted_at else None
        excluded = excluded_post_ids or set()
        previous_url = self.page.url or ""
        try:
            items = await self._collect_recent_owned_created_candidates(own_handle, expected_kind, limit=48)
            matched: list[dict[str, Any]] = []
            for item in items:
                post_id = str(item.get("post_id") or "")
                if not post_id or post_id in excluded or (target_post_id and post_id == target_post_id):
                    continue
                candidate_at = self._created_candidate_datetime(item)
                if recency_cutoff is not None and (candidate_at is None or candidate_at < recency_cutoff):
                    continue
                if recency_ceiling is not None and candidate_at is not None and candidate_at > recency_ceiling:
                    continue
                if self._created_text_matches(text, str(item.get("text") or "")):
                    item = dict(item)
                    item["_resolved_created_at"] = candidate_at
                    matched.append(item)
            if matched:
                if submitted_at is not None:
                    if expected_kind == "quote":
                        matched = [
                            item
                            for item in matched
                            if bool(item.get("is_quote"))
                            and self._created_candidate_references_target(item, target_post_id)
                        ]
                    elif expected_kind == "reply":
                        matched = [item for item in matched if bool(item.get("is_reply"))]
                        relation_metadata_present = any(
                            any(item.get(key) for key in ("reply_to_post_id", "target_post_id", "related_post_ids"))
                            or len(item.get("status_ids") or []) > 1
                            for item in matched
                        )
                        if relation_metadata_present:
                            matched = [
                                item
                                for item in matched
                                if self._created_candidate_references_target(item, target_post_id)
                            ]
                    else:
                        matched = [
                            item
                            for item in matched
                            if not bool(item.get("is_reply")) and not bool(item.get("is_quote"))
                        ]
                    if not matched:
                        return None
                if expected_kind == "reply":
                    classified = [item for item in matched if bool(item.get("is_reply"))]
                elif expected_kind == "quote":
                    classified = [item for item in matched if bool(item.get("is_quote"))]
                else:
                    classified = [
                        item
                        for item in matched
                        if not bool(item.get("is_reply")) and not bool(item.get("is_quote"))
                    ]
                # Classification can be absent or imperfect on X surfaces. When
                # at least one candidate positively matches, discard conflicting
                # rows; otherwise retain the recency/text fail-safe behavior.
                if classified:
                    matched = classified

                def _score(item: dict[str, Any]) -> tuple[int, int, float, int]:
                    kind_match = int(
                        (expected_kind == "reply" and bool(item.get("is_reply")))
                        or (expected_kind == "quote" and bool(item.get("is_quote")))
                        or (
                            expected_kind == "post"
                            and not bool(item.get("is_reply"))
                            and not bool(item.get("is_quote"))
                        )
                    )
                    relation_match = int(self._created_candidate_references_target(item, target_post_id))
                    surface_match = int(
                        (expected_kind == "reply" and item.get("source_surface") == "with_replies")
                        or (expected_kind in {"post", "quote"} and item.get("source_surface") == "posts")
                    )
                    created_at = item.get("_resolved_created_at")
                    created_score = created_at.timestamp() if isinstance(created_at, datetime) else float("-inf")
                    return relation_match, kind_match, created_score, surface_match

                best = sorted(matched, key=_score, reverse=True)[0]
                logger.info(
                    "created_post_id_resolved_from_profile kind=%s post_id=%s source=%s is_reply=%s is_quote=%s",
                    expected_kind,
                    best.get("post_id"),
                    best.get("source_surface") or "",
                    bool(best.get("is_reply")),
                    bool(best.get("is_quote")),
                )
                return str(best.get("post_id") or "") or None
        except Exception as exc:
            logger.warning(
                "created_post_id_profile_resolve_failed kind=%s handle=%s error=%s",
                expected_kind,
                own_handle,
                str(exc)[:260],
            )
        finally:
            if previous_url and self.page and (self.page.url or "") != previous_url:
                with contextlib.suppress(Exception):
                    await self._goto(previous_url)
        return None

    async def _guess_recent_post_id(self, *, allow_page_content: bool = False) -> str | None:
        if not self.page:
            return None
        try:
            current = self.page.url
            match = self.STATUS_URL_RE.search(current or "")
            if match:
                return match.group(1)
            if not allow_page_content:
                return None
            text = await self._page_content()
            found = self.STATUS_URL_RE.search(text)
            return found.group(1) if found else None
        except Exception:
            return None
