"""Core download and search logic.

Provides the :class:`Zimmporter` class which orchestrates YouTube Music
searches, parallel song downloads via yt-dlp, AAC conversion, metadata
enrichment, and MinIO upload.  ``billiard.Pool`` forks are used for
per-album concurrency.

Module-level :data:`YTDL_OPTS` holds the global yt-dlp configuration.
Because state is lost after a ``billiard.Pool`` fork, ``logger`` and
``progress_hooks`` are re-initialized in each worker via
:meth:`Zimmporter.init_logger`.
"""

import logging
import os
import shutil
import threading
import time

import requests
from ytmusicapi import YTMusic

from zimmporter.cert import get_ca_cert
from zimmporter.ytdlp_logger import YTDLPLogger

#: Temporary working directory for intermediate downloads and thumbnails.
temp_dir = "/data/zimmer/importer/"

#: Global yt-dlp options dict.
#:
#: Mutated per-call (``outtmpl``), shared across the main process and
#: forked workers.  The ``FFmpegExtractAudio`` postprocessor converts
#: to AAC at highest quality.
YTDL_OPTS = {
    "format": "bestaudio",
    # "extractor_args": {"youtube": ["player-client=web_embedded,web,tv"]},
    "addmetadata": True,
    "writethumbnail": True,
    "js_runtimes": {"deno": {"path": "/usr/local/bin/deno"}},
    "cachedir": "/tmp/yt-dlp-cache",
    # Skip any downloads that resulted in incomplete files.  With concurrent threads
    # writing to the same temp_dir this prevents a thread from trying to postprocess
    # a .webm file that another thread has already deleted or overwritten.
    "skip_download_incomplete_files": True,
    "postprocessors": [
        {"key": "FFmpegExtractAudio", "preferredcodec": "aac", "preferredquality": "best"},
        {"key": "FFmpegMetadata", "add_metadata": True},
        {"key": "EmbedThumbnail", "already_have_thumbnail": False},
    ],
}


# How many times to retry a song if download or conversion fails due to race conditions.
MAX_RETRIES = 5

# Base seconds to wait between retry attempts (e.g. race-condition recovery).
RETRY_DELAY = 10

# Backoff multiplier for HTTP 403 errors — exponential delay per attempt.
BACKOFF_MULTIPLIER = 2

# Cap on backoff-delayed retries (skip backoff after this many attempts).
BACKOFF_MAX_ATTEMPTS = 2


class Zimmporter:
    """Orchestrates YouTube Music search, download, conversion, and upload.

    On instantiation a :class:`ytmusicapi.YTMusic` client and a configured
    :class:`logging.Logger` are created.  Set ``self.yt = None`` before
    passing work to ``billiard.Pool`` to avoid pickling the HTTP client
    across forks.
    """

    def __init__(self):
        """Initialize YTMusic client and logging infrastructure."""
        self.yt = YTMusic()
        self.logger = self.init_logger()

    def init_logger(self) -> logging.Logger:
        """Initialize logging infrastructure.

        Creates a UTC-formatted console logger named ``Zimmporter`` and a
        custom :class:`YTDLPLogger` for yt-dlp output.  Mutates the global
        :data:`YTDL_OPTS` to inject logger and progress hook references.

        Returns:
            Configured logger instance.
        """
        logger = logging.getLogger("Zimmporter")
        logger.handlers.clear()
        logger.setLevel(logging.INFO)
        logger.propagate = False

        formatter = logging.Formatter(
            fmt="[%(asctime)s,%(msecs)03dZ] [%(levelname)s/%(name)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )
        formatter.converter = time.gmtime

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
        logger.setLevel(logging.INFO)

        self.ytdlp_logger = YTDLPLogger()
        YTDL_OPTS["logger"] = self.ytdlp_logger
        YTDL_OPTS["progress_hooks"] = [self._my_hook]

        return logger

    def search(self, string: str, filter: str = "albums", limit: int = 10) -> list[dict]:
        """Search YouTube Music and return structured result dicts.

        Args:
            string: Search query text.
            filter: Result type filter.  Pass ``"albums"``, ``"community_playlists"``,
                ``"songs"``, or ``"artists"``.
            limit: Maximum number of results to return.

        Returns:
            List of dicts keyed by ``resultType``. All results include ``thumbnail``:

            * ``"song"`` / ``"video"`` — ``videoId``, ``title``, ``artist``, ``duration``
            * ``"album"`` — ``browseId``, ``title``, ``year``, ``type``, ``artist``
            * ``"artist"`` — ``name``, ``subscribers``
            * ``"playlist"`` / ``"featured_playlists"`` — ``browseId``, ``title``, ``author``
        """
        results = []
        for item_count, result in enumerate(self.yt.search(string, filter=filter, limit=limit)):
            thumbnails = result.get("thumbnails", [])
            thumbnail_url = (
                max(thumbnails, key=lambda t: t.get("width", 0) * t.get("height", 0))["url"] if thumbnails else None
            )
            if result["resultType"] in ["song", "video"]:
                results.append(
                    {
                        "resultType": result["resultType"],
                        "videoId": result["videoId"],
                        "title": result["title"],
                        "artist": [a["name"] for a in result["artists"]] if "artists" in result else [result["artist"]],
                        "duration": result.get("duration", ""),
                        "thumbnail": thumbnail_url,
                    }
                )
            elif result["resultType"] in ["album"]:
                results.append(
                    {
                        "resultType": result["resultType"],
                        "browseId": result["browseId"],
                        "title": result["title"],
                        "year": result.get("year", ""),
                        "type": result.get("type", ""),
                        "artist": [a["name"] for a in result["artists"]],
                        "thumbnail": thumbnail_url,
                    }
                )
            elif result["resultType"] == "artist":
                results.append(
                    {
                        "resultType": "artist",
                        "name": [a["name"] for a in result["artists"]]
                        if "artists" in result
                        else [result.get("artist", "")],
                        "subscribers": result.get("subscribers", "N/A"),
                        "thumbnail": thumbnail_url,
                    }
                )
            elif result["resultType"] in ["featured_playlists", "playlist"]:
                results.append(
                    {
                        "resultType": result["resultType"],
                        "browseId": result["browseId"],
                        "title": result["title"],
                        "author": result.get("author", ""),
                        "thumbnail": thumbnail_url,
                        "trackCount": result.get("itemCount"),
                    }
                )

            item_count += 1
            if item_count >= limit:
                break
        return results

    def download_bulk(self, ids: str, album: bool = True, playlist: bool = False, concurrent: int = 4) -> None:
        """Download one or more albums or playlists using parallel workers.

        Splits ``ids`` on commas to support batch downloads.  For each item
        the ytmusicapi client fetches metadata, thumbnails are downloaded
        to :data:`temp_dir`, then ``billiard.Pool`` processes songs in
        parallel via :meth:`download_album_song` or
        :meth:`download_playlist_song`.

        Args:
            ids: Comma-separated browse IDs (e.g. ``"MPREb_xxx,MPREb_yyy"``).
            album: If ``True``, process IDs as albums.
            playlist: If ``True``, process IDs as playlists.
            concurrent: Number of parallel download workers per album/playlist.

        Note:
            Sets ``self.yt = None`` before each pool spawn to avoid
            pickling the YTMusic HTTP client across forks.
        """
        from billiard import Pool

        if not album and not playlist:
            return

        if playlist:
            for id in ids.split(","):
                id = id.strip()
                playlist_data = self.yt.get_playlist(id)
                album_name = playlist_data["title"]
                to_download = list()

                for song in playlist_data["tracks"]:
                    title = song["title"]
                    artist = "playlists"
                    thumnail_url = song["thumbnails"][-1]["url"]
                    thumbnail_path = f"{temp_dir}playlists/{album_name}/{title}/cover.jpg"
                    os.makedirs(f"{temp_dir}playlists/{album_name}/{title}", exist_ok=True)

                    with open(thumbnail_path, "wb") as f:
                        f.write(requests.get(thumnail_url, verify=get_ca_cert()).content)

                    to_download.append((song, playlist_data, artist, thumbnail_path))

                self.yt = None
                with Pool(concurrent) as p:
                    p.starmap(self.download_playlist_song, to_download)

                shutil.rmtree(f"{temp_dir}playlists/{album_name}", ignore_errors=True)

        if album:
            for id in ids.split(","):
                id = id.strip()
                album_data = self.yt.get_album(id)
                artist = album_data["artists"][0]["name"]
                album_name = album_data["title"]
                thumnail_url = album_data["thumbnails"][-1]["url"]
                thumbnail_path = f"{temp_dir}{artist}/{album_name}/cover.jpg"
                os.makedirs(f"{temp_dir}{artist}/{album_name}", exist_ok=True)

                with open(thumbnail_path, "wb") as f:
                    f.write(requests.get(thumnail_url, verify=get_ca_cert()).content)

                to_download = list()
                for song in album_data["tracks"]:
                    to_download.append((song, album_data, artist, thumbnail_path))

                self.yt = None

                with Pool(concurrent) as p:
                    p.starmap(self.download_album_song, to_download)

                shutil.rmtree(f"{temp_dir}{artist}/{album_name}", ignore_errors=True)

    @staticmethod
    def _build_s3_path(artist: str, album: str, title: str, track_number: int | None = None, ext: str = "m4a") -> str:
        a = artist.replace("/", "-")
        al = album.replace("/", "-")
        s = f"{track_number:02d} - {title}" if track_number is not None else title
        s = s.replace("/", "-")
        return f"{a}/{al}/{s}.{ext}"

    @staticmethod
    def download_playlist_song(
        song: dict,
        album: dict,
        artist: str,
        thumbnail_path: str,
        thread_id: int = None,
    ) -> dict:
        """Download, convert, and upload a single playlist song.

        Each call runs in its own thread with an isolated temp subdirectory so
        concurrent downloads never share output paths or interfere with each
        other's postprocessing (FFmpeg can delete one thread's .webm while
        another is still converting it).

        Args:
            song: Track dict from ytmusicapi (must contain ``title``, ``videoId``).
            album: Playlist metadata dict (must contain ``title``, ``year``).
            artist: Unused (always ``"playlists"`` for playlist downloads).
            thumbnail_path: Local path to the cover image.
            thread_id: Thread identifier used for temp dir isolation.

        Returns:
            Dict with keys ``title``, ``artist``, ``album``, ``track_number``,
            ``status``, ``s3_path``, ``error``.
        """
        import yt_dlp

        from zimmporter.postprocessors import EnrichMeta, UploadToS3

        title = song["title"]
        zimm = Zimmporter()
        tid = thread_id or os.getpid()
        sid = f"worker_{tid}"

        # Isolated temp dir per thread — prevents cross-thread file races
        work_dir = f"{temp_dir}playlists/{album['title']}/{sid}/songs"
        os.makedirs(work_dir, exist_ok=True)
        song_dir = f"{work_dir}{title}"
        zimm.logger.info(f"[{sid}] [STARTING] {album['title']} - {title} (dir: {song_dir.replace(temp_dir, '')})")
        zimm.ytdlp_logger.set_song(title)
        zimm.ytdlp_logger.set_album(album["title"])

        YTDL_OPTS["outtmpl"] = f"{song_dir}/%(id)s.%(ext)s"
        s3_path = Zimmporter._build_s3_path("playlists", album["title"], title)
        err_msg = None

        with yt_dlp.YoutubeDL(YTDL_OPTS) as ydl:
            metadata = {
                "title": title,
                "artist": "playlists",
                "album": album["title"],
                "date": str(album.get("year", "")),
            }
            ydl.add_post_processor(EnrichMeta(metadata, thumbnail_path), when="post_process")
            ydl.add_post_processor(UploadToS3(metadata), when="post_process")

            for attempt in range(MAX_RETRIES):
                try:
                    ydl.download([f"https://music.youtube.com/watch?v={song['videoId']}"])
                    break
                except Exception as err:
                    is_403 = getattr(err, "response", None) is not None and err.response.status_code == 403
                    zimm.logger.warning(f"[{sid}] Attempt {attempt + 1}/{MAX_RETRIES} failed for {title}: {err}")
                    if attempt < MAX_RETRIES - 1:
                        delay = (
                            RETRY_DELAY * (BACKOFF_MULTIPLIER**attempt)
                            if is_403 and attempt < BACKOFF_MAX_ATTEMPTS
                            else RETRY_DELAY
                        )
                        time.sleep(delay)
                    else:
                        err_msg = str(err)

        # Clean up isolated temp dir regardless of success/failure
        shutil.rmtree(work_dir, ignore_errors=True)

        zimm.logger.info(f"[{sid}] [FINISHED] {album['title']} - {title}")

        return {
            "title": title,
            "artist": "playlists",
            "album": album["title"],
            "track_number": None,
            "status": "success" if err_msg is None else "failed",
            "s3_path": s3_path,
            "error": err_msg,
        }

    @staticmethod
    def download_album_song(
        song: dict,
        album: dict,
        artist: str,
        thumbnail_path: str,
        thread_id: int = None,
    ) -> dict:
        """Download, convert, and upload a single album song.

        Each call runs in its own thread with an isolated temp subdirectory so
        concurrent downloads never share output paths or interfere with each
        other's postprocessing (FFmpeg can delete one thread's .webm while
        another is still converting it).

        Args:
            song: Track dict from ytmusicapi (must contain ``title``,
                ``trackNumber``, ``videoId``).
            album: Album metadata dict (must contain ``title``, ``year``).
            artist: Primary artist name.
            thumbnail_path: Local path to the cover image.
            thread_id: Thread identifier used for temp dir isolation.

        Returns:
            Dict with keys ``title``, ``artist``, ``album``, ``track_number``,
            ``status``, ``s3_path``, ``error``.
        """
        import yt_dlp

        from zimmporter.postprocessors import EnrichMeta, UploadToS3

        title = song["title"]
        trackNumber = song["trackNumber"]
        zimm = Zimmporter()
        tid = thread_id or os.getpid()
        sid = f"worker_{tid}"

        # Isolated temp dir per thread — prevents cross-thread file races
        work_dir = f"{temp_dir}{artist}/{album['title']}/{sid}/songs"
        os.makedirs(work_dir, exist_ok=True)
        song_dir = f"{work_dir}{trackNumber} - {title}"
        zimm.logger.info(
            f"[{sid}] [STARTING] {album['title']} - {title} ({trackNumber}) (dir: {song_dir.replace(temp_dir, '')})"
        )
        zimm.ytdlp_logger.set_song(title)
        zimm.ytdlp_logger.set_album(album["title"])

        YTDL_OPTS["outtmpl"] = f"{song_dir}/%(id)s.%(ext)s"
        s3_path = Zimmporter._build_s3_path(artist, album["title"], title, track_number=trackNumber)
        err_msg = None

        with yt_dlp.YoutubeDL(YTDL_OPTS) as ydl:
            metadata = {
                "title": title,
                "artist": artist,
                "album": album["title"],
                "date": str(album["year"]),
                "tracknumber": str(trackNumber),
            }
            ydl.add_post_processor(EnrichMeta(metadata, thumbnail_path), when="post_process")
            ydl.add_post_processor(UploadToS3(metadata), when="post_process")

            for attempt in range(MAX_RETRIES):
                try:
                    ydl.download([f"https://music.youtube.com/watch?v={song['videoId']}"])
                    break
                except Exception as err:
                    is_403 = getattr(err, "response", None) is not None and err.response.status_code == 403
                    zimm.logger.warning(f"[{sid}] Attempt {attempt + 1}/{MAX_RETRIES} failed for {title}: {err}")
                    if attempt < MAX_RETRIES - 1:
                        delay = (
                            RETRY_DELAY * (BACKOFF_MULTIPLIER**attempt)
                            if is_403 and attempt < BACKOFF_MAX_ATTEMPTS
                            else RETRY_DELAY
                        )
                        time.sleep(delay)
                    else:
                        err_msg = str(err)

        # Clean up isolated temp dir regardless of success/failure
        shutil.rmtree(work_dir, ignore_errors=True)

        zimm.logger.info(
            f"[{sid}] [FINISHED] {album['title']} - {title} ({trackNumber}) "
            f"(status: {'ok' if err_msg is None else 'failed'})"
        )

        return {
            "title": title,
            "artist": artist,
            "album": album["title"],
            "track_number": trackNumber,
            "status": "success" if err_msg is None else "failed",
            "s3_path": s3_path,
            "error": err_msg,
        }

    @staticmethod
    def _download_album_song_task(song, album, artist, thumbnail_path):
        """Pool-compatible wrapper for :meth:`download_album_song`."""
        return Zimmporter.download_album_song(song, album, artist, thumbnail_path, thread_id=threading.get_ident()) or {
            "title": song["title"]
        }

    @staticmethod
    def _download_playlist_song_task(song, album, artist, thumbnail_path):
        """Pool-compatible wrapper for :meth:`download_playlist_song`."""
        return Zimmporter.download_playlist_song(
            song, album, artist, thumbnail_path, thread_id=threading.get_ident()
        ) or {"title": song["title"]}

    def _my_hook(self, d):
        """yt-dlp progress hook — logs a message when download finishes."""
        if d["status"] == "finished":
            self.ytdlp_logger.info("Done downloading, now converting ...")
            self.ytdlp_logger.info(f"Path downloaded: {d['filename']}")
