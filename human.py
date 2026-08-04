from __future__ import annotations

import asyncio
import random

from .settings import ControllerSettings


class HumanMotion:
    def __init__(self, settings: ControllerSettings | None = None) -> None:
        self.settings = settings or ControllerSettings()

    async def jitter(self, ms_min: int | None = None, ms_max: int | None = None) -> None:
        low = ms_min if ms_min is not None else self.settings.anti_bot_pause_min_ms
        high = ms_max if ms_max is not None else self.settings.anti_bot_pause_max_ms
        await asyncio.sleep(random.uniform(low, high) / 1000.0)

    async def type_like_human(self, element, text: str) -> None:
        for ch in text:
            await element.type(
                ch,
                delay=random.uniform(
                    self.settings.anti_bot_typing_min_ms,
                    self.settings.anti_bot_typing_max_ms,
                ),
            )
            if random.random() < 0.02:
                await self.jitter(20, 220)

    async def move_mouse_random(self, page) -> None:
        viewport = page.viewport_size or {"width": 1365, "height": 768}
        x = random.randint(0, viewport["width"])
        y = random.randint(0, viewport["height"])
        duration_ms = max(0, self.settings.anti_bot_mouse_move_ms)
        steps = max(1, min(120, round(duration_ms / 16)))
        await page.mouse.move(x, y, steps=steps)
        if duration_ms:
            await asyncio.sleep(duration_ms / 1000.0)
        await self.jitter(20, 180)

    async def wait_for_network_idle(self, page, ms: int = 900) -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=ms)
        except Exception as exc:
            # Network-idle timeouts are expected on X's continuously active
            # pages.  Browser/context closure and transport failures are not:
            # callers need those errors in order to recover the runtime.
            error_text = f"{type(exc).__name__}: {exc}".lower()
            if "timeout" not in error_text:
                raise
            await self.jitter(ms, ms + 500)

    def pick_weighted(self, options: dict[str, float]) -> str:
        normalized = [max(weight, 0.0) for weight in options.values()]
        total = sum(normalized) or 1.0
        normalized = [w / total for w in normalized]
        keys = list(options.keys())
        return random.choices(keys, normalized, k=1)[0]
