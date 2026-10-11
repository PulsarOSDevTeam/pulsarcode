# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Pulsar Core HTTP server.

Security model
  * binds 127.0.0.1 only (exclusively on Windows); the spawner only trusts a Core that answers
    GET /healthz?nonce=<hex> with HMAC-SHA256(admin_token, nonce), so a squatter on the port is never
    handed keys, the admin token or a Claude Code session
  * every route except /healthz and /api/hello needs a bearer token:
      session token  -> that session only (gateway, stage write, council, its own stats/events/scenes)
      admin token    -> everything observational + control (run/core.json, 0600)
      view token     -> read-only; per-session (plv_ derived for a sid) sees only that session, the
                        global one (derived for sid "") sees everything, like the admin token
  * Host must be 127.0.0.1:<port> or localhost:<port> (defeats DNS rebinding)
  * a browser Origin other than our own is refused (defeats drive-by CSRF from web pages)
  * request bodies are capped (413), parsed off the event loop when large; request heads must arrive
    within PULSAR_CORE_HEADER_TIMEOUT seconds; at most PULSAR_MAX_CONNS connections stay open (beyond that
    the longest-idle one is closed, a request in progress never is), so idle or half-open sockets can
    neither exhaust file descriptors nor push Core into refusing work; upstream redirects are never
    followed

Plain Starlette (no FastAPI, no pydantic): every dependency is pure Python, so Core installs without a
compiler on any platform Python runs on.

Lifecycle: a Core exits once no session has been live for a while. A Core that is no longer the one
run/core.json names (an update started a Core of the new code beside it, see spawn.attach) retires: it
keeps serving the sessions that were launched against it and exits as soon as none of them is live.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import base64
import concurrent.futures
import hashlib
import hmac
import importlib.util
import json
import math
import os
import signal
import socket
import sys
import time
import traceback
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Any, Callable, Dict, List, Optional, Tuple

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from pulsar import __version__
from pulsar import config as K
from pulsar import providers as P
from pulsar import translate as T
from pulsar.core import state as ST
from pulsar.core.engine import Engine, OverflowEvent, Tuning, http_opt_in
from pulsar.core.stats import Stats
from pulsar.redact import redact
from pulsar.stage import pane as PANE


def _knob(name: str, default: float, lo: float = 0.0, hi: float = float("inf")) -> float:
    """A numeric PULSAR_* setting. Anything unparsable, NaN, infinite or out of [lo, hi] means the
    default: a typo in an environment variable must never stop Core from starting."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        val = float(raw)
    except ValueError:
        return default
    if math.isnan(val) or math.isinf(val) or val < lo or val > hi:
        return default
    return val


# Anthropic's own Messages API caps a request at 32 MB; nothing legitimate is larger.
MAX_BODY = int(_knob("PULSAR_MAX_BODY", 32 * 1024 * 1024, lo=4096, hi=1 << 34))
PARSE_IN_THREAD = 1024 * 1024        # JSON bodies above this are parsed off the event loop
# An oversized body from an authenticated caller is read and discarded (never stored), up to this much
# and this long, so the client can read our 413; closing with unread data would make most clients see
# a connection reset instead. Unauthenticated oversized requests get the 413 and are closed at once.
DRAIN_CAP = 4 * MAX_BODY
DRAIN_SECONDS = 2.0
# Seconds an inbound connection has to send a request head (PULSAR_CORE_HEADER_TIMEOUT). Not
# PULSAR_HEADER_TIMEOUT: that one is the wait for a provider's first byte (engine.Tuning), which users
# raise for slow models.
HEADER_TIMEOUT = 15.0
KEEP_ALIVE = 15
GRACEFUL_SHUTDOWN = 8
FD_LIMIT = 8192
MAX_CONNS = 4096                     # open connections; see _HeaderDeadline (PULSAR_MAX_CONNS overrides)
MAX_TOKENS_LIMIT = 1 << 31           # far above any model's output budget, below every wire's integer type
# JSON nesting is refused beyond this depth on every Python (3.9 to 3.11 stop near 1000 on their own,
# 3.12+ parse far deeper); no Messages request comes anywhere near it.
MAX_DEPTH = 512


class BodyError(ValueError):
    def __init__(self, status: int, kind: str, message: str) -> None:
        super().__init__(message)
        self.status, self.kind, self.message = status, kind, message


def _error(status: int, kind: str, msg: str) -> JSONResponse:
    return JSONResponse({"type": "error", "error": {"type": kind, "message": msg}}, status_code=status)


def _too_large() -> JSONResponse:
    return _error(413, "request_too_large", "request body is larger than %d MiB" % (MAX_BODY >> 20))


async def _drain(receive: Any, declared: int) -> None:
    """Read and drop a request body we refuse, bounded in size and time."""
    if declared > DRAIN_CAP:
        return

    async def run() -> None:
        seen = 0
        while seen <= DRAIN_CAP:
            msg = await receive()
            if msg.get("type") != "http.request":
                return
            seen += len(msg.get("body") or b"")
            if not msg.get("more_body"):
                return
    try:
        await asyncio.wait_for(run(), timeout=DRAIN_SECONDS)
    except asyncio.TimeoutError:
        pass


def _nested_ok(obj: Any, limit: int = MAX_DEPTH) -> bool:
    """True when `obj` (parsed JSON) nests at most `limit` containers deep. Iterative: never recurses."""
    stack = [(obj, 1)]
    while stack:
        o, d = stack.pop()
        if isinstance(o, (dict, list)):
            if d > limit:
                return False
            for v in (o.values() if isinstance(o, dict) else o):
                if isinstance(v, (dict, list)):
                    stack.append((v, d + 1))
    return True


def _token_of(headers: Dict[str, str]) -> str:
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return headers.get("x-api-key", "").strip()


class Guard:
    """Pure ASGI front door: Host (DNS rebinding), Origin (CSRF) and declared body size, before routing."""

    def __init__(self, app: Any, port: int = 0, authed: Optional[Callable[[str], bool]] = None) -> None:
        self.app = app
        self.port = port
        self.authed = authed
        self.hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port, "[::1]:%d" % port}
        self.origins = {"http://" + h for h in self.hosts}

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        kind = scope.get("type")
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers: Dict[str, str] = {}
        for k, v in scope.get("headers") or []:
            name = k.decode("latin-1").lower()
            if name not in headers:
                headers[name] = v.decode("latin-1")
        problem: Optional[JSONResponse] = None
        if self.port and headers.get("host", "") not in self.hosts:
            problem = _error(421, "forbidden", "Pulsar Core only answers on 127.0.0.1")
        elif headers.get("origin") and headers["origin"] not in self.origins:
            problem = _error(403, "forbidden", "cross-origin requests are refused")
        else:
            length = headers.get("content-length", "").strip()
            if length and not all("0" <= c <= "9" for c in length):
                problem = _error(400, "invalid_request_error", "bad content-length")
            elif length and (len(length) > 15 or int(length) > MAX_BODY):
                problem = _too_large()
                tok = _token_of(headers)
                if kind == "http" and tok and self.authed is not None and self.authed(tok):
                    await _drain(receive, int(length) if len(length) <= 15 else DRAIN_CAP + 1)
                else:
                    # Nobody we know: answer and hang up rather than hold the connection for a body.
                    problem.headers["connection"] = "close"
        if problem is None:
            await self.app(scope, receive, send)
        elif kind == "websocket":
            await send({"type": "websocket.close", "code": 1008})
        else:
            await problem(scope, receive, send)


# A file's stat signature is trusted once its mtime is older than this. Within one timestamp tick (2 s on
# FAT/exFAT, 1 s on HFS+, a few ms on Linux) a second os.replace() of the same size can reuse the inode
# the first one freed and look identical, so a younger file is compared by content.
SETTLE = 2.5
SESSION_CAP = 1 << 20                # a session file is a few hundred bytes; anything this large is not one
KEYS_CAP = 1 << 22


def _sig(st: os.stat_result) -> Tuple[int, int, int]:
    return st.st_mtime_ns, st.st_size, st.st_ino


def _settled(st: os.stat_result) -> bool:
    return time.time() - st.st_mtime > SETTLE


def _read_small(path: Any, cap: int) -> Tuple[os.stat_result, bytes]:
    """(stat, content) of one open file: the signature always describes the bytes read (no replace in between)."""
    with open(str(path), "rb") as fh:
        return os.fstat(fh.fileno()), fh.read(cap + 1)


class Keys:
    """keys.json (reloaded on change) + in-memory ephemeral keys + Core's own env."""

    def __init__(self) -> None:
        self._sig: Optional[Tuple[int, ...]] = None   # None: never read; (): the file is missing
        self._digest = b""
        self._keys: Dict[str, str] = {}
        self.ephemeral: Dict[str, str] = {}

    def _refresh(self) -> None:
        path = K.keys_path()
        try:
            st: Optional[os.stat_result] = os.stat(str(path))
        except FileNotFoundError:
            st = None
        except OSError:
            return  # unreadable for a moment (a replace in progress on Windows): keep the keys we have
        if st is None:
            if self._sig != ():
                self._keys, self._sig, self._digest = K.load_keys(), (), b""
            return
        sig = _sig(st)
        if sig == self._sig and _settled(st):
            return
        try:
            raw = _read_small(path, KEYS_CAP)[1]
        except OSError:
            return
        digest = hashlib.sha256(raw).digest()
        if digest != self._digest:
            self._keys = K.load_keys()
        self._sig, self._digest = sig, digest

    def get(self, profile: str) -> str:
        if profile in self.ephemeral:
            return self.ephemeral[profile]
        self._refresh()
        if profile in self._keys:
            return self._keys[profile]
        prov = P.get(profile.split("-")[0]) if profile else None
        if prov is not None:
            for var in prov.env:
                if os.environ.get(var):
                    return os.environ[var].strip()
        return ""


class _Known:
    __slots__ = ("sid", "sig", "digest", "sess")

    def __init__(self, sid: str, sig: Tuple[int, int, int], digest: bytes, sess: ST.Session) -> None:
        self.sid, self.sig, self.digest, self.sess = sid, sig, digest, sess


class Sessions:
    """Token -> Session, re-reading a session file whenever it changes (hot swap).

    A change is seen even when two writes land within one timestamp tick with the same size (two
    `pulsarcode switch` in a row between model ids of equal length): see SETTLE.
    """

    SERVED_KEEP = 4096

    def __init__(self, on_seen: Optional[Callable[[ST.Session], Any]] = None) -> None:
        self.on_seen = on_seen          # called with every session a token resolves to
        self._by_hash: Dict[str, _Known] = {}
        self._served: "OrderedDict[str, float]" = OrderedDict()   # sid -> last request, newest last
        self._scanned: Dict[str, Tuple[int, int, int]] = {}       # path -> signature when _scan last read it

    @staticmethod
    def _load(path: Any) -> Optional[Tuple[Tuple[int, int, int], bytes, ST.Session]]:
        """(signature, digest, session) of a session file; None when it is not a valid one. OSError passes."""
        st, raw = _read_small(path, SESSION_CAP)
        if len(raw) > SESSION_CAP:
            return None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            return None
        sess = ST.Session.from_json(data) if isinstance(data, dict) else None
        if sess is None:
            return None
        return _sig(st), hashlib.sha256(raw).digest(), sess

    def _scan(self) -> None:
        """Learn the session files written since the last scan. A file this scan already read, unchanged
        and settled since, costs one stat: an unknown token, however often it is tried, never makes Core
        re-read every session file on the event loop."""
        try:
            files = sorted(ST.sessions_dir().glob("*.json"))
        except OSError:
            return
        scanned: Dict[str, Tuple[int, int, int]] = {}
        for f in files:
            key = str(f)
            try:
                st = os.stat(key)
            except OSError:
                continue
            if self._scanned.get(key) == _sig(st) and _settled(st):
                scanned[key] = _sig(st)
                continue
            try:
                got = self._load(f)
            except OSError:
                continue
            scanned[key] = got[0] if got is not None else _sig(st)
            if got is None or ST.session_path(got[2].sid) != f:
                continue
            sig, digest, sess = got
            self._by_hash[sess.token_sha256] = _Known(sess.sid, sig, digest, sess)
        self._scanned = scanned

    def _fresh(self, h: str, known: _Known) -> Optional[ST.Session]:
        path = ST.session_path(known.sid)
        try:
            st = os.stat(str(path))
        except FileNotFoundError:
            self._by_hash.pop(h, None)      # revoked (the launcher's lease ended, or the file was removed)
            return None
        except OSError:
            return known.sess               # unreadable for a moment (a replace in progress on Windows)
        if _sig(st) == known.sig and _settled(st):
            return known.sess
        try:
            got = self._load(path)
        except FileNotFoundError:
            self._by_hash.pop(h, None)
            return None
        except OSError:
            return known.sess
        if got is None or got[2].token_sha256 != h or got[2].sid != known.sid:
            self._by_hash.pop(h, None)
            return None
        sig, digest, sess = got
        if digest == known.digest:
            known.sig = sig                 # the same content (rewritten unchanged, or only settled since)
            return known.sess
        self._by_hash[h] = _Known(known.sid, sig, digest, sess)
        return sess

    def lookup(self, token: str) -> Optional[ST.Session]:
        if not token.startswith(ST.SESSION_TOKEN_PREFIX):
            return None
        h = ST.token_hash(token)
        known = self._by_hash.get(h)
        if known is None:
            self._scan()
            known = self._by_hash.get(h)
            if known is None:
                return None
        sess = self._fresh(h, known)
        if sess is not None:
            if self.on_seen is not None:
                self.on_seen(sess)
            self._served[sess.sid] = time.time()
            self._served.move_to_end(sess.sid)
            while len(self._served) > self.SERVED_KEEP:
                self._served.popitem(last=False)
        return sess

    def served(self, sid: str) -> bool:
        """True when `sid` has called this Core (so its Claude Code, MCP server or status line points here)."""
        return sid in self._served

    def get(self, sid: str) -> Optional[ST.Session]:
        for known in list(self._by_hash.values()):
            if known.sid == sid:
                return known.sess
        return ST.load_session(sid)


class StageBus:
    def __init__(self, keep: int = 40) -> None:
        self.scenes: List[Dict[str, Any]] = []
        self.keep = keep
        self.subs: List["asyncio.Queue[Dict[str, Any]]"] = []
        # The sid each subscriber is limited to (None: a global viewer that sees every session).
        self.scope: Dict[Any, Optional[str]] = {}

    def subscribe(self, only: Optional[str]) -> "asyncio.Queue[Dict[str, Any]]":
        q: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue(maxsize=200)
        self.subs.append(q)
        self.scope[q] = only
        return q

    def unsubscribe(self, q: Any) -> None:
        if q in self.subs:
            self.subs.remove(q)
        self.scope.pop(q, None)

    def viewers(self, sid: Optional[str] = None) -> int:
        """Open viewers. With a sid, only those that would show that session's scenes (its own viewers and
        global ones): a session never learns about, or is held back by, another session's viewers."""
        if sid is None:
            return len(self.subs)
        return sum(1 for q in self.subs if self.scope.get(q) in (None, sid))

    def push(self, sid: str, scene: Dict[str, Any], op: str = "show") -> Dict[str, Any]:
        item = {"sid": sid, "op": op, "scene": scene, "t": time.time()}
        if op == "clear":
            self.scenes = [s for s in self.scenes if s["sid"] != sid]
        else:
            self.scenes.append(item)
            self.scenes = self.scenes[-self.keep:]
        for q in list(self.subs):
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                pass
        return item


def _bearer(req: Request) -> str:
    auth = req.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return req.headers.get("x-api-key", "").strip()


def _bad_max_tokens(body: Dict[str, Any]) -> str:
    """Why body["max_tokens"] is unusable, or "" (absent, null, or a positive integer). An integral float
    such as 1024.0 is normalized in place."""
    if "max_tokens" not in body or body["max_tokens"] is None:
        return ""
    v = body["max_tokens"]
    if isinstance(v, float) and math.isfinite(v) and v.is_integer():
        v = body["max_tokens"] = int(v)
    if isinstance(v, bool) or not isinstance(v, int):
        return "max_tokens: must be an integer"
    if not 1 <= v <= MAX_TOKENS_LIMIT:
        return "max_tokens: must be between 1 and %d" % MAX_TOKENS_LIMIT
    return ""


def _finite(v: Any) -> bool:
    try:
        return math.isfinite(float(v))
    except (OverflowError, ValueError, TypeError):
        return False


_NUMBERS = ("temperature", "top_p", "top_k")
_LISTS = ("messages", "tools", "stop_sequences")


def _bad_request(body: Dict[str, Any]) -> str:
    """Anthropic's own type checks for the Messages fields that translation relies on, so a malformed
    request gets a 400 invalid_request_error instead of a 500. Null optional fields are dropped."""
    problem = _bad_max_tokens(body)
    if problem:
        return problem
    for name in _NUMBERS + _LISTS[1:]:
        if name in body and body[name] is None:
            del body[name]
    for name in _NUMBERS:
        v = body.get(name)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not _finite(v)):
            return "%s: must be a number" % name
    for name in _LISTS:
        if name in body and not isinstance(body[name], list):
            return "%s: must be a list" % name
    if any(not isinstance(x, str) for x in body.get("stop_sequences") or ()):
        return "stop_sequences: must be a list of strings"
    return _bad_content(body.get("messages") or ())


def _bad_source(block: Any) -> str:
    """Why an image or document block's source is unusable (it must be an object), or ""."""
    if isinstance(block, dict) and block.get("type") in ("image", "document"):
        src = block.get("source")
        if src is not None and not isinstance(src, dict):
            return "source: must be an object"
    return ""


def _bad_content(messages: Any) -> str:
    """Anthropic's check on image and document sources, in messages and in tool results, so a malformed
    block is a 400 here rather than a fault in translation or a 400 that trips a provider's breaker."""
    for i, m in enumerate(messages):
        content = m.get("content") if isinstance(m, dict) else None
        if not isinstance(content, list):
            continue
        for j, b in enumerate(content):
            problem = _bad_source(b)
            if problem:
                return "messages.%d.content.%d.%s" % (i, j, problem)
            inner = b.get("content") if isinstance(b, dict) and b.get("type") == "tool_result" else None
            for k, c in enumerate(inner if isinstance(inner, list) else ()):
                problem = _bad_source(c)
                if problem:
                    return "messages.%d.content.%d.content.%d.%s" % (i, j, k, problem)
    return ""


_NO_STORE = {"Cache-Control": "no-store"}
_TRUE = ("1", "true", "yes", "on")

# The minimal Stage viewer, served when pulsar.stage.web cannot build its page. Like the full one, its one
# inline script runs only by its hash.
_STAGE_FALLBACK_JS = ("new EventSource('/pulsar/stage/stream'+location.search).addEventListener('scene',e=>{"
                      "o.textContent=JSON.stringify(JSON.parse(e.data).scene,null,2)})")
_STAGE_FALLBACK = ("<!doctype html><title>Pulsar Stage</title><pre id=o></pre><script>%s</script>"
                   % _STAGE_FALLBACK_JS)
_STAGE_FALLBACK_CSP = ("default-src 'none'; script-src 'sha256-%s'; connect-src 'self'; base-uri 'none'; "
                       "form-action 'none'" % base64.b64encode(hashlib.sha256(
                           _STAGE_FALLBACK_JS.encode("utf-8")).digest()).decode("ascii"))


async def _http_error(request: Request, exc: Exception) -> Response:
    """Routing errors (405 for a method no route takes) as JSON {"detail": ...}, as Core always answered them."""
    status = getattr(exc, "status_code", 500)
    headers = getattr(exc, "headers", None)
    if status < 200 or status in (204, 205, 304):
        return Response(status_code=status, headers=headers)
    return JSONResponse({"detail": getattr(exc, "detail", "")}, status_code=status, headers=headers)


def _log_fault(where: str, exc: BaseException) -> None:
    """An unexpected exception, to Core's log (stderr): the traceback, with keys and Bearer values masked."""
    try:
        text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        sys.stderr.write("pulsar core: %s failed: %s\n" % (where, redact(text)))
        sys.stderr.flush()
    except Exception:
        pass


def _core_log() -> str:
    """The file Core's log (its stderr) is written to when that is Pulsar's own logs/core.log, else "".

    spawn only points stderr there with PULSAR_LOG=1 (otherwise at the null device), so this asks the open
    file itself rather than the environment: a Core started without PULSAR_LOG keeps no log, whatever a
    later launch sets."""
    try:
        path = ST.run_dir().parent / "logs" / "core.log"
        if os.path.samestat(os.fstat(sys.stderr.fileno()), os.stat(str(path))):
            return str(path)
    except (OSError, ValueError, AttributeError):
        pass
    return ""


def _tilde(path: str) -> str:
    """`path` with the user's home directory shown as ~."""
    home = os.path.expanduser("~").rstrip("/\\")
    if home and home != "~" and path.startswith(home) and path[len(home):len(home) + 1] in ("/", "\\"):
        return "~" + path[len(home):]
    return path


def _fault_text(exc: BaseException) -> str:
    """The notice for a fault inside Core: what happened, and where the details are (or how to get them)."""
    log = _core_log()
    if log:
        where = "The details are in %s." % _tilde(log)
    else:
        where = ("To record the details, quit Claude Code, run `pulsarcode stop`, then start again with "
                 "`PULSAR_LOG=1 pulsarcode`: Core then writes them to %s."
                 % _tilde(str(ST.run_dir().parent / "logs" / "core.log")))
    return ("◉ Pulsar Code hit an internal error (%s) before the provider answered.\n"
            "This is a fault in Pulsar Code, not in your provider or key. If it happens on every message, something "
            "in this conversation triggers it: `/clear` starts a fresh one.\n%s" % (type(exc).__name__, where))


def _fault_stream(exc: BaseException, model: str) -> str:
    """A complete reply (SSE) that tells the user Core itself failed, instead of an HTTP 500."""
    tr = T.StreamTranslator(P.clean_text(model, 120) or "pulsar")
    return "".join(T.sse(ev) for ev in tr.start() + tr.finish(_fault_text(exc)))


def _fault_message(exc: BaseException, model: str) -> Dict[str, Any]:
    """The same notice as a complete (non-streaming) Messages reply."""
    return T.text_message(_fault_text(exc), P.clean_text(model, 120) or "pulsar")


def _route(path: str, endpoint: Callable[..., Any], methods: List[str]) -> Route:
    """A Route that takes exactly `methods`. Starlette adds HEAD to every GET route; Core never answered
    HEAD there (it falls through to the 404 catch-all), and keeps it that way."""
    route = Route(path, endpoint, methods=methods)
    route.methods = {m.upper() for m in methods}
    return route


def _raster_reply(status: int, body: Dict[str, Any]) -> Response:
    if status == 204:
        return Response(status_code=204, headers=_NO_STORE)
    return JSONResponse(body, status_code=status, headers=_NO_STORE)


def _successor(pid: int, since: float) -> Optional[ST.CoreInfo]:
    """The Core run/core.json names when it is another live process, published after `since` (this Core's
    start): new launches attach to that one, so this Core only serves the sessions it already has."""
    info = ST.read_core()
    if info is None or info.pid == pid or info.started <= since or not ST.pid_alive(info.pid):
        return None
    return info


def _proxy_tolerant(client: Callable[[str], Any]) -> Callable[[str], Any]:
    """Wrap Engine.client so a proxy variable httpx cannot use does not fail every request.

    httpx.AsyncClient(trust_env=True) raises ValueError for ALL_PROXY=socks4://... (any scheme but http,
    https, socks5, socks5h), which would surface as a bare 500 on every /v1/messages. spawn already leaves
    such variables out of Core's env; this covers a Core started by hand (or make_app in-process): the client
    is built with those variables hidden, so the remaining HTTP(S) proxy, or none, is used. The note names
    the variable and scheme only, never the value (a proxy URL can carry a password).
    """
    told: List[str] = []

    def build(url: str) -> Any:
        try:
            return client(url)
        except ImportError:
            raise
        except Exception:
            bad = ST.unusable_proxies()
            if not bad:
                raise
        saved = {var: os.environ.pop(var) for var in bad if var in os.environ}
        try:
            return client(url)          # synchronous: nothing else runs on the loop while the vars are hidden
        finally:
            os.environ.update(saved)
            for var in bad:
                if var not in told:
                    told.append(var)
                    print("pulsarcode core: ignoring %s: its %s:// proxy is not supported; use socks5:// or "
                          "http:// instead" % (var, ST.proxy_scheme(saved.get(var, ""))), file=sys.stderr)

    return build


def make_app(admin_token: str, port: int, transport: Any = None, tuning: Optional[Tuning] = None) -> Starlette:
    hooks: List[Callable[[], Any]] = []

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        yield
        # Runs during uvicorn's graceful shutdown, before it re-raises SIGTERM (which would otherwise kill
        # the process before any `finally` and leave run/core.json behind).
        app.state.closing = True
        for fn in list(hooks):
            try:
                fn()
            except Exception:
                pass

    stats = Stats()
    keys = Keys()
    sessions = Sessions()
    stage = StageBus()
    engine = Engine(stats, keys.get, K.home(), transport=transport, tuning=tuning)
    engine.client = _proxy_tolerant(engine.client)   # type: ignore[method-assign]
    # The first time any caller (status line, MCP server, Claude Code) shows up as a session, its plan[0]
    # model is the launch model Claude Code keeps sending: see state.ModelPicks.
    sessions.on_seen = engine.picks.observe
    build = ST.build_id(refresh=True)   # the code this process runs, fixed at start (see spawn.attach)
    # A Claude Code Stage pane (pulsar/fnplug) polls /pulsar/stage/raster about every 0.6 s with
    # X-Pulsar-Viewer; it counts as a viewer of its session for 3 s after each poll.
    panes = PANE.PaneSeen(ttl=3.0)
    pane_sids: "OrderedDict[str, None]" = OrderedDict()
    # Frames are drawn off the event loop (a 512x256 frame takes tens of ms), one at a time: that bounds the
    # CPU a polling pane can take, and pulsar.stage.raster keeps a frame cache that is not thread-safe.
    raster_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="pulsar-raster")
    hooks.append(lambda: raster_pool.shutdown(wait=False))

    def leaving() -> bool:
        srv = app.state.server
        return bool(app.state.closing or app.state.shutdown_requested or (srv is not None and srv.should_exit))

    def same(a: str, b: str) -> bool:
        return bool(a) and bool(b) and hmac.compare_digest(a.encode("utf-8", "replace"), b.encode("utf-8", "replace"))

    def is_admin(tok: str) -> bool:
        return same(tok, admin_token)

    def view_scope(tok: str, sid: str = "") -> Optional[str]:
        """"" for the global view token, the sid for a per-session one, None when tok is not a view token."""
        if not tok.startswith(ST.VIEW_TOKEN_PREFIX):
            return None
        if sid and same(tok, ST.view_token(admin_token, sid)):
            return sid
        if same(tok, ST.view_token(admin_token, "")):
            return ""
        return None

    def is_viewer(tok: str, sid: str = "") -> bool:
        return is_admin(tok) or view_scope(tok, sid) is not None

    def ours(live: List[ST.Session]) -> List[ST.Session]:
        """The live sessions a retiring Core still serves: launched before it retired, or calling it."""
        cutoff = app.state.retiring
        return [s for s in live if s.created <= cutoff or sessions.served(s.sid)]

    def retire(cutoff: float) -> int:
        """Stop being the Core new launches use; keep serving the sessions launched up to `cutoff`, and any
        session that calls us. Returns how many live sessions still hold this Core."""
        if not app.state.retiring or cutoff < app.state.retiring:
            app.state.retiring = cutoff
        return len(ours(ST.live_sessions(reap=False)))

    def note_pane(sid: str, header: str) -> None:
        panes.note(sid, header)
        if panes.state(sid):
            pane_sids[sid] = None
            pane_sids.move_to_end(sid)
            while len(pane_sids) > 512:
                pane_sids.popitem(last=False)

    def viewers(sid: Optional[str]) -> int:
        """Open Stage viewers that show `sid`'s scenes (every session's for None): web and terminal viewers
        on the stage stream, plus a Claude Code Stage pane that polled within the last 3 s."""
        n = stage.viewers(sid)
        if sid:
            return n + (1 if panes.active(sid) else 0)
        return n + sum(1 for s in list(pane_sids) if panes.active(s))

    err = _error

    def side_channel_refusal() -> JSONResponse:
        return err(403, "permission_error", "Pulsar Core: this session talks to its provider directly (cloud or "
                   "direct route); its token grants only this session's Stage, stats and council.")

    async def read_json(request: Request) -> Any:
        """The request body as JSON. Raises BodyError (413 over MAX_BODY, 400 for anything unparsable)."""
        buf = bytearray()
        total = 0
        deadline = 0.0
        async for chunk in request.stream():
            if not chunk:
                continue
            total += len(chunk)
            if total <= MAX_BODY:
                buf += chunk
                continue
            # Over the cap (a chunked body has no content-length for Guard to check): stop storing, drain
            # a bounded amount so the client can read the 413, then give up on the rest.
            if not deadline:
                buf = bytearray()
                deadline = time.time() + DRAIN_SECONDS
            if total > DRAIN_CAP or time.time() > deadline:
                break
        if total > MAX_BODY:
            raise BodyError(413, "request_too_large", "request body is larger than %d MiB" % (MAX_BODY >> 20))
        request.state.body_size = len(buf)
        if not buf:
            return {}
        big = len(buf) > PARSE_IN_THREAD
        try:
            obj = await asyncio.to_thread(json.loads, buf) if big else json.loads(buf)
        except (ValueError, RecursionError):
            raise BodyError(400, "invalid_request_error", "request body is not valid JSON")
        # The same nesting limit on every Python version (3.12+ parse far deeper than 3.9 to 3.11 do).
        if buf.count(b"[") + buf.count(b"{") > MAX_DEPTH and \
                not (await asyncio.to_thread(_nested_ok, obj) if big else _nested_ok(obj)):
            raise BodyError(400, "invalid_request_error", "request body nests deeper than %d levels" % MAX_DEPTH)
        return obj

    def bad_body(exc: BodyError) -> JSONResponse:
        return err(exc.status, exc.kind, exc.message)

    # -- public ----------------------------------------------------------------
    async def healthz(request: Request) -> Response:
        # Public, so it carries nothing an attacker can replay: no pid, no session count. With
        # ?nonce=<hex> it adds hex(HMAC-SHA256(admin_token, nonce)), which only this process can compute,
        # and the build of the code it runs (spawn.attach starts a new Core when that is not the code on disk).
        out: Dict[str, Any] = {"status": "ok", "service": "pulsar-core", "version": __version__}
        if leaving():
            # Never prove ourselves while going away: a launch must not hand Claude Code a URL that is
            # about to die (and could then be squatted). It starts a fresh Core instead.
            out["status"] = "closing"
            return JSONResponse(out, status_code=503, headers={"connection": "close"})
        nonce = request.query_params.get("nonce", "")
        if nonce:
            if not ST.valid_nonce(nonce):
                return err(400, "invalid_request_error", "nonce must be 32 to 128 lowercase hex characters")
            out["proof"] = ST.core_proof(admin_token, nonce)
            out["build"] = build
            if app.state.retiring:
                out["retiring"] = True
            # A launcher verifies us right before it registers its session: stay up long enough for that.
            app.state.last_proof = time.time()
        return JSONResponse(out)

    async def hello(request: Request) -> Response:
        # Claude Code probes this (unauthenticated) when it starts against a custom base URL.
        return JSONResponse({"ok": True})

    # -- gateway -----------------------------------------------------------------
    async def messages(request: Request) -> Response:
        sess = sessions.lookup(_bearer(request))
        if sess is None:
            return err(401, "authentication_error", "Pulsar Core: unknown session token. Relaunch with `pulsarcode`.")
        if sess.side_channel:
            return side_channel_refusal()
        app.state.last_activity = time.time()
        try:
            body = await read_json(request)
        except BodyError as exc:
            return bad_body(exc)
        if not isinstance(body, dict):
            return err(400, "invalid_request_error", "body must be a JSON object")
        problem = _bad_request(body)
        if problem:
            return err(400, "invalid_request_error", problem)
        incoming = {k: request.headers.get(k, "") for k in ("anthropic-version", "anthropic-beta")}
        if body.get("stream"):
            # Peek at the first event (a ping arrives within PULSAR_PING_INTERVAL while a target is opened):
            # a context overflow before anything was sent is answered as a real HTTP 400, as Anthropic does.
            gen = engine.stream(sess, body, incoming)
            try:
                first: Optional[str] = await gen.__anext__()
            except StopAsyncIteration:
                first = None
            except Exception as exc:
                # A fault before the first byte never becomes a bare HTTP 500 (Claude Code would re-send the
                # whole request ten times over minutes): it is logged and answered once, readably.
                _log_fault("/v1/messages", exc)
                return StreamingResponse(iter([_fault_stream(exc, str(body.get("model") or "pulsar"))]),
                                         media_type="text/event-stream",
                                         headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
            if isinstance(first, OverflowEvent):
                await gen.aclose()
                return err(400, "invalid_request_error", first.message)

            async def relay_stream() -> Any:
                try:
                    if first is not None:
                        yield first
                    async for chunk in gen:
                        yield chunk
                finally:
                    await gen.aclose()
            return StreamingResponse(relay_stream(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        try:
            status, payload = await engine.plain(sess, body, incoming)
        except Exception as exc:
            # Same guard as the stream: Claude Code sends some requests without streaming (the auto-mode
            # classifier, its non-streaming fallback) and would retry a bare 500 many times over.
            _log_fault("/v1/messages", exc)
            return JSONResponse(_fault_message(exc, str(body.get("model") or "pulsar")))
        return JSONResponse(payload, status_code=status)

    async def count_tokens(request: Request) -> Response:
        sess = sessions.lookup(_bearer(request))
        if sess is None:
            return err(401, "authentication_error", "unknown session token")
        if sess.side_channel:
            return side_channel_refusal()
        try:
            body = await read_json(request)
        except BodyError as exc:
            return bad_body(exc)
        body = body if isinstance(body, dict) else {}
        problem = _bad_max_tokens(body)
        if problem:
            return err(400, "invalid_request_error", problem)
        if getattr(request.state, "body_size", 0) > PARSE_IN_THREAD:
            return JSONResponse({"input_tokens": await asyncio.to_thread(T.estimate_tokens, body)})
        return JSONResponse({"input_tokens": T.estimate_tokens(body)})

    async def models(request: Request) -> Response:
        sess = sessions.lookup(_bearer(request))
        if sess is None:
            return err(401, "authentication_error", "unknown session token")
        from pulsar.catalog import read_cache
        ids: List[str] = []

        def add(mid: Any) -> None:
            # Ids come from providers and custom endpoints: never pass terminal control sequences on to
            # Claude Code's model picker.
            if isinstance(mid, str) and mid and len(mid) <= 256 and mid not in ids and \
                    not any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F for c in mid):
                ids.append(mid)
        for t in sess.plan:
            add(t.model)
        for t in sess.plan:
            for m in read_cache(t.profile, ttl=10 * 365 * 86400) or []:
                add(getattr(m, "id", None))
        data = [{"id": i, "type": "model", "display_name": i, "created_at": "2026-01-01T00:00:00Z"} for i in ids[:500]]
        return JSONResponse({"data": data, "has_more": False, "first_id": ids[0] if ids else None,
                             "last_id": ids[-1] if ids else None})

    # -- pulsar: callers and scopes ----------------------------------------------------
    def caller(request: Request) -> Tuple[str, Optional[ST.Session], Optional[str]]:
        """(who, session, scope). scope is the only sid this caller may see, or None for a global caller
        (the admin token and the global view token). Session and per-session view tokens are always scoped.

        A token in the URL (?t=) counts only when it is a read-only view token: one in a URL ends up in
        browser history and logs, and checking it never reads a file."""
        tok = _bearer(request)
        if not tok:
            tok = request.query_params.get("t", "")
            tok = tok if tok.startswith(ST.VIEW_TOKEN_PREFIX) else ""
        if is_admin(tok):
            return "admin", None, None
        sess = sessions.lookup(tok)
        if sess is not None:
            return "session", sess, sess.sid
        scope = view_scope(tok, request.query_params.get("sid", ""))
        if scope is not None:
            return "viewer", None, (scope or None)
        return "", None, None

    def wanted(request: Request, scope: Optional[str]) -> Optional[str]:
        """The sid to filter on: a scoped caller's own sid; for a global caller, an optional ?sid=."""
        return scope if scope is not None else (request.query_params.get("sid") or None)

    def breakers_for(sid: str) -> Dict[str, Any]:
        sess = sessions.get(sid)
        if sess is None:
            return {}
        profiles = {t.profile for t in sess.plan}
        return {k: v for k, v in engine.breakers.state().items() if k.split("|", 1)[0] in profiles}

    def snapshot_for(scope: Optional[str], only: Optional[str]) -> Dict[str, Any]:
        if scope is not None:
            snap = stats.snapshot(scope)
            snap["breakers"] = breakers_for(scope)
            return snap
        snap = stats.snapshot(only)
        snap["breakers"] = engine.breakers.state()
        return snap

    # -- pulsar: stats + events -----------------------------------------------------
    async def get_stats(request: Request) -> Response:
        who, sess, scope = caller(request)
        if not who:
            return err(401, "authentication_error", "token required")
        if scope is not None:
            own = sess or sessions.get(scope)
            snap = snapshot_for(scope, scope)
            if own is not None:
                snap["plan"] = [vars(t) for t in own.plan]
            return JSONResponse(snap)
        snap = snapshot_for(None, wanted(request, None))
        snap["sessions_live"] = [{"sid": s.sid, "label": s.label, "cwd": s.cwd,
                                  "plan": [vars(t) for t in s.plan], "pid": s.pid}
                                 for s in ST.live_sessions(reap=False)]
        return JSONResponse(snap)

    async def events(request: Request) -> Response:
        who, _sess, scope = caller(request)
        if not who:
            return err(401, "authentication_error", "token required")
        only = wanted(request, scope)
        q = stats.subscribe()

        async def gen():
            try:
                yield ": pulsar events\n\n"
                last_tick = 0.0
                while not app.state.closing:
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=1.0)
                        # Scoped callers only ever see events tagged with their own sid (untagged events,
                        # e.g. another session's heal, are global information).
                        if only is None or ev.get("sid") == only:
                            yield "event: %s\ndata: %s\n\n" % (ev["type"], json.dumps(ev))
                    except asyncio.TimeoutError:
                        pass
                    if time.time() - last_tick >= 1.0:
                        last_tick = time.time()
                        yield "event: tick\ndata: %s\n\n" % json.dumps(snapshot_for(scope, only))
                        if await request.is_disconnected():
                            break
            finally:
                stats.unsubscribe(q)
        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    # -- pulsar: stage -----------------------------------------------------------------
    async def stage_post(request: Request) -> Response:
        who, sess, _scope = caller(request)
        if who not in ("session", "admin"):
            return err(401, "authentication_error", "session token required")
        try:
            payload = await read_json(request)
        except BodyError as exc:
            return bad_body(exc)
        if not isinstance(payload, dict):
            return err(400, "invalid_request_error", "body must be a JSON object")
        from pulsar.stage.schema import SceneError, normalize
        op = str(payload.get("op") or "show")
        sid = sess.sid if sess else str(payload.get("sid") or "operator")[:64]
        if op == "clear":
            stage.push(sid, {}, "clear")
            return JSONResponse({"ok": True})
        scene_in = payload.get("scene") or {}
        if not isinstance(scene_in, dict):
            return err(400, "invalid_request_error", "scene must be a JSON object")
        try:
            scene = normalize(scene_in)
        except SceneError as exc:
            return err(400, "invalid_request_error", str(exc))
        item = stage.push(sid, scene, op if op in ("show", "update") else "show")
        stats.emit("stage", sid=sid, kind=scene["kind"], title=scene.get("title", ""))
        own = sess.sid if sess else None
        return JSONResponse({"ok": True, "id": scene["id"], "viewers": viewers(own),
                             "pane": bool(own) and panes.active(own), "t": item["t"]})

    async def stage_stream(request: Request) -> Response:
        who, _sess, scope = caller(request)
        if not who:
            return err(401, "authentication_error", "token required")
        only = wanted(request, scope)
        q = stage.subscribe(only)

        async def gen():
            try:
                backlog = [s for s in stage.scenes if not only or s["sid"] == only][-1:]
                for item in backlog:
                    yield "event: scene\ndata: %s\n\n" % json.dumps(item)
                quiet = 0.0
                while not app.state.closing:
                    try:
                        item = await asyncio.wait_for(q.get(), timeout=1.0)
                    except asyncio.TimeoutError:
                        quiet += 1.0
                        if quiet >= 15.0:
                            quiet = 0.0
                            yield ": keepalive\n\n"
                            if await request.is_disconnected():
                                break
                        continue
                    if only and item["sid"] != only:
                        continue
                    yield "event: %s\ndata: %s\n\n" % ("clear" if item["op"] == "clear" else "scene", json.dumps(item))
            finally:
                stage.unsubscribe(q)
        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    async def stage_page(request: Request) -> Response:
        tok = request.query_params.get("t", "")
        if not is_viewer(tok, request.query_params.get("sid", "")):
            return HTMLResponse("<h1>401</h1><p>Open the link printed by <code>pulsarcode stage --web</code>.</p>",
                                status_code=401)
        try:
            from pulsar.stage.web import CSP_LIVE, page
            html, csp = page(), CSP_LIVE
        except Exception:
            html, csp = _STAGE_FALLBACK, _STAGE_FALLBACK_CSP
        # The page's own policy (its one inline script by hash), sent as a header too: a header also covers
        # what comes before the page's <meta> policy, and it alone can refuse framing.
        return HTMLResponse(html, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                           "X-Frame-Options": "DENY",
                                           "Content-Security-Policy": csp + "; frame-ancestors 'none'"})

    async def stage_viewer(request: Request) -> Response:
        """A read-only web-stage URL for the calling session (or all sessions for admin)."""
        who, sess, _scope = caller(request)
        if who not in ("session", "admin"):
            return err(401, "authentication_error", "token required")
        sid = sess.sid if sess else ""
        tok = ST.view_token(admin_token, sid)
        base = "http://127.0.0.1:%d" % port if port else ""
        return JSONResponse({"url": base + "/stage?t=" + tok + ("&sid=" + sid if sid else ""), "sid": sid,
                             "viewers": viewers(sid or None), "pane": bool(sid) and panes.active(sid)})

    async def stage_scenes(request: Request) -> Response:
        who, _sess, scope = caller(request)
        if not who:
            return err(401, "authentication_error", "token required")
        only = wanted(request, scope)
        return JSONResponse({"scenes": [s for s in stage.scenes if not only or s["sid"] == only]})

    async def stage_raster(request: Request) -> Response:
        """Frames for the Claude Code Stage pane (pulsar.stage.pane.raster_route), scoped exactly like
        /pulsar/stage/scenes: a session sees its own scenes, a view token its sid's, the admin token all."""
        who, sess, scope = caller(request)
        if not who:
            return err(401, "authentication_error", "token required")
        if sess is not None:
            note_pane(sess.sid, request.headers.get(PANE.VIEWER_HEADER, ""))
        only = wanted(request, scope)
        items = [s for s in stage.scenes if not only or s["sid"] == only]
        query = request.query_params
        if str(query.get("meta") or "").strip().lower() in _TRUE:
            return _raster_reply(*PANE.raster_route(items, query))     # the cheap poll: no cells drawn
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(raster_pool, lambda: _raster_reply(*PANE.raster_route(items, query)))

    # -- pulsar: council ------------------------------------------------------------------
    async def council(request: Request) -> Response:
        who, sess, _scope = caller(request)
        if who != "session" or sess is None:
            return err(401, "authentication_error", "session token required")
        try:
            payload = await read_json(request)
        except BodyError as exc:
            return bad_body(exc)
        if not isinstance(payload, dict):
            return err(400, "invalid_request_error", "body must be a JSON object")
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            return err(400, "invalid_request_error", "prompt is required")
        refused: List[Dict[str, Any]] = []
        targets = _council_targets(sess, payload.get("models"), refused, engine)
        if not targets and not refused:
            return err(400, "invalid_request_error", "no models available for council")
        system = str(payload.get("system") or "")
        try:
            max_tokens = max(64, min(int(payload.get("max_tokens") or 2048), 16384))
        except (TypeError, ValueError):
            return err(400, "invalid_request_error", "max_tokens must be an integer")
        stats.emit("council", sid=sess.sid, models=[t.model for t in targets])
        allow_http = http_opt_in(sess)
        answers = await asyncio.gather(*(engine.ask(t, prompt, system, max_tokens, allow_http=allow_http)
                                         for t in targets))
        return JSONResponse({"answers": list(answers) + refused})

    # -- pulsar: control -------------------------------------------------------------------
    async def ephemeral_key(request: Request) -> Response:
        if not is_admin(_bearer(request)):
            return err(401, "authentication_error", "admin token required")
        try:
            payload = await read_json(request)
        except BodyError as exc:
            return bad_body(exc)
        if not isinstance(payload, dict):
            return err(400, "invalid_request_error", "body must be a JSON object")
        prof, key = str(payload.get("profile") or ""), str(payload.get("key") or "")
        if len(prof) > 128 or len(key) > 16384:
            return err(400, "invalid_request_error", "profile or key too long")
        if prof:
            if key:
                keys.ephemeral[prof] = key
            else:
                keys.ephemeral.pop(prof, None)
        return JSONResponse({"ok": True})

    async def shutdown(request: Request) -> Response:
        if not is_admin(_bearer(request)):
            return err(401, "authentication_error", "admin token required")
        app.state.shutdown_requested = True
        return JSONResponse({"ok": True})

    async def retire_route(request: Request) -> Response:
        """A launch of other code started (or is starting) a Core of its own: serve the sessions we have,
        then exit; at once when none of them is live."""
        if not is_admin(_bearer(request)):
            return err(401, "authentication_error", "admin token required")
        holding = retire(time.time())
        if not holding and not stats.flights:
            app.state.shutdown_requested = True
        return JSONResponse({"ok": True, "retiring": True, "sessions": holding})

    async def fallback(request: Request) -> Response:
        path = request.path_params.get("path", "")
        return err(404, "not_found_error", "Pulsar Core does not serve /" + path[:200])

    every = ["GET", "POST", "PUT", "DELETE", "HEAD", "PATCH"]
    routes = [
        _route("/healthz", healthz, ["GET"]),
        _route("/api/hello", hello, ["GET", "HEAD"]),
        _route("/v1/messages", messages, ["POST"]),
        _route("/v1/messages/count_tokens", count_tokens, ["POST"]),
        _route("/v1/models", models, ["GET"]),
        _route("/pulsar/stats", get_stats, ["GET"]),
        _route("/pulsar/events", events, ["GET"]),
        _route("/pulsar/stage", stage_post, ["POST"]),
        _route("/pulsar/stage/stream", stage_stream, ["GET"]),
        _route("/stage", stage_page, ["GET"]),
        _route("/pulsar/stage/viewer", stage_viewer, ["GET"]),
        _route("/pulsar/stage/scenes", stage_scenes, ["GET"]),
        _route("/pulsar/stage/raster", stage_raster, ["GET"]),
        _route("/pulsar/council", council, ["POST"]),
        _route("/pulsar/ephemeral-key", ephemeral_key, ["POST"]),
        _route("/pulsar/shutdown", shutdown, ["POST"]),
        _route("/pulsar/retire", retire_route, ["POST"]),
        _route("/{path:path}", fallback, every),
    ]
    app = Starlette(routes=routes, lifespan=lifespan, exception_handlers={HTTPException: _http_error},
                    middleware=[Middleware(Guard, port=port,
                                           authed=lambda tok: is_admin(tok) or sessions.lookup(tok) is not None)])
    app.state.stats, app.state.keys, app.state.sessions, app.state.stage, app.state.engine = \
        stats, keys, sessions, stage, engine
    app.state.build, app.state.panes, app.state.retire, app.state.ours = build, panes, retire, ours
    app.state.last_activity = time.time()
    app.state.last_proof = 0.0          # when a launcher last verified us (it is about to register a session)
    app.state.shutdown_hooks = hooks
    app.state.closing = False
    app.state.shutdown_requested = False
    app.state.retiring = 0.0            # set: retired at this time (see retire), serving only its own sessions
    app.state.server = None             # the uvicorn.Server, when serve() runs us
    return app


def _cloud(t: ST.Target) -> bool:
    """Bedrock, Vertex and subscription tokens are spoken to by Claude Code itself; Core cannot reach them."""
    prov = P.get(t.provider)
    return prov is not None and prov.route == "cloud"


def _council_targets(sess: ST.Session, wanted: Any, errors: Optional[List[Dict[str, Any]]] = None,
                     engine: Optional[Engine] = None) -> List[ST.Target]:
    """Targets for a council call: explicit 'profile:model' / 'model' strings, else the session's other models.

    A bare model id goes to the profile that serves it: a plan target with that model, else the connected
    profile whose cached catalog lists it. It falls back to the primary's profile only when the primary's
    catalog is unknown; otherwise it is not asked (the prompt and a key never go to a provider that does not
    serve the model) and an error answer is added to `errors`.
    The default council is plan[1:], the session's other models. A plan with one target would only ask the
    model that is already answering: it is then joined by up to two of its provider's recommended models,
    each from another vendor than the ones already asked (the same profile and key, so the prompt goes to
    no new provider).
    """
    from pulsar.catalog import picks, read_cache
    out: List[ST.Target] = []
    if isinstance(wanted, list) and wanted:
        state = K.load()
        eng = engine or Engine(Stats(), lambda _p: "", K.home())
        for w in wanted[:6]:
            w = str(w).strip()
            if not w:
                continue
            if ":" in w and w.split(":", 1)[0] in state.profiles:
                prof_id, model = w.split(":", 1)
                prof = state.profiles.get(prof_id)
                if prof is not None:
                    out.append(ST.Target(prof.id, prof.provider, model, prof.base_url, prof.native, prof.context))
                continue
            owner = eng.find_owner(w, sess)
            if owner is not None and not _cloud(owner):
                out.append(ST.Target(owner.profile, owner.provider, w, owner.base_url, owner.native, owner.context))
                continue
            primary = next((t for t in sess.plan if not _cloud(t)), None)
            prof = state.profiles.get(primary.profile) if primary is not None else state.profiles.get(state.active)
            if prof is not None and not read_cache(prof.id, ttl=10 * 365 * 86400):
                out.append(ST.Target(prof.id, prof.provider, w, prof.base_url, prof.native, prof.context))
                continue
            if errors is not None:
                errors.append({"model": P.clean_text(w, 120), "provider": "", "ok": False, "seconds": 0.0,
                               "error": "no connected provider lists this model; name it as <profile>:<model>"})
        return [t for t in out if not _cloud(t)]
    seen = set()
    pool = sess.plan[1:] if len(sess.plan) > 1 else sess.plan
    for t in pool:
        if t.key() not in seen and not _cloud(t):
            seen.add(t.key())
            out.append(t)
    if len(sess.plan) == 1 and out:
        own, prov = out[0], P.get(out[0].provider)
        if prov is not None:
            listed = read_cache(own.profile, ttl=10 * 365 * 86400) or []
            vendors = {own.model.split("/")[0]}
            for m in picks(listed, prov, limit=6):
                if m.id.split("/")[0] not in vendors and len(out) < 3:
                    vendors.add(m.id.split("/")[0])
                    out.append(ST.Target(own.profile, own.provider, m.id, own.base_url, own.native, m.context))
    return out[:4]


# ---------------------------------------------------------------------------
# Process lifecycle
# ---------------------------------------------------------------------------

class _HeaderDeadline:
    """Mixin for uvicorn's HTTP protocols that keeps idle sockets from crowding out real work.

    * Header deadline: a connection must complete a request head within `header_deadline` seconds of
      connecting (or of its previous response), or it is closed. A request whose head is complete is
      never timed out, so SSE streams (/v1/messages, /pulsar/events, the stage stream) stay open for as
      long as they need.
    * Connection cap: at most `max_conns` connections stay open. When a new one would exceed it, the
      connection that has been idle longest (no request in progress: a bare connect, a partial head, a
      keep-alive between requests) is closed; a request in progress is never cut. Only when every open
      connection is mid-request is the newcomer itself turned away.

    Unlike uvicorn's limit_concurrency (which counts idle sockets too and answers 503 to every request,
    /healthz included, once enough of them are open), an unauthenticated process holding thousands of
    silent connections therefore costs Core nothing but the churn of closing them.
    """

    header_deadline = 15.0
    max_conns = MAX_CONNS
    _pulsar_open: "OrderedDict[Any, None]"   # per generated class: open connections, longest idle first
    _pulsar_timer: Any = None

    def connection_made(self, transport: Any) -> None:
        super().connection_made(transport)  # type: ignore[misc]
        self._pulsar_arm()
        self._pulsar_open[self] = None
        self._pulsar_shed()

    def on_response_complete(self) -> None:
        super().on_response_complete()  # type: ignore[misc]
        tr = getattr(self, "transport", None)
        if tr is not None and not tr.is_closing():
            self._pulsar_arm()
            if self in self._pulsar_open:
                self._pulsar_open.move_to_end(self)   # idle from now on: the newest idle connection

    def connection_lost(self, exc: Optional[BaseException]) -> None:
        self._pulsar_disarm()
        self._pulsar_open.pop(self, None)
        super().connection_lost(exc)  # type: ignore[misc]

    def _pulsar_busy(self) -> bool:
        """A request is being served on this connection (its head arrived, its response is not done)."""
        cycle = getattr(self, "cycle", None)
        return cycle is not None and not getattr(cycle, "response_complete", True)

    def _pulsar_close(self) -> None:
        self._pulsar_disarm()
        self._pulsar_open.pop(self, None)
        tr = getattr(self, "transport", None)
        if tr is not None and not tr.is_closing():
            tr.close()

    def _pulsar_shed(self) -> None:
        conns = self._pulsar_open
        if len(conns) <= self.max_conns:
            return
        # Oldest first; connections mid-request are skipped (and requeued, so a long stream is not
        # re-examined on every connect). Bounded: each open connection is looked at most once.
        for _ in range(len(conns)):
            if len(conns) <= self.max_conns:
                return
            victim = next(iter(conns))
            if victim is self:
                conns.move_to_end(victim)
                continue
            if victim._pulsar_busy():
                conns.move_to_end(victim)
                continue
            victim._pulsar_close()
        if len(conns) > self.max_conns:
            self._pulsar_close()   # every other connection is serving a request: turn the newcomer away

    def _pulsar_arm(self) -> None:
        self._pulsar_disarm()
        if self.header_deadline <= 0:
            return
        loop = getattr(self, "loop", None) or asyncio.get_event_loop()
        self._pulsar_timer = loop.call_later(self.header_deadline, self._pulsar_expire)

    def _pulsar_disarm(self) -> None:
        timer, self._pulsar_timer = self._pulsar_timer, None
        if timer is not None:
            timer.cancel()

    def _pulsar_expire(self) -> None:
        self._pulsar_timer = None
        if self._pulsar_busy():
            return  # a request is being served; on_response_complete re-arms
        tr = getattr(self, "transport", None)
        if tr is not None and not tr.is_closing():
            tr.close()


def _http_protocol(header_timeout: float, impl: str = "auto", max_conns: int = 0) -> Any:
    """uvicorn's own HTTP protocol (httptools when installed, else h11) with the header deadline and the
    connection cap. Returns `impl` unchanged when neither is wanted or uvicorn's classes are missing."""
    if header_timeout <= 0 and max_conns <= 0:
        return impl
    try:
        if impl == "httptools" or (impl == "auto" and importlib.util.find_spec("httptools") is not None):
            from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol as base
        else:
            from uvicorn.protocols.http.h11_impl import H11Protocol as base  # type: ignore[assignment]
        return type("PulsarHTTP", (_HeaderDeadline, base),
                    {"header_deadline": max(0.0, header_timeout), "max_conns": max_conns if max_conns > 0 else 1 << 30,
                     "_pulsar_open": OrderedDict()})
    except Exception:
        return impl


def _conn_budget() -> int:
    """How many connections Core keeps open: PULSAR_MAX_CONNS, else MAX_CONNS within the descriptor limit
    (leaving room for upstream connections, session files and logs)."""
    want = int(_knob("PULSAR_MAX_CONNS", 0, lo=16, hi=1 << 20))
    if want:
        return want
    try:
        import resource
        soft = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
        if soft != resource.RLIM_INFINITY:
            return max(64, min(MAX_CONNS, soft - 256))
    except (ImportError, ValueError, OSError):
        pass
    return MAX_CONNS


def _raise_fd_limit(want: int = FD_LIMIT) -> None:
    """Lift the soft RLIMIT_NOFILE (256 by default on macOS) so a burst of sockets cannot starve Core."""
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = want if hard == resource.RLIM_INFINITY else min(hard, want)
        if soft != resource.RLIM_INFINITY and soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (ImportError, ValueError, OSError):
        pass


def _bind(port: int) -> socket.socket:
    """Bind 127.0.0.1:port ourselves, before anything else, so the port is ours as early as possible."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            # Without this, another account's socket can bind the same port with SO_REUSEADDR and steal it.
            excl = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if excl is not None:
                sock.setsockopt(socket.SOL_SOCKET, excl, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # restart over TIME_WAIT; never shares a listener
        sock.bind(("127.0.0.1", port))
    except OSError:
        sock.close()
        raise
    sock.set_inheritable(False)
    return sock


def serve(port: int, admin_token: str) -> None:
    import uvicorn
    pid = os.getpid()

    def release() -> None:
        """Remove run/core.json while it still names this process. Idempotent; safe from any exit path."""
        try:
            ST.clear_core(pid)
        except Exception:
            pass

    def on_term(signum: int, frame: Any) -> None:
        # Installed before uvicorn takes over the signals, and restored by uvicorn when it re-raises a
        # captured SIGTERM after its graceful shutdown: either way the descriptor goes first.
        release()
        raise SystemExit(0)

    atexit.register(release)
    for name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, on_term)
            except (OSError, ValueError):
                pass
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)  # a closing terminal is not a reason to drop sessions
    _raise_fd_limit()
    try:
        sock = _bind(port)
    except OSError as exc:
        print("pulsarcode core: 127.0.0.1:%d is taken (%s)" % (port, exc.strerror or exc), file=sys.stderr)
        raise SystemExit(ST.PORT_IN_USE_EXIT)

    app = make_app(admin_token, port)
    app.state.shutdown_hooks.append(release)
    # No limit_concurrency: uvicorn counts idle sockets against it and then answers 503 to everything,
    # /healthz included. The header deadline and the connection cap in _HeaderDeadline bound sockets.
    head_deadline = _knob("PULSAR_CORE_HEADER_TIMEOUT", HEADER_TIMEOUT, lo=1.0, hi=3600.0)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False,
                            lifespan="on", timeout_keep_alive=KEEP_ALIVE,
                            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN, server_header=False,
                            http=_http_protocol(head_deadline, max_conns=_conn_budget()))
    server = uvicorn.Server(config)
    app.state.server = server
    grace = _knob("PULSAR_CORE_GRACE", 120.0)
    idle_cap = _knob("PULSAR_CORE_IDLE", 12 * 3600.0)
    scan_every = max(0.2, _knob("PULSAR_CORE_SCAN", 5.0))
    quiet_for = _knob("PULSAR_CORE_QUIET", 10.0)
    started = time.time()

    async def lifecycle() -> None:
        """Exit when no session has been live for a while; release core.json the moment we start leaving.

        A Core that run/core.json no longer names (a launch of newer code started its own) retires: it
        exits as soon as none of the sessions it serves is live, whatever the grace period says."""
        quiet_since = 0.0
        next_scan = 0.0
        while True:
            if app.state.shutdown_requested:
                server.should_exit = True
            if server.should_exit:
                # Before the graceful drain: new launches must not attach to a Core that is going away.
                app.state.closing = True
                release()
                return
            now = time.time()
            if now >= next_scan:
                next_scan = now + scan_every
                live = ST.live_sessions(reap=True)
                flying = bool(app.state.stats.flights)
                if not app.state.retiring:
                    heir = _successor(pid, started)
                    if heir is not None:
                        app.state.retire(heir.started)
                if app.state.retiring and not flying and not app.state.ours(live):
                    server.should_exit = True
                    continue
                # A launcher that just verified us is about to register its session: not quiet yet.
                attaching = now - app.state.last_proof < max(quiet_for, 2.0)
                if live:
                    quiet_since = 0.0
                elif now - started > grace:
                    quiet_since = quiet_since or now
                    if now - quiet_since >= quiet_for and not flying and not attaching:
                        server.should_exit = True
                        continue
                # The idle cap only ever applies with no live session: a session left open overnight
                # must still find its gateway in the morning.
                if not live and idle_cap and now - app.state.last_activity > idle_cap and not flying:
                    server.should_exit = True
                    continue
            await asyncio.sleep(0.2)

    async def run() -> None:
        task = asyncio.ensure_future(lifecycle())
        try:
            await server.serve(sockets=[sock])
        finally:
            task.cancel()
            release()
            try:
                await app.state.engine.aclose()
            except Exception:
                pass

    asyncio.run(run())


def _token_from_stdin() -> str:
    """The admin token the spawner wrote to stdin (one line); "" on a terminal or when nothing is there."""
    try:
        stream = sys.stdin
        if stream is None or stream.isatty():
            return ""
        line = stream.readline(4096)
    except (OSError, ValueError, AttributeError):
        return ""
    return line.strip() if isinstance(line, str) else ""


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="pulsarcode core")
    ap.add_argument("--port", type=int, default=ST.DEFAULT_CORE_PORT)
    args = ap.parse_args(argv)
    # The spawner (pulsar.core.spawn) generates the admin token, writes it to our stdin (never argv, never
    # the environment, whose initial copy stays readable in /proc/<pid>/environ), waits for /healthz?nonce=
    # to answer with HMAC(admin_token, nonce), and only then publishes run/core.json; a failed bind or a
    # squatter on the port can never become a trusted descriptor. PULSAR_ADMIN_TOKEN is still accepted
    # for a Core started by hand. Stdin is read only when the spawner says it wrote the token there
    # (PULSAR_TOKEN_STDIN=1): a supervisor's stdin pipe that stays open would otherwise block Core forever.
    from_stdin = os.environ.pop("PULSAR_TOKEN_STDIN", "") == "1"
    token = os.environ.pop("PULSAR_ADMIN_TOKEN", "") or (_token_from_stdin() if from_stdin else "")
    if not token.startswith(ST.ADMIN_TOKEN_PREFIX):
        print("pulsarcode core: start it with `pulsarcode` (PULSAR_ADMIN_TOKEN missing)", file=sys.stderr)
        return 2
    K.ensure_home()
    ST.sessions_dir().mkdir(parents=True, exist_ok=True)
    serve(args.port, token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
