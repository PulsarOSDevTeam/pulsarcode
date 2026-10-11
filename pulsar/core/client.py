# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Minimal stdlib client for Pulsar Core (statusline, MCP server, top, stage viewer).

Never routes through HTTP(S)_PROXY: Core is loopback-only. Never follows a
redirect, and refuses any base URL that is not plain http on loopback, so a
token can never be bounced to another host. A client that holds the admin
token makes Core prove it holds the same token (HMAC challenge on /healthz)
before the token leaves this process. Import-cheap so `pulsarcode
statusline` stays fast.

probe() tells four outcomes apart, because they call for different actions:
ok (proven Core), bad (something else answers: never trust it), busy (a
listener that is slow, overloaded or shedding connections: wait, do not
replace it), down (nothing listens: start a Core), plus closing (Core said
it is shutting down: do not attach to it).
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterator, Optional, Tuple

from pulsar.core import state as ST

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Pulsar Core never redirects; anything that does is not Core, so the request fails."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused (Pulsar Core never redirects)",
                                     headers, fp)


def opener() -> urllib.request.OpenerDirector:
    """A urllib opener for loopback Core traffic: no proxies, no redirects."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


_OPENER = opener()


class CoreUnavailable(Exception):
    """Core could not be reached or refused the call.

    `status` is the HTTP status when Core answered (0 otherwise). `refused` is True when nothing listens
    on Core's port any more (connection refused): the caller may start a Core again with
    `pulsar.core.spawn.revive(url)`.
    """

    def __init__(self, message: str = "", status: int = 0, refused: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.refused = refused


def is_loopback_url(url: str) -> bool:
    try:
        u = urllib.parse.urlsplit(url)
        return u.scheme == "http" and (u.hostname or "") in _LOOPBACK_HOSTS and not u.username and not u.password
    except ValueError:
        return False


def _refused(exc: BaseException) -> bool:
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, ConnectionRefusedError) or isinstance(exc, ConnectionRefusedError)


def _busy_error(exc: BaseException) -> bool:
    """A listener exists but did not answer in time, or dropped the connection without a reply."""
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, (socket.timeout, TimeoutError, ConnectionResetError, ConnectionAbortedError,
                               BrokenPipeError, http.client.RemoteDisconnected))


def _json(raw: bytes) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def probe(base_url: str, admin_token: str = "", timeout: float = 0.5) -> Tuple[str, Optional[Dict[str, Any]], bool]:
    """Ask whatever listens at base_url to prove it is the Core holding `admin_token`.

    Returns (state, data, refused):
      "ok"       a Pulsar Core answered with a valid proof (or, without admin_token, a well-formed health reply)
      "bad"      something answered, but it is not that Core (wrong or missing proof, not JSON, an error status)
      "busy"     a listener is there but slow or shedding load (503, timed out, dropped the connection): a
                 live Core under load looks like this, so callers wait instead of starting another Core
      "closing"  a Core said it is shutting down (unauthenticated: treat it as gone, never as trusted)
      "down"     nothing answered; `refused` is True for connection refused
    """
    if not is_loopback_url(base_url):
        return "bad", None, False
    nonce = ST.new_nonce() if admin_token else ""
    url = base_url.rstrip("/") + "/healthz" + ("?nonce=" + nonce if nonce else "")
    try:
        with _OPENER.open(url, timeout=timeout) as resp:
            raw = resp.read(4096)
    except urllib.error.HTTPError as exc:
        if exc.code != 503:
            return "bad", None, False
        try:
            body = _json(exc.read(4096)) or {}
        except Exception:
            body = {}
        if body.get("service") == "pulsar-core" and body.get("status") == "closing":
            return "closing", body, False
        return "busy", None, False
    except (urllib.error.URLError, OSError, ValueError) as exc:
        if _busy_error(exc):
            return "busy", None, False
        return "down", None, _refused(exc)
    except Exception:  # http.client.BadStatusLine and friends: a non-HTTP squatter
        return "bad", None, False
    data = _json(raw)
    if data is None or data.get("service") != "pulsar-core":
        return "bad", None, False
    if admin_token and not ST.proof_ok(admin_token, nonce, data.get("proof")):
        return "bad", data, False
    return "ok", data, False


class CoreClient:
    def __init__(self, base_url: str, token: str, sid: str = "") -> None:
        self.base = base_url.rstrip("/")
        self.token = token
        self.sid = sid
        self._proven = False

    # -- discovery ---------------------------------------------------------
    @classmethod
    def from_session_env(cls, environ: Optional[Dict[str, str]] = None) -> Optional["CoreClient"]:
        """Inside a Claude Code session (MCP server, statusline): act as that session."""
        env = environ if environ is not None else os.environ
        url = env.get("PULSAR_CORE", "")
        # Cloud and direct routes keep Claude Code's own credentials, so Core's session token rides
        # in PULSAR_SESSION_TOKEN; through Core it is the ANTHROPIC_AUTH_TOKEN Claude Code sends.
        token = env.get("PULSAR_SESSION_TOKEN", "") or env.get("ANTHROPIC_AUTH_TOKEN", "")
        if url and token.startswith(ST.SESSION_TOKEN_PREFIX) and is_loopback_url(url):
            return cls(url, token, env.get("PULSAR_SESSION", ""))
        return None

    @classmethod
    def admin(cls) -> Optional["CoreClient"]:
        """From the operator's shell (top, switch, stage viewer): use the admin token.

        The token is only sent after the process on Core's port proves it holds it (first call).
        """
        info = ST.read_core()
        if info is None:
            return None
        return cls(info.url, info.admin_token)

    @classmethod
    def any(cls) -> Optional["CoreClient"]:
        return cls.from_session_env() or cls.admin()

    # -- transport ---------------------------------------------------------
    def _check(self, timeout: float) -> None:
        """Refuse to send a token anywhere but a loopback Core that proved its identity (admin tokens)."""
        if not is_loopback_url(self.base):
            raise CoreUnavailable("refusing a Pulsar Core URL that is not http on loopback: %s" % self.base[:80])
        if self._proven or not self.token.startswith(ST.ADMIN_TOKEN_PREFIX):
            return
        state, _, refused = probe(self.base, self.token, timeout=max(0.3, min(timeout, 3.0)))
        if state == "ok":
            self._proven = True
            return
        if state == "down":
            raise CoreUnavailable("Pulsar Core is not running at %s" % self.base, refused=refused)
        if state == "busy":
            raise CoreUnavailable("Pulsar Core at %s is busy; try again in a moment" % self.base, status=503)
        if state == "closing":
            raise CoreUnavailable("Pulsar Core at %s is shutting down" % self.base, status=503)
        raise CoreUnavailable("the process on %s is not this user's Pulsar Core (identity proof failed); "
                              "the admin token was not sent" % self.base)

    def _req(self, method: str, path: str, body: Any = None, timeout: float = 5.0) -> urllib.request.Request:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        return req

    def call(self, method: str, path: str, body: Any = None, timeout: float = 5.0) -> Tuple[int, Any]:
        self._check(timeout)
        try:
            with _OPENER.open(self._req(method, path, body), timeout=timeout) as resp:
                raw = resp.read(32_000_000)
                status = resp.status
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise CoreUnavailable("Pulsar Core never redirects; refused a %d from %s" % (exc.code, self.base),
                                      status=exc.code)
            try:
                raw = exc.read(200_000)
            except OSError:
                raw = b""
            status = exc.code
        except (urllib.error.URLError, OSError) as exc:
            raise CoreUnavailable(str(getattr(exc, "reason", exc)), refused=_refused(exc))
        except Exception as exc:  # http.client protocol errors from something that is not Core
            raise CoreUnavailable("bad reply from %s: %s" % (self.base, type(exc).__name__))
        try:
            return status, json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            return status, raw.decode("utf-8", errors="replace")

    def get(self, path: str, timeout: float = 5.0) -> Any:
        status, data = self.call("GET", path, timeout=timeout)
        if status >= 400:
            raise CoreUnavailable("HTTP %d: %s" % (status, data), status=status)
        return data

    def post(self, path: str, body: Any, timeout: float = 30.0) -> Any:
        status, data = self.call("POST", path, body, timeout=timeout)
        if status >= 400:
            raise CoreUnavailable("HTTP %d: %s" % (status, data), status=status)
        return data

    def events(self, path: str = "/pulsar/events", timeout: float = 30.0) -> Iterator[Tuple[str, Any]]:
        """Server-sent events as (event, data). Ends when the stream closes."""
        self._check(timeout)
        req = self._req("GET", path)
        req.add_header("Accept", "text/event-stream")
        try:
            resp = _OPENER.open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            raise CoreUnavailable("HTTP %d from %s" % (exc.code, self.base), status=exc.code)
        except (urllib.error.URLError, OSError) as exc:
            raise CoreUnavailable(str(getattr(exc, "reason", exc)), refused=_refused(exc))
        except Exception as exc:
            raise CoreUnavailable("bad reply from %s: %s" % (self.base, type(exc).__name__))
        event, data_lines = "message", []
        with resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    if data_lines:
                        text = "\n".join(data_lines)
                        try:
                            payload: Any = json.loads(text)
                        except ValueError:
                            payload = text
                        yield event, payload
                    event, data_lines = "message", []
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())

    def healthy(self, timeout: float = 0.5) -> bool:
        """True when Core answers at this URL. A client holding the admin token only counts a Core that
        proves it holds that token, so an impostor on a stale port is never "healthy" to it."""
        if not is_loopback_url(self.base):
            return False
        if self.token.startswith(ST.ADMIN_TOKEN_PREFIX):
            return self.proven(timeout)
        try:
            with _OPENER.open(self.base + "/healthz", timeout=timeout) as resp:
                return resp.status == 200
        except Exception:
            return False

    def proven(self, timeout: float = 0.5) -> bool:
        """True when the Core at this URL proves it holds this client's admin token."""
        if not self.token.startswith(ST.ADMIN_TOKEN_PREFIX):
            return False
        try:
            self._check(timeout)
        except CoreUnavailable:
            return False
        return True
