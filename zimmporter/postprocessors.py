"""yt-dlp postprocessors for metadata enrichment and S3 upload.

:class:`EnrichMeta` writes ID3 and MP4 tags plus embeds cover art.
:class:`UploadToS3` uploads the final file to an S3-compatible bucket
configured via environment variables.
"""

import os

import boto3
import mutagen
from botocore.config import Config
from mutagen.easyid3 import EasyID3
from mutagen.id3 import USLT
from mutagen.mp4 import MP4, MP4Cover
from yt_dlp.postprocessor import PostProcessor

from zimmporter.cert import get_ca_cert


class EnrichMeta(PostProcessor):
    """Write ID3v2.4 and MP4 metadata + embed cover art into the audio file.

    Applied as a yt-dlp postprocessor after FFmpeg converts the audio
    to AAC.  Operates in-place on the file at ``info["filepath"]``.
    """

    def __init__(self, metadata: dict, cover: str) -> None:
        """Initialize with metadata dict and path to cover JPEG.

        Args:
            metadata: Mapping of tag keys to values
                (``title``, ``artist``, ``album``, ``date``, ``tracknumber``).
            cover: Absolute path to the cover image (JPEG).
        """
        super().__init__()
        self.metadata = metadata
        self.cover = cover

    def run(self, info: dict) -> tuple[list, dict]:
        """Write tags and embed cover art.

        Args:
            info: yt-dlp info dict containing ``filepath``.

        Returns:
            Tuple of (empty list, info dict) as required by yt-dlp.
        """
        EasyID3.RegisterTextKey("year", "TDRC")
        file = mutagen.File(info["filepath"], easy=True)
        for key in self.metadata:
            if key == "lyrics":
                continue
            value = self.metadata[key]
            if value is None:
                continue
            file[key] = value
            self.to_screen(f"Setting {key} to {value}")

        if self.metadata.get("genre") is None:
            self._clear_genre(file)

        file.save()

        if self.metadata.get("lyrics"):
            self._write_lyrics(info["filepath"], self.metadata["lyrics"])

        file = MP4(info["filepath"])
        with open(self.cover, "rb") as f:
            file["covr"] = [MP4Cover(f.read(), imageformat=MP4Cover.FORMAT_JPEG)]
        file.save()

        return [], info

    def _write_lyrics(self, path: str, lyrics: str) -> None:
        """Embed lyrics into the audio file's standard lyrics tag.

        Writes ``USLT`` for ID3 (mp3) or ``©lyr`` for MP4 (aac/m4a), and
        degrades silently on any failure so metadata enrichment is never
        blocked by a lyrics write error.
        """
        try:
            file = mutagen.File(path)
            if isinstance(file, MP4):
                file["\xa9lyr"] = lyrics
            else:
                file.add(USLT(encoding=3, lang="eng", desc="", text=lyrics))
            file.save()
            self.to_screen("Embedded lyrics")
        except Exception as err:  # noqa: BLE001 - lyrics are best-effort
            self.to_screen(f"Failed to embed lyrics: {err}")

    def _clear_genre(self, file) -> None:
        """Remove any existing genre tag so stale values are not kept.

        Clears the genre on both MP4 (``©gen``) and ID3 (``TCON``/``genre``)
        spellings.  Best-effort: missing tags or unsupported files are
        silently ignored.
        """
        for tag in ("genre", "\xa9gen", "TCON"):
            try:
                if tag in file:
                    del file[tag]
            except Exception:  # noqa: BLE001 - tag removal is best-effort
                pass


class UploadToS3(PostProcessor):
    """Upload completed audio file to S3 and remove the local copy.

    Reads S3 credentials from environment variables:

    * ``AWS_ENDPOINT_URL`` — S3-compatible endpoint URL (no default)
    * ``AWS_ACCESS_KEY_ID`` — access key (no default)
    * ``AWS_SECRET_ACCESS_KEY`` — secret key (no default)
    * ``AWS_BUCKET`` — bucket name (no default)
    * ``AWS_DEFAULT_REGION`` — region (default ``us-east-1``)
    """

    def __init__(self, metadata: dict) -> None:
        """Initialize with metadata dict for object key construction.

        Args:
            metadata: Mapping containing ``title``, ``artist``, ``album``.
        """
        super().__init__()
        self.metadata = metadata

    def run(self, info: dict) -> tuple[list, dict]:
        artist = self.metadata["artist"].replace("/", "-")
        album = self.metadata["album"].replace("/", "-")
        track = self.metadata.get("tracknumber")
        song = self.metadata["title"].replace("/", "-")
        if track:
            song = f"{int(track):02d} - {song}"

        s3_path = f"{artist}/{album}/{song}.{info['ext']}"

        access_key = os.getenv("AWS_ACCESS_KEY_ID")
        secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
        endpoint = os.getenv("AWS_ENDPOINT_URL")
        bucket = os.getenv("AWS_BUCKET")
        use_https = os.getenv("AWS_USE_SSL", "true").lower() == "true"
        region = os.getenv("AWS_DEFAULT_REGION", "us-east-1")

        botocore_config = Config(
            connect_timeout=300,
            read_timeout=300,
            max_pool_connections=10,
            retries={"max_attempts": 5, "mode": "standard"},
        )

        self.to_screen(f"Uploading {info['filepath']} to {bucket}/{s3_path}")

        client = boto3.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        ).client(
            "s3",
            endpoint_url=endpoint,
            config=botocore_config,
            verify=get_ca_cert() if use_https else None,
        )
        client.upload_file(info["filepath"], bucket, s3_path, ExtraArgs={"Tagging": "provider=zimmporter"})
        os.remove(info["filepath"])
        return [], info
