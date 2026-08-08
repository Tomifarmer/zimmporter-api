"""Best-effort album genre lookup against the iTunes Search API.

YouTube Music does not expose a real genre through ``ytmusicapi`` or
yt-dlp; the tags written by yt-dlp only ever contain the watch-page
category ``Music``.  Genres are therefore resolved from iTunes at
download time and embedded into the audio file's tags.

Lookups are best-effort and never fail a download: any miss, timeout,
or HTTP error returns ``None`` (leaving the file with no genre tag).

The feature can be disabled entirely by setting ``ENABLE_GENRE`` to
``false``.

Environment variables:

* ``ENABLE_GENRE`` — enable/disable genre lookup (default ``true``)
* ``ITUNES_LOOKUP_LIMIT`` — number of candidate albums to fetch per
  lookup (default ``3``)
"""

import os

import requests

from zimmporter.cert import get_ca_cert

#: iTunes Search API endpoint.
_ITUNES_SEARCH_URL = "https://itunes.apple.com/search"

#: Request timeout in seconds — a slow lookup must never block imports.
_TIMEOUT = 5.0

#: Number of candidate albums to inspect per lookup.
_CANDIDATES = int(os.getenv("ITUNES_LOOKUP_LIMIT", "3"))


def is_enabled() -> bool:
    """Return whether genre lookup is enabled.

    Controlled by the ``ENABLE_GENRE`` env var, defaulting to enabled.
    """
    return os.getenv("ENABLE_GENRE", "true").strip().lower() != "false"


def lookup_genre(artist: str, album: str) -> str | None:
    """Look up the real genre of an album via the iTunes Search API.

    Searches for the artist and album name and returns the album's
    ``primaryGenreName`` when a release matches on both fields
    (case- and whitespace-insensitively).  Any missing album, network
    error, or unexpected response yields ``None``.

    Args:
        artist: Primary artist name of the album.
        album: Album title.

    Returns:
        The album genre as a string, or ``None`` if unavailable/disabled.
    """
    if not is_enabled():
        return None

    params = {
        "term": f"{artist} {album}",
        "media": "music",
        "entity": "album",
        "limit": _CANDIDATES,
    }
    try:
        resp = requests.get(
            _ITUNES_SEARCH_URL,
            params=params,
            timeout=_TIMEOUT,
            verify=get_ca_cert(),
        )
    except requests.RequestException:
        return None
    if not resp.ok:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None

    results = data.get("results", [])
    if not isinstance(results, list):
        return None

    normalized_artist = _normalize(artist)
    normalized_album = _normalize(album)
    for result in results:
        if not isinstance(result, dict):
            continue
        if (
            _normalize(result.get("artistName", "")) == normalized_artist
            and _normalize(result.get("collectionName", "")) == normalized_album
        ):
            genre = result.get("primaryGenreName")
            if genre:
                return str(genre)
    return None


def _normalize(value: str) -> str:
    """Normalize a name for case- and whitespace-insensitive matching."""
    return " ".join(value.strip().lower().split())
