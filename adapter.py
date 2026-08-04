"""Public XController adapter surface composed from cohesive internal concern modules."""

from __future__ import annotations

import functools
import inspect
import logging
import re

from .base import SocialPlatformAdapter
from ._adapter_read import _AdapterReadMixin
from ._adapter_runtime import ImagePath, ImagePathInput, _AdapterRuntimeMixin
from ._adapter_write import _AdapterWriteMixin

logger = logging.getLogger(__name__)

__all__ = ["ImagePath", "ImagePathInput", "XController", "XTextAdapter"]


class XTextAdapter(_AdapterWriteMixin, _AdapterReadMixin, _AdapterRuntimeMixin, SocialPlatformAdapter):
    """Click-first Playwright controller for automating common X workflows."""

    platform = "x"
    BASE_URL = "https://x.com"
    NAV_RETRIES = 3
    POST_ID_RE = re.compile(r"([0-9]+)")
    STATUS_URL_RE = re.compile(r"/status/([0-9]+)")
    PROFILE_URL_RE = re.compile(r"https?://(?:www\.)?x\.com/([^/?#]+)$")
    HANDLE_TEXT_RE = re.compile(r"@([A-Za-z0-9_]{1,15})")
    ACCOUNT_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
    PROFILE_TITLE_RE = re.compile(r"^(.*?)\s+\(@([A-Za-z0-9_]{1,15})\)")
    PROGRAMMER_ERROR_TYPES = (AssertionError, AttributeError, KeyError, TypeError)


def _serialize_public_async_methods(cls: type[XTextAdapter]) -> None:
    """Apply the per-instance operation lock to the complete async public API."""
    def wrap(method):
        @functools.wraps(method)
        async def serialized(self, *args, **kwargs):
            async with self._serialized_operation():
                return await method(self, *args, **kwargs)

        serialized.__xcontroller_serialized__ = True
        return serialized

    for name, method in inspect.getmembers(cls, predicate=inspect.iscoroutinefunction):
        if name.startswith("_") or getattr(method, "__xcontroller_serialized__", False):
            continue
        setattr(cls, name, wrap(method))


_serialize_public_async_methods(XTextAdapter)


XController = XTextAdapter
