import os
from unittest.mock import MagicMock

from db.engine import get_session
from db.models import AvailableAlbum
from tasks.index import _run_index, _run_navidrome_index, _scan_bucket, upsert_available_album


class TestUpsertAvailableAlbum:
    def test_creates_row(self, sqlite_db):
        upsert_available_album("Artist", "Album", browse_id="MPREb_1", track_count=10)

        with get_session() as session:
            row = session.query(AvailableAlbum).first()
            assert row is not None
            assert row.artist == "Artist"
            assert row.album == "Album"
            assert row.browse_id == "MPREb_1"
            assert row.track_count == 10

    def test_updates_existing_row(self, sqlite_db):
        upsert_available_album("Artist", "Album", browse_id="MPREb_1")
        upsert_available_album("Artist", "Album", browse_id="MPREb_2", track_count=7)

        with get_session() as session:
            rows = session.query(AvailableAlbum).all()
            assert len(rows) == 1
            assert rows[0].browse_id == "MPREb_2"
            assert rows[0].track_count == 7

    def test_does_not_overwrite_browse_id_when_none(self, sqlite_db):
        upsert_available_album("Artist", "Album", browse_id="MPREb_1")
        upsert_available_album("Artist", "Album")

        with get_session() as session:
            row = session.query(AvailableAlbum).first()
            assert row.browse_id == "MPREb_1"

    def test_stores_genre_on_create(self, sqlite_db):
        upsert_available_album("Artist", "Album", browse_id="MPREb_1", genre="Electronic")

        with get_session() as session:
            row = session.query(AvailableAlbum).first()
            assert row.genre == "Electronic"

    def test_updates_genre_when_provided(self, sqlite_db):
        upsert_available_album("Artist", "Album", browse_id="MPREb_1")
        upsert_available_album("Artist", "Album", genre="Dance")

        with get_session() as session:
            row = session.query(AvailableAlbum).first()
            assert row.genre == "Dance"

    def test_does_not_overwrite_genre_when_none(self, sqlite_db):
        upsert_available_album("Artist", "Album", browse_id="MPREb_1", genre="Electronic")
        upsert_available_album("Artist", "Album")

        with get_session() as session:
            row = session.query(AvailableAlbum).first()
            assert row.genre == "Electronic"

    def test_retries_once_after_concurrent_duplicate_insert(self, sqlite_db, mocker):
        from sqlalchemy.exc import IntegrityError

        import tasks.index as index_module

        upsert_available_album("Artist", "Album", browse_id="MPREb_keep", track_count=3)

        real_get_session = index_module.get_session
        failing_session = MagicMock()
        failing_session.__enter__.return_value = failing_session
        failing_session.__exit__.side_effect = IntegrityError(
            "INSERT INTO available_albums ...",
            {},
            Exception("Duplicate entry 'Artist-Album' for key 'uq_available_artist_album'"),
        )
        mocker.patch.object(
            index_module,
            "get_session",
            side_effect=[failing_session, real_get_session()],
        )

        upsert_available_album("Artist", "Album", browse_id="MPREb_new", track_count=5)

        with get_session() as session:
            rows = session.query(AvailableAlbum).all()
            assert len(rows) == 1
            assert rows[0].browse_id == "MPREb_new"
            assert rows[0].track_count == 5


class TestScanBucket:
    def test_walks_artist_and_album_prefixes(self):
        paginator = MagicMock()
        outer_page = {"CommonPrefixes": [{"Prefix": "Artist One/"}, {"Prefix": "Artist Two/"}]}
        inner_page_one = {"CommonPrefixes": [{"Prefix": "Artist One/Album One/"}]}
        inner_page_two = {"CommonPrefixes": [{"Prefix": "Artist Two/Album Two/"}]}
        contents_page = {"Contents": [{"Key": "a.m4a"}, {"Key": "b.m4a"}]}
        paginator.paginate.side_effect = [
            [outer_page],
            [inner_page_one],
            [contents_page],
            [inner_page_two],
            [contents_page],
        ]

        client = MagicMock()
        client.get_paginator.return_value = paginator

        found = _scan_bucket(client, "bucket")

        assert ("Artist One", "Album One", 2) in found
        assert ("Artist Two", "Album Two", 2) in found


class TestIndexAlbums:
    def test_upserts_and_prunes(self, sqlite_db, mocker):
        import tasks.index as index_module

        mocker.patch.dict(os.environ, {"AWS_BUCKET": "test-bucket"})
        mocker.patch.object(
            index_module,
            "_scan_bucket",
            return_value={("Artist One", "Album One", 12), ("Artist Two", "Album Two", 5)},
        )
        mocker.patch.object(index_module, "_get_s3_client", return_value=MagicMock())

        with get_session() as session:
            session.add(AvailableAlbum(artist="Ghost Artist", album="Ghost Album", browse_id="MPREb_ghost"))
            session.commit()

        result = _run_index()

        assert result["indexed"] == 2
        assert result["pruned"] == 1
        with get_session() as session:
            rows = session.query(AvailableAlbum).all()
            assert {(r.artist, r.album) for r in rows} == {("Artist One", "Album One"), ("Artist Two", "Album Two")}

    def test_preserves_browse_id_across_scans(self, sqlite_db, mocker):
        import tasks.index as index_module

        mocker.patch.dict(os.environ, {"AWS_BUCKET": "test-bucket"})
        mocker.patch.object(
            index_module,
            "_scan_bucket",
            return_value={("Artist One", "Album One", 12)},
        )
        mocker.patch.object(index_module, "_get_s3_client", return_value=MagicMock())

        upsert_available_album("Artist One", "Album One", browse_id="MPREb_keep")
        _run_index()

        with get_session() as session:
            row = session.query(AvailableAlbum).filter_by(artist="Artist One", album="Album One").first()
            assert row.browse_id == "MPREb_keep"

    def test_skips_when_no_bucket(self, sqlite_db, mocker):
        import tasks.index as index_module

        mocker.patch.dict(os.environ, {"AWS_BUCKET": ""})
        scan = mocker.patch.object(index_module, "_scan_bucket")

        result = _run_index()

        assert result["indexed"] == 0
        scan.assert_not_called()


class TestNavidromeIndex:
    def test_upserts_and_prunes(self, sqlite_db, mocker):
        mocker.patch(
            "zimmporter.navidrome.get_albums",
            return_value=[("Artist One", "Album One", 12), ("Artist Two", "Album Two", 5)],
        )

        with get_session() as session:
            session.add(AvailableAlbum(artist="Ghost Artist", album="Ghost Album", browse_id="MPREb_ghost"))
            session.commit()

        result = _run_navidrome_index()

        assert result["indexed"] == 2
        assert result["pruned"] == 1
        with get_session() as session:
            rows = session.query(AvailableAlbum).all()
            assert {(r.artist, r.album) for r in rows} == {("Artist One", "Album One"), ("Artist Two", "Album Two")}

    def test_preserves_browse_id_across_scans(self, sqlite_db, mocker):
        mocker.patch("zimmporter.navidrome.get_albums", return_value=[("Artist One", "Album One", 12)])

        upsert_available_album("Artist One", "Album One", browse_id="MPREb_keep")
        _run_navidrome_index()

        with get_session() as session:
            row = session.query(AvailableAlbum).filter_by(artist="Artist One", album="Album One").first()
            assert row.browse_id == "MPREb_keep"

    def test_collapses_duplicate_feed_entries(self, sqlite_db, mocker):
        mocker.patch(
            "zimmporter.navidrome.get_albums",
            return_value=[("Artist One", "Album One", 12), ("Artist One", "Album One", 5)],
        )

        result = _run_navidrome_index()

        assert result["indexed"] == 2
        assert result["added"] == 1
        with get_session() as session:
            rows = session.query(AvailableAlbum).all()
            assert len(rows) == 1
            assert rows[0].track_count == 5

    def test_matches_existing_row_case_insensitively(self, sqlite_db, mocker):
        mocker.patch("zimmporter.navidrome.get_albums", return_value=[("artist one", "album one", 12)])

        upsert_available_album("Artist One", "Album One", browse_id="MPREb_keep")

        result = _run_navidrome_index()

        assert result["added"] == 0
        assert result["updated"] == 1
        with get_session() as session:
            rows = session.query(AvailableAlbum).all()
            assert len(rows) == 1
            assert rows[0].browse_id == "MPREb_keep"
            assert rows[0].track_count == 12

    def test_matches_existing_row_ignoring_trailing_whitespace(self, sqlite_db, mocker):
        mocker.patch(
            "zimmporter.navidrome.get_albums",
            return_value=[("Artist One  ", "Album One  ", 12)],
        )

        upsert_available_album("Artist One", "Album One", browse_id="MPREb_keep")

        result = _run_navidrome_index()

        assert result["added"] == 0
        assert result["updated"] == 1
        with get_session() as session:
            rows = session.query(AvailableAlbum).all()
            assert len(rows) == 1
            assert rows[0].browse_id == "MPREb_keep"

    def test_retries_once_on_concurrent_duplicate_key(self, sqlite_db, mocker):
        from sqlalchemy.exc import IntegrityError

        import tasks.index as index_module

        mocker.patch("zimmporter.navidrome.get_albums", return_value=[("Artist One", "Album One", 12)])
        success = {"indexed": 1, "added": 0, "updated": 1, "pruned": 0, "scanned_at": "x"}
        reconcile = mocker.patch.object(
            index_module,
            "_reconcile_found",
            side_effect=[IntegrityError("t", {}, Exception("duplicate")), success],
        )

        result = _run_navidrome_index()

        assert reconcile.call_count == 2
        assert result["indexed"] == 1
        assert result["added"] == 0
