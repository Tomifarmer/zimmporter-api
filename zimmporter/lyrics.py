"""Best-effort lyrics lookup against the LRCLIB API.

Lyrics are fetched at download time and embedded into the audio file's
ID3/MP4 tags so players such as Navidrome can display them.  Lookups are
non-blocking and never fail a download: any error or miss returns ``None``.

Only **plain (unsynced)** lyrics are embedded.  Downloads source arbitrary
YouTube clips rather than the studio master, so synced LRC timestamps never
reliably line up with the actual audio; timestamps are therefore stripped.

The feature can be disabled entirely by setting ``ENABLE_LYRICS`` to
``false``.

Environment variables:

* ``ENABLE_LYRICS`` — enable/disable lyric fetching (default ``true``)
* ``LRCLIB_BASE_URL`` — LRCLIB endpoint override (default
  ``https://lrclib.net/api``)
"""

import os
import re
import time

import requests

LRCLIB_BASE_URL = os.getenv("LRCLIB_BASE_URL", "https://lrclib.net/api").rstrip("/")

#: timeouts and retry tuned so a slow/unreachable lyrics API never blocks imports
TIMEOUT = 5.0
RETRIES = 1
SLEEP_BETWEEN_RETRIES = 0.5

#: Matches one or more leading LRC ``[mm:ss.xx]`` timestamp groups on a line.
_LRC_TAG_RE = re.compile(r"^\s*(\[[^\]]*\])+\s*")


def is_enabled() -> bool:
    """Return whether lyric fetching is enabled.

    Controlled by the ``ENABLE_LYRICS`` env var, defaulting to enabled.
    """
    return os.getenv("ENABLE_LYRICS", "true").strip().lower() != "false"


def fetch_lyrics(artist: str, title: str) -> str | None:
    """Fetch lyrics for a track, returning plain text or ``None``.

    Queries LRCLIB by artist and track name, falling back to a broader
    ``/api/search`` when the exact ``/api/get`` lookup misses.  Any LRC
    timestamps are stripped so the returned text is always plain and correct
    regardless of which clip the audio was sourced from.

    Args:
        artist: Track artist name.
        title: Track title.

    Returns:
        The lyrics as a string, or ``None`` if unavailable/disabled.
    """
    if not is_enabled():
        return None

    client = requests.Session()
    try:
        exact = _get(client, artist, title)
    except requests.RequestException:
        return None
    else:
        if exact is not None:
            return exact

    try:
        matches = _search(client, artist, title)
    except requests.RequestException:
        return None
    else:
        if not matches:
            return None
        return _lyric_from(matches[0])


def _json_or_none(resp) -> dict | None:
    if not resp.ok:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _get(client: requests.Session, artist: str, title: str) -> str | None:
    for _ in range(RETRIES + 1):
        try:
            resp = client.get(
                f"{LRCLIB_BASE_URL}/get",
                params={"artist_name": artist, "track_name": title},
                timeout=TIMEOUT,
            )
            data = _json_or_none(resp)
            if data:
                return _lyric_from(data)
            return None
        except requests.RequestException:
            time.sleep(SLEEP_BETWEEN_RETRIES)
    return None


def _search(client: requests.Session, artist: str, title: str) -> list[dict]:
    resp = client.get(
        f"{LRCLIB_BASE_URL}/search",
        params={"artist_name": artist, "track_name": title},
        timeout=TIMEOUT,
    )
    if not resp.ok:
        return []
    try:
        data = resp.json()
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def _lyric_from(item: dict) -> str | None:
    """Return plain (unsynced) lyrics from an LRCLIB item.

    Downloads source arbitrary YouTube clips rather than the studio master,
    so synced LRC timestamps can never be trusted.  Prefers the fullest
    available text (``syncedLyrics``, which is the most complete) and strips
    every ``[mm:ss.xx]`` timestamp so the returned text is pure lyrics.
    """
    raw = item.get("syncedLyrics") or item.get("plainLyrics")
    if not raw:
        return None
    return _strip_lrc_timestamps(raw)


def _strip_lrc_timestamps(text: str) -> str:
    """Remove leading LRC timestamp groups from every line."""
    lines = []
    for line in text.splitlines():
        stripped = _LRC_TAG_RE.sub("", line)
        lines.append(stripped)
    return "\n".join(lines)
