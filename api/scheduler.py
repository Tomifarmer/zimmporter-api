"""Periodic library index dispatcher.

Runs inside the API pod/container (no separate Celery beat) and dispatches the
library index Celery task(s) on a fixed interval.  The actual scan executes on
a Celery worker: the S3 scan (``tasks.index_albums``) runs where boto3 + the S3
credentials live, and the Navidrome scan (``tasks.index_navidrome``) needs only
``requests``.

A Valkey lock de-duplicates the dispatch when the API runs with multiple
replicas, so exactly one process publishes each task per interval.

Environment variables (with defaults):

* ``INDEX_INTERVAL_MINUTES`` — Dispatch interval in minutes (default 30,
  minimum 1).
* ``INDEX_SOURCE`` — Which library sources to index: ``s3`` (default),
  ``navidrome``, or ``both``.
"""

import asyncio
import logging
import os

from redis import Redis

from tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

#: Valkey lock key preventing concurrent dispatch from multiple API replicas.
_LOCK_KEY = "zimmporter:index:dispatch:lock"

#: Valkey lock key for the Navidrome index dispatch.
_NAVIDROME_LOCK_KEY = "zimmporter:navidrome:dispatch:lock"

#: Lock TTL (seconds).  Long enough to cover a dispatch burst; the task itself
#: runs asynchronously on a worker so the lock only needs to gate the publish.
_LOCK_TTL_SECONDS = 300


def _interval_seconds() -> int:
    return max(int(os.environ.get("INDEX_INTERVAL_MINUTES", "30")), 1) * 60


def _index_sources() -> list[str]:
    """Resolve the configured index sources from ``INDEX_SOURCE``.

    Returns:
        List of task names to dispatch, e.g. ``["tasks.index_albums"]``.
    """
    source = os.environ.get("INDEX_SOURCE", "s3").strip().lower()
    tasks = {
        "s3": ["tasks.index_albums"],
        "navidrome": ["tasks.index_navidrome"],
        "both": ["tasks.index_albums", "tasks.index_navidrome"],
    }
    if source not in tasks:
        logger.warning("Unknown INDEX_SOURCE %r; falling back to 's3'", source)
        return tasks["s3"]
    return tasks[source]


def _dispatch_task(task_name: str, lock_key: str) -> bool:
    """Dispatch a Celery task once, guarded by a Valkey lock.

    Args:
        task_name: Full Celery task name (e.g. ``tasks.index_albums``).
        lock_key: Valkey key used to de-duplicate the dispatch.

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
        acquired = r.set(lock_key, "1", ex=_LOCK_TTL_SECONDS, nx=True)
    except Exception:
        logger.exception("Failed to acquire library index dispatch lock")
        acquired = True
    if not acquired:
        logger.info("Skipping %s dispatch: lock held by another replica", task_name)
        return False
    try:
        celery_app.send_task(task_name)
        logger.info("Dispatched %s (every %d min)", task_name, _interval_seconds() // 60)
        return True
    except Exception:
        logger.exception("Failed to dispatch %s", task_name)
        return False


def _dispatch_index() -> bool:
    """Dispatch the configured index tasks, each guarded by a Valkey lock.

    Returns:
        ``True`` if at least one task was published this cycle.
    """
    published = False
    for task_name in _index_sources():
        lock_key = _NAVIDROME_LOCK_KEY if "navidrome" in task_name else _LOCK_KEY
        if _dispatch_task(task_name, lock_key):
            published = True
    return published


async def run_index_scheduler() -> None:
    """Periodically dispatch the configured library index task(s) until cancelled.

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
