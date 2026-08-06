"""Valkey-backed store for the yt-dlp cookies file.

The cookies file (full YouTube session tokens) is uploaded through the
UI, validated by :mod:`api.routes.cookies`, and persisted here instead of
on a shared file volume.  Both the API and the Celery worker reach the
same Valkey instance, so a running worker picks up a freshly uploaded
file on its next job without a shared mount.

Contents are never exposed through the API — only metadata is.  Any
Valkey failure degrades gracefully: reads return ``None`` (no cookies)
and writes are swallowed so a flaky Valkey never breaks an upload or a
download.

Storage uses the same Valkey database as :mod:`zimmporter.cookie_health`
(0 = broker, 1 = backend, 2 = search cache, 3 = cookies).
"""

import datetime
import json
import logging

from zimmporter.redis_client import get_redis

logger = logging.getLogger("Zimmporter")

#: Valkey database holding cookie content and metadata (see module docstring).
_DB = 3

#: Key holding the raw cookie file content.
_CONTENT_KEY = "zimmporter:cookies:content"

#: Key holding ``{"modified_at": "<ISO UTC>"}`` metadata.
_META_KEY = "zimmporter:cookies:meta"


def _redis():
    """Return a Valkey client for the cookie store (``None`` on failure)."""
    try:
        return get_redis(_DB)
    except Exception:
        return None


def get_content() -> bytes | None:
    """Return the stored cookie file content, or ``None`` when unset/unreadable."""
    r = _redis()
    if r is None:
        return None
    try:
        raw = r.get(_CONTENT_KEY)
        return bytes(raw) if raw is not None else None
    except Exception:
        logger.warning("Failed to read cookies content from Valkey", exc_info=True)
        return None


def get_modified_at() -> datetime.datetime | None:
    """Return the last upload timestamp, or ``None`` when unknown."""
    r = _redis()
    if r is None:
        return None
    try:
        raw = r.get(_META_KEY)
        if raw is None:
            return None
        meta = json.loads(raw)
        return datetime.datetime.fromisoformat(meta["modified_at"])
    except Exception:
        logger.warning("Failed to read cookies metadata from Valkey", exc_info=True)
        return None


def set_content(content: bytes, modified_at: datetime.datetime) -> None:
    """Persist cookie file content and its upload timestamp.

    Args:
        content: Raw bytes of the Netscape-format cookies file.
        modified_at: UTC timestamp of the upload.
    """
    r = _redis()
    if r is None:
        return
    try:
        meta = json.dumps({"modified_at": modified_at.isoformat()})
        pipe = r.pipeline()
        pipe.set(_CONTENT_KEY, content)
        pipe.set(_META_KEY, meta)
        pipe.execute()
    except Exception:
        logger.warning("Failed to store cookies content in Valkey", exc_info=True)


def reset() -> None:
    """Delete the stored cookie content and metadata."""
    r = _redis()
    if r is None:
        return
    try:
        pipe = r.pipeline()
        pipe.delete(_CONTENT_KEY)
        pipe.delete(_META_KEY)
        pipe.execute()
    except Exception:
        logger.warning("Failed to clear cookies content in Valkey", exc_info=True)
