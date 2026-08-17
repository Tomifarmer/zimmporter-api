from db.engine import get_session
from db.models import AvailableAlbum, Job, Song


def _seed_job(session, job_type="album", status="success", requested_by=None, requested_groups=None):
    job = Job(
        job_type=job_type,
        browse_id="browse_id",
        status=status,
        message="Test job",
        requested_by=requested_by,
        requested_groups=requested_groups,
    )
    session.add(job)
    session.flush()
    return job


def _seed_album(session, artist, album, genre=None, track_count=None):
    row = AvailableAlbum(
        artist=artist,
        album=album,
        genre=genre,
        track_count=track_count,
    )
    session.add(row)
    session.flush()
    return row


def _seed_song(session, job, title="Song", status="success"):
    song = Song(
        job_id=job.id,
        title=title,
        artist="Artist",
        album="Album",
        track_number=1,
        status=status,
    )
    session.add(song)
    session.flush()
    return song


class TestStats:
    def test_aggregates_jobs_library_and_genres(self, test_client, mocker):
        mocker.patch("api.routes.stats._lookup_genre", return_value="Pop")
        with get_session() as session:
            _seed_job(session, job_type="album", status="success")
            _seed_job(session, job_type="album", status="success")
            _seed_job(session, job_type="album", status="failed")
            _seed_job(session, job_type="playlist", status="pending")
            _seed_job(session, job_type="playlist", status="running")

            _seed_album(session, "Artist A", "Album 1", genre="Electronic", track_count=10)
            _seed_album(session, "Artist A", "Album 2", genre="Electronic", track_count=8)
            _seed_album(session, "Artist B", "Album 3", genre="Dance", track_count=12)
            _seed_album(session, "Artist B", "Album 4", genre=None, track_count=5)
            _seed_album(session, "playlists", "Playlist X", track_count=20)
            _seed_album(session, "playlists", "Playlist Y", track_count=15)

        resp = test_client.get("/stats")
        assert resp.status_code == 200
        data = resp.json()

        assert data["jobs"]["total"] == 5
        assert data["jobs"]["by_status"]["success"] == 2
        assert data["jobs"]["by_status"]["failed"] == 1
        assert data["jobs"]["by_status"]["pending"] == 1
        assert data["jobs"]["by_status"]["running"] == 1
        assert data["jobs"]["by_type"] == {"album": 3, "playlist": 2}

        assert data["library"] == {
            "albums": 4,
            "playlists": 2,
            "artists": 2,
            "tracks": 70,
        }

        genres = {g["genre"]: g["count"] for g in data["genres"]}
        assert genres == {"Electronic": 2, "Dance": 1, "Pop": 1}

    def test_backfill_persists_resolved_genre(self, test_client, mocker):
        mocker.patch("api.routes.stats._lookup_genre", return_value="Pop")
        with get_session() as session:
            _seed_album(session, "Artist B", "Album 4", genre=None, track_count=5)
            _seed_album(session, "playlists", "Playlist X", track_count=20)

        test_client.get("/stats")

        with get_session() as session:
            album = session.query(AvailableAlbum).filter(AvailableAlbum.album == "Album 4").first()
            playlist = (
                session.query(AvailableAlbum).filter(AvailableAlbum.album == "Playlist X").first()
            )
            assert album.genre == "Pop"
            assert playlist.genre is None

    def test_backfill_is_bounded_per_request(self, test_client, mocker):
        lookup = mocker.patch("api.routes.stats._lookup_genre", return_value="Pop")
        with get_session() as session:
            for i in range(6):
                _seed_album(session, "Artist", f"Album {i}", genre=None, track_count=1)

        test_client.get("/stats")
        assert lookup.call_count == 3

        test_client.get("/stats")
        assert lookup.call_count == 6

    def test_stats_respects_user_visibility(self, test_client, monkeypatch, mocker):
        mocker.patch("api.routes.stats._lookup_genre", return_value="Pop")
        monkeypatch.setenv("USE_SOCIAL_LOGIN", "true")
        monkeypatch.setenv("OIDC_ISSUER_URL", "https://idp.example.com")
        monkeypatch.setenv("OIDC_CLIENT_ID", "client-id")
        mocker.patch("api.app._validate_oidc_token", return_value={"sub": "auth-1", "name": "Seeker"})

        with get_session() as session:
            _seed_job(session, job_type="album", status="success", requested_by="Seeker")
            _seed_job(session, job_type="playlist", status="success", requested_by="Someone Else")
            _seed_album(session, "Artist A", "Album 1", genre="Electronic", track_count=10)

        resp = test_client.get("/stats", headers={"Authorization": "Bearer test-token"})
        data = resp.json()
        assert data["jobs"]["total"] == 1
        assert data["jobs"]["by_type"] == {"album": 1, "playlist": 0}

    def test_top_users_lists_most_active_social_users(self, test_client, mocker):
        mocker.patch("api.routes.stats._lookup_genre", return_value="Pop")
        with get_session() as session:
            alice_album = _seed_job(session, job_type="album", status="success", requested_by="Alice")
            _seed_song(session, alice_album)
            _seed_song(session, alice_album, title="Song 2")
            _seed_song(session, alice_album, title="Failed", status="failed")
            alice_playlist = _seed_job(
                session, job_type="playlist", status="success", requested_by="Alice"
            )
            _seed_song(session, alice_playlist)
            bob_job = _seed_job(session, job_type="album", status="success", requested_by="Bob")
            _seed_song(session, bob_job)
            _seed_job(session, job_type="album", status="success")

        resp = test_client.get("/stats")
        data = resp.json()
        assert data["top_users"] == [
            {"user": "Alice", "jobs": 2, "tracks": 3},
            {"user": "Bob", "jobs": 1, "tracks": 1},
        ]

    def test_stats_with_empty_db(self, test_client):
        resp = test_client.get("/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["jobs"] == {
            "total": 0,
            "by_status": {},
            "by_type": {"album": 0, "playlist": 0},
        }
        assert data["library"] == {"albums": 0, "playlists": 0, "artists": 0, "tracks": 0}
        assert data["genres"] == []
        assert data["top_users"] == []