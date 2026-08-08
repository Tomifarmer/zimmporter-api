import os
from unittest.mock import MagicMock, patch

from zimmporter.core import Zimmporter, _flag_stale_cookies, apply_cookie_config


class TestBuildS3Path:
    def test_basic_path(self):
        path = Zimmporter._build_s3_path("Artist", "Album", "Song")
        assert path == "Artist/Album/Song.m4a"

    def test_replaces_slashes_in_artist(self):
        path = Zimmporter._build_s3_path("Artist / Band", "Album", "Song")
        assert path == "Artist - Band/Album/Song.m4a"

    def test_replaces_slashes_in_album(self):
        path = Zimmporter._build_s3_path("Artist", "Album / EP", "Song")
        assert path == "Artist/Album - EP/Song.m4a"

    def test_replaces_slashes_in_title(self):
        path = Zimmporter._build_s3_path("Artist", "Album", "Song / Remix")
        assert path == "Artist/Album/Song - Remix.m4a"

    def test_replaces_slashes_everywhere(self):
        path = Zimmporter._build_s3_path("A / B", "C / D", "E / F")
        assert path == "A - B/C - D/E - F.m4a"

    def test_custom_extension(self):
        path = Zimmporter._build_s3_path("Artist", "Album", "Song", ext="mp3")
        assert path == "Artist/Album/Song.mp3"

    def test_default_extension_is_m4a(self):
        path = Zimmporter._build_s3_path("A", "B", "C")
        assert path.endswith(".m4a")


class TestSearch:
    def test_search_returns_results(self):
        zimm = Zimmporter()
        results = zimm.search("test query")
        assert len(results) > 0
        assert all(isinstance(r, dict) for r in results)

    def test_search_album_has_correct_keys(self):
        zimm = Zimmporter()
        results = zimm.search("test query")
        albums = [r for r in results if r["resultType"] == "album"]
        assert len(albums) > 0
        album = albums[0]
        assert "browseId" in album
        assert "title" in album
        assert "year" in album
        assert "type" in album
        assert "artist" in album
        assert "thumbnail" in album

    def test_search_song_has_correct_keys(self):
        zimm = Zimmporter()
        results = zimm.search("test query")
        songs = [r for r in results if r["resultType"] == "song"]
        assert len(songs) > 0
        song = songs[0]
        assert "videoId" in song
        assert "title" in song
        assert "artist" in song
        assert "duration" in song
        assert "thumbnail" in song

    def test_search_artist_has_correct_keys(self):
        zimm = Zimmporter()
        results = zimm.search("test query")
        artists = [r for r in results if r["resultType"] == "artist"]
        assert len(artists) > 0
        artist = artists[0]
        assert "name" in artist
        assert "subscribers" in artist

    def test_search_playlist_has_correct_keys(self):
        zimm = Zimmporter()
        results = zimm.search("test query")
        playlists = [r for r in results if r["resultType"] == "playlist"]
        assert len(playlists) > 0
        pl = playlists[0]
        assert "browseId" in pl
        assert "title" in pl
        assert "author" in pl
        assert "trackCount" in pl

    def test_search_respects_limit(self, mocker):
        mock_yt = mocker.patch("zimmporter.core.YTMusic")
        mock_instance = MagicMock()
        search_results = [
            {
                "resultType": "album",
                "browseId": f"id_{i}",
                "title": f"Album {i}",
                "artists": [{"name": "A"}],
                "thumbnails": [],
            }
            for i in range(20)
        ]
        mock_instance.search.return_value = search_results
        mock_yt.return_value = mock_instance

        zimm = Zimmporter()
        results = zimm.search("query", limit=5)
        assert len(results) == 5

    def test_search_thumbnail_picks_largest(self, mocker):
        mock_yt = mocker.patch("zimmporter.core.YTMusic")
        mock_instance = MagicMock()
        mock_instance.search.return_value = [
            {
                "resultType": "album",
                "browseId": "id",
                "title": "T",
                "year": "2024",
                "type": "Album",
                "artists": [{"name": "A"}],
                "thumbnails": [
                    {"url": "https://small.jpg", "width": 60, "height": 60},
                    {"url": "https://large.jpg", "width": 400, "height": 400},
                ],
            }
        ]
        mock_yt.return_value = mock_instance

        zimm = Zimmporter()
        results = zimm.search("q")
        assert results[0]["thumbnail"] == "https://large.jpg"

    def test_search_empty_results(self, mocker):
        mock_yt = mocker.patch("zimmporter.core.YTMusic")
        mock_instance = MagicMock()
        mock_instance.search.return_value = []
        mock_yt.return_value = mock_instance

        zimm = Zimmporter()
        results = zimm.search("no results")
        assert results == []


class TestDownloadAlbumSong:
    def test_returns_correct_dict_shape(self):
        result = Zimmporter.download_album_song(
            {"title": "Track One", "videoId": "vid1", "trackNumber": 1},
            {"title": "Album", "year": 2024},
            "Artist",
            "/fake/cover.jpg",
            thread_id=1,
        )
        assert result["title"] == "Track One"
        assert result["artist"] == "Artist"
        assert result["album"] == "Album"
        assert result["track_number"] == 1
        assert result["status"] == "success"
        assert result["s3_path"] == "Artist/Album/01 - Track One.m4a"
        assert result["error"] is None

    def test_returns_error_on_failure(self, mocker):
        mocker.patch("zimmporter.core.RETRY_DELAY", 0)
        mocker.patch("zimmporter.core.MAX_RETRIES", 2)
        mock_ydl = mocker.patch("yt_dlp.YoutubeDL")
        mock_ydl_context = MagicMock()
        mock_ydl_instance = MagicMock()
        mock_ydl_instance.download.side_effect = Exception("Download failed")
        mock_ydl_context.__enter__.return_value = mock_ydl_instance
        mock_ydl.return_value = mock_ydl_context

        result = Zimmporter.download_album_song(
            {"title": "Track One", "videoId": "vid1", "trackNumber": 1},
            {"title": "Album", "year": 2024},
            "Artist",
            "/fake/cover.jpg",
            thread_id=1,
        )
        assert result["status"] == "failed"
        assert "Download failed" in result["error"]

    def test_s3_path_with_slashes(self):
        result = Zimmporter.download_album_song(
            {"title": "Song / Title", "videoId": "vid1", "trackNumber": 1},
            {"title": "Album / Name", "year": 2024},
            "Artist / Name",
            "/fake/cover.jpg",
            thread_id=1,
        )
        assert result["s3_path"] == "Artist - Name/Album - Name/01 - Song - Title.m4a"

    def test_lyrics_passed_to_enrich_meta(self, mocker):
        mocker.patch("zimmporter.core._fetch_lyrics", return_value="la la la")
        mock_ydl = mocker.patch("yt_dlp.YoutubeDL")
        mock_ydl_context = MagicMock()
        mock_ydl_instance = MagicMock()
        mock_ydl_context.__enter__.return_value = mock_ydl_instance
        mock_ydl.return_value = mock_ydl_context

        Zimmporter.download_album_song(
            {"title": "Track One", "videoId": "vid1", "trackNumber": 1},
            {"title": "Album", "year": 2024},
            "Artist",
            "/fake/cover.jpg",
            thread_id=1,
        )

        enrich = mock_ydl_instance.add_post_processor.call_args_list[0][0][0]
        assert enrich.metadata["lyrics"] == "la la la"

    def test_skips_lyrics_when_none(self, mocker):
        mocker.patch("zimmporter.core._fetch_lyrics", return_value=None)
        mock_ydl = mocker.patch("yt_dlp.YoutubeDL")
        mock_ydl_context = MagicMock()
        mock_ydl_instance = MagicMock()
        mock_ydl_context.__enter__.return_value = mock_ydl_instance
        mock_ydl.return_value = mock_ydl_context

        Zimmporter.download_album_song(
            {"title": "Track One", "videoId": "vid1", "trackNumber": 1},
            {"title": "Album", "year": 2024},
            "Artist",
            "/fake/cover.jpg",
            thread_id=1,
        )

        enrich = mock_ydl_instance.add_post_processor.call_args_list[0][0][0]
        assert "lyrics" not in enrich.metadata

    def test_genre_passed_to_enrich_meta(self, mocker):
        mocker.patch("zimmporter.core._fetch_lyrics", return_value=None)
        mock_ydl = mocker.patch("yt_dlp.YoutubeDL")
        mock_ydl_context = MagicMock()
        mock_ydl_instance = MagicMock()
        mock_ydl_context.__enter__.return_value = mock_ydl_instance
        mock_ydl.return_value = mock_ydl_context

        Zimmporter.download_album_song(
            {"title": "Track One", "videoId": "vid1", "trackNumber": 1},
            {"title": "Revival", "year": 2024, "genre": "Hip-Hop/Rap"},
            "Eminem",
            "/fake/cover.jpg",
            thread_id=1,
        )

        enrich = mock_ydl_instance.add_post_processor.call_args_list[0][0][0]
        assert enrich.metadata["genre"] == "Hip-Hop/Rap"

    def test_genre_none_when_album_lacks_genre(self, mocker):
        mocker.patch("zimmporter.core._fetch_lyrics", return_value=None)
        mock_ydl = mocker.patch("yt_dlp.YoutubeDL")
        mock_ydl_context = MagicMock()
        mock_ydl_instance = MagicMock()
        mock_ydl_context.__enter__.return_value = mock_ydl_instance
        mock_ydl.return_value = mock_ydl_context

        Zimmporter.download_album_song(
            {"title": "Track One", "videoId": "vid1", "trackNumber": 1},
            {"title": "Revival", "year": 2024},
            "Artist",
            "/fake/cover.jpg",
            thread_id=1,
        )

        enrich = mock_ydl_instance.add_post_processor.call_args_list[0][0][0]
        assert enrich.metadata["genre"] is None


class TestDownloadBulk:
    def test_albums_lookup_genre_once_per_album(self, mocker, tmp_path):
        mocker.patch("zimmporter.core.temp_dir", f"{tmp_path}/")
        zimm = mocker.patch("zimmporter.core.Zimmporter", wraps=object).return_value
        # simpler: build a real instance with mocked client
        zimm = Zimmporter()
        zimm.yt = MagicMock()
        album_data = {
            "title": "Revival",
            "year": "2017",
            "artists": [{"name": "Eminem"}],
            "thumbnails": [{"url": "https://example.com/cover.jpg"}],
            "tracks": [
                {"title": "A", "videoId": "v1", "trackNumber": 1},
                {"title": "B", "videoId": "v2", "trackNumber": 2},
            ],
        }
        zimm.yt.get_album.return_value = album_data
        lookup = mocker.patch("zimmporter.core._lookup_genre", return_value="Hip-Hop/Rap")
        mocker.patch("zimmporter.core.requests.get", return_value=MagicMock(content=b"img"))

        captured = {}

        class FakePool:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def starmap(self, fn, items):
                captured["items"] = list(items)

        mocker.patch("billiard.Pool", FakePool)

        zimm.download_bulk("MPREb_1", album=True, playlist=False, concurrent=2)

        lookup.assert_called_once_with("Eminem", "Revival")
        assert captured["items"][0][1]["genre"] == "Hip-Hop/Rap"

    def test_playlists_skip_genre_lookup(self, mocker, tmp_path):
        mocker.patch("zimmporter.core.temp_dir", f"{tmp_path}/")
        zimm = Zimmporter()
        zimm.yt = MagicMock()
        zimm.yt.get_playlist.return_value = {
            "title": "My Playlist",
            "tracks": [{"title": "T1", "videoId": "v1", "thumbnails": [{"url": "https://example.com/c.jpg"}]}],
        }

        lookup = mocker.patch("zimmporter.core._lookup_genre", return_value="Hip-Hop/Rap")
        mocker.patch("zimmporter.core.requests.get", return_value=MagicMock(content=b"img"))

        captured = {}

        class FakePool:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def starmap(self, fn, items):
                captured["items"] = list(items)

        mocker.patch("billiard.Pool", FakePool)

        zimm.download_bulk("VL1", album=False, playlist=True, concurrent=2)

        lookup.assert_not_called()
        assert "genre" not in captured["items"][0][1]


class TestDownloadPlaylistSong:
    def test_artist_is_playlists(self):
        result = Zimmporter.download_playlist_song(
            {"title": "PL Track", "videoId": "vidpl1"},
            {"title": "My Playlist", "year": 2024},
            "playlists",
            "/fake/cover.jpg",
            thread_id=1,
        )
        assert result["artist"] == "playlists"
        assert result["track_number"] is None

    def test_returns_correct_dict_shape(self):
        result = Zimmporter.download_playlist_song(
            {"title": "PL Track", "videoId": "vidpl1"},
            {"title": "My Playlist"},
            "playlists",
            "/fake/cover.jpg",
            thread_id=1,
        )
        assert result["title"] == "PL Track"
        assert result["artist"] == "playlists"
        assert result["album"] == "My Playlist"
        assert result["status"] == "success"
        assert result["s3_path"] == "playlists/My Playlist/PL Track.m4a"


class TestApplyCookieConfig:
    def test_cookiefile_added_from_store(self, tmp_path, monkeypatch):
        monkeypatch.setattr("zimmporter.core.get_content", lambda: b"# Netscape HTTP Cookie File\n")
        monkeypatch.setattr("zimmporter.core.is_stale", lambda: False)
        opts = {"cachedir": str(tmp_path / "cache")}
        apply_cookie_config(opts)
        assert opts["cookiefile"] == str(tmp_path / "cache" / "cookies" / "cookies.txt")
        assert os.path.isfile(opts["cookiefile"])

    def test_cookiefile_skipped_when_store_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("zimmporter.core.get_content", lambda: None)
        monkeypatch.setattr("zimmporter.core.is_stale", lambda: False)
        opts = {"cachedir": str(tmp_path / "cache")}
        apply_cookie_config(opts)
        assert "cookiefile" not in opts

    def test_cookiefile_skipped_when_stale(self, tmp_path, monkeypatch):
        monkeypatch.setattr("zimmporter.core.get_content", lambda: b"# Netscape HTTP Cookie File\n")
        monkeypatch.setattr("zimmporter.core.is_stale", lambda: True)
        opts = {"cachedir": str(tmp_path / "cache")}
        apply_cookie_config(opts)
        assert "cookiefile" not in opts

    def test_cookiefile_removed_when_stale(self, tmp_path, monkeypatch):
        monkeypatch.setattr("zimmporter.core.get_content", lambda: b"# Netscape HTTP Cookie File\n")
        monkeypatch.setattr("zimmporter.core.is_stale", lambda: True)
        opts = {"cachedir": str(tmp_path / "cache"), "cookiefile": "/old/cookies.txt"}
        apply_cookie_config(opts)
        assert "cookiefile" not in opts

    def test_cookiefile_skipped_when_cache_not_writable(self, tmp_path, monkeypatch):
        """Must not crash when the cache dir can't be written (read-only FS pod)."""
        monkeypatch.setattr("zimmporter.core.get_content", lambda: b"# Netscape HTTP Cookie File\n")
        monkeypatch.setattr("zimmporter.core.is_stale", lambda: False)

        def _deny(*args, **kwargs):
            raise OSError(30, "Read-only file system")

        monkeypatch.setattr("os.makedirs", _deny)
        opts = {"cachedir": str(tmp_path / "cache")}
        apply_cookie_config(opts)
        assert "cookiefile" not in opts


class TestFlagStaleCookies:
    @patch("zimmporter.cookie_health.mark_stale")
    def test_marks_stale_on_bot_check_error(self, mock_mark):
        _flag_stale_cookies(Exception("Sign in to confirm you're not a bot. See FAQ."))
        mock_mark.assert_called_once()

    @patch("zimmporter.cookie_health.mark_stale")
    def test_marks_stale_on_rotation_warning(self, mock_mark):
        _flag_stale_cookies(Exception("YouTube account cookies are no longer valid. They have likely been rotated."))
        mock_mark.assert_called_once()

    @patch("zimmporter.cookie_health.mark_stale")
    def test_does_not_mark_on_unrelated_error(self, mock_mark):
        _flag_stale_cookies(Exception("HTTP Error 404: Not Found"))
        mock_mark.assert_not_called()

    @patch("zimmporter.cookie_health.mark_stale")
    def test_drops_cookiefile_from_opts_on_stale(self, mock_mark):
        from zimmporter.core import YTDL_OPTS

        YTDL_OPTS["cookiefile"] = "/tmp/yt-dlp-cache/cookies/cookies.txt"
        try:
            _flag_stale_cookies(Exception("Sign in to confirm you're not a bot. See FAQ."))
            assert "cookiefile" not in YTDL_OPTS
        finally:
            YTDL_OPTS.pop("cookiefile", None)

    @patch("zimmporter.cookie_health.mark_stale")
    def test_keeps_cookiefile_on_unrelated_error(self, mock_mark):
        from zimmporter.core import YTDL_OPTS

        YTDL_OPTS["cookiefile"] = "/tmp/yt-dlp-cache/cookies/cookies.txt"
        try:
            _flag_stale_cookies(Exception("HTTP Error 404: Not Found"))
            assert YTDL_OPTS["cookiefile"] == "/tmp/yt-dlp-cache/cookies/cookies.txt"
        finally:
            YTDL_OPTS.pop("cookiefile", None)
