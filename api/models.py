"""Pydantic request/response models for the FastAPI routes.

Each model maps to a specific endpoint's input or output so that
FastAPI can auto-generate OpenAPI documentation and validate payloads.
"""

import datetime as dt

from pydantic import BaseModel, Field


class SearchResponse(BaseModel):
    """Response body for ``GET /search``.

    Attributes:
        results: Raw list of result dicts from :meth:`zimmporter.core.Zimmporter.search`.
    """

    results: list[dict]


class DownloadRequest(BaseModel):
    """Request body for ``POST /download/album`` and ``POST /download/playlist``.

    Attributes:
        id: Browse ID or comma-separated IDs (e.g. ``MPREb_xxx``).
        concurrent: Number of parallel download workers (1-32, default 4).
    """

    id: str = Field(..., description="Album or Playlist browse ID (comma-separated for multiple)")
    concurrent: int = Field(default=4, ge=1, le=32, description="Number of concurrent download workers")


class JobResponse(BaseModel):
    """Response body for POST download endpoints.

    Returns immediately after queuing; actual progress is tracked via
    ``GET /jobs/{job_id}``.

    Attributes:
        job_id: Auto-generated database primary key.
        status: Initial status, always ``"pending"``.
    """

    job_id: int
    status: str


class SongStatusResponse(BaseModel):
    """Per-song status embedded in :class:`JobStatusResponse`.

    Attributes:
        id: Database primary key.
        title: Song title.
        artist: Artist name (or ``"playlists"`` for playlist songs).
        album: Album or playlist title.
        track_number: Track index within the album (``None`` for playlists).
        status: One of ``pending``, ``downloading``, ``success``, ``failed``.
        minio_path: S3 object key if upload succeeded.
        error: Failure message if status is ``failed``.
    """

    id: int
    title: str
    artist: str
    album: str
    track_number: int | None = None
    status: str
    minio_path: str | None = None
    error: str | None = None


class JobStatusResponse(BaseModel):
    """Response body for ``GET /jobs/{id}`` and ``GET /jobs``.

    Includes aggregate progress counters and the full list of per-song
    statuses.

    Attributes:
        job_id: Database primary key (matches Celery ``task_id``).
        job_type: ``"album"`` or ``"playlist"``.
        browse_id: Original comma-separated browse ID passed in the request.
        status: One of ``pending``, ``running``, ``success``, ``failed``.
        message: Human-readable progress message.
        error: Failure message if job failed.
        current_album: Album name currently being processed.
        album_progress: 1-based index of the current album in the batch.
        total_albums: Total number of albums/playlists in the batch.
        current_song: Number of songs completed for the current album.
        total_songs: Total songs in the current album.
        album_name: Original album or playlist title (raw, unformatted).
        artist: Artist name (NULL for playlists).
        created_at: UTC datetime when the job was created.
        updated_at: Last UTC update timestamp.
        songs_downloaded: Count of songs with status ``"success"``.
        songs: Per-song status details.
    """

    job_id: int
    job_type: str
    browse_id: str
    status: str
    message: str | None = None
    error: str | None = None
    current_album: str | None = None
    album_progress: int = 0
    total_albums: int = 0
    current_song: int = 0
    total_songs: int = 0
    artist: str | None = None
    album_name: str | None = None
    created_at: dt.datetime | None = None
    updated_at: dt.datetime | None = None
    songs_downloaded: int = Field(default=0, description="Number of songs with status ``success``")
    songs: list[SongStatusResponse] = []
