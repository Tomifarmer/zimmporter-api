import datetime

import pytest

VALID_COOKIE_FILE = (
    b"# Netscape HTTP Cookie File\n"
    b".youtube.com\tTRUE\t/\tTRUE\t1822220532\tSID\tfake_sid_value\n"
    b".google.com\tTRUE\t/\tTRUE\t1822220532\tSIDCC\tfake_sidcc_value\n"
)

NO_YOUTUBE_COOKIE_FILE = b"# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t1822220532\tsome\tvalue\n"


@pytest.fixture
def stored_cookies(monkeypatch):
    """Patch the Valkey cookie store with an in-memory dict."""
    import zimmporter.cookie_store as cookie_store

    store = {"content": None, "modified_at": None}

    def _get_content():
        return store["content"]

    def _get_modified_at():
        return store["modified_at"]

    def _set_content(content, modified_at):
        store["content"] = content
        store["modified_at"] = modified_at

    monkeypatch.setattr(cookie_store, "get_content", _get_content)
    monkeypatch.setattr(cookie_store, "get_modified_at", _get_modified_at)
    monkeypatch.setattr(cookie_store, "set_content", _set_content)
    return store


class TestGetCookies:
    def test_returns_not_exists_when_nothing_stored(self, test_client, stored_cookies):
        resp = test_client.get("/cookies")
        assert resp.status_code == 200
        data = resp.json()
        assert data["exists"] is False
        assert data["cookie_count"] == 0
        assert data["size"] == 0

    def test_returns_metadata_when_cookies_stored(self, test_client, stored_cookies):
        stored_cookies["content"] = VALID_COOKIE_FILE
        stored_cookies["modified_at"] = datetime.datetime(2026, 8, 1, 10, 0, tzinfo=datetime.UTC)
        resp = test_client.get("/cookies")
        assert resp.status_code == 200
        data = resp.json()
        assert data["exists"] is True
        assert data["cookie_count"] == 2
        assert data["size"] == len(VALID_COOKIE_FILE)
        assert any(domain.endswith("youtube.com") for domain in data["domains"])
        assert data["modified_at"] == "2026-08-01T10:00:00Z"

    def test_never_exposes_cookie_values(self, test_client, stored_cookies):
        stored_cookies["content"] = VALID_COOKIE_FILE
        stored_cookies["modified_at"] = datetime.datetime(2026, 8, 1, tzinfo=datetime.UTC)
        data = test_client.get("/cookies").json()
        assert "fake_sid_value" not in str(data)

    def test_not_stale_when_no_flag(self, test_client, stored_cookies):
        stored_cookies["content"] = VALID_COOKIE_FILE
        stored_cookies["modified_at"] = datetime.datetime(2026, 8, 1, tzinfo=datetime.UTC)
        data = test_client.get("/cookies").json()
        assert data["is_stale"] is False

    def test_stale_when_flag_set(self, test_client, stored_cookies, monkeypatch):
        stored_cookies["content"] = VALID_COOKIE_FILE
        stored_cookies["modified_at"] = datetime.datetime(2026, 8, 1, tzinfo=datetime.UTC)
        monkeypatch.setattr("api.routes.cookies.cookie_health.is_stale", lambda: True)
        data = test_client.get("/cookies").json()
        assert data["is_stale"] is True

    def test_stale_when_session_cookie_expired(self, test_client, stored_cookies):
        expired = b"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t1000000000\tSID\tfake_sid_value\n"
        stored_cookies["content"] = expired
        stored_cookies["modified_at"] = datetime.datetime(2026, 8, 1, tzinfo=datetime.UTC)
        data = test_client.get("/cookies").json()
        assert data["exists"] is True
        assert data["is_stale"] is True


class TestUploadCookies:
    def test_upload_valid_file(self, test_client, stored_cookies):
        resp = test_client.post("/cookies", files={"file": ("cookies.txt", VALID_COOKIE_FILE, "text/plain")})
        assert resp.status_code == 200
        data = resp.json()
        assert data["exists"] is True
        assert data["cookie_count"] == 2
        assert any(domain.endswith("youtube.com") for domain in data["domains"])
        assert stored_cookies["content"] == VALID_COOKIE_FILE
        assert stored_cookies["modified_at"] is not None

    def test_upload_replaces_existing_content(self, test_client, stored_cookies):
        stored_cookies["content"] = NO_YOUTUBE_COOKIE_FILE
        resp = test_client.post("/cookies", files={"file": ("cookies.txt", VALID_COOKIE_FILE, "text/plain")})
        assert resp.status_code == 200
        assert stored_cookies["content"] == VALID_COOKIE_FILE

    def test_rejects_garbage_content(self, test_client, stored_cookies):
        resp = test_client.post("/cookies", files={"file": ("cookies.txt", b"this is not a cookie file", "text/plain")})
        assert resp.status_code == 400
        assert stored_cookies["content"] is None

    def test_rejects_file_without_youtube_cookies(self, test_client, stored_cookies):
        resp = test_client.post(
            "/cookies",
            files={"file": ("cookies.txt", NO_YOUTUBE_COOKIE_FILE, "text/plain")},
        )
        assert resp.status_code == 400
        assert stored_cookies["content"] is None

    def test_rejects_empty_file(self, test_client, stored_cookies):
        resp = test_client.post("/cookies", files={"file": ("cookies.txt", b"", "text/plain")})
        assert resp.status_code == 400

    def test_rejects_oversized_file(self, test_client, stored_cookies, monkeypatch):
        monkeypatch.setattr("api.routes.cookies._MAX_COOKIE_SIZE", 64)
        resp = test_client.post("/cookies", files={"file": ("cookies.txt", b"x" * 128, "text/plain")})
        assert resp.status_code == 413
        assert stored_cookies["content"] is None

    def test_upload_clears_stale_flag(self, test_client, stored_cookies, monkeypatch):
        from zimmporter import cookie_health

        monkeypatch.setattr("api.routes.cookies.cookie_health.is_stale", lambda: True)
        clear = []
        monkeypatch.setattr(cookie_health, "clear_stale", lambda: clear.append(True))
        resp = test_client.post("/cookies", files={"file": ("cookies.txt", VALID_COOKIE_FILE, "text/plain")})
        assert resp.status_code == 200
        assert clear == [True]

    def test_upload_works_without_writable_tempdir(self, test_client, stored_cookies, monkeypatch):
        """Upload must not depend on a writable /tmp (read-only root FS pods)."""
        import tempfile

        def _no_tempdir():
            raise FileNotFoundError("No usable temporary directory found")

        monkeypatch.setattr(tempfile, "gettempdir", _no_tempdir)
        monkeypatch.setattr(tempfile, "NamedTemporaryFile", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
        resp = test_client.post(
            "/cookies",
            files={"file": ("export-2026-08-07.txt", VALID_COOKIE_FILE, "text/plain")},
        )
        assert resp.status_code == 200, resp.text
        assert stored_cookies["content"] == VALID_COOKIE_FILE
