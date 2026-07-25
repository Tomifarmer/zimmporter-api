class TestDownloadAlbum:
    def test_album_download_returns_job_id(self, test_client, mock_celery_tasks):
        mock_album_task, _ = mock_celery_tasks
        resp = test_client.post("/download/album", json={"id": "MPREb_abc123", "concurrent": 4})
        assert resp.status_code == 200
        data = resp.json()
        assert "job_id" in data
        assert data["job_id"] > 0
        assert data["status"] == "pending"

    def test_album_download_creates_job_in_db(self, test_client, mock_celery_tasks):
        from db.engine import get_session
        from db.models import Job

        resp = test_client.post("/download/album", json={"id": "MPREb_abc123"})
        job_id = resp.json()["job_id"]
        with get_session() as session:
            job = session.query(Job).filter(Job.id == job_id).first()
            assert job is not None
            assert job.job_type == "album"
            assert job.browse_id == "MPREb_abc123"
            assert job.status == "pending"

    def test_album_download_triggers_celery_task(self, test_client, mock_celery_tasks):
        mock_album_task, _ = mock_celery_tasks
        resp = test_client.post("/download/album", json={"id": "MPREb_abc123"})
        job_id = resp.json()["job_id"]
        mock_album_task.assert_called_once()
        args, kwargs = mock_album_task.call_args
        assert kwargs["task_id"] == str(job_id)
        assert kwargs["args"] == ["MPREb_abc123"]

    def test_album_download_default_concurrent(self, test_client, mock_celery_tasks):
        mock_album_task, _ = mock_celery_tasks
        test_client.post("/download/album", json={"id": "MPREb_abc123"})
        _, kwargs = mock_album_task.call_args
        assert kwargs["kwargs"]["concurrent"] == 4

    def test_album_download_custom_concurrent(self, test_client, mock_celery_tasks):
        mock_album_task, _ = mock_celery_tasks
        test_client.post("/download/album", json={"id": "MPREb_abc123", "concurrent": 8})
        _, kwargs = mock_album_task.call_args
        assert kwargs["kwargs"]["concurrent"] == 8


class TestDownloadPlaylist:
    def test_playlist_download_returns_job_id(self, test_client, mock_celery_tasks):
        _, mock_playlist_task = mock_celery_tasks
        resp = test_client.post("/download/playlist", json={"id": "VLplay_001", "concurrent": 4})
        assert resp.status_code == 200
        data = resp.json()
        assert "job_id" in data
        assert data["job_id"] > 0

    def test_playlist_download_creates_job_in_db(self, test_client, mock_celery_tasks):
        from db.engine import get_session
        from db.models import Job

        _, mock_playlist_task = mock_celery_tasks
        resp = test_client.post("/download/playlist", json={"id": "VLplay_001"})
        job_id = resp.json()["job_id"]
        with get_session() as session:
            job = session.query(Job).filter(Job.id == job_id).first()
            assert job is not None
            assert job.job_type == "playlist"
            assert job.status == "pending"

    def test_playlist_download_triggers_celery_task(self, test_client, mock_celery_tasks):
        _, mock_playlist_task = mock_celery_tasks
        resp = test_client.post("/download/playlist", json={"id": "VLplay_001"})
        job_id = resp.json()["job_id"]
        mock_playlist_task.assert_called_once()
        args, kwargs = mock_playlist_task.call_args
        assert kwargs["task_id"] == str(job_id)


class TestDownloadValidation:
    def test_concurrent_too_low_returns_422(self, test_client):
        resp = test_client.post("/download/album", json={"id": "MPREb_abc123", "concurrent": 0})
        assert resp.status_code == 422

    def test_concurrent_too_high_returns_422(self, test_client):
        resp = test_client.post("/download/album", json={"id": "MPREb_abc123", "concurrent": 33})
        assert resp.status_code == 422

    def test_missing_id_returns_422(self, test_client):
        resp = test_client.post("/download/album", json={})
        assert resp.status_code == 422
