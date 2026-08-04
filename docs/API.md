# API Notes

This document describes the public package surface shipped on `main`.

This package exposes one main controller class:

- `XController`
- `XTextAdapter` (compatibility alias of `XController`)
- `SyncXController` / `XControllerService` for synchronous callers

Additional exported diagnostics types:

- `AccountStats`
- `ActionFailureInfo`
- `ActionPreflight`
- `ActionResult`
- `ControllerHealth`
- `LoginState`
- `MediaCaptureData`
- `MediaPreflight`
- `ObservedMediaData`
- `TimelineReadResult`
- `UIActionError`

## Session Lifecycle

- `await start()`: starts a persistent Chromium context
- `await close()`: closes the browser context and Playwright resources
- `await is_logged_in() -> bool`
- `await login_state() -> LoginState`
- `await open_login_page() -> None`
- `await current_state() -> dict[str, str]`
  When a soft UI failure was recorded, `current_state()` also includes `last_action_error`.

`login_state()` is passive: it uses XController-owned selector tables and current URL/state detection without forcing home navigation. It returns `logged_in`, `page_state`, `url`, `browser_started`, `active_home_tab`, and `login_required`.

## Synchronous Facade

`SyncXController(profile_path=..., settings=..., proxy=..., call_timeout_seconds=120, close_timeout_seconds=10)` owns an internal event loop thread and exposes synchronous methods for service runtimes that cannot call async controller methods directly. Calls are serialized because one controller owns one stateful browser page. Expired calls are cancelled and raise `TimeoutError`; because cancellation cannot prove that a browser-side action stopped, the facade is then poisoned and must be closed/recreated. An already-running synchronous Playwright executor call may outlive in-process cancellation and retain the profile lock; deployments that require hard timeout isolation should run the controller in a restartable worker process. Shutdown uses its own finite timeout. `XControllerService` is an alias.

The facade forwards stable service APIs including lifecycle, login state, current state/surface, health checks, detailed timeline reads, notifications, thread context, search, action preflight, detailed actions, metrics, account stats, debug snapshots, and media capture.

One async adapter also serializes its public workflows around the single stateful page. `adapter.sync()` must be called before `adapter.start()`; an already-started adapter is rejected rather than transferred across event loops.

`ControllerSettings(playwright_mode="sync", prefer_sync_playwright=True)` controls Playwright transport inside the browser controller. `SyncXController` controls the caller contract and removes the need for downstream code to own `asyncio.new_event_loop()`, worker threads, or `asyncio.run_coroutine_threadsafe()`.

## Navigation / Recovery

- `await return_home(force_refresh: bool = False) -> bool`
  Returns to home if possible. When `force_refresh=True`, the controller reloads the home surface after returning.
- `await settle_after_action(tab: str = "for_you", force_refresh: bool = False, reset_scroll: bool = False) -> bool`
  Opt-in helper for services that want to settle back to a home tab after an action.

## Read Operations

- `await read_timeline(limit: int = 20) -> list[ObservedPostData]`
- `await read_timeline_detailed(limit: int = 20, tab: str = "for_you", force_refresh: bool = False, reset_scroll: bool = False) -> TimelineReadResult`
- `await read_following_timeline(limit: int = 20) -> list[ObservedPostData]`
- `await search_posts(query: str, limit: int = 10) -> list[ObservedPostData]`
- `await read_visible_posts(limit: int = 20) -> list[ObservedPostData]`
- `await read_notifications(limit: int = 20, unread_only: bool = False) -> list[ObservedNotificationData]`
- `await read_mentions(account_handle: str, hours_back: int = 2, limit: int = 120, ...)`
- `await read_post_thread_context(post_id, limit: int = 6, ...) -> list[ObservedPostData]`
- `await account_stats(handle: str | None = None) -> AccountStats`
- `await profile_recent_metrics(username: str, limit: int = 40, source: str | None = None) -> list[dict[str, int | str | bool]]`
- `await post_metrics(platform_post_id: str) -> dict[str, int]`
- `await capture_post_media(platform_post_id, output_dir, frame_count: int = 3, *, overwrite: bool = False) -> list[MediaCaptureData]`

`read_notifications(unread_only=True)` returns unread notifications without a separate alias method.

`profile_recent_metrics(source="posts" | "with_replies")` samples visible profile posts or replies-surface rows and returns the post id, URL, author handle, text, common metrics, source surface, and `is_reply` / `is_quote` / `is_repost` classification flags. `limit` is capped internally to protect browser scans.

`read_timeline_detailed(force_refresh=True)` performs a home reload before collecting timeline posts. `reset_scroll=True` only presses Home to read the newest visible DOM items at the top of the feed; it does not reload the UI.

`capture_post_media()` opens the target post, resolves the exact main article, screenshots image cards and representative video frames into `output_dir`, and returns normalized artifact rows. `frame_count` must be a positive integer. Existing generated names are refused unless `overwrite=True`, and symlink targets are always refused.

`account_stats()` samples public profile-level data: handle, display name, profile URL, followers, following, posts, likes, media, verified state, bio, location, and joined date text. When `handle` is omitted, it uses the authenticated account when detectable and falls back to the current profile surface if needed. Counts are normalized from compact X strings such as `1.2K`, `3.4M`, and `5B`. Unavailable fields remain zero or `None`, and `raw["warnings"]` plus `raw["current_url"]` explain what was missing. Browser transport failures such as `profile_in_use`, `playwright_driver_connection_closed`, and `target_page_or_context_closed` are raised as `RuntimeError`.

## Write / Engagement Operations

- `await post_text(text: str, image_paths: str | Sequence[str] | None = None) -> str | None`
- `await view_post(platform_post_id: str, dwell_seconds: tuple[int, int] = (3, 8)) -> bool`
- `await like_post(platform_post_id: str) -> bool`
- `await reply_to_post(platform_post_id: str, text: str, image_paths: str | Sequence[str] | None = None) -> str | None`
- `await quote_post(platform_post_id: str, text: str = "", image_paths: str | Sequence[str] | None = None) -> str | None`
- `await delete_post(platform_post_id: str) -> bool`
- `await delete_reply(platform_post_id: str) -> bool`
- `await delete_repost(platform_post_id: str) -> bool`
- `await delete_quote(platform_post_id: str) -> bool`
- `await delete_all_posts() -> list[str]`
- `await delete_all_replies() -> list[str]`
- `await delete_all_reposts() -> list[str]`
- `await delete_all_quotes() -> list[str]`
- `await delete_all_content() -> dict[str, list[str]]`
- `await follow_user(username: str) -> bool`
- `await unfollow_user(username: str) -> bool`
- `await engage_post(platform_post_id: str, do_view: bool = True, do_like: bool = False, ...) -> dict[str, bool]`

Detailed write/action variants:

- `await post_text_detailed(text: str, image_paths: ...) -> ActionResult`
- `await reply_to_post_detailed(platform_post_id: str, text: str, image_paths: ...) -> ActionResult`
- `await quote_post_detailed(platform_post_id: str, text: str = "", image_paths: ...) -> ActionResult`
- `await like_post_detailed(platform_post_id: str) -> ActionResult`
- `await view_post_detailed(platform_post_id: str, dwell_seconds: tuple[int, int] = (3, 8)) -> ActionResult`
- `await repost_post_detailed(platform_post_id: str) -> ActionResult`
- `await follow_user_detailed(username: str) -> ActionResult`
- `await unfollow_user_detailed(username: str) -> ActionResult`
- `await delete_post_detailed(platform_post_id: str, kind: str = "post") -> ActionResult`

Detailed action methods report the surface observed after the action in `current_url`, `current_state`, and `active_home_tab`. They do not promise to leave the browser on a single surface across all action types because X composer and confirmation flows differ. Call `settle_after_action(tab="for_you", force_refresh=..., reset_scroll=...)` when a service requires a known home surface before the next read.

Preflight and diagnostics:

- `await preflight_action(platform_post_id: str, action: str = "reply", open_composer: bool = False) -> ActionPreflight`
- `await attach_images_preflight(image_paths) -> MediaPreflight`
- `await current_surface() -> dict[str, str]`
- `await settle_home(tab: str = "for_you", force_nav: bool = False) -> bool`
- `await health_check() -> ControllerHealth`
- `await debug_snapshot(output_dir, article_limit: int = 12, *, overwrite: bool = False, include_sensitive_content: bool = False) -> dict`

`debug_snapshot()` writes a redacted manifest by default. HTML, a screenshot, article summaries, visible dialogs, and detailed login data are omitted unless `include_sensitive_content=True`; when enabled, all emitted artifacts are sensitive browser-session data. Existing requested targets are never overwritten unless `overwrite=True`, symlink targets are refused, and safe mode refuses a directory that already contains `page.html` or `page.png` rather than silently leaving sensitive artifacts beside a new manifest.

Image attachment preflight validates PNG, JPEG, GIF, and WebP file signatures in addition to paths and extensions.

## Compatibility Guidance

- `SocialPlatformAdapter.quote_post()`, `delete_quote()`, and `delete_all_quotes()` remain optional, non-abstract extension hooks in the 0.1.x contract. Existing third-party adapter subclasses are not made abstract by the quote-cleanup additions.
- `reply_to_post()` is the canonical X-specific method name.
- `comment_post()` has been removed. Use `reply_to_post()`.
- `read_unread_notifications()` has been removed. Use `read_notifications(unread_only=True)`.
- `recover_home()` and `refresh_home()` have been replaced by `return_home(force_refresh=False)`.
- `post_text()`, `reply_to_post()`, and `quote_post()` accept `image_paths` for local image uploads. `post_image()`, `reply_with_image()`, and `quote_post_with_image()` remain only as deprecated compatibility wrappers.
- Delete methods verify exact target identity, independently observed content kind, and ownership before deleting authored content.
  `delete_repost()` verifies repost state instead of authorship because reposts target another author's post.
- Bulk delete methods run until the relevant profile surface is exhausted; they no longer expose a caller-supplied item limit.

## Diagnostics

`XTextAdapter.last_action_error` holds the latest soft UI failure that was converted into a boolean/empty-result outcome.

`ActionResult.failure_stage` uses a stable taxonomy:

- `not_started`
- `target_lookup`
- `media_attach`
- `text_entry`
- `submit_lookup`
- `confirmation`
- `post_submit`
- `composer_open`
- `action_control`
- `preflight`
- `unknown`

Common `ActionResult.failure_reason` values include:

- `page_not_started`
- `target_post_not_found`
- `media_preflight_failed`
- `text_entry_failed`
- `reply_submit_trigger_not_found`
- `submit_blocked_by_audience_modal`
- `submit_not_confirmed`
- `reply_limited`
- `unknown_exception`
- `target_user_not_found`

Parser fallbacks now emit bounded structured parser warnings (category/reason/context) into controller diagnostics. They are exposed in timeline warnings (`TimelineReadResult.warnings`) and compact recent-warning summaries surfaced by `health_check()` / `debug_snapshot()`.

For long-running services, prefer detailed methods over legacy compact methods. The compact methods are kept for compatibility and return the same shapes as before; a future major version is expected to make the `ActionResult`-returning methods the default.

`ActionFailureInfo` contains:

- `action`
- `error_type`
- `message`
- `url`
- `selector`
- `occurred_at`

Set `ControllerSettings(strict_ui_failures=True)` to turn those soft UI failures into `UIActionError`.

## Data Model

`ObservedPostData` contains:

- `platform_post_id`
- `author`
- `text`
- `raw`

Convenience helpers:

- `.metrics`: returns `raw["metrics"]` when present
- `.author_limited`: true when X shows an author-controlled post limit, such as restricted replies
- `.reply_limited`: true when the detected author limit affects replies
- `.author_limit_notice`: the detected X notice text, for example `Only some accounts can reply.`
- `.to_dict()`: returns a serializable copy

`ObservedNotificationData` contains:

- `notification_id`
- `notification_type`
- `actor`
- `text`
- `raw`

Convenience helpers:

- `.platform_post_id`: returns the linked post id when present
- `.unread`: returns the best-effort unread flag
- `.author_limited`: true when X shows an author-controlled post limit on the linked post
- `.reply_limited`: true when the detected author limit affects replies
- `.author_limit_notice`: the detected X notice text
- `.to_dict()`: returns a serializable copy

`MediaCaptureData` contains:

- `kind`
- `path`
- `target_post_id`
- `source_url`
- `thumbnail_url`
- `alt_text`
- `raw`
- `.to_dict()`: returns a serializable copy

`LoginState` contains:

- `logged_in`
- `page_state`
- `url`
- `browser_started`
- `active_home_tab`
- `login_required`
- `raw`
- `.to_dict()`: returns a serializable copy

`AccountStats` contains:

- `handle`
- `display_name`
- `profile_url`
- `followers`
- `following`
- `posts`
- `likes`
- `media`
- `verified`
- `bio`
- `location`
- `joined_at`
- `captured_at`
- `raw`

`raw` includes `current_url`, `target_url`, `warnings`, and parser diagnostics such as count sources.

## Settings

`ControllerSettings` accepts:

- another `ControllerSettings`
- a plain object with matching attributes
- a mapping/dict with matching keys

Use `ControllerSettings.to_dict()` when you need to persist or inspect the active values.

Mappings with unknown keys are rejected instead of silently ignored. Plain objects contribute only matching attributes. Invalid boolean or integer field types, transport modes, account handles, cooldowns, negative or reversed timing ranges, non-positive or reversed viewport ranges, and empty user-agent strings raise during settings construction instead of failing after browser startup. `default_user_agent=None` (the default) preserves the bundled browser's native user agent; set a non-empty string only when an embedding application explicitly needs an override.

Notable maintainability setting:

- `strict_ui_failures`
  Raises `UIActionError` for soft UI failures instead of quietly returning fallback values.
- `playwright_mode`
  Accepts `"auto"`, `"async"`, or `"sync"` to control whether startup prefers async Playwright or the sync fallback.
- `prefer_sync_playwright`
  Optional boolean override for callers that need explicit runtime/thread ownership.
- `expected_account_handle`
  Optional X handle that must match the authenticated persistent profile before any mutation is allowed.
- `rate_limit_cooldown_seconds`
  Positive local cooldown applied after a scoped visible X system message reports rate limiting. Defaults to 300 seconds.
