from db.engine import get_session
from db.models import Job, Song


def _create_job(
    session,
    job_type="album",
    browse_id="MPREb_test",
    status="pending",
    requested_by=None,
    requested_groups=None,
    error=None,
):
    job = Job(
        job_type=job_type,
        browse_id=browse_id,
        status=status,
        message="Test job",
        requested_by=requested_by,
        requested_groups=requested_groups,
        error=error,
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


class TestListJobsFiltered:
    """Tests verifying that jobs are filtered by the authenticated user."""

    def test_authenticated_user_sees_own_jobs(self, test_client, monkeypatch, mocker):
        monkeypatch.setenv("GITHUB_CLIENT_ID", "my-client")

        mocker.patch(
            "api.app._validate_github_token",
            return_value={"sub": "octocat", "name": "Octocat", "provider": "github"},
        )

        with get_session() as session:
            _create_job(session, browse_id="my-job", requested_by="Octocat")
            _create_job(session, browse_id="other-job", requested_by="someone-else")

        resp = test_client.get(
            "/jobs",
            headers={"Authorization": "Bearer ghs_octocat_token"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["browse_id"] == "my-job"

    def test_unauthenticated_user_sees_all_jobs(self, test_client):
        with get_session() as session:
            _create_job(session, browse_id="my-job", requested_by="Octocat")
            _create_job(session, browse_id="other-job", requested_by="someone-else")

        resp = test_client.get("/jobs")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2


def _oidc_auth(monkeypatch, mocker, name="Seeker", sub="auth-1", groups=None):
    monkeypatch.setenv("USE_SOCIAL_LOGIN", "true")
    monkeypatch.setenv("OIDC_ISSUER_URL", "https://idp.example.com")
    monkeypatch.setenv("OIDC_CLIENT_ID", "client-id")
    claims = {"sub": sub, "name": name}
    if groups is not None:
        claims["groups"] = groups
    mocker.patch("api.app._validate_oidc_token", return_value=claims)


class TestGroupVisibility:
    """Tests verifying group-based job visibility for OIDC users."""

    def _auth(self, monkeypatch, mocker, **kwargs):
        _oidc_auth(monkeypatch, mocker, **kwargs)
        return {"Authorization": "Bearer test-token"}

    def test_same_group_user_sees_group_jobs(self, test_client, monkeypatch, mocker):
        headers = self._auth(monkeypatch, mocker, name="Fan", groups=["SEB"])
        with get_session() as session:
            _create_job(session, browse_id="seb-job", requested_by="Fan", requested_groups=",SEB,")
            _create_job(session, browse_id="ibr-job", requested_by="Other", requested_groups=",IBR,")

        resp = test_client.get("/jobs", headers=headers)
        assert resp.status_code == 200
        assert [j["browse_id"] for j in resp.json()] == ["seb-job"]

    def test_system_jobs_visible_to_all_group_users(self, test_client, monkeypatch, mocker):
        headers = self._auth(monkeypatch, mocker, name="User", groups=["SEB"])
        with get_session() as session:
            _create_job(session, browse_id="system-job", requested_by=None, requested_groups=None)
            _create_job(session, browse_id="seb-job", requested_by="Fan", requested_groups=",SEB,")

        resp = test_client.get("/jobs", headers=headers)
        across = {j["browse_id"] for j in resp.json()}
        assert across == {"system-job", "seb-job"}

    def test_user_always_sees_own_jobs_without_groups(self, test_client, monkeypatch, mocker):
        headers = self._auth(monkeypatch, mocker, name="Seeker", groups=["SEB"])
        with get_session() as session:
            _create_job(session, browse_id="legacy-own", requested_by="Seeker", requested_groups=None)
            _create_job(session, browse_id="other", requested_by="Fan", requested_groups=",SEB,")

        resp = test_client.get("/jobs", headers=headers)
        across = {j["browse_id"] for j in resp.json()}
        assert across == {"legacy-own", "other"}

    def test_admin_group_sees_all_jobs(self, test_client, monkeypatch, mocker):
        monkeypatch.setenv("JOB_ADMIN_GROUPS", "IBR")
        headers = self._auth(monkeypatch, mocker, name="Overlord", groups=["IBR"])
        with get_session() as session:
            _create_job(session, browse_id="ibr-job", requested_by="A", requested_groups=",IBR,")
            _create_job(session, browse_id="seb-job", requested_by="B", requested_groups=",SEB,")

        resp = test_client.get("/jobs", headers=headers)
        assert len(resp.json()) == 2

    def test_group_not_in_admin_env_does_not_bypass(self, test_client, monkeypatch, mocker):
        headers = self._auth(monkeypatch, mocker, name="IBRUser", groups=["IBR"])
        with get_session() as session:
            _create_job(session, browse_id="ibr-job", requested_by="A", requested_groups=",IBR,")
            _create_job(session, browse_id="seb-job", requested_by="B", requested_groups=",SEB,")

        resp = test_client.get("/jobs", headers=headers)
        assert [j["browse_id"] for j in resp.json()] == ["ibr-job"]

    def test_get_job_returns_404_for_other_group(self, test_client, monkeypatch, mocker):
        headers = self._auth(monkeypatch, mocker, name="User", groups=["SEB"])
        with get_session() as session:
            ibr = _create_job(session, browse_id="ibr", requested_by="A", requested_groups=",IBR,")
            seb = _create_job(session, browse_id="seb", requested_by="B", requested_groups=",SEB,")

        assert test_client.get(f"/jobs/{ibr.id}", headers=headers).status_code == 404
        assert test_client.get(f"/jobs/{seb.id}", headers=headers).status_code == 200

    def test_get_job_admin_can_read_any_job(self, test_client, monkeypatch, mocker):
        monkeypatch.setenv("JOB_ADMIN_GROUPS", "IBR")
        headers = self._auth(monkeypatch, mocker, name="Overlord", groups=["IBR"])
        with get_session() as session:
            seb = _create_job(session, browse_id="seb", requested_by="B", requested_groups=",SEB,")

        assert test_client.get(f"/jobs/{seb.id}", headers=headers).status_code == 200

    def test_retry_forbidden_for_other_group(self, test_client, monkeypatch, mocker):
        headers = self._auth(monkeypatch, mocker, name="User", groups=["SEB"])
        with get_session() as session:
            ibr = _create_job(session, status="running", requested_by="A", requested_groups=",IBR,")
            _create_song(session, ibr.id, status="failed", error="err")

        resp = test_client.post(f"/jobs/{ibr.id}/retry", headers=headers)
        assert resp.status_code == 403

    def test_retry_allowed_for_same_group(self, test_client, monkeypatch, mocker):
        headers = self._auth(monkeypatch, mocker, name="Fan", groups=["SEB"])
        with get_session() as session:
            seb = _create_job(session, status="running", requested_by="B", requested_groups=",SEB,")
            _create_song(session, seb.id, status="failed", error="err")

        resp = test_client.post(f"/jobs/{seb.id}/retry", headers=headers)
        assert resp.status_code == 200

    def test_retry_allowed_for_admin(self, test_client, monkeypatch, mocker):
        monkeypatch.setenv("JOB_ADMIN_GROUPS", "IBR")
        headers = self._auth(monkeypatch, mocker, name="Overlord", groups=["IBR"])
        with get_session() as session:
            job = _create_job(session, status="running", requested_by="B", requested_groups=",SEB,")
            _create_song(session, job.id, status="failed", error="err")

        resp = test_client.post(f"/jobs/{job.id}/retry", headers=headers)
        assert resp.status_code == 200

    def test_stats_respect_group_visibility(self, test_client, monkeypatch, mocker):
        headers = self._auth(monkeypatch, mocker, name="Fan", groups=["SEB"])
        with get_session() as session:
            _create_job(session, status="success", requested_by="A", requested_groups=",SEB,")
            _create_job(session, status="success", requested_by="B", requested_groups=",IBR,")

        resp = test_client.get("/jobs/stats", headers=headers)
        data = resp.json()
        assert data["total"] == 1
        assert data["success"] == 1


class TestListJobsByStatus:
    """Tests verifying that the ``status`` query parameter filters before pagination."""

    def test_status_all_returns_every_job(self, test_client):
        with get_session() as session:
            _create_job(session, browse_id="p", status="pending")
            _create_job(session, browse_id="r", status="running")
            _create_job(session, browse_id="s", status="success")

        resp = test_client.get("/jobs?status=all")
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    def test_status_pending_returns_only_pending(self, test_client):
        with get_session() as session:
            _create_job(session, browse_id="p", status="pending")
            _create_job(session, browse_id="r", status="running")

        resp = test_client.get("/jobs?status=pending")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["browse_id"] == "p"

    def test_status_running_returns_only_running(self, test_client):
        with get_session() as session:
            _create_job(session, browse_id="p", status="pending")
            _create_job(session, browse_id="r", status="running")

        resp = test_client.get("/jobs?status=running")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["browse_id"] == "r"

    def test_status_failed_returns_only_failed(self, test_client):
        with get_session() as session:
            _create_job(session, browse_id="s", status="success")
            _create_job(session, browse_id="f", status="failed")

        resp = test_client.get("/jobs?status=failed")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["browse_id"] == "f"

    def test_status_success_excludes_partial_jobs(self, test_client):
        with get_session() as session:
            clean = _create_job(session, browse_id="clean", status="success")
            _create_song(session, clean.id, status="success")
            partial = _create_job(session, browse_id="partial", status="success")
            _create_song(session, partial.id, status="success")
            _create_song(session, partial.id, status="failed")

        resp = test_client.get("/jobs?status=success")
        data = resp.json()
        assert [j["browse_id"] for j in data] == ["clean"]

    def test_status_partial_returns_jobs_with_failed_songs(self, test_client):
        with get_session() as session:
            failed = _create_job(session, browse_id="failed", status="failed")
            _create_song(session, failed.id, status="failed")
            partial = _create_job(session, browse_id="partial", status="success")
            _create_song(session, partial.id, status="failed")
            clean = _create_job(session, browse_id="clean", status="success")
            _create_song(session, clean.id, status="success")

        resp = test_client.get("/jobs?status=partial")
        data = resp.json()
        assert {j["browse_id"] for j in data} == {"failed", "partial"}

    def test_status_filter_applies_before_pagination(self, test_client):
        with get_session() as session:
            for i in range(5):
                _create_job(session, browse_id=f"p{i}", status="pending")
                _create_job(session, browse_id=f"r{i}", status="running")

        resp = test_client.get("/jobs?status=pending&limit=3&offset=3")
        data = resp.json()
        assert len(data) == 2
        assert all(j["browse_id"].startswith("p") for j in data)


class TestJobStats:
    def test_stats_return_zeros_when_empty(self, test_client):
        resp = test_client.get("/jobs/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data == {
            "total": 0,
            "pending": 0,
            "running": 0,
            "success": 0,
            "failed": 0,
            "partial": 0,
        }

    def test_stats_counts_job_statuses(self, test_client):
        with get_session() as session:
            _create_job(session, status="pending")
            _create_job(session, status="running")
            _create_job(session, status="success")
            _create_job(session, status="failed")

        resp = test_client.get("/jobs/stats")
        data = resp.json()
        assert data["total"] == 4
        assert data["pending"] == 1
        assert data["running"] == 2
        assert data["success"] == 1
        assert data["failed"] == 1
        assert data["partial"] == 0

    def test_stats_counts_partial_jobs(self, test_client):
        with get_session() as session:
            success_job = _create_job(session, status="success")
            _create_song(session, success_job.id, status="success")
            _create_song(session, success_job.id, status="failed")
            failed_job = _create_job(session, status="failed")
            _create_song(session, failed_job.id, status="failed")
            clean_job = _create_job(session, status="success")
            _create_song(session, clean_job.id, status="success")

        resp = test_client.get("/jobs/stats")
        data = resp.json()
        assert data["total"] == 3
        assert data["success"] == 1
        assert data["failed"] == 1
        assert data["partial"] == 2

    def test_stats_respects_authenticated_user(self, test_client, monkeypatch, mocker):
        monkeypatch.setenv("GITHUB_CLIENT_ID", "my-client")
        mocker.patch(
            "api.app._validate_github_token",
            return_value={"sub": "octocat", "name": "Octocat", "provider": "github"},
        )

        with get_session() as session:
            _create_job(session, status="success", requested_by="Octocat")
            _create_job(session, status="failed", requested_by="Octocat")
            _create_job(session, status="success", requested_by="someone-else")

        resp = test_client.get(
            "/jobs/stats",
            headers={"Authorization": "Bearer ghs_octocat_token"},
        )
        data = resp.json()
        assert data["total"] == 2
        assert data["success"] == 1
        assert data["failed"] == 1


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

    def test_retry_allows_failed_job_without_songs(self, test_client, mock_celery_tasks):
        mock_album_task, _ = mock_celery_tasks
        with get_session() as session:
            job = _create_job(session, status="failed", error="Aborted before songs were inserted")

        resp = test_client.post(f"/jobs/{job.id}/retry")
        assert resp.status_code == 200
        mock_album_task.assert_called_once()

        with get_session() as session:
            job = session.query(Job).filter(Job.id == job.id).first()
            assert job.status == "running"
            assert job.error is None

    def test_retry_returns_404_for_missing_job(self, test_client):
        resp = test_client.post("/jobs/99999/retry")
        assert resp.status_code == 404

    def test_retry_clears_stale_job_error(self, test_client, mock_celery_tasks):
        mock_album_task, _ = mock_celery_tasks
        with get_session() as session:
            job = _create_job(session, status="failed", error="Job stalled — worker likely crashed")
            _create_song(session, job.id, status="failed", error="Worker crashed")

        resp = test_client.post(f"/jobs/{job.id}/retry")
        assert resp.status_code == 200

        with get_session() as session:
            job = session.query(Job).filter(Job.id == job.id).first()
            assert job.status == "running"
            assert job.error is None


class TestDeleteJob:
    def _auth(self, monkeypatch, mocker, **kwargs):
        _oidc_auth(monkeypatch, mocker, **kwargs)
        return {"Authorization": "Bearer test-token"}

    def test_delete_removes_job_and_songs(self, test_client):
        with get_session() as session:
            job = _create_job(session)
            _create_song(session, job.id, title="S1")
            _create_song(session, job.id, title="S2")

        resp = test_client.delete(f"/jobs/{job.id}")
        assert resp.status_code == 200
        assert resp.json() == {"job_id": job.id, "status": "deleted"}

        with get_session() as session:
            assert session.query(Job).filter(Job.id == job.id).first() is None
            assert session.query(Song).filter(Song.job_id == job.id).all() == []

    def test_delete_returns_404_for_missing_job(self, test_client):
        resp = test_client.delete("/jobs/99999")
        assert resp.status_code == 404

    def test_delete_without_social_login_allows_any_job(self, test_client):
        with get_session() as session:
            job = _create_job(session, requested_by="Octocat")

        resp = test_client.delete(f"/jobs/{job.id}")
        assert resp.status_code == 200

    def test_delete_owner_can_delete_own_job(self, test_client, monkeypatch, mocker):
        headers = self._auth(monkeypatch, mocker, name="Fan", groups=["SEB"])
        with get_session() as session:
            own = _create_job(session, requested_by="Fan", requested_groups=",SEB,")
            _create_song(session, own.id, title="S1")

        resp = test_client.delete(f"/jobs/{own.id}", headers=headers)
        assert resp.status_code == 200

        with get_session() as session:
            assert session.query(Song).filter(Song.job_id == own.id).all() == []

    def test_delete_forbidden_for_other_user(self, test_client, monkeypatch, mocker):
        headers = self._auth(monkeypatch, mocker, name="Fan", groups=["SEB"])
        with get_session() as session:
            other = _create_job(session, requested_by="Someone", requested_groups=",SEB,")

        resp = test_client.delete(f"/jobs/{other.id}", headers=headers)
        assert resp.status_code == 403

    def test_delete_forbidden_for_group_member_who_is_not_owner(
        self, test_client, monkeypatch, mocker
    ):
        headers = self._auth(monkeypatch, mocker, name="Bystander", groups=["SEB"])
        with get_session() as session:
            job = _create_job(session, requested_by="Owner", requested_groups=",SEB,")

        resp = test_client.delete(f"/jobs/{job.id}", headers=headers)
        assert resp.status_code == 403

    def test_delete_admin_can_delete_any_job(self, test_client, monkeypatch, mocker):
        monkeypatch.setenv("JOB_ADMIN_GROUPS", "IBR")
        headers = self._auth(monkeypatch, mocker, name="Overlord", groups=["IBR"])
        with get_session() as session:
            job = _create_job(session, requested_by="B", requested_groups=",SEB,")

        resp = test_client.delete(f"/jobs/{job.id}", headers=headers)
        assert resp.status_code == 200

    def test_delete_forbidden_for_user_without_groups(self, test_client, monkeypatch, mocker):
        headers = self._auth(monkeypatch, mocker, name="Loner")
        with get_session() as session:
            job = _create_job(session, requested_by="Someone")

        resp = test_client.delete(f"/jobs/{job.id}", headers=headers)
        assert resp.status_code == 403


class TestCanDeleteFlag:
    """Tests for the ``can_delete`` flag returned with each job."""

    def _auth(self, monkeypatch, mocker, **kwargs):
        _oidc_auth(monkeypatch, mocker, **kwargs)
        return {"Authorization": "Bearer test-token"}

    def test_flag_true_for_everyone_without_social_login(self, test_client):
        with get_session() as session:
            _create_job(session, browse_id="a", requested_by="Octocat")

        data = test_client.get("/jobs").json()
        assert data[0]["can_delete"] is True

    def test_flag_true_for_own_job_in_social_mode(self, test_client, monkeypatch, mocker):
        headers = self._auth(monkeypatch, mocker, name="Fan", groups=["SEB"])
        with get_session() as session:
            _create_job(session, browse_id="own", requested_by="Fan", requested_groups=",SEB,")

        data = test_client.get("/jobs", headers=headers).json()
        assert data[0]["browse_id"] == "own"
        assert data[0]["can_delete"] is True

    def test_flag_false_for_foreign_job_in_social_mode(self, test_client, monkeypatch, mocker):
        headers = self._auth(monkeypatch, mocker, name="Fan", groups=["SEB"])
        with get_session() as session:
            foreign = _create_job(session, browse_id="sys", requested_by=None)

        detail = test_client.get(f"/jobs/{foreign.id}", headers=headers).json()
        assert detail["can_delete"] is False

    def test_flag_true_for_admin_on_any_job(self, test_client, monkeypatch, mocker):
        monkeypatch.setenv("JOB_ADMIN_GROUPS", "IBR")
        headers = self._auth(monkeypatch, mocker, name="Overlord", groups=["IBR"])
        with get_session() as session:
            job = _create_job(session, browse_id="seb", requested_by="B", requested_groups=",SEB,")

        listing = test_client.get("/jobs", headers=headers).json()
        detail = test_client.get(f"/jobs/{job.id}", headers=headers).json()
        assert listing[0]["can_delete"] is True
        assert detail["can_delete"] is True
