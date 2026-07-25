class TestHealthEndpoint:
    def test_health_returns_200(self, test_client):
        resp = test_client.get("/health")
        assert resp.status_code == 200

    def test_health_has_status_key(self, test_client):
        resp = test_client.get("/health")
        data = resp.json()
        assert "status" in data
        assert data["status"] in ("ok", "degraded")

    def test_health_has_components(self, test_client):
        resp = test_client.get("/health")
        data = resp.json()
        assert "components" in data
        for component in ("api", "redis", "celery_worker", "mariadb"):
            assert component in data["components"]

    def test_health_has_timestamp(self, test_client):
        resp = test_client.get("/health")
        data = resp.json()
        assert "timestamp" in data

    def test_health_api_component_is_ok(self, test_client):
        resp = test_client.get("/health")
        data = resp.json()
        assert data["components"]["api"] == "ok"

    def test_health_degraded_when_mariadb_fails(self, test_client, mocker):
        mocker.patch("api.app.get_session", side_effect=Exception("DB down"))
        resp = test_client.get("/health")
        data = resp.json()
        assert data["status"] == "degraded"
        assert data["components"]["mariadb"] == "error"
