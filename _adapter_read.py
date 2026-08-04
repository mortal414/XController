"""Timeline, notification, account, and diagnostics read flows for XTextAdapter."""

from __future__ import annotations

import logging
import contextlib
import hashlib
import json
import os
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import quote_plus, urlparse

from . import _ui_selectors as ui
from ._adapter_runtime import ImagePath, ImagePathInput
from .base import (
    AccountStats,
    ActionPreflight,
    ControllerHealth,
    MediaCaptureData,
    MediaPreflight,
    ObservedNotificationData,
    ObservedPostData,
    TimelineReadResult,
)

logger = logging.getLogger(__name__)


def _require_positive_int(value: Any, name: str) -> int:
    """Normalize a public integer option while rejecting invalid ranges."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if normalized <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return normalized


class _AdapterReadMixin:
    """Read-only and preflight capabilities for timeline, notifications, and account surfaces."""

    def _parser_warning_count(self) -> int:
        return len(getattr(self, "_recent_parser_warnings", []))

    def _record_parser_warning(
        self,
        *,
        category: str,
        reason: str,
        context: str = "",
        warning_bucket: list[str] | None = None,
        raw_bucket: dict[str, Any] | None = None,
    ) -> str:
        entry = {
            "category": str(category or "parser")[:80],
            "reason": str(reason or "unknown")[:120],
            "context": str(context or "")[:120],
            "url": str(self.page.url if self.page else "")[:260],
        }
        recent = getattr(self, "_recent_parser_warnings", None)
        if not isinstance(recent, list):
            recent = []
            self._recent_parser_warnings = recent
        recent.append(entry)
        limit = max(1, int(getattr(self, "_recent_parser_warning_limit", 25)))
        if len(recent) > limit:
            del recent[:-limit]
        token = f"parser:{entry['category']}:{entry['reason']}"
        if entry["context"]:
            token = f"{token}:{entry['context']}"
        if warning_bucket is not None and token not in warning_bucket:
            warning_bucket.append(token)
        if raw_bucket is not None:
            parser_warnings = raw_bucket.get("parser_warnings")
            if not isinstance(parser_warnings, list):
                parser_warnings = []
                raw_bucket["parser_warnings"] = parser_warnings
            parser_warnings.append(dict(entry))
        return token

    def _parser_warning_tokens_since(self, start_index: int) -> list[str]:
        recent = getattr(self, "_recent_parser_warnings", [])
        if not isinstance(recent, list) or not recent:
            return []
        offset = max(0, int(start_index))
        tokens: list[str] = []
        for item in recent[offset:]:
            if not isinstance(item, dict):
                continue
            category = str(item.get("category") or "parser")[:80]
            reason = str(item.get("reason") or "unknown")[:120]
            context = str(item.get("context") or "")[:120]
            token = f"parser:{category}:{reason}"
            if context:
                token = f"{token}:{context}"
            if token not in tokens:
                tokens.append(token)
        return tokens

    async def read_timeline_detailed(
        self,
        limit: int = 20,
        tab: str = "for_you",
        force_refresh: bool = False,
        reset_scroll: bool = False,
    ) -> TimelineReadResult:
        """Read timeline posts with tab, refresh, and scroll-reset controls.

        Args:
            limit: Maximum number of posts to collect from the timeline.
            tab: Timeline tab to target (`for_you` or `following`).
            force_refresh: Whether to force home refresh before collecting posts.
            reset_scroll: Whether to move to the top of the timeline before reading.
        """
        limit = max(1, int(limit))
        requested_tab = str(tab or "for_you").strip().lower().replace("-", "_").replace(" ", "_")
        if requested_tab not in {"for_you", "following"}:
            requested_tab = "for_you"
        warnings: list[str] = []
        parser_warning_start = self._parser_warning_count()
        force_refreshed = False
        if force_refresh:
            if await self.return_home(force_refresh=True):
                force_refreshed = True
                if not await self._select_home_tab(requested_tab):
                    warnings.append("home_tab_select_failed")
            else:
                warnings.append("home_settle_failed")
        elif not await self.settle_home(requested_tab):
            warnings.append("home_settle_failed")
        if reset_scroll and self.page:
            await self._keyboard_press("Home")
            await self.human.jitter(180, 520)
        else:
            await self.human.jitter(120, 420)
        if not self.page:
            warnings.extend(self._parser_warning_tokens_since(parser_warning_start))
            return TimelineReadResult(
                posts=[],
                requested_tab=requested_tab,
                active_tab="",
                source_url="",
                current_state="not_started",
                raw_count=0,
                article_count=0,
                force_refreshed=force_refreshed,
                reset_scroll=reset_scroll,
                warnings=[*warnings, "page_not_started"],
            )

        active_tab = await self._active_home_tab()
        if active_tab and active_tab != requested_tab:
            warnings.append(f"active_tab_mismatch:{active_tab}")
        article_count = await self._count_locator(self.page.locator("article"))
        first_pass = await self._collect_posts_from_current_page(
            limit=limit,
            scroll_rounds=max(4, (limit // 4) + 6),
            max_scan=max(limit * 3, 40),
            stagnation_limit=3,
            allow_backtrack=True,
        )
        if first_pass:
            warnings.extend(self._parser_warning_tokens_since(parser_warning_start))
            return TimelineReadResult(
                posts=first_pass,
                requested_tab=requested_tab,
                active_tab=active_tab,
                source_url=self.page.url or "",
                current_state=await self._current_state_name(),
                raw_count=len(first_pass),
                article_count=article_count,
                force_refreshed=force_refreshed,
                reset_scroll=reset_scroll,
                warnings=warnings,
            )

        # Retry once after forcing home navigation; timeline occasionally renders late.
        await self._goto(f"{self.BASE_URL}/home")
        await self.human.jitter(220, 620)
        await self._select_home_tab(requested_tab)
        posts = await self._collect_posts_from_current_page(
            limit=limit,
            scroll_rounds=max(5, (limit // 4) + 7),
            max_scan=max(limit * 3, 45),
            stagnation_limit=3,
            allow_backtrack=True,
        )
        article_count = await self._count_locator(self.page.locator("article")) if self.page else 0
        active_tab = await self._active_home_tab()
        if not posts:
            warnings.append("timeline_empty_after_retry")
        warnings.extend(self._parser_warning_tokens_since(parser_warning_start))
        return TimelineReadResult(
            posts=posts,
            requested_tab=requested_tab,
            active_tab=active_tab,
            source_url=self.page.url if self.page else "",
            current_state=await self._current_state_name(),
            raw_count=len(posts),
            article_count=article_count,
            force_refreshed=force_refreshed,
            reset_scroll=reset_scroll,
            warnings=warnings,
        )

    async def read_timeline(self, limit: int = 20) -> list[ObservedPostData]:
        """Read posts from the default home timeline.

        Args:
            limit: Maximum number of posts to return.
        """
        result = await self.read_timeline_detailed(limit=limit)
        return result.posts

    async def read_following_timeline(self, limit: int = 20) -> list[ObservedPostData]:
        """Read posts from the Following timeline tab.

        Args:
            limit: Maximum number of posts to return.
        """
        result = await self.read_timeline_detailed(limit=limit, tab="following")
        return result.posts

    async def read_visible_posts(self, limit: int = 20) -> list[ObservedPostData]:
        """Read data from the active X surface using the `read_visible_posts` flow."""
        limit = max(1, int(limit))
        if not self.page:
            return []
        return await self._collect_posts_from_current_page(
            limit=limit,
            scroll_rounds=max(2, (limit // 5) + 2),
            max_scan=max(limit * 3, 30),
            stagnation_limit=2,
            allow_backtrack=False,
        )

    async def _notification_article_unread(self, article: Any) -> bool:
        if not article:
            return False
        for selector in ui.NOTIFICATION_UNREAD_SELECTORS:
            with contextlib.suppress(Exception):
                marker = article.locator(selector).first
                if await self._count_locator(marker):
                    return True
        with contextlib.suppress(Exception):
            aria = (await self._get_attribute(article, "aria-label")) or ""
            if re.search(r"\bunread\b", aria, flags=re.IGNORECASE):
                return True
        return False

    async def _extract_notification_actor_handle(self, article: Any) -> str:
        if not article:
            return ""
        links = article.locator('a[href^="/"]')
        with contextlib.suppress(Exception):
            for idx in range(min(await self._count_locator(links), 12)):
                href = (await self._get_attribute(links.nth(idx), "href")) or ""
                handle = self._extract_profile_handle_from_href(href)
                if handle:
                    return handle
        with contextlib.suppress(Exception):
            return self._extract_handle_from_text(await self._inner_text(article, timeout_ms=1200))
        return ""

    def _classify_notification_type(self, text: str, social_context: str = "") -> str:
        raw = f"{social_context}\n{text}".lower()
        if "followed you" in raw:
            return "follow"
        if "mentioned you" in raw or "mention" in raw:
            return "mention"
        if "replied" in raw or "replying to" in raw:
            return "reply"
        if "quoted" in raw:
            return "quote"
        if "reposted" in raw or "retweeted" in raw:
            return "repost"
        if "liked" in raw:
            return "like"
        return "notification"

    def _notification_id(
        self,
        post_id: str,
        actor: str,
        notification_type: str,
        created_at: datetime | None,
        text: str,
    ) -> str:
        if post_id:
            return f"{notification_type}:{post_id}:{actor or '-'}"
        fingerprint = "|".join(
            [
                actor,
                notification_type,
                created_at.isoformat() if created_at else "",
                text[:600],
            ]
        )
        digest = hashlib.sha1(fingerprint.encode("utf-8", errors="ignore"), usedforsecurity=False).hexdigest()[:16]
        return f"{notification_type}:{digest}"

    async def _extract_notification_from_article(self, article: Any) -> ObservedNotificationData | None:
        if not article:
            return None

        status_urls = await self._extract_article_status_urls(article)
        url = await self._extract_article_status_url(article)
        post_id = await self._article_primary_post_id(article)
        status_post_ids = self._extract_status_ids_from_urls(status_urls)
        related_post_ids = [item for item in status_post_ids if item != post_id]
        text = (await self._extract_article_text(article))[:4000]
        body = text
        with contextlib.suppress(Exception):
            body = (await self._inner_text(article, timeout_ms=1200)).strip()[:4000]
        social_context = await self._extract_article_social_context(article)
        actor = await self._extract_notification_actor_handle(article)
        if not actor:
            actor = await self._extract_article_author_handle(article)
        actor_display_name = await self._extract_article_author_display_name(article)
        created_at = await self._extract_article_timestamp(article)
        unread = await self._notification_article_unread(article)
        metrics = await self._extract_article_metrics(article)
        media = await self._extract_article_media(article)
        limit_state = self._extract_article_author_limit_state(body, social_context)
        notification_type = self._classify_notification_type(body or text, social_context)
        notification_id = self._notification_id(post_id, actor, notification_type, created_at, body or text)

        return ObservedNotificationData(
            notification_id=notification_id,
            notification_type=notification_type,
            actor=actor,
            text=text or body,
            raw={
                "post_id": post_id,
                "platform_post_id": post_id,
                "url": url,
                "status_urls": status_urls,
                "status_post_ids": status_post_ids,
                "related_post_ids": related_post_ids,
                "actor_handle": actor,
                "actor_display_name": actor_display_name,
                "created_at": created_at.isoformat() if created_at else None,
                "unread": unread,
                "social_context": social_context,
                "body": body,
                "media": media,
                **limit_state,
                **metrics,
                "metrics": metrics,
            },
        )

    async def _collect_notifications_from_current_page(
        self,
        limit: int,
        unread_only: bool = False,
        scroll_rounds: int = 6,
        max_scan: int = 80,
        stagnation_limit: int = 2,
    ) -> list[ObservedNotificationData]:
        if not self.page:
            return []

        notifications: list[ObservedNotificationData] = []
        seen_ids: set[str] = set()
        stagnant_rounds = 0
        for round_idx in range(max(1, int(scroll_rounds))):
            articles = self.page.locator('article[data-testid="tweet"], article')
            total = await self._count_locator(articles)
            new_items = 0
            for idx in range(min(total, max(1, int(max_scan)))):
                notification = await self._extract_notification_from_article(articles.nth(idx))
                if not notification:
                    continue
                if notification.notification_id in seen_ids:
                    continue
                seen_ids.add(notification.notification_id)
                new_items += 1
                if unread_only and not notification.unread:
                    continue
                notifications.append(notification)
                if len(notifications) >= limit:
                    return notifications

            if round_idx < scroll_rounds - 1:
                if new_items == 0:
                    stagnant_rounds += 1
                else:
                    stagnant_rounds = 0
                if stagnant_rounds >= stagnation_limit:
                    break
                await self._random_scroll(random.randint(420, 1180))
                await self.human.jitter(260, 720)

        return notifications

    async def _wait_for_notification_articles(self, attempts: int = 4) -> int:
        if not self.page:
            return 0
        article_count = 0
        for attempt in range(max(1, int(attempts))):
            articles = self.page.locator('article[data-testid="tweet"], article')
            article_count = await self._count_locator(articles)
            if article_count > 0:
                return article_count
            await self._wait_network_idle(1600 if attempt == 0 else 900)
            await self.human.jitter(220, 620)
        return article_count

    async def read_notifications(
        self,
        limit: int = 20,
        unread_only: bool = False,
    ) -> list[ObservedNotificationData]:
        """Read data from the active X surface using the `read_notifications` flow."""
        if not self.page:
            return []

        max_items = max(1, min(int(limit), 400))
        if not await self._looks_like_notifications_page():
            if not await self._open_notifications_via_click():
                with contextlib.suppress(Exception):
                    await self._goto(f"{self.BASE_URL}/notifications")
        if not await self._looks_like_notifications_page():
            return []

        await self._wait_for_notification_articles()
        notifications = await self._collect_notifications_from_current_page(
            limit=max_items,
            unread_only=unread_only,
            scroll_rounds=max(2, (max_items // 5) + 3),
            max_scan=max(max_items * 4, 40),
            stagnation_limit=2,
        )
        if notifications:
            return notifications

        opened_mentions = await self._open_notifications_mentions_tab()
        if not opened_mentions:
            with contextlib.suppress(Exception):
                await self._goto(f"{self.BASE_URL}/notifications/mentions")
            opened_mentions = await self._looks_like_notifications_page()
        if opened_mentions:
            await self._wait_for_notification_articles()
            mentions = await self._collect_notifications_from_current_page(
                limit=max_items,
                unread_only=unread_only,
                scroll_rounds=max(2, (max_items // 5) + 3),
                max_scan=max(max_items * 4, 40),
                stagnation_limit=2,
            )
            if mentions:
                return mentions
        return notifications

    def _parse_iso_datetime(self, value: str | None) -> datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        text = raw.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except Exception as exc:
            warning = self._record_parser_warning(
                category="timestamp",
                reason="iso_datetime_parse_failed",
                context="fromisoformat",
            )
            logger.debug("parse_iso_datetime_failed value=%s error=%s", raw[:120], str(exc)[:260])
            logger.debug("parse_iso_datetime_warning warning=%s", warning)
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    async def _extract_article_timestamp(self, article: Any) -> datetime | None:
        if not article:
            return None
        try:
            time_node = article.locator("time").first
            if not await self._count_locator(time_node):
                return None
            stamp = await self._get_attribute(time_node, "datetime")
            return self._parse_iso_datetime(stamp)
        except Exception as exc:
            warning = self._record_parser_warning(
                category="timestamp",
                reason="article_timestamp_extraction_failed",
                context="time_locator",
            )
            logger.debug("article_timestamp_extraction_failed error=%s", str(exc)[:260])
            logger.debug("article_timestamp_warning warning=%s", warning)
            return None

    async def read_mentions(
        self,
        account_handle: str,
        hours_back: int = 2,
        limit: int = 120,
        min_scroll_rounds: int = 8,
        max_scroll_rounds: int = 36,
    ) -> list[ObservedPostData]:
        """Read data from the active X surface using the `read_mentions` flow."""
        handle = self._normalize_account_handle(account_handle).lower()
        if not handle:
            raise ValueError("account_handle must be a valid X handle")
        requested_hours = _require_positive_int(hours_back, "hours_back")
        requested_limit = _require_positive_int(limit, "limit")
        requested_min_rounds = _require_positive_int(min_scroll_rounds, "min_scroll_rounds")
        requested_max_rounds = _require_positive_int(max_scroll_rounds, "max_scroll_rounds")
        if requested_max_rounds < requested_min_rounds:
            raise ValueError("max_scroll_rounds must be greater than or equal to min_scroll_rounds")
        if not self.page:
            return []

        if not await self._looks_like_notifications_page():
            if not await self._open_notifications_via_click():
                with contextlib.suppress(Exception):
                    await self._goto(f"{self.BASE_URL}/notifications")
        if not await self._looks_like_notifications_page():
            return []

        if not await self._open_notifications_mentions_tab():
            return []

        max_items = min(requested_limit, 400)
        min_rounds = min(requested_min_rounds, 100)
        max_rounds = min(requested_max_rounds, 140)
        if max_rounds < min_rounds:
            min_rounds = max_rounds
        cutoff = datetime.now(timezone.utc) - timedelta(hours=min(requested_hours, 72))

        mentions: list[ObservedPostData] = []
        seen_ids: set[str] = set()
        saw_older_than_cutoff = False
        consecutive_old_rounds = 0
        stagnant_rounds = 0

        for round_idx in range(max_rounds):
            articles = self.page.locator('article[data-testid="tweet"], article')
            total = await self._count_locator(articles)
            new_mentions = 0
            old_in_round = False

            for idx in range(min(total, 120)):
                item = articles.nth(idx)
                href = await self._extract_article_status_url(item)
                post_id = await self._article_primary_post_id(item)
                if not post_id or post_id in seen_ids:
                    continue

                text = (await self._extract_article_text(item))[:4000]
                if f"@{handle}" not in text.lower():
                    continue
                body = text
                with contextlib.suppress(Exception):
                    body = (await self._inner_text(item, timeout_ms=1200)).strip()[:4000]

                author = ""
                auth_locator = item.locator('div[data-testid="User-Name"] a[href^="/"]').first
                if await self._count_locator(auth_locator):
                    auth_href = (await self._get_attribute(auth_locator, "href")) or ""
                    author = auth_href.strip("/").split("/")[-1]
                if author and author.lower() == handle:
                    continue

                created_at = await self._extract_article_timestamp(item)
                if created_at and created_at <= cutoff:
                    old_in_round = True
                    seen_ids.add(post_id)
                    continue

                metrics = await self._extract_article_metrics(item)
                limit_state = self._extract_article_author_limit_state(body)
                mentions.append(
                    ObservedPostData(
                        post_id,
                        author,
                        text,
                        {
                            "url": href,
                            "mentioned_handle": handle,
                            "created_at": created_at.isoformat() if created_at else None,
                            "body": body,
                            **limit_state,
                            **metrics,
                            "metrics": metrics,
                        },
                    )
                )
                seen_ids.add(post_id)
                new_mentions += 1
                if len(mentions) >= max_items:
                    return mentions

            if old_in_round:
                saw_older_than_cutoff = True
                consecutive_old_rounds += 1
            else:
                consecutive_old_rounds = 0

            if new_mentions == 0:
                stagnant_rounds += 1
            else:
                stagnant_rounds = 0

            rounds_done = round_idx + 1
            if rounds_done >= min_rounds and saw_older_than_cutoff and consecutive_old_rounds >= 2:
                break
            if rounds_done >= min_rounds and saw_older_than_cutoff and stagnant_rounds >= 4:
                break

            if round_idx < max_rounds - 1:
                await self._random_scroll(random.randint(460, 1280))
                await self.human.jitter(260, 720)

        return mentions[:max_items]

    async def search_posts(self, query: str, limit: int = 10) -> list[ObservedPostData]:
        """Execute the `search_posts` controller operation."""
        normalized_query = str(query or "").strip()
        if not normalized_query:
            raise ValueError("query must not be empty")
        limit = _require_positive_int(limit, "limit")
        if not self.page:
            return []

        # Click-first flow: open search UI, type, and switch tabs.
        if await self._open_search_query(normalized_query):
            await self._set_search_mode("live")
            posts = await self._collect_posts_from_current_page(
                limit=limit,
                scroll_rounds=max(3, (limit // 4) + 4),
                max_scan=max(limit * 3, 35),
                stagnation_limit=2,
                allow_backtrack=False,
            )
            if posts:
                return posts
            await self._set_search_mode("top")
            posts = await self._collect_posts_from_current_page(
                limit=limit,
                scroll_rounds=max(3, (limit // 4) + 4),
                max_scan=max(limit * 3, 35),
                stagnation_limit=2,
                allow_backtrack=False,
            )
            if posts:
                return posts

        # URL fallback only if UI-driven flow fails.
        search_urls = [
            f"{self.BASE_URL}/search?q={quote_plus(normalized_query)}&f=live",
            f"{self.BASE_URL}/search?q={quote_plus(normalized_query)}&f=top",
        ]
        for idx, url in enumerate(search_urls, start=1):
            try:
                await self._goto(url)
            except Exception as exc:
                logger.warning("search_navigation_failed url=%s error=%s", url, str(exc)[:260])
                continue
            posts = await self._collect_posts_from_current_page(
                limit=limit,
                scroll_rounds=max(3, (limit // 4) + 4),
                max_scan=max(limit * 3, 35),
                stagnation_limit=2,
                allow_backtrack=False,
            )
            if posts:
                return posts
            logger.info("search_empty_result query=%s mode=%s", normalized_query, "live" if idx == 1 else "top")
        return []

    def _parse_count_token(self, raw: str) -> int:
        token = (raw or "").strip().lower().replace(",", "").replace(" ", "")
        if not token:
            return 0
        mult = 1
        if token.endswith("k"):
            mult = 1_000
            token = token[:-1]
        elif token.endswith("m"):
            mult = 1_000_000
            token = token[:-1]
        elif token.endswith("b"):
            mult = 1_000_000_000
            token = token[:-1]
        try:
            return int(float(token) * mult)
        except Exception as exc:
            warning = self._record_parser_warning(
                category="metrics",
                reason="count_token_parse_failed",
                context="numeric_token",
            )
            logger.debug("parse_count_token_failed token=%s error=%s", token[:80], str(exc)[:260])
            logger.debug("parse_count_token_warning warning=%s", warning)
            return 0

    def _extract_metric_token(self, text: str, pattern: str) -> int:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            return 0
        return self._parse_count_token(match.group(1))

    def _zero_metrics(self) -> dict[str, int]:
        return {"views": 0, "likes": 0, "replies": 0, "comments": 0, "reposts": 0, "follows": 0}

    def _extract_metrics_from_text(self, text: str) -> dict[str, int]:
        metrics = self._zero_metrics()
        raw = str(text or "")
        metrics["views"] = self._extract_metric_token(raw, r"([\d.,]+[kmb]?)\s+views?\b")
        metrics["likes"] = self._extract_metric_token(raw, r"([\d.,]+[kmb]?)\s+likes?\b")
        metrics["replies"] = self._extract_metric_token(raw, r"([\d.,]+[kmb]?)\s+repl(?:y|ies)\b")
        metrics["comments"] = metrics["replies"]
        metrics["reposts"] = self._extract_metric_token(raw, r"([\d.,]+[kmb]?)\s+(?:reposts?|retweets?)\b")
        return metrics

    def _line_metric_label(self, line: str) -> str | None:
        normalized = re.sub(r"[^a-z]", "", str(line or "").lower())
        return {
            "reply": "replies",
            "replies": "replies",
            "comment": "comments",
            "comments": "comments",
            "repost": "reposts",
            "reposts": "reposts",
            "retweet": "reposts",
            "retweets": "reposts",
            "like": "likes",
            "likes": "likes",
            "view": "views",
            "views": "views",
        }.get(normalized)

    def _article_metric_region(self, body: str, article_text: str) -> str:
        raw_body = str(body or "")
        raw_text = str(article_text or "").strip()
        if not raw_body:
            return ""
        if not raw_text:
            return raw_body[-800:]

        body_lines = [line.strip() for line in raw_body.splitlines()]
        text_lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        if not text_lines:
            return raw_body[-800:]
        lowered_text = [line.lower() for line in text_lines]
        lowered_body = [line.lower() for line in body_lines]
        window = len(lowered_text)
        for idx in range(0, max(0, len(lowered_body) - window + 1)):
            if lowered_body[idx : idx + window] == lowered_text:
                return "\n".join(body_lines[idx + window :])
        if len(raw_text) >= 12 and raw_text in raw_body:
            return raw_body.split(raw_text, 1)[1]
        return ""

    def _extract_metrics_from_article_region(self, region: str) -> dict[str, int]:
        metrics = self._extract_metrics_from_text(region)
        lines = [line.strip() for line in str(region or "").splitlines() if line.strip()]
        consumed: set[int] = set()
        for idx, line in enumerate(lines):
            if not re.fullmatch(r"\d[\d,]*(?:\.\d+)?\s?[kKmMbB]?", line):
                continue
            value = self._parse_count_token(line)
            if not 0 <= value < 5_000_000_000:
                continue
            label = self._line_metric_label(lines[idx + 1]) if idx + 1 < len(lines) else None
            label_idx = idx + 1 if label else None
            if not label and idx > 0:
                label = self._line_metric_label(lines[idx - 1])
                label_idx = idx - 1 if label else None
            if not label:
                continue
            key = "replies" if label == "comments" else label
            metrics[key] = max(metrics.get(key, 0), value)
            if key == "replies":
                metrics["comments"] = metrics["replies"]
            consumed.add(idx)
            if label_idx is not None:
                consumed.add(label_idx)

        numeric_values_reversed: list[int] = []
        for idx in range(len(lines) - 1, -1, -1):
            line = lines[idx]
            if idx in consumed or self._line_metric_label(line):
                break
            if re.fullmatch(r"\d+\s?[smhd]", line):
                break
            if not re.fullmatch(r"\d[\d,]*(?:\.\d+)?\s?[kKmMbB]?", line):
                break
            value = self._parse_count_token(line)
            if 0 <= value < 5_000_000_000:
                numeric_values_reversed.append(value)
        numeric_values = list(reversed(numeric_values_reversed))
        if len(numeric_values) >= 4:
            replies, reposts, likes, views = numeric_values[-4:]
            metrics["replies"] = max(metrics["replies"], replies)
            metrics["comments"] = metrics["replies"]
            metrics["reposts"] = max(metrics["reposts"], reposts)
            metrics["likes"] = max(metrics["likes"], likes)
            metrics["views"] = max(metrics["views"], views)
        elif len(numeric_values) == 3:
            reposts, likes, views = numeric_values[-3:]
            metrics["reposts"] = max(metrics["reposts"], reposts)
            metrics["likes"] = max(metrics["likes"], likes)
            metrics["views"] = max(metrics["views"], views)
        return metrics

    async def _extract_article_metrics(self, article: Any) -> dict[str, int]:
        metrics = self._zero_metrics()
        if not article:
            return metrics
        try:
            body = await self._inner_text(article)
            article_text = await self._extract_article_text(article)
            metric_region = self._article_metric_region(body, article_text)
            parsed = self._extract_metrics_from_article_region(metric_region)
            for key in metrics:
                metrics[key] = max(metrics[key], int(parsed.get(key) or 0))
        except Exception as exc:
            warning = self._record_parser_warning(
                category="metrics",
                reason="article_metrics_text_failed",
                context="inner_text",
            )
            logger.debug("extract_article_metrics_text_failed error=%s", str(exc)[:260])
            logger.debug("extract_article_metrics_warning warning=%s", warning)

        # Pull aria-label text from known metric buttons as a fallback.
        selector_map = {
            "replies": 'button[data-testid="reply"], [data-testid="reply"]',
            "reposts": 'button[data-testid="retweet"], button[data-testid="unretweet"], [data-testid="retweet"], [data-testid="unretweet"]',
            "likes": 'button[data-testid="like"], button[data-testid="unlike"], [data-testid="like"], [data-testid="unlike"]',
            "views": 'a[href$="/analytics"], a[aria-label*="View post analytics"], a[aria-label*="Views"], [aria-label*="Views"]',
        }
        for key, selector in selector_map.items():
            try:
                locators = article.locator(selector)
                total = await self._count_locator(locators)
                if not total:
                    continue
                for idx in range(min(total, 12)):
                    locator = locators.nth(idx)
                    aria = (await self._get_attribute(locator, "aria-label")) or ""
                    if not aria:
                        aria = await self._inner_text(locator)
                    if not aria:
                        continue
                    token = self._extract_metric_token(aria, r"([\d.,]+[kmb]?)")
                    if token > 0:
                        metrics[key] = max(metrics[key], token)
            except Exception as exc:
                warning = self._record_parser_warning(
                    category="metrics",
                    reason="article_metrics_button_failed",
                    context=key,
                )
                logger.debug(
                    "extract_article_metrics_button_failed key=%s selector=%s error=%s",
                    key,
                    selector,
                    str(exc)[:260],
                )
                logger.debug("extract_article_metrics_button_warning warning=%s", warning)
                continue
        metrics["comments"] = metrics["replies"]
        return metrics

    def _extract_labeled_count(self, text: str, labels: Sequence[str]) -> tuple[int, str] | None:
        compact = re.sub(r"\s+", " ", str(text or "")).strip()
        if not compact:
            return None
        token = r"(\d[\d,]*(?:\.\d+)?\s?[kKmMbB]?)"
        for label in labels:
            escaped = re.escape(label)
            for pattern in (
                rf"{token}\s+{escaped}\b",
                rf"\b{escaped}\b\s+{token}",
            ):
                match = re.search(pattern, compact, flags=re.IGNORECASE)
                if match:
                    return self._parse_count_token(match.group(1)), match.group(0)
        return None

    def _extract_first_count(self, text: str) -> tuple[int, str] | None:
        compact = re.sub(r"\s+", " ", str(text or "")).strip()
        match = re.search(r"\b(\d[\d,]*(?:\.\d+)?\s?[kKmMbB]?)\b", compact)
        if not match:
            return None
        return self._parse_count_token(match.group(1)), match.group(1)

    def _link_path_matches(self, href: str, endings: Sequence[str]) -> bool:
        try:
            path = urlparse(str(href or "")).path.rstrip("/").lower()
        except Exception as exc:
            logger.debug("link_path_match_failed href=%s error=%s", str(href)[:200], str(exc)[:260])
            return False
        return any(path.endswith(f"/{ending.strip('/').lower()}") for ending in endings)

    def _extract_account_count(
        self,
        payload: dict[str, Any],
        *,
        labels: Sequence[str],
        link_endings: Sequence[str] = (),
        allow_text_fallback: bool = True,
    ) -> tuple[int, str] | None:
        links = payload.get("links")
        if link_endings and isinstance(links, list):
            for item in links:
                if not isinstance(item, dict):
                    continue
                href = str(item.get("href") or "")
                if not self._link_path_matches(href, link_endings):
                    continue
                text = " ".join(
                    str(item.get(key) or "")
                    for key in ("text", "aria_label", "title")
                    if str(item.get(key) or "").strip()
                )
                found = self._extract_labeled_count(text, labels) or self._extract_first_count(text)
                if found is not None:
                    return found[0], f"link:{href or '-'}:{found[1]}"

        if not allow_text_fallback:
            return None

        text_candidates = [
            *[str(value or "") for value in payload.get("user_name_blocks", []) if isinstance(value, str)],
            str(payload.get("profile_text") or ""),
            str(payload.get("meta_description") or ""),
            str(payload.get("title") or ""),
        ]
        for text in text_candidates:
            found = self._extract_labeled_count(text, labels)
            if found is not None:
                return found[0], f"text:{found[1]}"
        return None

    def _profile_title_identity(self, title: str) -> tuple[str, str]:
        match = self.PROFILE_TITLE_RE.search(str(title or "").strip())
        if not match:
            return "", ""
        return match.group(1).strip(), self._normalize_account_handle(match.group(2))

    def _profile_identity_from_payload(self, requested_handle: str, payload: dict[str, Any]) -> tuple[str, str]:
        title_display, title_handle = self._profile_title_identity(str(payload.get("title") or ""))
        url_handle = self._profile_handle_from_url(str(payload.get("current_url") or ""))
        resolved_handle = title_handle or url_handle or requested_handle
        display_name = ""
        blocks = payload.get("user_name_blocks")
        if isinstance(blocks, list):
            for block in blocks:
                lines = [line.strip() for line in str(block or "").splitlines() if line.strip()]
                if not lines:
                    continue
                handle_index = -1
                for idx, line in enumerate(lines):
                    if self._normalize_account_handle(line).lower() == resolved_handle.lower():
                        handle_index = idx
                        break
                if handle_index > 0:
                    display_name = lines[handle_index - 1]
                    break
                for line in lines:
                    lowered = line.lower()
                    if line.startswith("@") or lowered.endswith(" posts") or lowered in {"posts", "replies", "media", "likes"}:
                        continue
                    if self._extract_labeled_count(line, ("post", "posts", "follower", "followers", "following")):
                        continue
                    display_name = line
                    break
                if display_name:
                    break
        if not display_name:
            display_name = title_display
        return resolved_handle, display_name

    def _account_stats_from_payload(
        self,
        requested_handle: str,
        payload: dict[str, Any],
        captured_at: str,
        raw_base: dict[str, Any] | None = None,
    ) -> AccountStats:
        raw: dict[str, Any] = dict(raw_base or {})
        warnings = list(raw.get("warnings") or [])
        raw["warnings"] = warnings
        raw["dom"] = payload
        raw["current_url"] = str(payload.get("current_url") or raw.get("current_url") or "")

        handle, display_name = self._profile_identity_from_payload(requested_handle, payload)
        if not handle:
            handle = requested_handle
            warnings.append("profile_handle_unavailable")
        profile_url = f"{self.BASE_URL}/{handle}" if handle else str(payload.get("current_url") or "")

        count_specs = {
            "followers": {"labels": ("followers", "follower"), "link_endings": ("followers", "verified_followers"), "allow_text_fallback": True},
            "following": {"labels": ("following",), "link_endings": ("following",), "allow_text_fallback": True},
            "posts": {"labels": ("posts", "post"), "link_endings": (), "allow_text_fallback": True},
            "likes": {"labels": ("likes", "like"), "link_endings": ("likes",), "allow_text_fallback": False},
            "media": {"labels": ("media",), "link_endings": ("media",), "allow_text_fallback": False},
        }
        counts: dict[str, int] = {}
        count_sources: dict[str, str] = {}
        for key, spec in count_specs.items():
            found = self._extract_account_count(
                payload,
                labels=spec["labels"],  # type: ignore[arg-type]
                link_endings=spec["link_endings"],  # type: ignore[arg-type]
                allow_text_fallback=bool(spec["allow_text_fallback"]),
            )
            if found is None:
                counts[key] = 0
                warnings.append(f"{key}_count_unavailable")
            else:
                counts[key], count_sources[key] = found
        raw["count_sources"] = count_sources

        verified_value = payload.get("verified")
        verified = bool(verified_value) if verified_value is not None else None
        if verified is None:
            warnings.append("verified_state_unavailable")

        return AccountStats(
            handle=handle,
            display_name=display_name,
            profile_url=profile_url,
            followers=counts["followers"],
            following=counts["following"],
            posts=counts["posts"],
            likes=counts["likes"],
            media=counts["media"],
            verified=verified,
            bio=str(payload.get("bio") or ""),
            location=str(payload.get("location") or ""),
            joined_at=str(payload.get("joined_at") or ""),
            captured_at=captured_at,
            raw=raw,
        )

    def _account_stats_dom_script(self) -> str:
        return """() => {
            const compact = value => String(value || '').replace(/\\s+/g, ' ').trim();
            const visibleText = node => {
                if (!node) return '';
                const style = window.getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                if (style && (style.display === 'none' || style.visibility === 'hidden')) return '';
                if (rect && rect.width === 0 && rect.height === 0) return '';
                return compact(node.innerText || node.textContent || '');
            };
            const root = document.querySelector('main [data-testid="primaryColumn"]') || document.querySelector('main') || document.body;
            const firstText = selectors => {
                for (const selector of selectors) {
                    const node = root.querySelector(selector) || document.querySelector(selector);
                    const text = visibleText(node);
                    if (text) return text;
                }
                return '';
            };
            const links = Array.from(root.querySelectorAll('a[href]')).slice(0, 240).map(link => ({
                href: link.href || link.getAttribute('href') || '',
                text: visibleText(link),
                aria_label: link.getAttribute('aria-label') || '',
                title: link.getAttribute('title') || ''
            }));
            const userNameBlocks = Array.from(root.querySelectorAll('[data-testid="UserName"]'))
                .slice(0, 12)
                .map(visibleText)
                .filter(Boolean);
            const verifiedNode = root.querySelector(
                '[data-testid="icon-verified"], svg[aria-label*="Verified"], [aria-label*="Verified account"], [aria-label*="Blue verified"]'
            );
            return {
                current_url: window.location.href,
                title: document.title || '',
                meta_description: document.querySelector('meta[name="description"]')?.getAttribute('content') || '',
                profile_text: visibleText(root).slice(0, 12000),
                user_name_blocks: userNameBlocks,
                bio: firstText(['[data-testid="UserDescription"]']),
                location: firstText(['[data-testid="UserLocation"]']),
                joined_at: firstText(['[data-testid="UserJoinDate"]']),
                verified: Boolean(verifiedNode),
                links
            };
        }"""

    async def _profile_payload_from_page(self, page: Any | None) -> dict[str, Any]:
        value = await self._page_evaluate(page, self._account_stats_dom_script())
        return value if isinstance(value, dict) else {}

    def _account_stats_error(
        self,
        handle: str,
        captured_at: str,
        reason: str,
        raw: dict[str, Any] | None = None,
    ) -> AccountStats:
        details = dict(raw or {})
        warnings = list(details.get("warnings") or [])
        warnings.append(reason)
        details["warnings"] = warnings
        details["error"] = reason
        return AccountStats(handle=handle, profile_url=f"{self.BASE_URL}/{handle}" if handle else "", captured_at=captured_at, raw=details)

    async def account_stats(self, handle: str | None = None) -> AccountStats:
        """Return service diagnostics for `account_stats`."""
        captured_at = datetime.now(timezone.utc).isoformat()
        requested_handle = self._normalize_account_handle(handle)
        raw: dict[str, Any] = {
            "requested_handle": handle or "",
            "normalized_requested_handle": requested_handle,
            "warnings": [],
        }

        if handle is not None and not requested_handle:
            return self._account_stats_error("", captured_at, "invalid_handle", raw)

        if not self.page:
            return self._account_stats_error(requested_handle, captured_at, "page_not_started", raw)

        target_handle = requested_handle
        if not target_handle:
            detected = await self._get_authenticated_handle()
            target_handle = self._normalize_account_handle(detected)
            raw["detected_authenticated_handle"] = detected or ""
        if not target_handle:
            current_handle = self._profile_handle_from_url(await self._page_url(self.page))
            target_handle = current_handle
            if current_handle:
                raw["warnings"].append("authenticated_handle_unavailable_used_current_profile")
        if not target_handle:
            return self._account_stats_error("", captured_at, "account_handle_unavailable", raw)

        target_url = f"{self.BASE_URL}/{target_handle}"
        raw["target_url"] = target_url

        stats_page = None
        temporary_page = False
        active_page_used = False
        active_previous_url = await self._page_url(self.page)
        payload: dict[str, Any] = {}
        try:
            stats_page = await self._new_context_page()
            temporary_page = stats_page is not None
            if stats_page is None:
                raw["warnings"].append("temporary_page_unavailable_used_active_page")
                active_page_used = True
                await self._goto(target_url)
                stats_page = self.page
            else:
                await self._page_goto(stats_page, target_url)
            payload = await self._profile_payload_from_page(stats_page)
            if not payload:
                return self._account_stats_error(target_handle, captured_at, "profile_payload_unavailable", raw)
        except Exception as exc:
            if self._is_profile_in_use_error(exc):
                raise RuntimeError("profile_in_use") from exc
            if self._is_driver_connection_closed(exc):
                raise RuntimeError("playwright_driver_connection_closed") from exc
            if self._is_target_closed_error(exc):
                raise RuntimeError("target_page_or_context_closed") from exc
            raw["exception_type"] = type(exc).__name__
            raw["exception"] = str(exc)[:400]
            return self._account_stats_error(target_handle, captured_at, "profile_stats_capture_failed", raw)
        finally:
            if temporary_page:
                await self._close_context_page(stats_page)
            elif active_page_used and active_previous_url and active_previous_url != target_url and self.page:
                with contextlib.suppress(Exception):
                    await self._goto(active_previous_url)
                    raw["restored_url"] = active_previous_url

        return self._account_stats_from_payload(target_handle, payload, captured_at, raw)

    async def profile_recent_metrics(
        self,
        username: str,
        limit: int = 40,
        source: str | None = None,
    ) -> list[dict[str, int | str | bool]]:
        """Return service diagnostics for `profile_recent_metrics`."""
        handle = self._normalize_account_handle(username)
        if not handle:
            raise ValueError("username must be a valid X handle")
        if isinstance(limit, bool):
            raise ValueError("limit must be a non-negative integer")
        try:
            max_rows = int(limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("limit must be a non-negative integer") from exc
        if max_rows < 0:
            raise ValueError("limit must be a non-negative integer")
        max_rows = min(max_rows, 3000)
        if max_rows == 0:
            return []
        if not self.page:
            return []
        source_filter = str(source or "").strip().lower()
        if source_filter in {"reply", "replies"}:
            source_filter = "with_replies"
        if source_filter and source_filter not in {"posts", "with_replies"}:
            source_filter = ""
        rows: list[dict[str, int | str | bool]] = []
        seen: set[str] = set()
        endpoints = [
            (f"{self.BASE_URL}/{handle}", "posts"),
            (f"{self.BASE_URL}/{handle}/with_replies", "with_replies"),
        ]
        if source_filter:
            endpoints = [item for item in endpoints if item[1] == source_filter]
        try:
            for endpoint, source in endpoints:
                await self._goto(endpoint)
                scroll_rounds = max(2, (max_rows // 6) + 2)
                for round_idx in range(scroll_rounds):
                    articles = self.page.locator("article")
                    total = await self._count_locator(articles)
                    for idx in range(min(total, max(max_rows * 2, 50))):
                        article = articles.nth(idx)
                        status_link = article.locator('a[href*="/status/"]').first
                        if not await self._count_locator(status_link):
                            continue
                        href = (await self._get_attribute(status_link, "href")) or ""
                        post_id = self._extract_post_id(href)
                        if not post_id or post_id in seen:
                            continue
                        seen.add(post_id)
                        item = await self._classify_profile_article(article, handle)
                        text = str(item.get("text") or (await self._extract_article_text(article)))[:900]
                        metrics = await self._extract_article_metrics(article)
                        rows.append(
                            {
                                "post_id": post_id,
                                "url": str(item.get("url") or href or f"{self.BASE_URL}/i/web/status/{post_id}"),
                                "author": str(item.get("author") or ""),
                                "is_reply": bool(item.get("is_reply")),
                                "is_repost": bool(item.get("is_repost")),
                                "is_quote": bool(item.get("is_quote")),
                                "likes": int(metrics.get("likes") or 0),
                                "replies": int(metrics.get("replies") or 0),
                                "comments": int(metrics.get("comments") or 0),
                                "reposts": int(metrics.get("reposts") or 0),
                                "views": int(metrics.get("views") or 0),
                                "text": text,
                                "source": source,
                            }
                        )
                        if len(rows) >= max_rows:
                            return rows[:max_rows]
                    if round_idx < scroll_rounds - 1:
                        await self._random_scroll(random.randint(320, 1080))
                        await self.human.jitter(240, 580)
            return rows[:max_rows]
        finally:
            await self._return_home()

    async def _extract_article_text(self, article: Any) -> str:
        tweet_text = article.locator('div[data-testid="tweetText"]').first
        if await self._count_locator(tweet_text):
            with contextlib.suppress(Exception):
                return (await self._inner_text(tweet_text, timeout_ms=1200)).strip()
        with contextlib.suppress(Exception):
            return (await self._inner_text(article, timeout_ms=1200)).strip()
        return ""

    async def _article_summary(self, article: Any, *, target_post_id: str = "", thread_index: int | None = None) -> dict[str, Any]:
        url = await self._extract_article_status_url(article)
        post_id = await self._article_primary_post_id(article)
        text = (await self._extract_article_text(article))[:1200]
        body = text
        with contextlib.suppress(Exception):
            body = (await self._inner_text(article, timeout_ms=1200)).strip()[:3000]
        social_context = await self._extract_article_social_context(article)
        created_at = await self._extract_article_timestamp(article)
        metrics = await self._extract_article_metrics(article)
        limit_state = self._extract_article_author_limit_state(body, social_context)
        row = {
            "post_id": post_id,
            "url": url,
            "author": await self._extract_article_author_handle(article),
            "author_display_name": await self._extract_article_author_display_name(article),
            "text": text,
            "body": body,
            "created_at": created_at.isoformat() if created_at else None,
            "social_context": social_context,
            "metrics": metrics,
            "media": await self._extract_article_media(article),
            **limit_state,
        }
        if target_post_id:
            row["is_target"] = post_id == target_post_id
        if thread_index is not None:
            row["thread_index"] = thread_index
        return row

    async def preflight_action(
        self,
        platform_post_id: str,
        action: str = "reply",
        *,
        open_composer: bool = False,
    ) -> ActionPreflight:
        """Return service diagnostics for `preflight_action`."""
        post_id = self._normalize_post_id(platform_post_id)
        action_name = str(action or "").strip().lower()
        if action_name not in {"reply", "quote", "like"}:
            action_name = "reply"
        if not self.page:
            return ActionPreflight(False, action_name, post_id, reason="page_not_started", current_state="not_started")
        if not post_id:
            return ActionPreflight(False, action_name, "", reason="target_post_not_found", current_url=self.page.url or "")

        article = await self._find_post_article_in_context(post_id, scan_rounds=2)
        if article is None:
            with contextlib.suppress(Exception):
                await self._open_post_page(post_id)
                article = await self._find_post_article_in_context(post_id, scan_rounds=1)

        result = ActionPreflight(
            ok=False,
            action=action_name,
            target_post_id=post_id,
            target_url=f"{self.BASE_URL}/i/web/status/{post_id}",
            current_url=self.page.url or "",
            current_state=await self._current_state_name(),
            active_home_tab=await self._active_home_tab(),
            article_found=article is not None,
        )
        if article is None:
            result.reason = "target_post_not_found"
            return result

        summary = await self._article_summary(article, target_post_id=post_id)
        result.raw = {"article": summary}
        result.author_limited = bool(summary.get("author_limited"))
        result.reply_limited = bool(summary.get("reply_limited"))
        result.author_limit_notice = str(summary.get("author_limit_notice") or "")
        result.quote_limited = False

        selectors = {
            "reply": ui.COMMENT_BUTTONS,
            "quote": ui.REPOST_BUTTONS,
            "like": ui.LIKE_BUTTONS,
        }[action_name]
        button = await self._find_first_in_scope(article, selectors, timeout_ms=900, require_enabled=False)
        result.button_found = button is not None
        if button is not None:
            result.button_enabled = await self._is_button_enabled(button)
        if action_name == "reply" and result.reply_limited:
            result.reason = "reply_limited"
            return result
        if not result.button_found:
            result.reason = f"{action_name}_button_not_found"
            return result
        if not result.button_enabled:
            result.reason = "submit_disabled"
            return result
        if open_composer and action_name in {"reply", "quote"}:
            box = await (
                self._open_reply_box_in_current_context(post_id)
                if action_name == "reply"
                else self._open_quote_box_in_current_context(post_id)
            )
            result.composer_opened = box is not None
            if box is not None:
                submit = await self._find_reply_submit_button(box, timeout_ms=900)
                result.submit_available = submit is not None
            await self._dismiss_reply_ui()
            if not result.composer_opened:
                result.reason = "composer_not_opened"
                return result
        result.ok = True
        return result

    @staticmethod
    def _image_signature_matches(extension: str, header: bytes) -> bool:
        signatures = {
            ".gif": lambda value: value.startswith((b"GIF87a", b"GIF89a")),
            ".jpeg": lambda value: value.startswith(b"\xff\xd8\xff"),
            ".jpg": lambda value: value.startswith(b"\xff\xd8\xff"),
            ".png": lambda value: value.startswith(b"\x89PNG\r\n\x1a\n"),
            ".webp": lambda value: len(value) >= 12 and value[:4] == b"RIFF" and value[8:12] == b"WEBP",
        }
        validator = signatures.get(extension.lower())
        return bool(validator and validator(header))

    async def attach_images_preflight(self, image_paths: ImagePathInput | None) -> MediaPreflight:
        """Return service diagnostics for `attach_images_preflight`."""
        candidates: list[ImagePath]
        if image_paths is None:
            candidates = []
        elif isinstance(image_paths, (str, os.PathLike)):
            candidates = [image_paths]
        else:
            candidates = list(image_paths)
        errors: list[dict[str, Any]] = []
        normalized: list[str] = []
        if len(candidates) > ui.MAX_IMAGES_PER_POST:
            errors.append({"reason": "too_many_images", "max": ui.MAX_IMAGES_PER_POST, "count": len(candidates)})
        for candidate in candidates:
            path = Path(candidate).expanduser()
            if not path.is_absolute():
                path = (Path.cwd() / path).resolve()
            else:
                path = path.resolve()
            item = {"path": str(path), "exists": path.is_file(), "supported_extension": path.suffix.lower() in ui.SUPPORTED_IMAGE_EXTENSIONS}
            if not path.is_file():
                item["reason"] = "file_not_found"
                errors.append(item)
                continue
            if path.suffix.lower() not in ui.SUPPORTED_IMAGE_EXTENSIONS:
                item["reason"] = "unsupported_extension"
                errors.append(item)
                continue
            try:
                item["size_bytes"] = path.stat().st_size
                with path.open("rb") as image_file:
                    header = image_file.read(12)
            except OSError as exc:
                item["reason"] = "file_unreadable"
                item["error_type"] = type(exc).__name__
                errors.append(item)
                continue
            if not self._image_signature_matches(path.suffix.lower(), header):
                item["reason"] = "invalid_image_signature"
                errors.append(item)
                continue
            normalized.append(str(path))
        upload_input_found = False
        if self.page:
            with contextlib.suppress(Exception):
                upload_input_found = await self._find_media_input_for_composer(None, timeout_ms=300) is not None
        return MediaPreflight(
            ok=not errors,
            normalized_paths=normalized,
            file_count=len(candidates),
            max_file_count=ui.MAX_IMAGES_PER_POST,
            errors=errors,
            raw={"upload_input_found": upload_input_found},
        )

    async def debug_snapshot(
        self,
        output_dir: str | os.PathLike[str],
        article_limit: int = 12,
        *,
        overwrite: bool = False,
        include_sensitive_content: bool = False,
    ) -> dict[str, Any]:
        """Write a safe manifest, with full page artifacts only by explicit opt-in."""
        if self.page and not await self._page_is_trusted(self.page):
            raise RuntimeError("debug_snapshot_requires_trusted_x_origin")
        out_dir = Path(output_dir).expanduser()
        if not out_dir.is_absolute():
            out_dir = (Path.cwd() / out_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(out_dir, 0o700)
        html_path = out_dir / "page.html"
        screenshot_path = out_dir / "page.png"
        manifest_path = out_dir / "manifest.json"
        all_targets = (manifest_path, html_path, screenshot_path)
        symlinks = [path.name for path in all_targets if path.is_symlink()]
        if symlinks:
            raise ValueError(f"debug_snapshot_symlink_target:{','.join(symlinks)}")
        sensitive_existing = [path.name for path in (html_path, screenshot_path) if path.exists()]
        if sensitive_existing and not include_sensitive_content:
            raise FileExistsError(
                f"debug_snapshot_sensitive_artifacts_present:{','.join(sensitive_existing)}"
            )
        targets = all_targets if include_sensitive_content else (manifest_path,)
        existing = [path.name for path in targets if path.exists()]
        if existing and not overwrite:
            raise FileExistsError(f"debug_snapshot_target_exists:{','.join(existing)}")

        if include_sensitive_content:
            page_html = await self._page_content()
            html_path.write_text(page_html, encoding="utf-8")
            with contextlib.suppress(OSError):
                os.chmod(html_path, 0o600)
            try:
                await self._screenshot(str(screenshot_path))
                with contextlib.suppress(OSError):
                    os.chmod(screenshot_path, 0o600)
            except Exception as exc:
                logger.debug("debug_snapshot_screenshot_failed path=%s error=%s", str(screenshot_path), str(exc)[:260])

        articles_summary: list[dict[str, Any]] = []
        article_count = 0
        if self.page:
            articles = self.page.locator("article")
            article_count = await self._count_locator(articles)
            for idx in range(
                min(article_count, max(0, int(article_limit))) if include_sensitive_content else 0
            ):
                try:
                    articles_summary.append(await self._article_summary(articles.nth(idx), thread_index=idx))
                except Exception as exc:
                    logger.debug("debug_snapshot_article_summary_failed index=%s error=%s", idx, str(exc)[:260])

        selector_probe = {
            "reply_button": bool(self.page and await self._find_first(ui.COMMENT_BUTTONS, timeout_ms=200)),
            "quote_button": bool(self.page and await self._find_first(ui.REPOST_BUTTONS, timeout_ms=200)),
            "like_button": bool(self.page and await self._find_first(ui.LIKE_BUTTONS, timeout_ms=200)),
            "submit_button": bool(self.page and await self._find_first(ui.POST_BUTTONS, timeout_ms=200)),
            "media_input": bool(self.page and await self._find_media_input_for_composer(None, timeout_ms=200)),
        }
        login_state = await self.login_state()
        login_details = (
            login_state.to_dict()
            if include_sensitive_content
            else {
                "logged_in": login_state.logged_in,
                "page_state": login_state.page_state,
                "browser_started": login_state.browser_started,
                "login_required": login_state.login_required,
            }
        )
        recent_parser_warnings = [
            dict(item) for item in getattr(self, "_recent_parser_warnings", [])[-5:]
        ]
        if not include_sensitive_content:
            for warning in recent_parser_warnings:
                if "url" in warning:
                    warning["url"] = self._redact_url_for_log(warning.get("url"))
        manifest = {
            "artifact_security": {
                "sensitive": True,
                "classification": "browser_session_snapshot",
                "contains_page_content": include_sensitive_content,
                "sensitive_content_opted_in": include_sensitive_content,
                "share_with_care": True,
            },
            "url": self._redact_url_for_log(self.page.url if self.page else ""),
            "current_state": await self._current_state_name(),
            "login_state": "logged_in" if login_state.logged_in else "login_required" if self.page else "not_started",
            "login_details": login_details,
            "active_home_tab": await self._active_home_tab(),
            "article_count": article_count,
            "articles": articles_summary,
            "visible_dialogs": await self._visible_dialogs() if include_sensitive_content else [],
            "composer_state": {"open": await self._is_compose_state()},
            "selector_probe": selector_probe,
            "recent_parser_warnings": recent_parser_warnings,
            "screenshot_path": str(screenshot_path) if include_sensitive_content else "",
            "html_path": str(html_path) if include_sensitive_content else "",
        }
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(manifest_path, 0o600)
        return {"manifest_path": str(manifest_path), **manifest}

    async def capture_post_media(
        self,
        platform_post_id: str,
        output_dir: str | os.PathLike[str],
        frame_count: int = 3,
        *,
        overwrite: bool = False,
    ) -> list[MediaCaptureData]:
        """Return service diagnostics for `capture_post_media`."""
        target_id = self._normalize_post_id(platform_post_id)
        if not target_id or not self.page:
            return []
        if isinstance(frame_count, bool) or int(frame_count) <= 0:
            raise ValueError("frame_count must be a positive integer")
        out_dir = Path(output_dir).expanduser()
        if not out_dir.is_absolute():
            out_dir = (Path.cwd() / out_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(out_dir, 0o700)
        existing = list(out_dir.glob(f"{target_id}_*.png"))
        symlinks = [path.name for path in existing if path.is_symlink()]
        if symlinks:
            raise ValueError(f"media_capture_symlink_target:{','.join(symlinks)}")
        if existing and not overwrite:
            raise FileExistsError(
                f"media_capture_target_exists:{','.join(path.name for path in existing)}"
            )

        if not await self._open_post_page(target_id):
            return []
        article = await self._resolve_target_article(target_id)
        if article is None:
            return []
        captures = await self._capture_article_media_nodes(
            article,
            target_id,
            out_dir,
            frame_count=frame_count,
            overwrite=overwrite,
        )
        for item in captures:
            if item.path:
                with contextlib.suppress(OSError):
                    os.chmod(item.path, 0o600)
        return [item for item in captures if item.path]

    async def _visible_dialogs(self) -> list[str]:
        if not self.page or not await self._page_is_trusted(self.page):
            return []
        try:
            value = await self._evaluate(
                r"""() => Array.from(document.querySelectorAll('[role="dialog"]'))
                    .filter(node => {
                        const rect = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);
                        return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                    })
                    .map(node => String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 240))"""
            )
            return [str(item) for item in value] if isinstance(value, list) else []
        except Exception as exc:
            logger.debug("visible_dialogs_probe_failed error=%s", str(exc)[:260])
            return []

    async def _visible_system_messages(self) -> list[str]:
        """Return visible system UI text without scanning timeline article content."""
        if not self.page or not await self._page_is_trusted(self.page):
            return []
        try:
            value = await self._evaluate(
                r"""() => Array.from(document.querySelectorAll([
                        '[role="dialog"]',
                        '[role="alert"]',
                        '[role="status"]',
                        '[aria-live="assertive"]',
                        '[aria-live="polite"]',
                        '[data-testid="toast"]',
                        '[data-testid="error-detail"]'
                    ].join(',')))
                    .filter(node => {
                        if (node.closest('article')) return false;
                        const rect = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);
                        return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                    })
                    .map(node => String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 400))
                    .filter(Boolean)"""
            )
            if not isinstance(value, list):
                return []
            messages: list[str] = []
            for item in value:
                text = str(item).strip()
                if text and text not in messages:
                    messages.append(text)
            return messages
        except Exception as exc:
            logger.debug("visible_system_messages_probe_failed error=%s", str(exc)[:260])
            return []

    async def _wait_for_selector_compat(self, selector: str, timeout_ms: int) -> Any:
        """Wait for a selector through the active Playwright execution mode."""
        page = self._sync_page if self._sync_mode and self._sync_page is not None else self.page
        if page is None:
            return None
        if self._sync_mode:
            return await self._run_sync(page.wait_for_selector, selector, timeout=timeout_ms)
        return await page.wait_for_selector(selector, timeout=timeout_ms)

    async def health_check(self) -> ControllerHealth:
        """Return structured controller state from `health_check`."""
        browser_started = self.page is not None
        raw: dict[str, Any] = {
            "recent_parser_warnings": [
                dict(item) for item in getattr(self, "_recent_parser_warnings", [])[-5:]
            ]
        }
        try:
            state = await self.login_state()
        except Exception as exc:
            reason = "target_closed" if self._is_target_closed_error(exc) else "state_probe_failed"
            raw.update(
                {
                    "degraded": True,
                    "degraded_reason": reason,
                    "health_check_error": {
                        "type": type(exc).__name__,
                        "message": str(exc)[:400],
                    },
                }
            )
            return ControllerHealth(
                browser_started=browser_started,
                logged_in=False,
                current_url=await self._page_url(self.page),
                current_state="unavailable" if browser_started else "not_started",
                login_required=True,
                last_action_error=self.last_action_error.to_dict() if self.last_action_error else {},
                raw=raw,
            )

        system_messages = await self._visible_system_messages()
        system_text = " ".join(system_messages).lower()
        visible_dialogs = await self._visible_dialogs()
        raw["visible_system_messages"] = system_messages
        return ControllerHealth(
            browser_started=browser_started,
            logged_in=state.logged_in,
            current_url=state.url,
            current_state=state.page_state,
            active_home_tab=state.active_home_tab,
            login_required=state.login_required,
            account_locked=any(
                marker in system_text
                for marker in (
                    "account is locked",
                    "account has been locked",
                    "temporarily restricted",
                    "account is suspended",
                    "account suspended",
                )
            ),
            rate_limited=any(
                marker in system_text
                for marker in (
                    "rate limit",
                    "rate-limit",
                    "too many requests",
                    "try again later",
                    "reached your limit",
                )
            ),
            blocking_modal_present=bool(visible_dialogs),
            last_action_error=self.last_action_error.to_dict() if self.last_action_error else {},
            raw=raw,
        )

    async def read_post_thread_context(
        self,
        post_id: str,
        limit: int = 6,
        include_parent: bool = True,
        include_target: bool = True,
        include_replies: bool = True,
    ) -> list[ObservedPostData]:
        """Read data from the active X surface using the `read_post_thread_context` flow."""
        target_id = self._normalize_post_id(post_id)
        if not target_id or not await self._open_post_page(target_id) or not self.page:
            return []
        for _ in range(3):
            articles_probe = self.page.locator("article")
            if await self._count_locator(articles_probe):
                break
            with contextlib.suppress(Exception):
                await self._wait_for_selector_compat("article", timeout_ms=1800)
            await self.human.jitter(250, 650)
        summaries: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        target_seen = False
        max_rows = max(1, int(limit)) * 4
        for scan_attempt in range(4):
            articles = self.page.locator("article")
            total = await self._count_locator(articles)
            for idx in range(min(total, max_rows)):
                article = articles.nth(idx)
                summary = await self._article_summary(article, target_post_id=target_id, thread_index=idx)
                item_id = str(summary.get("post_id") or "")
                if not item_id or item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
                summaries.append(summary)
                if item_id == target_id:
                    target_seen = True
            if target_seen or scan_attempt >= 3:
                break
            await self._random_scroll(850)
            await self.human.jitter(220, 620)
            await self._wait_network_idle(900)
        target_summary_index = next(
            (idx for idx, summary in enumerate(summaries) if str(summary.get("post_id") or "") == target_id),
            -1,
        )
        rows: list[ObservedPostData] = []
        for summary_index, summary in enumerate(summaries):
            item_id = str(summary.get("post_id") or "")
            if not item_id:
                continue
            is_target = item_id == target_id
            if is_target:
                thread_role = "target"
            elif target_summary_index < 0:
                thread_role = "unknown"
            elif summary_index < target_summary_index:
                thread_role = "parent"
            else:
                thread_role = "reply"
            if is_target and not include_target:
                continue
            if thread_role == "parent" and not include_parent:
                continue
            if thread_role == "reply" and not include_replies:
                continue
            if thread_role == "unknown" and not (include_parent and include_replies):
                continue
            rows.append(
                ObservedPostData(
                    platform_post_id=item_id,
                    author=str(summary.get("author") or ""),
                    text=str(summary.get("text") or ""),
                    raw={**summary, "conversation_id": target_id, "thread_role": thread_role},
                )
            )
        max_limit = max(1, int(limit))
        if len(rows) > max_limit:
            target_index = next((idx for idx, row in enumerate(rows) if row.platform_post_id == target_id), -1)
            if target_index >= 0:
                parent_slots = (max_limit - 1) // 2
                start = max(0, target_index - parent_slots)
                end = min(len(rows), start + max_limit)
                start = max(0, end - max_limit)
                rows = rows[start:end]
            elif include_parent and not include_replies:
                rows = rows[-max_limit:]
            else:
                rows = rows[:max_limit]
        return rows
