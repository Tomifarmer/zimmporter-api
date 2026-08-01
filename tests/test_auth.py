class TestAuthDisabled:
    """When neither USE_SIMPLE_AUTH nor USE_SOCIAL_LOGIN is set, all requests pass."""

    def test_search_passes_without_credentials(self, test_client):
        resp = test_client.get("/search?q=aurora")
        assert resp.status_code == 200

    def test_health_passes_without_credentials(self, test_client):
        resp = test_client.get("/health")
        assert resp.status_code == 200


class TestApiKeyAuth:
    """Tests for USE_SIMPLE_AUTH=true with X-API-Key header."""

    def test_missing_key_returns_401(self, test_client, monkeypatch):
        monkeypatch.setenv("USE_SIMPLE_AUTH", "true")
        monkeypatch.setenv("API_KEY", "secret-123")

        resp = test_client.get("/search?q=aurora")
        assert resp.status_code == 401

    def test_wrong_key_returns_401(self, test_client, monkeypatch):
        monkeypatch.setenv("USE_SIMPLE_AUTH", "true")
        monkeypatch.setenv("API_KEY", "secret-123")

        resp = test_client.get("/search?q=aurora", headers={"X-API-Key": "wrong-key"})
        assert resp.status_code == 401

    def test_correct_key_passes(self, test_client, monkeypatch):
        monkeypatch.setenv("USE_SIMPLE_AUTH", "true")
        monkeypatch.setenv("API_KEY", "secret-123")

        resp = test_client.get("/search?q=aurora", headers={"X-API-Key": "secret-123"})
        assert resp.status_code == 200

    def test_health_bypasses_api_key_auth(self, test_client, monkeypatch):
        monkeypatch.setenv("USE_SIMPLE_AUTH", "true")
        monkeypatch.setenv("API_KEY", "secret-123")

        resp = test_client.get("/health")
        assert resp.status_code == 200


class TestOidcAuth:
    """Tests for USE_SOCIAL_LOGIN=true with Authorization: Bearer header."""

    def test_missing_token_returns_401(self, test_client, monkeypatch):
        monkeypatch.setenv("USE_SOCIAL_LOGIN", "true")
        monkeypatch.setenv("OIDC_ISSUER_URL", "https://example.com/oidc")
        monkeypatch.setenv("OIDC_CLIENT_ID", "my-client")

        resp = test_client.get("/search?q=aurora")
        assert resp.status_code == 401
        assert "authentication token" in resp.json()["detail"]

    def test_invalid_token_returns_401(self, test_client, monkeypatch, mocker):
        monkeypatch.setenv("USE_SOCIAL_LOGIN", "true")
        monkeypatch.setenv("OIDC_ISSUER_URL", "https://example.com/oidc")
        monkeypatch.setenv("OIDC_CLIENT_ID", "my-client")
        mocker.patch("api.app._validate_oidc_token", return_value=None)

        resp = test_client.get(
            "/search?q=aurora",
            headers={"Authorization": "Bearer invalid-token"},
        )
        assert resp.status_code == 401

    def test_valid_token_passes(self, test_client, monkeypatch, mocker):
        monkeypatch.setenv("USE_SOCIAL_LOGIN", "true")
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
        monkeypatch.setenv("USE_SOCIAL_LOGIN", "true")
        monkeypatch.setenv("OIDC_ISSUER_URL", "https://example.com/oidc")
        monkeypatch.setenv("OIDC_CLIENT_ID", "my-client")

        resp = test_client.get("/health")
        assert resp.status_code == 200


class TestDualAuth:
    """Both USE_SIMPLE_AUTH and USE_SOCIAL_LOGIN enabled — either method suffices."""

    def test_api_key_method_passes(self, test_client, monkeypatch):
        monkeypatch.setenv("USE_SIMPLE_AUTH", "true")
        monkeypatch.setenv("API_KEY", "secret-123")
        monkeypatch.setenv("USE_SOCIAL_LOGIN", "true")
        monkeypatch.setenv("OIDC_ISSUER_URL", "https://example.com/oidc")
        monkeypatch.setenv("OIDC_CLIENT_ID", "my-client")

        resp = test_client.get("/search?q=aurora", headers={"X-API-Key": "secret-123"})
        assert resp.status_code == 200

    def test_bearer_method_passes(self, test_client, monkeypatch, mocker):
        monkeypatch.setenv("USE_SIMPLE_AUTH", "true")
        monkeypatch.setenv("API_KEY", "secret-123")
        monkeypatch.setenv("USE_SOCIAL_LOGIN", "true")
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
        monkeypatch.setenv("USE_SIMPLE_AUTH", "true")
        monkeypatch.setenv("API_KEY", "secret-123")
        monkeypatch.setenv("USE_SOCIAL_LOGIN", "true")
        monkeypatch.setenv("OIDC_ISSUER_URL", "https://example.com/oidc")
        monkeypatch.setenv("OIDC_CLIENT_ID", "my-client")

        resp = test_client.get("/search?q=aurora")
        assert resp.status_code == 401


class TestValidateOidcToken:
    """Unit tests for _validate_oidc_token directly."""

    def test_returns_none_when_oidc_disabled(self, monkeypatch):
        monkeypatch.delenv("USE_SOCIAL_LOGIN", raising=False)
        from api.app import _validate_oidc_token

        assert _validate_oidc_token("some-token") is None

    def test_returns_claims_for_valid_token(self, monkeypatch, mocker):
        monkeypatch.setenv("USE_SOCIAL_LOGIN", "true")
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
        monkeypatch.setenv("USE_SOCIAL_LOGIN", "true")
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
        monkeypatch.setenv("USE_SOCIAL_LOGIN", "true")
        monkeypatch.setenv("OIDC_ISSUER_URL", "https://example.com/oidc")
        monkeypatch.setenv("OIDC_CLIENT_ID", "my-client")

        mocker.patch("api.app._get_jwks_client", return_value=None)

        from api.app import _validate_oidc_token

        assert _validate_oidc_token("any-token") is None


class TestValidateGitHubToken:
    """Unit tests for _validate_github_token directly."""

    def test_returns_none_when_no_client_id(self, monkeypatch):
        monkeypatch.delenv("GITHUB_CLIENT_ID", raising=False)
        from api.app import _validate_github_token

        assert _validate_github_token("some-token") is None

    def test_returns_none_on_request_error(self, monkeypatch, mocker):
        monkeypatch.setenv("GITHUB_CLIENT_ID", "my-client")
        from requests.exceptions import RequestException

        mocker.patch("requests.get", side_effect=RequestException("network error"))

        from api.app import _validate_github_token

        assert _validate_github_token("ghs_some_token") is None

    def test_returns_none_on_non_200(self, monkeypatch, mocker):
        monkeypatch.setenv("GITHUB_CLIENT_ID", "my-client")
        mock_resp = mocker.MagicMock()
        mock_resp.status_code = 401
        mocker.patch("requests.get", return_value=mock_resp)

        from api.app import _validate_github_token

        assert _validate_github_token("ghs_bad_token") is None

    def test_returns_none_when_no_login(self, monkeypatch, mocker):
        monkeypatch.setenv("GITHUB_CLIENT_ID", "my-client")
        mock_resp = mocker.MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": 12345}
        mocker.patch("requests.get", return_value=mock_resp)

        from api.app import _validate_github_token

        assert _validate_github_token("ghs_no_login") is None

    def test_returns_claims_for_valid_token(self, monkeypatch, mocker):
        monkeypatch.setenv("GITHUB_CLIENT_ID", "my-client")
        mock_resp = mocker.MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "login": "octocat",
            "name": "Octo Cat",
            "email": "octocat@github.com",
        }
        mocker.patch("requests.get", return_value=mock_resp)

        from api.app import _validate_github_token

        result = _validate_github_token("ghs_valid_token")
        assert result == {
            "sub": "octocat",
            "name": "Octo Cat",
            "email": "octocat@github.com",
            "provider": "github",
        }

    def test_falls_back_to_login_when_no_name(self, monkeypatch, mocker):
        monkeypatch.setenv("GITHUB_CLIENT_ID", "my-client")
        mock_resp = mocker.MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"login": "octocat", "email": "octocat@github.com"}
        mocker.patch("requests.get", return_value=mock_resp)

        from api.app import _validate_github_token

        result = _validate_github_token("ghs_valid_token")
        assert result == {
            "sub": "octocat",
            "name": "octocat",
            "email": "octocat@github.com",
            "provider": "github",
        }


class TestGitHubAuthIntegration:
    """Integration tests for GitHub Bearer token auth in the middleware."""

    def test_github_token_auth_works(self, test_client, monkeypatch, mocker):
        monkeypatch.setenv("GITHUB_CLIENT_ID", "my-client")
        mocker.patch(
            "api.app._validate_github_token",
            return_value={"sub": "octocat", "name": "Octo Cat", "provider": "github"},
        )

        resp = test_client.get("/search?q=aurora", headers={"Authorization": "Bearer ghs_token"})
        assert resp.status_code == 200

    def test_github_token_fallback_after_oidc_fails(self, test_client, monkeypatch, mocker):
        monkeypatch.setenv("USE_SOCIAL_LOGIN", "true")
        monkeypatch.setenv("OIDC_ISSUER_URL", "https://example.com/oidc")
        monkeypatch.setenv("OIDC_CLIENT_ID", "my-client")
        monkeypatch.setenv("GITHUB_CLIENT_ID", "my-client")
        mocker.patch("api.app._validate_oidc_token", return_value=None)
        mocker.patch(
            "api.app._validate_github_token",
            return_value={"sub": "octocat", "name": "Octo Cat", "provider": "github"},
        )

        resp = test_client.get("/search?q=aurora", headers={"Authorization": "Bearer ghs_token"})
        assert resp.status_code == 200

    def test_github_only_no_oidc(self, test_client, monkeypatch, mocker):
        monkeypatch.setenv("GITHUB_CLIENT_ID", "my-client")
        mocker.patch(
            "api.app._validate_github_token",
            return_value={"sub": "octocat", "name": "Octo Cat", "provider": "github"},
        )

        resp = test_client.get("/search?q=aurora", headers={"Authorization": "Bearer ghs_token"})
        assert resp.status_code == 200

    def test_github_missing_token_returns_401(self, test_client, monkeypatch):
        monkeypatch.setenv("GITHUB_CLIENT_ID", "my-client")

        resp = test_client.get("/search?q=aurora")
        assert resp.status_code == 401
        assert "authentication token" in resp.json()["detail"]

    def test_github_bad_token_returns_401(self, test_client, monkeypatch, mocker):
        monkeypatch.setenv("GITHUB_CLIENT_ID", "my-client")
        mocker.patch("api.app._validate_github_token", return_value=None)

        resp = test_client.get("/search?q=aurora", headers={"Authorization": "Bearer bad_ghs"})
        assert resp.status_code == 401

    def test_dont_try_bearer_when_only_api_key(self, test_client, monkeypatch):
        monkeypatch.setenv("USE_SIMPLE_AUTH", "true")
        monkeypatch.setenv("API_KEY", "secret-123")

        resp = test_client.get("/search?q=aurora")
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid or missing API key"
