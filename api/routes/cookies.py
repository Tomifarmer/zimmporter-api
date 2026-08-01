"""Cookie management routes — ``GET /cookies`` and ``POST /cookies``.

Allows an authenticated user to upload the Netscape-format yt-dlp cookies
file (exported from a browser extension) through the UI instead of
scp'ing it to the host.  The file is written atomically into
:data:`COOKIE_DIR` — a shared Docker volume the worker mounts read-only —
so running workers pick it up on their next download job.

Only metadata is ever returned; the cookie contents (full session tokens)
are never exposed through the API.
"""

import datetime
import http.cookiejar
import os
import tempfile
from contextlib import suppress
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, UploadFile

from api.models import CookieStatus

#: Directory the API writes cookies into (shared volume with the worker).
COOKIE_DIR = os.environ.get("COOKIE_DIR", "/var/zimmporter/cookies")

#: Cookie file name inside :data:`COOKIE_DIR`.
COOKIE_FILENAME = "cookies.txt"

#: Maximum accepted cookies file size (2 MB).
_MAX_COOKIE_SIZE = 2 * 1024 * 1024

cookies_router = APIRouter(prefix="/cookies", tags=["cookies"])


def _cookie_path() -> str:
    """Absolute path to the cookie file inside :data:`COOKIE_DIR`."""
    return os.path.join(COOKIE_DIR, COOKIE_FILENAME)


def _parse_cookies(content: bytes) -> list[dict]:
    """Parse Netscape-format cookie file content.

    Uses the stdlib :class:`http.cookiejar.MozillaCookieJar` to validate
    the file and return a list of ``{"domain", "name"}`` summaries.  Raises
    ``HTTPException(400)`` when the content is empty or not a valid cookie
    file.

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
    return [{"domain": cookie.domain, "name": cookie.name} for cookie in jar]


@cookies_router.get("", response_model=CookieStatus)
def get_cookies() -> CookieStatus:
    """Return metadata about the currently configured cookies file.

    Returns ``exists: false`` when no file is present or it cannot be read
    or parsed.  Never returns the file contents.
    """
    path = _cookie_path()
    if not os.path.isfile(path):
        return CookieStatus(exists=False)
    try:
        with open(path, "rb") as f:
            content = f.read()
        modified_at = datetime.datetime.fromtimestamp(os.path.getmtime(path), datetime.UTC)
    except OSError:
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
        modified_at=modified_at,
    )


@cookies_router.post("", response_model=CookieStatus)
def upload_cookies(
    file: Annotated[UploadFile, File(description="Netscape-format cookies.txt")],
) -> CookieStatus:
    """Upload a new cookies file, atomically replacing the current one.

    Validates that the file parses as a Netscape cookie file and contains
    at least one YouTube cookie before replacing the stored file.

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

    os.makedirs(COOKIE_DIR, exist_ok=True)
    dest = _cookie_path()
    tmp_path = f"{dest}.tmp"
    try:
        with open(tmp_path, "wb") as f:
            f.write(content)
        os.replace(tmp_path, dest)
    finally:
        if os.path.exists(tmp_path):
            with suppress(OSError):
                os.remove(tmp_path)

    return CookieStatus(
        exists=True,
        size=len(content),
        cookie_count=len(cookies),
        domains=sorted({cookie["domain"] for cookie in cookies}),
        modified_at=datetime.datetime.now(datetime.UTC),
    )
