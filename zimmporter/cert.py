"""Private CA certificate configuration.

Provides helpers for injecting a private CA certificate into all HTTPS clients
(requests, boto3/S3, urllib-based clients like PyJWKClient) via environment
variables.  yt-dlp and ytmusicapi pick up the cert automatically through
``REQUESTS_CA_BUNDLE``.

Set ``CA_CERT`` to the path of a PEM file mounted into the container.  Also set
``REQUESTS_CA_BUNDLE`` to the same path for coverage in forked workers.

If the configured file does not exist, a warning is logged and the clients fall
back to their default system CA bundle.
"""

import logging
import os
import ssl

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


def get_ssl_context() -> ssl.SSLContext | None:
    """Build an ``ssl.SSLContext`` that trusts the private CA certificate.

    Reads the cert path from :func:`get_ca_cert`.  If the file exists,
    returns a default context with the private CA added to the verified
    locations, so ``urllib``-based clients (e.g. ``PyJWKClient``) trust
    servers signed by the private CA.

    If neither env var is set, the file is missing, or it fails to load,
    logs a warning and returns ``None`` so callers fall back to the
    default system verification behavior.
    """
    ca = get_ca_cert()
    if ca is None or not os.path.isfile(ca):
        if ca is not None:
            logger.warning(f"CA_CERT path does not exist ({ca}), using system CA bundle.")
        return None
    try:
        ctx = ssl.create_default_context()
        ctx.load_verify_locations(cafile=ca)
        return ctx
    except ssl.SSLError as e:
        logger.warning(f"Failed to load CA certificate {ca}: {e}, using system CA bundle.")
        return None
