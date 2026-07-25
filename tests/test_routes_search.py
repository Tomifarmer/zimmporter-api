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
