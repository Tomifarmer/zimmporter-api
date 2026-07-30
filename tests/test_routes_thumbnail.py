"""Tests for the ``GET /thumbnail`` proxy endpoint."""

from unittest.mock import MagicMock

import pytest
from requests import HTTPError, RequestException


class TestThumbnailProxy:
    def test_proxy_thumbnail_success(self, test_client, mocker):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "image/webp"}
        mock_resp.iter_content.return_value = [b"fake_image_bytes"]
        mock_resp.raise_for_status.return_value = None
        mocker.patch("requests.get", return_value=mock_resp)

        resp = test_client.get("/thumbnail?url=https://lh3.googleusercontent.com/test.jpg")
        assert resp.status_code == 200
        assert resp.content == b"fake_image_bytes"
        assert resp.headers["content-type"] == "image/webp"

    def test_proxy_thumbnail_default_content_type(self, test_client, mocker):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_resp.iter_content.return_value = [b"data"]
        mock_resp.raise_for_status.return_value = None
        mocker.patch("requests.get", return_value=mock_resp)

        resp = test_client.get("/thumbnail?url=https://example.com/img.jpg")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"

    def test_proxy_thumbnail_upstream_fails(self, test_client, mocker):
        http_error = HTTPError("Not Found")
        http_error.response = MagicMock()
        http_error.response.status_code = 404

        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.raise_for_status.side_effect = http_error
        mocker.patch("requests.get", return_value=mock_resp)

        resp = test_client.get("/thumbnail?url=https://example.com/missing.jpg")
        assert resp.status_code == 404

    def test_proxy_thumbnail_upstream_connection_error(self, test_client, mocker):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = RequestException("Connection refused")
        mock_resp.response = None
        mocker.patch("requests.get", return_value=mock_resp)

        resp = test_client.get("/thumbnail?url=https://example.com/down.jpg")
        assert resp.status_code == 502

    def test_proxy_thumbnail_exceeds_max_size(self, test_client, mocker):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "image/jpeg"}
        mock_resp.iter_content.return_value = [b"x" * (10 * 1024 * 1024 + 1)]
        mock_resp.raise_for_status.return_value = None
        mocker.patch("requests.get", return_value=mock_resp)

        resp = test_client.get("/thumbnail?url=https://example.com/huge.jpg")
        assert resp.status_code == 413

    def test_proxy_thumbnail_missing_url(self, test_client):
        resp = test_client.get("/thumbnail")
        assert resp.status_code == 422

    def test_proxy_thumbnail_cache_hit(self, test_client, mocker):
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.hgetall.return_value = {
            b"data": b"cached_image_bytes",
            b"content_type": b"image/webp",
        }
        mocker.patch("redis.Redis.from_url", return_value=mock_redis)

        resp = test_client.get("/thumbnail?url=https://example.com/cached.jpg")
        assert resp.status_code == 200
        assert resp.content == b"cached_image_bytes"
        assert resp.headers["content-type"] == "image/webp"
        assert resp.headers["x-cache"] == "HIT"

    def test_proxy_thumbnail_cache_hit_defaults_content_type(self, test_client, mocker):
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.hgetall.return_value = {
            b"data": b"cached_no_type",
        }
        mocker.patch("redis.Redis.from_url", return_value=mock_redis)

        resp = test_client.get("/thumbnail?url=https://example.com/notype.jpg")
        assert resp.status_code == 200
        assert resp.content == b"cached_no_type"
        assert resp.headers["content-type"] == "image/jpeg"

    def test_proxy_thumbnail_cache_miss_stores_result(self, test_client, mocker):
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.hgetall.return_value = {}
        mocker.patch("redis.Redis.from_url", return_value=mock_redis)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "image/png"}
        mock_resp.iter_content.return_value = [b"fresh_image_data"]
        mock_resp.raise_for_status.return_value = None
        mocker.patch("requests.get", return_value=mock_resp)

        resp = test_client.get("/thumbnail?url=https://example.com/fresh.png")
        assert resp.status_code == 200
        assert resp.content == b"fresh_image_data"
        assert resp.headers["x-cache"] == "MISS"

        mock_redis.hset.assert_called_once()
        mock_redis.expire.assert_called_once()

    def test_proxy_thumbnail_cache_redis_error_falls_through(self, test_client, mocker):
        mock_redis = MagicMock()
        mock_redis.ping.side_effect = Exception("Connection refused")
        mocker.patch("redis.Redis.from_url", return_value=mock_redis)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "image/jpeg"}
        mock_resp.iter_content.return_value = [b"fallback_data"]
        mock_resp.raise_for_status.return_value = None
        mocker.patch("requests.get", return_value=mock_resp)

        resp = test_client.get("/thumbnail?url=https://example.com/fallback.jpg")
        assert resp.status_code == 200
        assert resp.content == b"fallback_data"
        assert resp.headers["x-cache"] == "MISS"
