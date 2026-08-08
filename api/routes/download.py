"""Download routes — ``POST /download/album`` and ``POST /download/playlist``.

Each endpoint creates a database :class:`db.models.Job` row first (to
obtain an auto-increment ID), then triggers the matching Celery task
with ``task_id`` set to the DB row's primary key.  The response returns
immediately with the ``job_id`` so callers can poll ``GET
/jobs/{job_id}`` for progress.
"""

from fastapi import APIRouter, Request

from api.models import DownloadRequest, JobResponse
from api.user import get_requested_by, get_requested_groups_delimited
from db.engine import get_session
from db.models import Job
from tasks.download import download_album, download_playlist

download_router = APIRouter(prefix="/download", tags=["download"])


@download_router.post("/album", response_model=JobResponse)
def album_download(req: DownloadRequest, request: Request) -> JobResponse:
    """Queue one or more albums for download.

    A ``Job`` row is inserted with status ``pending``, then
    ``tasks.download_album.apply_async`` is called with the DB row's
    auto-increment ID as ``task_id``.

    Args:
        req: Album browse ID(s) and desired concurrency.
        request: FastAPI request (used to extract the authenticated user).

    Returns:
        The new ``job_id`` (always with status ``"pending"``).
    """
    with get_session() as session:
        job = Job(
            id=None,
            job_type="album",
            browse_id=req.id,
            status="pending",
            message="Queued",
            requested_by=get_requested_by(request),
            requested_groups=get_requested_groups_delimited(request),
        )
        session.add(job)
        session.flush()
        job_id = job.id

    download_album.apply_async(args=[req.id], kwargs={"concurrent": req.concurrent}, task_id=str(job_id))
    return JobResponse(job_id=job_id, status="pending")


@download_router.post("/playlist", response_model=JobResponse)
def playlist_download(req: DownloadRequest, request: Request) -> JobResponse:
    """Queue one or more playlists for download.

    Same flow as :func:`album_download` but dispatches
    ``tasks.download_playlist`` instead.

    Args:
        req: Playlist browse ID(s) and desired concurrency.
        request: FastAPI request (used to extract the authenticated user).

    Returns:
        The new ``job_id`` (always with status ``"pending"``).
    """
    with get_session() as session:
        job = Job(
            id=None,
            job_type="playlist",
            browse_id=req.id,
            status="pending",
            message="Queued",
            requested_by=get_requested_by(request),
            requested_groups=get_requested_groups_delimited(request),
        )
        session.add(job)
        session.flush()
        job_id = job.id

    download_playlist.apply_async(args=[req.id], kwargs={"concurrent": req.concurrent}, task_id=str(job_id))
    return JobResponse(job_id=job_id, status="pending")
