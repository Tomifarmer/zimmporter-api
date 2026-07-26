"""SQLAlchemy ORM models for job tracking.

Tracks download jobs (albums or playlists) and individual song-level
status in MariaDB.  Used by API routes for status queries and by
Celery tasks for progress updates.
"""

import datetime

from sqlalchemy import (
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy import (
    Date as SaDate,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""

    pass


class Job(Base):
    """Represents a download job (album or playlist batch).

    The primary key (`id`) doubles as the Celery ``task_id`` so that API
    callers can poll ``GET /jobs/{id}`` for real-time progress.

    Attributes:
        id: Auto-incrementing primary key.
        job_type: Either ``"album"`` or ``"playlist"``.
        browse_id: Comma-separated YT Music browse IDs from the request.
        status: ``pending``, ``running``, ``success``, or ``failed``.
        message: Human-readable progress message.
        error: Exception message if job failed.
        current_album: Album/playlist title currently being processed.
        album_progress: 1-based index of current album in batch.
        total_albums: Number of albums/playlists in the batch.
        current_song: Songs completed for current album.
        total_songs: Total songs in current album.
        artist: Artist name (NULL for playlists).
        requested_by: Name or sub of the OIDC user who requested the job (NULL for API key or unauthenticated requests).
        created_at: UTC timestamp of job creation.
        updated_at: UTC timestamp of last update.
        songs: Relationship to :class:`Song` rows (cascade delete).
    """

    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_type = Column(Enum("album", "playlist", name="job_type_enum"), nullable=False)
    browse_id = Column(String(512), nullable=False)
    status = Column(
        Enum("pending", "running", "success", "failed", name="job_status_enum"),
        nullable=False,
        default="pending",
    )
    message = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    current_album = Column(String(512), nullable=True)
    album_name = Column(String(512), nullable=True)
    artist = Column(String(512), nullable=True)
    requested_by = Column(String(256), nullable=True)
    album_progress = Column(Integer, default=0)
    total_albums = Column(Integer, default=0)
    current_song = Column(Integer, default=0)
    total_songs = Column(Integer, default=0)
    created_at = Column(DateTime(3), default=lambda: datetime.datetime.now(datetime.UTC))
    updated_at = Column(
        DateTime(3),
        default=lambda: datetime.datetime.now(datetime.UTC),
        onupdate=lambda: datetime.datetime.now(datetime.UTC),
    )

    songs = relationship("Song", back_populates="job", cascade="all, delete-orphan")


class Song(Base):
    """Tracks per-song status within a :class:`Job`.

    Rows are inserted before downloads begin (status ``pending``) and
    updated to ``success`` or ``failed`` after yt-dlp completes.

    Attributes:
        id: Auto-incrementing primary key.
        job_id: Foreign key to :class:`Job` (cascade delete).
        title: Song title.
        artist: Artist name (or ``"playlists"`` for playlist songs).
        album: Album or playlist title.
        track_number: Track index (``None`` for playlists).
        status: ``pending``, ``downloading``, ``success``, or ``failed``.
        s3_path: S3 object key after successful upload.
        error: Exception message if download or upload failed.
        created_at: UTC timestamp of row creation.
        job: Relationship back to :class:`Job`.
    """

    __tablename__ = "songs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(512), nullable=False)
    artist = Column(String(512), nullable=False)
    album = Column(String(512), nullable=False)
    track_number = Column(Integer, nullable=True)
    status = Column(
        Enum("pending", "downloading", "success", "failed", name="song_status_enum"),
        nullable=False,
        default="pending",
    )
    s3_path = Column(String(1024), nullable=True)
    error = Column(Text, nullable=True)
    release_date = Column(SaDate, nullable=True)
    created_at = Column(DateTime(3), default=lambda: datetime.datetime.now(datetime.UTC))

    job = relationship("Job", back_populates="songs")
