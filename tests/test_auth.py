import pytest


class TestAuthDisabled:
    """When neither REQUIRE_AUTH nor OIDC_ENABLED is set, all requests pass."""

    def test_search_passes_without_credentials(self, test_client):
        resp = test_client.get("/search?q=aurora")
        assert resp.status_code == 200

    def test_health_passes_without_credentials(self, test_client):
        resp = test_client.get("/health")
        assert resp.status_code == 200


class TestApiKeyAuth:
    """Tests for REQUIRE_AUTH=true with X-API-Key header."""

    def test_missing_key_returns_401(self, test_client, monkeypatch):
        monkeypatch.setenv("REQUIRE_AUTH", "true")
        monkeypatch.setenv("API_KEY", "secret-123")

        resp = test_client.get("/search?q=aurora")
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid or missing API key"

    def test_wrong_key_returns_401(self, test_client, monkeypatch):
        monkeypatch.setenv("REQUIRE_AUTH", "true")
        monkeypatch.setenv("API_KEY", "secret-123")

        resp = test_client.get("/search?q=aurora", headers={"X-API-Key": "wrong-key"})
        assert resp.status_code == 401

    def test_correct_key_passes(self, test_client, monkeypatch):
        monkeypatch.setenv("REQUIRE_AUTH", "true")
        monkeypatch.setenv("API_KEY", "secret-123")

        resp = test_client.get("/search?q=aurora", headers={"X-API-Key": "secret-123"})
        assert resp.status_code == 200

    def test_health_bypasses_api_key_auth(self, test_client, monkeypatch):
        monkeypatch.setenv("REQUIRE_AUTH", "true")
        monkeypatch.setenv("API_KEY", "secret-123")

        resp = test_client.get("/health")
        assert resp.status_code == 200


class TestOidcAuth:
    """Tests for OIDC_ENABLED=true with Authorization: Bearer header."""

    def test_missing_token_returns_401(self, test_client, monkeypatch):
        monkeypatch.setenv("OIDC_ENABLED", "true")
        monkeypatch.setenv("OIDC_ISSUER_URL", "https://example.com/oidc")
        monkeypatch.setenv("OIDC_CLIENT_ID", "my-client")

        resp = test_client.get("/search?q=aurora")
        assert resp.status_code == 401
        assert "OIDC token" in resp.json()["detail"]

    def test_invalid_token_returns_401(self, test_client, monkeypatch, mocker):
        monkeypatch.setenv("OIDC_ENABLED", "true")
        monkeypatch.setenv("OIDC_ISSUER_URL", "https://example.com/oidc")
        monkeypatch.setenv("OIDC_CLIENT_ID", "my-client")
        mocker.patch("api.app._validate_oidc_token", return_value=None)

        resp = test_client.get(
            "/search?q=aurora",
            headers={"Authorization": "Bearer invalid-token"},
        )
        assert resp.status_code == 401

    def test_valid_token_passes(self, test_client, monkeypatch, mocker):
        monkeypatch.setenv("OIDC_ENABLED", "true")
        monkeypatch.setenv("OIDC_ISSUER_URL", "https://example.com/oidc")
        monkeypatch.setenv("OIDC_CLIENT_ID", "my-client")
        mocker.patch(
            "api.app._validate_oidc_token",
            return_value={"sub": "user123", "email": "user@example.com"},
        )

        resp = test_client.get(
            "/search?q=aurora",
            headers={"Authorization": "Bearer valid-token"},
        )
        assert resp.status_code == 200

    def test_health_bypasses_oidc_auth(self, test_client, monkeypatch):
        monkeypatch.setenv("OIDC_ENABLED", "true")
        monkeypatch.setenv("OIDC_ISSUER_URL", "https://example.com/oidc")
        monkeypatch.setenv("OIDC_CLIENT_ID", "my-client")

        resp = test_client.get("/health")
        assert resp.status_code == 200


class TestDualAuth:
    """Both REQUIRE_AUTH and OIDC_ENABLED enabled — either method suffices."""

    def test_api_key_method_passes(self, test_client, monkeypatch):
        monkeypatch.setenv("REQUIRE_AUTH", "true")
        monkeypatch.setenv("API_KEY", "secret-123")
        monkeypatch.setenv("OIDC_ENABLED", "true")
        monkeypatch.setenv("OIDC_ISSUER_URL", "https://example.com/oidc")
        monkeypatch.setenv("OIDC_CLIENT_ID", "my-client")

        resp = test_client.get("/search?q=aurora", headers={"X-API-Key": "secret-123"})
        assert resp.status_code == 200

    def test_bearer_method_passes(self, test_client, monkeypatch, mocker):
        monkeypatch.setenv("REQUIRE_AUTH", "true")
        monkeypatch.setenv("API_KEY", "secret-123")
        monkeypatch.setenv("OIDC_ENABLED", "true")
        monkeypatch.setenv("OIDC_ISSUER_URL", "https://example.com/oidc")
        monkeypatch.setenv("OIDC_CLIENT_ID", "my-client")
        mocker.patch(
            "api.app._validate_oidc_token",
            return_value={"sub": "user123"},
        )

        resp = test_client.get(
            "/search?q=aurora",
            headers={"Authorization": "Bearer valid-token"},
        )
        assert resp.status_code == 200

    def test_no_credentials_returns_401(self, test_client, monkeypatch):
        monkeypatch.setenv("REQUIRE_AUTH", "true")
        monkeypatch.setenv("API_KEY", "secret-123")
        monkeypatch.setenv("OIDC_ENABLED", "true")
        monkeypatch.setenv("OIDC_ISSUER_URL", "https://example.com/oidc")
        monkeypatch.setenv("OIDC_CLIENT_ID", "my-client")

        resp = test_client.get("/search?q=aurora")
        assert resp.status_code == 401


class TestValidateOidcToken:
    """Unit tests for _validate_oidc_token directly."""

    def test_returns_none_when_oidc_disabled(self, monkeypatch):
        monkeypatch.delenv("OIDC_ENABLED", raising=False)
        from api.app import _validate_oidc_token

        assert _validate_oidc_token("some-token") is None

    def test_returns_claims_for_valid_token(self, monkeypatch, mocker):
        monkeypatch.setenv("OIDC_ENABLED", "true")
        monkeypatch.setenv("OIDC_ISSUER_URL", "https://example.com/oidc")
        monkeypatch.setenv("OIDC_CLIENT_ID", "my-client")

        mock_client = mocker.MagicMock()
        mock_client.get_signing_key_from_jwt.return_value.key = "mock-key"
        mocker.patch("api.app._get_jwks_client", return_value=mock_client)

        expected_claims = {"sub": "user123", "iss": "https://example.com/oidc"}
        mocker.patch("jwt.decode", return_value=expected_claims)

        from api.app import _validate_oidc_token

        assert _validate_oidc_token("valid.jwt.token") == expected_claims

    def test_returns_none_on_decode_error(self, monkeypatch, mocker):
        monkeypatch.setenv("OIDC_ENABLED", "true")
        monkeypatch.setenv("OIDC_ISSUER_URL", "https://example.com/oidc")
        monkeypatch.setenv("OIDC_CLIENT_ID", "my-client")

        import jwt as pyjwt

        mock_client = mocker.MagicMock()
        mock_client.get_signing_key_from_jwt.return_value.key = "mock-key"
        mocker.patch("api.app._get_jwks_client", return_value=mock_client)
        mocker.patch("jwt.decode", side_effect=pyjwt.PyJWTError("bad token"))

        from api.app import _validate_oidc_token

        assert _validate_oidc_token("bad.jwt.token") is None

    def test_returns_none_when_no_jwks_client(self, monkeypatch, mocker):
        monkeypatch.setenv("OIDC_ENABLED", "true")
        monkeypatch.setenv("OIDC_ISSUER_URL", "https://example.com/oidc")
        monkeypatch.setenv("OIDC_CLIENT_ID", "my-client")

        mocker.patch("api.app._get_jwks_client", return_value=None)

        from api.app import _validate_oidc_token

        assert _validate_oidc_token("any-token") is None
