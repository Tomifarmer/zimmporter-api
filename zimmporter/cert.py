"""Private CA certificate configuration.

Provides helpers for injecting a private CA certificate into all HTTPS clients
(requests, boto3/S3, urllib-based clients like PyJWKClient) via environment
variables.  yt-dlp and ytmusicapi pick up the cert automatically through
``REQUESTS_CA_BUNDLE``.

Set ``CA_CERT`` to the path of a PEM file mounted into the container.  Also set
``REQUESTS_CA_BUNDLE`` to the same path for coverage in forked workers.

If the configured file does not exist or fails to load, an error is logged and
the clients fall back to their default system CA bundle.
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

    Reads the cert path from :func:`get_ca_cert`.  If the file exists, wires
    the private CA into ``requests`` by setting the module-level
    ``DEFAULT_CA_BUNDLE_PATH`` attributes that ``requests.adapters`` actually
    reads at call time (``requests.utils`` and ``requests.certs`` are bound at
    import time to certifi and are not consulted by the transport), and exports
    ``SSL_CERT_FILE`` for any env-aware client.  All ``requests`` calls (and
    libraries built on top of it like ytmusicapi) then trust the private CA.

    If the configured path does not exist, logs an error (but leaves the default
    system bundle unchanged).  Call once at application/worker startup.
    """
    ca = get_ca_cert()
    if ca is None:
        return

    import requests
    import requests.adapters
    import requests.utils

    if os.path.isfile(ca):
        requests.certs.DEFAULT_CA_BUNDLE_PATH = ca
        requests.utils.DEFAULT_CA_BUNDLE_PATH = ca
        requests.adapters.DEFAULT_CA_BUNDLE_PATH = ca
        os.environ["SSL_CERT_FILE"] = ca
        logger.info(f"Using private CA certificate: {ca}")
    else:
        logger.error(
            f"CA_CERT path does not exist ({ca}); HTTPS clients will fall back to the "
            f"system CA bundle and may fail to verify servers signed by the private CA."
        )


def get_ssl_context() -> ssl.SSLContext | None:
    """Build an ``ssl.SSLContext`` that trusts the private CA certificate.

    Reads the cert path from :func:`get_ca_cert`.  If the file exists,
    returns a default context with the private CA added to the verified
    locations, so ``urllib``-based clients (e.g. ``PyJWKClient``) trust
    servers signed by the private CA.

    If neither env var is set, the file is missing, or it fails to load,
    logs an error and returns ``None`` so callers can log/fall back to the
    default system verification behavior.
    """
    ca = get_ca_cert()
    if ca is None or not os.path.isfile(ca):
        if ca is not None:
            logger.error(
                f"CA_CERT path does not exist ({ca}); urllib-based clients (PyJWKClient) "
                f"will use the system CA bundle and may fail to verify private-CA servers."
            )
        return None
    try:
        ctx = ssl.create_default_context()
        ctx.load_verify_locations(cafile=ca)
        return ctx
    except ssl.SSLError as e:
        logger.error(
            f"Failed to load CA certificate {ca}: {e}; urllib-based clients (PyJWKClient) "
            f"will use the system CA bundle."
        )
        return None
