# XController

`XController` is a click-first Playwright library for automating common X.com workflows with a persistent browser profile.

It is designed for reuse from scripts and services instead of a single hard-coded workflow.

`main` is the clean integration branch for downstream projects. Development work uses short-lived branches and focused pull requests.

## What It Supports

- Persistent Chromium profile sessions
- Login checks and login-page handoff
- Home/timeline recovery with optional forced refresh
- Timeline, search, notifications, mentions, and visible-post reads
- Post, view, like, reply, quote, follow, and unfollow actions
- Image attachments for new posts, replies, and quote posts
- Post media capture for downstream media analysis
- Synchronous service facade for non-async runtimes
- Verified delete flows for own posts, replies, quotes, and reposts
- Bulk cleanup helpers for posts, replies, quotes, reposts, or all content
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

- `main`: releasable controller package and the branch downstream projects should consume.
- Use a short-lived branch for each focused change, keep it current with `origin/main`, and merge it through review after CI passes.
- Keep credentials, persistent browser profiles, local diagnostics, and generated artifacts out of every branch.

See [Branching workflow](docs/BRANCHING.md) for the exact commands.

## Notes For Reuse

- The controller is stateful and intended to be reused across multiple actions inside one started session.
- Public async workflows on one controller instance are serialized because they share one stateful page. Create separate controller/profile instances for intentional parallelism.
- The library does not expose a command-line interface; use the async adapter or synchronous facade from Python.
- Most flows prefer UI clicks and only fall back to direct URL navigation when recovery is needed.
- Navigation and action surfaces are restricted to HTTPS pages on `x.com` and `www.x.com`.
- Sync Playwright fallback is preferred on Windows to avoid event-loop/subprocess issues.
- Set `ControllerSettings(headless=True)` to launch Chromium without a visible window. Keep the default visible mode for manual login and debugging.
- Methods that mutate state are browser automation, not transactional API calls. Detailed results distinguish a confirmed result from an ambiguous submit outcome, and an ambiguous write trigger is not automatically replayed.
- `controller.last_action_error` records the latest soft UI failure with action, URL, selector summary, and message.
- Set `ControllerSettings(strict_ui_failures=True)` when you want soft UI failures to raise `UIActionError` instead of returning `False` or an empty result.
- Use the `*_detailed()` write methods, `preflight_action()`, `read_timeline_detailed()`, `attach_images_preflight()`, `debug_snapshot()`, and `health_check()` for long-running service integrations that need structured diagnostics.
- Use `SyncXController` when a service needs synchronous calls without owning an event-loop thread or coroutine bridge. Its calls are serialized and bounded by `call_timeout_seconds` (120 seconds by default); shutdown uses `close_timeout_seconds` (10 seconds by default). A timed-out call poisons that facade because the browser-side outcome is unknown. An in-flight synchronous Playwright call cannot be forcibly stopped in-process, so a service that must guarantee profile-lock release should isolate the controller in a restartable worker process.
- Call `adapter.sync()` only before starting that adapter. Wrapping an already-started async browser would cross event-loop ownership and is rejected.
- Use `login_state()` for passive login detection without importing selector internals.
- Use `capture_post_media(..., overwrite=False)` to create local artifacts for image cards and representative video frames attached to a post. Existing capture names are refused by default; replacing them requires explicit opt-in.
- Use `settle_after_action(tab="for_you", force_refresh=..., reset_scroll=...)` when a service needs a known home surface after a write/action flow.
- Use `account_stats(handle=None)` to sample public profile-level counts and metadata. It opens a temporary page when possible so periodic sampling does not move the controller's active feed page.
- Set `ControllerSettings(playwright_mode="async")`, `playwright_mode="sync"`, or `prefer_sync_playwright=True/False` when the embedding service needs explicit Playwright transport ownership. The synchronous facade controls the caller contract separately.
- Set `ControllerSettings(expected_account_handle="your_handle")` to make every mutation fail closed unless the active persistent profile can be verified as that account.
- Mutations are blocked while login is required, an account-lock or blocking modal is visible, or a scoped X system message reports rate limiting. A detected rate limit starts a local cooldown controlled by `rate_limit_cooldown_seconds` (300 seconds by default).

## Credential and Artifact Boundary

XController never accepts an X password or authentication token. Authentication lives in the persistent Chromium profile supplied by `profile_path`; protect that directory as credential material and never commit or share it. The repository ignores `data/profiles/` by default.

`debug_snapshot()` writes only a redacted JSON manifest by default. Passing `include_sensitive_content=True` additionally writes page HTML, a screenshot, visible dialogs, and article summaries. Snapshot targets and generated media-capture names are not replaced unless `overwrite=True`; symlink targets are refused. Treat opted-in snapshots and captured media as sensitive, choose a private location, and remove artifacts according to your retention policy. Do not point artifact output at a tracked source directory.

## CI/CD

GitHub Actions should validate `main` before the downstream project updates from it. The included workflow runs Ruff, executes the `tests/` suite with pytest and warnings treated as errors, builds both distributions, then installs the wheel and smoke-tests the public package surface on Linux and Windows.

See [CI/CD instructions](docs/CI_CD.md) for the release and update flow.

## Additional Docs

- [API reference](docs/API.md)
- [Architecture notes](docs/ARCHITECTURE.md)
- [Branching workflow](docs/BRANCHING.md)
- [CI/CD instructions](docs/CI_CD.md)
- [Change log](CHANGELOG.md)
