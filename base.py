from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ObservedMediaData:
    """Normalized media payload attached to an observed post."""

    kind: str
    url: str = ""
    thumbnail_url: str = ""
    alt_text: str = ""
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    local_path: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "url": self.url,
            "thumbnail_url": self.thumbnail_url,
            "alt_text": self.alt_text,
            "width": self.width,
            "height": self.height,
            "duration_ms": self.duration_ms,
            "local_path": self.local_path,
            "raw": dict(self.raw),
        }


@dataclass
class MediaCaptureData:
    """Local artifact captured from media attached to a post."""

    kind: str
    path: str
    target_post_id: str
    source_url: str = ""
    thumbnail_url: str = ""
    alt_text: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "target_post_id": self.target_post_id,
            "source_url": self.source_url,
            "thumbnail_url": self.thumbnail_url,
            "alt_text": self.alt_text,
            "raw": dict(self.raw),
        }


@dataclass
class ObservedPostData:
    """Normalized post payload returned by read/search operations."""

    platform_post_id: str
    author: str
    text: str
    raw: dict[str, Any]

    @property
    def metrics(self) -> dict[str, Any]:
        value = self.raw.get("metrics")
        return value if isinstance(value, dict) else {}

    @property
    def author_limited(self) -> bool:
        return bool(self.raw.get("author_limited"))

    @property
    def reply_limited(self) -> bool:
        return bool(self.raw.get("reply_limited"))

    @property
    def author_limit_notice(self) -> str:
        return str(self.raw.get("author_limit_notice") or "")

    @property
    def author_handle(self) -> str:
        return str(self.raw.get("author_handle") or self.author or "")

    @property
    def author_display_name(self) -> str:
        return str(self.raw.get("author_display_name") or "")

    @property
    def author_id(self) -> str:
        return str(self.raw.get("author_id") or "")

    @property
    def created_at(self) -> str:
        return str(self.raw.get("created_at") or "")

    @property
    def url(self) -> str:
        return str(self.raw.get("url") or "")

    @property
    def media(self) -> list[dict[str, Any]]:
        value = self.raw.get("media")
        return value if isinstance(value, list) else []

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform_post_id": self.platform_post_id,
            "author": self.author,
            "text": self.text,
            "raw": dict(self.raw),
        }


@dataclass
class ObservedNotificationData:
    """Normalized notification payload returned by notification reads."""

    notification_id: str
    notification_type: str
    actor: str
    text: str
    raw: dict[str, Any]

    @property
    def platform_post_id(self) -> str:
        value = self.raw.get("post_id") or self.raw.get("platform_post_id")
        return str(value or "")

    @property
    def unread(self) -> bool:
        return bool(self.raw.get("unread"))

    @property
    def author_limited(self) -> bool:
        return bool(self.raw.get("author_limited"))

    @property
    def reply_limited(self) -> bool:
        return bool(self.raw.get("reply_limited"))

    @property
    def author_limit_notice(self) -> str:
        return str(self.raw.get("author_limit_notice") or "")

    @property
    def actor_handle(self) -> str:
        return str(self.raw.get("actor_handle") or self.actor or "")

    @property
    def actor_display_name(self) -> str:
        return str(self.raw.get("actor_display_name") or "")

    @property
    def actor_id(self) -> str:
        return str(self.raw.get("actor_id") or "")

    @property
    def created_at(self) -> str:
        return str(self.raw.get("created_at") or "")

    @property
    def url(self) -> str:
        return str(self.raw.get("url") or "")

    @property
    def metrics(self) -> dict[str, Any]:
        value = self.raw.get("metrics")
        return value if isinstance(value, dict) else {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "notification_id": self.notification_id,
            "notification_type": self.notification_type,
            "actor": self.actor,
            "text": self.text,
            "raw": dict(self.raw),
        }


@dataclass
class AccountStats:
    """Normalized public account/profile stats captured from an X profile surface."""

    handle: str
    display_name: str = ""
    profile_url: str = ""
    followers: int = 0
    following: int = 0
    posts: int = 0
    likes: int = 0
    media: int = 0
    verified: bool | None = None
    bio: str = ""
    location: str = ""
    joined_at: str = ""
    captured_at: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "display_name": self.display_name,
            "profile_url": self.profile_url,
            "followers": self.followers,
            "following": self.following,
            "posts": self.posts,
            "likes": self.likes,
            "media": self.media,
            "verified": self.verified,
            "bio": self.bio,
            "location": self.location,
            "joined_at": self.joined_at,
            "captured_at": self.captured_at,
            "raw": dict(self.raw),
        }


@dataclass
class ActionResult:
    """Structured result for browser actions that can fail for UI-specific reasons."""

    ok: bool
    action: str
    target_post_id: str = ""
    created_post_id: str = ""
    target_author: str = ""
    failure_reason: str = ""
    failure_stage: str = "unknown"
    attempts: int = 0
    target_url: str = ""
    current_url: str = ""
    current_state: str = ""
    active_home_tab: str = ""
    composer_opened: bool = False
    submit_clicked: bool = False
    confirmation_observed: bool = False
    media_paths: list[str] = field(default_factory=list)
    media_attached: bool = False
    restriction_state: dict[str, Any] = field(default_factory=dict)
    diagnostic: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "action": self.action,
            "target_post_id": self.target_post_id,
            "created_post_id": self.created_post_id,
            "target_author": self.target_author,
            "failure_reason": self.failure_reason,
            "failure_stage": self.failure_stage,
            "attempts": self.attempts,
            "target_url": self.target_url,
            "current_url": self.current_url,
            "current_state": self.current_state,
            "active_home_tab": self.active_home_tab,
            "composer_opened": self.composer_opened,
            "submit_clicked": self.submit_clicked,
            "confirmation_observed": self.confirmation_observed,
            "media_paths": list(self.media_paths),
            "media_attached": self.media_attached,
            "restriction_state": dict(self.restriction_state),
            "diagnostic": dict(self.diagnostic),
            "raw": dict(self.raw),
        }


@dataclass
class ActionPreflight:
    """Non-mutating or low-mutation action feasibility probe."""

    ok: bool
    action: str
    target_post_id: str
    target_url: str = ""
    current_url: str = ""
    current_state: str = ""
    active_home_tab: str = ""
    reason: str = ""
    article_found: bool = False
    button_found: bool = False
    button_enabled: bool = False
    composer_opened: bool = False
    submit_available: bool = False
    reply_limited: bool = False
    quote_limited: bool = False
    author_limited: bool = False
    author_limit_notice: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "action": self.action,
            "target_post_id": self.target_post_id,
            "target_url": self.target_url,
            "current_url": self.current_url,
            "current_state": self.current_state,
            "active_home_tab": self.active_home_tab,
            "reason": self.reason,
            "article_found": self.article_found,
            "button_found": self.button_found,
            "button_enabled": self.button_enabled,
            "composer_opened": self.composer_opened,
            "submit_available": self.submit_available,
            "reply_limited": self.reply_limited,
            "quote_limited": self.quote_limited,
            "author_limited": self.author_limited,
            "author_limit_notice": self.author_limit_notice,
            "raw": dict(self.raw),
        }


@dataclass
class TimelineReadResult:
    """Detailed timeline read result with requested and observed surface state."""

    posts: list[ObservedPostData]
    requested_tab: str
    active_tab: str
    source_url: str
    current_state: str
    raw_count: int
    article_count: int
    force_refreshed: bool = False
    reset_scroll: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "posts": [post.to_dict() for post in self.posts],
            "requested_tab": self.requested_tab,
            "active_tab": self.active_tab,
            "source_url": self.source_url,
            "current_state": self.current_state,
            "raw_count": self.raw_count,
            "article_count": self.article_count,
            "force_refreshed": self.force_refreshed,
            "reset_scroll": self.reset_scroll,
            "warnings": list(self.warnings),
        }


@dataclass
class MediaPreflight:
    """Validation result for local media paths before upload."""

    ok: bool
    normalized_paths: list[str] = field(default_factory=list)
    file_count: int = 0
    max_file_count: int = 4
    errors: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "normalized_paths": list(self.normalized_paths),
            "file_count": self.file_count,
            "max_file_count": self.max_file_count,
            "errors": [dict(item) for item in self.errors],
            "raw": dict(self.raw),
        }


@dataclass
class ControllerHealth:
    """Snapshot of browser/session health for long-running services."""

    browser_started: bool
    logged_in: bool
    current_url: str = ""
    current_state: str = ""
    active_home_tab: str = ""
    login_required: bool = False
    account_locked: bool = False
    rate_limited: bool = False
    blocking_modal_present: bool = False
    last_action_error: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "browser_started": self.browser_started,
            "logged_in": self.logged_in,
            "current_url": self.current_url,
            "current_state": self.current_state,
            "active_home_tab": self.active_home_tab,
            "login_required": self.login_required,
            "account_locked": self.account_locked,
            "rate_limited": self.rate_limited,
            "blocking_modal_present": self.blocking_modal_present,
            "last_action_error": dict(self.last_action_error),
            "raw": dict(self.raw),
        }


@dataclass
class LoginState:
    """Passive login/session state without forcing navigation."""

    logged_in: bool
    page_state: str
    url: str = ""
    browser_started: bool = False
    active_home_tab: str = ""
    login_required: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "logged_in": self.logged_in,
            "page_state": self.page_state,
            "url": self.url,
            "browser_started": self.browser_started,
            "active_home_tab": self.active_home_tab,
            "login_required": self.login_required,
            "raw": dict(self.raw),
        }


class SocialPlatformAdapter(ABC):
    """Base async interface for click-driven social platform automation."""

    platform: str = "base"

    @abstractmethod
    async def start(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def is_logged_in(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def open_login_page(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def current_state(self) -> dict[str, str]:
        raise NotImplementedError

    @abstractmethod
    async def login_state(self) -> LoginState:
        raise NotImplementedError

    @abstractmethod
    async def return_home(self, force_refresh: bool = False) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def settle_after_action(
        self,
        tab: str = "for_you",
        force_refresh: bool = False,
        reset_scroll: bool = False,
    ) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def profile_recent_metrics(
        self,
        username: str,
        limit: int = 40,
        source: str | None = None,
    ) -> list[dict[str, int | str | bool]]:
        raise NotImplementedError

    @abstractmethod
    async def account_stats(self, handle: str | None = None) -> AccountStats:
        """Return public account/profile-level stats for a handle or the authenticated account."""
        raise NotImplementedError

    @abstractmethod
    async def capture_post_media(
        self,
        platform_post_id: str,
        output_dir: str,
        frame_count: int = 3,
        *,
        overwrite: bool = False,
    ) -> list[MediaCaptureData]:
        raise NotImplementedError

    @abstractmethod
    async def read_timeline(self, limit: int = 20) -> list[ObservedPostData]:
        raise NotImplementedError

    @abstractmethod
    async def search_posts(self, query: str, limit: int = 10) -> list[ObservedPostData]:
        raise NotImplementedError

    @abstractmethod
    async def read_notifications(self, limit: int = 20, unread_only: bool = False) -> list[ObservedNotificationData]:
        raise NotImplementedError

    @abstractmethod
    async def post_text(self, text: str, image_paths: Any | None = None) -> str | None:
        raise NotImplementedError

    async def quote_post(
        self,
        platform_post_id: str,
        text: str = "",
        image_paths: Any | None = None,
    ) -> str | None:
        raise NotImplementedError

    @abstractmethod
    async def like_post(self, platform_post_id: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def reply_to_post(
        self,
        platform_post_id: str,
        text: str,
        image_paths: Any | None = None,
    ) -> str | None:
        raise NotImplementedError

    @abstractmethod
    async def view_post(self, platform_post_id: str, dwell_seconds: tuple[int, int] = (3, 8)) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def follow_user(self, username: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def unfollow_user(self, username: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def post_metrics(self, platform_post_id: str) -> dict[str, int]:
        raise NotImplementedError

    @abstractmethod
    async def delete_post(self, platform_post_id: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def delete_reply(self, platform_post_id: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def delete_repost(self, platform_post_id: str) -> bool:
        raise NotImplementedError

    async def delete_quote(self, platform_post_id: str) -> bool:
        """Delete an owned quote post when the implementation supports it."""
        raise NotImplementedError

    @abstractmethod
    async def delete_all_posts(self) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    async def delete_all_replies(self) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    async def delete_all_reposts(self) -> list[str]:
        raise NotImplementedError

    async def delete_all_quotes(self) -> list[str]:
        """Delete owned quote posts when the implementation supports it."""
        raise NotImplementedError

    @abstractmethod
    async def delete_all_content(self) -> dict[str, list[str]]:
        raise NotImplementedError
