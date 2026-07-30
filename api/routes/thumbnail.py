"""Thumbnail proxy route — ``GET /thumbnail``.

Proxies thumbnail image requests through the backend so frontends
without direct internet access can still display YouTube Music
thumbnails.  Controlled by the ``API_PROXY_FETCH`` environment
variable (checked in the search route, not here).

Responses are cached in Valkey (db 3) to reduce upstream bandwidth
and latency.
"""

import hashlib
import logging

import requests
from fastapi import APIRouter, HTTPException, Query
from redis import Redis
from starlette.responses import Response

from tasks.celery_app import celery_app

logger = logging.getLogger("zimmporter.thumbnail")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

#: Maximum allowed thumbnail size in bytes (10 MB).
MAX_SIZE = 10 * 1024 * 1024

#: How long to cache thumbnails in Valkey (24 hours).
CACHE_TTL = 86400

thumbnail_router = APIRouter(prefix="/thumbnail", tags=["thumbnail"])

_REDIS: Redis | None = None


def _get_redis() -> Redis | None:
    global _REDIS
    if _REDIS is not None:
        try:
            _REDIS.ping()
            return _REDIS
        except Exception:
            _REDIS = None
    try:
        _REDIS = Redis.from_url(
            celery_app.conf.broker_url,
            db=3,
            socket_connect_timeout=2,
            socket_timeout=5,
            health_check_interval=30,
        )
        _REDIS.ping()
    except Exception:
        logger.debug("Valkey not available, thumbnail caching disabled")
        _REDIS = None
    return _REDIS


def _cache_key(url: str) -> str:
    return f"thumb:{hashlib.md5(url.encode(), usedforsecurity=False).hexdigest()}"


@thumbnail_router.get("")
def proxy_thumbnail(
    url: str = Query(..., min_length=1, description="CDN thumbnail URL to proxy"),
) -> Response:
    """Fetch and return a thumbnail image from the given CDN URL.

    The backend (which has outbound internet access via proxy) downloads
    the image from YouTube Music's CDN and returns it to the caller.
    Responses are cached in Valkey (db 3) for up to 24 hours.

    Args:
        url: The absolute CDN URL of the thumbnail to proxy.

    Returns:
        The raw image bytes with the upstream ``Content-Type`` preserved.

    Raises:
        HTTPException 502: If the upstream request fails.
        HTTPException 413: If the upstream response exceeds :data:`MAX_SIZE`.
    """
    redis = _get_redis()
    key = _cache_key(url)

    cache_headers = {
        "X-Cache": "HIT",
        "Cache-Control": "public, max-age=86400",
    }
    if redis is not None:
        try:
            cached = redis.hgetall(key)
            if cached:
                content_type = cached.get(b"content_type", b"image/jpeg")
                logger.info("Thumbnail cache HIT for %s (%.1f KB)", url, len(cached[b"data"]) / 1024)
                return Response(
                    content=cached[b"data"],
                    media_type=content_type.decode(),
                    headers=cache_headers,
                )
        except Exception:
            logger.warning("Failed to read thumbnail from cache for %s", url)

    logger.info("Thumbnail cache MISS for %s", url)
    try:
        upstream = requests.get(url, stream=True, timeout=15)
        upstream.raise_for_status()
    except requests.RequestException as exc:
        status = exc.response.status_code if exc.response is not None else 502
        logger.warning("Thumbnail proxy failed for %s: %s", url, exc)
        raise HTTPException(status_code=status, detail="Failed to fetch thumbnail") from exc

    content_type = upstream.headers.get("content-type", "image/jpeg")

    size = 0
    chunks: list[bytes] = []
    for chunk in upstream.iter_content(chunk_size=65536):
        size += len(chunk)
        if size > MAX_SIZE:
            logger.warning("Thumbnail %s exceeds %d bytes, rejecting", url, MAX_SIZE)
            raise HTTPException(status_code=413, detail="Thumbnail exceeds maximum allowed size")
        chunks.append(chunk)

    data = b"".join(chunks)

    if redis is not None:
        try:
            redis.hset(key, mapping={"data": data, "content_type": content_type})
            redis.expire(key, CACHE_TTL)
        except Exception:
            logger.warning("Failed to cache thumbnail %s", url)

    return Response(content=data, media_type=content_type, headers={"X-Cache": "MISS", "Cache-Control": "public, max-age=86400"})
