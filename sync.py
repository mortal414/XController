from __future__ import annotations

import asyncio
import math
import threading
import time
from collections.abc import Awaitable
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, TypeVar

from .adapter import ImagePathInput, XTextAdapter
from .base import (
    AccountStats,
    ActionPreflight,
    ActionResult,
    ControllerHealth,
    LoginState,
    MediaCaptureData,
    ObservedNotificationData,
    ObservedPostData,
    TimelineReadResult,
)

T = TypeVar("T")

DEFAULT_CALL_TIMEOUT_SECONDS = 120.0
DEFAULT_CLOSE_TIMEOUT_SECONDS = 10.0
LOOP_STOP_GRACE_SECONDS = 0.25


class SyncXController:
    """Synchronous facade for XController's async browser service APIs."""

    def __init__(
        self,
        profile_path: str | None = None,
        settings: Any | None = None,
        proxy: str | None = None,
        *,
        adapter: XTextAdapter | None = None,
        call_timeout_seconds: float = DEFAULT_CALL_TIMEOUT_SECONDS,
        close_timeout_seconds: float = DEFAULT_CLOSE_TIMEOUT_SECONDS,
    ) -> None:
        if adapter is None and profile_path is None:
            raise ValueError("profile_path is required when adapter is not provided")
        self._call_timeout_seconds = self._normalize_timeout(call_timeout_seconds, "call_timeout_seconds")
        self._close_timeout_seconds = self._normalize_timeout(close_timeout_seconds, "close_timeout_seconds")
        self._adapter = adapter or XTextAdapter(profile_path=str(profile_path), settings=settings, proxy=proxy)
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._close_complete = threading.Event()
        self._state_lock = threading.Lock()
        self._call_lock = threading.RLock()
        self._closed = False
        self._closing = False
        self._poisoned = False
        self._close_error: BaseException | None = None
        self._active_future: Future[Any] | None = None
        self._loop_error: BaseException | None = None
        self._thread = threading.Thread(target=self._run_loop, name="xcontroller-sync-loop", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)
        if not self._loop.is_running():
            raise RuntimeError("sync_controller_loop_not_started")

    @staticmethod
    def _normalize_timeout(value: float, name: str) -> float:
        if isinstance(value, bool):
            raise ValueError(f"{name} must be a finite number greater than zero")
        try:
            normalized = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be a finite number greater than zero") from exc
        if not math.isfinite(normalized) or normalized <= 0 or normalized > threading.TIMEOUT_MAX:
            raise ValueError(f"{name} must be a finite number greater than zero")
        return normalized

    @staticmethod
    def _close_awaitable(awaitable: Awaitable[Any]) -> None:
        close = getattr(awaitable, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    @property
    def adapter(self) -> XTextAdapter:
        return self._adapter

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._ready.set)
        try:
            self._loop.run_forever()
        except BaseException as exc:
            self._loop_error = exc
        finally:
            try:
                pending = [task for task in asyncio.all_tasks(self._loop) if not task.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            except BaseException as exc:
                if self._loop_error is None:
                    self._loop_error = exc
            finally:
                if not self._loop.is_closed():
                    self._loop.close()

    def _call(
        self,
        awaitable: Awaitable[T],
        *,
        allow_closing: bool = False,
        timeout_seconds: float | None = None,
    ) -> T:
        timeout = self._call_timeout_seconds if timeout_seconds is None else self._normalize_timeout(
            timeout_seconds,
            "timeout_seconds",
        )
        if threading.current_thread() is self._thread:
            self._close_awaitable(awaitable)
            raise RuntimeError("sync_controller_call_from_event_loop_thread")
        if not allow_closing:
            with self._state_lock:
                if self._closed:
                    self._close_awaitable(awaitable)
                    raise RuntimeError("sync_controller_closed")
                if self._closing:
                    self._close_awaitable(awaitable)
                    raise RuntimeError("sync_controller_closing")
                if self._poisoned:
                    self._close_awaitable(awaitable)
                    raise RuntimeError("sync_controller_timed_out")

        deadline = time.monotonic() + timeout
        future: Future[T] | None = None
        if not self._call_lock.acquire(timeout=timeout):
            self._close_awaitable(awaitable)
            raise TimeoutError(f"sync_controller_call_timed_out:{timeout:g}s")
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._close_awaitable(awaitable)
                raise TimeoutError(f"sync_controller_call_timed_out:{timeout:g}s")
            with self._state_lock:
                if self._closed:
                    self._close_awaitable(awaitable)
                    raise RuntimeError("sync_controller_closed")
                if self._closing and not allow_closing:
                    self._close_awaitable(awaitable)
                    raise RuntimeError("sync_controller_closing")
                if self._poisoned and not allow_closing:
                    self._close_awaitable(awaitable)
                    raise RuntimeError("sync_controller_timed_out")
                if not self._loop.is_running():
                    self._close_awaitable(awaitable)
                    raise RuntimeError("sync_controller_loop_not_running")
                try:
                    future = asyncio.run_coroutine_threadsafe(awaitable, self._loop)
                except BaseException:
                    self._close_awaitable(awaitable)
                    raise
                self._active_future = future

            try:
                return future.result(timeout=remaining)
            except FutureTimeoutError:
                if future.done():
                    # The future can complete between result() reaching its
                    # timeout and this handler running. Recover the completed
                    # outcome instead of misreporting a timeout (and preserve
                    # any exception raised by the coroutine itself).
                    return future.result()
                # Cancellation cannot stop an in-flight sync Playwright call in
                # the adapter's executor. Poison the facade so no subsequent
                # workflow can interleave with an outcome-ambiguous operation.
                with self._state_lock:
                    self._poisoned = True
                future.cancel()
                raise TimeoutError(f"sync_controller_call_timed_out:{timeout:g}s") from None
            except FutureCancelledError:
                with self._state_lock:
                    closing = self._closing
                reason = "sync_controller_closing" if closing else "sync_controller_call_cancelled"
                raise RuntimeError(reason) from None
            finally:
                with self._state_lock:
                    if self._active_future is future:
                        self._active_future = None
        finally:
            self._call_lock.release()

    def __enter__(self) -> SyncXController:
        self.start()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()

    def start(self) -> None:
        return self._call(self._adapter.start())

    def close(self) -> None:
        if threading.current_thread() is self._thread:
            raise RuntimeError("sync_controller_close_from_event_loop_thread")

        with self._state_lock:
            if self._closed:
                close_error = self._close_error
                if close_error is not None:
                    raise close_error
                return None
            if self._closing:
                wait_for_owner = True
                active_future = None
            else:
                wait_for_owner = False
                self._closing = True
                active_future = self._active_future

        if wait_for_owner:
            if not self._close_complete.wait(timeout=self._close_timeout_seconds):
                raise TimeoutError("sync_controller_close_timed_out")
            with self._state_lock:
                close_error = self._close_error
            if close_error is not None:
                raise close_error
            return None

        if active_future is not None:
            active_future.cancel()

        deadline = time.monotonic() + self._close_timeout_seconds
        close_error: BaseException | None = None
        try:
            remaining = max(0.001, deadline - time.monotonic())
            self._call(
                self._adapter.close(),
                allow_closing=True,
                timeout_seconds=remaining,
            )
        except BaseException as exc:
            close_error = exc
        finally:
            if self._loop.is_running():
                try:
                    self._loop.call_soon_threadsafe(self._loop.stop)
                except RuntimeError as exc:
                    if close_error is None:
                        close_error = exc
            remaining = max(0.0, deadline - time.monotonic())
            self._thread.join(timeout=max(remaining, LOOP_STOP_GRACE_SECONDS))
            if self._thread.is_alive():
                if close_error is None:
                    close_error = TimeoutError("sync_controller_close_timed_out")
            elif not self._loop.is_closed():
                self._loop.close()
            if close_error is None and self._loop_error is not None:
                close_error = self._loop_error
            with self._state_lock:
                self._close_error = close_error
                self._closed = True
                self._closing = False
            self._close_complete.set()
        if close_error:
            raise close_error
        return None

    def is_logged_in(self) -> bool:
        return self._call(self._adapter.is_logged_in())

    def open_login_page(self) -> None:
        return self._call(self._adapter.open_login_page())

    def login_state(self) -> LoginState:
        return self._call(self._adapter.login_state())

    def current_state(self) -> dict[str, str]:
        return self._call(self._adapter.current_state())

    def current_surface(self) -> dict[str, str]:
        return self._call(self._adapter.current_surface())

    def health_check(self) -> ControllerHealth:
        return self._call(self._adapter.health_check())

    def return_home(self, force_refresh: bool = False) -> bool:
        return self._call(self._adapter.return_home(force_refresh=force_refresh))

    def settle_after_action(
        self,
        tab: str = "for_you",
        force_refresh: bool = False,
        reset_scroll: bool = False,
    ) -> bool:
        return self._call(
            self._adapter.settle_after_action(
                tab=tab,
                force_refresh=force_refresh,
                reset_scroll=reset_scroll,
            )
        )

    def read_timeline_detailed(
        self,
        limit: int = 20,
        tab: str = "for_you",
        force_refresh: bool = False,
        reset_scroll: bool = False,
    ) -> TimelineReadResult:
        return self._call(
            self._adapter.read_timeline_detailed(
                limit=limit,
                tab=tab,
                force_refresh=force_refresh,
                reset_scroll=reset_scroll,
            )
        )

    def read_notifications(
        self,
        limit: int = 20,
        unread_only: bool = False,
    ) -> list[ObservedNotificationData]:
        return self._call(self._adapter.read_notifications(limit=limit, unread_only=unread_only))

    def read_post_thread_context(
        self,
        post_id: str,
        limit: int = 6,
        include_parent: bool = True,
        include_target: bool = True,
        include_replies: bool = True,
    ) -> list[ObservedPostData]:
        return self._call(
            self._adapter.read_post_thread_context(
                post_id,
                limit=limit,
                include_parent=include_parent,
                include_target=include_target,
                include_replies=include_replies,
            )
        )

    def search_posts(self, query: str, limit: int = 10) -> list[ObservedPostData]:
        return self._call(self._adapter.search_posts(query, limit=limit))

    def preflight_action(
        self,
        platform_post_id: str,
        action: str = "reply",
        *,
        open_composer: bool = False,
    ) -> ActionPreflight:
        return self._call(
            self._adapter.preflight_action(
                platform_post_id,
                action=action,
                open_composer=open_composer,
            )
        )

    def view_post_detailed(
        self,
        platform_post_id: str,
        dwell_seconds: tuple[int, int] = (3, 8),
    ) -> ActionResult:
        return self._call(self._adapter.view_post_detailed(platform_post_id, dwell_seconds=dwell_seconds))

    def like_post_detailed(self, platform_post_id: str) -> ActionResult:
        return self._call(self._adapter.like_post_detailed(platform_post_id))

    def reply_to_post_detailed(
        self,
        platform_post_id: str,
        text: str,
        image_paths: ImagePathInput | None = None,
    ) -> ActionResult:
        return self._call(self._adapter.reply_to_post_detailed(platform_post_id, text, image_paths=image_paths))

    def quote_post_detailed(
        self,
        platform_post_id: str,
        text: str = "",
        image_paths: ImagePathInput | None = None,
    ) -> ActionResult:
        return self._call(self._adapter.quote_post_detailed(platform_post_id, text=text, image_paths=image_paths))

    def repost_post_detailed(self, platform_post_id: str) -> ActionResult:
        return self._call(self._adapter.repost_post_detailed(platform_post_id))

    def delete_post(self, platform_post_id: str) -> bool:
        return self._call(self._adapter.delete_post(platform_post_id))

    def delete_reply(self, platform_post_id: str) -> bool:
        return self._call(self._adapter.delete_reply(platform_post_id))

    def delete_repost(self, platform_post_id: str) -> bool:
        return self._call(self._adapter.delete_repost(platform_post_id))

    def delete_quote(self, platform_post_id: str) -> bool:
        return self._call(self._adapter.delete_quote(platform_post_id))

    def delete_post_detailed(self, platform_post_id: str, kind: str = "post") -> ActionResult:
        return self._call(self._adapter.delete_post_detailed(platform_post_id, kind=kind))

    def delete_all_posts(self) -> list[str]:
        return self._call(self._adapter.delete_all_posts())

    def delete_all_replies(self) -> list[str]:
        return self._call(self._adapter.delete_all_replies())

    def delete_all_reposts(self) -> list[str]:
        return self._call(self._adapter.delete_all_reposts())

    def delete_all_quotes(self) -> list[str]:
        return self._call(self._adapter.delete_all_quotes())

    def delete_all_content(self) -> dict[str, list[str]]:
        return self._call(self._adapter.delete_all_content())

    def follow_user_detailed(self, username: str) -> ActionResult:
        return self._call(self._adapter.follow_user_detailed(username))

    def unfollow_user_detailed(self, username: str) -> ActionResult:
        return self._call(self._adapter.unfollow_user_detailed(username))

    def post_text_detailed(self, text: str, image_paths: ImagePathInput | None = None) -> ActionResult:
        return self._call(self._adapter.post_text_detailed(text, image_paths=image_paths))

    def post_metrics(self, platform_post_id: str) -> dict[str, int]:
        return self._call(self._adapter.post_metrics(platform_post_id))

    def profile_recent_metrics(
        self,
        username: str,
        limit: int = 40,
        source: str | None = None,
    ) -> list[dict[str, int | str | bool]]:
        return self._call(self._adapter.profile_recent_metrics(username, limit=limit, source=source))

    def account_stats(self, handle: str | None = None) -> AccountStats:
        return self._call(self._adapter.account_stats(handle))

    def debug_snapshot(
        self,
        output_dir: str,
        article_limit: int = 12,
        *,
        overwrite: bool = False,
        include_sensitive_content: bool = False,
    ) -> dict[str, Any]:
        return self._call(
            self._adapter.debug_snapshot(
                output_dir,
                article_limit=article_limit,
                overwrite=overwrite,
                include_sensitive_content=include_sensitive_content,
            )
        )

    def capture_post_media(
        self,
        platform_post_id: str,
        output_dir: str,
        frame_count: int = 3,
        *,
        overwrite: bool = False,
    ) -> list[MediaCaptureData]:
        return self._call(
            self._adapter.capture_post_media(
                platform_post_id,
                output_dir,
                frame_count=frame_count,
                overwrite=overwrite,
            )
        )


XControllerService = SyncXController
