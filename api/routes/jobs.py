"""Job status routes — ``GET /jobs/{job_id}`` and ``GET /jobs``.

Reads from the MariaDB ``jobs`` and ``songs`` tables to return the
current state of download jobs.  The ``GET /jobs`` endpoint supports
pagination via ``limit`` and ``offset`` query parameters.
"""

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import func

from api.models import JobResponse, JobStatsResponse, JobStatusResponse
from db.engine import get_session
from db.models import Job, Song
from tasks.download import download_album, download_playlist

jobs_router = APIRouter(prefix="/jobs", tags=["jobs"])


def _get_requested_by(request: Request) -> str | None:
    """Extract the requesting user's name from an authenticated request."""
    user = request.scope.get("user")
    if user is None:
        return None
    return user.get("name") or user.get("sub")


def _build_response(job: Job, songs: list) -> dict:
    """Convert a :class:`db.models.Job` and its :class:`db.models.Song`\'s into a response model.

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
        "requested_by": job.requested_by,
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
                "s3_path": s.s3_path,
                "error": s.error,
            }
            for s in songs
        ],
    }


@jobs_router.get("/stats", response_model=JobStatsResponse)
def get_job_stats(request: Request) -> JobStatsResponse:
    """Return aggregate job status counts across all records.

    Computes counts over every job (respecting the authenticated user
    filter) rather than a single page, so the frontend can display
    accurate global status totals next to the paginated job list.

    Args:
        request: FastAPI request (used to extract the authenticated user).

    Returns:
        :class:`JobStatsResponse` with global counts.
    """
    requested_by = _get_requested_by(request)

    with get_session() as session:
        job_query = session.query(Job)
        if requested_by:
            job_query = job_query.filter(Job.requested_by == requested_by)

        status_counts = dict(
            job_query.with_entities(Job.status, func.count()).group_by(Job.status).all()
        )

        song_query = session.query(Song).filter(Song.status == "failed")
        if requested_by:
            song_query = song_query.join(Job, Job.id == Song.job_id).filter(
                Job.requested_by == requested_by
            )
        partial_ids = {job_id for (job_id,) in song_query.with_entities(Song.job_id).distinct().all()}

        success_partial_query = session.query(Song.job_id).filter(Song.status == "failed").join(
            Job, Job.id == Song.job_id
        ).filter(Job.status == "success")
        if requested_by:
            success_partial_query = success_partial_query.filter(Job.requested_by == requested_by)
        success_partial = success_partial_query.distinct().count()

    total = sum(status_counts.values())
    pending = status_counts.get("pending", 0)
    running = status_counts.get("running", 0) + pending
    failed = status_counts.get("failed", 0)
    success = status_counts.get("success", 0) - success_partial

    return JobStatsResponse(
        total=total,
        pending=pending,
        running=running,
        success=success,
        failed=failed,
        partial=len(partial_ids),
    )


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
def list_jobs(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    status: str = "all",
) -> list[JobStatusResponse]:
    """List recent jobs with embedded song statuses.

    When the request is authenticated via OIDC Bearer token, only jobs
    requested by that user are returned.  Unauthenticated requests (auth
    disabled or API key) see all jobs.

    Jobs are returned newest-first (ordered by ``created_at`` descending).

    The ``status`` parameter filters jobs before pagination so the
    frontend can page through a single status.  ``success`` excludes
    partial successes (jobs whose status is ``success`` but that have at
    least one failed song); those are returned by ``partial`` instead.

    Args:
        request: FastAPI request (used to extract the authenticated user).
        limit: Maximum number of jobs (default 50).
        offset: Number of jobs to skip (default 0).
        status: Job status filter: ``all``, ``pending``, ``running``,
            ``success``, ``failed``, or ``partial`` (default ``all``).

    Returns:
        List of :class:`JobStatusResponse` objects.
    """
    requested_by = _get_requested_by(request)

    with get_session() as session:
        query = session.query(Job)
        if requested_by:
            query = query.filter(Job.requested_by == requested_by)

        if status in ("success", "partial"):
            partial_ids = (
                session.query(Song.job_id)
                .filter(Song.status == "failed")
                .distinct()
                .scalar_subquery()
            )
            if status == "success":
                query = query.filter(Job.status == "success").filter(~Job.id.in_(partial_ids))
            else:
                query = query.filter(Job.id.in_(partial_ids))
        elif status == "failed":
            query = query.filter(Job.status == "failed")
        elif status == "pending":
            query = query.filter(Job.status == "pending")
        elif status == "running":
            query = query.filter(Job.status == "running")

        jobs = (
            query.order_by(Job.created_at.desc(), Job.id.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        results = []
        for job in jobs:
            songs = session.query(Song).filter(Song.job_id == job.id).order_by(Song.id.asc()).all()
            results.append(JobStatusResponse(**_build_response(job, songs)))

    return results


@jobs_router.post("/{job_id}/retry", response_model=JobResponse)
def retry_job(job_id: int, request: Request) -> JobResponse:
    """Reset failed songs in a job and re-queue it.

    Finds all songs with status ``failed`` for the given job, resets
    them to ``pending`` (clearing the error), and dispatches the
    original download task again.

    Args:
        job_id: Database primary key of the job to retry.
        request: FastAPI request (used to verify job ownership).

    Returns:
        The ``job_id`` with the new status ``"running"``.

    Raises:
        HTTPException: 404 if the job does not exist.
        HTTPException: 400 if the job has no failed songs to retry.
        HTTPException: 403 if the job belongs to a different OIDC user.
    """
    requested_by = _get_requested_by(request)

    with get_session() as session:
        job = session.query(Job).filter(Job.id == job_id).first()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        if requested_by and job.requested_by and job.requested_by != requested_by:
            raise HTTPException(status_code=403, detail="You do not own this job")

        failed_songs = session.query(Song).filter(Song.job_id == job_id, Song.status == "failed").all()
        if not failed_songs:
            raise HTTPException(status_code=400, detail="No failed songs to retry")

        for song in failed_songs:
            song.status = "pending"
            song.error = None

        job_type = job.job_type
        browse_id = job.browse_id
        job.status = "running"
        job.message = "Retrying failed songs"
        session.commit()

    task = download_album if job_type == "album" else download_playlist
    task.apply_async(args=[browse_id], kwargs={"concurrent": 4}, task_id=str(job_id))

    return JobResponse(job_id=job_id, status="running")
