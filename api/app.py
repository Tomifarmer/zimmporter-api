"""FastAPI application entry point.

Creates the :class:`FastAPI` instance, mounts route modules, initializes
the database on startup, and exposes a ``/health`` endpoint that validates
all backend components (Valkey, Celery worker, MariaDB) and triggers
configurable job retention cleanup (``JOB_RETENTION_DAYS``, default 0)
and stalled-job detection (``JOB_STALLED_TIMEOUT``, default 5 minutes).
"""

import datetime
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import jwt
import redis
import requests
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from jwt import PyJWKClient
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from api.routes.cookies import cookies_router
from api.routes.download import download_router
from api.routes.jobs import jobs_router
from api.routes.search import search_router
from api.routes.thumbnail import thumbnail_router
from db.engine import get_session, init_db
from db.models import Song
from tasks.celery_app import celery_app
from zimmporter import __version__
from zimmporter.cert import configure_ssl

logger = logging.getLogger("zimmporter.auth")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure_ssl()
    init_db()
    yield


app = FastAPI(title="Zimmporter API", version=__version__, lifespan=lifespan)
app.include_router(search_router)
app.include_router(download_router)
app.include_router(jobs_router)
app.include_router(thumbnail_router)
app.include_router(cookies_router)


# ── JWKS cache ──────────────────────────────────────────────────────────────

_jwks_client: PyJWKClient | None = None


def _get_jwks_client() -> PyJWKClient | None:
    global _jwks_client
    social_login_enabled = os.environ.get("USE_SOCIAL_LOGIN", "").lower() == "true"
    if not social_login_enabled:
        logger.warning("OIDC not enabled, skipping JWKS client setup")
        return None
    if _jwks_client is None:
        issuer = os.environ.get("OIDC_ISSUER_URL", "")
        if not issuer:
            logger.error("OIDC_ISSUER_URL is not set")
            return None
        oidc_config_url = issuer.rstrip("/") + "/.well-known/openid-configuration"
        logger.info("Fetching OIDC config from %s", oidc_config_url)
        try:
            resp = requests.get(oidc_config_url, timeout=10)
            resp.raise_for_status()
            jwks_uri = resp.json().get("jwks_uri", "")
            if not jwks_uri:
                logger.error("No jwks_uri in OIDC config response")
                return None
            logger.info("Found JWKS URI: %s", jwks_uri)
            _jwks_client = PyJWKClient(jwks_uri, cache_keys=True)
        except requests.RequestException as e:
            logger.error("Failed to fetch OIDC config from %s: %s", oidc_config_url, e)
            return None
        except Exception as e:
            logger.error("Unexpected error setting up JWKS client: %s", e)
            return None
    return _jwks_client


class AuthMiddleware(BaseHTTPMiddleware):
    """Middleware that enforces authentication on all routes except ``/health``.

    Supports three independent authentication methods, each controlled by
    its own environment variable:

    * **API key** — ``USE_SIMPLE_AUTH=true`` + ``API_KEY``, header ``X-API-Key``.
    * **OIDC Bearer token** — ``USE_SOCIAL_LOGIN=true`` enables OIDC (Google) +
      bearer token validation; ``OIDC_ISSUER_URL`` + ``OIDC_CLIENT_ID`` control
      the JWKS validation, header ``Authorization: Bearer <JWT>``.
    * **GitHub Bearer token** — ``GITHUB_CLIENT_ID`` must be set; validates
      the token against the GitHub API.

    When a Bearer token is provided, OIDC JWKS validation is tried first,
    then GitHub API validation as a fallback.  If both are enabled,
    **any** method alone is sufficient for a request to pass.
    """

    async def dispatch(self, request: Request, call_next) -> JSONResponse:
        if request.method == "OPTIONS" or request.url.path in ("/health", "/thumbnail"):
            return await call_next(request)

        api_key_enabled = os.environ.get("USE_SIMPLE_AUTH", "").lower() == "true"
        social_login_enabled = os.environ.get("USE_SOCIAL_LOGIN", "").lower() == "true"
        github_enabled = bool(os.environ.get("GITHUB_CLIENT_ID", ""))

        if not api_key_enabled and not social_login_enabled and not github_enabled:
            return await call_next(request)

        errors: list[str] = []

        # ── API key check ───────────────────────────────────────────────
        if api_key_enabled:
            expected_key = os.environ.get("API_KEY")
            provided_key = request.headers.get("X-API-Key")
            if expected_key and provided_key and provided_key == expected_key:
                return await call_next(request)
            errors.append("Invalid or missing API key")

        # ── Bearer token check (OIDC first, then GitHub fallback) ───────
        bearer_enabled = social_login_enabled or github_enabled
        if bearer_enabled:
            auth_header = request.headers.get("Authorization", "")
            logger.info("Authorization header present: %s", bool(auth_header))
            if auth_header.startswith("Bearer "):
                token = auth_header.removeprefix("Bearer ")
                logger.info("Bearer token length: %d", len(token))

                user = None
                if social_login_enabled:
                    user = _validate_oidc_token(token)
                if user is None and github_enabled:
                    user = _validate_github_token(token)
                if user is not None:
                    request.scope["user"] = user
                    return await call_next(request)

            errors.append("Invalid or missing authentication token")

        headers = {}
        origin = request.headers.get("origin")
        if origin:
            headers["Access-Control-Allow-Origin"] = origin
            headers["Access-Control-Allow-Credentials"] = "true"
            headers["Access-Control-Allow-Methods"] = "*"
            headers["Access-Control-Allow-Headers"] = "*"
        return JSONResponse(status_code=401, content={"detail": "; ".join(errors)}, headers=headers)


def _validate_oidc_token(token: str) -> dict[str, Any] | None:
    client = _get_jwks_client()
    if client is None:
        logger.warning("JWKS client not available, skipping OIDC validation")
        return None
    issuer = os.environ.get("OIDC_ISSUER_URL", "")
    audience = os.environ.get("OIDC_CLIENT_ID", "")
    logger.info("Validating OIDC token: issuer=%s, audience=%s", issuer, audience)
    try:
        signing_key = client.get_signing_key_from_jwt(token)
        logger.info("Found signing key for token")
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"],
            audience=audience,
            issuer=issuer,
            options={"verify_exp": True},
        )
        logger.info("OIDC token valid, sub=%s, name=%s", claims.get("sub"), claims.get("name"))
        return claims
    except jwt.ExpiredSignatureError:
        logger.warning("OIDC token has expired")
        return None
    except jwt.InvalidAudienceError:
        logger.warning("OIDC token audience mismatch (expected %s)", audience)
        return None
    except jwt.InvalidIssuerError:
        logger.warning("OIDC token issuer mismatch (expected %s)", issuer)
        return None
    except jwt.PyJWTError as e:
        logger.warning("OIDC token validation failed: %s", e)
        return None


def _validate_github_token(token: str) -> dict[str, Any] | None:
    """Validate a Bearer token against the GitHub API.

    Calls ``GET https://api.github.com/user`` with the token.  On success
    returns a dict with ``sub``, ``name``, ``email``, and ``provider`` set
    to ``"github"`` so downstream code (e.g. ``_get_requested_by``) can
    use it consistently with OIDC claims.

    Returns ``None`` when ``GITHUB_CLIENT_ID`` is not set (GitHub auth
    not configured), the API returns non-200, or the response lacks a
    ``login`` field.
    """
    client_id = os.environ.get("GITHUB_CLIENT_ID", "")
    if not client_id:
        return None
    try:
        resp = requests.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.v3+json",
            },
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning("GitHub token validation failed: HTTP %d", resp.status_code)
            return None
        data = resp.json()
        login = data.get("login")
        if not login:
            logger.warning("GitHub token validation failed: no login in response")
            return None
        logger.info("GitHub token valid, user=%s", login)
        return {
            "sub": login,
            "name": data.get("name") or login,
            "email": data.get("email"),
            "provider": "github",
        }
    except requests.RequestException as e:
        logger.warning("GitHub token validation request failed: %s", e)
        return None


app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ALLOWED_ORIGINS", "*"),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(AuthMiddleware)


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
        _fail_stalled_jobs()

    return {
        "status": "degraded" if healthy_count < len(components) else "ok",
        "components": components,
        "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
    }


def _clean_old_jobs() -> None:
    """Delete jobs and songs older than the configured retention period.

    Controlled by the ``JOB_RETENTION_DAYS`` environment variable (default 0 —
    never purge).  Called automatically on every successful ``/health`` check.
    Silently ignores errors to avoid breaking the health probe.
    """
    retention_days = int(os.environ.get("JOB_RETENTION_DAYS", "0"))
    if retention_days <= 0:
        return
    try:
        with get_session() as session:
            from db.models import Job

            cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=retention_days)
            session.query(Job).filter(Job.created_at < cutoff).delete(synchronize_session=False)
            session.query(Song).filter(Song.created_at < cutoff).delete(synchronize_session=False)
    except Exception:
        pass


def _fail_stalled_jobs() -> None:
    """Mark jobs stuck in ``pending``/``running`` for > N minutes as failed.

    When a Celery worker crashes (OOM, ``SIGKILL``, node failure) the task
    never completes, leaving the DB row stuck indefinitely.  This function
    detects those stalled jobs and fails them along with any pending songs.

    Controlled by the ``JOB_STALLED_TIMEOUT`` environment variable (default
    5 minutes).  Silently ignores errors to avoid breaking the health probe.
    """
    timeout = int(os.environ.get("JOB_STALLED_TIMEOUT", "5"))
    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=timeout)
    try:
        with get_session() as session:
            from db.models import Job

            stalled = session.query(Job).filter(Job.status.in_(["pending", "running"]), Job.updated_at < cutoff).all()
            for job in stalled:
                job.status = "failed"
                job.error = "Job stalled — worker likely crashed"
                job.message = "Marked as failed due to inactivity"
                session.query(Song).filter(
                    Song.job_id == job.id,
                    Song.status.in_(["pending", "downloading"]),
                ).update({"status": "failed", "error": "Worker crashed"}, synchronize_session=False)
            session.commit()
    except Exception:
        pass
