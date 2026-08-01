import os

import pytest

VALID_COOKIE_FILE = (
    b"# Netscape HTTP Cookie File\n"
    b".youtube.com\tTRUE\t/\tTRUE\t1822220532\tSID\tfake_sid_value\n"
    b".google.com\tTRUE\t/\tTRUE\t1822220532\tSIDCC\tfake_sidcc_value\n"
)

NO_YOUTUBE_COOKIE_FILE = b"# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t1822220532\tsome\tvalue\n"


@pytest.fixture
def cookie_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("api.routes.cookies.COOKIE_DIR", str(tmp_path))
    return tmp_path


class TestGetCookies:
    def test_returns_not_exists_when_no_file(self, test_client, cookie_dir):
        resp = test_client.get("/cookies")
        assert resp.status_code == 200
        data = resp.json()
        assert data["exists"] is False
        assert data["cookie_count"] == 0
        assert data["size"] == 0

    def test_returns_metadata_when_file_present(self, test_client, cookie_dir):
        (cookie_dir / "cookies.txt").write_bytes(VALID_COOKIE_FILE)
        resp = test_client.get("/cookies")
        assert resp.status_code == 200
        data = resp.json()
        assert data["exists"] is True
        assert data["cookie_count"] == 2
        assert data["size"] == len(VALID_COOKIE_FILE)
        assert any(domain.endswith("youtube.com") for domain in data["domains"])
        assert data["modified_at"] is not None

    def test_never_exposes_cookie_values(self, test_client, cookie_dir):
        (cookie_dir / "cookies.txt").write_bytes(VALID_COOKIE_FILE)
        data = test_client.get("/cookies").json()
        assert "fake_sid_value" not in str(data)

    def test_not_stale_when_no_flag(self, test_client, cookie_dir):
        (cookie_dir / "cookies.txt").write_bytes(VALID_COOKIE_FILE)
        data = test_client.get("/cookies").json()
        assert data["is_stale"] is False

    def test_stale_when_flag_set(self, test_client, cookie_dir, monkeypatch):
        (cookie_dir / "cookies.txt").write_bytes(VALID_COOKIE_FILE)
        monkeypatch.setattr("api.routes.cookies.cookie_health.is_stale", lambda: True)
        data = test_client.get("/cookies").json()
        assert data["is_stale"] is True

    def test_stale_when_session_cookie_expired(self, test_client, cookie_dir):
        expired = (
            b"# Netscape HTTP Cookie File\n"
            b".youtube.com\tTRUE\t/\tTRUE\t1000000000\tSID\tfake_sid_value\n"
        )
        (cookie_dir / "cookies.txt").write_bytes(expired)
        data = test_client.get("/cookies").json()
        assert data["exists"] is True
        assert data["is_stale"] is True


class TestUploadCookies:
    def test_upload_valid_file(self, test_client, cookie_dir):
        resp = test_client.post("/cookies", files={"file": ("cookies.txt", VALID_COOKIE_FILE, "text/plain")})
        assert resp.status_code == 200
        data = resp.json()
        assert data["exists"] is True
        assert data["cookie_count"] == 2
        assert any(domain.endswith("youtube.com") for domain in data["domains"])
        stored = (cookie_dir / "cookies.txt").read_bytes()
        assert stored == VALID_COOKIE_FILE

    def test_upload_replaces_existing_file(self, test_client, cookie_dir):
        (cookie_dir / "cookies.txt").write_bytes(NO_YOUTUBE_COOKIE_FILE)
        resp = test_client.post("/cookies", files={"file": ("cookies.txt", VALID_COOKIE_FILE, "text/plain")})
        assert resp.status_code == 200
        stored = (cookie_dir / "cookies.txt").read_bytes()
        assert stored == VALID_COOKIE_FILE

    def test_rejects_garbage_content(self, test_client, cookie_dir):
        resp = test_client.post("/cookies", files={"file": ("cookies.txt", b"this is not a cookie file", "text/plain")})
        assert resp.status_code == 400

    def test_rejects_file_without_youtube_cookies(self, test_client, cookie_dir):
        resp = test_client.post(
            "/cookies",
            files={"file": ("cookies.txt", NO_YOUTUBE_COOKIE_FILE, "text/plain")},
        )
        assert resp.status_code == 400
        assert not (cookie_dir / "cookies.txt").exists()

    def test_rejects_empty_file(self, test_client, cookie_dir):
        resp = test_client.post("/cookies", files={"file": ("cookies.txt", b"", "text/plain")})
        assert resp.status_code == 400

    def test_rejects_oversized_file(self, test_client, cookie_dir, monkeypatch):
        monkeypatch.setattr("api.routes.cookies._MAX_COOKIE_SIZE", 64)
        resp = test_client.post("/cookies", files={"file": ("cookies.txt", b"x" * 128, "text/plain")})
        assert resp.status_code == 413

    def test_upload_does_not_leave_temp_file(self, test_client, cookie_dir):
        test_client.post("/cookies", files={"file": ("cookies.txt", VALID_COOKIE_FILE, "text/plain")})
        assert [p.name for p in cookie_dir.iterdir()] == ["cookies.txt"]
        assert os.path.exists(cookie_dir / "cookies.txt")

    def test_upload_clears_stale_flag(self, test_client, cookie_dir, monkeypatch):
        from zimmporter import cookie_health

        monkeypatch.setattr("api.routes.cookies.cookie_health.is_stale", lambda: True)
        clear = []
        monkeypatch.setattr(cookie_health, "clear_stale", lambda: clear.append(True))
        resp = test_client.post("/cookies", files={"file": ("cookies.txt", VALID_COOKIE_FILE, "text/plain")})
        assert resp.status_code == 200
        assert clear == [True]
