"""Private CA certificate configuration.

Provides helpers for injecting a private CA certificate into all HTTPS clients
(requests, urllib3/MinIO) via environment variables.  yt-dlp and ytmusicapi
pick up the cert automatically through ``REQUESTS_CA_BUNDLE``.

Set ``CA_CERT`` to the path of a PEM file mounted into the container.  Also set
``REQUESTS_CA_BUNDLE`` to the same path for coverage in forked workers.

If the configured file does not exist, a warning is logged and the clients fall
back to their default system CA bundle.
"""

import logging
import os

logger = logging.getLogger("Zimmporter")


def get_ca_cert() -> str | None:
    """Return the private CA cert path from environment variables.

    Checks ``CA_CERT`` first, then falls back to ``REQUESTS_CA_BUNDLE``.
    Returns ``None`` if neither is set.

    Returns:
        Absolute path to a PEM file, or ``None``.
    """
    return os.environ.get("CA_CERT") or os.environ.get("REQUESTS_CA_BUNDLE")


def configure_ssl() -> None:
    """Configure the global requests library to use a custom CA bundle.

    Reads the cert path from :func:`get_ca_cert`.  If the file exists, sets
    ``requests.certs.DEFAULT_CA_BUNDLE_PATH`` so all ``requests`` calls (and
    libraries built on top of it like ytmusicapi and yt-dlp) trust the private CA.

    If the configured path does not exist, logs a warning and leaves the default
    system bundle unchanged.  Call once at application/worker startup.
    """
    ca = get_ca_cert()
    if ca is None:
        return

    import requests

    if os.path.isfile(ca):
        requests.certs.DEFAULT_CA_BUNDLE_PATH = ca
        logger.info(f"Using private CA certificate: {ca}")
    else:
        logger.warning(f"CA_CERT path does not exist ({ca}), falling back to system CA bundle.")
