# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Pulsar Core hardening: identity proof, descriptor lifecycle, spawn lock, scopes, body and socket limits.

Some tests start real Core processes on loopback. Every one of them is stopped through its own admin token
in a finalizer (never by a process number), and they run with a short grace period as a second safety net.
"""
import http.server
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager

import pytest
from starlette.testclient import TestClient

import pulsar
from pulsar import config as K
from pulsar.core import client as CL
from pulsar.core import server as SV
from pulsar.core import spawn
from pulsar.core import state as ST
from pulsar.core.server import make_app
from pulsar.core.stats import Stats

ADMIN = ST.ADMIN_TOKEN_PREFIX + "admin-security-test"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
POSIX = os.name != "nt"
posix_only = pytest.mark.skipif(not POSIX, reason="POSIX signals and process semantics")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def free_base(span: int = 12) -> int:
    """A run of `span` free ports, so a Core started by a test never lands on another test's port."""
    for _ in range(50):
        base = free_port()
        if base + span < 65535 and all(spawn.port_free(p) for p in range(base, base + span)):
            return base
    raise RuntimeError("no free port range")


def one_process(pid: object) -> bool:
    """A number that can name one process of ours: an integer above 1 (0, -1 and the like name groups or every
    process; 1 is the system's own), never a bool."""
    return isinstance(pid, int) and not isinstance(pid, bool) and pid > 1


def wait_dead(pid: int, timeout: float = 15.0) -> bool:
    if not one_process(pid):
        return True                          # never one of our Cores: nothing to wait for
    deadline = time.time() + timeout
    while time.time() < deadline:
        if POSIX:
            try:
                os.waitpid(pid, os.WNOHANG)  # reap our own children so they do not linger as zombies
            except (ChildProcessError, OSError):
                pass
        if not ST.pid_alive(pid):
            return True
        time.sleep(0.05)
    return False


def reap(pid: int) -> bool:
    """True once `pid` is nothing of ours to wait for: reaped now, or not (or no longer) our child.
    Reaping only ever touches this process's own children; it signals nothing."""
    if not POSIX or not one_process(pid):
        return True
    try:
        return os.waitpid(pid, os.WNOHANG)[0] == pid
    except OSError:
        return True


def stop_core(info: ST.CoreInfo, timeout: float = 10.0) -> None:
    """Stop a Core a test started, through its own admin token, once it proves it holds that token. Never by a
    process number read back: by teardown that number may belong to another process."""
    if CL.probe(info.url, info.admin_token, timeout=1.0)[0] != "ok":
        reap(info.pid)
        return
    try:
        CL.CoreClient(info.url, info.admin_token).post("/pulsar/shutdown", {}, timeout=3)
    except CL.CoreUnavailable:
        return
    deadline = time.time() + timeout
    while time.time() < deadline and not (spawn.port_free(info.port) and reap(info.pid)):
        time.sleep(0.05)


def command_line(pid: int) -> str:
    """The command line the system reports for process `pid` ("" when it cannot be read). Reads only."""
    proc = "/proc/%d/cmdline" % pid
    if os.path.exists(proc):
        with open(proc, "rb") as fh:
            return fh.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    try:
        out = subprocess.run(["/bin/ps", "-ww", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=10, env=dict(os.environ, LC_ALL="C"))
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip()


def signal_own_child(info: ST.CoreInfo, sig: int) -> None:
    """Signal a Core this test started, once this step proves its number is that Core: an integer above 1, our
    own child that has not exited (an unreaped child's number cannot have been given to anything else), and a
    command line that runs `pulsar core` on the Core's own port."""
    pid = info.pid
    assert one_process(pid), "%r names no single process: not signalled" % (pid,)
    assert os.waitpid(pid, os.WNOHANG) == (0, 0), "%d is not our running child: not signalled" % pid
    assert command_line(pid).endswith("pulsar core --port %d" % info.port), "%d is not this Core: not signalled" % pid
    os.kill(pid, sig)


@posix_only
def test_a_process_number_is_signalled_only_once_proven_to_be_this_tests_core(monkeypatch):
    # -1 or 0 would reach every process of the user (or of the process group) and 1 is the system's own; a child of
    # this test whose command line is not a Pulsar Core on the given port is not signalled either. The signal
    # itself is replaced here, so nothing can leave this test whatever the helpers do.
    sent = []
    monkeypatch.setattr(os, "kill", lambda *a: sent.append(a))
    for pid in (-1, 0, 1, True, None):
        with pytest.raises(AssertionError):
            signal_own_child(ST.CoreInfo(4747, pid, pulsar.__version__, "pla_x", 0.0), signal.SIGTERM)
        assert reap(pid) and wait_dead(pid, 0.1), pid
    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    try:
        with pytest.raises(AssertionError):
            signal_own_child(ST.CoreInfo(4747, child.pid, pulsar.__version__, "pla_x", 0.0), signal.SIGTERM)
    finally:
        child.stdin.close()                  # it ends by itself once its input closes: no signal needed
        child.wait(timeout=30)
    assert sent == []


@pytest.fixture
def cores(monkeypatch):
    """Real Cores for one test: each CoreInfo recorded here, and the one core.json names, is stopped through
    its admin token at teardown; their short grace period is the second safety net."""
    monkeypatch.setenv("PULSAR_PORT", str(free_base()))
    monkeypatch.setenv("PULSAR_CORE_GRACE", "8")
    monkeypatch.setenv("PULSAR_CORE_QUIET", "0")
    K.ensure_home()
    started = []
    yield started
    info = ST.read_core()
    seen = set()
    for core in ([info] if info else []) + started[::-1]:     # newest first: a revived Core holds an old port
        if (core.port, core.pid, core.admin_token) not in seen:
            seen.add((core.port, core.pid, core.admin_token))
            stop_core(core)


class Fake(http.server.ThreadingHTTPServer):
    """A local process pretending to be Core (squatter / impostor). Records every request it receives."""

    daemon_threads = True

    def __init__(self, port: int, reply=None, redirect_to: str = "") -> None:
        self.seen = []
        self.reply = reply or {"status": "ok", "service": "pulsar-core", "version": pulsar.__version__,
                               "pid": 0, "sessions": 0}
        self.redirect_to = redirect_to
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _any(self):
                n = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
                outer.seen.append((self.command, self.path, self.headers.get("authorization"), body))
                if outer.redirect_to and not self.path.startswith("/healthz"):
                    self.send_response(302)
                    self.send_header("Location", outer.redirect_to + self.path)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                out = json.dumps(outer.reply if self.path.startswith("/healthz") else {"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            do_GET = do_POST = do_HEAD = _any

        super().__init__(("127.0.0.1", port), H)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def tokens_seen(self):
        return [a for _, _, a, _ in self.seen if a]

    def stop(self):
        self.shutdown()
        self.server_close()


@contextmanager
def live_app(app, port, header_timeout=0.0, impl="auto", max_conns=0):
    """make_app() on a real uvicorn socket in a thread (SSE, raw sockets, chunked bodies)."""
    import uvicorn
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False, lifespan="on",
                         http=SV._http_protocol(header_timeout, impl, max_conns=max_conns),
                         timeout_graceful_shutdown=1)
    srv = uvicorn.Server(cfg)
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    deadline = time.time() + 10
    while not srv.started and time.time() < deadline:
        time.sleep(0.02)
    assert srv.started
    try:
        yield "http://127.0.0.1:%d" % port
    finally:
        app.state.closing = True
        srv.should_exit = True
        th.join(10)


def get_json(url, token=None, timeout=5):
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read())


# ---------------------------------------------------------------------------
# 1. Identity proof
# ---------------------------------------------------------------------------

def test_healthz_is_public_but_carries_nothing_replayable(pulsar_home):
    c = TestClient(make_app(ADMIN, 0))
    pub = c.get("/healthz").json()
    assert pub["service"] == "pulsar-core" and "pid" not in pub and "sessions" not in pub and "proof" not in pub
    nonce = ST.new_nonce()
    proved = c.get("/healthz", params={"nonce": nonce}).json()
    assert ST.proof_ok(ADMIN, nonce, proved["proof"])
    assert not ST.proof_ok("pla_other", nonce, proved["proof"])
    assert not ST.proof_ok(ADMIN, ST.new_nonce(), proved["proof"])
    assert c.get("/healthz", params={"nonce": "zz" * 32}).status_code == 400
    assert c.get("/healthz", params={"nonce": "ab"}).status_code == 400
    assert c.head("/api/hello").status_code == 200


def test_attach_refuses_an_impostor_on_a_stale_descriptor(pulsar_home):
    # Core died uncleanly (SIGKILL/OOM): core.json survives; another local user squats the port and
    # answers /healthz with the old (public) pid and version.
    port = free_port()
    K.ensure_home()
    ST.write_core(ST.CoreInfo(port, 424242, pulsar.__version__, "pla_real_admin_token_of_victim", 0))
    fake = Fake(port, {"status": "ok", "service": "pulsar-core", "version": pulsar.__version__, "pid": 424242})
    try:
        assert spawn.attach() is None
        assert spawn.health(port, admin_token="pla_real_admin_token_of_victim") is None
        assert not spawn.verify(port, "pla_real_admin_token_of_victim")
        admin = CL.CoreClient.admin()
        with pytest.raises(CL.CoreUnavailable):
            admin.post("/pulsar/ephemeral-key", {"profile": "openrouter", "key": "sk-or-v1-" + "x" * 40})
        with pytest.raises(CL.CoreUnavailable):
            list(admin.events())
        assert not admin.proven()
        assert spawn.stop_core() is False
        assert fake.tokens_seen() == []                       # the admin token never reached it
        assert not any("sk-or" in body for *_, body in fake.seen)
        assert all(path.startswith("/healthz?nonce=") for _, path, _, _ in fake.seen)
    finally:
        fake.stop()
    # its pid is dead, so the stale descriptor is dropped
    assert ST.read_core() is None


def test_client_never_follows_redirects_nor_leaves_loopback(pulsar_home):
    sink = Fake(free_port())
    bouncer = Fake(free_port(), redirect_to="http://127.0.0.1:%d" % sink.server_address[1])
    try:
        c = CL.CoreClient("http://127.0.0.1:%d" % bouncer.server_address[1], "pls_session-token")
        with pytest.raises(CL.CoreUnavailable) as ei:
            c.get("/pulsar/stats")
        assert ei.value.status == 302
        assert sink.seen == []                                # the token was not re-sent anywhere
        with pytest.raises(urllib.error.HTTPError):
            spawn._OPENER.open("http://127.0.0.1:%d/x" % bouncer.server_address[1], timeout=2)
        assert sink.seen == []
    finally:
        sink.stop()
        bouncer.stop()
    for url in ("http://evil.example:4747", "https://127.0.0.1:4747", "http://user:pw@127.0.0.1:1"):
        with pytest.raises(CL.CoreUnavailable):
            CL.CoreClient(url, "pls_x").get("/pulsar/stats")
    assert CL.CoreClient.from_session_env({"PULSAR_CORE": "http://10.0.0.5:4747", "ANTHROPIC_AUTH_TOKEN": "pls_x"}) is None
    assert CL.CoreClient.from_session_env({"PULSAR_CORE": "http://localhost:4747", "ANTHROPIC_AUTH_TOKEN": "pls_x"})


def test_connection_refused_is_flagged_for_respawn(pulsar_home):
    port = free_port()
    with pytest.raises(CL.CoreUnavailable) as ei:
        CL.CoreClient("http://127.0.0.1:%d" % port, "pls_x").get("/pulsar/stats", timeout=1)
    assert ei.value.refused
    with pytest.raises(CL.CoreUnavailable) as ei:
        CL.CoreClient("http://127.0.0.1:%d" % port, ADMIN).get("/pulsar/stats", timeout=1)
    assert ei.value.refused


def test_spawn_uses_safe_argv_and_env(pulsar_home, monkeypatch):
    seen = {"stdin_data": b""}

    class Pipe:
        def __init__(self):
            self.data, self.closed = b"", False

        def write(self, b):
            self.data += b

        def flush(self):
            pass

        def close(self):
            self.closed = True

    class P:
        pid = 99999999
        returncode = 1

        def __init__(self):
            self.stdin = Pipe()

        def poll(self):
            return 1

    def fake_popen(argv, **kw):
        seen["argv"], seen["env"], seen["cwd"], seen["stdin"] = argv, kw["env"], kw["cwd"], kw["stdin"]
        seen["proc"] = P()
        return seen["proc"]

    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "pls_leak")
    monkeypatch.setenv("PULSAR_SESSION_TOKEN", "pls_leak2")
    monkeypatch.setenv("PULSAR_ADMIN_TOKEN", ST.ADMIN_TOKEN_PREFIX + "inherited")   # never handed down
    monkeypatch.setattr(spawn.subprocess, "Popen", fake_popen)
    monkeypatch.setenv("PULSAR_PORT", str(free_base()))
    with pytest.raises(RuntimeError):
        spawn.ensure_core(timeout=1)
    argv = seen["argv"]
    assert argv[0] == sys.executable and "-E" in argv and "-B" in argv and "-c" in argv
    assert argv[-4:-1] == ["pulsar", "core", "--port"]
    assert "-m" not in argv and seen["cwd"] == os.path.abspath(str(K.home()))   # never the app dir (Windows updates)
    assert not any(a.startswith(ST.ADMIN_TOKEN_PREFIX) for a in argv)
    # the admin token goes to Core on stdin, one line, then the pipe is closed; never argv, never the
    # environment (/proc/<pid>/environ keeps the initial copy)
    handed = seen["proc"].stdin
    token = handed.data.decode("ascii").strip()
    assert seen["stdin"] == subprocess.PIPE and handed.closed and handed.data == (token + "\n").encode("ascii")
    assert token.startswith(ST.ADMIN_TOKEN_PREFIX) and token != ST.ADMIN_TOKEN_PREFIX + "inherited"
    assert "PULSAR_ADMIN_TOKEN" not in seen["env"] and not any(token in v for v in seen["env"].values())
    assert "ANTHROPIC_AUTH_TOKEN" not in seen["env"] and "PULSAR_SESSION_TOKEN" not in seen["env"]
    assert seen["env"].get("PULSAR_TOKEN_STDIN") == "1"   # the only way Core knows to read its stdin
    assert ST.read_core() is None                         # a failed start never publishes a descriptor


@posix_only
def test_a_core_started_into_a_fresh_home_claims_the_home_first(pulsar_home):
    # Starting a Core creates run/ and logs/ in the home, and Core refuses an unclaimed folder that already holds
    # files. Whoever asks for a Core (not only the launcher, which claims the home itself) gets the home claimed
    # first, never a Core that exits at start.
    assert not pulsar_home.exists()
    info = spawn.ensure_core()
    try:
        assert spawn.verify(info.port, info.admin_token)
        assert K.marker_kind(pulsar_home) == "created"
    finally:
        stop_core(info)


def test_core_started_by_hand_never_waits_on_an_open_stdin(pulsar_home):
    # A supervisor (or a shell pipeline) can hand Core a stdin pipe that stays open: without
    # PULSAR_ADMIN_TOKEN, Core must say so and exit rather than wait on it forever.
    env = {k: v for k, v in os.environ.items() if k not in ("PULSAR_ADMIN_TOKEN", "PULSAR_TOKEN_STDIN")}
    proc = subprocess.Popen(pulsar.python_argv("core", "--port", str(free_port())), stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env, cwd=pulsar.ROOT)
    try:
        code = proc.wait(timeout=30)                      # stdin stays open the whole time
    except subprocess.TimeoutExpired:
        proc.kill()                                       # the process this test started, through its own handle
        proc.wait()
        raise AssertionError("pulsarcode core waited on an open stdin")
    finally:
        proc.stdin.close()
    err = proc.stderr.read().decode("utf-8", "replace")
    proc.stderr.close()
    assert code == 2 and "PULSAR_ADMIN_TOKEN missing" in err


@posix_only
def test_real_core_proof_sigterm_clears_descriptor(cores):
    info = spawn.ensure_core()
    cores.append(info)
    assert spawn.attach() == info and spawn.verify(info.port, info.admin_token)
    assert not spawn.verify(info.port, "pla_wrong")
    for name in ("cmdline", "environ"):                 # the token reached Core on stdin, nowhere else
        if os.path.exists("/proc/%d/%s" % (info.pid, name)):
            with open("/proc/%d/%s" % (info.pid, name), "rb") as fh:
                assert info.admin_token.encode() not in fh.read()
    sess, tok = ST.new_session([ST.Target("openrouter", "openrouter", "m/x")], {})
    assert get_json(info.url + "/v1/models", tok)["data"][0]["id"] == "m/x"
    # an open SSE observer must not hold up shutdown
    stream = CL.CoreClient(info.url, info.admin_token).events(timeout=10)
    assert next(stream)[0] == "tick"
    signal_own_child(info, signal.SIGTERM)
    assert wait_dead(info.pid, 15)
    assert not ST.core_file().exists()
    stream.close()


def test_squatter_that_wins_the_bind_race_is_detected(cores, monkeypatch):
    # Something binds the port we picked between our free-port check and our child's bind, and answers
    # /healthz like Core (old pid scheme).
    base = int(os.environ["PULSAR_PORT"])
    fake = Fake(base)
    real_free = spawn.port_free
    lied = []

    def port_free(p):
        if p == base and not lied:
            lied.append(p)
            return True       # the squatter arrives right after our check
        return real_free(p)

    monkeypatch.setattr(spawn, "port_free", port_free)
    try:
        info = spawn.ensure_core()
        cores.append(info)
        assert info.port != base and spawn.verify(info.port, info.admin_token)
        assert ST.read_core() == info
        assert fake.tokens_seen() == [] and all(p.startswith("/healthz?nonce=") for _, p, _, _ in fake.seen)
    finally:
        fake.stop()


def test_squatter_on_every_port_fails_closed(pulsar_home, monkeypatch):
    monkeypatch.setenv("PULSAR_PORT", str(free_base()))
    fakes = []

    def popen(argv, **kw):
        port = int(argv[-1])
        fakes.append(Fake(port))            # the attacker takes each port we pick

        class P:
            pid, returncode = 99999998, None

            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0
        return P()

    monkeypatch.setattr(spawn.subprocess, "Popen", popen)
    try:
        with pytest.raises(RuntimeError, match="took Pulsar Core's loopback port"):
            spawn.ensure_core(timeout=3)
        assert len(fakes) == spawn.SPAWN_ATTEMPTS
        assert ST.read_core() is None
        assert all(f.tokens_seen() == [] for f in fakes)
    finally:
        for f in fakes:
            f.stop()


# ---------------------------------------------------------------------------
# 2-3. Descriptor and lifetime
# ---------------------------------------------------------------------------

def test_clear_core_only_removes_its_own_descriptor(pulsar_home):
    K.ensure_home()
    ST.write_core(ST.CoreInfo(1, 4242, "v", "pla_x"))
    assert ST.clear_core(4243) is False and ST.core_file().exists()
    assert ST.clear_core(4242) is True and not ST.core_file().exists()


@posix_only
def test_idle_cap_never_exits_while_a_session_is_live(cores, monkeypatch):
    monkeypatch.setenv("PULSAR_CORE_IDLE", "0.5")
    monkeypatch.setenv("PULSAR_CORE_GRACE", "0")
    monkeypatch.setenv("PULSAR_CORE_SCAN", "0.2")
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        sess, _ = ST.new_session([ST.Target("openrouter", "openrouter", "m")], {})
        sess.pid = holder.pid
        ST.save_session(sess)
        info = spawn.ensure_core()
        cores.append(info)
        time.sleep(2.5)                               # 5x the idle cap, 12 lifecycle scans
        assert ST.pid_alive(info.pid) and spawn.verify(info.port, info.admin_token)
        holder.kill()
        holder.wait()
        assert wait_dead(info.pid, 15)                # no live session left: it does go away
        assert not ST.core_file().exists()
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait()


@posix_only
def test_revive_restarts_core_on_the_sessions_port(cores):
    info = spawn.ensure_core()
    cores.append(info)
    signal_own_child(info, signal.SIGKILL)            # crash: the descriptor stays behind
    assert wait_dead(info.pid)
    assert ST.read_core() is not None and spawn.attach() is None
    with pytest.raises(CL.CoreUnavailable) as ei:
        CL.CoreClient(info.url, "pls_x").get("/pulsar/stats", timeout=1)
    assert ei.value.refused
    again = spawn.revive(info.url)
    assert again is not None
    cores.append(again)
    assert again.port == info.port and again.pid != info.pid and spawn.attach() == again


# ---------------------------------------------------------------------------
# 4. Spawn lock
# ---------------------------------------------------------------------------

LOCK_HOLDER = r"""
import os, sys, time
sys.path.insert(0, %r)
from pulsar.core import spawn
with spawn.spawn_lock(timeout=5) as held:
    print("held" if held else "nolock", flush=True)
    time.sleep(float(sys.argv[1]))
"""


def test_spawn_lock_is_exclusive_and_dies_with_its_holder(pulsar_home):
    K.ensure_home()
    (ST.run_dir() / "core.lock").write_text("999999")      # a "stale" lock file from an old version
    code = LOCK_HOLDER % pulsar.ROOT
    holder = subprocess.Popen([sys.executable, "-c", code, "30"], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        t0 = time.time()
        with spawn.spawn_lock(timeout=0.4) as held:
            assert not held and time.time() - t0 >= 0.35
        holder.kill()                                         # SIGKILL: no cleanup code runs
        holder.wait()
        t0 = time.time()
        with spawn.spawn_lock(timeout=5) as held:
            assert held and time.time() - t0 < 1.0
        assert (ST.run_dir() / "core.lock").exists()          # never unlinked
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait()


RACER = r"""
import json, sys, time
sys.path.insert(0, %r)
from pulsar.core import spawn
t = float(sys.argv[1])
while time.time() < t:
    time.sleep(0.001)
try:
    info = spawn.ensure_core()
    print(json.dumps({"port": info.port, "pid": info.pid}))
except Exception as e:
    print(json.dumps({"error": "%%s: %%s" %% (type(e).__name__, e)}))
"""


def test_concurrent_launches_with_a_stale_lock_start_exactly_one_core(cores):
    # Six launches at once, with a lock file left by a SIGKILLed launcher.
    (ST.run_dir() / "core.lock").write_text("999999")
    go = time.time() + 1.0
    procs = [subprocess.Popen([sys.executable, "-c", RACER % pulsar.ROOT, str(go)], stdout=subprocess.PIPE, text=True)
             for _ in range(6)]
    res = [json.loads(p.communicate(timeout=60)[0].strip().splitlines()[-1]) for p in procs]
    assert all("error" not in r for r in res), res
    assert len({(r["port"], r["pid"]) for r in res}) == 1, res
    info = ST.read_core()
    assert info is not None and info.pid == res[0]["pid"] and spawn.verify(info.port, info.admin_token)


# ---------------------------------------------------------------------------
# 5. Scopes
# ---------------------------------------------------------------------------

def _two_sessions():
    K.ensure_home()
    sa, ta = ST.new_session([ST.Target("openrouter", "openrouter", "moonshotai/kimi-k2")], label="project-A",
                            cwd="/work/project-A")
    sb, tb = ST.new_session([ST.Target("custom-corp", "custom", "secret-model", "https://llm.internal.corp/v1")],
                            label="client-B-confidential", cwd="/work/client-B-confidential")
    return sa, ta, sb, tb


def _leaks_b(text, sb):
    return any(s in text for s in (sb.sid, "client-B", "llm.internal.corp", "secret-model", "custom-corp"))


def test_session_view_token_only_sees_its_own_session(pulsar_home):
    sa, ta, sb, tb = _two_sessions()
    app = make_app(ADMIN, 0)
    app.state.stats.begin("r1", sb.sid, "secret-model", "custom")
    app.state.stats.end("r1", ok=False, error="HTTP 401 Invalid API key provided: sk-proj-" + "Q" * 30)
    c = TestClient(app)
    view = c.get("/pulsar/stage/viewer", headers={"authorization": "Bearer " + ta}).json()
    tok = view["url"].split("t=")[1].split("&")[0]
    assert view["sid"] == sa.sid and tok == ST.view_token(ADMIN, sa.sid)
    c.post("/pulsar/stage", json={"scene": {"title": "A scene"}}, headers={"authorization": "Bearer " + ta})
    c.post("/pulsar/stage", json={"scene": {"title": "B private"}}, headers={"authorization": "Bearer " + tb})
    for q in ("?t=%s&sid=%s" % (tok, sa.sid),):
        st = c.get("/pulsar/stats" + q)
        assert st.status_code == 200
        body = st.text
        assert "sessions_live" not in st.json() and "sessions" not in st.json() and not _leaks_b(body, sb)
        assert st.json()["session"]["requests"] == 0
        scenes = c.get("/pulsar/stage/scenes" + q).json()["scenes"]
        assert [s["scene"]["title"] for s in scenes] == ["A scene"]
    # a scoped token cannot widen its scope by naming another sid, or no sid
    for q in ("?t=%s&sid=%s" % (tok, sb.sid), "?t=%s" % tok):
        assert c.get("/pulsar/stats" + q).status_code == 401
        assert c.get("/pulsar/stage/scenes" + q).status_code == 401
    # session tokens are scoped the same way and ignore ?sid=
    st = c.get("/pulsar/stats?sid=" + sb.sid, headers={"authorization": "Bearer " + ta})
    assert not _leaks_b(st.text, sb)
    sc = c.get("/pulsar/stage/scenes?sid=" + sb.sid, headers={"authorization": "Bearer " + ta}).json()["scenes"]
    assert [s["scene"]["title"] for s in sc] == ["A scene"]
    # the admin token and the admin-derived global view token see everything
    for q, h in (("", {"authorization": "Bearer " + ADMIN}), ("?t=" + ST.view_token(ADMIN, ""), {})):
        full = c.get("/pulsar/stats" + q, headers=h).json()
        assert {s["sid"] for s in full["sessions_live"]} >= {sa.sid, sb.sid} and sb.sid in full["sessions"]
        assert "sk-proj-QQQQ" not in json.dumps(full)                  # redacted at the source
        assert len(c.get("/pulsar/stage/scenes" + q, headers=h).json()["scenes"]) == 2


def test_only_view_tokens_ride_in_urls_and_unknown_tokens_cost_no_rereads(pulsar_home, monkeypatch):
    sa, ta, sb, tb = _two_sessions()
    c = TestClient(make_app(ADMIN, 0))
    # an admin or session token in a URL would end up in browser history and logs: refused there
    for tok in (ADMIN, ta):
        assert c.get("/pulsar/stats?t=" + tok).status_code == 401
        assert c.get("/pulsar/stage/scenes?t=" + tok).status_code == 401
        assert c.get("/pulsar/stats", headers={"authorization": "Bearer " + tok}).status_code == 200
    # the read-only view tokens are made for URLs (`pulsarcode stage --web`)
    assert c.get("/pulsar/stats?t=%s&sid=%s" % (ST.view_token(ADMIN, sa.sid), sa.sid)).status_code == 200
    assert c.get("/pulsar/stage/scenes?t=" + ST.view_token(ADMIN, "")).status_code == 200
    # unknown session tokens (from a URL or a header) never make Core re-read settled session files
    old = time.time() - 60
    for s in (sa, sb):
        os.utime(str(ST.session_path(s.sid)), (old, old))
    sessions = SV.Sessions()
    assert sessions.lookup(ta) is not None
    reads = []
    real = SV.Sessions._load
    monkeypatch.setattr(SV.Sessions, "_load", staticmethod(lambda p: reads.append(p) or real(p)))
    for n in range(20):
        assert sessions.lookup("pls_unknown-%02d-padding" % n) is None
        assert c.get("/pulsar/stage/scenes?t=pls_unknown-%02d-padding" % n).status_code == 401
    assert reads == []
    sc, tc = ST.new_session([ST.Target("openrouter", "openrouter", "m/late")])    # a new session is still seen
    assert sessions.lookup(tc).sid == sc.sid
    assert reads and all(p == ST.session_path(sc.sid) for p in reads)              # and only its file is read


def test_the_stage_page_runs_only_its_own_script(pulsar_home, monkeypatch):
    # The policy Core sends with the page: its one inline script runs by its hash (a script injected into the page
    # would never run), nothing else loads, and no other page may frame it. The same holds for the minimal page
    # served when the full viewer cannot be built.
    import base64
    import hashlib
    import re
    from pulsar.stage import web as W
    c = TestClient(make_app(ADMIN, 0))
    url = "/stage?t=" + ST.view_token(ADMIN, "")
    for broken in (False, True):
        if broken:
            monkeypatch.setattr(W, "page", lambda scenes=None: 1 / 0)
        r = c.get(url)
        assert r.status_code == 200, broken
        policy = {d.split()[0]: " ".join(d.split()[1:]) for d in r.headers["content-security-policy"].split(";")
                  if d.strip()}
        scripts = re.findall(r"<script>(.*?)</script>", r.text, re.S)
        assert len(scripts) == 1, broken
        digest = base64.b64encode(hashlib.sha256(scripts[0].encode("utf-8")).digest()).decode("ascii")
        assert policy["script-src"] == "'sha256-%s'" % digest, broken
        assert policy["default-src"] == "'none'" and policy["frame-ancestors"] == "'none'", broken


def _first(stream, name, limit=50):
    for _ in range(limit):
        ev, data = next(stream)
        if ev == name:
            return data
    raise AssertionError("no %s event" % name)


def test_scoped_event_and_stage_streams_never_carry_other_sessions(pulsar_home):
    sa, ta, sb, tb = _two_sessions()
    port = free_port()
    app = make_app(ADMIN, port)
    st = app.state.stats
    st.begin("rb", sb.sid, "secret-model", "custom")
    st.end("rb", ok=False, error="boom from B")
    with live_app(app, port) as url:
        a_client, b_client = CL.CoreClient(url, ta), CL.CoreClient(url, tb)
        tok = ST.view_token(ADMIN, sa.sid)
        for n, (client, path) in enumerate(((CL.CoreClient(url, tok), "/pulsar/events?sid=" + sa.sid),
                                            (a_client, "/pulsar/events"))):
            stream = client.events(path, timeout=10)
            tick = _first(stream, "tick")
            assert "sessions" not in tick and not _leaks_b(json.dumps(tick), sb)
            b_client.post("/pulsar/stage", {"scene": {"title": "B private %d" % n}})   # B's traffic while A watches
            a_client.post("/pulsar/stage", {"scene": {"title": "A event %d" % n}})
            seen = []
            for _ in range(20):
                ev, data = next(stream)
                seen.append((ev, data))
                if ev == "stage":
                    break
            assert seen[-1][1]["sid"] == sa.sid and seen[-1][1]["title"] == "A event %d" % n
            assert not any(_leaks_b(json.dumps(d), sb) or "B private" in json.dumps(d) for _, d in seen)
            stream.close()
        # stage stream: B's scenes never reach A's viewer
        stream = CL.CoreClient(url, tok).events("/pulsar/stage/stream?sid=" + sa.sid, timeout=10)
        b_client.post("/pulsar/stage", {"scene": {"title": "B private stream"}})
        a_client.post("/pulsar/stage", {"scene": {"title": "A stream"}})
        for _ in range(5):
            ev, data = next(stream)
            assert ev == "scene" and data["sid"] == sa.sid, data
            if data["scene"]["title"] == "A stream":
                break
        else:
            raise AssertionError("A's scene never arrived")
        stream.close()
        # the global view token still sees every session
        g = _first(CL.CoreClient(url, ST.view_token(ADMIN, "")).events(timeout=10), "tick")
        assert sb.sid in g["sessions"]


# ---------------------------------------------------------------------------
# 6. Bodies
# ---------------------------------------------------------------------------

def test_body_limits_and_bad_json(pulsar_home, monkeypatch):
    sa, ta, _, _ = _two_sessions()
    monkeypatch.setattr(SV, "MAX_BODY", 4096)
    c = TestClient(make_app(ADMIN, 0))
    h = {"authorization": "Bearer " + ta}
    big = {"messages": [{"role": "user", "content": "x" * 8000}]}
    r = c.post("/v1/messages/count_tokens", json=big, headers=h)
    assert r.status_code == 413 and r.json()["error"]["type"] == "request_too_large" and r.json()["type"] == "error"

    def chunks():
        for _ in range(10):
            yield b"[" + b"1," * 300 + b"1]"
    r = c.post("/v1/messages/count_tokens", content=chunks(), headers=h)    # chunked: no content-length
    assert r.status_code == 413
    for path, hdr in (("/v1/messages", h), ("/v1/messages/count_tokens", h), ("/pulsar/stage", h),
                      ("/pulsar/council", h), ("/pulsar/ephemeral-key", {"authorization": "Bearer " + ADMIN})):
        bad = c.post(path, content=b"{not json", headers=dict(hdr, **{"content-type": "application/json"}))
        assert bad.status_code == 400, path
        deep = c.post(path, content=b"[" * 1500 + b"]" * 1500, headers=hdr)
        assert deep.status_code == 400, path
    assert c.post("/pulsar/stage", json=["x"], headers=h).status_code == 400
    assert c.post("/pulsar/council", json={"prompt": "x", "max_tokens": "lots"}, headers=h).status_code == 400


def test_large_bodies_are_parsed_off_the_event_loop(pulsar_home, monkeypatch):
    sa, ta, _, _ = _two_sessions()
    calls = []
    real = SV.asyncio.to_thread

    async def spy(fn, *a, **kw):
        calls.append(getattr(fn, "__name__", str(fn)))
        return await real(fn, *a, **kw)

    monkeypatch.setattr(SV.asyncio, "to_thread", spy)
    c = TestClient(make_app(ADMIN, 0))
    small = c.post("/v1/messages/count_tokens", json={"messages": [{"role": "user", "content": "hi"}]},
                   headers={"authorization": "Bearer " + ta})
    assert small.status_code == 200 and calls == []
    r = c.post("/v1/messages/count_tokens", json={"messages": [{"role": "user", "content": "y" * (2 << 20)}]},
               headers={"authorization": "Bearer " + ta})
    assert r.status_code == 200 and r.json()["input_tokens"] > 0
    assert "loads" in calls and "estimate_tokens" in calls
    if not os.environ.get("PULSAR_MAX_BODY"):
        assert SV.MAX_BODY == 32 * 1024 * 1024


# ---------------------------------------------------------------------------
# 7. Sockets
# ---------------------------------------------------------------------------

def _recv_response(conn: socket.socket, deadline: float = 5.0) -> bytes:
    """One whole HTTP/1.1 response (head plus content-length body): h11 sends head and body in separate
    writes, so a single recv() can return the head alone."""
    buf = b""
    end = time.time() + deadline
    while time.time() < end:
        head, sep, body = buf.partition(b"\r\n\r\n")
        if sep:
            length = 0
            for line in head.split(b"\r\n")[1:]:
                name, _, value = line.partition(b":")
                if name.strip().lower() == b"content-length":
                    length = int(value.strip())
            if len(body) >= length:
                return buf
        chunk = conn.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf


@pytest.mark.parametrize("impl", ["httptools", "h11"])
def test_half_open_connections_are_closed_but_streams_live_on(pulsar_home, impl):
    if impl == "httptools":
        pytest.importorskip("httptools")
    sa, ta, _, _ = _two_sessions()
    port = free_port()
    app = make_app(ADMIN, port)
    proto = SV._http_protocol(0.6, impl)
    assert issubclass(proto, SV._HeaderDeadline) and (impl.lower() in proto.__mro__[2].__name__.lower())
    with live_app(app, port, header_timeout=0.6, impl=impl) as url:
        idle = socket.create_connection(("127.0.0.1", port))
        partial = socket.create_connection(("127.0.0.1", port))
        partial.sendall(b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n" % port)    # never finishes
        for s in (idle, partial):
            s.settimeout(5)
        t0 = time.time()
        assert idle.recv(1024) == b"" and partial.recv(1024) == b""
        assert 0.4 < time.time() - t0 < 4
        # a stream outlives the header deadline many times over
        stream = CL.CoreClient(url, ta).events(timeout=10)
        t0 = time.time()
        ticks = 0
        while time.time() - t0 < 2.5:
            ev, _ = next(stream)
            ticks += ev == "tick"
        assert ticks >= 2
        stream.close()
        # keep-alive reuse still works right after a response
        conn = socket.create_connection(("127.0.0.1", port))
        conn.settimeout(5)
        for _ in range(2):
            conn.sendall(b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n" % port)
            assert b"pulsar-core" in _recv_response(conn)
        conn.close()
        idle.close()
        partial.close()


def test_fd_limit_is_raised(monkeypatch):
    resource = pytest.importorskip("resource")
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
        SV._raise_fd_limit()
        new_soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        assert new_soft == (8192 if hard == resource.RLIM_INFINITY else min(hard, 8192))
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_bind_conflict_exits_with_port_in_use(pulsar_home):
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    port = taken.getsockname()[1]
    env = dict(os.environ, PULSAR_ADMIN_TOKEN=ADMIN)
    try:
        p = subprocess.run(pulsar.python_argv("core", "--port", str(port)), env=env, cwd=pulsar.ROOT,
                           capture_output=True, text=True, timeout=60)
        assert p.returncode == ST.PORT_IN_USE_EXIT and "taken" in p.stderr
        assert ADMIN not in p.stderr + p.stdout
    finally:
        taken.close()


# ---------------------------------------------------------------------------
# 10. Stats never carry a key
# ---------------------------------------------------------------------------

def test_stats_redacts_keys_in_errors_and_events():
    st = Stats()
    st.begin("r", "s1", "m", "p")
    st.end("r", ok=False, error="Invalid API key provided: sk-or-v1-" + "a" * 48)
    st.relay("r2", "p|m", "q|m", "401 Bearer nvapi-" + "b" * 30)
    st.emit("loop", sid="s1", detail="Authorization: Bearer gsk_" + "c" * 30)
    dump = json.dumps(st.snapshot()) + json.dumps(list(st.events))
    assert "a" * 40 not in dump and "b" * 25 not in dump and "c" * 25 not in dump


def test_models_list_drops_control_characters(pulsar_home, monkeypatch):
    K.ensure_home()
    _, tok = ST.new_session([ST.Target("openrouter", "openrouter", "good/model")], {})

    class M:
        def __init__(self, i):
            self.id = i

    import pulsar.catalog as C
    monkeypatch.setattr(C, "read_cache", lambda prof, ttl=0: [M("ok/two"), M("evil\x1b]52;c;aGk=\x07"), M("x\x9bJ")])
    ids = [m["id"] for m in TestClient(make_app(ADMIN, 0)).get("/v1/models", headers={"authorization": "Bearer " + tok}).json()["data"]]
    assert ids == ["good/model", "ok/two"]



# ---------------------------------------------------------------------------
# 11. Load, lifecycle and malformed input
# ---------------------------------------------------------------------------

def _closed(sock, timeout=5.0):
    """True when the peer closes the connection within `timeout` (whatever it sent before that)."""
    sock.settimeout(timeout)
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            if sock.recv(65536) == b"":
                return True
    except ConnectionResetError:
        return True
    except socket.timeout:
        return False
    return False


def test_idle_flood_never_makes_core_refuse_work(pulsar_home):
    # Silent unauthenticated connections used to count against uvicorn's limit_concurrency,
    # so Core answered 503 to everything (/healthz included) and a launch started a second Core.
    sa, ta, _, _ = _two_sessions()
    port = free_port()
    app = make_app(ADMIN, port)
    with live_app(app, port, header_timeout=30, max_conns=16) as url:
        stream = CL.CoreClient(url, ta).events(timeout=10)       # a request in progress (SSE)
        assert _first(stream, "tick") is not None
        flood = [socket.create_connection(("127.0.0.1", port)) for _ in range(48)]
        time.sleep(0.3)
        assert CL.probe(url, ADMIN, timeout=3)[0] == "ok"
        assert CL.CoreClient(url, ta).get("/pulsar/stats")["session"] is not None
        # the oldest silent sockets were closed to stay within the cap; the newest are still open
        assert _closed(flood[0]) and _closed(flood[20])
        flood[-1].settimeout(0.3)
        with pytest.raises(socket.timeout):
            flood[-1].recv(1)
        # the stream that was mid-request when the flood began was never cut
        assert _first(stream, "tick") is not None
        stream.close()
        for f in flood:
            f.close()


def test_server_config_has_no_connection_limit_that_counts_idle_sockets(monkeypatch):
    seen = {}

    class FakeServer:
        should_exit = False

        def __init__(self, config):
            seen["config"] = config

        async def serve(self, sockets=None):
            return None

    import uvicorn
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    monkeypatch.setattr(SV, "_bind", lambda port: socket.socket())
    monkeypatch.setattr(SV.signal, "signal", lambda *a: None)
    monkeypatch.setattr(SV.atexit, "register", lambda fn: None)
    SV.serve(free_port(), ADMIN)
    cfg = seen["config"]
    assert cfg.limit_concurrency is None
    assert issubclass(cfg.http, SV._HeaderDeadline) and cfg.http.max_conns >= 64


def test_inbound_head_deadline_has_its_own_knob(monkeypatch):
    # PULSAR_HEADER_TIMEOUT is the wait for a provider's first byte; raising it for a slow model must never let
    # idle loopback sockets hold Core's connection slots for that long.
    seen = {}

    class FakeServer:
        should_exit = False

        def __init__(self, config):
            seen["config"] = config

        async def serve(self, sockets=None):
            return None

    import uvicorn
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    monkeypatch.setattr(SV, "_bind", lambda port: socket.socket())
    monkeypatch.setattr(SV.signal, "signal", lambda *a: None)
    monkeypatch.setattr(SV.atexit, "register", lambda fn: None)
    monkeypatch.setenv("PULSAR_HEADER_TIMEOUT", "600")
    for raw, want in (("", 15.0), ("30", 30.0), ("0", 15.0), ("-2", 15.0), ("nan", 15.0), ("15s", 15.0)):
        monkeypatch.setenv("PULSAR_CORE_HEADER_TIMEOUT", raw)
        SV.serve(free_port(), ADMIN)
        assert seen["config"].http.header_deadline == want, raw


def test_probe_tells_busy_and_closing_apart_from_impostors(pulsar_home):
    class Busy(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = b'{"error": "overloaded"}'
            self.send_response(503)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    busy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Busy)
    threading.Thread(target=busy.serve_forever, daemon=True).start()
    try:
        assert CL.probe("http://127.0.0.1:%d" % busy.server_address[1], ADMIN, timeout=2)[0] == "busy"
    finally:
        busy.shutdown()
        busy.server_close()
    # a listener that accepts but never answers (or a Core stuck for a moment) is busy, not down
    hang = socket.socket()
    hang.bind(("127.0.0.1", 0))
    hang.listen(8)
    try:
        assert CL.probe("http://127.0.0.1:%d" % hang.getsockname()[1], ADMIN, timeout=0.3)[0] == "busy"
    finally:
        hang.close()
    # a closing Core never proves itself
    app = make_app(ADMIN, 0)
    c = TestClient(app)
    app.state.closing = True
    r = c.get("/healthz", params={"nonce": ST.new_nonce()})
    assert r.status_code == 503 and r.json()["status"] == "closing" and "proof" not in r.json()


def test_attach_waits_out_a_busy_core_instead_of_replacing_it(pulsar_home, monkeypatch):
    K.ensure_home()
    info = ST.CoreInfo(free_port(), os.getpid(), pulsar.__version__, ADMIN, 0)
    ST.write_core(info)
    answers = ["busy", "busy", "busy", "ok"]

    def probe(url, token="", timeout=0.5):
        state = answers.pop(0) if answers else "ok"
        return state, ({"service": "pulsar-core", "version": pulsar.__version__, "build": ST.build_id()}
                       if state == "ok" else None), False

    monkeypatch.setattr(spawn, "probe", probe)
    monkeypatch.setattr(spawn, "_spawn", lambda *a, **k: pytest.fail("started a second Core"))
    assert spawn.ensure_core() == info and answers == []
    # bounded: a Core that stays busy is given up on after `wait`
    answers[:] = ["busy"] * 1000
    t0 = time.time()
    assert spawn.attach(wait=0.5) is None and time.time() - t0 < 2.5
    # a dead pid is never waited for
    ST.write_core(ST.CoreInfo(info.port, 99999999, pulsar.__version__, ADMIN, 0))
    answers[:] = ["busy"] * 1000
    t0 = time.time()
    assert spawn.attach() is None and time.time() - t0 < 1.0


def test_await_core_never_kills_its_own_busy_child(monkeypatch):
    states = iter(["down", "busy", "busy", "busy", "ok"])
    monkeypatch.setattr(spawn, "probe", lambda url, token="", timeout=0.5: (next(states), None, False))

    class P:
        def poll(self):
            return None
    assert spawn._await_core(P(), 1, ADMIN, 10) == "ok"
    monkeypatch.setattr(spawn, "probe", lambda url, token="", timeout=0.5: ("closing", None, False))
    assert spawn._await_core(P(), 1, ADMIN, 10) == "squatted"


@posix_only
def test_a_verified_core_stays_up_for_the_launch_that_verified_it(cores, monkeypatch):
    # A launch attached to a Core in its idle-exit window, which then exited before
    # the session was registered. A proof now counts as activity for a quiet window.
    monkeypatch.setenv("PULSAR_CORE_GRACE", "0")
    monkeypatch.setenv("PULSAR_CORE_QUIET", "2")
    monkeypatch.setenv("PULSAR_CORE_SCAN", "0.2")
    info = spawn.ensure_core()
    cores.append(info)
    for _ in range(8):                                  # 4 s of launches attaching, no session yet
        assert spawn.attach(wait=0) == info
        time.sleep(0.5)
    sess, _ = ST.new_session([ST.Target("openrouter", "openrouter", "m")], {})   # registered late
    time.sleep(1.0)
    assert ST.pid_alive(info.pid) and spawn.verify(info.port, info.admin_token)
    ST.session_path(sess.sid).unlink()
    assert wait_dead(info.pid, 15)                       # nobody left: it does go away
    assert not ST.core_file().exists()


def test_max_tokens_and_field_types_get_400_not_500(pulsar_home):
    sa, ta, _, _ = _two_sessions()
    c = TestClient(make_app(ADMIN, 0), raise_server_exceptions=True)
    h = {"authorization": "Bearer " + ta}
    for mt in ('"abc"', "1e400", "[1]", "true", "-5", "0", "9999999999999999999999", "1.5"):
        for stream in ("false", "true"):
            body = '{"model":"m","max_tokens":%s,"stream":%s,"messages":[{"role":"user","content":"hi"}]}' % (mt, stream)
            r = c.post("/v1/messages", content=body, headers=h)
            assert r.status_code == 400 and r.json()["error"]["type"] == "invalid_request_error", (mt, r.text)
        r = c.post("/v1/messages/count_tokens", content='{"max_tokens":%s,"messages":[]}' % mt, headers=h)
        assert r.status_code == 400, mt
    for field, val in (("messages", "5"), ("tools", "5"), ("temperature", '"hot"'), ("top_p", "[1]"),
                       ("temperature", "1" + "0" * 400), ("top_k", "1e400"),
                       ("stop_sequences", "5"), ("stop_sequences", "[1]"), ("top_k", "true")):
        body = '{"model":"m","max_tokens":5,"messages":[{"role":"user","content":"hi"}],"%s":%s}' % (field, val)
        r = c.post("/v1/messages", content=body, headers=h)
        assert r.status_code == 400 and field in r.json()["error"]["message"], (field, val)
    body = {"max_tokens": 1024.0, "temperature": None}
    assert SV._bad_request(body) == "" and body == {"max_tokens": 1024} and type(body["max_tokens"]) is int
    assert c.post("/v1/messages/count_tokens", json={"max_tokens": 64000, "messages": []}, headers=h).status_code == 200


def test_malformed_knobs_fall_back_to_defaults(monkeypatch):
    for raw in ("15s", "nan", "inf", "-inf", "-3", "", "1e999"):
        monkeypatch.setenv("PULSAR_TEST_KNOB", raw)
        assert SV._knob("PULSAR_TEST_KNOB", 7.0) == 7.0, raw
    monkeypatch.setenv("PULSAR_TEST_KNOB", "2.5")
    assert SV._knob("PULSAR_TEST_KNOB", 7.0) == 2.5
    for raw in ("abc", "99999", "0", "-1", " "):
        monkeypatch.setenv("PULSAR_PORT", raw)
        assert spawn._base_port() == ST.DEFAULT_CORE_PORT, raw
    monkeypatch.setenv("PULSAR_PORT", "5000")
    assert spawn._base_port() == 5000


@posix_only
def test_core_starts_despite_malformed_knobs(cores, monkeypatch):
    for k, v in (("PULSAR_HEADER_TIMEOUT", "15s"), ("PULSAR_CORE_SCAN", "5s"), ("PULSAR_MAX_BODY", "32M"),
                 ("PULSAR_CORE_IDLE", "nan"), ("PULSAR_CORE_GRACE", "soon"), ("PULSAR_MAX_CONNS", "lots"),
                 ("PULSAR_CORE_HEADER_TIMEOUT", "0")):
        monkeypatch.setenv(k, v)
    info = spawn.ensure_core(timeout=15)
    cores.append(info)
    assert spawn.verify(info.port, info.admin_token)


def test_spawn_lock_does_not_wait_where_locks_are_unsupported(pulsar_home, monkeypatch):
    import errno
    K.ensure_home()
    if POSIX:
        import fcntl
        real = fcntl.flock

        def flock(fd, op):
            if op & fcntl.LOCK_EX:
                raise OSError(errno.ENOLCK, "No locks available")
            return real(fd, op)
        monkeypatch.setattr(fcntl, "flock", flock)
    else:
        import msvcrt

        def locking(fd, mode, n):
            raise OSError(errno.EINVAL, "not supported")
        monkeypatch.setattr(msvcrt, "locking", locking)
    t0 = time.time()
    with spawn.spawn_lock(timeout=10) as held:
        assert held is False and time.time() - t0 < 1.0
    fd = os.open(str(ST.run_dir() / "core.lock"), os.O_RDWR)
    try:
        assert spawn._try_lock(fd) is None
    finally:
        os.close(fd)


NOLOCK_RACER = r"""
import errno, fcntl, json, sys, time
sys.path.insert(0, %r)
real = fcntl.flock
def flock(fd, op):
    if op & fcntl.LOCK_EX:
        raise OSError(errno.ENOLCK, "No locks available")
    return real(fd, op)
fcntl.flock = flock
from pulsar.core import spawn
t = float(sys.argv[1])
while time.time() < t:
    time.sleep(0.001)
try:
    info = spawn.ensure_core()
    print(json.dumps({"port": info.port, "pid": info.pid}))
except Exception as e:
    print(json.dumps({"error": "%%s: %%s" %% (type(e).__name__, e)}))
"""


@posix_only
def test_concurrent_launches_without_locks_still_share_one_core(cores):
    go = time.time() + 1.0
    procs = [subprocess.Popen([sys.executable, "-c", NOLOCK_RACER % pulsar.ROOT, str(go)], stdout=subprocess.PIPE,
                              text=True) for _ in range(4)]
    res = [json.loads(p.communicate(timeout=90)[0].strip().splitlines()[-1]) for p in procs]
    assert all("error" not in r for r in res), res
    assert len({(r["port"], r["pid"]) for r in res}) == 1, res


def test_publish_core_never_replaces_a_descriptor(pulsar_home):
    # Launches that could not take the spawn lock publish in one step: the first wins and the second is told so
    # (a check followed by an ordinary write let two launches each keep a Core of their own).
    K.ensure_home()
    first = ST.CoreInfo(4747, 111, pulsar.__version__, "pla_a", 1.0)
    assert ST.publish_core(first) is True
    assert ST.publish_core(ST.CoreInfo(4748, 222, pulsar.__version__, "pla_b", 2.0)) is False
    assert ST.read_core() == first
    assert [p.name for p in ST.run_dir().iterdir() if p.name.startswith(".core.json.")] == []   # no temp file left
    if os.name != "nt":
        assert os.stat(str(ST.core_file())).st_mode & 0o777 == 0o600


def test_viewer_counts_are_scoped_to_the_callers_session(pulsar_home):
    # Another session's viewer made A's MCP server think A's stage was on screen.
    sa, ta, sb, tb = _two_sessions()
    port = free_port()
    app = make_app(ADMIN, port)
    with live_app(app, port) as url:
        a, b, admin = CL.CoreClient(url, ta), CL.CoreClient(url, tb), CL.CoreClient(url, ADMIN)
        assert a.get("/pulsar/stage/viewer")["viewers"] == 0
        vb = b.get("/pulsar/stage/viewer")["url"].split("?", 1)[1]
        stream = OPENER.open(url + "/pulsar/stage/stream?" + vb, timeout=10)          # B's own viewer
        deadline = time.time() + 5
        while admin.get("/pulsar/stage/viewer")["viewers"] < 1 and time.time() < deadline:
            time.sleep(0.05)
        assert admin.get("/pulsar/stage/viewer")["viewers"] == 1
        assert a.get("/pulsar/stage/viewer")["viewers"] == 0
        assert a.post("/pulsar/stage", {"scene": {"title": "A"}})["viewers"] == 0
        assert b.get("/pulsar/stage/viewer")["viewers"] == 1
        assert b.post("/pulsar/stage", {"scene": {"title": "B"}})["viewers"] == 1
        stream.close()
        # a global viewer shows every session's scenes, so it counts for each of them
        g = OPENER.open(url + "/pulsar/stage/stream?t=" + ST.view_token(ADMIN, ""), timeout=10)
        deadline = time.time() + 5
        while a.get("/pulsar/stage/viewer")["viewers"] < 1 and time.time() < deadline:
            time.sleep(0.05)
        assert a.get("/pulsar/stage/viewer")["viewers"] == 1
        g.close()


def test_admin_client_health_needs_the_proof(pulsar_home):
    # With a stale core.json, an impostor answering /healthz looked "healthy" to the
    # admin client, and `pulsarcode stage --web` opened the impostor's port.
    port = free_port()
    fake = Fake(port)
    try:
        assert not CL.CoreClient("http://127.0.0.1:%d" % port, ADMIN).healthy()
        assert CL.CoreClient("http://127.0.0.1:%d" % port, "pls_x").healthy()     # display-only check
        K.ensure_home()
        ST.write_core(ST.CoreInfo(port, 424242, pulsar.__version__, ADMIN, 0))
        from pulsar.stage import term
        assert term.web_url() is None
        assert fake.tokens_seen() == []
    finally:
        fake.stop()
    c = TestClient(make_app(ADMIN, 0))
    nonce = ST.new_nonce()
    assert ST.proof_ok(ADMIN, nonce, c.get("/healthz", params={"nonce": nonce}).json()["proof"])


def test_unauthenticated_oversized_request_is_answered_at_once(pulsar_home):
    sa, ta, _, _ = _two_sessions()
    port = free_port()
    app = make_app(ADMIN, port)
    with live_app(app, port):
        for auth, most in ((b"", 1.0), (b"Authorization: Bearer pls_unknown\r\n", 1.0)):
            s = socket.create_connection(("127.0.0.1", port))
            s.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n%sContent-Length: 100000000\r\n\r\n"
                      % (port, auth))
            s.settimeout(10)
            t0 = time.time()
            got = s.recv(200)
            assert got.startswith(b"HTTP/1.1 413") and time.time() - t0 < most
            assert _closed(s)                                # and hung up: no body is waited for
            s.close()
        # a known session still gets its body drained (bounded) so it can read the 413
        s = socket.create_connection(("127.0.0.1", port))
        s.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nAuthorization: Bearer %s\r\n"
                  b"Content-Length: 100000000\r\n\r\n" % (port, ta.encode()))
        s.settimeout(10)
        t0 = time.time()
        got = s.recv(200)
        assert got.startswith(b"HTTP/1.1 413") and SV.DRAIN_SECONDS - 0.5 < time.time() - t0 < SV.DRAIN_SECONDS + 3
        s.close()
