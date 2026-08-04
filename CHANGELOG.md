# Changelog

Use this file for the next feature or release notes.

## Unreleased

- Harden browser boundaries and mutation safety: restrict action surfaces to trusted X origins, require exact status/article identity, remove DOM/force-click fallbacks, fail closed on content-kind mismatches, and avoid replaying ambiguous write triggers.
- Validate controller settings (including unknown mapping keys) and public handles/limits before browser work, bound read results exactly, filter out-of-window mentions, and improve closed-page health reporting and cleanup.
- Serialize synchronous facade calls with finite configurable timeouts, poison the facade after an outcome-ambiguous timeout, make shutdown cancellation-aware, and add complete follow/unfollow parity.
- Make debug snapshots manifest-only by default with explicit sensitive-content opt-in, and prevent snapshots or media capture from silently replacing existing artifacts.
- Keep newly surfaced quote and quote-cleanup hooks non-abstract so existing `SocialPlatformAdapter` subclasses remain compatible in 0.1.x.
- Align branch and CI documentation with `main` as the only published long-lived branch.
- Make tests load the current checkout hermetically from arbitrary worktree names and add focused safety regressions for browser, write, read, settings, lifecycle, and sync behavior.
- Use the bundled Chromium user agent by default instead of a stale hard-coded browser version, while retaining an explicit override, and treat mouse-move timing as milliseconds rather than a Playwright step count.
- Issue #12: harden browser diagnostics by centralizing `ActionResult.failure_stage` taxonomy, adding structured parser-warning capture for read fallbacks, and enriching unknown-id submission confirmations for reply/quote flows.
- Refactor the former monolithic `adapter.py` into cohesive internal modules:
  `_adapter_runtime.py`, `_adapter_read.py`, and `_adapter_write.py` while keeping
  `XController`/`XTextAdapter` imports and behavior unchanged from the package root.
- Standardize adapter module loggers to module-local `logging.getLogger(__name__)`.
- Keep compatibility exports used by the sync facade (`ImagePath`, `ImagePathInput`)
  on `adapter.py` after the internal split.
- Add `AccountStats` and `account_stats(handle=None)` for public profile/account-level stats with compact count normalization and raw parse diagnostics.
- Add service-integration data models: `ActionResult`, `ActionPreflight`, `TimelineReadResult`, `MediaPreflight`, `ControllerHealth`, and `ObservedMediaData`.
- Add detailed action methods, action preflight, media preflight, thread-context reads, health checks, and debug snapshots for long-running browser services.
- Add timeline tab control with detailed `For You` / `Following` read reporting.
- Add explicit Playwright runtime controls through `playwright_mode` and `prefer_sync_playwright`.
- Split selector tables and soft-failure diagnostics into dedicated internal modules.
- Add `ActionFailureInfo`, `UIActionError`, and `strict_ui_failures` for observable or fail-fast UI automation errors.
- Add runtime lifecycle tests and expand CI to Ubuntu and Windows runners.
- Detect author-limited reply notices such as "Only some accounts can reply" in read results.
- Add recent and unread notification reads.
- Add local image attachments for new posts, replies, and quote posts.
- Replace duplicate home and notification aliases with `return_home(force_refresh=False)` and `read_notifications(unread_only=True)`.
- Remove `comment_post()` in favor of canonical `reply_to_post()`.
- Add `SyncXController` / `XControllerService` for synchronous service callers.
- Add `LoginState` and passive `login_state()` selector-owned login detection.
- Add `MediaCaptureData` and `capture_post_media()` for local post media artifacts.
- Add `settle_after_action()` for opt-in post-action home-tab settling.
