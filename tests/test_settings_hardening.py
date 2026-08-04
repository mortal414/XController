from __future__ import annotations

import unittest

from XController.settings import ControllerSettings


class ControllerSettingsHardeningTests(unittest.TestCase):

    def test_from_any_rejects_unknown_mapping_keys(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown controller setting"):
            ControllerSettings.from_any({"headless": True, "typo_headless": False})
    def test_accepts_supported_playwright_modes(self) -> None:
        for mode in ("auto", "async", "sync"):
            with self.subTest(mode=mode):
                self.assertEqual(ControllerSettings(playwright_mode=mode).playwright_mode, mode)

    def test_rejects_unsupported_playwright_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "playwright_mode"):
            ControllerSettings(playwright_mode="threaded")
        with self.assertRaisesRegex(TypeError, "prefer_sync_playwright"):
            ControllerSettings(prefer_sync_playwright=1)  # type: ignore[arg-type]

    def test_rejects_negative_or_reversed_timing_ranges(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-negative"):
            ControllerSettings(anti_bot_pause_min_ms=-1)
        with self.assertRaisesRegex(ValueError, "must not exceed"):
            ControllerSettings(human_scroll_min_ms=10, human_scroll_max_ms=5)

    def test_rejects_invalid_viewport_and_user_agent(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive"):
            ControllerSettings(browser_width_min=0)
        with self.assertRaisesRegex(ValueError, "must not exceed"):
            ControllerSettings(browser_height_min=1000, browser_height_max=900)
        with self.assertRaisesRegex(ValueError, "non-empty"):
            ControllerSettings(default_user_agent="   ")
        self.assertIsNone(ControllerSettings().default_user_agent)
        self.assertEqual(
            ControllerSettings(default_user_agent="custom-agent").default_user_agent,
            "custom-agent",
        )

    def test_validates_mutation_safety_settings(self) -> None:
        settings = ControllerSettings(
            expected_account_handle="@Expected_User",
            rate_limit_cooldown_seconds=45,
        )
        self.assertEqual(settings.expected_account_handle, "Expected_User")
        self.assertEqual(settings.rate_limit_cooldown_seconds, 45)
        with self.assertRaisesRegex(ValueError, "valid X handle"):
            ControllerSettings(expected_account_handle="bad/handle")
        with self.assertRaisesRegex(ValueError, "must be positive"):
            ControllerSettings(rate_limit_cooldown_seconds=0)

    def test_rejects_wrong_scalar_types(self) -> None:
        with self.assertRaisesRegex(TypeError, "strict_ui_failures"):
            ControllerSettings(strict_ui_failures=1)  # type: ignore[arg-type]
        with self.assertRaisesRegex(TypeError, "anti_bot_typing_min_ms"):
            ControllerSettings(anti_bot_typing_min_ms=1.5)  # type: ignore[arg-type]
        with self.assertRaisesRegex(TypeError, "default_user_agent"):
            ControllerSettings(default_user_agent=123)  # type: ignore[arg-type]

    def test_from_any_revalidates_mutated_settings(self) -> None:
        settings = ControllerSettings()
        settings.browser_width_min = -1
        with self.assertRaisesRegex(ValueError, "positive"):
            ControllerSettings.from_any(settings)


if __name__ == "__main__":
    unittest.main()
