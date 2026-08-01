"""Celery application configuration.

Creates a Celery instance wired to a Valkey/Redis broker.  Configured
for JSON serialization, late acknowledgments, and one-at-a-time task
prefetching so that only one download job runs per worker process.

Environment variables (with defaults):

* ``CELERY_BROKER`` — Broker URL (default ``redis://localhost:6379/0``)
* ``CELERY_BACKEND`` — Result backend URL (default ``redis://localhost:6379/1``)

Although the underlying broker is Valkey, the standard ``redis://`` URL
scheme works because the ``redis`` Python client is drop-in compatible.
"""

import os

from celery import Celery
from celery.signals import worker_process_init

from zimmporter.cert import configure_ssl

configure_ssl()


@worker_process_init.connect
def _warmup_ytdlp_plugins(**kwargs) -> None:
    """Load yt-dlp plugins once, serially, before any task threads race on them.

    ``yt_dlp.YoutubeDL`` loads plugins lazily on first instantiation, guarded by
    a process-global flag that is not thread-safe.  When a download task fans out
    over a ``ThreadPoolExecutor``, several threads can construct ``YoutubeDL``
    concurrently on first use, each re-importing the POT provider plugin modules
    and tripping yt-dlp's "already registered" assertion.  Pre-loading them here
    (which runs once per forked worker process, before any task is consumed)
    removes the race.  Imported lazily so the API container (no yt-dlp) is safe.
    """
    from yt_dlp.plugins import load_all_plugins

    load_all_plugins()


broker_url = os.environ.get("CELERY_BROKER", "redis://localhost:6379/0")
backend_url = os.environ.get("CELERY_BACKEND", "redis://localhost:6379/1")

celery_app = Celery(
    "zimmporter",
    broker=broker_url,
    backend=backend_url,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    worker_redirect_stdouts=False,
    include=["tasks.download"],
)
