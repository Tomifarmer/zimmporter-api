"""Shared Valkey client helper.

redis-py's ``Redis.from_url(url, db=N)`` ignores the ``db`` keyword when the
URL already carries a database path (the URL wins).  To actually select a
Valkey database we inject it into the URL path instead, deriving the
connection details (host/port/auth) from the Celery broker URL.

Databases: ``0`` = broker, ``1`` = backend, ``2`` = search cache, ``3`` = cookies.
"""

from urllib.parse import urlsplit, urlunsplit

from redis import Redis


def get_redis(database: int = 0) -> Redis:
    """Return a Redis client bound to ``database``.

    Args:
        database: Valkey database index to select.

    Returns:
        A configured :class:`redis.Redis` client.
    """
    from tasks.celery_app import celery_app

    parts = urlsplit(celery_app.conf.broker_url)
    url = urlunsplit((parts.scheme, parts.netloc, f"/{database}", parts.query, parts.fragment))
    return Redis.from_url(url)
