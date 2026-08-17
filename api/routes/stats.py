"""Statistics routes — ``GET /stats``.

Aggregates job counts (scoped to the requesting user/group, mirroring
``GET /jobs/stats``) and library size plus genre distribution from the
``available_albums`` index for the frontend stats tab.

Genres are only stored from download time onward, so albums that predate
the column are lazily backfilled here (bounded per request) via the iTunes
Search API.  The backfill is best-effort: misses, timeouts, and disabled
lookup all leave the row untouched without failing the request.
"""

import logging
import os

from fastapi import APIRouter, Request
from sqlalchemy import func, or_

from api.models import (
    GenreCount,
    JobStatsAggregate,
    JobTypeCounts,
    LibraryStats,
    StatsResponse,
    TopUserCount,
)
from api.routes.jobs import _visible_predicates
from db.engine import get_session
from db.models import AvailableAlbum, Job, Song
from zimmporter.core import _lookup_genre

logger = logging.getLogger(__name__)

stats_router = APIRouter(prefix="/stats", tags=["stats"])

#: Maximum number of missing genres resolved per request (self-heals over time).
_GENRE_BACKFILL_LIMIT = int(os.getenv("GENRE_BACKFILL_LIMIT", "3"))

#: Maximum number of top users returned.
_TOP_USERS_LIMIT = int(os.getenv("TOP_USERS_LIMIT", "5"))


def _backfill_genres() -> None:
    """Resolve a bounded number of missing album genres and persist them.

    Selects up to :data:`_GENRE_BACKFILL_LIMIT` albums with ``genre IS NULL``
    (playlists excluded) and looks each one up via the iTunes Search API.
    Errors are swallowed — genre is best-effort metadata and must never fail
    the stats request.
    """
    try:
        with get_session() as session:
            rows = (
                session.query(AvailableAlbum)
                .filter(AvailableAlbum.artist != "playlists", AvailableAlbum.genre.is_(None))
                .order_by(AvailableAlbum.id.asc())
                .limit(_GENRE_BACKFILL_LIMIT)
                .all()
            )
            for row in rows:
                genre = _lookup_genre(row.artist, row.album)
                if genre:
                    row.genre = genre
    except Exception:  # noqa: BLE001 - genre backfill is best-effort
        logger.warning("Genre backfill failed", exc_info=True)


@stats_router.get("", response_model=StatsResponse)
def get_stats(request: Request) -> StatsResponse:
    """Return aggregate job, library, and genre statistics.

    Job aggregates respect the authenticated user/group visibility rules
    (the same predicates as ``GET /jobs/stats``); library and genre numbers
    cover the whole ``available_albums`` index.

    Args:
        request: FastAPI request (used to extract the authenticated user).

    Returns:
        :class:`StatsResponse` with job aggregates, library size, and the
        genre distribution.
    """
    _backfill_genres()

    predicates = _visible_predicates(request)

    with get_session() as session:
        job_query = session.query(Job)
        if predicates:
            job_query = job_query.filter(or_(*predicates))

        total = job_query.count()
        status_counts = dict(
            job_query.with_entities(Job.status, func.count()).group_by(Job.status).all()
        )
        type_counts = dict(
            job_query.with_entities(Job.job_type, func.count()).group_by(Job.job_type).all()
        )

        library = LibraryStats(
            albums=session.query(AvailableAlbum)
            .filter(AvailableAlbum.artist != "playlists")
            .count(),
            playlists=session.query(AvailableAlbum)
            .filter(AvailableAlbum.artist == "playlists")
            .count(),
            artists=(
                session.query(func.count(func.distinct(AvailableAlbum.artist)))
                .filter(AvailableAlbum.artist != "playlists")
                .scalar()
                or 0
            ),
            tracks=(
                session.query(func.coalesce(func.sum(AvailableAlbum.track_count), 0)).scalar()
                or 0
            ),
        )

        genre_rows = (
            session.query(AvailableAlbum.genre, func.count())
            .filter(
                AvailableAlbum.artist != "playlists",
                AvailableAlbum.genre.isnot(None),
            )
            .group_by(AvailableAlbum.genre)
            .order_by(func.count().desc())
            .all()
        )

        user_query = session.query(Job).filter(Job.requested_by.isnot(None))
        if predicates:
            user_query = user_query.filter(or_(*predicates))

        user_job_counts = dict(
            user_query.with_entities(Job.requested_by, func.count(Job.id))
            .group_by(Job.requested_by)
            .order_by(func.count(Job.id).desc())
            .limit(_TOP_USERS_LIMIT)
            .all()
        )
        user_track_query = (
            session.query(Job.requested_by, func.count(Song.id))
            .join(Song, Song.job_id == Job.id)
            .filter(Job.requested_by.isnot(None), Song.status == "success")
        )
        if predicates:
            user_track_query = user_track_query.filter(or_(*predicates))
        user_track_counts = dict(user_track_query.group_by(Job.requested_by).all())

    top_users = [
        TopUserCount(user=user, jobs=jobs, tracks=user_track_counts.get(user, 0))
        for user, jobs in user_job_counts.items()
    ]

    return StatsResponse(
        jobs=JobStatsAggregate(
            total=total,
            by_status=status_counts,
            by_type=JobTypeCounts(
                album=type_counts.get("album", 0),
                playlist=type_counts.get("playlist", 0),
            ),
        ),
        library=library,
        genres=[GenreCount(genre=genre, count=count) for genre, count in genre_rows],
        top_users=top_users,
    )