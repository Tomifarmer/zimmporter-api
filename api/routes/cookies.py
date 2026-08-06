"""Cookie management routes — ``GET /cookies`` and ``POST /cookies``.

Allows an authenticated user to upload the Netscape-format yt-dlp cookies
file (exported from a browser extension) through the UI instead of
scp'ing it to the host.  The content is validated and stored in Valkey
via :mod:`zimmporter.cookie_store` — no shared file volume is needed, so
running Celery workers pick it up on their next download job.

Only metadata is ever returned; the cookie contents (full session tokens)
are never exposed through the API.
"""

import datetime
import http.cookiejar
import tempfile
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, UploadFile

from api.models import CookieStatus
from zimmporter import cookie_health, cookie_store

#: Maximum accepted cookies file size (2 MB).
_MAX_COOKIE_SIZE = 2 * 1024 * 1024

#: Session cookies whose expiry drives the date-based staleness check.
_SESSION_COOKIE_NAMES = {
    "SID",
    "__Secure-1PSID",
    "__Secure-3PSID",
    "SAPISID",
    "__Secure-1PAPISID",
    "__Secure-3PAPISID",
    "LOGIN_INFO",
}

cookies_router = APIRouter(prefix="/cookies", tags=["cookies"])


def _parse_cookies(content: bytes) -> list[dict]:
    """Parse Netscape-format cookie file content.

    Uses the stdlib :class:`http.cookiejar.MozillaCookieJar` to validate
    the file and return a list of ``{"domain", "name", "expires"}``
    summaries.  Raises ``HTTPException(400)`` when the content is empty
    or not a valid cookie file.

    Args:
        content: Raw bytes of a Netscape-format cookies file.

    Returns:
        List of parsed cookie summaries (never full values).
    """
    if not content.strip():
        raise HTTPException(status_code=400, detail="Cookie file is empty")
    jar = http.cookiejar.MozillaCookieJar()
    try:
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".txt") as tmp:
            tmp.write(content)
            tmp.flush()
            jar.load(tmp.name, ignore_discard=True, ignore_expires=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Not a valid Netscape cookies file") from exc
    return [
        {"domain": cookie.domain, "name": cookie.name, "expires": cookie.expires} for cookie in jar
    ]


def _cookies_expired(cookies: list[dict]) -> bool:
    """Whether any session cookie has passed its expiration timestamp.

    Session cookies (``expires`` 0 or None) are skipped — only real expiry
    dates count here.  This catches age-stale exports; rotated cookies that
    still carry future expiries are caught by the worker-set flag instead.
    """
    now = int(datetime.datetime.now(datetime.UTC).timestamp())
    for cookie in cookies:
        if cookie["name"] in _SESSION_COOKIE_NAMES and cookie.get("expires") and cookie["expires"] <= now:
            return True
    return False


@cookies_router.get("", response_model=CookieStatus)
def get_cookies() -> CookieStatus:
    """Return metadata about the currently configured cookies file.

    Returns ``exists: false`` when nothing has been uploaded or the stored
    content cannot be read or parsed.  Never returns the contents.
    """
    content = cookie_store.get_content()
    if content is None:
        return CookieStatus(exists=False)
    try:
        cookies = _parse_cookies(content)
    except HTTPException:
        cookies = []
    return CookieStatus(
        exists=True,
        size=len(content),
        cookie_count=len(cookies),
        domains=sorted({cookie["domain"] for cookie in cookies}),
        modified_at=cookie_store.get_modified_at(),
        is_stale=cookie_health.is_stale() or _cookies_expired(cookies),
    )


@cookies_router.post("", response_model=CookieStatus)
def upload_cookies(
    file: Annotated[UploadFile, File(description="Netscape-format cookies.txt")],
) -> CookieStatus:
    """Upload a new cookies file, storing it in Valkey.

    Validates that the file parses as a Netscape cookie file and contains
    at least one YouTube cookie before storing it.  Replaces any previous
    content and clears the staleness flag.

    Args:
        file: Multipart upload of the exported cookies file.

    Returns:
        :class:`CookieStatus` metadata for the newly stored file.
    """
    content = file.file.read(_MAX_COOKIE_SIZE + 1)
    if len(content) > _MAX_COOKIE_SIZE:
        raise HTTPException(status_code=413, detail="Cookie file too large (max 2 MB)")
    cookies = _parse_cookies(content)
    if not any(cookie["domain"].endswith("youtube.com") for cookie in cookies):
        raise HTTPException(status_code=400, detail="No YouTube cookies found in file")

    modified_at = datetime.datetime.now(datetime.UTC)
    cookie_store.set_content(content, modified_at)
    cookie_health.clear_stale()

    return CookieStatus(
        exists=True,
        size=len(content),
        cookie_count=len(cookies),
        domains=sorted({cookie["domain"] for cookie in cookies}),
        modified_at=modified_at,
    )
