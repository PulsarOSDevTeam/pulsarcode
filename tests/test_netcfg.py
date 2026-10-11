# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
import ssl

import pytest

from pulsar import netcfg


@pytest.mark.parametrize("host,local", [
    ("127.0.0.1:11434", True), ("[::1]:8080", True), ("::1", True), ("localhost", True), ("ollama.local:11434", True),
    ("192.168.1.5", True), ("10.0.0.2:8000", True), ("api.openai.com", False), ("8.8.8.8", False),
    ("[2606:4700::1111]:443", False), ("evil.com:80", False), ("", False), ("localhost.evil.com", False),
])
def test_local_hosts_are_never_proxied(host, local):
    assert netcfg.is_local_host(host) is local


def test_ssl_context_honours_bundle_env(tmp_path, monkeypatch):
    import certifi
    monkeypatch.setenv("PULSAR_CA_BUNDLE", certifi.where())
    netcfg.reset()
    try:
        ctx = netcfg.ssl_context()
        assert isinstance(ctx, ssl.SSLContext) and ctx.verify_mode == ssl.CERT_REQUIRED
        assert netcfg.ssl_context() is ctx
    finally:
        netcfg.reset()


def test_socks_only_proxy_is_skipped(monkeypatch):
    for v in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:7890")
    assert netcfg.env_http_proxy() == ""
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:7890")
    assert netcfg.env_http_proxy() == "http://127.0.0.1:7890"


@pytest.mark.skipif(not getattr(ssl, "VERIFY_X509_STRICT", 0), reason="no strict X.509 flag in this ssl module")
def test_strict_x509_is_relaxed_only_for_a_ca_bundle_the_user_configured(monkeypatch):
    # Python 3.13 rejects many corporate TLS-inspection CAs (no key usage extension) under
    # VERIFY_X509_STRICT; a bundle named on purpose is accepted like curl and Node accept it.
    import certifi
    strict = ssl.VERIFY_X509_STRICT
    for var in ("PULSAR_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
                "SSL_CERT_DIR"):
        monkeypatch.delenv(var, raising=False)
    try:
        monkeypatch.setenv("SSL_CERT_FILE", certifi.where())
        netcfg.reset()
        ctx = netcfg.ssl_context()
        assert not ctx.verify_flags & strict
        assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname   # chain and hostname still checked
        # the default store keeps whatever this Python enforces by default
        monkeypatch.delenv("SSL_CERT_FILE")
        monkeypatch.setenv("PULSAR_TRUSTSTORE", "0")
        netcfg.reset()
        default = netcfg.ssl_context()
        assert (default.verify_flags & strict) == (ssl.create_default_context().verify_flags & strict)
        # NODE_EXTRA_CA_CERTS adds a CA to the OS store, and verify flags cover a whole context: the store
        # and the extra CA are relaxed together (documented), still with chain and hostname checks
        monkeypatch.setenv("NODE_EXTRA_CA_CERTS", certifi.where())
        netcfg.reset()
        extra = netcfg.ssl_context()
        assert not extra.verify_flags & strict
        assert extra.verify_mode == ssl.CERT_REQUIRED and extra.check_hostname
    finally:
        netcfg.reset()
