from db.engine import get_session
from db.models import Song
from tasks.download import flush_song_updates


def _seed_song(title, status, error):
    with get_session() as session:
        session.add(
            Song(
                job_id=1,
                title=title,
                artist="Artist",
                album="Album",
                track_number=1,
                status=status,
                error=error,
            )
        )
        session.commit()


def _get_song(title):
    with get_session() as session:
        return (
            session.query(Song)
            .filter(Song.job_id == 1, Song.title == title)
            .first()
        )


def test_success_update_clears_stale_error(sqlite_db):
    _seed_song(title="crash song", status="failed", error="Worker crashed")

    with get_session() as session:
        flush_song_updates(
            session,
            [
                {
                    "job_id": 1,
                    "title": "crash song",
                    "status": "success",
                    "s3_path": "s3://bucket/crash.m4a",
                }
            ],
        )

    song = _get_song("crash song")
    assert song.status == "success"
    assert song.error is None


def test_failed_song_keeps_error(sqlite_db):
    _seed_song(title="bad", status="failed", error="Worker crashed")

    with get_session() as session:
        flush_song_updates(
            session,
            [
                {
                    "job_id": 1,
                    "title": "bad",
                    "status": "failed",
                    "error": "HTTP 403 Forbidden",
                }
            ],
        )

    song = _get_song("bad")
    assert song.status == "failed"
    assert song.error == "HTTP 403 Forbidden"