import os
import ssl

import certifi

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
