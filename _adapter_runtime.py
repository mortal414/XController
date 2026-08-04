"""Lifecycle, startup, navigation, and low-level browser helpers for XTextAdapter."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Page, async_playwright
from playwright.sync_api import BrowserContext as SyncBrowserContext, Page as SyncPage, sync_playwright as sync_playwright

from . import _ui_selectors as ui
from ._diagnostics import ActionFailureInfo, ActionFailureStage, UIActionError
from .base import (
    ActionResult,
    LoginState,
    MediaCaptureData,
    ObservedMediaData,
    ObservedPostData,
)
from .human import HumanMotion
from .settings import ControllerSettings

logger = logging.getLogger(__name__)

ImagePath = str | os.PathLike[str]
ImagePathInput = ImagePath | Sequence[ImagePath]


class _AdapterRuntimeMixin:
    """Runtime lifecycle and UI interaction primitives shared across adapter concerns."""

    TRUSTED_X_HOSTS = frozenset({"x.com", "www.x.com"})
    NEUTRAL_PAGE_URLS = frozenset({"", "about:blank"})

    def __init__(self, profile_path: str, settings: Any | None = None, proxy: str | None = None):
        self.settings = ControllerSettings.from_any(settings)
        if profile_path is None or not str(profile_path).strip():
            raise ValueError("profile_path must be a non-empty path")
        profile_dir = Path(profile_path).expanduser()
        if not profile_dir.is_absolute():
            profile_dir = (Path.cwd() / profile_dir).resolve()
        profile_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(profile_dir, 0o700)
        self.profile_path = str(profile_dir)
        self.proxy = proxy
        self.playwright = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._sync_playwright = None
        self._sync_context: SyncBrowserContext | None = None
        self._sync_page: SyncPage | None = None
        self._sync_mode = False
        self._executor: ThreadPoolExecutor | None = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="social-agent-playwright",
        )
        self._authenticated_handle: str | None = None
        self.last_action_error: ActionFailureInfo | None = None
        self._recent_parser_warnings: list[dict[str, str]] = []
        self._recent_parser_warning_limit = 25
        self._operation_lock = asyncio.Lock()
        self._operation_owner: asyncio.Task[Any] | None = None
        self._operation_depth = 0
        self._locator_binding_prefix = (
            f"xcontroller-{time.monotonic_ns():x}-{random.getrandbits(32):08x}"
        )
        self._locator_binding_counter = 0
        self.human = HumanMotion(self.settings)

    @contextlib.asynccontextmanager
    async def _serialized_operation(self):
        """Serialize public workflows while allowing intentional nested calls."""
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("controller_operation_requires_async_task")
        if self._operation_owner is task:
            self._operation_depth += 1
            try:
                yield
            finally:
                self._operation_depth -= 1
            return

        await self._operation_lock.acquire()
        self._operation_owner = task
        self._operation_depth = 1
        try:
            yield
        finally:
            self._operation_depth = 0
            self._operation_owner = None
            self._operation_lock.release()

    async def start(self) -> None:
        """Start the Playwright runtime used by this adapter instance."""
        if self.context:
            try:
                if await self._page_is_safe_for_navigation(self.page):
                    return
                await self._refresh_page_handle()
                if await self._page_is_safe_for_navigation(self.page):
                    return
            except Exception as exc:
                logger.warning("Existing browser context is unusable; restarting. error=%s", str(exc)[:280])
            await self._hard_reset_browser_state()
        if self._sync_page:
            self.context = self._sync_context  # type: ignore[assignment]
            self.page = self._sync_page  # type: ignore[assignment]
            self._sync_mode = True
            try:
                if await self._page_is_safe_for_navigation(self.page):
                    return
                await self._refresh_page_handle()
                if await self._page_is_safe_for_navigation(self.page):
                    return
            except Exception as exc:
                logger.warning("Existing sync browser context is unusable; restarting. error=%s", str(exc)[:280])
            await self._hard_reset_browser_state()

        start_retries = 3
        for attempt in range(1, start_retries + 1):
            try:
                if self._prefer_sync_playwright():
                    await self._start_sync_fallback()
                    if self._sync_page is None:
                        raise RuntimeError("playwright_sync_fallback_not_initialized")
                    self.context = self._sync_context  # type: ignore[assignment]
                    self.page = self._sync_page  # type: ignore[assignment]
                    await self._wait_network_idle(1800)
                    return

                try:
                    self.playwright = await async_playwright().start()
                except NotImplementedError as exc:
                    logger.warning(
                        "Async Playwright start unavailable in this event loop; falling back to sync driver. %s",
                        exc,
                    )
                    await self._start_sync_fallback()
                else:
                    viewport = {
                        "width": random.randint(self.settings.browser_width_min, self.settings.browser_width_max),
                        "height": random.randint(self.settings.browser_height_min, self.settings.browser_height_max),
                    }
                    context_kwargs: dict[str, Any] = {
                        "headless": bool(self.settings.headless),
                        "viewport": viewport,
                    }
                    if self.settings.default_user_agent:
                        context_kwargs["user_agent"] = self.settings.default_user_agent
                    if self.proxy:
                        context_kwargs["proxy"] = {"server": self.proxy}
                    self.context = await self.playwright.chromium.launch_persistent_context(
                        user_data_dir=self.profile_path,
                        **context_kwargs,
                    )
                    self._sync_mode = False
                    await self._refresh_page_handle()
                    await self._wait_network_idle(1800)
                    return

                if self._sync_page is None:
                    raise RuntimeError("playwright_sync_fallback_not_initialized")
                self.context = self._sync_context  # type: ignore[assignment]
                self.page = self._sync_page  # type: ignore[assignment]
                await self._wait_network_idle(1800)
                return
            except Exception as exc:
                transient = self._is_target_closed_error(exc) or self._is_driver_connection_closed(exc)
                profile_issue = self._is_profile_in_use_error(exc)
                await self._hard_reset_browser_state()
                if attempt < start_retries and transient and not profile_issue:
                    logger.warning(
                        "Playwright start failed (attempt %s/%s). Retrying in %ss. error=%s",
                        attempt,
                        start_retries,
                        attempt,
                        str(exc)[:280],
                    )
                    await asyncio.sleep(float(attempt))
                    continue
                if profile_issue:
                    raise RuntimeError("profile_in_use") from exc
                if self._is_driver_connection_closed(exc):
                    raise RuntimeError("playwright_driver_connection_closed") from exc
                if self._is_target_closed_error(exc):
                    raise RuntimeError("target_page_or_context_closed") from exc
                raise

    def _prefer_sync_playwright(self) -> bool:
        if self.settings.prefer_sync_playwright is not None:
            return bool(self.settings.prefer_sync_playwright)
        mode = str(self.settings.playwright_mode or "auto").strip().lower()
        if mode == "sync":
            return True
        if mode == "async":
            return False
        # Windows asyncio subprocess handling is frequently unavailable in this runtime.
        # Prefer sync Playwright there to avoid repeated transport failures.
        return os.name == "nt"

    async def _start_sync_fallback(self) -> None:
        viewport = {
            "width": random.randint(self.settings.browser_width_min, self.settings.browser_width_max),
            "height": random.randint(self.settings.browser_height_min, self.settings.browser_height_max),
        }
        context_kwargs: dict[str, Any] = {
            "headless": bool(self.settings.headless),
            "viewport": viewport,
        }
        if self.settings.default_user_agent:
            context_kwargs["user_agent"] = self.settings.default_user_agent
        if self.proxy:
            context_kwargs["proxy"] = {"server": self.proxy}
        await self._run_sync(self._start_sync_browser, context_kwargs)
        self._sync_mode = True

    async def _hard_reset_browser_state(self) -> None:
        self.context = None
        self.page = None
        self._sync_mode = False
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass
            self.playwright = None
        if self._sync_playwright or self._sync_context or self._sync_page:
            try:
                await self._run_sync(self._close_sync_browser)
            except Exception:
                try:
                    self._close_sync_browser()
                except Exception:
                    pass

    def _start_sync_browser(self, context_kwargs: dict[str, Any]) -> None:
        if self._sync_playwright:
            try:
                if self._sync_context:
                    self._sync_context.close()
                self._sync_playwright.stop()
            except Exception:
                pass
            self._sync_playwright = None
            self._sync_context = None
            self._sync_page = None
        logger.info("Starting Playwright sync fallback")
        self._sync_playwright = sync_playwright().start()
        self._sync_context = self._sync_playwright.chromium.launch_persistent_context(
            user_data_dir=self.profile_path,
            **context_kwargs,
        )
        self._sync_page = self._select_or_create_sync_page()

    def _select_or_create_sync_page(self) -> SyncPage:
        if not self._sync_context:
            raise RuntimeError("Browser context not started")
        neutral_page: SyncPage | None = None
        for candidate in self._sync_context.pages:
            try:
                if candidate.is_closed():
                    continue
                url = str(candidate.url or "")
            except Exception:
                continue
            if self._is_trusted_x_url(url):
                return candidate
            if neutral_page is None and self._is_neutral_page_url(url):
                neutral_page = candidate
        return neutral_page or self._sync_context.new_page()

    async def _run_sync(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="social-agent-playwright")
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(self._executor, partial(func, *args, **kwargs))
        except Exception as exc:
            if self._is_profile_in_use_error(exc):
                raise RuntimeError("profile_in_use") from exc
            if self._is_driver_connection_closed(exc):
                raise RuntimeError("playwright_driver_connection_closed") from exc
            if self._is_target_closed_error(exc):
                raise RuntimeError("target_page_or_context_closed") from exc
            raise

    def _is_target_closed_error(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return (
            "target page, context or browser has been closed" in text
            or "targetclosederror" in text
            or "has been closed" in text and "page" in text
        )

    def _is_driver_connection_closed(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return (
            "connection closed while reading from the driver" in text
            or "connection closed while reading from driver" in text
            or "playwright_driver_connection_closed" in text
            or "driver process exited" in text
        )

    def _is_profile_in_use_error(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return (
            "profile appears to be in use" in text
            or "user data directory is already in use" in text
            or "process singleton" in text
            or "profile_in_use" in text
        )

    def _clear_action_error(self) -> None:
        self.last_action_error = None

    def _record_action_error(self, action: str, exc: Exception, *, selector: str = "") -> None:
        failure = ActionFailureInfo(
            action=action,
            error_type=type(exc).__name__,
            message=str(exc)[:260],
            url=self.page.url if self.page else "",
            selector=selector,
        )
        self.last_action_error = failure
        logger.warning(
            "ui_action_failed action=%s type=%s url=%s selector=%s error=%s",
            failure.action,
            failure.error_type,
            failure.url,
            failure.selector or "-",
            failure.message,
        )
        if self.settings.strict_ui_failures:
            raise UIActionError(failure) from exc

    def _handle_soft_ui_error(self, action: str, exc: Exception, *, selector: str = "") -> None:
        if isinstance(exc, self.PROGRAMMER_ERROR_TYPES):
            raise
        if self._is_driver_connection_closed(exc):
            raise RuntimeError("playwright_driver_connection_closed") from exc
        if self._is_target_closed_error(exc):
            raise RuntimeError("target_page_or_context_closed") from exc
        self._record_action_error(action, exc, selector=selector)

    async def _current_state_name(self) -> str:
        try:
            return str((await self.current_state()).get("state") or "")
        except Exception:
            return "unknown"

    async def _active_home_tab(self) -> str:
        if not self.page or not await self._page_is_trusted(self.page):
            return ""
        try:
            value = await self._evaluate(
                r"""() => {
                    const normalize = text => String(text || '').toLowerCase().replace(/\s+/g, ' ').trim();
                    const tabs = Array.from(document.querySelectorAll('[role="tab"], a[aria-current="page"]'));
                    for (const tab of tabs) {
                        const text = normalize(tab.innerText || tab.textContent || tab.getAttribute('aria-label'));
                        const current = tab.getAttribute('aria-selected') === 'true' || tab.getAttribute('aria-current') === 'page';
                        if (!current) continue;
                        if (text.includes('following')) return 'following';
                        if (text.includes('for you')) return 'for_you';
                    }
                    return '';
                }"""
            )
            if value in {"for_you", "following"}:
                return str(value)
        except Exception:
            pass
        return ""

    async def current_surface(self) -> dict[str, str]:
        """Return structured controller state from `current_surface`."""
        state = await self.current_state()
        state_name = str(state.get("state") or "")
        url = str(state.get("url") or "")
        active_home_tab = await self._active_home_tab()
        return {
            "state": state_name,
            "current_state": state_name,
            "url": url,
            "active_home_tab": active_home_tab,
            "active_tab": active_home_tab,
        }

    def sync(self):
        """Return the synchronous service facade for this adapter instance."""
        from .sync import SyncXController

        if self.context or self.page or self._sync_context or self._sync_page:
            raise RuntimeError("sync_facade_requires_unstarted_adapter")
        return SyncXController(adapter=self)

    async def _fill_action_context(self, result: ActionResult) -> ActionResult:
        result.current_url = self.page.url if self.page else ""
        result.current_state = await self._current_state_name()
        result.active_home_tab = await self._active_home_tab()
        if self.last_action_error:
            result.diagnostic = dict(result.diagnostic or {})
            result.diagnostic.setdefault("last_action_error", self.last_action_error.to_dict())
        return result

    async def _action_result(
        self,
        action: str,
        *,
        ok: bool = False,
        target_post_id: str = "",
        created_post_id: str = "",
        failure_reason: str = "",
        failure_stage: str = ActionFailureStage.UNKNOWN,
        attempts: int = 0,
        media_paths: list[str] | None = None,
        raw: dict[str, Any] | None = None,
        diagnostic: dict[str, Any] | None = None,
    ) -> ActionResult:
        post_id = self._normalize_post_id(target_post_id)
        result = ActionResult(
            ok=ok,
            action=action,
            target_post_id=post_id,
            created_post_id=created_post_id,
            target_url=f"{self.BASE_URL}/i/web/status/{post_id}" if post_id else "",
            failure_reason=failure_reason or ("unknown_failure" if not ok else ""),
            failure_stage="" if ok else (failure_stage or ActionFailureStage.UNKNOWN),
            attempts=attempts,
            media_paths=list(media_paths or []),
            diagnostic=dict(diagnostic or {}),
            raw=dict(raw or {}),
        )
        return await self._fill_action_context(result)

    async def _screenshot(self, path: str) -> None:
        if not self.page:
            return
        if self._sync_mode:
            await self._run_sync(self._sync_page.screenshot, path=path, full_page=True)
            return
        await self.page.screenshot(path=path, full_page=True)

    async def _wait_network_idle(self, ms: int) -> None:
        if not self.page:
            return
        if self._sync_mode:
            await self._run_sync(self._wait_network_idle_sync, self._sync_page, ms)
            return
        await self.human.wait_for_network_idle(self.page, ms=ms)

    def _wait_network_idle_sync(self, page: SyncPage | None, ms: int) -> None:
        if not page:
            return
        try:
            page.wait_for_load_state("networkidle", timeout=ms)
        except Exception as exc:
            if "timeout" in str(exc).lower() or "timeout" in type(exc).__name__.lower():
                return
            raise

    async def close(self) -> None:
        """Close the Playwright runtime used by this adapter instance."""
        first_error: Exception | None = None
        async_context = self.context if not self._sync_mode else None
        async_playwright_instance = self.playwright
        has_sync_runtime = bool(
            self._sync_mode or self._sync_context or self._sync_page or self._sync_playwright
        )
        try:
            if async_context:
                try:
                    await async_context.close()
                except Exception as exc:
                    first_error = exc

            if has_sync_runtime:
                try:
                    await self._run_sync(self._close_sync_browser)
                except RuntimeError as exc:
                    ignored = {
                        "target_page_or_context_closed",
                        "playwright_driver_connection_closed",
                        "profile_in_use",
                    }
                    if str(exc) not in ignored and first_error is None:
                        first_error = exc
                except Exception as exc:
                    if first_error is None:
                        first_error = exc

            if async_playwright_instance:
                try:
                    await async_playwright_instance.stop()
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
        finally:
            self.context = None
            self.page = None
            self.playwright = None
            self._sync_context = None
            self._sync_page = None
            self._sync_playwright = None
            self._sync_mode = False
            self._shutdown_executor()

        if first_error is not None:
            raise first_error

    def _close_sync_browser(self) -> None:
        if self._sync_context:
            try:
                self._sync_context.close()
            except Exception:
                pass
            self._sync_context = None
        if self._sync_playwright:
            try:
                self._sync_playwright.stop()
            except Exception:
                pass
            self._sync_playwright = None
        self._sync_page = None

        return

    def _shutdown_executor(self) -> None:
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    async def _count_locator(self, locator: Any) -> int:
        if self._sync_mode:
            return await self._run_sync(locator.count)
        return await locator.count()

    async def _any_selector(self, selectors: list[str]) -> bool:
        if not self.page:
            return False
        for selector in selectors:
            locator = self.page.locator(selector)
            if await self._count_locator(locator):
                return True
        return False

    async def _get_attribute(self, locator: Any, attr: str) -> str | None:
        if self._sync_mode:
            return await self._run_sync(locator.get_attribute, attr)
        return await locator.get_attribute(attr)

    async def _inner_text(self, locator: Any, timeout_ms: int | None = None) -> str:
        if self._sync_mode:
            if timeout_ms is None:
                return await self._run_sync(locator.inner_text)
            return await self._run_sync(locator.inner_text, timeout=timeout_ms)
        if timeout_ms is None:
            return await locator.inner_text()
        return await locator.inner_text(timeout=timeout_ms)

    async def _click(self, locator: Any) -> None:
        if self._sync_mode:
            await self._run_sync(locator.click, timeout=3500)
            return
        await locator.click(timeout=3500)

    def _click_sync(self, locator: Any) -> None:
        locator.click(timeout=3500)

    async def _type_text(self, element: Any, text: str) -> None:
        if not text:
            return
        if self._sync_mode:
            await self._run_sync(self._type_text_sync, element, text)
            return
        await self.human.type_like_human(element, text)

    def _type_text_sync(self, element: Any, text: str) -> None:
        # Keep sync mode operations inside a single worker thread.
        if not self._sync_page:
            return
        with contextlib.suppress(Exception):
            element.click(timeout=2200)
        for ch in text:
            self._sync_page.keyboard.type(ch)
            time.sleep(random.uniform(self.settings.anti_bot_typing_min_ms, self.settings.anti_bot_typing_max_ms) / 1000.0)
            if random.random() < 0.02:
                time.sleep(random.uniform(20, 220) / 1000.0)

    async def _set_input_files(self, locator: Any, files: list[str], timeout_ms: int = 6000) -> None:
        if self._sync_mode:
            await self._run_sync(locator.set_input_files, files, timeout=timeout_ms)
            return
        await locator.set_input_files(files, timeout=timeout_ms)

    async def _composer_scopes_for_textbox(self, textbox: Any | None, include_page: bool = True) -> list[Any]:
        scopes: list[Any] = []
        if textbox is not None:
            for xpath in (
                "ancestor::*[@role='dialog'][1]",
                "ancestor::*[self::form or self::article][1]",
            ):
                with contextlib.suppress(Exception):
                    scope = textbox.locator(f"xpath={xpath}").first
                    if await self._count_locator(scope):
                        scopes.append(scope)
        if include_page and self.page:
            scopes.append(self.page)
        return scopes

    async def _find_media_input_for_composer(self, textbox: Any | None, timeout_ms: int = 3000) -> Any | None:
        deadline = time.monotonic() + (max(0, timeout_ms) / 1000.0)
        while True:
            for scope in await self._composer_scopes_for_textbox(
                textbox,
                include_page=textbox is None,
            ):
                for selector in ui.MEDIA_INPUT_SELECTORS:
                    with contextlib.suppress(Exception):
                        locator = scope.locator(selector)
                        if await self._count_locator(locator):
                            return locator.first
            if timeout_ms <= 0 or time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.16)

    async def _wait_for_media_attachment(self, textbox: Any | None, timeout_seconds: float = 12.0) -> bool:
        deadline = time.monotonic() + max(1.0, timeout_seconds)
        while time.monotonic() < deadline:
            scopes = await self._composer_scopes_for_textbox(textbox, include_page=False)
            if not scopes and textbox is None and self.page:
                scopes = [self.page]
            for scope in scopes:
                for selector in ui.MEDIA_PREVIEW_SELECTORS:
                    with contextlib.suppress(Exception):
                        if await self._count_locator(scope.locator(selector)):
                            return True
            await asyncio.sleep(0.25)
        return False

    async def _attach_images_to_composer(self, textbox: Any | None, image_paths: list[str]) -> bool:
        if not image_paths:
            return True
        media_input = await self._find_media_input_for_composer(textbox, timeout_ms=3500)
        if not media_input:
            logger.warning("media_file_input_not_found count=%s", len(image_paths))
            return False
        try:
            await self._set_input_files(media_input, image_paths)
            await self.human.jitter(650, 1500)
            if await self._wait_for_media_attachment(textbox):
                return True
            logger.warning("media_upload_preview_not_found count=%s", len(image_paths))
            return False
        except Exception as exc:
            logger.warning("media_upload_failed count=%s error=%s", len(image_paths), str(exc)[:260])
            return False

    async def _random_scroll(self, *steps: int) -> None:
        if self._sync_mode:
            await self._run_sync(self._random_scroll_sync, *steps)
            return
        if not self.page:
            return
        if not steps:
            steps = (random.randint(-500, 650),)
        for index, step in enumerate(steps):
            await self._smooth_scroll_async(step)
            if index < len(steps) - 1:
                await self.human.jitter(90, 340)

    def _random_scroll_sync(self, *steps: int) -> None:
        if not self._sync_page:
            return
        if not steps:
            steps = (random.randint(-500, 650),)
        for index, step in enumerate(steps):
            self._smooth_scroll_sync(step)
            if index < len(steps) - 1:
                time.sleep(random.uniform(0.08, 0.35))

    async def _smooth_scroll_async(self, delta: int) -> None:
        if not self.page:
            return
        direction = 1 if delta >= 0 else -1
        remaining = abs(int(delta))
        if remaining == 0:
            return
        chunks = max(2, min(10, (remaining // 140) + 1))
        for idx in range(chunks):
            if remaining <= 0:
                break
            tail = max(1, chunks - idx)
            low = 50
            high = min(260, max(low + 10, remaining // tail + 90))
            chunk = min(remaining, random.randint(low, high))
            await self.page.mouse.wheel(0, direction * chunk)
            remaining -= chunk
            await self.human.jitter(45, 170)
        if remaining > 0:
            await self.page.mouse.wheel(0, direction * remaining)
            await self.human.jitter(45, 140)

    def _smooth_scroll_sync(self, delta: int) -> None:
        if not self._sync_page:
            return
        direction = 1 if delta >= 0 else -1
        remaining = abs(int(delta))
        if remaining == 0:
            return
        chunks = max(2, min(10, (remaining // 140) + 1))
        for idx in range(chunks):
            if remaining <= 0:
                break
            tail = max(1, chunks - idx)
            low = 50
            high = min(260, max(low + 10, remaining // tail + 90))
            chunk = min(remaining, random.randint(low, high))
            self._sync_page.mouse.wheel(0, direction * chunk)
            remaining -= chunk
            time.sleep(random.uniform(0.04, 0.17))
        if remaining > 0:
            self._sync_page.mouse.wheel(0, direction * remaining)
            time.sleep(random.uniform(0.04, 0.14))

    async def _move_mouse(self) -> None:
        if not self.page:
            return
        if self._sync_mode:
            await self._run_sync(self._move_mouse_sync)
            return
        await self.human.move_mouse_random(self.page)

    def _move_mouse_sync(self) -> None:
        if not self._sync_page:
            return
        viewport = self._sync_page.viewport_size or {"width": 1365, "height": 768}
        x = random.randint(0, viewport["width"])
        y = random.randint(0, viewport["height"])
        duration_ms = max(0, self.settings.anti_bot_mouse_move_ms)
        steps = max(1, min(120, round(duration_ms / 16)))
        self._sync_page.mouse.move(x, y, steps=steps)
        if duration_ms:
            time.sleep(duration_ms / 1000.0)
        time.sleep(random.uniform(20, 180) / 1000.0)

    async def _page_content(self) -> str:
        if not self.page:
            return ""
        if self._sync_mode:
            return await self._run_sync(self._page_content_sync, self._sync_page)
        return await self.page.content()

    def _page_content_sync(self, page: SyncPage | None) -> str:
        if not page:
            return ""
        try:
            return page.content()
        except Exception:
            return ""

    async def _evaluate(self, script: str) -> Any:
        if not self.page:
            return None
        if self._sync_mode:
            return await self._run_sync(self._sync_page.evaluate, script)
        return await self.page.evaluate(script)

    async def _new_context_page(self) -> Any | None:
        if self._sync_mode:
            if not self._sync_context:
                return None
            return await self._run_sync(self._sync_context.new_page)
        if not self.context:
            return None
        return await self.context.new_page()

    async def _close_context_page(self, page: Any | None) -> None:
        if not page:
            return
        try:
            if self._sync_mode:
                await self._run_sync(page.close)
            else:
                await page.close()
        except Exception:
            pass

    async def _page_url(self, page: Any | None) -> str:
        if not page:
            return ""
        try:
            if self._sync_mode:
                return str(await self._run_sync(lambda: getattr(page, "url", "") or ""))
            return str(getattr(page, "url", "") or "")
        except Exception:
            return ""

    async def _page_wait_network_idle(self, page: Any | None, ms: int = 1200) -> None:
        if not page:
            return
        try:
            if self._sync_mode:
                await self._run_sync(page.wait_for_load_state, "networkidle", timeout=ms)
            else:
                await page.wait_for_load_state("networkidle", timeout=ms)
        except Exception as exc:
            if "timeout" in str(exc).lower() or "timeout" in type(exc).__name__.lower():
                return
            raise

    async def _page_goto(self, page: Any, url: str) -> None:
        target_url = self._normalize_trusted_x_url(url)
        if not target_url:
            raise ValueError("navigation_url_must_use_trusted_x_origin")
        if not await self._page_is_safe_for_navigation(page):
            raise RuntimeError("unsafe_navigation_page")
        retries = max(1, int(self.NAV_RETRIES))
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                if self._sync_mode:
                    await self._run_sync(page.goto, target_url, wait_until="domcontentloaded")
                else:
                    await page.goto(target_url, wait_until="domcontentloaded")
                await self.human.jitter(300, 900)
                await self._page_wait_network_idle(page, 1400)
                current_url = await self._page_url(page)
                if not self._normalize_trusted_x_url(current_url):
                    self._detach_active_page(page)
                    raise RuntimeError("navigation_left_trusted_x_origin")
                return
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "page_navigation_failed attempt=%s/%s url=%s error=%s",
                    attempt,
                    retries,
                    self._redact_url_for_log(target_url),
                    str(exc)[:300],
                )
                if attempt >= retries or not self._is_retryable_error(exc):
                    if self._is_profile_in_use_error(exc):
                        raise RuntimeError("profile_in_use") from exc
                    if self._is_driver_connection_closed(exc):
                        raise RuntimeError("playwright_driver_connection_closed") from exc
                    if self._is_target_closed_error(exc):
                        raise RuntimeError("target_page_or_context_closed") from exc
                    raise
                await asyncio.sleep(min(1.8, 0.4 * attempt + 0.2))
        if last_exc:
            raise last_exc

    async def _page_evaluate(self, page: Any | None, script: str) -> Any:
        if not page:
            return None
        if self._sync_mode:
            return await self._run_sync(page.evaluate, script)
        return await page.evaluate(script)

    async def _scroll_snapshot(self) -> dict[str, float]:
        snapshot = await self._evaluate(
            """() => ({
                top: Number(window.scrollY || document.documentElement.scrollTop || 0),
                viewport: Number(window.innerHeight || document.documentElement.clientHeight || 0),
                total: Number(Math.max(
                    document.body ? document.body.scrollHeight : 0,
                    document.documentElement ? document.documentElement.scrollHeight : 0
                ) || 0)
            })"""
        )
        if not isinstance(snapshot, dict):
            return {"top": 0.0, "viewport": 0.0, "total": 0.0}
        return {
            "top": float(snapshot.get("top") or 0.0),
            "viewport": float(snapshot.get("viewport") or 0.0),
            "total": float(snapshot.get("total") or 0.0),
        }

    def _is_snapshot_at_bottom(self, snapshot: dict[str, float], margin_px: int = 180) -> bool:
        top = float(snapshot.get("top") or 0.0)
        viewport = float(snapshot.get("viewport") or 0.0)
        total = float(snapshot.get("total") or 0.0)
        if total <= 0:
            return False
        return (top + viewport) >= max(0.0, total - float(margin_px))

    async def _scroll_profile_surface_forward(self, kind: str) -> bool:
        before = await self._scroll_snapshot()
        step = random.randint(520, 1280) if kind == "reply" else random.randint(260, 760)
        await self._random_scroll(step)
        await self.human.jitter(180, 520 if kind == "reply" else 420)
        after = await self._scroll_snapshot()
        if self._is_snapshot_at_bottom(after):
            return True
        return abs(after.get("top", 0.0) - before.get("top", 0.0)) < 24.0

    def _is_retryable_error(self, exc: Exception) -> bool:
        return (
            self._is_target_closed_error(exc)
            or self._is_driver_connection_closed(exc)
            or "timeout" in str(exc).lower()
        )

    async def _refresh_page_handle(self) -> None:
        if not self.context:
            return
        if self._sync_mode:
            if not self._sync_context:
                self.page = None
                self._sync_page = None
                return
            selected = await self._run_sync(self._select_or_create_sync_page)
            self._sync_page = selected
            self.page = selected  # type: ignore[assignment]
            return

        neutral_page: Any | None = None
        for candidate in list(self.context.pages):
            if await self._page_is_closed(candidate):
                continue
            candidate_url = await self._page_url(candidate)
            if self._is_trusted_x_url(candidate_url):
                self.page = candidate
                return
            if neutral_page is None and self._is_neutral_page_url(candidate_url):
                neutral_page = candidate
        self.page = neutral_page or await self.context.new_page()

    async def _goto(self, url: str) -> None:
        target_url = self._normalize_trusted_x_url(url)
        if not target_url:
            raise ValueError("navigation_url_must_use_trusted_x_origin")
        retries = max(1, int(self.NAV_RETRIES))
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                if self.page and not await self._page_is_safe_for_navigation(self.page):
                    self._detach_active_page(self.page)
                if not self.page:
                    await self._refresh_page_handle()
                if not self.page:
                    raise RuntimeError("Browser context not started")
                page = self.page
                if self._sync_mode:
                    await self._run_sync(page.goto, target_url, wait_until="domcontentloaded")
                else:
                    await page.goto(target_url, wait_until="domcontentloaded")
                await self.human.jitter(300, 900)
                await self._wait_network_idle(1400)
                if not await self._page_is_trusted(page):
                    self._detach_active_page(page)
                    raise RuntimeError("navigation_left_trusted_x_origin")
                return
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "navigation_failed attempt=%s/%s url=%s error=%s",
                    attempt,
                    retries,
                    self._redact_url_for_log(target_url),
                    str(exc)[:300],
                )
                if attempt >= retries or not self._is_retryable_error(exc):
                    if self._is_profile_in_use_error(exc):
                        raise RuntimeError("profile_in_use") from exc
                    if self._is_driver_connection_closed(exc):
                        raise RuntimeError("playwright_driver_connection_closed") from exc
                    if self._is_target_closed_error(exc):
                        raise RuntimeError("target_page_or_context_closed") from exc
                    raise
                await asyncio.sleep(min(1.8, 0.4 * attempt + 0.2))
                await self._refresh_page_handle()
        if last_exc:
            raise last_exc

    async def _go_back(self) -> None:
        if not self.page:
            return
        if not await self._page_is_trusted(self.page):
            raise RuntimeError("unsafe_navigation_page")
        back_btn = await self._find_first(ui.BACK_BUTTONS, timeout_ms=900)
        if back_btn:
            try:
                await self._click(back_btn)
                await self.human.jitter(250, 700)
                await self._wait_network_idle(1100)
            except Exception:
                pass
            else:
                if await self._page_is_trusted(self.page):
                    return
                page = self.page
                self._detach_active_page(page)
                raise RuntimeError("navigation_left_trusted_x_origin")
        if self._sync_mode:
            await self._run_sync(self._sync_page.go_back, wait_until="domcontentloaded")
        else:
            await self.page.go_back(wait_until="domcontentloaded")
        await self.human.jitter(250, 700)
        await self._wait_network_idle(1100)
        if self.page and not await self._page_is_trusted(self.page):
            page = self.page
            self._detach_active_page(page)
            raise RuntimeError("navigation_left_trusted_x_origin")

    async def _keyboard_press(self, shortcut: str) -> None:
        if not self.page:
            return
        if self._sync_mode:
            await self._run_sync(self._sync_page.keyboard.press, shortcut)
        else:
            await self.page.keyboard.press(shortcut)

    def _normalize_trusted_x_url(self, value: Any) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        if raw.startswith("/"):
            raw = f"{self.BASE_URL}{raw}"
        try:
            parsed = urlparse(raw)
            port = parsed.port
        except (TypeError, ValueError):
            return ""
        if parsed.scheme.lower() != "https":
            return ""
        if (parsed.hostname or "").lower() not in {"x.com", "www.x.com"}:
            return ""
        if parsed.username or parsed.password or port not in {None, 443}:
            return ""
        return raw

    def _redact_url_for_log(self, value: Any) -> str:
        trusted = self._normalize_trusted_x_url(value)
        if not trusted:
            return "<untrusted-url>"
        parsed = urlparse(trusted)
        return parsed._replace(query="", fragment="").geturl()

    def _normalize_post_id(self, value: Any) -> str:
        raw = str(value or "").strip()
        if self.POST_ID_RE.fullmatch(raw):
            return raw
        if not self._is_trusted_x_url(raw):
            return ""
        match = re.search(r"/status/([0-9]+)(?=$|/)", urlparse(raw).path)
        return match.group(1) if match else ""

    def _normalize_username(self, username: str) -> str:
        return str(username or "").strip().lstrip("@")

    def _normalize_account_handle(self, username: str | None) -> str:
        handle = self._normalize_username(str(username or ""))
        if not handle or not self.ACCOUNT_HANDLE_RE.fullmatch(handle):
            return ""
        if handle.lower() in ui.RESERVED_PROFILE_PATHS:
            return ""
        return handle

    def _profile_handle_from_url(self, url: str) -> str:
        trusted_url = self._normalize_trusted_x_url(url)
        if not trusted_url:
            return ""
        try:
            parsed = urlparse(trusted_url)
        except Exception:
            return ""
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            return ""
        return self._normalize_account_handle(parts[0])

    def _is_trusted_x_url(self, value: Any) -> bool:
        raw = str(value or "").strip()
        return bool(raw and not raw.startswith("/") and self._normalize_trusted_x_url(raw))

    def _is_neutral_page_url(self, value: Any) -> bool:
        return str(value or "").strip().lower() in self.NEUTRAL_PAGE_URLS

    async def _page_is_closed(self, page: Any | None) -> bool:
        if page is None:
            return True
        checker = getattr(page, "is_closed", None)
        if not callable(checker):
            return False
        try:
            if self._sync_mode:
                return bool(await self._run_sync(checker))
            result = checker()
            if inspect.isawaitable(result):
                result = await result
            return bool(result)
        except Exception:
            return True

    async def _page_is_safe_for_navigation(self, page: Any | None) -> bool:
        if await self._page_is_closed(page):
            return False
        url = await self._page_url(page)
        return self._is_trusted_x_url(url) or self._is_neutral_page_url(url)

    async def _page_is_trusted(self, page: Any | None) -> bool:
        if await self._page_is_closed(page):
            return False
        return self._is_trusted_x_url(await self._page_url(page))

    def _detach_active_page(self, page: Any | None) -> None:
        if page is not None and self.page is page:
            self.page = None
        if page is not None and self._sync_page is page:
            self._sync_page = None

    def _normalize_image_paths(self, image_paths: ImagePathInput | None) -> list[str]:
        if image_paths is None:
            return []
        if isinstance(image_paths, (str, os.PathLike)):
            candidates = [image_paths]
        else:
            candidates = list(image_paths)

        if len(candidates) > ui.MAX_IMAGES_PER_POST:
            raise ValueError(f"X image posts support up to {ui.MAX_IMAGES_PER_POST} images")

        normalized: list[str] = []
        for candidate in candidates:
            path = Path(candidate).expanduser()
            if not path.is_absolute():
                path = (Path.cwd() / path).resolve()
            else:
                path = path.resolve()
            if not path.is_file():
                raise FileNotFoundError(f"Image file does not exist: {path}")
            if path.suffix.lower() not in ui.SUPPORTED_IMAGE_EXTENSIONS:
                supported = ", ".join(sorted(ui.SUPPORTED_IMAGE_EXTENSIONS))
                raise ValueError(f"Unsupported image extension for X upload: {path.suffix or '<none>'}. Supported: {supported}")
            normalized.append(str(path))
        return normalized

    async def _is_compose_state(self) -> bool:
        if not self.page or not await self._page_is_trusted(self.page):
            return False
        url = self.page.url or ""
        return "/compose/" in url

    async def _wait_for_post_submission(self, textbox: Any | None = None, timeout_seconds: float = 8.0) -> bool:
        if not self.page:
            return False
        started_in_compose = await self._is_compose_state()
        initial_text = ""
        if textbox is not None:
            with contextlib.suppress(Exception):
                initial_text = (await self._inner_text(textbox, timeout_ms=700)).strip()
        deadline = time.monotonic() + max(1.5, timeout_seconds)
        while time.monotonic() < deadline:
            if started_in_compose and not await self._is_compose_state():
                return True
            if textbox is not None:
                if not await self._count_locator(textbox) or not await self._is_visible(textbox):
                    return True
                if initial_text:
                    with contextlib.suppress(Exception):
                        if not (await self._inner_text(textbox, timeout_ms=500)).strip():
                            return True
            await asyncio.sleep(0.30)
        return False

    async def _submit_post(self, textbox: Any | None = None) -> bool:
        """Deliver at most one submit trigger and wait for an observable transition.

        A timeout is intentionally not retried: the first trigger may have reached X,
        and replaying it could create duplicate content.
        """
        if not self.page:
            return False
        post_btn = await self._find_first(ui.POST_BUTTONS, timeout_ms=900)
        if post_btn:
            disabled = (await self._get_attribute(post_btn, "aria-disabled") or "").strip().lower()
            if disabled in {"true", "1"}:
                logger.warning("submit_post_button_disabled")
                return False
            try:
                await self._click(post_btn)
            except Exception as exc:
                logger.warning("submit_post_button_click_failed error=%s", str(exc)[:260])
                return False
        else:
            shortcut = "Meta+Enter" if sys.platform == "darwin" else "Control+Enter"
            try:
                await self._keyboard_press(shortcut)
            except Exception as exc:
                logger.warning("submit_post_shortcut_failed key=%s error=%s", shortcut, str(exc)[:260])
                return False
        await self.human.jitter(280, 760)
        return await self._wait_for_post_submission(textbox, 4.5)

    async def _is_visible(self, locator: Any) -> bool:
        try:
            if self._sync_mode:
                return bool(await self._run_sync(locator.is_visible))
            return bool(await locator.is_visible())
        except Exception:
            return False

    async def _has_reply_audience_modal(self) -> bool:
        if not self.page:
            return False
        for modal_selector in ui.REPLY_AUDIENCE_MODAL_SELECTORS:
            modal = self.page.locator(modal_selector).first
            if not await self._count_locator(modal):
                continue
            if await self._is_visible(modal):
                return True
        return False

    async def _find_reply_audience_done_button(self, modal: Any) -> Any | None:
        if not self.page or modal is None:
            return None
        candidates: list[Any] = []
        with contextlib.suppress(Exception):
            candidates.append(modal.get_by_role("button", name=re.compile(r"^\s*Done\s*$", re.IGNORECASE)).first)
        for selector in ui.REPLY_AUDIENCE_DONE_BUTTONS:
            with contextlib.suppress(Exception):
                candidates.append(modal.locator(selector).first)
        with contextlib.suppress(Exception):
            text_span = modal.locator('span:has-text("Done")').first
            if await self._count_locator(text_span):
                candidates.append(text_span.locator("xpath=ancestor::*[self::button or @role='button'][1]").first)

        for candidate in candidates:
            try:
                if not await self._count_locator(candidate):
                    continue
                if not await self._is_visible(candidate):
                    continue
                if not await self._is_button_enabled(candidate):
                    continue
                return candidate
            except Exception:
                continue
        return None

    async def _confirm_reply_audience_if_needed(self) -> bool:
        if not self.page:
            return False
        if not await self._has_reply_audience_modal():
            return False
        logger.info("reply_audience_modal_detected")
        active_modal = None
        for modal_selector in ui.REPLY_AUDIENCE_MODAL_SELECTORS:
            modal = self.page.locator(modal_selector).first
            if not await self._count_locator(modal):
                continue
            if not await self._is_visible(modal):
                continue
            active_modal = modal
            break
        if active_modal is None:
            logger.warning("reply_audience_modal_detected_but_scope_missing")
            return False

        done_btn = await self._find_reply_audience_done_button(active_modal)
        if done_btn is None:
            logger.warning("reply_audience_done_button_not_found")
            return False
        try:
            await self._click(done_btn)
        except Exception as exc:
            logger.warning("reply_audience_done_click_failed error=%s", str(exc)[:260])
            return False

        logger.info("reply_audience_done_click")
        await self.human.jitter(220, 680)
        await self._wait_network_idle(800)
        for _ in range(10):
            if not await self._has_reply_audience_modal():
                logger.info("reply_audience_modal_closed")
                return True
            await asyncio.sleep(0.10)

        logger.warning("reply_audience_modal_still_open_after_done")
        return False

    async def _return_home(self, force_nav: bool = False) -> bool:
        if not self.page or not await self._page_is_trusted(self.page):
            return False
        if await self._looks_like_home_timeline():
            return True
        before = self.page.url or ""
        for _ in range(3):
            try:
                await self._go_back()
            except Exception:
                break
            if await self._looks_like_home_timeline():
                return True
            current = self.page.url or ""
            if current == before:
                break
            before = current
        if await self._open_home_via_click():
            return True
        if force_nav:
            await self._goto(f"{self.BASE_URL}/home")
            return await self._looks_like_home_timeline()
        return False

    async def _reload_current_page(self) -> None:
        if not self.page:
            return
        if not await self._page_is_trusted(self.page):
            raise RuntimeError("unsafe_navigation_page")
        page = self.page
        if self._sync_mode:
            await self._run_sync(page.reload, wait_until="domcontentloaded")
        else:
            await page.reload(wait_until="domcontentloaded")
        if not await self._page_is_trusted(page):
            self._detach_active_page(page)
            raise RuntimeError("navigation_left_trusted_x_origin")
        await self.human.jitter(260, 780)
        await self._wait_network_idle(1300)

    async def return_home(self, force_refresh: bool = False) -> bool:
        """Normalize navigation state using `return_home`."""
        if not self.page:
            return False
        home_ready = await self._return_home(force_nav=force_refresh)
        if not home_ready:
            return False
        if not force_refresh:
            return True
        try:
            await self._reload_current_page()
        except Exception as exc:
            logger.warning("return_home_refresh_failed error=%s", str(exc)[:260])
            return False
        return await self._looks_like_home_timeline()

    async def settle_after_action(
        self,
        tab: str = "for_you",
        force_refresh: bool = False,
        reset_scroll: bool = False,
    ) -> bool:
        """Normalize navigation state using `settle_after_action`."""
        requested_tab = str(tab or "for_you").strip().lower().replace("-", "_").replace(" ", "_")
        if requested_tab not in {"for_you", "following"}:
            requested_tab = "for_you"
        if force_refresh:
            settled = await self.return_home(force_refresh=True)
            if settled:
                settled = await self._select_home_tab(requested_tab)
        else:
            settled = await self.settle_home(requested_tab)
        if settled and reset_scroll and self.page:
            await self._keyboard_press("Home")
            await self.human.jitter(180, 520)
        return settled

    async def _find_first(self, selectors: list[str], timeout_ms: int = 0):
        if not self.page:
            raise RuntimeError("Browser context not started")
        deadline = time.monotonic() + (max(0, timeout_ms) / 1000.0)
        while True:
            for selector in selectors:
                locator = self.page.locator(selector)
                count = await self._count_locator(locator)
                for idx in range(count):
                    candidate = locator.nth(idx)
                    if await self._is_visible(candidate):
                        return candidate
            if timeout_ms <= 0 or time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.16)

    async def _is_button_enabled(self, locator: Any) -> bool:
        checker = getattr(locator, "is_enabled", None)
        if callable(checker):
            try:
                if self._sync_mode:
                    enabled = bool(await self._run_sync(checker))
                else:
                    result = checker()
                    if inspect.isawaitable(result):
                        result = await result
                    enabled = bool(result)
                if not enabled:
                    return False
            except Exception:
                return False
        aria_disabled = (await self._get_attribute(locator, "aria-disabled") or "").strip().lower()
        disabled_attr = (await self._get_attribute(locator, "disabled") or "").strip().lower()
        return aria_disabled not in {"true", "1"} and disabled_attr not in {"true", "1", "disabled"}

    async def _find_first_enabled(self, selectors: list[str], timeout_ms: int = 0):
        if not self.page:
            raise RuntimeError("Browser context not started")
        deadline = time.monotonic() + (max(0, timeout_ms) / 1000.0)
        while True:
            for selector in selectors:
                locator = self.page.locator(selector)
                count = await self._count_locator(locator)
                for i in range(count):
                    candidate = locator.nth(i)
                    if await self._is_visible(candidate) and await self._is_button_enabled(candidate):
                        return candidate
            if timeout_ms <= 0 or time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.16)

    async def _find_first_in_scope(
        self,
        scope: Any,
        selectors: list[str],
        timeout_ms: int = 0,
        require_enabled: bool = False,
    ):
        deadline = time.monotonic() + (max(0, timeout_ms) / 1000.0)
        while True:
            for selector in selectors:
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
            await asyncio.sleep(0.14)

    async def _bind_locator(self, locator: Any, purpose: str) -> tuple[Any | None, str]:
        """Return a stable locator for one DOM node, or fail if it detached.

        Playwright's ``nth()`` locators are live queries: an insertion ahead of the
        selected node can silently retarget a later click. A private, per-adapter
        attribute turns the selected node into an identity-keyed locator. The
        binding is re-counted and rechecked before it is returned, so a node that
        detached during selection fails closed.
        """
        if not self.page or locator is None or not await self._page_is_trusted(self.page):
            return None, ""
        self._locator_binding_counter += 1
        safe_purpose = re.sub(r"[^a-z0-9_-]+", "-", str(purpose or "node").casefold()).strip("-") or "node"
        proposed = f"{self._locator_binding_prefix}-{safe_purpose}-{self._locator_binding_counter:x}"
        script = """
            (node, payload) => {
                const attr = payload.attribute;
                const prefix = payload.prefix;
                const existing = node.getAttribute(attr) || '';
                if (existing.startsWith(prefix + '-')) return existing;
                node.setAttribute(attr, payload.token);
                return payload.token;
            }
        """
        try:
            payload = {
                "attribute": "data-xcontroller-binding",
                "prefix": self._locator_binding_prefix,
                "token": proposed,
            }
            if self._sync_mode:
                token = str(await self._run_sync(locator.evaluate, script, payload) or "")
            else:
                token = str(await locator.evaluate(script, payload) or "")
            if not token.startswith(f"{self._locator_binding_prefix}-"):
                return None, ""
            stable = self.page.locator(f'[data-xcontroller-binding="{token}"]').first
            if await self._count_locator(stable) != 1 or not await self._is_visible(stable):
                return None, ""
            return stable, token
        except Exception:
            return None, ""

    async def _snapshot_visible_bindings(self, selectors: list[str]) -> set[str]:
        """Bind and return identities for surfaces that are visible right now."""
        if not self.page or not await self._page_is_trusted(self.page):
            return set()
        tokens: set[str] = set()
        for selector in selectors:
            locator = self.page.locator(selector)
            for idx in range(await self._count_locator(locator)):
                candidate = locator.nth(idx)
                if not await self._is_visible(candidate):
                    continue
                _, token = await self._bind_locator(candidate, "baseline")
                if token:
                    tokens.add(token)
        return tokens

    async def _visible_scope_count(self, selector: str) -> int:
        """Count visible matches without touching a non-X page."""
        if not self.page or not await self._page_is_trusted(self.page):
            return 0
        locator = self.page.locator(selector)
        visible = 0
        for index in range(await self._count_locator(locator)):
            if await self._is_visible(locator.nth(index)):
                visible += 1
        return visible

    async def _find_new_bound_locator(
        self,
        scope: Any,
        selectors: list[str],
        baseline_tokens: set[str],
        *,
        timeout_ms: int,
        purpose: str,
        require_enabled: bool = False,
    ) -> Any | None:
        """Find a newly visible node and return its stable identity locator."""
        if not self.page or scope is None or not await self._page_is_trusted(self.page):
            return None
        deadline = time.monotonic() + (max(0, timeout_ms) / 1000.0)
        while True:
            for selector in selectors:
                locator = scope.locator(selector)
                for idx in range(await self._count_locator(locator)):
                    candidate = locator.nth(idx)
                    if not await self._is_visible(candidate):
                        continue
                    if require_enabled and not await self._is_button_enabled(candidate):
                        continue
                    stable, token = await self._bind_locator(candidate, purpose)
                    if stable is not None and token and token not in baseline_tokens:
                        return stable
            if timeout_ms <= 0 or time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.14)

    async def _composer_dialog_matches_target(
        self,
        textbox: Any,
        post_id: str,
        baseline_dialog_tokens: set[str],
    ) -> bool:
        """Verify that a new composer dialog visibly references the target post."""
        normalized = self._normalize_post_id(post_id)
        if textbox is None or not normalized:
            return False
        try:
            dialog = textbox.locator("xpath=ancestor::*[@role='dialog'][1]").first
            if not await self._count_locator(dialog) or not await self._is_visible(dialog):
                return False
            stable_dialog, token = await self._bind_locator(dialog, "composer-dialog")
            if stable_dialog is None or not token or token in baseline_dialog_tokens:
                return False
            target_link = await self._find_exact_status_link(stable_dialog, normalized)
            return target_link is not None and await self._is_visible(target_link)
        except Exception:
            return False

    async def _find_new_target_composer(
        self,
        post_id: str,
        baseline_textbox_tokens: set[str],
        baseline_dialog_tokens: set[str],
        *,
        timeout_ms: int,
    ) -> Any | None:
        """Return only a new composer whose dialog references ``post_id``."""
        if not self.page or not await self._page_is_trusted(self.page):
            return None
        deadline = time.monotonic() + (max(0, timeout_ms) / 1000.0)
        rejected_tokens: set[str] = set()
        while True:
            box = await self._find_new_bound_locator(
                self.page,
                ui.COMPOSE_TEXTBOXES,
                baseline_textbox_tokens | rejected_tokens,
                timeout_ms=0,
                purpose="target-composer",
            )
            if box is not None:
                token = (await self._get_attribute(box, "data-xcontroller-binding")) or ""
                if await self._composer_dialog_matches_target(box, post_id, baseline_dialog_tokens):
                    return box
                if token:
                    rejected_tokens.add(token)
            if timeout_ms <= 0 or time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.14)

    async def _find_reply_submit_button(self, textbox: Any | None = None, timeout_ms: int = 1400):
        if not self.page:
            return None
        scopes: list[Any] = []
        if textbox is not None:
            with contextlib.suppress(Exception):
                dialog = textbox.locator("xpath=ancestor::*[@role='dialog'][1]").first
                if await self._count_locator(dialog) and await self._is_visible(dialog):
                    scopes.append(dialog)
            with contextlib.suppress(Exception):
                parent = textbox.locator("xpath=ancestor::*[self::form or self::article][1]").first
                if await self._count_locator(parent) and await self._is_visible(parent):
                    scopes.append(parent)
        if textbox is None:
            scopes.append(self.page)

        for scope in scopes:
            btn = await self._find_first_in_scope(
                scope,
                ui.REPLY_SEND_BUTTONS,
                timeout_ms=timeout_ms,
                require_enabled=True,
            )
            if not btn:
                continue
            with contextlib.suppress(Exception):
                testid = (await self._get_attribute(btn, "data-testid")) or ""
                label = (await self._inner_text(btn) or "").strip().replace("\n", " ")[:80]
                logger.info("reply_submit_candidate testid=%s label=%s", testid or "-", label or "-")
            return btn

        if textbox is None:
            return await self._find_first_enabled(ui.REPLY_SEND_BUTTONS, timeout_ms=timeout_ms)
        return None

    async def _like_in_current_context(self, platform_post_id: str) -> bool:
        if not self.page:
            return False
        post_id = self._normalize_post_id(platform_post_id)
        if not post_id:
            return False
        article = await self._find_exact_article(post_id)
        if article is None:
            return False
        active_btn = await self._find_first_in_scope(article, ui.LIKE_ACTIVE_BUTTONS, timeout_ms=700)
        if active_btn:
            return True
        like_btn = await self._find_first_in_scope(
            article,
            ui.LIKE_BUTTONS,
            timeout_ms=1200,
            require_enabled=True,
        )
        if not like_btn:
            return False
        if await self._article_primary_post_id(article) != post_id:
            return False
        await self._click(like_btn)
        await self.human.jitter(350, 900)
        refreshed = await self._find_exact_article(post_id)
        if refreshed is None:
            return False
        return await self._find_first_in_scope(refreshed, ui.LIKE_ACTIVE_BUTTONS, timeout_ms=900) is not None

    async def _find_post_article_in_context(self, platform_post_id: str, scan_rounds: int = 1) -> Any | None:
        if not self.page:
            return None
        post_id = self._normalize_post_id(platform_post_id)
        if not post_id:
            return None

        rounds = max(1, int(scan_rounds))
        for idx in range(rounds):
            article = await self._find_exact_article(post_id)
            if article is not None:
                return article
            if idx < rounds - 1:
                await self._random_scroll(random.randint(240, 760))
                await self.human.jitter(130, 380)
        return None

    async def _dismiss_reply_ui(self) -> None:
        if not self.page:
            return
        close_btn = await self._find_first(ui.REPLY_CLOSE_BUTTONS, timeout_ms=400)
        if close_btn:
            with contextlib.suppress(Exception):
                await self._click(close_btn)
                await self.human.jitter(160, 460)
                await self._wait_network_idle(700)
                return
        with contextlib.suppress(Exception):
            await self._keyboard_press("Escape")
            await self.human.jitter(140, 380)

    async def _open_reply_box_in_current_context(self, platform_post_id: str, scan_rounds: int = 2) -> Any | None:
        if not self.page or not await self._page_is_trusted(self.page):
            return None
        post_id = self._normalize_post_id(platform_post_id)
        if not post_id:
            return None
        baseline_boxes = await self._snapshot_visible_bindings(ui.COMPOSE_TEXTBOXES)
        baseline_dialogs = await self._snapshot_visible_bindings(['[role="dialog"]'])
        article = await self._find_post_article_in_context(platform_post_id, scan_rounds=max(1, scan_rounds))
        reply_btn = None
        if article:
            reply_btn = await self._find_first_in_scope(
                article,
                ui.COMMENT_BUTTONS,
                timeout_ms=1100,
                require_enabled=True,
            )
        if not reply_btn:
            return None

        if await self._article_primary_post_id(article) != post_id:
            return None
        await self._click(reply_btn)
        await self.human.jitter(280, 900)
        box = await self._find_new_target_composer(
            post_id,
            baseline_boxes,
            baseline_dialogs,
            timeout_ms=2600,
        )
        if box:
            return box
        if await self._confirm_reply_audience_if_needed():
            box = await self._find_new_target_composer(
                post_id,
                baseline_boxes,
                baseline_dialogs,
                timeout_ms=1400,
            )
            if box:
                return box
        return None

    async def _open_quote_box_in_current_context(self, platform_post_id: str, scan_rounds: int = 2) -> Any | None:
        if not self.page or not await self._page_is_trusted(self.page):
            return None
        post_id = self._normalize_post_id(platform_post_id)
        if not post_id:
            return None
        baseline_boxes = await self._snapshot_visible_bindings(ui.COMPOSE_TEXTBOXES)
        baseline_dialogs = await self._snapshot_visible_bindings(['[role="dialog"]'])
        baseline_menus = await self._snapshot_visible_bindings(['[role="menu"]'])
        article = await self._find_post_article_in_context(platform_post_id, scan_rounds=max(1, scan_rounds))
        repost_btn = None
        if article:
            repost_btn = await self._find_first_in_scope(
                article,
                ui.REPOST_BUTTONS,
                timeout_ms=1400,
                require_enabled=True,
            )
        if not repost_btn:
            return None

        if await self._article_primary_post_id(article) != post_id:
            return None
        await self._click(repost_btn)
        await self.human.jitter(220, 700)
        menu = await self._find_new_bound_locator(
            self.page,
            ['[role="menu"]'],
            baseline_menus,
            timeout_ms=2600,
            purpose="quote-menu",
        )
        quote_item = (
            await self._find_first_in_scope(
                menu,
                ui.QUOTE_MENU_ITEMS,
                timeout_ms=600,
                require_enabled=True,
            )
            if menu is not None
            else None
        )
        if not quote_item:
            logger.warning("quote_menu_item_not_found target=%s", post_id or platform_post_id)
            with contextlib.suppress(Exception):
                await self._keyboard_press("Escape")
                await self.human.jitter(120, 360)
            return None
        await self._click(quote_item)
        await self.human.jitter(350, 1000)
        box = await self._find_new_target_composer(
            post_id,
            baseline_boxes,
            baseline_dialogs,
            timeout_ms=3200,
        )
        if box:
            return box
        with contextlib.suppress(Exception):
            await self._keyboard_press("Escape")
            await self.human.jitter(120, 360)
        return None

    async def _looks_like_home_timeline(self) -> bool:
        if not self.page or not await self._page_is_trusted(self.page):
            return False
        if await self._any_selector(ui.HOME_SELECTORS):
            return True
        if "/home" in (self.page.url or ""):
            return True
        primary = self.page.locator('div[data-testid="primaryColumn"]').first
        if not await self._count_locator(primary):
            return False
        aria_label = (await self._get_attribute(primary, "aria-label") or "").lower()
        aria_labelledby = (await self._get_attribute(primary, "aria-labelledby") or "").lower()
        if "home timeline" in aria_label:
            return True
        return "home" in aria_labelledby and "timeline" in aria_labelledby

    async def _looks_like_notifications_page(self) -> bool:
        if not self.page or not await self._page_is_trusted(self.page):
            return False
        url = (self.page.url or "").lower()
        if "/notifications" in url:
            return True
        return await self._any_selector(ui.NOTIFICATIONS_ACTIVE_SELECTORS)

    async def _open_home_via_click(self) -> bool:
        if not self.page or not await self._page_is_trusted(self.page):
            return False
        if await self._looks_like_home_timeline():
            return True
        for _ in range(2):
            home_btn = await self._find_first(ui.HOME_ENTRY_SELECTORS, timeout_ms=1200)
            if home_btn:
                try:
                    await self._click(home_btn)
                    await self.human.jitter(220, 620)
                    await self._wait_network_idle(1200)
                except Exception as exc:
                    logger.warning("open_home_click_failed error=%s", str(exc)[:260])
                if await self._looks_like_home_timeline():
                    return True
        return False

    async def _select_home_tab(self, tab: str) -> bool:
        if not self.page or not await self._page_is_trusted(self.page):
            return False
        target = str(tab or "for_you").strip().lower()
        if target in {"for-you", "for you", "foryou"}:
            target = "for_you"
        if target not in {"for_you", "following"}:
            return False
        selectors = ui.HOME_FOLLOWING_TAB_SELECTORS if target == "following" else ui.HOME_FOR_YOU_TAB_SELECTORS
        active = await self._active_home_tab()
        if active == target:
            return True
        tab_node = await self._find_first(selectors, timeout_ms=1600)
        if not tab_node:
            return False
        with contextlib.suppress(Exception):
            await self._click(tab_node)
            await self.human.jitter(260, 760)
            await self._wait_network_idle(1200)
        active = await self._active_home_tab()
        return active in {"", target}

    async def settle_home(self, tab: str = "for_you", force_nav: bool = False) -> bool:
        """Normalize navigation state using `settle_home`."""
        if not self.page:
            return False
        if force_nav or not await self._looks_like_home_timeline():
            if not await self._open_home_via_click():
                await self._goto(f"{self.BASE_URL}/home")
        if not await self._looks_like_home_timeline():
            return False
        return await self._select_home_tab(tab)

    async def _open_notifications_via_click(self) -> bool:
        if not self.page or not await self._page_is_trusted(self.page):
            return False
        if await self._looks_like_notifications_page():
            return True
        for _ in range(2):
            bell = await self._find_first(ui.NOTIFICATIONS_ENTRY_SELECTORS, timeout_ms=1300)
            if not bell:
                continue
            with contextlib.suppress(Exception):
                await self._click(bell)
                await self.human.jitter(220, 620)
                await self._wait_network_idle(1400)
            if await self._looks_like_notifications_page():
                return True
        return False

    async def _open_notifications_mentions_tab(self) -> bool:
        if not self.page or not await self._page_is_trusted(self.page):
            return False
        for _ in range(3):
            tab = await self._find_first(ui.NOTIFICATIONS_MENTIONS_TAB_SELECTORS, timeout_ms=1200)
            if not tab:
                break
            with contextlib.suppress(Exception):
                await self._click(tab)
                await self.human.jitter(220, 620)
                await self._wait_network_idle(1200)
            current_url = (self.page.url or "").lower()
            if "/notifications/mentions" in current_url:
                return True
            with contextlib.suppress(Exception):
                aria_current = (await self._get_attribute(tab, "aria-current") or "").strip().lower()
                if aria_current == "page":
                    return True
        return "/notifications/mentions" in ((self.page.url or "").lower())

    async def _clear_input_like_human(self) -> None:
        try:
            await self._keyboard_press("Control+A")
            await self._keyboard_press("Backspace")
            await self.human.jitter(60, 220)
        except Exception as exc:
            logger.debug("clear_input_like_human_failed error=%s", str(exc)[:260])

    async def _open_search_query(self, query: str) -> bool:
        if not self.page:
            return False
        self._clear_action_error()
        query_text = (query or "").strip()
        if not query_text:
            return False

        # Prefer UI navigation first.
        if not await self._looks_like_home_timeline():
            if not await self._open_home_via_click():
                with contextlib.suppress(Exception):
                    await self._goto(f"{self.BASE_URL}/home")

        entry = await self._find_first(ui.SEARCH_ENTRY_SELECTORS, timeout_ms=1200)
        if entry:
            with contextlib.suppress(Exception):
                await self._click(entry)
                await self.human.jitter(180, 480)

        search_box = await self._find_first(ui.SEARCH_INPUT_SELECTORS, timeout_ms=2500)
        if not search_box:
            return False
        try:
            await self._click(search_box)
            await self._clear_input_like_human()
            await self._type_text(search_box, query_text)
            await self.human.jitter(120, 360)
            await self._keyboard_press("Enter")
            await self.human.jitter(300, 900)
            await self._wait_network_idle(1400)
            return "/search" in ((self.page.url or "").lower())
        except Exception as exc:
            self._handle_soft_ui_error(
                "open_search_query",
                exc,
                selector=" / ".join(ui.SEARCH_INPUT_SELECTORS),
            )
            return False

    async def _set_search_mode(self, mode: str) -> bool:
        if not self.page or not await self._page_is_trusted(self.page):
            return False
        self._clear_action_error()
        target_mode = (mode or "").strip().lower()
        if target_mode not in {"live", "top"}:
            return False
        selectors = ui.SEARCH_TAB_LATEST_SELECTORS if target_mode == "live" else ui.SEARCH_TAB_TOP_SELECTORS
        tab = await self._find_first(selectors, timeout_ms=1400)
        if not tab:
            return False
        try:
            await self._click(tab)
            await self.human.jitter(220, 620)
            await self._wait_network_idle(1200)
            return True
        except Exception as exc:
            self._handle_soft_ui_error("set_search_mode", exc, selector=" / ".join(selectors))
            return False

    async def _open_profile_page(self, username: str) -> bool:
        if not self.page:
            return False
        handle = self._normalize_account_handle(username)
        if not handle:
            return False
        try:
            await self._goto(f"{self.BASE_URL}/{handle}")
        except Exception as exc:
            logger.warning("open_profile_failed handle=%s error=%s", handle, str(exc)[:260])
            return False
        current_handle = self._profile_handle_from_url(self.page.url or "")
        return current_handle.lower() == handle.lower()

    def _extract_profile_handle_from_href(self, href: str) -> str:
        raw = str(href or "").strip()
        if not raw:
            return ""
        if raw.startswith("http"):
            trusted = self._normalize_trusted_x_url(raw)
            if not trusted:
                return ""
            match = self.PROFILE_URL_RE.search(trusted)
            if match:
                return self._normalize_username(match.group(1))
        path = raw.split("?", 1)[0].split("#", 1)[0].strip("/")
        if not path or "/" in path:
            return ""
        if path.lower() in ui.RESERVED_PROFILE_PATHS:
            return ""
        return self._normalize_username(path)

    def _extract_handle_from_text(self, text: str) -> str:
        match = self.HANDLE_TEXT_RE.search(str(text or ""))
        return self._normalize_username(match.group(1) if match else "")

    async def _get_authenticated_handle(self, force_refresh: bool = False) -> str | None:
        if self._authenticated_handle and not force_refresh:
            return self._authenticated_handle
        if not self.page:
            return None
        for attempt in range(2):
            # Only authenticated side-navigation controls are identity evidence.
            # Generic profile aria-label links can belong to any visible author.
            for selector in (
                'a[data-testid="AppTabBar_Profile_Link"]',
                '[data-testid="SideNav_AccountSwitcher_Button"]',
            ):
                locator = self.page.locator(selector).first
                if not await self._count_locator(locator):
                    continue
                href = await self._get_attribute(locator, "href")
                handle = self._extract_profile_handle_from_href(href or "")
                if not handle:
                    text = ""
                    with contextlib.suppress(Exception):
                        text = await self._inner_text(locator)
                    handle = self._extract_handle_from_text(text)
                if handle:
                    self._authenticated_handle = handle
                    return handle

            account_switcher = self.page.locator('[data-testid="SideNav_AccountSwitcher_Button"]').first
            if await self._count_locator(account_switcher):
                with contextlib.suppress(Exception):
                    text = await self._inner_text(account_switcher)
                    handle = self._extract_handle_from_text(text)
                    if handle:
                        self._authenticated_handle = handle
                        return handle

            if attempt == 0 and not await self._looks_like_home_timeline():
                with contextlib.suppress(Exception):
                    if not await self._open_home_via_click():
                        await self._goto(f"{self.BASE_URL}/home")
        return None

    async def _extract_article_author_handle(self, article: Any) -> str:
        if not article:
            return ""
        auth_locator = article.locator('div[data-testid="User-Name"] a[href^="/"]').first
        if await self._count_locator(auth_locator):
            href = (await self._get_attribute(auth_locator, "href")) or ""
            handle = self._extract_profile_handle_from_href(href)
            if handle:
                return handle
        with contextlib.suppress(Exception):
            name_block = article.locator('div[data-testid="User-Name"]').first
            if await self._count_locator(name_block):
                return self._extract_handle_from_text(await self._inner_text(name_block))
        return ""

    async def _extract_article_author_display_name(self, article: Any) -> str:
        if not article:
            return ""
        with contextlib.suppress(Exception):
            name_block = article.locator('div[data-testid="User-Name"]').first
            if await self._count_locator(name_block):
                for line in (await self._inner_text(name_block, timeout_ms=1200)).splitlines():
                    clean = line.strip()
                    if clean and not clean.startswith("@"):
                        return clean
        return ""

    async def _extract_article_media(self, article: Any) -> list[dict[str, Any]]:
        if not article:
            return []
        media: list[dict[str, Any]] = []
        with contextlib.suppress(Exception):
            images = article.locator("img")
            for idx in range(min(await self._count_locator(images), 12)):
                image = images.nth(idx)
                src = (await self._get_attribute(image, "src")) or ""
                alt = (await self._get_attribute(image, "alt")) or ""
                if not src or "profile_images" in src:
                    continue
                media.append(ObservedMediaData(kind="image", url=src, thumbnail_url=src, alt_text=alt).to_dict())
        with contextlib.suppress(Exception):
            videos = article.locator("video")
            for idx in range(min(await self._count_locator(videos), 6)):
                video = videos.nth(idx)
                src = (await self._get_attribute(video, "src")) or ""
                poster = (await self._get_attribute(video, "poster")) or ""
                media.append(ObservedMediaData(kind="video", url=src, thumbnail_url=poster).to_dict())
        return media

    async def _screenshot_locator(self, locator: Any, path: Path, *, overwrite: bool = False) -> bool:
        if path.is_symlink():
            raise ValueError(f"media_capture_symlink_target:{path.name}")
        if path.exists() and not overwrite:
            raise FileExistsError(f"media_capture_target_exists:{path.name}")
        try:
            if self._sync_mode:
                await self._run_sync(locator.screenshot, path=str(path))
            else:
                await locator.screenshot(path=str(path))
            return True
        except Exception as exc:
            logger.warning("media_capture_screenshot_failed path=%s error=%s", path, str(exc)[:220])
            return False

    async def _media_node_belongs_to_post(self, locator: Any, target_post_id: str) -> bool:
        script = """node => {
            const link = node.closest('a[href*="/status/"]');
            if (link) return link.getAttribute('href') || '';
            const article = node.closest('article');
            if (!article) return '';
            let parent = node.parentElement;
            while (parent && parent !== article) {
                if (parent.matches('[data-testid="quoteTweet"], [data-testid="card.wrapper"]')) return '';
                const candidate = parent.querySelector(':scope > a[href*="/status/"]');
                if (candidate) return candidate.getAttribute('href') || '';
                parent = parent.parentElement;
            }
            const timestamp = article.querySelector('time');
            const timestampLink = timestamp && timestamp.closest('a[href*="/status/"]');
            return timestampLink ? (timestampLink.getAttribute('href') || '') : '';
        }"""
        try:
            if self._sync_mode:
                href = await self._run_sync(locator.evaluate, script)
            else:
                href = await locator.evaluate(script)
        except Exception:
            return False
        return self._normalize_post_id(self._normalize_trusted_x_url(href)) == target_post_id

    async def _play_video_locator(self, locator: Any) -> None:
        script = """node => {
            try {
                node.muted = true;
                const result = node.play && node.play();
                if (result && result.catch) result.catch(() => {});
            } catch (_err) {}
        }"""
        with contextlib.suppress(Exception):
            if self._sync_mode:
                await self._run_sync(locator.evaluate, script)
            else:
                await locator.evaluate(script)

    async def _capture_article_media_nodes(
        self,
        article: Any,
        target_post_id: str,
        output_dir: Path,
        frame_count: int = 3,
        *,
        overwrite: bool = False,
    ) -> list[MediaCaptureData]:
        if not article:
            return []
        captures: list[MediaCaptureData] = []
        seen_sources: set[str] = set()

        try:
            images = article.locator('[data-testid="tweetPhoto"] img')
            for idx in range(min(await self._count_locator(images), 24)):
                image = images.nth(idx)
                src = (await self._get_attribute(image, "src")) or ""
                alt = (await self._get_attribute(image, "alt")) or ""
                if not src or "profile_images" in src or src in seen_sources:
                    continue
                if not await self._media_node_belongs_to_post(image, target_post_id):
                    continue
                seen_sources.add(src)
                path = output_dir / f"{target_post_id}_image_{idx + 1}.png"
                ok = await self._screenshot_locator(image, path, overwrite=overwrite)
                captures.append(
                    MediaCaptureData(
                        kind="image",
                        path=str(path) if ok else "",
                        target_post_id=target_post_id,
                        source_url=src,
                        thumbnail_url=src,
                        alt_text=alt,
                        raw={"selector": "img", "index": idx, "screenshot_ok": ok},
                    )
                )
        except (FileExistsError, ValueError):
            raise
        except Exception:
            pass

        max_frames = max(1, min(int(frame_count), 10))
        try:
            videos = article.locator("video")
            for video_idx in range(min(await self._count_locator(videos), 6)):
                video = videos.nth(video_idx)
                src = (await self._get_attribute(video, "src")) or ""
                poster = (await self._get_attribute(video, "poster")) or ""
                if not await self._media_node_belongs_to_post(video, target_post_id):
                    continue
                await self._play_video_locator(video)
                for frame_idx in range(max_frames):
                    path = output_dir / f"{target_post_id}_video_{video_idx + 1}_frame_{frame_idx + 1}.png"
                    ok = await self._screenshot_locator(video, path, overwrite=overwrite)
                    captures.append(
                        MediaCaptureData(
                            kind="video",
                            path=str(path) if ok else "",
                            target_post_id=target_post_id,
                            source_url=src,
                            thumbnail_url=poster,
                            raw={
                                "selector": "video",
                                "index": video_idx,
                                "frame_index": frame_idx,
                                "screenshot_ok": ok,
                            },
                        )
                    )
                    if frame_idx < max_frames - 1:
                        await asyncio.sleep(0.35)
        except (FileExistsError, ValueError):
            raise
        except Exception:
            pass
        return captures

    async def _extract_article_status_url(self, article: Any) -> str:
        urls = await self._extract_article_status_urls(article)
        if not urls:
            return ""
        primary_id = await self._article_primary_post_id(article)
        if primary_id:
            for url in urls:
                if self._normalize_post_id(url) == primary_id:
                    return url
        return urls[0]

    async def _extract_article_status_urls(self, article: Any) -> list[str]:
        if not article:
            return []
        links = article.locator('a[href*="/status/"]')
        total = await self._count_locator(links)
        if not total:
            return []
        urls: list[str] = []
        for idx in range(min(total, 20)):
            href = (await self._get_attribute(links.nth(idx), "href")) or ""
            url = self._normalize_trusted_x_url(href)
            if url and url not in urls:
                urls.append(url)
        return urls

    async def _article_primary_post_id(self, article: Any) -> str:
        """Return the outer article's canonical post ID, never a nested quote ID."""
        if not article:
            return ""
        with contextlib.suppress(Exception):
            times = article.locator("time")
            if await self._count_locator(times):
                timestamp_link = times.first.locator("xpath=ancestor::a[contains(@href, '/status/')][1]")
                if await self._count_locator(timestamp_link):
                    href = (await self._get_attribute(timestamp_link.first, "href")) or ""
                    post_id = self._normalize_post_id(
                        self._normalize_trusted_x_url(href)
                    )
                    if post_id:
                        return post_id
        urls = await self._extract_article_status_urls(article)
        return self._normalize_post_id(urls[0]) if urls else ""

    async def _find_exact_article(self, post_id: str, *, max_articles: int = 80) -> Any | None:
        if not self.page or not await self._page_is_trusted(self.page):
            return None
        normalized = self._normalize_post_id(post_id)
        if not normalized:
            return None
        articles = self.page.locator("article")
        total = await self._count_locator(articles)
        for idx in range(min(total, max(1, int(max_articles)))):
            candidate = articles.nth(idx)
            if not await self._is_visible(candidate):
                continue
            if await self._article_primary_post_id(candidate) == normalized:
                stable, _ = await self._bind_locator(candidate, f"article-{normalized}")
                if stable is None:
                    continue
                # Binding can race a virtualized timeline update. Revalidate the
                # identity-keyed locator and fail if the selected node detached or
                # changed before returning it to a downstream click workflow.
                if not await self._is_visible(stable):
                    return None
                if await self._article_primary_post_id(stable) != normalized:
                    return None
                return stable
        return None

    async def _find_exact_status_link(self, scope: Any, post_id: str) -> Any | None:
        normalized = self._normalize_post_id(post_id)
        if not scope or not normalized:
            return None
        links = scope.locator('a[href*="/status/"]')
        total = await self._count_locator(links)
        for idx in range(min(total, 100)):
            candidate = links.nth(idx)
            href = (await self._get_attribute(candidate, "href")) or ""
            if self._normalize_post_id(self._normalize_trusted_x_url(href)) == normalized:
                return candidate
        return None

    def _extract_status_ids_from_urls(self, urls: Sequence[str]) -> list[str]:
        post_ids: list[str] = []
        for url in urls:
            post_id = self._normalize_post_id(str(url or ""))
            if post_id and post_id not in post_ids:
                post_ids.append(post_id)
        return post_ids

    async def _extract_article_social_context(self, article: Any) -> str:
        if not article:
            return ""
        context_node = article.locator('[data-testid="socialContext"]').first
        if not await self._count_locator(context_node):
            return ""
        with contextlib.suppress(Exception):
            return (await self._inner_text(context_node, timeout_ms=1200)).strip()
        return ""

    def _normalize_article_notice(self, value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    def _extract_matching_notice(self, text: str, patterns: Sequence[re.Pattern[str]]) -> str:
        compact = self._normalize_article_notice(text)
        if not compact:
            return ""
        for pattern in patterns:
            match = pattern.search(compact)
            if match:
                return match.group(0).strip()
        return ""

    def _extract_article_author_limit_state(self, body: str, social_context: str = "") -> dict[str, Any]:
        raw = "\n".join(part for part in (body, social_context) if part)
        reply_notice = self._extract_matching_notice(raw, ui.REPLY_LIMIT_NOTICE_PATTERNS)
        reply_blocked_notice = self._extract_matching_notice(raw, ui.REPLY_LIMIT_BLOCKED_PATTERNS)
        author_limited = bool(reply_notice or reply_blocked_notice)
        notice = reply_notice or reply_blocked_notice
        limit_type = "reply" if author_limited else ""
        return {
            "author_limited": author_limited,
            "author_limit_type": limit_type,
            "author_limit_notice": notice,
            "reply_limited": author_limited,
            "reply_limit_notice": notice,
            "reply_limit_blocked": bool(reply_blocked_notice),
        }

    async def _article_has_reply_context(self, article: Any, body: str = "") -> bool:
        if not article:
            return False
        if "replying to" in str(body or "").lower():
            return True
        reply_markers = [
            'span:has-text("Replying to")',
            'div:has-text("Replying to")',
            'a:has-text("Replying to")',
        ]
        for selector in reply_markers:
            with contextlib.suppress(Exception):
                marker = article.locator(selector).first
                if await self._count_locator(marker):
                    return True
        return False

    async def _article_has_quote_context(self, article: Any, body: str = "") -> bool:
        if not article:
            return False
        if await self._article_has_reply_context(article, body=body):
            return False
        urls = await self._extract_article_status_urls(article)
        return len(self._extract_status_ids_from_urls(urls)) >= 2

    async def _classify_profile_article(self, article: Any, own_handle: str) -> dict[str, Any]:
        urls = await self._extract_article_status_urls(article)
        post_id = await self._article_primary_post_id(article)
        url = next((candidate for candidate in urls if self._normalize_post_id(candidate) == post_id), "")
        status_ids = self._extract_status_ids_from_urls(urls)
        author = await self._extract_article_author_handle(article)
        text = (await self._extract_article_text(article))[:4000]
        body = ""
        with contextlib.suppress(Exception):
            body = (await self._inner_text(article, timeout_ms=1200)).strip()
        created_at = await self._extract_article_timestamp(article)
        social_context = await self._extract_article_social_context(article)
        own_handle_lower = own_handle.lower()
        author_lower = author.lower()
        social_lower = social_context.lower()
        body_lower = body.lower()
        is_repost = bool("reposted" in social_lower and author_lower != own_handle_lower)
        reply_context = await self._article_has_reply_context(article, body=body)
        quote_context = await self._article_has_quote_context(article, body=body)
        is_reply = bool(author_lower == own_handle_lower and ("replying to" in body_lower or reply_context))
        is_quote = bool(author_lower == own_handle_lower and not is_repost and not is_reply and quote_context)
        limit_state = self._extract_article_author_limit_state(body, social_context)
        return {
            "post_id": post_id,
            "url": url or (f"{self.BASE_URL}/i/web/status/{post_id}" if post_id else ""),
            "author": author,
            "text": text,
            "created_at": created_at.isoformat() if created_at else None,
            "status_ids": status_ids,
            "social_context": social_context,
            "is_reply": is_reply,
            "is_repost": is_repost,
            "is_quote": is_quote,
            **limit_state,
        }

    def _profile_item_matches_kind(self, item: dict[str, Any], kind: str, own_handle: str) -> bool:
        author = str(item.get("author") or "").lower()
        own_handle_lower = own_handle.lower()
        if kind == "repost":
            return bool(item.get("is_repost"))
        if author != own_handle_lower:
            return False
        if kind == "reply":
            return bool(item.get("is_reply"))
        if kind == "quote":
            return bool(item.get("is_quote"))
        if kind == "post":
            return (
                not bool(item.get("is_reply"))
                and not bool(item.get("is_repost"))
                and not bool(item.get("is_quote"))
            )
        return False

    def _profile_surface_url(self, own_handle: str, kind: str) -> str:
        handle = self._normalize_account_handle(own_handle)
        if not handle:
            return ""
        if kind == "reply":
            return f"{self.BASE_URL}/{handle}/with_replies"
        return f"{self.BASE_URL}/{handle}"

    async def _open_profile_surface(self, own_handle: str, kind: str) -> bool:
        if not self.page:
            return False
        target_url = self._profile_surface_url(own_handle, kind)
        if not target_url:
            return False
        current = (self.page.url or "").rstrip("/")
        if current == target_url.rstrip("/"):
            return True
        try:
            await self._goto(target_url)
            await self.human.jitter(280, 760)
            return self._profile_handle_from_url(self.page.url or "").lower() == own_handle.lower()
        except Exception as exc:
            logger.warning(
                "open_profile_surface_failed kind=%s handle=%s error=%s",
                kind,
                own_handle,
                str(exc)[:260],
            )
            return False

    async def _find_profile_article_by_post_id(
        self,
        own_handle: str,
        post_id: str,
        kind: str,
        scan_rounds: int = 7,
    ) -> Any | None:
        if not self.page:
            return None
        if not await self._open_profile_surface(own_handle, kind):
            return None
        rounds = max(2, int(scan_rounds))
        for round_idx in range(rounds):
            article = await self._find_exact_article(post_id)
            if article is not None:
                return article
            if round_idx < rounds - 1:
                await self._random_scroll(random.randint(380, 1160))
                await self.human.jitter(220, 620)
        return None

    async def _find_owned_status_article_by_post_id(
        self,
        own_handle: str,
        post_id: str,
        kind: str,
    ) -> tuple[Any, dict[str, Any]] | None:
        if not self.page:
            return None
        post_id = self._normalize_post_id(post_id)
        if not post_id:
            return None
        try:
            await self._goto(f"{self.BASE_URL}/i/web/status/{post_id}")
        except Exception as exc:
            logger.warning("open_direct_delete_status_failed target=%s error=%s", post_id, str(exc)[:260])
            return None
        article = await self._find_post_article_in_context(post_id, scan_rounds=2)
        if article is None:
            return None
        item = await self._classify_profile_article(article, own_handle)
        if str(item.get("post_id") or "") != post_id:
            return None
        if not self._profile_item_matches_kind(item, kind, own_handle):
            logger.warning(
                "direct_delete_status_kind_mismatch target=%s kind=%s author=%s is_reply=%s is_repost=%s is_quote=%s",
                post_id,
                kind,
                item.get("author") or "",
                bool(item.get("is_reply")),
                bool(item.get("is_repost")),
                bool(item.get("is_quote")),
            )
            return None
        return article, item

    async def _wait_for_profile_article_disappearance(
        self,
        own_handle: str,
        post_id: str,
        kind: str,
        retries: int = 14,
    ) -> bool:
        if not self.page:
            return True
        for _ in range(max(4, retries)):
            article = await self._find_exact_article(post_id)
            if article is None:
                return True
            await asyncio.sleep(0.25)
        return False

    async def _collect_visible_profile_candidates(
        self,
        own_handle: str,
        kind: str,
        limit: int = 8,
        excluded_post_ids: set[str] | None = None,
    ) -> list[tuple[Any, dict[str, Any]]]:
        if not self.page:
            return []
        excluded = excluded_post_ids or set()
        articles = self.page.locator("article")
        total = await self._count_locator(articles)
        rows: list[tuple[Any, dict[str, Any]]] = []
        scan_cap = max(limit * 8, 60 if kind == "reply" else 32)
        for idx in range(min(total, scan_cap)):
            article = articles.nth(idx)
            item = await self._classify_profile_article(article, own_handle)
            post_id = str(item.get("post_id") or "")
            if not post_id or post_id in excluded:
                continue
            if not self._profile_item_matches_kind(item, kind, own_handle):
                continue
            rows.append((article, item))
            if len(rows) >= limit:
                return rows
        return rows

    async def _collect_profile_items(self, own_handle: str, kind: str, limit: int = 20) -> list[dict[str, Any]]:
        if not self.page:
            return []
        if not await self._open_profile_surface(own_handle, kind):
            return []

        rows: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        stagnation_rounds = 0
        scroll_rounds = max(4, (max(1, int(limit)) // 4) + 6)
        for round_idx in range(scroll_rounds):
            articles = self.page.locator("article")
            total = await self._count_locator(articles)
            new_items = 0
            for idx in range(min(total, max(limit * 3, 70))):
                article = articles.nth(idx)
                item = await self._classify_profile_article(article, own_handle)
                post_id = str(item.get("post_id") or "")
                if not post_id or post_id in seen_ids:
                    continue
                seen_ids.add(post_id)
                if not self._profile_item_matches_kind(item, kind, own_handle):
                    continue
                rows.append(item)
                new_items += 1
                if len(rows) >= limit:
                    return rows
            if round_idx < scroll_rounds - 1:
                if new_items == 0:
                    stagnation_rounds += 1
                else:
                    stagnation_rounds = 0
                if stagnation_rounds >= 3:
                    break
                await self._random_scroll(random.randint(380, 1180))
                await self.human.jitter(260, 680)
        return rows

    async def _collect_recent_owned_created_candidates(
        self,
        own_handle: str,
        kind: str,
        limit: int = 36,
    ) -> list[dict[str, Any]]:
        if not self.page:
            return []
        own_handle_lower = own_handle.lower()
        if kind == "reply":
            surfaces = ("reply", "post")
        elif kind == "quote":
            surfaces = ("post", "reply")
        else:
            surfaces = ("post", "reply")

        rows: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        per_surface_limit = max(12, min(max(1, int(limit)), 80))
        for surface_kind in surfaces:
            if len(rows) >= limit:
                break
            if not await self._open_profile_surface(own_handle, surface_kind):
                continue
            stagnation_rounds = 0
            scroll_rounds = max(3, (per_surface_limit // 5) + 4)
            for round_idx in range(scroll_rounds):
                articles = self.page.locator("article")
                total = await self._count_locator(articles)
                new_items = 0
                for idx in range(min(total, max(per_surface_limit * 3, 70))):
                    article = articles.nth(idx)
                    item = await self._classify_profile_article(article, own_handle)
                    post_id = str(item.get("post_id") or "")
                    author = str(item.get("author") or "").lower()
                    if not post_id or post_id in seen_ids or author != own_handle_lower:
                        continue
                    seen_ids.add(post_id)
                    if bool(item.get("is_repost")):
                        continue
                    item["source_surface"] = "with_replies" if surface_kind == "reply" else "posts"
                    rows.append(item)
                    new_items += 1
                    if len(rows) >= limit:
                        return rows
                if round_idx < scroll_rounds - 1:
                    if new_items == 0:
                        stagnation_rounds += 1
                    else:
                        stagnation_rounds = 0
                    if stagnation_rounds >= 2:
                        break
                    await self._random_scroll(random.randint(380, 1180))
                    await self.human.jitter(260, 680)
        return rows

    async def _resolve_target_article(self, post_id: str) -> Any | None:
        return await self._find_post_article_in_context(post_id, scan_rounds=1)

    async def _wait_for_post_disappearance(self, post_id: str, retries: int = 14) -> bool:
        for _ in range(max(3, retries)):
            if not self.page:
                return True
            current_url = self.page.url or ""
            article = await self._find_post_article_in_context(post_id, scan_rounds=1)
            if self._normalize_post_id(current_url) != post_id and article is None:
                return True
            if article is None:
                return True
            await asyncio.sleep(0.25)
        return False

    async def current_state(self) -> dict[str, str]:
        """Return structured controller state from `current_state`."""
        if not self.page:
            return {"state": "not_started", "url": ""}
        url = await self._page_url(self.page)
        if await self._page_is_closed(self.page):
            return {"state": "closed", "url": url}
        state: dict[str, str]
        if not self._normalize_trusted_x_url(url):
            state = {"state": "unknown", "url": url}
        elif self._looks_like_login_flow_url(url) and await self._any_selector(ui.LOGIN_SELECTORS):
            state = {"state": "login", "url": url}
        elif "/compose/" in url:
            state = {"state": "compose", "url": url}
        elif self.STATUS_URL_RE.search(url):
            state = {"state": "status", "url": url}
        elif "/notifications" in url:
            state = {"state": "notifications", "url": url}
        elif "/search" in url:
            state = {"state": "search", "url": url}
        elif await self._looks_like_home_timeline():
            state = {"state": "home", "url": url}
        elif self.PROFILE_URL_RE.search(url):
            state = {"state": "profile", "url": url}
        else:
            state = {"state": "unknown", "url": url}
        if self.last_action_error:
            state["last_action_error"] = self.last_action_error.summary
        return state

    def _looks_like_login_flow_url(self, url: str) -> bool:
        if not self._normalize_trusted_x_url(url):
            return False
        lowered = str(url or "").lower()
        return any(token in lowered for token in ("/login", "/i/flow/", "/i/jf/onboarding")) or "mode=login" in lowered

    async def login_state(self) -> LoginState:
        """Return structured controller state from `login_state`."""
        if not self.page:
            return LoginState(
                logged_in=False,
                page_state="not_started",
                url="",
                browser_started=False,
                login_required=True,
                raw={"reason": "page_not_started"},
            )

        state = await self.current_state()
        page_state = str(state.get("state") or "unknown")
        url = str(state.get("url") or self.page.url or "")
        trusted_origin = bool(self._is_trusted_x_url(url))
        if page_state == "closed" or not trusted_origin:
            return LoginState(
                logged_in=False,
                page_state=page_state,
                url=url,
                browser_started=page_state != "closed",
                active_home_tab="",
                login_required=True,
                raw={"reason": "closed_page" if page_state == "closed" else "untrusted_origin"},
            )
        login_markers_visible = await self._any_selector(ui.LOGIN_SELECTORS)
        logged_in_selectors_visible = await self._any_selector(ui.LOGGED_IN_SELECTORS)
        login_link_visible = False
        with contextlib.suppress(Exception):
            login_link_visible = bool(await self._count_locator(self.page.locator('a[href="/login"]')))

        login_url = self._looks_like_login_flow_url(url)
        known_logged_in_surface = page_state in {"home", "search", "status", "profile", "notifications", "compose"}
        logged_in = bool(trusted_origin and not login_url and not login_markers_visible and logged_in_selectors_visible)
        raw: dict[str, Any] = {
            "login_markers_visible": login_markers_visible,
            "logged_in_selectors_visible": logged_in_selectors_visible,
            "login_link_visible": login_link_visible,
            "trusted_origin": trusted_origin,
            "known_surface": known_logged_in_surface,
        }
        if self.last_action_error:
            raw["last_action_error"] = self.last_action_error.to_dict()
        return LoginState(
            logged_in=logged_in,
            page_state=page_state,
            url=url,
            browser_started=True,
            active_home_tab=await self._active_home_tab(),
            login_required=not logged_in,
            raw=raw,
        )

    async def is_logged_in(self) -> bool:
        """Provide login-flow behavior through `is_logged_in`."""
        if not self.page:
            return False
        if not await self._page_is_trusted(self.page):
            await self._goto(f"{self.BASE_URL}/home")
        elif not await self._open_home_via_click():
            await self._goto(f"{self.BASE_URL}/home")
        current_url = self.page.url or ""
        if not self._normalize_trusted_x_url(current_url):
            return False
        login_markers_visible = await self._any_selector(ui.LOGIN_SELECTORS)
        if self._looks_like_login_flow_url(current_url):
            return False
        if login_markers_visible:
            return False
        if await self._any_selector(ui.LOGGED_IN_SELECTORS):
            return True
        return False

    async def open_login_page(self) -> None:
        """Provide login-flow behavior through `open_login_page`."""
        await self._goto(f"{self.BASE_URL}/i/flow/login")
        await self._wait_network_idle(1400)

    async def _collect_posts_from_current_page(
        self,
        limit: int,
        scroll_rounds: int,
        max_scan: int,
        stagnation_limit: int,
        allow_backtrack: bool = False,
    ) -> list[ObservedPostData]:
        if not self.page or not await self._page_is_trusted(self.page):
            return []
        posts: list[ObservedPostData] = []
        seen_ids: set[str] = set()
        articles = self.page.locator("article")
        stagnant_rounds = 0
        for round_idx in range(scroll_rounds):
            total = await self._count_locator(articles)
            new_posts_in_round = 0
            for idx in range(min(total, max_scan)):
                item = articles.nth(idx)
                href = await self._extract_article_status_url(item)
                post_id = await self._article_primary_post_id(item)
                if not post_id or post_id in seen_ids:
                    continue
                seen_ids.add(post_id)
                text = (await self._extract_article_text(item))[:4000]
                body = text
                with contextlib.suppress(Exception):
                    body = (await self._inner_text(item, timeout_ms=1200)).strip()[:4000]
                author = await self._extract_article_author_handle(item)
                author_display_name = await self._extract_article_author_display_name(item)
                created_at = await self._extract_article_timestamp(item)
                social_context = await self._extract_article_social_context(item)
                metrics = await self._extract_article_metrics(item)
                media = await self._extract_article_media(item)
                limit_state = self._extract_article_author_limit_state(body, social_context)
                posts.append(
                    ObservedPostData(
                        post_id,
                        author,
                        text,
                        {
                            "post_id": post_id,
                            "url": href,
                            "author_handle": author,
                            "author_display_name": author_display_name,
                            "created_at": created_at.isoformat() if created_at else None,
                            "text": text,
                            "body": body,
                            "social_context": social_context,
                            "media": media,
                            **limit_state,
                            **metrics,
                            "metrics": metrics,
                        },
                    )
                )
                new_posts_in_round += 1
                if len(posts) >= limit:
                    return posts
            if round_idx < scroll_rounds - 1:
                if new_posts_in_round == 0:
                    stagnant_rounds += 1
                else:
                    stagnant_rounds = 0
                if stagnant_rounds >= stagnation_limit:
                    break
                if allow_backtrack and round_idx % 4 == 3:
                    await self._random_scroll(random.randint(-180, -60), random.randint(420, 1200))
                else:
                    await self._random_scroll(random.randint(420, 1200))
                await self.human.jitter(300, 780)
        return posts
