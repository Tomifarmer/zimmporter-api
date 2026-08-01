"""Shared "stale cookies" flag stored in Valkey.

yt-dlp only detects rotated/invalid YouTube account cookies at download
time — the exported cookie file itself looks structurally valid (all the
expected session cookies are present).  When a worker sees yt-dlp reject
the session it records that here, and ``GET /cookies`` reads the flag so
the frontend can warn the user.  Uploading a fresh cookies file clears it.
"""

import datetime
import logging
import time

from redis import Redis

from tasks.celery_app import celery_app

logger = logging.getLogger("Zimmporter")

#: Valkey database holding the cookie-health flag (0 = broker, 1 = backend, 2 = search cache).
_DB = 3

#: Flag key.  The value is an ISO timestamp of when staleness was detected.
_KEY = "zimmporter:cookies:stale"

#: How long a staleness flag stays valid.  Re-marked on each new detection and
#: cleared on upload, so this is only a safety net for abandoned deployments.
_TTL_SECONDS = 7 * 24 * 3600

#: Minimum seconds between consecutive marks — keeps a worker from opening a
#: new Redis connection on every failed retry within a single song.
_MARK_INTERVAL = 60

_last_mark = 0.0


def _redis() -> Redis | None:
    """Return a Valkey client for the cookie-health db (``None`` on failure)."""
    try:
        return Redis.from_url(celery_app.conf.broker_url, db=_DB)
    except Exception:
        return None


def mark_stale() -> None:
    """Record that the configured cookies were rejected by YouTube."""
    global _last_mark
    now = time.monotonic()
    if now - _last_mark < _MARK_INTERVAL:
        return
    _last_mark = now

    r = _redis()
    if r is None:
        return
    try:
        r.set(_KEY, datetime.datetime.now(datetime.UTC).isoformat(), ex=_TTL_SECONDS)
    except Exception:
        logger.warning("Failed to mark cookies stale in Valkey", exc_info=True)


def is_stale() -> bool:
    """Whether a worker has recently flagged the cookies as stale."""
    r = _redis()
    if r is None:
        return False
    try:
        return bool(r.get(_KEY))
    except Exception:
        logger.warning("Failed to read cookie staleness flag from Valkey", exc_info=True)
        return False


def clear_stale() -> None:
    """Clear the staleness flag, called after a fresh cookie upload."""
    r = _redis()
    if r is None:
        return
    try:
        r.delete(_KEY)
    except Exception:
        logger.warning("Failed to clear cookie staleness flag in Valkey", exc_info=True)
