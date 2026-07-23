"""Job status routes — ``GET /jobs/{job_id}`` and ``GET /jobs``.

Reads from the MariaDB ``jobs`` and ``songs`` tables to return the
current state of download jobs.  The ``GET /jobs`` endpoint supports
pagination via ``limit`` and ``offset`` query parameters.
"""

from fastapi import APIRouter, HTTPException

from api.models import JobStatusResponse
from db.engine import get_session
from db.models import Job, Song

jobs_router = APIRouter(prefix="/jobs", tags=["jobs"])


def _build_response(job: Job, songs: list) -> dict:
    """Convert a :class:`db.models.Job` and its :class:`db.models.Song`\ s into a response model.

    Args:
        job: Database Job row.
        songs: Related Song rows, ordered by id ascending.

    Returns:
        Populated :class:`JobStatusResponse`.
    """
    return {
        "job_id": job.id,
        "job_type": job.job_type,
        "browse_id": job.browse_id,
        "status": job.status,
        "message": job.message,
        "error": job.error,
        "current_album": job.current_album,
        "album_progress": job.album_progress,
        "total_albums": job.total_albums,
        "current_song": job.current_song,
        "total_songs": job.total_songs,
        "artist": job.artist,
        "album_name": job.album_name,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "songs_downloaded": sum(1 for s in songs if s.status == "success"),
        "songs": [
            {
                "id": s.id,
                "title": s.title,
                "artist": s.artist,
                "album": s.album,
                "track_number": s.track_number,
                "status": s.status,
                "minio_path": s.minio_path,
                "error": s.error,
            }
            for s in songs
        ],
    }


@jobs_router.get("/{job_id}", response_model=JobStatusResponse)
def get_job_status(job_id: int) -> JobStatusResponse:
    """Return the status of a specific job with all per-song details.

    Args:
        job_id: Database primary key (matches the ``job_id`` returned
            by the POST download endpoints).

    Returns:
        Full :class:`JobStatusResponse` with embedded song statuses.

    Raises:
        HTTPException: 404 if the job ID does not exist.
    """
    with get_session() as session:
        job = session.query(Job).filter(Job.id == job_id).first()

        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        songs = session.query(Song).filter(Song.job_id == job_id).order_by(Song.id.asc()).all()

        return JobStatusResponse(**_build_response(job, songs))


@jobs_router.get("", response_model=list[JobStatusResponse])
def list_jobs(limit: int = 50, offset: int = 0) -> list[JobStatusResponse]:
    """List recent jobs with embedded song statuses.

    Jobs are returned newest-first (ordered by ``created_at`` descending).

    Args:
        limit: Maximum number of jobs (default 50).
        offset: Number of jobs to skip (default 0).

    Returns:
        List of :class:`JobStatusResponse` objects.
    """
    with get_session() as session:
        jobs = session.query(Job).order_by(Job.created_at.desc()).offset(offset).limit(limit).all()
        results = []
        for job in jobs:
            songs = session.query(Song).filter(Song.job_id == job.id).order_by(Song.id.asc()).all()
            results.append(JobStatusResponse(**_build_response(job, songs)))

    return results
