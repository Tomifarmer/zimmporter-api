"""Search route — ``GET /search``.

Synchronously calls :meth:`zimmporter.core.Zimmporter.search` via
ytmusicapi with Valkey caching.  Use this endpoint to find album or
playlist browse IDs before triggering a download.
"""

import json

from fastapi import APIRouter, Query
from redis import Redis

from api.models import SearchResponse
from tasks.celery_app import celery_app
from zimmporter.core import Zimmporter

search_router = APIRouter(prefix="/search", tags=["search"])
_cache_ttl = 300


def _get_redis() -> Redis:
    return Redis.from_url(celery_app.conf.broker_url, db=2)


@search_router.get("", response_model=SearchResponse)
def search(
    q: str = Query(..., description="Search query"),
    type: str = Query("albums", description="Result type: ``albums`` or ``playlists``"),
    limit: int = Query(10, ge=1, le=50, description="Number of results (1-50)"),
) -> SearchResponse:
    """Search YouTube Music for albums or playlists.

    Args:
        q: Free-text search query.
        type: Either ``"albums"`` (default) or ``"playlists"``.
        limit: Maximum number of results to return (1-50, default 10).

    Returns:
        :class:`SearchResponse` with a list of structured result dicts.
    """
    filter_value = "community_playlists" if type == "playlists" else "albums"
    cache_key = f"search:{q}:{filter_value}"
    r = _get_redis()

    cached = r.get(cache_key)
    if cached:
        return SearchResponse(results=json.loads(cached))

    zimm = Zimmporter()
    results = zimm.search(q, filter=filter_value, limit=limit)
    r.set(cache_key, json.dumps(results), ex=_cache_ttl)
    return SearchResponse(results=results)
