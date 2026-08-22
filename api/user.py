"""Helpers for extracting identity and group info from an authenticated request.

Centralizes access to ``request.scope["user"]`` so the routes behave
consistently for OIDC, GitHub, and API-key authentication.
"""

import os

from fastapi import Request


def get_requested_by(request: Request) -> str | None:
    """Extract the requesting user's name/handle from an authenticated request.

    Returns ``None`` when auth is disabled or the request was authenticated
    via API key.
    """
    user = request.scope.get("user")
    if user is None:
        return None
    return user.get("name") or user.get("sub")


def get_requested_groups(request: Request) -> list[str] | None:
    """Extract the requesting user's group memberships (the OIDC ``groups`` claim).

    Returns ``None`` when the request is unauthenticated or the token carries
    no groups (e.g. GitHub auth).
    """
    user = request.scope.get("user")
    if user is None:
        return None
    groups = user.get("groups")
    if not isinstance(groups, list):
        return None
    return [g for g in groups if isinstance(g, str) and g]


def get_requested_groups_delimited(request: Request) -> str | None:
    """Group names as a padded comma-separated string for DB storage.

    Produces values like ``",IBR,SEB,"`` so group overlap can be matched with
    a ``LIKE`` pattern without substring false positives. Returns ``None``
    when the requester has no groups.
    """
    groups = get_requested_groups(request)
    if not groups:
        return None
    return "," + ",".join(groups) + ","


def social_login_enabled() -> bool:
    """Return ``True`` when social (Bearer token) login mode is active.

    Mirrors the ``USE_SOCIAL_LOGIN`` check in :class:`api.app.AuthMiddleware`.
    """
    return os.environ.get("USE_SOCIAL_LOGIN", "").lower() == "true"


def _admin_groups() -> set[str]:
    """Groups allowed to bypass the per-group job filter.

    Read from the ``JOB_ADMIN_GROUPS`` environment variable (comma-separated).
    Defaults to empty — no admin bypass unless explicitly configured.
    """
    raw = os.environ.get("JOB_ADMIN_GROUPS", "")
    return {g.strip() for g in raw.split(",") if g.strip()}


def is_admin(request: Request) -> bool:
    """Return ``True`` when the requester belongs to an admin group."""
    groups = get_requested_groups(request)
    if not groups:
        return False
    admin = _admin_groups()
    if not admin:
        return False
    return bool(set(groups) & admin)
