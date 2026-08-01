"""Search route — ``GET /search``.

Synchronously calls :meth:`zimmporter.core.Zimmporter.search` via
ytmusicapi with Valkey caching.  Use this endpoint to find album or
playlist browse IDs before triggering a download.
"""

import base64
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from fastapi import APIRouter, Query
from redis import Redis
from starlette.requests import Request

from api.models import SearchResponse
from api.routes.thumbnail import _cache_key
from db.engine import get_session
from db.models import AvailableAlbum
from tasks.celery_app import celery_app
from zimmporter.core import Zimmporter

#: Maximum allowed thumbnail size in bytes (10 MB).
_MAX_THUMB_SIZE = 10 * 1024 * 1024

#: How long to cache thumbnails in Valkey (24 hours).
_THUMB_CACHE_TTL = 86400

search_router = APIRouter(prefix="/search", tags=["search"])
_cache_ttl = 300

_REDIS_THUMB: Redis | None = None


def _get_redis() -> Redis:
    return Redis.from_url(celery_app.conf.broker_url, db=2)


def _norm(value: str) -> str:
    """Normalize a name for fuzzy matching (lowercase, collapsed whitespace)."""
    return re.sub(r"\s+", " ", value or "").strip().lower()


def _load_available_index() -> tuple[set[str], set[tuple[str, str]]]:
    """Load the S3 library index for search-result matching.

    Returns:
        ``(browse_ids, names)`` where ``browse_ids`` contains exact YT Music
        browse IDs and ``names`` contains ``(normalized artist, normalized
        album)`` tuples from the ``available_albums`` table.  Failures are
        swallowed so search still works when the DB is unavailable.
    """
    browse_ids: set[str] = set()
    names: set[tuple[str, str]] = set()
    try:
        with get_session() as session:
            for row in session.query(AvailableAlbum).all():
                if row.browse_id:
                    browse_ids.add(row.browse_id)
                names.add((_norm(row.artist), _norm(row.album)))
    except Exception:
        pass
    return browse_ids, names


def _mark_available(results: list[dict]) -> None:
    """Add an ``available`` boolean to each search result in place.

    An album/playlist is flagged as available when its ``browseId`` matches
    the index exactly, or when a normalized artist + title matches a library
    entry.  Playlists are matched by title only (artist is ``"playlists"``).
    """
    browse_ids, names = _load_available_index()
    for result in results:
        available = bool(result.get("browseId") in browse_ids)
        if not available:
            title = _norm(result.get("title", ""))
            if result.get("resultType") in ("album", "song", "video"):
                for artist in result.get("artist", []) or []:
                    if (_norm(artist), title) in names:
                        available = True
                        break
            else:
                available = (_norm("playlists"), title) in names
        result["available"] = available


def _get_redis_thumb() -> Redis | None:
    global _REDIS_THUMB
    if _REDIS_THUMB is not None:
        try:
            _REDIS_THUMB.ping()
            return _REDIS_THUMB
        except Exception:
            _REDIS_THUMB = None
    try:
        _REDIS_THUMB = Redis.from_url(
            celery_app.conf.broker_url,
            db=3,
            socket_connect_timeout=2,
            socket_timeout=5,
            health_check_interval=30,
        )
        _REDIS_THUMB.ping()
    except Exception:
        _REDIS_THUMB = None
    return _REDIS_THUMB


def _shrink_thumbnail(url: str, max_dim: int = 200) -> str:
    """Reduce the dimensions of a YouTube CDN thumbnail URL.

    YouTube's image CDNs encode dimensions in the URL path as
    ``=w<width>-h<height>-...``.  This replaces the dimension
    segment with ``w{max_dim}-h{max_dim}`` so the proxy returns
    a much smaller image, reducing transfer time to the browser.
    If the URL doesn't match the expected pattern it is returned
    unchanged.
    """
    return re.sub(r"=w\d+-h\d+-", f"=w{max_dim}-h{max_dim}-", url) if "=" in url else url


def _fetch_thumbnail_bytes(url: str) -> tuple[bytes, str] | None:
    """Fetch a single thumbnail, checking Valkey cache first.

    Args:
        url: CDN thumbnail URL (will be shrunk first).

    Returns:
        ``(image_bytes, content_type)`` or ``None`` on failure.
    """
    key = _cache_key(url)

    redis = _get_redis_thumb()
    if redis is not None:
        try:
            cached = redis.hgetall(key)
            if cached:
                return cached[b"data"], cached.get(b"content_type", b"image/jpeg").decode()
        except Exception:
            pass

    try:
        upstream = requests.get(url, stream=True, timeout=15)
        upstream.raise_for_status()
    except requests.RequestException:
        return None

    content_type = upstream.headers.get("content-type", "image/jpeg")

    size = 0
    chunks: list[bytes] = []
    for chunk in upstream.iter_content(chunk_size=65536):
        size += len(chunk)
        if size > _MAX_THUMB_SIZE:
            return None
        chunks.append(chunk)

    data = b"".join(chunks)

    if redis is not None:
        try:
            redis.hset(key, mapping={"data": data, "content_type": content_type})
            redis.expire(key, _THUMB_CACHE_TTL)
        except Exception:
            pass

    return data, content_type


@search_router.get("", response_model=SearchResponse)
def search(
    request: Request,
    q: str = Query(..., description="Search query"),
    type: str = Query(
        "albums", description="Result type: ``albums``, ``featured_playlists``, or ``community_playlists``"
    ),
    limit: int = Query(10, ge=1, le=50, description="Number of results (1-50)"),
) -> SearchResponse:
    """Search YouTube Music for albums or playlists.

    Args:
        request: FastAPI request (injected automatically).
        q: Free-text search query.
        type: Either ``"albums"`` (default), ``"featured_playlists"``, or ``"community_playlists"``.
        limit: Maximum number of results to return (1-50, default 10).

    Returns:
        :class:`SearchResponse` with a list of structured result dicts.
    """
    filter_value = type if type in ("albums", "featured_playlists", "community_playlists") else "albums"
    cache_key = f"search:{q}:{filter_value}:{limit}"
    r = _get_redis()

    cached = r.get(cache_key)
    if cached:
        results = json.loads(cached)
    else:
        zimm = Zimmporter()
        results = zimm.search(q, filter=filter_value, limit=limit)
        r.set(cache_key, json.dumps(results), ex=_cache_ttl)

    if os.environ.get("API_PROXY_FETCH", "").lower() == "true":
        with ThreadPoolExecutor(max_workers=10) as pool:
            fut_to_idx: dict = {}
            for i, r_item in enumerate(results):
                thumb = r_item.get("thumbnail")
                if thumb:
                    fut = pool.submit(_fetch_thumbnail_bytes, thumb)
                    fut_to_idx[fut] = i

            for fut in as_completed(fut_to_idx):
                result = fut.result()
                if result is not None:
                    data, content_type = result
                    b64 = base64.b64encode(data).decode("ascii")
                    results[fut_to_idx[fut]]["thumbnail"] = f"data:{content_type};base64,{b64}"

    _mark_available(results)

    return SearchResponse(results=results)
