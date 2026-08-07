"""Celery download tasks.

Wraps :class:`zimmporter.core.Zimmporter` album/playlist download logic
in Celery tasks.  Each task:

1. Fetches metadata (album tracks or playlist songs) via ytmusicapi
2. Downloads thumbnails to :data:`zimmporter.core.temp_dir`
3. Inserts per-song :class:`db.models.Song` rows (status ``pending``)
4. Spawns a ``billiard.Pool`` to download songs in parallel
5. Updates each song row and job progress as each song completes (real-time)
6. Cleans up temporary files

Uses :class:`billiard.Pool` (Celery's vendored multiprocessing) rather
than stdlib ``multiprocessing`` because the latter conflicts with task
forking inside a worker process.

Song rows are updated via direct SQL ``UPDATE`` statements with batching
(committing every :data:`BATCH_SIZE` updates per session).  This avoids
loading song rows into ORM instances and performs far fewer round trips
than per-song commit-within-per-song sessions.

The ``_init_worker`` initializer re-creates the :class:`YTDLPLogger` in
each forked child because logger state is lost after ``os.fork()``.
"""

import logging
import os
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from sqlalchemy import update as sa_update

from db.engine import get_session
from db.models import Job, Song
from tasks.celery_app import celery_app
from tasks.index import upsert_available_album
from zimmporter.cert import get_ca_cert
from zimmporter.core import YTDL_OPTS, Zimmporter, apply_cookie_config, temp_dir
from zimmporter.ytdlp_logger import YTDLPLogger

logger = logging.getLogger(__name__)

BATCH_SIZE = 2


def flush_song_updates(session, updates: list[dict]) -> None:
    """Persist a batch of per-song updates in one commit.

    ``None`` values are skipped so existing data (e.g. ``s3_path`` from a
    previous attempt) is preserved, except that a song which completed with
    ``status == "success"`` always has its ``error`` cleared so a stale
    error (e.g. from an earlier failed run) never leaks into a successful
    row.
    """
    for u in updates:
        values = {k: v for k, v in u.items() if v is not None}
        if values.get("status") == "success":
            values["error"] = None
        if values:
            session.execute(
                sa_update(Song)
                .where(Song.job_id == u["job_id"])
                .where(Song.title == u["title"])
                .values(**values)
            )
    session.commit()


def _refresh_cookie_config() -> None:
    """Re-apply the yt-dlp cookiefile from the Valkey cookie store.

    ``core.apply_cookie_config`` writes the current store content into the
    yt-dlp cache directory each call, so running workers pick up a freshly
    uploaded cookies file without a restart.  ``YTDL_OPTS`` is a module
    global mutated in place and inherited by forked pool children.
    """
    apply_cookie_config(YTDL_OPTS)


# Song update batching threshold — commit every BATCH_SIZE song updates per session
def _init_worker() -> None:
    """Re-initialize yt-dlp logger in each forked pool child."""
    YTDLPLogger()
    logging.getLogger("yt_dlp").setLevel(logging.ERROR)


def _update_job(session, job_id: int, **kwargs) -> None:
    """Update fields on a :class:`db.models.Job` row.

    Args:
        session: Active SQLAlchemy session.
        job_id: Primary key of the job to update.
        **kwargs: Attribute name / value pairs to set.
    """
    job = session.query(Job).get(job_id)
    if job:
        for k, v in kwargs.items():
            if hasattr(job, k):
                setattr(job, k, v)


def _update_song(session, job_id: int, title: str, **kwargs) -> None:
    """Update fields on a :class:`db.models.Song` row.

    Looks up the song by ``job_id`` and ``title``, selecting the oldest
    row first (handles duplicate titles within an album).

    Args:
        session: Active SQLAlchemy session.
        job_id: Parent job ID.
        title: Song title to match.
        **kwargs: Attribute name / value pairs to set.
    """
    song = session.query(Song).filter_by(job_id=job_id, title=title).order_by(Song.id.asc()).first()
    if song:
        for k, v in kwargs.items():
            if hasattr(song, k):
                setattr(song, k, v)


@celery_app.task(bind=True, name="tasks.download_album", max_retries=0)
def download_album(self, ids: str, concurrent: int = 4) -> dict:
    """Download one or more albums.

    For each album browse ID:
    * Fetches track listing and metadata via ytmusicapi
    * Downloads the album cover thumbnail
    * Inserts ``pending`` song rows into the DB
    * Spawns ``concurrent`` pool workers to download, convert, and upload
    * Updates DB per-song and per-job progress after each song completes
    * Cleans up ``temp_dir`` after the album finishes

    The Celery ``task_id`` must match the DB ``Job.id`` (enforced by
    :func:`api.routes.download.album_download`).

    Args:
        ids: Comma-separated album browse IDs (e.g. ``"MPREb_xxx,MPREb_yyy"``).
        concurrent: Number of parallel download workers per album.

    Returns:
        ``{"status": "success"}`` on completion.

    Raises:
        Exception: Job is marked ``failed`` in DB and task is retried
            (though ``max_retries=0`` means it will ultimately fail).
    """
    with get_session() as session:
        session.query(Job).filter(Job.id == self.request.id).update(
            {"status": "running", "message": "Started", "current_album": None}
        )

    _refresh_cookie_config()
    zimm = Zimmporter()
    ids_list = [id.strip() for id in ids.split(",")]

    try:
        total_al = len(ids_list)
        for album_idx, id in enumerate(ids_list):
            album_data = zimm.yt.get_album(id)
            artist = album_data["artists"][0]["name"]
            album_name = album_data["title"]
            release_date = album_data.get("releaseDate") or None
            thumbnail_url = album_data["thumbnails"][-1]["url"]
            thumbnail_path = f"{temp_dir}{artist}/{album_name}/cover.jpg"
            os.makedirs(f"{temp_dir}{artist}/{album_name}", exist_ok=True)

            with open(thumbnail_path, "wb") as f:
                f.write(requests.get(thumbnail_url, verify=get_ca_cert()).content)

            tracks = album_data["tracks"]
            total_tracks = len(tracks)

            with get_session() as session:
                existing_titles = {
                    row[0] for row in session.query(Song.title).filter(Song.job_id == self.request.id).all()
                }
                successful_titles = {
                    row[0]
                    for row in session.query(Song.title)
                    .filter(Song.job_id == self.request.id, Song.status == "success")
                    .all()
                }
                songs_rows = [
                    Song(
                        job_id=self.request.id,
                        title=song["title"],
                        artist=artist,
                        album=album_name,
                        track_number=song.get("trackNumber"),
                        status="pending",
                        release_date=release_date,
                    )
                    for song in tracks
                    if song["title"] not in existing_titles
                ]
                if songs_rows:
                    session.add_all(songs_rows)
                _update_job(
                    session,
                    self.request.id,
                    status="running",
                    message=f"Preparing album: {album_name}",
                    current_album=f"{artist} - {album_name}",
                    album_progress=album_idx + 1,
                    total_albums=total_al,
                    current_song=0,
                    total_songs=total_tracks,
                    album_name=album_name,
                    artist=artist,
                )
            to_download = [
                (song, album_data, artist, thumbnail_path) for song in tracks if song["title"] not in successful_titles
            ]
            total_tracks = len(to_download)

            zimm.yt = None
            with ThreadPoolExecutor(max_workers=concurrent) as executor:
                futures = [executor.submit(zimm._download_album_song_task, *args) for args in to_download]

                updates = []

                for idx, future in enumerate(as_completed(futures)):
                    result = future.result()
                    # Build update payload (skip None values so existing data isn't cleared)
                    song_update = {"job_id": self.request.id, "title": result["title"]}
                    if result.get("status") is not None:
                        song_update["status"] = result["status"]
                    if result.get("s3_path") is not None:
                        song_update["s3_path"] = result["s3_path"]
                    if result.get("error") is not None:
                        song_update["error"] = result["error"]

                    updates.append(song_update)

                    # Commit accumulated batch when threshold reached (song + job progress in one session)
                    if len(updates) >= BATCH_SIZE:
                        with get_session() as session:
                            flush_song_updates(session, updates)
                            _update_job(
                                session,
                                self.request.id,
                                message=f"Processed {idx + 1}/{total_tracks} songs",
                                current_song=idx + 1,
                            )
                        updates.clear()

                # Final flush of any remaining updates after all futures complete
                with get_session() as session:
                    flush_song_updates(session, updates)

            shutil.rmtree(f"{temp_dir}{artist}/{album_name}", ignore_errors=True)

            with get_session() as session:
                success_count = (
                    session.query(Song)
                    .filter(
                        Song.job_id == self.request.id,
                        Song.album == album_name,
                        Song.status == "success",
                    )
                    .count()
                )
            upsert_available_album(artist, album_name, browse_id=id, track_count=success_count)

        with get_session() as session:
            _update_job(
                session,
                self.request.id,
                status="success",
                message="All albums downloaded successfully",
                current_album=None,
                current_song=0,
            )
        return {"status": "success"}

    except Exception as exc:
        with get_session() as session:
            _update_job(
                session,
                self.request.id,
                status="failed",
                message=str(exc),
                error=str(exc),
            )
        raise self.retry(exc=exc) from exc


@celery_app.task(bind=True, name="tasks.download_playlist", max_retries=0)
def download_playlist(self, ids: str, concurrent: int = 4) -> dict:
    """Download one or more playlists.

    Same flow as :func:`download_album` but treats each browse ID as a
    playlist.  Artist is set to ``"playlists"``, thumbnails are downloaded
    per-song (not per-playlist), and ``track_number`` is ``None``.

    The Celery ``task_id`` must match the DB ``Job.id`` (enforced by
    :func:`api.routes.download.playlist_download`).

    Args:
        ids: Comma-separated playlist browse IDs (e.g. ``"VLx_xxx"``).
        concurrent: Number of parallel download workers per playlist.

    Returns:
        ``{"status": "success"}`` on completion.

    Raises:
        Exception: Job is marked ``failed`` in DB.
    """
    with get_session() as session:
        session.query(Job).filter(Job.id == self.request.id).update(
            {"status": "running", "message": "Started", "current_album": None}
        )

    _refresh_cookie_config()
    zimm = Zimmporter()
    ids_list = [id.strip() for id in ids.split(",")]

    try:
        total_pl = len(ids_list)
        for pl_idx, id in enumerate(ids_list):
            playlist_data = zimm.yt.get_playlist(id)
            album_name = playlist_data["title"]
            to_download = list()
            unavailable = list()

            playlist_thumb_url = playlist_data["thumbnails"][-1]["url"]
            cover_dir = f"{temp_dir}playlists/{album_name}"
            cover_path = f"{cover_dir}/cover.jpg"
            os.makedirs(cover_dir, exist_ok=True)
            with open(cover_path, "wb") as f:
                f.write(requests.get(playlist_thumb_url, verify=get_ca_cert()).content)

            for song in playlist_data["tracks"]:
                if song.get("videoId") is None:
                    logger.warning("Skipping song with None videoId: %s", song.get("title", "Unknown"))
                    unavailable.append(song)
                    continue
                to_download.append((song, playlist_data, "playlists", cover_path))

            total_tracks = len(to_download)

            with get_session() as session:
                existing_titles = {
                    row[0] for row in session.query(Song.title).filter(Song.job_id == self.request.id).all()
                }
                successful_titles = {
                    row[0]
                    for row in session.query(Song.title)
                    .filter(Song.job_id == self.request.id, Song.status == "success")
                    .all()
                }
                songs_rows = [
                    Song(
                        job_id=self.request.id,
                        title=song["title"],
                        artist="playlists",
                        album=album_name,
                        track_number=None,
                        status="pending",
                    )
                    for song, _, _, _ in to_download
                    if song["title"] not in existing_titles
                ]
                unavailable_rows = [
                    Song(
                        job_id=self.request.id,
                        title=song["title"],
                        artist="playlists",
                        album=album_name,
                        track_number=None,
                        status="unavailable",
                    )
                    for song in unavailable
                    if song["title"] not in existing_titles
                ]
                if songs_rows:
                    session.add_all(songs_rows)
                if unavailable_rows:
                    session.add_all(unavailable_rows)
                _update_job(
                    session,
                    self.request.id,
                    status="running",
                    message=f"Preparing playlist: {album_name}",
                    current_album=f"Playlist: {album_name}",
                    album_progress=pl_idx + 1,
                    total_albums=total_pl,
                    current_song=0,
                    total_songs=total_tracks,
                    album_name=album_name,
                )

            to_download = [
                (song, playlist_data, artist, thumb)
                for song, playlist_data, artist, thumb in to_download
                if song["title"] not in successful_titles
            ]
            total_tracks = len(to_download)

            zimm.yt = None
            with ThreadPoolExecutor(max_workers=concurrent) as executor:
                futures = [executor.submit(zimm._download_playlist_song_task, *args) for args in to_download]

                updates = []

                for idx, future in enumerate(as_completed(futures)):
                    result = future.result()
                    # Build update payload (skip None values so existing data isn't cleared)
                    song_update = {"job_id": self.request.id, "title": result["title"]}
                    if result.get("status") is not None:
                        song_update["status"] = result["status"]
                    if result.get("s3_path") is not None:
                        song_update["s3_path"] = result["s3_path"]
                    if result.get("error") is not None:
                        song_update["error"] = result["error"]

                    updates.append(song_update)

                    # Commit accumulated batch when threshold reached (song + job progress in one session)
                    if len(updates) >= BATCH_SIZE:
                        with get_session() as session:
                            flush_song_updates(session, updates)
                            _update_job(
                                session,
                                self.request.id,
                                message=f"Processed {idx + 1}/{total_tracks} songs",
                                current_song=idx + 1,
                            )
                        updates.clear()

                # Final flush of any remaining updates after all futures complete
                with get_session() as session:
                    flush_song_updates(session, updates)

            shutil.rmtree(f"{temp_dir}playlists/{album_name}", ignore_errors=True)

            with get_session() as session:
                success_count = (
                    session.query(Song)
                    .filter(
                        Song.job_id == self.request.id,
                        Song.album == album_name,
                        Song.status == "success",
                    )
                    .count()
                )
            upsert_available_album("playlists", album_name, browse_id=id, track_count=success_count)

        with get_session() as session:
            _update_job(
                session,
                self.request.id,
                status="success",
                message="All playlists downloaded successfully",
                current_album=None,
                current_song=0,
            )
        return {"status": "success"}

    except Exception as exc:
        with get_session() as session:
            _update_job(
                session,
                self.request.id,
                status="failed",
                message=str(exc),
                error=str(exc),
            )
        raise self.retry(exc=exc) from exc
