from unittest.mock import MagicMock

from zimmporter.core import Zimmporter


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
        assert result["s3_path"] == "Artist/Album/Track One.m4a"
        assert result["error"] is None

    def test_returns_error_on_failure(self, mocker):
        mocker.patch("zimmporter.core.RETRY_DELAY", 0)
        mocker.patch("zimmporter.core.MAX_RETRIES", 2)
        mock_ydl = mocker.patch("zimmporter.core.yt_dlp.YoutubeDL")
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
        assert result["s3_path"] == "Artist - Name/Album - Name/Song - Title.m4a"


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
