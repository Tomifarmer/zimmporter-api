import os
import tempfile

from yt_dlp.postprocessor import PostProcessor

from zimmporter.postprocessors import EnrichMeta, UploadToS3


def test_enrich_meta_is_postprocessor():
    pp = EnrichMeta({"title": "Test"}, "/fake/cover.jpg")
    assert isinstance(pp, PostProcessor)


def test_enrich_meta_run_returns_tuple(mocker):
    fd_a, audio_path = tempfile.mkstemp(suffix=".m4a")
    os.write(fd_a, b"audio_data")
    os.close(fd_a)
    fd_c, cover_path = tempfile.mkstemp(suffix=".jpg")
    os.write(fd_c, b"cover_data")
    os.close(fd_c)

    mocker.patch("zimmporter.postprocessors.mutagen.File")
    mocker.patch("zimmporter.postprocessors.MP4")
    mocker.patch("zimmporter.postprocessors.MP4Cover")

    pp = EnrichMeta({"title": "Test", "artist": "A"}, cover_path)
    result = pp.run({"filepath": audio_path})
    assert result == ([], {"filepath": audio_path})

    os.unlink(audio_path)
    os.unlink(cover_path)


def test_enrich_meta_embeds_lyrics(mocker):
    fd_a, audio_path = tempfile.mkstemp(suffix=".m4a")
    os.write(fd_a, b"audio_data")
    os.close(fd_a)
    fd_c, cover_path = tempfile.mkstemp(suffix=".jpg")
    os.write(fd_c, b"cover_data")
    os.close(fd_c)

    mocker.patch("zimmporter.postprocessors.mutagen.File")
    mocker.patch("zimmporter.postprocessors.MP4")
    mocker.patch("zimmporter.postprocessors.MP4Cover")
    write_mock = mocker.patch.object(EnrichMeta, "_write_lyrics")

    pp = EnrichMeta({"title": "Test", "lyrics": "hello world"}, cover_path)
    pp.run({"filepath": audio_path})
    write_mock.assert_called_once_with(audio_path, "hello world")

    os.unlink(audio_path)
    os.unlink(cover_path)


def test_enrich_meta_skips_lyrics_when_absent(mocker):
    fd_a, audio_path = tempfile.mkstemp(suffix=".m4a")
    os.write(fd_a, b"audio_data")
    os.close(fd_a)
    fd_c, cover_path = tempfile.mkstemp(suffix=".jpg")
    os.write(fd_c, b"cover_data")
    os.close(fd_c)

    mocker.patch("zimmporter.postprocessors.mutagen.File")
    mocker.patch("zimmporter.postprocessors.MP4")
    mocker.patch("zimmporter.postprocessors.MP4Cover")
    write_mock = mocker.patch.object(EnrichMeta, "_write_lyrics")

    pp = EnrichMeta({"title": "Test"}, cover_path)
    pp.run({"filepath": audio_path})
    write_mock.assert_not_called()

    os.unlink(audio_path)
    os.unlink(cover_path)


def test_write_lyrics_mp4_atom(mocker):
    fake_mp4_cls = type("FakeMP4", (), {})
    mocker.patch("zimmporter.postprocessors.MP4", fake_mp4_cls)
    fake_file = mocker.MagicMock()
    fake_file.__class__ = fake_mp4_cls
    mocker.patch("zimmporter.postprocessors.mutagen.File", return_value=fake_file)

    pp = EnrichMeta({"title": "Test"}, "/fake/cover.jpg")
    pp._write_lyrics("/fake/song.m4a", "hello world")

    fake_file.__setitem__.assert_called_once_with("\xa9lyr", "hello world")
    fake_file.save.assert_called_once()


def test_write_lyrics_id3_uslt(mocker):
    mocker.patch("zimmporter.postprocessors.MP4", type("DummyMP4", (), {}))
    fake_file = mocker.Mock()
    mocker.patch("zimmporter.postprocessors.mutagen.File", return_value=fake_file)

    pp = EnrichMeta({"title": "Test"}, "/fake/cover.jpg")
    pp._write_lyrics("/fake/song.mp3", "hello world")

    fake_file.add.assert_called_once()
    uslt = fake_file.add.call_args.args[0]
    assert uslt.lang == "eng"
    assert uslt.text == "hello world"
    fake_file.save.assert_called_once()


def test_write_lyrics_never_raises(mocker):
    mocker.patch("zimmporter.postprocessors.mutagen.File", side_effect=RuntimeError("boom"))

    pp = EnrichMeta({"title": "Test"}, "/fake/cover.jpg")
    pp._write_lyrics("/fake/song.m4a", "hello world")


def test_upload_to_s3_is_postprocessor():
    pp = UploadToS3({"title": "Test", "artist": "A", "album": "B"})
    assert isinstance(pp, PostProcessor)


def test_upload_to_s3_replaces_slashes_in_path():
    pp = UploadToS3({"title": "Song / Title", "artist": "Artist / Name", "album": "Album / Name"})
    assert pp.metadata["title"] == "Song / Title"
    assert pp.metadata["artist"] == "Artist / Name"
    assert pp.metadata["album"] == "Album / Name"


def test_upload_to_s3_calls_boto3_upload():
    import boto3

    fd, audio_path = tempfile.mkstemp(suffix=".m4a")
    os.write(fd, b"audio_data")
    os.close(fd)

    pp = UploadToS3({"title": "Test Song", "artist": "Test Artist", "album": "Test Album"})
    pp.run({"filepath": audio_path, "ext": "m4a"})

    mock_session = boto3.Session
    mock_client = mock_session.return_value.client.return_value
    mock_client.upload_file.assert_called_once()
    args, kwargs = mock_client.upload_file.call_args
    assert args[1] == "test_bucket"
    assert args[2] == "Test Artist/Test Album/Test Song.m4a"
    assert kwargs["ExtraArgs"] == {"Tagging": "provider=zimmporter"}
    assert not os.path.exists(audio_path)


def test_upload_to_s3_deletes_local_file():
    fd, audio_path = tempfile.mkstemp(suffix=".m4a")
    os.write(fd, b"data")
    os.close(fd)
    assert os.path.exists(audio_path)

    pp = UploadToS3({"title": "T", "artist": "A", "album": "B"})
    pp.run({"filepath": audio_path, "ext": "m4a"})
    assert not os.path.exists(audio_path)


def test_upload_to_s3_uses_env_vars():
    import boto3

    os.environ["AWS_ACCESS_KEY_ID"] = "custom_key"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "custom_secret"
    os.environ["AWS_ENDPOINT_URL"] = "https://custom.example.com"
    os.environ["AWS_BUCKET"] = "custom_bucket"

    fd, audio_path = tempfile.mkstemp(suffix=".mp3")
    os.write(fd, b"x")
    os.close(fd)

    pp = UploadToS3({"title": "T", "artist": "A", "album": "B"})
    pp.run({"filepath": audio_path, "ext": "mp3"})

    mock_session = boto3.Session
    session_call_kwargs = mock_session.call_args.kwargs
    assert session_call_kwargs["aws_access_key_id"] == "custom_key"
    assert session_call_kwargs["aws_secret_access_key"] == "custom_secret"

    client_call_kwargs = mock_session.return_value.client.call_args.kwargs
    assert client_call_kwargs["endpoint_url"] == "https://custom.example.com"

    mock_client = mock_session.return_value.client.return_value
    mock_client.upload_file.assert_called_once()
    upload_args, _ = mock_client.upload_file.call_args
    assert upload_args[1] == "custom_bucket"
