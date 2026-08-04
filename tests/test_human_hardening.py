from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from XController.human import HumanMotion
from XController.settings import ControllerSettings


class _Page:
    def __init__(self, error: Exception):
        self.error = error

    async def wait_for_load_state(self, *_args, **_kwargs) -> None:
        raise self.error


class _MousePage:
    viewport_size = {"width": 100, "height": 100}

    def __init__(self) -> None:
        self.mouse = unittest.mock.MagicMock()
        self.mouse.move = AsyncMock()


class HumanMotionHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def test_network_idle_timeout_settles_without_failing(self) -> None:
        human = HumanMotion()
        human.jitter = AsyncMock()

        await human.wait_for_network_idle(_Page(TimeoutError("timed out")), ms=25)

        human.jitter.assert_awaited_once_with(25, 525)

    async def test_network_idle_propagates_target_closure(self) -> None:
        human = HumanMotion()
        human.jitter = AsyncMock()

        with self.assertRaisesRegex(RuntimeError, "Target page has been closed"):
            await human.wait_for_network_idle(_Page(RuntimeError("Target page has been closed")), ms=25)
        human.jitter.assert_not_awaited()

    async def test_mouse_move_setting_is_duration_not_raw_step_count(self) -> None:
        human = HumanMotion(ControllerSettings(anti_bot_mouse_move_ms=32))
        human.jitter = AsyncMock()
        page = _MousePage()

        with patch("XController.human.asyncio.sleep", new=AsyncMock()) as sleep:
            await human.move_mouse_random(page)

        self.assertEqual(page.mouse.move.await_args.kwargs["steps"], 2)
        sleep.assert_awaited_once_with(0.032)
        human.jitter.assert_awaited_once_with(20, 180)


if __name__ == "__main__":
    unittest.main()
