# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Network settings shared by Core (httpx) and the CLI (urllib).

One TLS trust decision for every request Pulsar makes, so a key that
verifies in the CLI also works in Core behind a TLS-inspecting corporate
proxy (Zscaler, Netskope, ...):

  1. PULSAR_CA_BUNDLE, SSL_CERT_FILE, REQUESTS_CA_BUNDLE, CURL_CA_BUNDLE (first file that exists)
  2. else the OS trust store (truststore when installed, else ssl's defaults)
     plus certifi's bundle when it is available
  3. NODE_EXTRA_CA_CERTS is added on top when set (many corporate setups only set that one)

Python 3.13's VERIFY_X509_STRICT profile checks are relaxed for a context that
holds a CA the user configured (1, or 3): see _accept_private_ca. With 3 that
is the whole context, the OS store included; only 2 alone keeps Python's own
settings.

Local model servers (loopback, private ranges, *.local) never go through an
HTTP(S) proxy: a corporate proxy cannot reach your laptop's Ollama.
"""

from __future__ import annotations

import ipaddress
import os
import ssl
import threading
from typing import Optional

_lock = threading.Lock()
_ctx: Optional[ssl.SSLContext] = None


def _bundle_from_env() -> str:
    for var in ("PULSAR_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        path = os.environ.get(var, "")
        if path and os.path.isfile(path):
            return path
    return ""


def ssl_context() -> ssl.SSLContext:
    """The client TLS context for every outbound HTTPS request (cached)."""
    global _ctx
    with _lock:
        if _ctx is not None:
            return _ctx
        bundle = _bundle_from_env()
        if bundle:
            ctx = ssl.create_default_context(cafile=bundle)
            _accept_private_ca(ctx)
        else:
            ctx = None
            if os.environ.get("PULSAR_TRUSTSTORE", "1") != "0":
                try:
                    import truststore  # type: ignore[import-not-found]
                    ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                except Exception:
                    ctx = None
            if ctx is None:
                ctx = ssl.create_default_context()
            cert_dir = os.environ.get("SSL_CERT_DIR", "")
            try:
                if cert_dir and os.path.isdir(cert_dir):
                    ctx.load_verify_locations(capath=cert_dir)
            except (OSError, ssl.SSLError):
                pass
            try:
                import certifi  # type: ignore[import-not-found]
                ctx.load_verify_locations(cafile=certifi.where())
            except Exception:
                pass
        extra = os.environ.get("NODE_EXTRA_CA_CERTS", "")
        if extra and os.path.isfile(extra):
            try:
                ctx.load_verify_locations(cafile=extra)
                _accept_private_ca(ctx)
            except (OSError, ssl.SSLError):
                pass
        _ctx = ctx
        return ctx


def _accept_private_ca(ctx: ssl.SSLContext) -> None:
    """Accept a CA the user configured on purpose the way curl, Node and Python 3.12 do.

    Python 3.13 turned on VERIFY_X509_STRICT in create_default_context. Many corporate TLS-inspection
    CAs lack the key usage extension it demands, so every request behind such a proxy failed even with
    the company bundle configured. Chain, hostname and expiry checks all stay; only the RFC 5280
    profile checks are relaxed. Verify flags belong to a whole context, never to one CA in it, so when
    NODE_EXTRA_CA_CERTS adds a CA to the OS store the relaxation covers the OS store too (as in Python
    3.12, curl and Node); naming a bundle in PULSAR_CA_BUNDLE instead limits trust to that bundle.
    """
    flag = getattr(ssl, "VERIFY_X509_STRICT", 0)
    if not flag:
        return
    try:
        ctx.verify_flags &= ~flag
    except (AttributeError, ValueError):
        pass


def reset() -> None:
    """Forget the cached context (tests)."""
    global _ctx
    with _lock:
        _ctx = None


def env_http_proxy() -> str:
    """The HTTP(S) proxy from the environment, ignoring SOCKS-only settings (used when SOCKS support is missing)."""
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        val = os.environ.get(var, "")
        if val.startswith(("http://", "https://")):
            return val
    return ""


def is_local_host(host: str) -> bool:
    """Loopback, private, link-local or mDNS (.local) host: never proxied."""
    from urllib.parse import urlsplit
    raw = (host or "").strip().lower()
    if raw.count(":") > 1 and not raw.startswith("["):
        h = raw                             # a bare IPv6 address
    else:
        try:
            h = urlsplit("//" + raw).hostname or ""   # host, host:port, [v6]:port
        except ValueError:
            return False
    if not h:
        return False
    if h in ("localhost", "host.docker.internal") or h.endswith((".localhost", ".local")):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local
