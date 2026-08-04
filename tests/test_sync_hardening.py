from __future__ import annotations

import asyncio
import threading
import time
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from unittest.mock import patch

from XController.sync import SyncXController


class _ConcurrentAdapter:
    def __init__(self) -> None:
        self.active_calls = 0
        self.max_active_calls = 0

    async def login_state(self) -> str:
        self.active_calls += 1
        self.max_active_calls = max(self.max_active_calls, self.active_calls)
        try:
            await asyncio.sleep(0.03)
            return "logged_in"
        finally:
            self.active_calls -= 1

    async def close(self) -> None:
        return None


class _UnfollowAdapter:
    def __init__(self) -> None:
        self.username: str | None = None

    async def unfollow_user_detailed(self, username: str) -> dict[str, str]:
        self.username = username
        return {"username": username}

    async def close(self) -> None:
        return None


class _HangingCloseAdapter:
    async def close(self) -> None:
        await asyncio.Event().wait()


class _RecoveringTimeoutAdapter:
    def __init__(self) -> None:
        self.calls = 0

    async def login_state(self) -> str:
        self.calls += 1
        if self.calls == 1:
            await asyncio.Event().wait()
        return "recovered"

    async def close(self) -> None:
        return None


class _ActiveCallAdapter:
    def __init__(self) -> None:
        self.started = threading.Event()

    async def login_state(self) -> str:
        self.started.set()
        await asyncio.Event().wait()
        return "unreachable"

    async def close(self) -> None:
        return None


class _SlowCloseAdapter:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        await asyncio.sleep(0.05)


class _FailingCloseAdapter:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        await asyncio.sleep(0.05)
        raise RuntimeError("adapter close failed")


class _SnapshotAdapter:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] | None = None

    async def debug_snapshot(self, output_dir: str, **kwargs: object) -> dict[str, object]:
        self.kwargs = {"output_dir": output_dir, **kwargs}
        return dict(self.kwargs)

    async def close(self) -> None:
        return None


class _BoundaryCompletedFuture(Future[str]):
    """Simulate completion immediately after result() reports its timeout."""

    def __init__(self, *, value: str = "completed", error: BaseException | None = None) -> None:
        super().__init__()
        self._value = value
        self._error = error
        self.result_calls = 0

    def result(self, timeout: float | None = None) -> str:
        self.result_calls += 1
        if self.result_calls == 1:
            if self._error is None:
                self.set_result(self._value)
            else:
                self.set_exception(self._error)
            raise FutureTimeoutError
        return super().result(timeout)


class SyncXControllerHardeningTests(unittest.TestCase):
    def test_calls_from_multiple_threads_are_serialized(self) -> None:
        adapter = _ConcurrentAdapter()
        controller = SyncXController(adapter=adapter)  # type: ignore[arg-type]
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _index: controller.login_state(), range(2)))
        finally:
            controller.close()

        self.assertEqual(results, ["logged_in", "logged_in"])
        self.assertEqual(adapter.max_active_calls, 1)

    def test_unfollow_detailed_is_forwarded(self) -> None:
        adapter = _UnfollowAdapter()
        controller = SyncXController(adapter=adapter)  # type: ignore[arg-type]
        try:
            result = controller.unfollow_user_detailed("example")
        finally:
            controller.close()

        self.assertEqual(result, {"username": "example"})
        self.assertEqual(adapter.username, "example")

    def test_debug_snapshot_safety_options_are_forwarded(self) -> None:
        adapter = _SnapshotAdapter()
        controller = SyncXController(adapter=adapter)  # type: ignore[arg-type]
        try:
            result = controller.debug_snapshot(
                "snapshot-dir",
                article_limit=3,
                overwrite=True,
                include_sensitive_content=True,
            )
        finally:
            controller.close()

        expected = {
            "output_dir": "snapshot-dir",
            "article_limit": 3,
            "overwrite": True,
            "include_sensitive_content": True,
        }
        self.assertEqual(result, expected)
        self.assertEqual(adapter.kwargs, expected)

    def test_close_timeout_is_bounded_and_stops_loop(self) -> None:
        controller = SyncXController(
            adapter=_HangingCloseAdapter(),  # type: ignore[arg-type]
            close_timeout_seconds=0.05,
        )

        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "sync_controller_.*timed_out"):
            controller.close()
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.0)
        self.assertFalse(controller._thread.is_alive())
        self.assertTrue(controller._loop.is_closed())

    def test_call_after_close_fails_without_leaking_coroutine(self) -> None:
        adapter = _ConcurrentAdapter()
        controller = SyncXController(adapter=adapter)  # type: ignore[arg-type]
        controller.close()

        with self.assertRaisesRegex(RuntimeError, "sync_controller_closed"):
            controller.login_state()

    def test_call_timeout_poisons_controller_until_close(self) -> None:
        adapter = _RecoveringTimeoutAdapter()
        controller = SyncXController(
            adapter=adapter,  # type: ignore[arg-type]
            call_timeout_seconds=0.05,
        )
        try:
            with self.assertRaisesRegex(TimeoutError, "sync_controller_call_timed_out"):
                controller.login_state()
            with self.assertRaisesRegex(RuntimeError, "sync_controller_timed_out"):
                controller.login_state()
            self.assertEqual(adapter.calls, 1)
        finally:
            controller.close()

    def test_timeout_boundary_recovers_completed_future_outcome(self) -> None:
        adapter = _ConcurrentAdapter()
        controller = SyncXController(adapter=adapter)  # type: ignore[arg-type]

        def submit_at_boundary(awaitable: object, _loop: object) -> _BoundaryCompletedFuture:
            awaitable.close()  # type: ignore[attr-defined]
            return boundary_future

        try:
            boundary_future = _BoundaryCompletedFuture(value="completed")
            with patch(
                "XController.sync.asyncio.run_coroutine_threadsafe",
                side_effect=submit_at_boundary,
            ):
                self.assertEqual(controller.login_state(), "completed")
            self.assertEqual(boundary_future.result_calls, 2)
            self.assertFalse(controller._poisoned)

            boundary_future = _BoundaryCompletedFuture(error=ValueError("boundary failure"))
            with (
                patch(
                    "XController.sync.asyncio.run_coroutine_threadsafe",
                    side_effect=submit_at_boundary,
                ),
                self.assertRaisesRegex(ValueError, "boundary failure"),
            ):
                controller.login_state()
            self.assertEqual(boundary_future.result_calls, 2)
            self.assertFalse(controller._poisoned)
        finally:
            controller.close()

    def test_close_cancels_an_active_call_without_deadlocking(self) -> None:
        adapter = _ActiveCallAdapter()
        controller = SyncXController(
            adapter=adapter,  # type: ignore[arg-type]
            close_timeout_seconds=0.5,
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            call = executor.submit(controller.login_state)
            self.assertTrue(adapter.started.wait(timeout=0.5))
            controller.close()
            with self.assertRaisesRegex(RuntimeError, "sync_controller_closing"):
                call.result(timeout=0.5)

        self.assertFalse(controller._thread.is_alive())

    def test_concurrent_close_calls_close_adapter_once(self) -> None:
        adapter = _SlowCloseAdapter()
        controller = SyncXController(
            adapter=adapter,  # type: ignore[arg-type]
            close_timeout_seconds=0.5,
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _index: controller.close(), range(2)))

        self.assertEqual(results, [None, None])
        self.assertEqual(adapter.close_calls, 1)

    def test_close_failure_is_propagated_to_all_waiters_and_later_calls(self) -> None:
        adapter = _FailingCloseAdapter()
        controller = SyncXController(
            adapter=adapter,  # type: ignore[arg-type]
            close_timeout_seconds=0.5,
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(controller.close) for _index in range(2)]
            for future in futures:
                with self.assertRaisesRegex(RuntimeError, "adapter close failed"):
                    future.result(timeout=1)

        self.assertEqual(adapter.close_calls, 1)
        with self.assertRaisesRegex(RuntimeError, "adapter close failed"):
            controller.close()

    def test_invalid_timeouts_are_rejected_before_thread_start(self) -> None:
        invalid_values = (0, -1, float("inf"), float("nan"), True, "invalid")
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "finite number"):
                SyncXController(
                    adapter=_ConcurrentAdapter(),  # type: ignore[arg-type]
                    call_timeout_seconds=value,  # type: ignore[arg-type]
                )


if __name__ == "__main__":
    unittest.main()
