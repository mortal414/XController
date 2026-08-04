# Architecture Notes

## Design Intent

The package is structured around one stateful adapter that owns:

- Playwright startup and shutdown
- browser/profile recovery
- shared DOM interaction helpers
- X-specific navigation and action flows
- login-state detection and media capture

The DOM selector surface and soft-failure diagnostics are now split into dedicated internal modules so the adapter remains the orchestrator instead of also being the source of truth for every selector and diagnostic type.

The library favors click-driven navigation first and uses direct URL navigation as a recovery path.

`main` is the repository's single long-lived branch and contains the reusable package, its regression tests, packaging metadata, and maintained documentation. Experimental applications belong in separate repositories or focused short-lived branches.

## Current Main-Branch Modules

- `adapter.py`
  Thin public composition layer that combines the runtime, read, and write mixins.
- `_adapter_runtime.py`
  Browser lifecycle, trusted-origin navigation, low-level DOM helpers, article identity, and shared state detection.
- `_adapter_read.py`
  Timeline, search, notification, profile, health, preflight, diagnostic, and media-capture reads.
- `_adapter_write.py`
  Compose, engagement, follow/unfollow, and verified deletion flows.
- `sync.py`
  Synchronous facade that owns an event-loop thread and forwards stable service APIs.
- `_ui_selectors.py`
  Centralized selector and UI rule tables for X-specific DOM matching.
- `_diagnostics.py`
  `ActionFailureInfo` and `UIActionError` used to surface soft UI failures.
- `base.py`
  Shared adapter contract, observed data models, detailed action results, preflight results, timeline read results, and health/media diagnostics.
- `settings.py`
  Runtime knobs for browser size, typing cadence, pauses, and user-agent defaults.
- `human.py`
  Helper methods for jitter, typing cadence, mouse movement, and network-idle waits.

## Adapter Organization

Inside `XController`, methods fall into clearer buckets:

- startup/shutdown
- sync fallback helpers
- low-level DOM helpers
- navigation helpers
- post collection/parsing helpers
- public read/write actions

The current maintenance boundary is:

- `_ui_selectors.py`
  Selector drift and X wording changes.
- `_adapter_runtime.py`
  Browser ownership, trusted X origin checks, state transitions, low-level actionability, and post/article identity.
- `_adapter_read.py`
  Non-mutating collection, parsing, health, diagnostics, and post media capture.
- `_adapter_write.py`
  Mutating orchestration and detailed action outcomes. A delivered write trigger is never replayed merely because confirmation is ambiguous.
- `sync.py`
  Synchronous caller contract only. It should not duplicate DOM selectors or browser flow logic.
- `_diagnostics.py`
  Soft-failure recording and strict-mode escalation.

## Service Integration APIs

Long-running service callers should prefer:

- `preflight_action()`
  Check reply, quote, or like feasibility before spending generation or media work.
- `*_detailed()`
  Capture stable failure stages and reasons for write actions while preserving legacy compact methods.
- `read_timeline_detailed()`
  Report requested tab, active tab, URL, article count, and warnings for home timeline reads.
- `SyncXController`
  Call the same service APIs from synchronous runtimes without a downstream event-loop bridge.
- `login_state()`
  Get passive login/session status without importing selector internals.
- `capture_post_media()`
  Capture local post media artifacts for downstream media analysis.
- `settle_after_action()`
  Settle back to a requested home tab after write/action flows when a service needs a known surface.
- `debug_snapshot()` and `health_check()`
  Capture current browser state without forcing downstream wrappers to duplicate selector probes.

The main cleanup rule applied here was: keep the public behavior stable, but consolidate duplicate internal logic where possible.

## Safety Boundaries

- Only HTTPS `x.com` and `www.x.com` pages are trusted as action surfaces. Existing unrelated browser tabs are not adopted for X interactions.
- Targeted actions resolve an exact normalized status ID; substring URL matches and first-article fallbacks are rejected.
- Destructive actions require an independently observed content kind, ownership/state checks, and an article-scoped native control.
- All mutations pass a shared health gate for trusted origin, login state, account locks, blocking modals, rate-limit cooldown, and optional expected-account identity.
- Persistent Chromium profiles are authentication material. They are ignored by Git and must be protected outside the package.
- Diagnostic snapshots and captured media may contain private visible content and must be stored and retained as sensitive artifacts.
- The synchronous facade serializes access to the one stateful page and applies finite call timeouts.

## Compatibility Choices

- `XController` remains an alias of `XTextAdapter`.
- `reply_to_post()` is the only public reply method name.
- `return_home(force_refresh=False)` is the single public home-recovery entrypoint.
- Image-capable post, reply, and quote methods accept `image_paths`; image-only wrappers are deprecated compatibility helpers.
- Detailed action methods report their observed final surface, but callers that require a known feed surface should call `settle_after_action()`.

## Branch Policy

- `main`
  Releasable controller code, tests, packaging, and documentation.
- Short-lived branches
  Focused implementation, test, or documentation work merged through pull requests.

The current remote has no `labui-testing` branch. CI validates pushes to `main` and pull requests targeting `main`.

## Packaging

This repository uses a flat package layout where the package root is the project root itself.

`pyproject.toml` now explicitly maps the package name `XController` to `.` so editable installs and wheels do not depend on implicit setuptools discovery.

## Recommended Next Steps

- Split non-X-specific browser helpers into a reusable internal module if another platform adapter is added.
- Keep focused core regression tests in `main`, especially for target identity, origin checks, at-most-once writes, lifecycle cleanup, and synchronous serialization.
- Add an opt-in real-browser selector-contract job that uses a disposable, non-authenticated profile; authenticated mutation coverage must remain an explicitly controlled environment.
- Introduce semantic version tags once the API is stable enough for third-party consumers.
