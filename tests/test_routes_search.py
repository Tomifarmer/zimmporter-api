class TestSearchRoute:
    def test_search_returns_results(self, test_client):
        resp = test_client.get("/search?q=aurora")
        assert resp.status_code == 200
        data = resp.json()
        assert "results" in data
        assert len(data["results"]) > 0

    def test_search_results_have_expected_fields(self, test_client):
        resp = test_client.get("/search?q=aurora")
        data = resp.json()
        album = next(r for r in data["results"] if r["resultType"] == "album")
        assert "browseId" in album
        assert "title" in album
        assert "artist" in album
        assert "thumbnail" in album

    def test_search_with_songs_filter(self, test_client):
        resp = test_client.get("/search?q=test&type=songs")
        assert resp.status_code == 200

    def test_search_with_playlists_filter(self, test_client):
        resp = test_client.get("/search?q=test&type=playlists")
        assert resp.status_code == 200

    def test_search_respects_limit(self, test_client, mocker):
        from zimmporter.core import Zimmporter

        mock_results = [
            {
                "resultType": "album",
                "browseId": f"id_{i}",
                "title": f"Album {i}",
                "year": "2024",
                "type": "Album",
                "artist": ["Artist"],
                "thumbnails": [{"url": "https://x.jpg", "width": 100, "height": 100}],
            }
            for i in range(5)
        ]
        mocker.patch.object(Zimmporter, "search", return_value=mock_results)

        resp = test_client.get("/search?q=test&limit=5")
        data = resp.json()
        assert len(data["results"]) == 5

    def test_search_empty_results(self, test_client, mocker):
        from zimmporter.core import Zimmporter

        mocker.patch.object(Zimmporter, "search", return_value=[])
        resp = test_client.get("/search?q=nonexistent")
        data = resp.json()
        assert data["results"] == []

    def test_search_missing_query_returns_422(self, test_client):
        resp = test_client.get("/search")
        assert resp.status_code == 422

    def test_search_limit_out_of_range_returns_422(self, test_client):
        resp = test_client.get("/search?q=test&limit=0")
        assert resp.status_code == 422
        resp = test_client.get("/search?q=test&limit=51")
        assert resp.status_code == 422

    def test_search_embeds_thumbnail_data_uri_when_proxy_enabled(self, test_client, mocker):
        import os

        from zimmporter.core import Zimmporter

        mocker.patch.dict(os.environ, {"API_PROXY_FETCH": "true"})
        mocker.patch(
            "api.routes.search._fetch_thumbnail_bytes",
            return_value=(b"fake_image_bytes", "image/jpeg"),
        )

        mock_results = [
            {
                "resultType": "album",
                "browseId": "MPREb_xxx",
                "title": "Test Album",
                "year": "2024",
                "type": "Album",
                "artist": ["Test Artist"],
                "thumbnail": "https://lh3.googleusercontent.com/abc123",
                "trackCount": 10,
            }
        ]
        mocker.patch.object(Zimmporter, "search", return_value=mock_results)

        resp = test_client.get("/search?q=test")
        assert resp.status_code == 200
        data = resp.json()
        thumb = data["results"][0]["thumbnail"]
        import base64

        assert thumb.startswith("data:image/jpeg;base64,")
        decoded = base64.b64decode(thumb.split(",", 1)[1])
        assert decoded == b"fake_image_bytes"

    def test_search_embeds_thumbnail_data_uri_for_youtube_url(self, test_client, mocker):
        import os

        from zimmporter.core import Zimmporter

        mocker.patch.dict(os.environ, {"API_PROXY_FETCH": "true"})
        mocker.patch(
            "api.routes.search._fetch_thumbnail_bytes",
            return_value=(b"small_image_data", "image/webp"),
        )
        mock_results = [
            {
                "resultType": "album",
                "browseId": "MPREb_xxx",
                "title": "Test Album",
                "year": "2024",
                "type": "Album",
                "artist": ["Test Artist"],
                "thumbnail": "https://yt3.googleusercontent.com/hash=w544-h544-l90-rj",
                "trackCount": 10,
            }
        ]
        mocker.patch.object(Zimmporter, "search", return_value=mock_results)

        resp = test_client.get("/search?q=test")
        assert resp.status_code == 200
        data = resp.json()
        thumb = data["results"][0]["thumbnail"]
        import base64

        assert thumb.startswith("data:image/webp;base64,")
        decoded = base64.b64decode(thumb.split(",", 1)[1])
        assert decoded == b"small_image_data"

    def test_search_does_not_rewrite_when_proxy_disabled(self, test_client, mocker):
        from zimmporter.core import Zimmporter

        original_url = "https://lh3.googleusercontent.com/abc123"
        mock_results = [
            {
                "resultType": "album",
                "browseId": "MPREb_xxx",
                "title": "Test Album",
                "year": "2024",
                "type": "Album",
                "artist": ["Test Artist"],
                "thumbnail": original_url,
                "trackCount": 10,
            }
        ]
        mocker.patch.object(Zimmporter, "search", return_value=mock_results)

        resp = test_client.get("/search?q=test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["results"][0]["thumbnail"] == original_url
