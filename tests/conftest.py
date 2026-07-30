import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from tests.mock_data import (
    MOCK_ALBUM_DATA,
    MOCK_ALBUM_DATA_NO_YEAR,
    MOCK_ALBUM_DATA_WITH_SLASHES,
    MOCK_PLAYLIST_DATA,
    MOCK_SEARCH_RESPONSE,
)

os.environ.setdefault("CELERY_BROKER", "redis://localhost:6379/0")
os.environ.setdefault("CELERY_BACKEND", "redis://localhost:6379/1")
os.environ.setdefault("AWS_ENDPOINT_URL", "http://localhost:9000")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test_access")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test_secret")
os.environ.setdefault("AWS_BUCKET", "test_bucket")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_USE_SSL", "false")
os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_PORT", "3306")
os.environ.setdefault("DB_USER", "root")
os.environ.setdefault("DB_PASS", "root")
os.environ.setdefault("DB_NAME", "zimmporter_test")

_patches = []

_ytm_instance = MagicMock()
_ytm_instance.search.return_value = MOCK_SEARCH_RESPONSE


def _get_album_side_effect(browse_id):
    if browse_id == "MPREb_slash":
        return MOCK_ALBUM_DATA_WITH_SLASHES
    if browse_id == "MPREb_noyear":
        return MOCK_ALBUM_DATA_NO_YEAR
    return MOCK_ALBUM_DATA


_ytm_instance.get_album.side_effect = _get_album_side_effect
_ytm_instance.get_playlist.return_value = MOCK_PLAYLIST_DATA

_patches.append(patch("ytmusicapi.YTMusic", return_value=_ytm_instance))

_celery_mock_app = MagicMock()
_celery_mock_app.conf.broker_url = "redis://localhost:6379/0"
_celery_mock_app.conf.backend_url = "redis://localhost:6379/1"
_patches.append(patch("celery.Celery", return_value=_celery_mock_app))

_redis_instance = MagicMock()
_redis_instance.ping.return_value = True
_redis_instance.get.return_value = None
_redis_instance.set.return_value = True
_patches.append(patch("redis.Redis", return_value=_redis_instance))
_patches.append(patch("redis.Redis.from_url", return_value=_redis_instance))
_patches.append(patch("redis.from_url", return_value=_redis_instance))

_boto3_client = MagicMock()
_boto3_session = MagicMock()
_boto3_session.client.return_value = _boto3_client
_patches.append(patch("boto3.Session", return_value=_boto3_session))

_mock_response = MagicMock()
_mock_response.content = b"fake_image_bytes"
_mock_response.iter_content.return_value = [b"fake_image_bytes"]
_patches.append(patch("requests.get", return_value=_mock_response))

_mock_pool = MagicMock()
_mock_pool.__enter__.return_value = _mock_pool
_patches.append(patch("billiard.Pool", return_value=_mock_pool))

mock_ydl_instance = MagicMock()
mock_ydl_context = MagicMock()
mock_ydl_context.__enter__.return_value = mock_ydl_instance
_patches.append(patch("yt_dlp.YoutubeDL", return_value=mock_ydl_context))

for p in _patches:
    p.start()


def pytest_unconfigure():
    for p in _patches:
        p.stop()


@pytest.fixture(autouse=True)
def _reset_ytdl_opts():
    import zimmporter.core as core_module

    core_module.YTDL_OPTS.update(
        {
            "format": "bestaudio",
            "addmetadata": True,
            "writethumbnail": True,
            "js_runtimes": {"deno": {"path": "/usr/bin/deno"}},
            "cachedir": "/tmp/yt-dlp-cache",
            "skip_download_incomplete_files": True,
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "aac", "preferredquality": "best"},
                {"key": "FFmpegMetadata", "add_metadata": True},
                {"key": "EmbedThumbnail", "already_have_thumbnail": False},
            ],
        }
    )


@pytest.fixture(autouse=True)
def _reset_mocks():
    _boto3_client.reset_mock()
    _boto3_session.reset_mock()


@pytest.fixture(autouse=True)
def _reset_temp_dir():
    import zimmporter.core as core_module

    core_module.temp_dir = tempfile.gettempdir() + "/zimmporter_test/"


@pytest.fixture
def sqlite_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import scoped_session, sessionmaker
    from sqlalchemy.pool import StaticPool

    import db.engine as db_engine_module
    from db.models import Base

    sqlite_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    db_engine_module.engine = sqlite_engine
    db_engine_module.session_factory = sessionmaker(bind=sqlite_engine, expire_on_commit=False)
    db_engine_module.ScopedSession = scoped_session(db_engine_module.session_factory)
    Base.metadata.create_all(sqlite_engine)
    yield
    Base.metadata.drop_all(sqlite_engine)


@pytest.fixture
def test_client(sqlite_db):
    from fastapi.testclient import TestClient

    from api.app import app

    with TestClient(app) as client:
        yield client


@pytest.fixture
def mock_yt_dlp():
    with patch("yt_dlp.YoutubeDL") as mock_ydl_class:
        mock_ydl_instance = MagicMock()
        mock_ydl_context = MagicMock()
        mock_ydl_context.__enter__.return_value = mock_ydl_instance
        mock_ydl_class.return_value = mock_ydl_context
        yield mock_ydl_instance


@pytest.fixture
def mock_celery_tasks():
    mock_album = MagicMock()
    mock_playlist = MagicMock()
    with (
        patch("api.routes.jobs.download_album", mock_album),
        patch("api.routes.jobs.download_playlist", mock_playlist),
        patch("api.routes.download.download_album", mock_album),
        patch("api.routes.download.download_playlist", mock_playlist),
    ):
        yield mock_album.apply_async, mock_playlist.apply_async
