"""Navidrome library client.

Queries a Navidrome server through its stable Subsonic-compatible API to
enumerate the albums already present in the library.  Navidrome reads the
same S3-backed music files as the importer and organises them by ID3 tags,
so it acts as a cleaner source of truth than parsing S3 path prefixes.

Uses ``getAlbumList2`` with ``type=alphabeticalByArtist`` and pages through
the results via ``size`` (max 500) + ``offset`` until a short page is
returned.  Credentials come from environment variables:

* ``NAVIDROME_URL`` — base URL of the Navidrome server.
* ``NAVIDROME_USER`` — Subsonic API username.
* ``NAVIDROME_PASS`` — Subsonic API password.

``requests`` is imported lazily so the API container (which lacks the
heavy worker dependencies) can import this module safely.
"""

import logging
import os
import urllib.parse

from zimmporter.cert import get_ca_cert

logger = logging.getLogger(__name__)

#: Subsonic API client version string.
_SUBSONIC_VERSION = "1.16.1"

#: Client identifier sent to Navidrome.
_CLIENT_NAME = "zimmporter"

#: Page size.  The Subsonic API caps ``size`` at 500.
_PAGE_SIZE = 500

#: Request timeout in seconds.
_TIMEOUT = 30


def _subsonic_credentials() -> tuple[str, str] | None:
    """Read the Navidrome user/password from the environment.

    Returns:
        ``(user, password)`` or ``None`` when either is missing.
    """
    user = os.getenv("NAVIDROME_USER")
    password = os.getenv("NAVIDROME_PASS")
    if user and password:
        return user, password
    return None


def get_albums() -> list[tuple[str, str, int]]:
    """Fetch all albums known to Navidrome.

    Returns:
        List of ``(artist, album, track_count)`` tuples.  An empty list is
        returned both for an empty library and for any failure; check the logs
        for the specific error (see :func:`_fetch_page`).
    """
    import requests

    base_url = os.getenv("NAVIDROME_URL")
    if not base_url:
        logger.warning("NAVIDROME_URL is not set; skipping Navidrome library scan")
        return []

    credentials = _subsonic_credentials()
    if credentials is None:
        logger.warning("NAVIDROME_USER/NAVIDROME_PASS not set; skipping Navidrome library scan")
        return []

    user, password = credentials
    logger.info("Navidrome library scan starting (url=%s, user=%s)", base_url.rstrip("/"), user)
    found: list[tuple[str, str, int]] = []

    offset = 0
    while True:
        page = _fetch_page(requests, base_url, user, password, offset)
        if not page:
            break
        found.extend(page)
        if len(page) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE

    logger.info("Navidrome library scan complete: %d albums indexed", len(found))
    return found


def _fetch_page(
    requests,
    base_url: str,
    user: str,
    password: str,
    offset: int,
) -> list[tuple[str, str, int]]:
    """Fetch a single page of albums from ``getAlbumList2``.

    Args:
        requests: The ``requests`` module (injected for testability).
        base_url: Navidrome base URL.
        user: Subsonic API username.
        password: Subsonic API password.
        offset: Pagination offset.

    Returns:
        List of ``(artist, album, track_count)`` tuples, or ``[]`` when the
        request failed or the response was malformed.
    """
    params = {
        "u": user,
        "p": password,
        "v": _SUBSONIC_VERSION,
        "c": _CLIENT_NAME,
        "f": "json",
        "type": "alphabeticalByArtist",
        "size": _PAGE_SIZE,
        "offset": offset,
    }
    url = f"{base_url.rstrip('/')}/rest/getAlbumList2"
    try:
        resp = requests.get(
            url,
            params=urllib.parse.urlencode(params),
            timeout=_TIMEOUT,
            verify=get_ca_cert(),
        )
    except Exception as exc:
        logger.error(
            "Navidrome getAlbumList2 request failed (url=%s, offset=%d): %s. "
            "Check NAVIDROME_URL reachability/DNS from the worker.",
            url,
            offset,
            exc,
        )
        return []

    if resp.status_code in (401, 403):
        logger.error(
            "Navidrome getAlbumList2 authentication failed (HTTP %d) for user %r at %s. "
            "Check NAVIDROME_USER/NAVIDROME_PASS; if Navidrome uses external auth "
            "(OIDC/Authelia), the Subsonic password may be disabled.",
            resp.status_code,
            user,
            base_url.rstrip("/"),
        )
        return []
    if not resp.ok:
        logger.error(
            "Navidrome getAlbumList2 returned HTTP %d (url=%s, offset=%d): %s",
            resp.status_code,
            url,
            offset,
            resp.text[:300],
        )
        return []

    try:
        payload = resp.json()
    except ValueError as exc:
        logger.error("Navidrome getAlbumList2 returned invalid JSON (url=%s): %s", url, exc)
        return []

    subsonic = payload.get("subsonic-response") or {}
    if subsonic.get("status") != "ok":
        error = subsonic.get("error") or {}
        logger.error("Navidrome getAlbumList2 error: %s", error.get("message"))
        return []

    page: list[tuple[str, str, int]] = []
    for album in (subsonic.get("albumList2") or {}).get("album", []):
        artist = album.get("artist") or ""
        title = album.get("name") or ""
        if artist and title:
            page.append((artist, title, int(album.get("songCount") or 0)))
    return page


def test_connection() -> None:
    """Print a connectivity/auth diagnostic against the configured Navidrome.

    Run inside the worker container:

        python -m zimmporter.navidrome

    Reports URL, user, HTTP status, the Subsonic status, and the number of
    albums the first page would return.
    """
    import requests

    base_url = os.getenv("NAVIDROME_URL")
    if not base_url:
        print("NAVIDROME_URL is not set")
        return
    credentials = _subsonic_credentials()
    if credentials is None:
        print("NAVIDROME_USER/NAVIDROME_PASS are not set")
        return

    user, password = credentials
    print(f"URL:   {base_url.rstrip('/')}")
    print(f"User:  {user}")

    params = {
        "u": user,
        "p": password,
        "v": _SUBSONIC_VERSION,
        "c": _CLIENT_NAME,
        "f": "json",
        "type": "alphabeticalByArtist",
        "size": 1,
        "offset": 0,
    }
    url = f"{base_url.rstrip('/')}/rest/getAlbumList2"
    try:
        resp = requests.get(
            url,
            params=urllib.parse.urlencode(params),
            timeout=_TIMEOUT,
            verify=get_ca_cert(),
        )
    except Exception as exc:
        print(f"REQUEST FAILED: {exc!r}")
        print("-> Navidrome is unreachable from this container. Check DNS/network/URL.")
        return

    print(f"HTTP status: {resp.status_code}")
    if resp.status_code in (401, 403):
        print("-> Authentication failed. Check NAVIDROME_USER/NAVIDROME_PASS.")
        return
    if not resp.ok:
        print(f"Body: {resp.text[:300]}")
        return

    try:
        payload = resp.json()
    except ValueError:
        print(f"Invalid JSON: {resp.text[:300]}")
        return

    subsonic = payload.get("subsonic-response") or {}
    print(f"Subsonic status: {subsonic.get('status')}")
    if subsonic.get("status") != "ok":
        print(f"Subsonic error: {subsonic.get('error')}")
        return
    albums = (subsonic.get("albumList2") or {}).get("album", [])
    print(f"Albums on first page: {len(albums)}")
    print("-> OK. The Navidrome index source should work.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    test_connection()
