"""Periodic S3 library index dispatcher.

Runs inside the API pod/container (no separate Celery beat) and dispatches the
``tasks.index_albums`` Celery task on a fixed interval.  The actual S3 scan
executes on a Celery worker, which is the container that has the S3 credentials
and boto3.

A Valkey lock de-duplicates the dispatch when the API runs with multiple
replicas, so exactly one process publishes the task per interval.

Environment variables (with defaults):

* ``INDEX_INTERVAL_MINUTES`` — Dispatch interval in minutes (default 30,
  minimum 1).
"""

import asyncio
import logging
import os

from redis import Redis

from tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

#: Valkey lock key preventing concurrent dispatch from multiple API replicas.
_LOCK_KEY = "zimmporter:index:dispatch:lock"

#: Lock TTL (seconds).  Long enough to cover a dispatch burst; the task itself
#: runs asynchronously on a worker so the lock only needs to gate the publish.
_LOCK_TTL_SECONDS = 300


def _interval_seconds() -> int:
    return max(int(os.environ.get("INDEX_INTERVAL_MINUTES", "30")), 1) * 60


def _dispatch_index() -> bool:
    """Dispatch ``tasks.index_albums`` once, guarded by a Valkey lock.

    Returns:
        ``True`` if this process published the task, ``False`` if another
        replica already holds the lock.
    """
    try:
        r = Redis.from_url(
            celery_app.conf.broker_url,
            db=4,
            socket_connect_timeout=2,
            socket_timeout=5,
        )
        acquired = r.set(_LOCK_KEY, "1", ex=_LOCK_TTL_SECONDS, nx=True)
    except Exception:
        logger.exception("Failed to acquire library index dispatch lock")
        acquired = True
    if not acquired:
        logger.info("Skipping library index dispatch: lock held by another replica")
        return False
    try:
        celery_app.send_task("tasks.index_albums")
        logger.info("Dispatched tasks.index_albums (every %d min)", _interval_seconds() // 60)
        return True
    except Exception:
        logger.exception("Failed to dispatch tasks.index_albums")
        return False


async def run_index_scheduler() -> None:
    """Periodically dispatch the S3 library index task until cancelled.

    Runs as an asyncio background task in the FastAPI lifespan.  Dispatches an
    initial scan shortly after startup, then every ``INDEX_INTERVAL_MINUTES``.
    """
    interval = _interval_seconds()
    await asyncio.sleep(5)
    while True:
        try:
            _dispatch_index()
        except Exception:
            logger.exception("Library index scheduler iteration failed")
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("Library index scheduler stopped")
            return
