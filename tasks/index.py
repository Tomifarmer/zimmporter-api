"""Library index tasks.

Reconcile the :class:`db.models.AvailableAlbum` table with the albums
present in the backend library so search results can be flagged as already
imported.  Two sources are supported, selected via ``INDEX_SOURCE``:

* ``s3`` (default) — scan the S3 bucket (``tasks.index_albums``).
* ``navidrome`` — query a Navidrome server's Subsonic API
  (``tasks.index_navidrome``).
* ``both`` — run both and merge the results.

Also exposes :func:`upsert_available_album` which download tasks call with
the exact ``browse_id`` for reliable matching.

The S3 layout mirrors :meth:`zimmporter.core.Zimmporter._build_s3_path`:
``{artist}/{album}/{track} - {title}.m4a``.
"""

import datetime
import logging
import os

from sqlalchemy.exc import IntegrityError

from db.engine import get_session
from db.models import AvailableAlbum
from tasks.celery_app import celery_app
from zimmporter.cert import get_ca_cert

logger = logging.getLogger(__name__)


def _get_s3_client():
    """Build a lazily-imported boto3 S3 client from environment variables."""
    import boto3
    from botocore.config import Config

    use_https = os.getenv("AWS_USE_SSL", "true").lower() == "true"
    return boto3.Session(
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
    ).client(
        "s3",
        endpoint_url=os.getenv("AWS_ENDPOINT_URL"),
        config=Config(connect_timeout=30, read_timeout=60, retries={"max_attempts": 5, "mode": "standard"}),
        verify=get_ca_cert() if use_https else None,
    )


def _scan_bucket(client, bucket: str) -> set[tuple[str, str, int]]:
    """Enumerate ``(artist, album, track_count)`` tuples present in the bucket.

    Walks top-level prefixes (artists) via ``Delimiter="/"``, then lists each
    artist's album sub-prefixes.  Track counts are summed from the per-album
    object listing.
    """
    found: set[tuple[str, str, int]] = set()
    paginator = client.get_paginator("list_objects_v2")

    for page in paginator.paginate(Bucket=bucket, Delimiter="/"):
        for prefix in page.get("CommonPrefixes", []):
            artist = prefix["Prefix"].rstrip("/")
            for inner_page in paginator.paginate(Bucket=bucket, Prefix=prefix["Prefix"], Delimiter="/"):
                for inner in inner_page.get("CommonPrefixes", []):
                    album = inner["Prefix"].rstrip("/").split("/", 1)[-1]
                    track_count = _count_tracks(client, bucket, f"{artist}/{album}/")
                    found.add((artist, album, track_count))
    return found


def _count_tracks(client, bucket: str, prefix: str) -> int:
    """Count objects under an album prefix (best-effort)."""
    total = 0
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        total += len(page.get("Contents", []))
    return total


def upsert_available_album(
    artist: str,
    album: str,
    browse_id: str | None = None,
    track_count: int | None = None,
    genre: str | None = None,
) -> None:
    """Create or update a row in ``available_albums``.

    Used both by :func:`index_albums` (no browse ID) and by download tasks
    (with the exact browse ID).  Failures are logged but swallowed so a flaky
    DB never breaks a download job.

    A bare look-then-create can lose a race: a concurrent writer (an index
    task running while a download finishes, or a parallel download) may insert
    the same ``(artist, album)`` between the lookup and the insert.  On such a
    duplicate-key error the upsert is retried once — by then the racing row is
    visible, so the retry becomes an update and the ``browse_id``/``track_count``
    are preserved.
    """
    try:
        _upsert_available_album_row(
            artist, album, browse_id=browse_id, track_count=track_count, genre=genre
        )
    except IntegrityError:
        logger.info(
            "Duplicate-key while upserting %s - %s (racing index/download); retrying as update",
            artist,
            album,
        )
        try:
            _upsert_available_album_row(
                artist, album, browse_id=browse_id, track_count=track_count, genre=genre
            )
        except Exception as exc:
            logger.warning("Failed to upsert available album %s - %s: %s", artist, album, exc)
    except Exception as exc:
        logger.warning("Failed to upsert available album %s - %s: %s", artist, album, exc)


def _upsert_available_album_row(
    artist: str,
    album: str,
    browse_id: str | None,
    track_count: int | None,
    genre: str | None = None,
) -> None:
    """Single look-then-create/update pass behind :func:`upsert_available_album`."""
    now = datetime.datetime.now(datetime.UTC)
    with get_session() as session:
        row = (
            session.query(AvailableAlbum)
            .filter(AvailableAlbum.artist == artist, AvailableAlbum.album == album)
            .first()
        )
        if row is None:
            session.add(
                AvailableAlbum(
                    artist=artist,
                    album=album,
                    browse_id=browse_id,
                    track_count=track_count,
                    genre=genre,
                    last_seen=now,
                )
            )
        else:
            if browse_id:
                row.browse_id = browse_id
            if track_count:
                row.track_count = track_count
            if genre:
                row.genre = genre
            row.last_seen = now


def _normalize_key(value: str) -> str:
    """Fold a name to the form MariaDB uses for the ``(artist, album)`` uniqueness.

    MariaDB's default ``utf8mb4`` collation treats the unique constraint
    case-insensitively and ignores trailing whitespace, so Python-side matching
    must do the same — otherwise the reconcile would ``INSERT`` rows the
    database already considers duplicates (raising ``IntegrityError`` on
    commit, failing the index task).
    """
    return value.strip().casefold()


def _reconcile_available(found) -> dict:
    """Upsert + prune the ``available_albums`` table from ``(artist, album)`` keys.

    Entries present in ``found`` are inserted or refreshed; entries in the
    table but absent from ``found`` are pruned.  Existing ``browse_id`` values
    are preserved (the scan sources have no browse IDs).

    Args:
        found: Iterable of ``(artist, album, track_count)`` tuples.

    Returns:
        Dict with ``indexed``, ``added``, ``updated``, ``pruned`` and
        ``scanned_at`` (ISO timestamp) keys.
    """
    found = list(found)
    try:
        return _reconcile_found(found)
    except IntegrityError:
        logger.info("Duplicate-key while reconciling (racing an in-flight download); retrying once")
        return _reconcile_found(found)


def _reconcile_found(found) -> dict:
    """Single fresh-snapshot reconcile pass over ``found``.

    Rows are matched on the collation-neutral key (:func:`_normalize_key`) so
    the case/whitespace of a feed entry aligns with the existing row, and
    newly-added rows are registered immediately so duplicate entries in
    ``found`` collapse into one insert instead of a second row.
    """
    found_keys = {(_normalize_key(artist), _normalize_key(album)) for artist, album, _ in found}

    added: list[str] = []
    updated: list[str] = []
    pruned: list[str] = []
    now = datetime.datetime.now(datetime.UTC)
    try:
        with get_session() as session:
            existing = session.query(AvailableAlbum).all()
            existing_by_key = {
                (_normalize_key(row.artist), _normalize_key(row.album)): row for row in existing
            }

            for artist, album, track_count in found:
                key = (_normalize_key(artist), _normalize_key(album))
                row = existing_by_key.get(key)
                if row is None:
                    row = AvailableAlbum(
                        artist=artist,
                        album=album,
                        track_count=track_count,
                        last_seen=now,
                    )
                    session.add(row)
                    existing_by_key[key] = row
                    added.append(f"{artist} - {album}")
                else:
                    if track_count:
                        row.track_count = track_count
                    row.last_seen = now
                    updated.append(f"{artist} - {album}")

            for row in existing:
                if (_normalize_key(row.artist), _normalize_key(row.album)) not in found_keys:
                    session.delete(row)
                    pruned.append(f"{row.artist} - {row.album}")
            session.commit()
    except Exception as exc:
        logger.error("Failed to reconcile available albums index: %s", exc)
        raise

    logger.info(
        "Library index reconcile complete: %d indexed (%d added, %d updated), %d pruned",
        len(found),
        len(added),
        len(updated),
        len(pruned),
    )
    for entry in added:
        logger.info("  [index] added: %s", entry)
    for entry in pruned:
        logger.info("  [index] pruned: %s", entry)
    for entry in updated:
        logger.debug("  [index] updated: %s", entry)

    return {
        "indexed": len(found),
        "added": len(added),
        "updated": len(updated),
        "pruned": len(pruned),
        "scanned_at": now.isoformat(),
    }


def _run_index() -> dict:
    """Scan the S3 bucket and reconcile the ``available_albums`` index.

    Returns:
        Dict with ``indexed`` (entries present in S3), ``added`` (new
        entries), ``updated`` (entries refreshed), ``pruned`` (entries no
        longer present in S3) and ``scanned_at`` ISO timestamps.
    """
    bucket = os.getenv("AWS_BUCKET")
    if not bucket:
        logger.warning("AWS_BUCKET is not set; skipping library index scan")
        return {"indexed": 0, "added": 0, "updated": 0, "pruned": 0, "scanned_at": None}

    logger.info("S3 library index scan starting (bucket=%s)", bucket)
    try:
        client = _get_s3_client()
        found = _scan_bucket(client, bucket)
    except Exception as exc:
        logger.error("S3 library index scan failed: %s", exc)
        raise

    if not found:
        logger.info("S3 library index scan found no albums in bucket %s", bucket)

    result = _reconcile_available(found)
    result["indexed"] = len(found)
    return result


def _run_navidrome_index() -> dict:
    """Query Navidrome and reconcile the ``available_albums`` index.

    Returns:
        Dict with ``indexed``, ``added``, ``updated``, ``pruned`` and
        ``scanned_at`` keys (``indexed`` from the Navidrome album list).
    """
    from zimmporter.navidrome import get_albums

    logger.info("Navidrome library index scan starting")
    found = get_albums()

    result = _reconcile_available(found)
    result["indexed"] = len(found)
    return result


@celery_app.task(name="tasks.index_albums")
def index_albums() -> dict:
    """Celery task wrapper around :func:`_run_index`."""
    return _run_index()


@celery_app.task(name="tasks.index_navidrome")
def index_navidrome() -> dict:
    """Celery task wrapper around :func:`_run_navidrome_index`."""
    return _run_navidrome_index()
