# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Files that tie Pulsar processes together. Stdlib only, import-cheap.

  run/core.json             {"port", "pid", "version", "admin_token", "started"}         0600
  run/sessions/<sid>.json   Session (see below)                                          0600

build_id() fingerprints the code on disk, so a launch never reuses a Core that runs older code.

A session's bearer token is never stored, only its SHA-256. The launcher
writes the session file, hands the raw token to Claude Code as
ANTHROPIC_AUTH_TOKEN and exports PULSAR_SESSION=<sid>, PULSAR_CORE=<url>
so the MCP server Claude Code spawns (which inherits that environment)
can talk to Core as the same session.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

SESSION_TOKEN_PREFIX = "pls_"
ADMIN_TOKEN_PREFIX = "pla_"
VIEW_TOKEN_PREFIX = "plv_"
DEFAULT_CORE_PORT = 4747
# How a session reaches its model (Session.flags["route"], set by launch.start_session): through Core, or
# Claude Code straight to the provider (direct) or cloud (Bedrock, Vertex), with Core as a side channel.
ROUTES = ("core", "direct", "cloud")
# `pulsarcode core` exits with this code when its loopback port is already taken (by a squatter or a
# peer), so the spawner knows to pick another port instead of reporting a crash.
PORT_IN_USE_EXIT = 3
_NONCE = re.compile(r"[0-9a-f]{32,128}")
_PACKAGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # .../pulsar
_build: Optional[str] = None
_digests: Dict[str, Any] = {}         # path -> ((size, mtime_ns, ctime_ns), sha256 hex) for build_id


def _home() -> Path:
    # The same rule as config.home(): an empty PULSAR_HOME means the default, never the current folder.
    return Path(os.path.expanduser(os.environ.get("PULSAR_HOME") or "~/.pulsarcode"))


def run_dir() -> Path:
    return _home() / "run"


def sessions_dir() -> Path:
    return run_dir() / "sessions"


def core_file() -> Path:
    return run_dir() / "core.json"


def _write_private(path: Path, text: str) -> None:
    from pulsar.config import write_private
    write_private(path, text)


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token(prefix: str = SESSION_TOKEN_PREFIX) -> str:
    return prefix + secrets.token_urlsafe(32)


def view_token(admin_token: str, sid: str = "") -> str:
    """Read-only token for stage/observatory viewers, derived (not stored)."""
    digest = hashlib.sha256(("view:" + admin_token + ":" + sid).encode()).hexdigest()[:40]
    return VIEW_TOKEN_PREFIX + digest


def new_nonce() -> str:
    """A fresh 32-byte challenge for Core's identity proof, as lowercase hex."""
    return secrets.token_hex(32)


def valid_nonce(nonce: str) -> bool:
    return bool(nonce) and _NONCE.fullmatch(nonce) is not None


def core_proof(admin_token: str, nonce: str) -> str:
    """hex(HMAC-SHA256(admin_token, nonce)): what GET /healthz?nonce=<hex> answers.

    Only the process that holds the admin token (the Core we spawned) can produce it, so a process that
    squats Core's port, or answers on the port of a stale run/core.json, is never trusted with keys,
    the admin token or a Claude Code session.
    """
    return hmac.new(admin_token.encode("utf-8"), nonce.encode("ascii"), hashlib.sha256).hexdigest()


def proof_ok(admin_token: str, nonce: str, proof: object) -> bool:
    if not admin_token or not isinstance(proof, str) or len(proof) != 64:
        return False
    return hmac.compare_digest(proof.encode("ascii", "replace"), core_proof(admin_token, nonce).encode("ascii"))


def build_id(refresh: bool = False) -> str:
    """Fingerprint of the Pulsar code on disk: SHA-256 over the path and contents of every pulsar/**/*.py.

    Computed once per process and cached; refresh=True looks again, rehashing only the files whose
    (size, mtime_ns, ctime_ns) changed since this process last read them (about 50 stat calls otherwise).
    Core takes it when it starts, so it names the code that Core runs, and reports it in its proven
    /healthz reply; a launch compares that with the code on disk now and starts a new Core when they
    differ, so an update (same version or not) never leaves old code serving new sessions. It hashes
    contents, not mtimes, so
    two installs of the same code that share a Pulsar home (an install.sh app folder and a pipx install or a
    git checkout of the same release) share one Core instead of retiring each other's on every launch.
    """
    global _build
    if _build is not None and not refresh:
        return _build
    entries: List[str] = []
    for top, dirs, files in os.walk(_PACKAGE):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__" and not d.startswith("."))
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(top, name)
            digest = _file_digest(path)
            if digest is None:
                continue
            rel = os.path.relpath(path, _PACKAGE).replace(os.sep, "/")
            entries.append("%s\0%s" % (rel, digest))
    _build = hashlib.sha256("\n".join(sorted(entries)).encode("utf-8", "surrogateescape")).hexdigest()[:20]
    return _build


def _file_digest(path: str) -> Optional[str]:
    """SHA-256 of one file's bytes, cached per (path, size, mtime_ns, ctime_ns); None when it cannot be read."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (st.st_size, st.st_mtime_ns, st.st_ctime_ns)   # ctime: a rewrite that restores the mtime still shows
    hit = _digests.get(path)
    if hit is not None and hit[0] == key:
        return hit[1]
    try:
        with open(path, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None
    _digests[path] = (key, digest)
    return digest


# Proxy variables httpx reads when trust_env is on (urllib.request.getproxies spellings).
PROXY_VARS = ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")


def proxy_scheme(value: str) -> str:
    """The scheme of a proxy setting as httpx reads it (a bare host:port means http)."""
    value = (value or "").strip()
    if "://" not in value:
        return "http"
    scheme = value.split("://", 1)[0].lower()
    return scheme if re.fullmatch(r"[a-z][a-z0-9+.-]{0,15}", scheme) else "unknown"   # never echo other text


def unusable_proxies(env: Optional[Dict[str, str]] = None) -> List[str]:
    """Names of the proxy variables in env (default os.environ) that httpx cannot use.

    httpx raises ValueError("Unknown scheme for proxy URL ...") from AsyncClient(trust_env=True) for any
    scheme but http, https, socks5 and socks5h, so ALL_PROXY=socks4://... (offered by some Clash and V2Ray
    setups) would fail every upstream request. Callers drop these variables and say so by name; a value is
    never returned or printed, since a proxy URL can carry a password.
    """
    env = os.environ if env is None else env
    bad: List[str] = []
    for var in PROXY_VARS:
        val = (env.get(var) or "").strip()
        if not val:
            continue
        url = val if "://" in val else "http://" + val
        try:
            import httpx
            httpx.Proxy(url)
        except ImportError:
            return []
        except Exception:
            bad.append(var)
    return bad


# ---------------------------------------------------------------------------
# Core descriptor
# ---------------------------------------------------------------------------

@dataclass
class CoreInfo:
    port: int
    pid: int
    version: str
    admin_token: str
    started: float = 0.0

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self.port


def read_core() -> Optional[CoreInfo]:
    data = _read_json(core_file())
    if not data:
        return None
    try:
        return CoreInfo(int(data["port"]), int(data["pid"]), str(data["version"]), str(data["admin_token"]),
                        float(data.get("started") or 0))
    except (KeyError, TypeError, ValueError):
        return None


def write_core(info: CoreInfo) -> None:
    _write_private(core_file(), json.dumps(asdict(info)))


def publish_core(info: CoreInfo) -> bool:
    """Write run/core.json only where none is there, in one step: a complete temp file is linked to the name,
    which fails when it exists. Launches that could not take the spawn lock race to publish their Cores; the
    first wins and the others see it. False (nothing written) when a descriptor was already there."""
    import tempfile
    from pulsar.config import ensure_home
    ensure_home()                           # as write_private does before any write inside the home
    path = core_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".")   # 0600 from the first byte
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(info)))
        try:
            os.link(tmp, str(path))
        except FileExistsError:
            return False
        except OSError:                     # no hard links on this filesystem: the ordinary atomic write
            write_core(info)
        return True
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def clear_core(pid: int) -> bool:
    """Remove run/core.json, but only while it still names `pid` (never a successor's descriptor)."""
    info = read_core()
    if info is not None and info.pid == pid:
        try:
            core_file().unlink()
            return True
        except OSError:
            pass
    return False


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

@dataclass
class Target:
    profile: str                 # config profile id (keys.json key)
    provider: str                # provider id ("custom" for pasted endpoints)
    model: str                   # upstream model id ("" = provider default / Claude default)
    base_url: str = ""           # custom endpoints / overrides only
    native: bool = False         # custom endpoint speaks Anthropic Messages
    context: int = 0             # known context window, 0 = unknown

    def key(self) -> str:
        return self.profile + "|" + self.model


@dataclass
class Session:
    sid: str
    token_sha256: str
    plan: List[Target]
    pid: int = 0                 # lease holder (becomes Claude Code's pid after exec)
    created: float = 0.0
    cwd: str = ""
    label: str = ""
    flags: Dict[str, Any] = field(default_factory=dict)   # stage, pilot, council, relay, loopguard

    @property
    def primary(self) -> Optional[Target]:
        return self.plan[0] if self.plan else None

    @property
    def route(self) -> str:
        """The route this session was launched with: "core", "direct" or "cloud" (flags["route"]), else ""."""
        route = str(self.flags.get("route") or "")
        return route if route in ROUTES else ""

    @property
    def side_channel(self) -> bool:
        """True for a cloud or direct session: Claude Code talks to its provider itself, and the session
        token grants only that session's Stage, stats and council, never Core's gateway (/v1/messages).

        Decided by the route the session was launched with (flags["route"]: "direct"/"cloud" -> True,
        "core" -> False), never by the live plan[0]: a switch rewrites the plan, not the way Claude Code
        reaches its model. Only a session file without a route (older launchers) falls back to plan[0]:
        a cloud provider (Bedrock, Vertex, a Claude subscription token) is never served by Core.
        """
        route = self.route
        if route:
            return route in ("cloud", "direct")
        return _plan_route(self.plan) == "cloud"

    def to_json(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    @staticmethod
    def from_json(data: Dict[str, Any]) -> Optional["Session"]:
        try:
            plan = [Target(**{k: t[k] for k in t if k in Target.__dataclass_fields__})
                    for t in data.get("plan") or [] if isinstance(t, dict)]
            return Session(sid=str(data["sid"]), token_sha256=str(data["token_sha256"]), plan=plan,
                           pid=int(data.get("pid") or 0), created=float(data.get("created") or 0),
                           cwd=str(data.get("cwd") or ""), label=str(data.get("label") or ""),
                           flags=dict(data.get("flags") or {}))
        except (KeyError, TypeError, ValueError):
            return None


def target_label(t: Optional[Target]) -> str:
    """What to call a target's provider in messages: the registry name ("DeepSeek", "Amazon Bedrock"), or the
    endpoint's host for a custom one."""
    from pulsar import providers as P      # lazy: this module stays import-cheap
    if t is None:
        return "its provider"
    if t.provider == "custom":
        host = re.sub(r"^[a-z][a-z0-9+.-]*://", "", t.base_url or "", flags=re.I).split("/")[0].rsplit("@", 1)[-1]
        return P.clean_text(host, 80) or "a custom endpoint"
    prov = P.get(t.provider)
    return prov.name if prov is not None else (P.clean_text(t.provider, 40) or "its provider")


def side_channel_note(sess: Session) -> str:
    """Why Pulsar cannot change the model of `sess`, or "" for a core session.

    On the cloud and direct routes Claude Code talks to its provider itself: a new plan would change nothing
    it sends (Core refuses that session's token on /v1/messages), so `pulsarcode switch` and pulsar_switch
    refuse instead of reporting a switch that never happens."""
    if not sess.side_channel:
        return ""
    name = target_label(sess.primary)
    sid = "".join(c for c in sess.sid if c.isalnum() or c in "-_")[:40]
    if sess.route == "direct":
        return "session %s talks to %s directly, so Pulsar cannot change its model" % (sid, name)
    return "%s is used by Claude Code itself in session %s, so Pulsar cannot change its model" % (name, sid)


def session_path(sid: str) -> Path:
    safe = "".join(c for c in sid if c.isalnum() or c in "-_")[:64]
    return sessions_dir() / (safe + ".json")


def _plan_route(plan: List[Target]) -> str:
    """"cloud" when plan[0] is a provider Claude Code speaks to itself (Bedrock, Vertex, a Claude login
    token), else "core". Only for sessions whose launcher did not record a route."""
    if not plan:
        return "core"
    from pulsar import providers as P      # lazy: this module stays import-cheap
    prov = P.get(plan[0].provider)
    return "cloud" if prov is not None and prov.route == "cloud" else "core"


def new_session(plan: List[Target], flags: Optional[Dict[str, Any]] = None, label: str = "",
                cwd: str = "") -> "tuple[Session, str]":
    """Create and persist a session. Returns (session, raw_token).

    flags["route"] ("core", "direct" or "cloud") is the route the session is launched with; when the caller
    did not set it, it is recorded from plan[0]'s provider ("cloud" or "core"), so it never changes after
    a switch rewrites the plan (Session.side_channel)."""
    token = new_token()
    sid = "s" + secrets.token_hex(6)
    flags = dict(flags or {})
    if str(flags.get("route") or "") not in ROUTES:
        flags["route"] = _plan_route(plan)
    s = Session(sid=sid, token_sha256=token_hash(token), plan=plan, pid=os.getpid(), created=time.time(),
                cwd=cwd or os.getcwd(), label=label, flags=flags)
    save_session(s)
    return s, token


def save_session(s: Session) -> None:
    _write_private(session_path(s.sid), json.dumps(s.to_json(), indent=1))


def load_session(sid: str) -> Optional[Session]:
    data = _read_json(session_path(sid))
    return Session.from_json(data) if data else None


def iter_sessions() -> Iterator[Session]:
    try:
        files = sorted(sessions_dir().glob("*.json"))
    except OSError:
        return
    for f in files:
        data = _read_json(f)
        s = Session.from_json(data) if data else None
        if s is not None:
            yield s


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # type: ignore[attr-defined]
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))  # type: ignore[attr-defined]
            ctypes.windll.kernel32.CloseHandle(h)  # type: ignore[attr-defined]
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def live_sessions(reap: bool = True) -> List[Session]:
    """Sessions whose lease holder is alive. Dead ones are deleted when reap=True."""
    out = []
    for s in iter_sessions():
        if pid_alive(s.pid):
            out.append(s)
        elif reap and time.time() - s.created > 30:
            try:
                session_path(s.sid).unlink()
            except OSError:
                pass
    return out


def update_plan(sid: str, plan: List[Target]) -> bool:
    s = load_session(sid)
    if s is None:
        return False
    s.plan = plan
    save_session(s)
    return True


def _bare_model(model: str) -> str:
    """A requested model id without Claude Code's window suffix ([1m], [200k], [long])."""
    req = (model or "").strip()
    for suffix in ("[1m]", "[200k]", "[long]"):
        if req.lower().endswith(suffix):
            req = req[: -len(suffix)]
    return req


class ModelPicks:
    """Which requested model ids are a real /model pick, per session (kept in Core's memory).

    Claude Code keeps sending the model it was launched with (ANTHROPIC_MODEL) on every request, also
    after `pulsarcode switch` or `pulsar_switch` rewrote the session plan. That id is the session's
    baseline: plan[0]'s model when Core first sees the session (the launch model: the status line and
    the MCP server call in as the session from the start), and, after each plan change (update_plan),
    the last id Claude Code requested before the change. A request for the baseline means "no explicit
    pick", so Core serves plan[0]; only a different id (a /model pick in Claude Code) overrides the plan,
    so a switch made after a /model pick still wins. Ids are compared without the [1m] suffix.
    """

    KEEP = 4096

    def __init__(self) -> None:
        self._by_sid: Dict[str, List[Any]] = {}     # sid -> [baseline, last requested, plan signature]

    @staticmethod
    def _signature(plan: List[Target]) -> str:
        return "\n".join("%s|%s|%s" % (t.profile, t.provider, t.model) for t in plan)

    def observe(self, sess: Session) -> List[Any]:
        """Start tracking `sess` (idempotent): its baseline is plan[0]'s model as Core first sees it."""
        entry = self._by_sid.get(sess.sid)
        if entry is None:
            if len(self._by_sid) >= self.KEEP:
                self._by_sid.pop(next(iter(self._by_sid)))
            first = _bare_model(sess.primary.model) if sess.primary else ""
            entry = self._by_sid[sess.sid] = [first, first, self._signature(sess.plan)]
        return entry

    def explicit(self, sess: Session, requested: str) -> str:
        """`requested` when it is a real pick that should override plan[0], else ""."""
        req = _bare_model(requested)
        if not req:
            return ""
        sig = self._signature(sess.plan)
        entry = self.observe(sess)
        if entry[2] != sig:                     # the plan changed (a switch): what Claude Code sent last is no pick
            entry[0], entry[2] = entry[1], sig
        entry[1] = req
        return "" if req == entry[0] else requested

    def baseline(self, sid: str) -> str:
        entry = self._by_sid.get(sid)
        return entry[0] if entry else ""
