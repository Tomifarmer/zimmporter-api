from db.engine import get_session
from db.models import Job, Song


def _create_job(session, job_type="album", browse_id="MPREb_test", status="pending"):
    job = Job(
        job_type=job_type,
        browse_id=browse_id,
        status=status,
        message="Test job",
    )
    session.add(job)
    session.flush()
    return job


def _create_song(session, job_id, title="Test Song", status="pending", s3_path=None, error=None):
    song = Song(
        job_id=job_id,
        title=title,
        artist="Test Artist",
        album="Test Album",
        track_number=1,
        status=status,
        s3_path=s3_path,
        error=error,
    )
    session.add(song)
    session.flush()
    return song


class TestGetJob:
    def test_get_job_returns_job_with_songs(self, test_client):
        with get_session() as session:
            job = _create_job(session)
            _create_song(session, job.id, title="Song A")
            _create_song(session, job.id, title="Song B")

        resp = test_client.get(f"/jobs/{job.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["job_id"] == job.id
        assert len(data["songs"]) == 2

    def test_get_job_returns_s3_path_in_songs(self, test_client):
        with get_session() as session:
            job = _create_job(session)
            _create_song(session, job.id, title="Uploaded", status="success", s3_path="Artist/Album/Song.m4a")

        resp = test_client.get(f"/jobs/{job.id}")
        data = resp.json()
        song = data["songs"][0]
        assert song["s3_path"] == "Artist/Album/Song.m4a"

    def test_get_job_returns_404_for_missing(self, test_client):
        resp = test_client.get("/jobs/99999")
        assert resp.status_code == 404

    def test_get_job_includes_song_statuses(self, test_client):
        with get_session() as session:
            job = _create_job(session)
            _create_song(session, job.id, title="S1", status="success")
            _create_song(session, job.id, title="S2", status="failed", error="Something broke")

        resp = test_client.get(f"/jobs/{job.id}")
        data = resp.json()
        songs = {s["title"]: s for s in data["songs"]}
        assert songs["S1"]["status"] == "success"
        assert songs["S2"]["status"] == "failed"
        assert songs["S2"]["error"] == "Something broke"

    def test_get_job_songs_downloaded_count(self, test_client):
        with get_session() as session:
            job = _create_job(session)
            _create_song(session, job.id, title="S1", status="success")
            _create_song(session, job.id, title="S2", status="success")
            _create_song(session, job.id, title="S3", status="failed")

        resp = test_client.get(f"/jobs/{job.id}")
        data = resp.json()
        assert data["songs_downloaded"] == 2


class TestListJobs:
    def test_list_jobs_returns_empty_list(self, test_client):
        resp = test_client.get("/jobs")
        assert resp.status_code == 200
        data = resp.json()
        assert data == []

    def test_list_jobs_returns_all_jobs(self, test_client):
        with get_session() as session:
            _create_job(session, browse_id="id1")
            _create_job(session, browse_id="id2")

        resp = test_client.get("/jobs")
        data = resp.json()
        assert len(data) == 2

    def test_list_jobs_respects_limit(self, test_client):
        with get_session() as session:
            for i in range(5):
                _create_job(session, browse_id=f"id_{i}")

        resp = test_client.get("/jobs?limit=3")
        data = resp.json()
        assert len(data) == 3

    def test_list_jobs_respects_offset(self, test_client):
        with get_session() as session:
            for i in range(5):
                _create_job(session, browse_id=f"id_{i}")

        resp = test_client.get("/jobs?limit=10&offset=2")
        data = resp.json()
        assert len(data) == 3


class TestRetryJob:
    def test_retry_resets_failed_songs(self, test_client, mock_celery_tasks):
        mock_album_task, _ = mock_celery_tasks
        with get_session() as session:
            job = _create_job(session, status="running")
            _create_song(session, job.id, title="S1", status="failed", error="err")
            _create_song(session, job.id, title="S2", status="success")

        resp = test_client.post(f"/jobs/{job.id}/retry")
        assert resp.status_code == 200

        with get_session() as session:
            job = session.query(Job).filter(Job.id == job.id).first()
            assert job.status == "running"
            songs = session.query(Song).filter(Song.job_id == job.id).all()
            for s in songs:
                if s.title == "S1":
                    assert s.status == "pending"
                    assert s.error is None
                else:
                    assert s.status == "success"

    def test_retry_calls_celery_task(self, test_client, mock_celery_tasks):
        mock_album_task, _ = mock_celery_tasks
        with get_session() as session:
            job = _create_job(session, job_type="album", status="running")
            _create_song(session, job.id, status="failed", error="err")

        test_client.post(f"/jobs/{job.id}/retry")
        mock_album_task.assert_called_once()

    def test_retry_returns_400_when_no_failed_songs(self, test_client):
        with get_session() as session:
            job = _create_job(session)
            _create_song(session, job.id, status="success")

        resp = test_client.post(f"/jobs/{job.id}/retry")
        assert resp.status_code == 400

    def test_retry_returns_404_for_missing_job(self, test_client):
        resp = test_client.post("/jobs/99999/retry")
        assert resp.status_code == 404
