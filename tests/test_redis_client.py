"""Tests for :mod:`zimmporter.redis_client` db selection."""

from unittest.mock import MagicMock


def test_get_redis_injects_db_into_url(monkeypatch):
    import zimmporter.redis_client as redis_client
    import tasks.celery_app as celery_module

    captured = {}
    mock_client = MagicMock()

    def _fake_from_url(url, **kwargs):
        captured["url"] = url
        return mock_client

    monkeypatch.setattr(redis_client.Redis, "from_url", staticmethod(_fake_from_url))
    monkeypatch.setattr(
        celery_module.celery_app.conf,
        "broker_url",
        "redis://valkey:6379/0",
    )

    client = redis_client.get_redis(3)

    assert client is mock_client
    assert captured["url"] == "redis://valkey:6379/3"


def test_get_redis_defaults_to_db0(monkeypatch):
    import zimmporter.redis_client as redis_client
    import tasks.celery_app as celery_module

    captured = {}
    mock_client = MagicMock()

    def _fake_from_url(url, **kwargs):
        captured["url"] = url
        return mock_client

    monkeypatch.setattr(redis_client.Redis, "from_url", staticmethod(_fake_from_url))
    monkeypatch.setattr(
        celery_module.celery_app.conf,
        "broker_url",
        "redis://valkey:6379/0",
    )

    redis_client.get_redis()

    assert captured["url"] == "redis://valkey:6379/0"
