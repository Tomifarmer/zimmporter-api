from requests.exceptions import RequestException

from zimmporter.genre import lookup_genre


class TestLookupGenre:
    def test_returns_genre_on_exact_match(self, mocker):
        mock_resp = mocker.Mock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "results": [
                {"artistName": "Eminem", "collectionName": "Revival", "primaryGenreName": "Hip-Hop/Rap"},
                {"artistName": "Other", "collectionName": "Revival (Remastered)", "primaryGenreName": "Pop"},
            ]
        }
        mocker.patch("zimmporter.genre.requests.get", return_value=mock_resp)

        assert lookup_genre("Eminem", "Revival") == "Hip-Hop/Rap"

    def test_matches_case_and_whitespace_insensitively(self, mocker):
        mock_resp = mocker.Mock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "results": [
                {"artistName": "eminem", "collectionName": "  Revival ", "primaryGenreName": "Hip-Hop/Rap"},
            ]
        }
        mocker.patch("zimmporter.genre.requests.get", return_value=mock_resp)

        assert lookup_genre("EmInEm", "Revival") == "Hip-Hop/Rap"

    def test_returns_none_when_no_result_matches(self, mocker):
        mock_resp = mocker.Mock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "results": [{"artistName": "Other", "collectionName": "Different", "primaryGenreName": "Pop"}]
        }
        mocker.patch("zimmporter.genre.requests.get", return_value=mock_resp)

        assert lookup_genre("Eminem", "Revival") is None

    def test_returns_none_on_empty_results(self, mocker):
        mock_resp = mocker.Mock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"results": []}
        mocker.patch("zimmporter.genre.requests.get", return_value=mock_resp)

        assert lookup_genre("Eminem", "Revival") is None

    def test_returns_none_on_http_error(self, mocker):
        mock_resp = mocker.Mock()
        mock_resp.ok = False
        mocker.patch("zimmporter.genre.requests.get", return_value=mock_resp)

        assert lookup_genre("Eminem", "Revival") is None

    def test_returns_none_on_request_exception(self, mocker):
        mock_get = mocker.patch("zimmporter.genre.requests.get", side_effect=RequestException("boom"))

        assert lookup_genre("Eminem", "Revival") is None
        mock_get.assert_called_once()

    def test_returns_none_when_disabled(self, mocker, monkeypatch):
        monkeypatch.setenv("ENABLE_GENRE", "false")
        mock_get = mocker.patch("zimmporter.genre.requests.get")

        assert lookup_genre("Eminem", "Revival") is None
        mock_get.assert_not_called()
