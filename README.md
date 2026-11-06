# XController

`XController` is a click-first Playwright library for automating common X.com workflows with a persistent browser profile.

I built it as a personal browser-automation experiment, with reusable scripts and services in mind.

**Status:** I'm not currently using or developing this project. X's interface can change, so the workflows may need repairs before use. Use only accounts and actions you are authorized to control.

`main` contains the controller package. The separate `labui-testing` branch contains lab UI, API, and test-harness experiments.

## What It Supports

- Persistent Chromium profile sessions
- Login checks and login-page handoff
- Home/timeline recovery with optional forced refresh
- Timeline, search, notifications, mentions, and visible-post reads
- Post, view, like, reply, quote, follow, and unfollow actions
- Image attachments for new posts, replies, and quote posts
- Post media capture for downstream media analysis
- Synchronous service facade for non-async runtimes
- Verified delete flows for own posts, replies, and reposts
- Bulk cleanup helpers for posts, replies, reposts, or all content
- Post metric scraping

## Installation

Core library only, from inside this directory:

```bash
python -m pip install -e .
playwright install chromium
```

Core library only, from the parent directory:

```bash
python -m pip install -e ./XController
playwright install chromium
```

The packaging metadata is configured so this flat package layout installs as `XController`.

## Quick Start

```python
import asyncio

from XController import XController


async def main() -> None:
    controller = XController(profile_path="data/profiles/default_profile")
    await controller.start()
    try:
        if not await controller.is_logged_in():
            await controller.open_login_page()
            return

        posts = await controller.search_posts("browser automation", limit=5)
        if posts:
            await controller.view_post(posts[0].platform_post_id)
    finally:
        await controller.close()


asyncio.run(main())
```

## Public API

Primary exports:

- `XController.XController`
- `XController.XTextAdapter`
- `XController.SyncXController`
- `XController.XControllerService`
- `XController.AccountStats`
- `XController.ActionFailureInfo`
- `XController.ActionPreflight`
- `XController.ActionResult`
- `XController.ControllerSettings`
- `XController.ControllerHealth`
- `XController.LoginState`
- `XController.MediaCaptureData`
- `XController.MediaPreflight`
- `XController.ObservedMediaData`
- `XController.ObservedNotificationData`
- `XController.ObservedPostData`
- `XController.TimelineReadResult`
- `XController.UIActionError`

Compatibility notes:

- `XController` and `XTextAdapter` are the same class.
- `SyncXController` and `XControllerService` are the same synchronous facade.
- `reply_to_post()` is the canonical X-specific reply method.
- `return_home(force_refresh=False)` replaces the older recovery/refresh split.
- `read_notifications(unread_only=True)` replaces the unread-notification alias.
- `post_text()`, `reply_to_post()`, and `quote_post()` accept `image_paths`; the image-only convenience wrappers are deprecated.

## Project Layout

- `adapter.py`: public adapter surface and compatibility re-exports
- `_adapter_runtime.py`: startup, shutdown, navigation, and low-level Playwright helpers
- `_adapter_read.py`: timeline, notifications, account stats, preflight, and diagnostics read flows
- `_adapter_write.py`: posting, engagement, delete, follow, and other mutating flows
- `sync.py`: synchronous service facade over the async controller APIs
- `_ui_selectors.py`: centralized X DOM selector and UI rule tables
- `_diagnostics.py`: soft-failure diagnostics and strict-mode error types
- `base.py`: shared adapter contract and post model
- `settings.py`: runtime settings and configuration normalization
- `human.py`: human-like timing and mouse/typing helpers
- `docs/API.md`: method-level API notes and compatibility guidance
- `docs/ARCHITECTURE.md`: internal structure and extension points
- `docs/BRANCHING.md`: branch responsibilities and daily git workflow
- `docs/CI_CD.md`: CI checks and release/update process
- `CHANGELOG.md`: next-feature notes

## Branch Workflow

- `main`: reusable controller package only. This is the branch the downstream project should pull from.
- `labui-testing`: manual UI/API tooling and test-only work. Keep it rebased on `main`.
- When a core fix is developed on `labui-testing`, cherry-pick or open a focused PR back into `main`. Do not merge the branch wholesale or the lab/test files come back.

See [Branching workflow](docs/BRANCHING.md) for the exact commands.

## Notes For Reuse

- The controller is stateful and intended to be reused across multiple actions inside one started session.
- Most flows prefer UI clicks and only fall back to direct URL navigation when recovery is needed.
- Sync Playwright fallback is preferred on Windows to avoid event-loop/subprocess issues.
- Set `ControllerSettings(headless=True)` to launch Chromium without a visible window. Keep the default visible mode for manual login and debugging.
- Methods that mutate state should be treated as best-effort browser automation, not transactional API calls.
- `controller.last_action_error` records the latest soft UI failure with action, URL, selector summary, and message.
- Set `ControllerSettings(strict_ui_failures=True)` when you want soft UI failures to raise `UIActionError` instead of returning `False` or an empty result.
- Use the `*_detailed()` write methods, `preflight_action()`, `read_timeline_detailed()`, `attach_images_preflight()`, `debug_snapshot()`, and `health_check()` for long-running service integrations that need structured diagnostics.
- Use `SyncXController` when a service needs synchronous calls without owning an event-loop thread or coroutine bridge.
- Use `login_state()` for passive login detection without importing selector internals.
- Use `capture_post_media()` to create local artifacts for image cards and representative video frames attached to a post.
- Use `settle_after_action(tab="for_you", force_refresh=..., reset_scroll=...)` when a service needs a known home surface after a write/action flow.
- Use `account_stats(handle=None)` to sample public profile-level counts and metadata. It opens a temporary page when possible so periodic sampling does not move the controller's active feed page.
- Set `ControllerSettings(playwright_mode="async")`, `playwright_mode="sync"`, or `prefer_sync_playwright=True/False` when the embedding service needs explicit Playwright transport ownership. The synchronous facade controls the caller contract separately.

## CI/CD

GitHub Actions should validate `main` before the downstream project updates from it. The included workflow performs install/import/build checks on both `main` and `labui-testing`, and runs the `tests/` suite with `unittest` discovery only when tests exist on the branch.

See [CI/CD instructions](docs/CI_CD.md) for the release and update flow.

## Additional Docs

- [API reference](docs/API.md)
- [Architecture notes](docs/ARCHITECTURE.md)
- [Branching workflow](docs/BRANCHING.md)
- [CI/CD instructions](docs/CI_CD.md)
- [Change log](CHANGELOG.md)
