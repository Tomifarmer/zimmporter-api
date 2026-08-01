"""S3 library index task.

Scans the S3 bucket for albums/playlists present in the backend library and
upserts them into the :class:`db.models.AvailableAlbum` table so search
results can be flagged as already imported.

Runs periodically via Celery beat (see :data:`tasks.celery_app.celery_app`),
but also exposes :func:`upsert_available_album` which download tasks call
with the exact ``browse_id`` for reliable matching.

The S3 layout mirrors :meth:`zimmporter.core.Zimmporter._build_s3_path`:
``{artist}/{album}/{track} - {title}.m4a``.
"""

import datetime
import logging
import os

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
) -> None:
    """Create or update a row in ``available_albums``.

    Used both by :func:`index_albums` (no browse ID) and by download tasks
    (with the exact browse ID).  Failures are logged but swallowed so a flaky
    DB never breaks a download job.
    """
    try:
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
                        last_seen=now,
                    )
                )
            else:
                if browse_id:
                    row.browse_id = browse_id
                if track_count:
                    row.track_count = track_count
                row.last_seen = now
    except Exception as exc:
        logger.warning("Failed to upsert available album %s - %s: %s", artist, album, exc)


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

    added: list[str] = []
    updated: list[str] = []
    pruned: list[str] = []
    found_keys = {(artist, album) for artist, album, _ in found}
    try:
        with get_session() as session:
            existing = session.query(AvailableAlbum).all()
            existing_by_key = {(row.artist, row.album): row for row in existing}
            now = datetime.datetime.now(datetime.UTC)

            for artist, album, track_count in found:
                row = existing_by_key.get((artist, album))
                if row is None:
                    session.add(
                        AvailableAlbum(
                            artist=artist,
                            album=album,
                            track_count=track_count,
                            last_seen=now,
                        )
                    )
                    added.append(f"{artist} - {album}")
                else:
                    if track_count:
                        row.track_count = track_count
                    row.last_seen = now
                    updated.append(f"{artist} - {album}")

            for row in existing:
                if (row.artist, row.album) not in found_keys:
                    session.delete(row)
                    pruned.append(f"{row.artist} - {row.album}")
            session.commit()
    except Exception as exc:
        logger.error("Failed to reconcile available albums index: %s", exc)
        raise

    logger.info(
        "S3 library index scan complete: %d indexed (%d added, %d updated), %d pruned",
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


@celery_app.task(name="tasks.index_albums")
def index_albums() -> dict:
    """Celery task wrapper around :func:`_run_index`."""
    return _run_index()
