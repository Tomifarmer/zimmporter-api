import os
from unittest.mock import MagicMock

from zimmporter.navidrome import _fetch_page, _subsonic_credentials, get_albums


def _album(artist, name, song_count):
    return {"artist": artist, "name": name, "songCount": song_count, "id": "album-id"}


def _response(*albums, status="ok"):
    payload = {"subsonic-response": {"status": status, "albumList2": {"album": list(albums)}}}
    mock = MagicMock()
    mock.json.return_value = payload
    mock.raise_for_status.return_value = None
    return mock


def _mock_requests(responses):
    mock_requests = MagicMock()
    mock_requests.get.side_effect = responses
    return mock_requests


class TestFetchPage:
    def test_parses_albums(self):
        mock = _mock_requests([_response(_album("Artist One", "Album One", 12))])
        page = _fetch_page(mock, "http://navidrome:4533", "user", "pass", 0)

        assert page == [("Artist One", "Album One", 12)]
        kwargs = mock.get.call_args
        assert kwargs.args[0] == "http://navidrome:4533/rest/getAlbumList2"

    def test_skips_albums_missing_names(self):
        mock = _mock_requests([_response(_album("Artist One", "", 3), _album("", "No Artist", 4))])
        page = _fetch_page(mock, "http://navidrome:4533", "user", "pass", 0)

        assert page == []

    def test_returns_empty_on_request_error(self):
        mock_requests = MagicMock()
        mock_requests.get.side_effect = Exception("connection refused")

        page = _fetch_page(mock_requests, "http://navidrome:4533", "user", "pass", 0)

        assert page == []

    def test_returns_empty_on_api_error(self):
        mock = _mock_requests([_response(status="failed")])
        mock.get.return_value.json.return_value = {
            "subsonic-response": {"status": "failed", "error": {"message": "bad auth"}}
        }

        page = _fetch_page(mock, "http://navidrome:4533", "user", "pass", 0)

        assert page == []


class TestGetAlbums:
    def test_paginates_until_short_page(self, mocker):
        full_page = [_album(f"Artist {i}", f"Album {i}", 10) for i in range(500)]
        short_page = [_album("Artist 500", "Album 500", 10)]

        mocker.patch.dict(
            os.environ,
            {"NAVIDROME_URL": "http://navidrome:4533", "NAVIDROME_USER": "user", "NAVIDROME_PASS": "pass"},
        )
        mocker.patch("requests.get", side_effect=[_response(*full_page), _response(*short_page)])

        albums = get_albums()

        assert len(albums) == 501
        assert ("Artist 0", "Album 0", 10) in albums
        assert ("Artist 500", "Album 500", 10) in albums

    def test_skips_when_no_url(self, mocker):
        mocker.patch.dict(os.environ, {"NAVIDROME_URL": ""})
        assert get_albums() == []

    def test_skips_when_no_credentials(self, mocker):
        mocker.patch.dict(
            os.environ,
            {"NAVIDROME_URL": "http://navidrome:4533", "NAVIDROME_USER": "", "NAVIDROME_PASS": ""},
        )
        get = mocker.patch("requests.get")

        assert get_albums() == []
        get.assert_not_called()


class TestSubsonicCredentials:
    def test_returns_pair_when_set(self, mocker):
        mocker.patch.dict(os.environ, {"NAVIDROME_USER": "u", "NAVIDROME_PASS": "p"})
        assert _subsonic_credentials() == ("u", "p")

    def test_returns_none_when_incomplete(self, mocker):
        mocker.patch.dict(os.environ, {"NAVIDROME_USER": "u", "NAVIDROME_PASS": ""})
        assert _subsonic_credentials() is None
