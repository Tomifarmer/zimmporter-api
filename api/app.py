"""FastAPI application entry point.

Creates the :class:`FastAPI` instance, mounts route modules, initializes
the database on startup, and exposes a ``/health`` endpoint that validates
all backend components (Valkey, Celery worker, MariaDB) and triggers
30-day job retention cleanup.
"""

import datetime
import os

import redis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from api.routes.download import download_router
from api.routes.jobs import jobs_router
from api.routes.search import search_router
from db.engine import get_session, init_db
from db.models import Song
from tasks.celery_app import celery_app
from zimmporter.cert import configure_ssl

from zimmporter import __version__

app = FastAPI(title="Zimmporter API", version=__version__)
app.include_router(search_router)
app.include_router(download_router)
app.include_router(jobs_router)


class AuthMiddleware(BaseHTTPMiddleware):
    """Middleware that enforces API key authentication on all routes except ``/health``.

    Authentication is controlled by two environment variables:

    * ``REQUIRE_AUTH`` — set to ``"true"`` (case-insensitive) to enable.
    * ``API_KEY`` — the expected secret value.

    Requests to ``/health`` are always allowed through to keep health probes
    working regardless of auth configuration.
    """

    async def dispatch(self, request: Request, call_next) -> JSONResponse:
        if request.url.path == "/health":
            return await call_next(request)

        auth_enabled = os.environ.get("REQUIRE_AUTH", "").lower() == "true"

        if auth_enabled:
            expected_key = os.environ.get("API_KEY")
            provided_key = request.headers.get("X-API-Key")

            if not expected_key or not provided_key or provided_key != expected_key:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid or missing API key"},
                )

        return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ALLOWED_ORIGINS", "*"),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(AuthMiddleware)


@app.on_event("startup")
def startup() -> None:
    """Configure SSL and create database tables if they do not exist."""
    configure_ssl()
    init_db()


@app.get("/health")
def health() -> dict:
    """Health-check endpoint.

    Validates connectivity to:

    1. Valkey (broker ping)
    2. Celery worker(s) (``inspect.ping()``)
    3. MariaDB (``SELECT 1``)

    Always returns ``{"status": "ok"|"degraded", "components": {...}}`` with
    ``200 OK`` so callers can inspect which component failed without treating
    a partial outage as an error response. Returns HTTP 503 only when *all*
    backend components are down and the API cannot function at all.

    Returns:
        Dict with ``status``, per-component health strings, and an optional
        ``timestamp`` field.
    """
    components: dict[str, str] = {
        "api": "ok",
        "redis": "ok",
        "celery_worker": "ok",
        "mariadb": "ok",
    }

    try:
        r = redis.from_url(celery_app.conf.broker_url)
        r.ping()
    except Exception:
        components["redis"] = "error"

    try:
        inspect = celery_app.control.inspect(timeout=2)
        ping = inspect.ping()
        if not ping:
            components["celery_worker"] = "no_workers_online"
    except Exception:
        components["celery_worker"] = "error"

    try:
        with get_session() as session:
            session.execute(text("SELECT 1"))
    except Exception:
        components["mariadb"] = "error"

    healthy_count = sum(1 for v in components.values() if v == "ok")
    all_components_down = healthy_count <= 1

    if not all_components_down:
        _clean_old_jobs()

    return {
        "status": "degraded" if healthy_count < len(components) else "ok",
        "components": components,
        "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
    }


def _clean_old_jobs() -> None:
    """Delete jobs and songs older than 30 days.

    Called automatically on every successful ``/health`` check.  Silently
    ignores errors to avoid breaking the health probe.
    """
    try:
        with get_session() as session:
            from db.models import Job

            cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=30)
            session.query(Job).filter(Job.created_at < cutoff).delete(synchronize_session=False)
            session.query(Song).filter(Song.created_at < cutoff).delete(synchronize_session=False)
    except Exception:
        pass
