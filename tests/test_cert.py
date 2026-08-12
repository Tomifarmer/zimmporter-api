import datetime
import http.server
import ipaddress
import os
import socket
import ssl
import threading
from unittest.mock import patch

import certifi
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption
from cryptography.x509.oid import NameOID

from zimmporter.cert import configure_ssl, get_ca_cert, get_ssl_context


def test_get_ca_cert_returns_none_when_no_env():
    for key in ("CA_CERT", "REQUESTS_CA_BUNDLE"):
        os.environ.pop(key, None)
    assert get_ca_cert() is None


def test_get_ca_cert_returns_ca_cert():
    os.environ["CA_CERT"] = "/custom/ca.pem"
    assert get_ca_cert() == "/custom/ca.pem"


def test_get_ca_cert_falls_back_to_requests_ca_bundle():
    os.environ.pop("CA_CERT", None)
    os.environ["REQUESTS_CA_BUNDLE"] = "/fallback/ca.pem"
    assert get_ca_cert() == "/fallback/ca.pem"


def test_get_ca_cert_prefers_ca_cert_over_requests():
    os.environ["CA_CERT"] = "/primary/ca.pem"
    os.environ["REQUESTS_CA_BUNDLE"] = "/fallback/ca.pem"
    assert get_ca_cert() == "/primary/ca.pem"


def test_configure_ssl_with_missing_file(caplog):
    os.environ["CA_CERT"] = "/nonexistent/ca.pem"
    configure_ssl()

    assert "CA_CERT path does not exist" in caplog.text


def test_get_ca_cert_after_clear(monkeypatch):
    monkeypatch.delenv("CA_CERT", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    assert get_ca_cert() is None


def test_get_ssl_context_none_when_no_env(monkeypatch):
    monkeypatch.delenv("CA_CERT", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    assert get_ssl_context() is None


def test_get_ssl_context_none_when_file_missing(monkeypatch, caplog):
    monkeypatch.setenv("CA_CERT", "/nonexistent/ca.pem")
    assert get_ssl_context() is None
    assert "CA_CERT path does not exist" in caplog.text


def test_get_ssl_context_builds_context_from_ca_cert(monkeypatch):
    monkeypatch.setenv("CA_CERT", certifi.where())
    ctx = get_ssl_context()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.get_ca_certs()


def test_get_ssl_context_none_on_invalid_pem(monkeypatch, tmp_path, caplog):
    bad_pem = tmp_path / "bad.pem"
    bad_pem.write_text("not a certificate")
    monkeypatch.setenv("CA_CERT", str(bad_pem))
    assert get_ssl_context() is None
    assert "Failed to load CA certificate" in caplog.text


def test_configure_ssl_sets_requests_adapter_bundle(monkeypatch, tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("fake")
    monkeypatch.setenv("CA_CERT", str(ca))
    import requests.adapters
    import requests.utils

    configure_ssl()
    assert requests.utils.DEFAULT_CA_BUNDLE_PATH == str(ca)
    assert requests.adapters.DEFAULT_CA_BUNDLE_PATH == str(ca)
    assert os.environ.get("SSL_CERT_FILE") == str(ca)


def _make_private_ca(tmp_path, server_uri="localhost"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Root CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    ca_pem = tmp_path / "test-ca.pem"
    ca_pem.write_bytes(
        ca_cert.public_bytes(serialization.Encoding.PEM)
    )

    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, server_uri)]))
        .issuer_name(ca_cert.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName(server_uri),
                    x509.IPAddress(ipaddress.ip_address(socket.gethostbyname(server_uri))),
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    cert_pem = tmp_path / "test-cert.pem"
    key_pem = tmp_path / "test-key.pem"
    cert_pem.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    key_pem.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return ca_pem, cert_pem, key_pem


def _b64url(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@pytest.fixture
def private_ca_jwks_server(tmp_path):
    ca_pem, cert_pem, key_pem = _make_private_ca(tmp_path, "localhost")
    public_numbers = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key().public_numbers()
    mod = public_numbers.n.to_bytes((public_numbers.n.bit_length() + 7) // 8, "big")
    exp = public_numbers.e.to_bytes((public_numbers.e.bit_length() + 7) // 8, "big")
    import json as _json

    jwks_body = _json.dumps(
        {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": "rsa-test",
                    "alg": "RS256",
                    "use": "sig",
                    "n": _b64url(mod),
                    "e": _b64url(exp),
                }
            ]
        }
    ).encode()

    class JwksHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(jwks_body)))
            self.end_headers()
            self.wfile.write(jwks_body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), JwksHandler)
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(str(cert_pem), str(key_pem))
    server.socket = server_ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"https://localhost:{server.server_address[1]}/"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_pyjwkclient_trusts_private_ca(monkeypatch, tmp_path, private_ca_jwks_server):
    monkeypatch.setenv("CA_CERT", str(tmp_path / "test-ca.pem"))
    ctx = get_ssl_context()
    assert ctx is not None

    from jwt import PyJWKClient, PyJWKClientConnectionError

    client = PyJWKClient(private_ca_jwks_server, cache_keys=True, ssl_context=ctx)
    jwk_set = client.get_jwk_set()
    assert len(jwk_set.keys) == 1


def test_pyjwkclient_fails_without_private_ca(monkeypatch, private_ca_jwks_server):
    monkeypatch.delenv("CA_CERT", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)

    from jwt import PyJWKClient, PyJWKClientConnectionError

    client = PyJWKClient(private_ca_jwks_server, cache_keys=True)
    with pytest.raises(PyJWKClientConnectionError):
        client.get_jwk_set()


def test_configure_ssl_missing_file_logs_error(monkeypatch, caplog):
    monkeypatch.setenv("CA_CERT", "/nonexistent/ca.pem")
    with patch("requests.certs.DEFAULT_CA_BUNDLE_PATH", None):
        configure_ssl()
    assert "system CA bundle" in caplog.text
