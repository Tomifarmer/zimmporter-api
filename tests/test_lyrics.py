import pytest
import requests

from zimmporter.lyrics import _lyric_from, fetch_lyrics, is_enabled


class FakeResponse:
    def __init__(self, ok, data):
        self.ok = ok
        self._data = data

    def json(self):
        return self._data


def _mock_get(mocker, get_result, search_result=None):
    mock_session = mocker.patch("zimmporter.lyrics.requests.Session")
    client = mock_session.return_value
    client.get.side_effect = [get_result] + (search_result or [])
    return client


def test_is_enabled_default_true(monkeypatch):
    monkeypatch.delenv("ENABLE_LYRICS", raising=False)
    assert is_enabled() is True


def test_is_enabled_false(monkeypatch):
    monkeypatch.setenv("ENABLE_LYRICS", "false")
    assert is_enabled() is False


def test_is_enabled_case_insensitive(monkeypatch):
    monkeypatch.setenv("ENABLE_LYRICS", "FALSE")
    assert is_enabled() is False


def test_fetch_strips_synced_timestamps(mocker):
    data = {"plainLyrics": "", "syncedLyrics": "[00:00.00] hello\n[00:05.00] world"}
    _mock_get(mocker, FakeResponse(True, data))
    assert fetch_lyrics("Artist", "Song") == "hello\nworld"


def test_fetch_empty_data_is_none(mocker):
    _mock_get(mocker, FakeResponse(False, None), search_result=[FakeResponse(False, [])])
    assert fetch_lyrics("Artist", "Song") is None


def test_fetch_falls_back_to_search(mocker):
    get_miss = FakeResponse(False, None)
    search_hit = FakeResponse(True, [{"artistName": "Artist", "trackName": "Song", "plainLyrics": "found"}])
    _mock_get(mocker, get_miss, search_result=[search_hit])
    assert fetch_lyrics("Artist", "Song") == "found"


def test_fetch_network_error_returns_none(mocker):
    client = mocker.patch("zimmporter.lyrics.requests.Session").return_value
    client.get.side_effect = requests.RequestException("boom")
    assert fetch_lyrics("Artist", "Song") is None


def test_fetch_disabled_makes_no_request(mocker, monkeypatch):
    monkeypatch.setenv("ENABLE_LYRICS", "false")
    get = mocker.patch("zimmporter.lyrics.requests.Session")
    assert fetch_lyrics("Artist", "Song") is None
    get.assert_not_called()


@pytest.mark.parametrize(
    "item,expected",
    [
        ({"plainLyrics": "p"}, "p"),
        ({"syncedLyrics": "[00:01.00] s"}, "s"),
        ({"plainLyrics": "p", "syncedLyrics": "[00:01.00] s"}, "s"),
        (
            {"syncedLyrics": "[00:18.20] Hello, it's me\n[00:23.54] I was wondering"},
            "Hello, it's me\nI was wondering",
        ),
        ({"plainLyrics": "p", "syncedLyrics": "no timestamps"}, "no timestamps"),
        ({"plainLyrics": "", "syncedLyrics": "[00:01.00] s"}, "s"),
        ({}, None),
        ({"plainLyrics": None, "syncedLyrics": None}, None),
    ],
)
def test_lyric_from(item, expected):
    assert _lyric_from(item) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("[00:01.00] line", "line"),
        ("[00:18.20] Hello, it's me", "Hello, it's me"),
        ("[00:01.00][00:05.00] multi", "multi"),
        ("[00:01.00] first\n[00:02.00] second", "first\nsecond"),
        ("plain text only", "plain text only"),
        ("[00:01.00]   spaced", "spaced"),
    ],
)
def test_strip_lrc_timestamps(text, expected):
    from zimmporter.lyrics import _strip_lrc_timestamps

    assert _strip_lrc_timestamps(text) == expected


def test_base_url_used(mocker):
    import zimmporter.lyrics as lyrics

    mocker.patch.object(lyrics, "LRCLIB_BASE_URL", "https://lyrics.example/api")
    client = mocker.patch("zimmporter.lyrics.requests.Session").return_value
    client.get.return_value = FakeResponse(True, {"plainLyrics": "x"})

    fetch_lyrics("A", "B")
    assert client.get.call_args.args[0] == "https://lyrics.example/api/get"
