"""FastAPI application entry point.

Creates the :class:`FastAPI` instance, mounts route modules, initializes
the database on startup, and exposes a ``/health`` endpoint that validates
all backend components (Valkey, Celery worker, MariaDB) and triggers
30-day job retention cleanup.
"""

import datetime
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

logger = logging.getLogger("zimmporter.auth")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

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

from api.routes.download import download_router
from api.routes.jobs import jobs_router
from api.routes.search import search_router
from db.engine import get_session, init_db
from db.models import Song
from tasks.celery_app import celery_app
from zimmporter import __version__
from zimmporter.cert import configure_ssl


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure_ssl()
    init_db()
    yield


app = FastAPI(title="Zimmporter API", version=__version__, lifespan=lifespan)
app.include_router(search_router)
app.include_router(download_router)
app.include_router(jobs_router)


# ── JWKS cache ──────────────────────────────────────────────────────────────

_jwks_client: PyJWKClient | None = None


def _get_jwks_client() -> PyJWKClient | None:
    global _jwks_client
    oidc_enabled = os.environ.get("USE_OIDC", "").lower() == "true"
    if not oidc_enabled:
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

    Supports two independent authentication methods, each controlled by its
    own environment variable:

    * **API key** — ``USE_SIMPLE_AUTH=true`` + ``API_KEY``, header ``X-API-Key``.
    * **OIDC Bearer token** — ``USE_OIDC=true`` + ``OIDC_ISSUER_URL`` +
      ``OIDC_CLIENT_ID``, header ``Authorization: Bearer <JWT>``.

    If both are enabled, **either** method is sufficient for a request to pass.
    """

    async def dispatch(self, request: Request, call_next) -> JSONResponse:
        if request.method == "OPTIONS" or request.url.path == "/health":
            return await call_next(request)

        api_key_enabled = os.environ.get("USE_SIMPLE_AUTH", "").lower() == "true"
        oidc_enabled = os.environ.get("USE_OIDC", "").lower() == "true"

        if not api_key_enabled and not oidc_enabled:
            return await call_next(request)

        errors: list[str] = []

        # ── API key check ───────────────────────────────────────────────
        if api_key_enabled:
            expected_key = os.environ.get("API_KEY")
            provided_key = request.headers.get("X-API-Key")
            if expected_key and provided_key and provided_key == expected_key:
                return await call_next(request)
            errors.append("Invalid or missing API key")

        # ── OIDC Bearer token check ─────────────────────────────────────
        if oidc_enabled:
            auth_header = request.headers.get("Authorization", "")
            logger.info("Authorization header present: %s", bool(auth_header))
            if auth_header.startswith("Bearer "):
                token = auth_header.removeprefix("Bearer ")
                logger.info("Bearer token length: %d, starts with eyJ (JWT?): %s", len(token), token.startswith("eyJ"))
                user = _validate_oidc_token(token)
                if user is not None:
                    request.state.user = user
                    return await call_next(request)
            errors.append("Invalid or missing OIDC token")

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
