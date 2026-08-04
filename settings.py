from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Any


@dataclass
class ControllerSettings:
    """Runtime knobs for browser behavior and human-like interaction timing."""

    strict_ui_failures: bool = False
    playwright_mode: str = "auto"
    prefer_sync_playwright: bool | None = None
    headless: bool = False
    expected_account_handle: str | None = None
    rate_limit_cooldown_seconds: int = 300

    anti_bot_typing_min_ms: int = 40
    anti_bot_typing_max_ms: int = 260
    anti_bot_pause_min_ms: int = 250
    anti_bot_pause_max_ms: int = 1200
    anti_bot_mouse_move_ms: int = 120
    human_scroll_min_ms: int = 500
    human_scroll_max_ms: int = 2500

    browser_width_min: int = 1280
    browser_width_max: int = 1780
    browser_height_min: int = 820
    browser_height_max: int = 1120

    default_user_agent: str | None = None

    def __post_init__(self) -> None:
        """Reject invalid runtime settings before a browser is launched."""
        for name in ("strict_ui_failures", "headless"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be bool")
        if self.playwright_mode not in {"auto", "async", "sync"}:
            raise ValueError("playwright_mode must be one of: auto, async, sync")
        if self.prefer_sync_playwright is not None and not isinstance(self.prefer_sync_playwright, bool):
            raise TypeError("prefer_sync_playwright must be bool or None")

        integer_fields = (
            "anti_bot_typing_min_ms",
            "anti_bot_typing_max_ms",
            "anti_bot_pause_min_ms",
            "anti_bot_pause_max_ms",
            "anti_bot_mouse_move_ms",
            "human_scroll_min_ms",
            "human_scroll_max_ms",
            "browser_width_min",
            "browser_width_max",
            "browser_height_min",
            "browser_height_max",
            "rate_limit_cooldown_seconds",
        )
        for name in integer_fields:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")

        timing_ranges = (
            ("anti_bot_typing", self.anti_bot_typing_min_ms, self.anti_bot_typing_max_ms),
            ("anti_bot_pause", self.anti_bot_pause_min_ms, self.anti_bot_pause_max_ms),
            ("human_scroll", self.human_scroll_min_ms, self.human_scroll_max_ms),
        )
        for name, minimum, maximum in timing_ranges:
            if minimum < 0 or maximum < 0:
                raise ValueError(f"{name} timing values must be non-negative")
            if minimum > maximum:
                raise ValueError(f"{name}_min_ms must not exceed {name}_max_ms")
        if self.anti_bot_mouse_move_ms < 0:
            raise ValueError("anti_bot_mouse_move_ms must be non-negative")
        if self.rate_limit_cooldown_seconds <= 0:
            raise ValueError("rate_limit_cooldown_seconds must be positive")

        viewport_ranges = (
            ("browser_width", self.browser_width_min, self.browser_width_max),
            ("browser_height", self.browser_height_min, self.browser_height_max),
        )
        for name, minimum, maximum in viewport_ranges:
            if minimum <= 0 or maximum <= 0:
                raise ValueError(f"{name} values must be positive")
            if minimum > maximum:
                raise ValueError(f"{name}_min must not exceed {name}_max")
        if self.default_user_agent is not None and not isinstance(self.default_user_agent, str):
            raise TypeError("default_user_agent must be a string or None")
        if isinstance(self.default_user_agent, str) and not self.default_user_agent.strip():
            raise ValueError("default_user_agent must be a non-empty string")
        if self.expected_account_handle is not None:
            if not isinstance(self.expected_account_handle, str):
                raise TypeError("expected_account_handle must be a string or None")
            handle = self.expected_account_handle.strip().lstrip("@")
            if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
                raise ValueError("expected_account_handle must be a valid X handle")
            self.expected_account_handle = handle

    @classmethod
    def from_any(cls, value: Any | None) -> "ControllerSettings":
        if isinstance(value, cls):
            value.__post_init__()
            return value
        if value is None:
            return cls()
        if isinstance(value, Mapping):
            unknown = sorted(set(value) - set(cls.__dataclass_fields__))
            if unknown:
                raise ValueError(f"unknown controller setting(s): {', '.join(unknown)}")
        data: dict[str, Any] = {}
        for key in cls.__dataclass_fields__.keys():
            if isinstance(value, Mapping) and key in value:
                data[key] = value[key]
            elif hasattr(value, key):
                data[key] = getattr(value, key)
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return {
            key: getattr(self, key)
            for key in self.__dataclass_fields__.keys()
        }
