"""Custom yt-dlp logger that injects album/song context into every message.

Used as the ``logger`` in :data:`zimmporter.core.YTDL_OPTS` so that
yt-dlp output lines are prefixed with ``[album/song]`` for easier
tracking in parallel downloads.
"""

import logging
import sys
import time


class YTDLPLogger:
    """Logs yt-dlp messages with per-song album/song context.

    All output goes to stdout via a UTC-formatted console handler.
    Call :meth:`set_song` / :meth:`set_album` before each download so
    the logger knows which track is being processed.
    """

    def __init__(self):
        """Create the logger and attach a UTC console handler."""
        self.raw_logger = logging.getLogger("YTDLP")
        self.raw_logger.handlers.clear()
        self.raw_logger.setLevel(logging.INFO)
        self.raw_logger.propagate = False

        self.extra_logging = {"song": "", "album": ""}

        formatter = logging.Formatter(
            fmt="[%(asctime)s,%(msecs)03dZ] [%(levelname)s/%(name)s] [%(album)s/%(song)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        formatter.converter = time.gmtime

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        self.raw_logger.addHandler(console_handler)
        self.raw_logger.setLevel(logging.INFO)
        self.logger = self.raw_logger

    def set_song(self, song: str) -> None:
        """Set the current song name for context in log lines."""
        self.extra_logging["song"] = song

    def set_album(self, album: str) -> None:
        """Set the current album name for context in log lines."""
        self.extra_logging["album"] = album

    def debug(self, msg: str) -> None:
        """Log a debug-level message (yt-dlp internals)."""
        if msg.startswith("[debug] "):
            self.logger.debug(msg, extra=self.extra_logging)
        else:
            self.info(msg)

    def info(self, msg: str) -> None:
        """Log an info-level message."""
        self.logger.info(msg=msg, extra=self.extra_logging)

    def warning(self, msg: str) -> None:
        """Log a warning-level message."""
        self.logger.warning(msg, extra=self.extra_logging)
        if "cookies are no longer valid" in msg or "rotated in the browser" in msg:
            try:
                from zimmporter.cookie_health import mark_stale

                mark_stale()
            except Exception:
                pass
            try:
                from zimmporter.core import YTDL_OPTS

                if "cookiefile" in YTDL_OPTS:
                    YTDL_OPTS.pop("cookiefile", None)
                    self.logger.warning(
                        "Cookies rejected by YouTube; removing them so retries run without cookies.",
                        extra=self.extra_logging,
                    )
            except Exception:
                pass

    def error(self, msg: str) -> None:
        """Log an error-level message."""
        self.logger.error(msg, extra=self.extra_logging)
