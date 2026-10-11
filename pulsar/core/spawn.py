# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Start or attach to Pulsar Core. Import-cheap (no Starlette, no uvicorn).

Trust: a Core is only trusted (attached to, handed ephemeral keys, written to
run/core.json, given to Claude Code) after it answers GET /healthz?nonce=<32
random bytes> with HMAC-SHA256(admin_token, nonce). Its pid and version are
public, so they prove nothing; the admin token never leaves the spawner and
the Core. A process that wins the bind race for the port we picked is
detected that way: we kill our child and retry on another port (bounded).

Concurrency: launches serialize on an OS lock (flock / msvcrt.locking) taken
on run/core.lock, a file that is never unlinked. The lock dies with its
holder, so there is no stale-lock takeover to race on. Where the filesystem
cannot lock at all, launches proceed at once, and a launch whose new Core
finds a peer's proven Core already published adopts it and stops its own.

Load: a Core that is slow or shedding connections ("busy") is waited for
with backoff, never replaced by a second Core while it may come back.

Updates: Core's proven /healthz reply names the build it runs (state.build_id:
the code on disk when it started). A Core whose build is not the code on disk
now is stale: the launch asks it to retire (it serves the sessions it has and
exits once none is live; at once when it has none) and starts a Core of the
current code, on another port while the old one still holds its own.

Environment: Core runs in the Pulsar home, never in the app folder (Windows
cannot replace a folder that is some process's working directory, which
would block updates while Core runs), and with loopback in NO_PROXY.
"""

from __future__ import annotations

import errno
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional, Set, Tuple

import pulsar
from pulsar import __version__
from pulsar.core import state as ST
from pulsar.core.client import CoreClient, CoreUnavailable, opener, probe

_OPENER = opener()           # no proxies, no redirects
SPAWN_ATTEMPTS = 4           # squatted or taken ports we step over before giving up
BUSY_WAIT = 6.0              # how long a launch waits for a live but busy Core before starting another
# errnos that mean "another process holds the lock"; anything else means the filesystem cannot lock
_CONTENDED = {errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES,
              getattr(errno, "EDEADLK", errno.EAGAIN), getattr(errno, "EDEADLOCK", errno.EAGAIN)}
_CHILD_ENV_DROP = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "PULSAR_SESSION_TOKEN",
                   "PULSAR_ADMIN_TOKEN", "ANTHROPIC_BASE_URL", "PULSAR_CORE", "PULSAR_SESSION")
# Paths Core reads from its environment; one given relative to the launcher's folder is made absolute,
# since Core runs in the Pulsar home.
_CHILD_ENV_PATHS = ("PULSAR_HOME", "PULSAR_CA_BUNDLE", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
                    "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS")
LOOPBACK_NO_PROXY = ("127.0.0.1", "localhost", "::1")
RETIRE_WAIT = 3.0            # how long a launch waits for a retired Core with no sessions to free its port


def _url(port: int) -> str:
    return "http://127.0.0.1:%d" % port


def health(port: int, timeout: float = 0.5, admin_token: str = "") -> Optional[Dict]:
    """Core's public health reply on `port`, or None.

    With admin_token, the reply only counts when it carries a valid HMAC proof for a fresh nonce (the
    returned dict then has "verified": True). Without it, any well-formed reply counts: use that for
    display only, never to decide where a token or key goes.
    """
    state, data, _ = probe(_url(port), admin_token, timeout=timeout)
    if state != "ok" or data is None:
        return None
    out = dict(data)
    out.pop("proof", None)
    if admin_token:
        out["verified"] = True
    return out


def verify(port: int, admin_token: str, timeout: float = 0.5) -> bool:
    """True when the process on `port` proves it holds `admin_token`."""
    return bool(admin_token) and probe(_url(port), admin_token, timeout=timeout)[0] == "ok"


def port_free(port: int) -> bool:
    """True when Core could bind 127.0.0.1:port now (same socket options as Core's own bind, so lingering
    TIME_WAIT connections of a previous Core do not count as taken, but any listener does)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            excl = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if excl is not None:
                s.setsockopt(socket.SOL_SOCKET, excl, 1)
        else:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Spawn lock
# ---------------------------------------------------------------------------

def _base_port() -> int:
    """PULSAR_PORT, or Core's default port when it is unset or not a usable port number."""
    raw = os.environ.get("PULSAR_PORT", "").strip()
    try:
        port = int(raw) if raw else ST.DEFAULT_CORE_PORT
    except ValueError:
        return ST.DEFAULT_CORE_PORT
    return port if 1 <= port <= 65535 - 200 else ST.DEFAULT_CORE_PORT


def _try_lock(fd: int) -> Optional[bool]:
    """True: we hold the lock. False: another process holds it. None: this filesystem cannot lock
    (ENOLCK, EOPNOTSUPP on some network filesystems), so waiting for it would only stall the launch."""
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        return False if exc.errno in _CONTENDED else None


def _unlock(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def spawn_lock(timeout: float = 45.0) -> Iterator[bool]:
    """Cross-process lock so concurrent launches never start two Cores.

    An OS lock on run/core.lock (never unlinked): the kernel drops it when the holder exits or is
    killed, so a crashed launcher cannot leave a stale lock behind and nobody ever "takes over" a lock.
    Yields True when the lock is held. After `timeout` (a holder that hangs, e.g. a stopped terminal),
    or at once where the filesystem does not support locks, it yields False and the caller proceeds
    unlocked rather than hang the launch.
    """
    ST.run_dir().mkdir(parents=True, exist_ok=True)
    lock = ST.run_dir() / "core.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(str(lock), flags, 0o600)
    except OSError:
        yield False
        return
    held = False
    try:
        deadline = time.time() + timeout
        while True:
            got = _try_lock(fd)
            held = got is True
            if got is not False or time.time() > deadline:
                break
            time.sleep(0.05)
        if held:
            try:  # diagnostics only; the lock is the OS lock, never the file contents
                os.ftruncate(fd, 0)
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, str(os.getpid()).encode())
            except OSError:
                pass
        yield held
    finally:
        if held:
            _unlock(fd)
        os.close(fd)


# ---------------------------------------------------------------------------
# Attach / spawn
# ---------------------------------------------------------------------------

def _attach(wait: float = BUSY_WAIT) -> Tuple[Optional[ST.CoreInfo], Optional[ST.CoreInfo]]:
    """(core, stale) for the Core named by run/core.json.

    core: it proves it holds the admin token and runs the code on disk now (same version and build).
    stale: it proves it holds the token but runs other code (an update since it started, or a Core that
    is retiring); a launch must not use it. Both None when there is no proven Core.
    A Core that is there but busy (503, slow, shedding connections) is retried with backoff for up to
    `wait` seconds while its pid lives: a loaded Core must not be mistaken for a dead one and replaced.
    """
    info = ST.read_core()
    if info is None or not info.admin_token:
        return None, None
    if info.version != __version__:
        wait = 0.0               # never usable: one probe tells whether it is there to retire
    deadline = time.time() + max(0.0, wait)
    timeout, pause = 0.6, 0.1
    while True:
        state, data, _ = probe(info.url, info.admin_token, timeout=timeout)
        if state == "ok":
            if data is None:
                return None, None
            if (info.version == __version__ and data.get("version") == __version__ and not data.get("retiring")
                    and data.get("build") == ST.build_id(refresh=True)):
                return info, None
            return None, info
        if state != "busy" or time.time() + pause >= deadline or not ST.pid_alive(info.pid):
            return None, None
        time.sleep(pause)
        left = deadline - time.time()
        timeout, pause = max(0.3, min(timeout * 2, 2.0, left)), min(pause * 2, 1.0)


def attach(wait: float = BUSY_WAIT) -> Optional[ST.CoreInfo]:
    """The Core named by run/core.json, if it is alive, runs this code (version and build), and proves it
    holds the admin token. A busy Core is waited for (see _attach); a stale one is never returned."""
    return _attach(wait)[0]


def _retire(info: ST.CoreInfo, wait: float = RETIRE_WAIT) -> None:
    """Ask a proven Core that runs other code to retire: it keeps serving the sessions launched against it
    and exits once none is live, at once when it has none (then we wait briefly for its port, so the new
    Core keeps the port people and tools expect). A Core from before /pulsar/retire is only stopped when no
    session is live at all, so a running session never loses its gateway."""
    client = CoreClient(info.url, info.admin_token)
    try:
        res = client.post("/pulsar/retire", {}, timeout=3)
        holding = int(res.get("sessions") or 0) if isinstance(res, dict) else 1
    except CoreUnavailable as exc:
        if exc.status != 404 or ST.live_sessions(reap=False):
            return
        try:
            client.post("/pulsar/shutdown", {}, timeout=3)
        except CoreUnavailable:
            return
        holding = 0
    except (TypeError, ValueError):
        return
    if holding:
        return
    deadline = time.time() + max(0.0, wait)
    while time.time() < deadline and ST.pid_alive(info.pid) and not port_free(info.port):
        time.sleep(0.05)


def _forget_stale() -> None:
    """Drop a run/core.json whose Core is gone (its pid is dead). Called with the spawn lock held."""
    info = ST.read_core()
    if info is not None and not ST.pid_alive(info.pid):
        ST.clear_core(info.pid)


def _pick_port(base: int, avoid: Set[int], prefer: int = 0) -> int:
    if prefer and prefer not in avoid and port_free(prefer):
        return prefer
    return next((p for p in range(base, base + 200) if p not in avoid and port_free(p)), 0)


def _stop(proc: "subprocess.Popen[Any]") -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:
            pass


def _await_core(proc: "subprocess.Popen[Any]", port: int, token: str, timeout: float) -> str:
    """'ok' (our child answered with a valid proof), 'squatted', 'exited' or 'timeout'."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        state, _, _ = probe(_url(port), token, timeout=0.5)
        if state == "ok":
            return "ok"
        if state in ("bad", "closing"):
            # Something answers on our port without the proof: only our child holds the token, so this
            # is another process that won the bind race (or a stale listener). Never trust it.
            return "squatted"
        if proc.poll() is not None:
            return "exited"
        # "down" (still starting) or "busy" (our child is up but loaded, e.g. a connection flood):
        # keep waiting for it; a busy child is never killed for being busy.
        time.sleep(0.05)
    return "timeout"


def _no_proxy(env: Dict[str, str]) -> None:
    """Add loopback to NO_PROXY/no_proxy (both spellings merged), as launch.build_env does for Claude Code:
    a local model server (Ollama, LM Studio, vLLM) is never sent through HTTP(S)_PROXY. PULSAR_PROXY_LOCAL=1
    opts out, for setups that really reach local servers through a proxy."""
    if env.get("PULSAR_PROXY_LOCAL") == "1":
        return
    hosts = []
    for var in ("NO_PROXY", "no_proxy"):
        for part in env.get(var, "").split(","):
            part = part.strip()
            if part and part not in hosts:
                hosts.append(part)
    for h in LOOPBACK_NO_PROXY:
        if h not in hosts:
            hosts.append(h)
    env["NO_PROXY"] = ",".join(hosts)
    if os.name != "nt":                        # one case-insensitive variable on Windows
        env["no_proxy"] = env["NO_PROXY"]


def _drop_unusable_proxies(env: Dict[str, str]) -> None:
    """Leave out of Core's env any proxy variable httpx cannot use (ALL_PROXY=socks4://... from some Clash and
    V2Ray setups): httpx would refuse to build a client and every request would fail. Core then uses the
    remaining HTTP(S) proxy, or none. The note names the variable and scheme only, never the value."""
    for var in ST.unusable_proxies(env):
        scheme = ST.proxy_scheme(env.pop(var, ""))
        try:
            sys.stderr.write("pulsar: ignoring %s for Pulsar Core: its %s:// proxy is not supported; "
                             "use socks5:// or http:// instead.\n" % (var, scheme))
        except Exception:
            pass


def _child_env(token: str) -> Dict[str, str]:
    env = dict(os.environ)
    for var in _CHILD_ENV_DROP:
        env.pop(var, None)
    for var in _CHILD_ENV_PATHS:
        val = env.get(var, "")
        path = os.path.expanduser(val) if var == "PULSAR_HOME" else val
        if path and not os.path.isabs(path):
            env[var] = os.path.abspath(path)
    _no_proxy(env)
    _drop_unusable_proxies(env)
    # The admin token goes to Core on stdin (see _hand_token), never in argv (world-readable) or the
    # environment (/proc/<pid>/environ keeps the initial copy even after Core pops the variable). This
    # marker is what tells Core to read it there: a Core started any other way never waits on stdin.
    env["PULSAR_TOKEN_STDIN"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env.pop("PYTHONSTARTUP", None)
    return env


def _hand_token(proc: "subprocess.Popen[Any]", token: str) -> None:
    """Write the admin token to Core's stdin, one line, and close it. A child that already died is fine:
    _await_core reports that."""
    pipe = getattr(proc, "stdin", None)
    if pipe is None:
        return
    try:
        pipe.write((token + "\n").encode("ascii"))
        pipe.flush()
    except (OSError, ValueError):
        pass
    finally:
        try:
            pipe.close()
        except (OSError, ValueError):
            pass


def _peer_core(wait: float) -> Optional[ST.CoreInfo]:
    """A proven Core a peer launch published, polling for up to `wait` seconds."""
    deadline = time.time() + wait
    while True:
        peer = attach(wait=0)
        if peer is not None or time.time() >= deadline:
            return peer
        time.sleep(0.1)


def _publish_unlocked(proc: "subprocess.Popen[Any]", info: ST.CoreInfo) -> ST.CoreInfo:
    """Publish a proven Core started without the spawn lock (a filesystem without locks, a holder that hung).
    Peers may have started Cores at the same time, and a check before an ordinary write let two of them each
    keep their own: the first to publish wins (ST.publish_core is one step), and every other launch stops its
    Core and uses the winner's, so there is still one Core."""
    for _ in range(3):
        if ST.publish_core(info):
            return info
        peer = attach(wait=1.0)
        if peer is not None:
            _stop(proc)
            return peer
        old = ST.read_core()            # a dead or retiring Core's descriptor: drop it, then publish again
        if old is not None:
            ST.clear_core(old.pid)
    ST.write_core(info)
    return info


def _spawn(timeout: float, prefer_port: int = 0, locked: bool = True) -> ST.CoreInfo:
    """Start a Core, prove it, publish it. `locked` is False when the spawn lock could not be taken
    (a filesystem without locks, or a holder that hung): peers may then be starting Cores at the same
    time, so a lost port race first waits briefly for the winner to publish and adopts its Core."""
    base = _base_port()
    home = os.path.abspath(str(ST.run_dir().parent))
    log = ST.run_dir().parent / "logs" / "core.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    avoid: Set[int] = set()
    squats = 0
    for _ in range(SPAWN_ATTEMPTS):
        port = _pick_port(base, avoid, prefer_port)
        if not port:
            raise RuntimeError("no free loopback port in %d-%d" % (base, base + 199))
        avoid.add(port)
        token = ST.new_token(ST.ADMIN_TOKEN_PREFIX)
        if os.environ.get("PULSAR_LOG") == "1":
            sink: Any = os.fdopen(os.open(str(log), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a")
        else:
            sink = open(os.devnull, "w")
        kwargs: Dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(pulsar.python_argv("core", "--port", str(port)), stdin=subprocess.PIPE,
                                    stdout=sink, stderr=sink, env=_child_env(token), cwd=home,
                                    close_fds=True, **kwargs)
        finally:
            sink.close()
        _hand_token(proc, token)
        outcome = _await_core(proc, port, token, timeout)
        if outcome == "ok":
            info = ST.CoreInfo(port, proc.pid, __version__, token, time.time())
            if not locked:
                return _publish_unlocked(proc, info)
            # A peer launch that could not take the lock (a filesystem without locks, a timed-out wait)
            # may have published its own proven Core meanwhile: use that one, so there is still one Core.
            peer = attach(wait=0)
            if peer is not None and peer.pid != proc.pid:
                _stop(proc)
                return peer
            ST.write_core(info)
            return info
        _stop(proc)
        lost = outcome == "squatted" or (outcome == "exited" and (proc.returncode == ST.PORT_IN_USE_EXIT
                                                                  or not port_free(port)))
        if lost:
            # Something else took the port between our check and our child's bind. Unlocked, that is most
            # likely a peer launch whose Core is about to publish itself: use it rather than start another.
            peer = _peer_core(min(timeout, 5.0) if not locked else 0)
            if peer is not None:
                return peer
            squats += 1
            continue  # try the next port
        if outcome == "exited":
            raise RuntimeError("Pulsar Core exited during start (code %s). Run with PULSAR_LOG=1 and read %s"
                               % (proc.returncode, log))
        raise RuntimeError("Pulsar Core did not answer on 127.0.0.1:%d within %ds" % (port, timeout))
    raise RuntimeError("another process took Pulsar Core's loopback port %d times in a row (%s); "
                       "refusing to trust it. Set PULSAR_PORT to another range or find the process with "
                       "`lsof -i :%d`" % (squats, ", ".join(str(p) for p in sorted(avoid)), min(avoid)))


def ensure_core(timeout: float = 25.0, prefer_port: int = 0) -> ST.CoreInfo:
    """Return a healthy, proven Core of this version, starting one if needed.

    prefer_port: start a new Core on this port when it is free (a session whose Core went away keeps
    its ANTHROPIC_BASE_URL working when Core comes back on the same port).
    """
    info = attach()
    if info is not None:
        return info
    # Claim the home before the lock and the log create run/ and logs/ in it: Core refuses an unclaimed folder
    # that already holds files, so a caller that did not claim it (the launcher does) would get a Core that exits.
    from pulsar import config as K
    K.ensure_home()
    with spawn_lock(timeout=timeout + 20.0) as held:
        # Re-check under the lock: a peer launch may have started Core while we waited.
        info, stale = _attach(wait=1.0)
        if info is not None:
            return info
        _forget_stale()
        if stale is not None:
            _retire(stale)       # runs other code: it keeps its own sessions, new launches get a new Core
        return _spawn(timeout, prefer_port, locked=held)


def revive(url: str = "", timeout: float = 25.0) -> Optional[ST.CoreInfo]:
    """For in-session clients whose Core went away (connection refused while the session is live).

    Starts (or attaches to) a proven Core, on the session's own port when it is free so Claude Code's
    ANTHROPIC_BASE_URL keeps working. Returns None when it could not. Never trusts whatever else may
    answer on that port.
    """
    port = 0
    if url:
        try:
            port = int(url.rstrip("/").rsplit(":", 1)[-1])
        except ValueError:
            port = 0
    try:
        return ensure_core(timeout=timeout, prefer_port=port)
    except (RuntimeError, OSError):
        return None


def stop_core(force: bool = False) -> bool:
    """Ask the Core in run/core.json to stop. Only a Core that proves its identity gets the admin token.

    It retires: new launches stop using it at once, and it exits as soon as no session it serves is live (at
    once when none is). A live session's Claude Code keeps sending to Core's port, so emptying that port
    under it would let any local process take the port and receive the session's next request. `force`
    stops it now, sessions or not.
    """
    info = ST.read_core()
    if info is None:
        return False
    state, _, _ = probe(info.url, info.admin_token, timeout=1.0)
    if state != "ok":
        if not ST.pid_alive(info.pid):
            ST.clear_core(info.pid)
        return False
    try:
        CoreClient(info.url, info.admin_token).post("/pulsar/shutdown" if force else "/pulsar/retire", {}, timeout=3)
    except CoreUnavailable:
        return False
    return True
