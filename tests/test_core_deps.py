# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Core's dependency diet (plain Starlette), session/key hot swap, the Stage pane route, Core's
environment and working directory, and build ids (an update never keeps old code serving launches).

Some tests start real Core processes on loopback; each one is stopped through its own admin token in a
finalizer, never by a process number.
"""
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import pulsar
from pulsar import config as K
from pulsar.core import server as SV
from pulsar.core import spawn
from pulsar.core import state as ST
from pulsar.core.server import make_app
from pulsar.stage import pane as PANE

ROOT = Path(__file__).resolve().parent.parent
ADMIN = ST.ADMIN_TOKEN_PREFIX + "admin-deps-test"
POSIX = os.name != "nt"
posix_only = pytest.mark.skipif(not POSIX, reason="POSIX signals and process semantics")


def _target(model="model-a"):
    return ST.Target("openrouter", "openrouter", model)


def _session(model="model-a"):
    K.ensure_home()
    return ST.new_session([_target(model)], {})


def _bearer(tok):
    return {"authorization": "Bearer " + tok}


def _requirements():
    out = []
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


# ---------------------------------------------------------------------------
# 1. Dependency diet
# ---------------------------------------------------------------------------

def test_core_server_imports_neither_fastapi_nor_pydantic():
    code = ("import sys; sys.path.insert(0, %r); import pulsar.core.server, pulsar.core.spawn; "
            "print(sorted(m for m in ('fastapi', 'pydantic', 'pydantic_core') if m in sys.modules))" % str(ROOT))
    out = subprocess.run([sys.executable, "-E", "-B", "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


def test_requirements_are_pure_python_and_cover_socks_and_the_os_trust_store():
    reqs = _requirements()
    names = [re.split(r"[\[<>=!~;\s]", r, maxsplit=1)[0].lower() for r in reqs]
    assert sorted(names) == ["httpx", "starlette", "truststore", "uvicorn"], reqs
    joined = " ".join(reqs).lower()
    for gone in ("fastapi", "pydantic", "uvicorn[standard]", "httptools", "uvloop", "websockets"):
        assert gone not in joined, gone
    assert any(r.replace(" ", "").startswith("httpx[socks]") for r in reqs)
    trust = next(r for r in reqs if r.startswith("truststore"))
    assert re.search(r"python_version\s*>=\s*'3\.10'", trust)       # truststore needs 3.10; 3.9 still installs
    starlette_req = next(r for r in reqs if r.startswith("starlette"))
    assert re.search(r">=0\.(\d+)", starlette_req) and int(re.search(r">=0\.(\d+)", starlette_req).group(1)) >= 40


def test_routing_answers_like_the_fastapi_core_did(pulsar_home):
    """The Starlette port keeps every status code and error shape, including the corner cases."""
    _, tok = _session()
    c = TestClient(make_app(ADMIN, 0))
    head = c.head("/healthz")                                     # GET-only routes never took HEAD
    assert head.status_code == 404 and head.headers["content-type"] == "application/json"
    assert c.head("/api/hello").status_code == 200
    r = c.options("/healthz")
    assert r.status_code == 405 and r.json() == {"detail": "Method Not Allowed"} and r.headers["allow"] == "GET"
    r = c.request("TRACE", "/anything")
    assert r.status_code == 405 and set(r.headers["allow"].split(", ")) == {"GET", "POST", "PUT", "DELETE", "HEAD", "PATCH"}
    r = c.post("/healthz")
    assert r.status_code == 404 and r.json() == {"type": "error", "error": {"type": "not_found_error",
                                                                           "message": "Pulsar Core does not serve /healthz"}}
    assert c.get("/v1/messages", headers=_bearer(tok)).status_code == 404
    assert c.get("/x/" + "a" * 400).json()["error"]["message"] == "Pulsar Core does not serve /x/" + "a" * 198
    assert c.get("/api/hello").json() == {"ok": True}
    assert c.post("/v1/messages", json={}).json()["error"]["type"] == "authentication_error"


def test_guard_still_fronts_every_route(pulsar_home):
    port = 4799
    c = TestClient(make_app(ADMIN, port), base_url="http://127.0.0.1:%d" % port)
    assert c.get("/pulsar/stage/raster", headers={"host": "evil.example:%d" % port}).status_code == 421
    assert c.get("/healthz", headers={"origin": "http://evil.example"}).status_code == 403
    big = c.post("/v1/messages", headers={"content-length": str(1 << 40)}, content=b"")
    assert big.status_code == 413 and big.headers.get("connection") == "close"


def test_deep_json_is_refused_the_same_on_every_python(pulsar_home):
    _, tok = _session()
    c = TestClient(make_app(ADMIN, 0))
    n = SV.MAX_DEPTH + 1
    deep = {"messages": [{"role": "user", "content": "hi"}], "x": json.loads("[" * (n - 1) + "]" * (n - 1))}
    raw = json.dumps(deep).encode()
    for path in ("/v1/messages", "/v1/messages/count_tokens", "/pulsar/stage", "/pulsar/council"):
        r = c.post(path, content=raw, headers=dict(_bearer(tok), **{"content-type": "application/json"}))
        assert r.status_code == 400 and "deeper than" in r.json()["error"]["message"], path
    ok = {"messages": [{"role": "user", "content": "hi"}], "x": json.loads("[" * 100 + "]" * 100)}
    assert c.post("/v1/messages/count_tokens", json=ok, headers=_bearer(tok)).status_code == 200
    assert SV._nested_ok([[[]]], 3) and not SV._nested_ok([[[[]]]], 3)
    assert SV._nested_ok({"a": [{"b": 1}]}, 3) and not SV._nested_ok({"a": [{"b": {}}]}, 3)


# ---------------------------------------------------------------------------
# 2. Hot swap within one timestamp tick
# ---------------------------------------------------------------------------

def _rewrite_in_place(path: Path, old: str, new: str) -> None:
    """What a coarse filesystem shows after a second write in the same tick: same mtime, same size, same
    inode (a reused one, or an in-place write); only the content differs."""
    st = os.stat(str(path))
    data = path.read_bytes()
    assert old.encode() in data and len(old) == len(new)
    with open(str(path), "r+b") as fh:
        fh.write(data.replace(old.encode(), new.encode()))
    os.utime(str(path), ns=(st.st_atime_ns, st.st_mtime_ns))
    after = os.stat(str(path))
    assert (after.st_mtime_ns, after.st_size, after.st_ino) == (st.st_mtime_ns, st.st_size, st.st_ino)


def test_switch_within_one_mtime_tick_is_seen(pulsar_home):
    sess, tok = _session("model-a")
    sessions = SV.Sessions()
    assert sessions.lookup(tok).primary.model == "model-a"
    _rewrite_in_place(ST.session_path(sess.sid), "model-a", "model-b")     # switch 1
    assert sessions.lookup(tok).primary.model == "model-b"
    _rewrite_in_place(ST.session_path(sess.sid), "model-b", "model-c")     # switch 2, same tick again
    assert sessions.lookup(tok).primary.model == "model-c"
    # through the gateway too: /v1/models lists the plan's model first
    c = TestClient(make_app(ADMIN, 0))
    assert c.get("/v1/models", headers=_bearer(tok)).json()["data"][0]["id"] == "model-c"
    _rewrite_in_place(ST.session_path(sess.sid), "model-c", "model-d")
    assert c.get("/v1/models", headers=_bearer(tok)).json()["data"][0]["id"] == "model-d"


def test_settled_session_files_are_not_reread(pulsar_home, monkeypatch):
    sess, tok = _session("model-a")
    path = ST.session_path(sess.sid)
    old = time.time() - 60
    os.utime(str(path), (old, old))
    sessions = SV.Sessions()
    assert sessions.lookup(tok) is not None
    reads = []
    real = SV.Sessions._load
    monkeypatch.setattr(SV.Sessions, "_load", staticmethod(lambda p: reads.append(p) or real(p)))
    for _ in range(5):
        assert sessions.lookup(tok).primary.model == "model-a"
    assert reads == []                                   # a settled, unchanged file costs one stat
    ST.update_plan(sess.sid, [_target("model-z")])      # a real write: new mtime and inode
    assert sessions.lookup(tok).primary.model == "model-z" and len(reads) == 1


def test_revoked_or_foreign_session_files_are_refused(pulsar_home):
    sess, tok = _session()
    other, _ = _session()
    sessions = SV.Sessions()
    assert sessions.lookup(tok) is not None and sessions.served(sess.sid) and not sessions.served(other.sid)
    data = json.loads(ST.session_path(sess.sid).read_text())
    data["token_sha256"] = "0" * 64                      # the file now belongs to another token
    ST.session_path(sess.sid).write_text(json.dumps(data))
    assert sessions.lookup(tok) is None
    ST.save_session(sess)
    assert sessions.lookup(tok) is not None
    ST.session_path(sess.sid).unlink()
    assert sessions.lookup(tok) is None
    assert sessions.lookup("pls_unknown") is None and sessions.lookup("nope") is None


def test_key_rotation_within_one_mtime_tick_is_seen(pulsar_home):
    K.ensure_home()
    K.set_key("groq", "fake-groq-ENVONLY-not-a-key-one")
    keys = SV.Keys()
    assert keys.get("groq") == "fake-groq-ENVONLY-not-a-key-one"
    _rewrite_in_place(K.keys_path(), "key-one", "key-two")
    assert keys.get("groq") == "fake-groq-ENVONLY-not-a-key-two"
    K.keys_path().unlink()
    assert keys.get("groq") == ""
    K.set_key("groq", "fake-groq-ENVONLY-not-a-key-new")
    assert keys.get("groq") == "fake-groq-ENVONLY-not-a-key-new"


# ---------------------------------------------------------------------------
# 3. Stage pane route
# ---------------------------------------------------------------------------

def _stage_app():
    sa, ta = _session()
    sb, tb = _session()
    port = 4798
    app = make_app(ADMIN, port)
    c = TestClient(app, base_url="http://127.0.0.1:%d" % port)
    return app, c, (sa, ta), (sb, tb)


def test_raster_route_is_scoped_like_scenes(pulsar_home):
    app, c, (sa, ta), (sb, tb) = _stage_app()
    assert c.get("/pulsar/stage/raster").status_code == 401
    assert c.get("/pulsar/stage/raster?t=plv_forged").status_code == 401
    empty = c.get("/pulsar/stage/raster", headers=_bearer(ta))
    assert empty.status_code == 204 and empty.content == b"" and empty.headers["cache-control"] == "no-store"
    assert c.post("/pulsar/stage", json={"scene": {"kind": "card", "id": "a1", "title": "A", "body": "alpha"}},
                  headers=_bearer(ta)).status_code == 200
    a = c.get("/pulsar/stage/raster?cols=40&rows=10", headers=_bearer(ta))
    assert a.status_code == 200 and a.headers["cache-control"] == "no-store"
    body = a.json()
    assert body["id"] == "a1" and body["cols"] == 40 and body["rows"] == 10 and body["cells"] and body["count"] == 1
    # B sees nothing of A's, even when it asks for A's sid or scene id
    for q in ("", "?sid=" + sa.sid, "?id=a1", "?sid=%s&id=a1" % sa.sid, "?idle=0&meta=1"):
        r = c.get("/pulsar/stage/raster" + q, headers=_bearer(tb))
        assert r.status_code == 204 and r.content == b"", q
    # view tokens: their own sid only; the global one and the admin token see every session
    va, vb = ST.view_token(ADMIN, sa.sid), ST.view_token(ADMIN, sb.sid)
    assert c.get("/pulsar/stage/raster?t=%s&sid=%s" % (va, sa.sid)).json()["id"] == "a1"
    assert c.get("/pulsar/stage/raster?t=%s&sid=%s" % (vb, sb.sid)).status_code == 204
    assert c.get("/pulsar/stage/raster?t=%s&sid=%s" % (va, sb.sid)).status_code == 401
    assert c.get("/pulsar/stage/raster?t=" + ST.view_token(ADMIN, "")).json()["id"] == "a1"
    assert c.get("/pulsar/stage/raster", headers=_bearer(ADMIN)).json()["id"] == "a1"
    assert c.get("/pulsar/stage/raster?sid=" + sb.sid, headers=_bearer(ADMIN)).status_code == 204
    # meta: the cheap version check, no cells
    meta = c.get("/pulsar/stage/raster?meta=1", headers=_bearer(ta)).json()
    assert "cells" not in meta and meta["id"] == "a1" and meta["count"] == 1
    # the idle screen when asked for, still scoped
    idle = c.get("/pulsar/stage/raster?idle=1&cols=30&rows=8", headers=_bearer(tb))
    assert idle.status_code == 200 and idle.json()["idle"] is True
    # cleared: nothing to show again
    c.post("/pulsar/stage", json={"op": "clear"}, headers=_bearer(ta))
    assert c.get("/pulsar/stage/raster", headers=_bearer(ta)).status_code == 204


def test_raster_query_is_clamped(pulsar_home):
    app, c, (sa, ta), _ = _stage_app()
    c.post("/pulsar/stage", json={"scene": {"kind": "deck", "id": "d", "title": "D",
                                            "slides": [{"title": "one"}, {"title": "two"}]}}, headers=_bearer(ta))
    for q, cols, rows in (("cols=99999&rows=99999", 512, 256), ("cols=-3&rows=0", 1, 1), ("cols=nan&rows=inf", 80, 24),
                          ("cols=1e400&rows=abc", 80, 24), ("cols=12.9&rows=3", 12, 3)):
        r = c.get("/pulsar/stage/raster?" + q, headers=_bearer(ta))
        assert r.status_code == 200, q
        b = r.json()
        assert (b["cols"], b["rows"]) == (cols, rows) and b["cells"], q
    assert c.get("/pulsar/stage/raster?slide=99&meta=1", headers=_bearer(ta)).json()["slide"] == 1
    assert c.get("/pulsar/stage/raster?slide=-9&meta=1", headers=_bearer(ta)).json()["slide"] == 0
    assert c.get("/pulsar/stage/raster?id=" + "x" * 5000, headers=_bearer(ta)).status_code == 204


def test_raster_frames_are_drawn_off_the_event_loop(pulsar_home, monkeypatch):
    app, c, (sa, ta), _ = _stage_app()
    c.post("/pulsar/stage", json={"scene": {"kind": "card", "id": "a", "body": "x"}}, headers=_bearer(ta))
    seen = []
    real = PANE.raster_route

    def spy(items, query):
        seen.append(threading.current_thread().name)
        return real(items, query)
    monkeypatch.setattr(PANE, "raster_route", spy)
    assert c.get("/pulsar/stage/raster?cols=512&rows=256", headers=_bearer(ta)).status_code == 200
    assert c.get("/pulsar/stage/raster?meta=1", headers=_bearer(ta)).status_code == 200
    assert seen[0].startswith("pulsar-raster") and not seen[1].startswith("pulsar-raster")


def test_a_polling_pane_counts_as_a_viewer_of_its_session_only(pulsar_home):
    app, c, (sa, ta), (sb, tb) = _stage_app()
    pane = dict(_bearer(ta), **{"X-Pulsar-Viewer": "pane"})

    def viewer(tok):
        return c.get("/pulsar/stage/viewer", headers=_bearer(tok)).json()

    assert viewer(ta)["viewers"] == 0 and viewer(ta)["pane"] is False
    assert c.get("/pulsar/stage/raster?meta=1", headers=pane).status_code == 204   # empty stage, still a poll
    assert viewer(ta)["viewers"] == 1 and viewer(ta)["pane"] is True
    assert viewer(tb)["viewers"] == 0 and viewer(tb)["pane"] is False            # never another session's
    assert viewer(ADMIN)["viewers"] == 1                                         # the global count includes it
    posted = c.post("/pulsar/stage", json={"scene": {"kind": "card", "body": "x"}}, headers=_bearer(ta)).json()
    assert posted["viewers"] == 1 and posted["pane"] is True
    posted_b = c.post("/pulsar/stage", json={"scene": {"kind": "card", "body": "y"}}, headers=_bearer(tb)).json()
    assert posted_b["viewers"] == 0 and posted_b["pane"] is False
    # it lapses ~3 s after the last poll
    assert app.state.panes.ttl == 3.0
    app.state.panes.ttl = 0.05
    time.sleep(0.15)
    assert viewer(ta)["viewers"] == 0 and viewer(ADMIN)["viewers"] == 0
    app.state.panes.ttl = 3.0
    # pane-auto (will dock itself for the next scene) counts; pane-standby and junk do not
    c.get("/pulsar/stage/raster?meta=1", headers=dict(_bearer(tb), **{"X-Pulsar-Viewer": "pane-auto"}))
    assert viewer(tb)["viewers"] == 1
    for state in ("pane-standby", "browser", ""):
        _, tok = _session()
        c.get("/pulsar/stage/raster?meta=1", headers=dict(_bearer(tok), **{"X-Pulsar-Viewer": state}))
        assert viewer(tok)["viewers"] == 0, state


def test_only_session_tokens_can_claim_a_pane(pulsar_home):
    app, c, (sa, ta), _ = _stage_app()
    hdr = {"X-Pulsar-Viewer": "pane"}
    c.get("/pulsar/stage/raster?t=%s&sid=%s" % (ST.view_token(ADMIN, sa.sid), sa.sid), headers=hdr)
    c.get("/pulsar/stage/raster?sid=" + sa.sid, headers=dict(_bearer(ADMIN), **hdr))
    assert c.get("/pulsar/stage/viewer", headers=_bearer(ta)).json()["viewers"] == 0


def test_mcp_server_does_not_open_a_split_while_a_pane_shows(pulsar_home, monkeypatch):
    from pulsar.mcp import server as MCP
    app, c, (sa, ta), _ = _stage_app()

    class Via:
        """CoreClient over the TestClient."""

        def get(self, path, timeout=5):
            return c.get(path, headers=_bearer(ta)).json()

    opened = []
    monkeypatch.setattr("pulsar.stage.launcher.open_viewer", lambda url, sid: opened.append(url) or "split")
    srv = MCP.Server.__new__(MCP.Server)
    srv.viewer_opened = False
    c.get("/pulsar/stage/raster?meta=1", headers=dict(_bearer(ta), **{"X-Pulsar-Viewer": "pane"}))
    assert srv._maybe_open_viewer(Via()) == "already on screen" and opened == []
    srv.viewer_opened = False
    app.state.panes.ttl = 0.0                                            # the pane went away
    time.sleep(0.01)
    assert srv._maybe_open_viewer(Via()) == "split" and len(opened) == 1


# ---------------------------------------------------------------------------
# 4. Build ids, retirement
# ---------------------------------------------------------------------------

def test_build_id_fingerprints_python_sources_only(tmp_path, monkeypatch):
    pkg = tmp_path / "pulsar"
    (pkg / "core").mkdir(parents=True)
    (pkg / "__pycache__").mkdir()
    (pkg / "__init__.py").write_text("x = 1\n")
    (pkg / "core" / "a.py").write_text("y = 2\n")
    monkeypatch.setattr(ST, "_PACKAGE", str(pkg))
    monkeypatch.setattr(ST, "_build", None)
    first = ST.build_id()
    assert re.fullmatch(r"[0-9a-f]{20}", first) and ST.build_id() == first
    (pkg / "__pycache__" / "a.cpython-39.pyc").write_bytes(b"junk")
    (pkg / "notes.txt").write_text("not code")
    assert ST.build_id(refresh=True) == first
    os.utime(str(pkg / "core" / "a.py"), ns=(1, 1_000_000_000))       # touched, same bytes: same code
    assert ST.build_id(refresh=True) == first
    (pkg / "core" / "a.py").write_text("y = 3\n")                       # an update: same size, new bytes
    os.utime(str(pkg / "core" / "a.py"), ns=(1, 1_000_000_000))       # even with the old mtime restored
    second = ST.build_id()
    assert second == first                                          # cached until asked to look again
    assert ST.build_id(refresh=True) != first
    (pkg / "core" / "b.py").write_text("")
    assert ST.build_id(refresh=True) not in (first, second)


def test_identical_installs_share_a_build_id(tmp_path, monkeypatch):
    # Two copies of the same release (install.sh's app folder and a pipx install sharing ~/.pulsarcode)
    # used to differ only in mtimes, so each launch retired the other's Core and started its own.
    ids = []
    for n, name in enumerate(("srcA", "srcB")):
        pkg = tmp_path / name / "pulsar"
        (pkg / "core").mkdir(parents=True)
        (pkg / "__init__.py").write_text("x = 1\n")
        (pkg / "core" / "a.py").write_text("y = 2\n")
        os.utime(str(pkg / "core" / "a.py"), ns=(n + 7, (n + 7) * 1_000_000_000))
        monkeypatch.setattr(ST, "_PACKAGE", str(pkg))
        ids.append(ST.build_id(refresh=True))
    assert ids[0] == ids[1]


def test_build_id_rehashes_only_changed_files(tmp_path, monkeypatch):
    pkg = tmp_path / "pulsar"
    pkg.mkdir()
    for i in range(3):
        (pkg / ("m%d.py" % i)).write_text("v = %d\n" % i)
    monkeypatch.setattr(ST, "_PACKAGE", str(pkg))
    ST.build_id(refresh=True)
    opened = []
    real_open = open

    def spy(path, *a, **kw):
        opened.append(os.path.basename(str(path)))
        return real_open(path, *a, **kw)

    import builtins
    monkeypatch.setattr(builtins, "open", spy)
    ST.build_id(refresh=True)
    assert opened == []
    (pkg / "m1.py").write_text("v = 11\n")
    ST.build_id(refresh=True)
    assert opened == ["m1.py"]


def test_proven_health_names_the_build_and_retirement(pulsar_home):
    app = make_app(ADMIN, 0)
    c = TestClient(app)
    assert "build" not in c.get("/healthz").json()                      # public: nothing to fingerprint
    nonce = ST.new_nonce()
    proved = c.get("/healthz", params={"nonce": nonce}).json()
    assert ST.proof_ok(ADMIN, nonce, proved["proof"]) and proved["build"] == ST.build_id() == app.state.build
    assert "retiring" not in proved
    sess, tok = _session()
    assert c.post("/pulsar/retire", headers=_bearer(tok)).status_code == 401
    r = c.post("/pulsar/retire", headers=_bearer(ADMIN)).json()
    assert r == {"ok": True, "retiring": True, "sessions": 1}            # its session still needs it
    assert app.state.shutdown_requested is False
    assert c.get("/healthz", params={"nonce": ST.new_nonce()}).json()["retiring"] is True
    # a session launched later against the successor is not ours; one that calls us is
    later, ltok = _session()
    assert [s.sid for s in app.state.ours(ST.live_sessions(reap=False))] == [sess.sid]
    c.get("/v1/models", headers=_bearer(ltok))
    assert sorted(s.sid for s in app.state.ours(ST.live_sessions(reap=False))) == sorted([sess.sid, later.sid])


def test_retire_with_no_sessions_exits_at_once(pulsar_home):
    K.ensure_home()
    app = make_app(ADMIN, 0)
    c = TestClient(app)
    assert c.post("/pulsar/retire", headers=_bearer(ADMIN)).json()["sessions"] == 0
    assert app.state.shutdown_requested is True
    assert c.get("/healthz", params={"nonce": ST.new_nonce()}).status_code == 503


def test_attach_treats_other_builds_versions_and_retiring_cores_as_stale(pulsar_home, monkeypatch):
    K.ensure_home()
    info = ST.CoreInfo(4790, os.getpid(), pulsar.__version__, ADMIN, 1.0)
    ST.write_core(info)
    reply = {}
    monkeypatch.setattr(spawn, "probe", lambda url, token="", timeout=0.5: ("ok", dict(reply), False))
    reply.update(service="pulsar-core", version=pulsar.__version__, build=ST.build_id(refresh=True))
    assert spawn.attach() == info and spawn._attach() == (info, None)
    for change in ({"build": "0" * 20}, {"build": None}, {"version": "0.0.1"}, {"retiring": True}):
        reply.clear()
        reply.update(service="pulsar-core", version=pulsar.__version__, build=ST.build_id())
        reply.update(change)
        if reply["build"] is None:
            del reply["build"]                                           # a Core from before build ids
        assert spawn.attach() is None and spawn._attach() == (None, info), change


def test_a_busy_core_of_another_version_is_not_waited_for(pulsar_home, monkeypatch):
    K.ensure_home()
    ST.write_core(ST.CoreInfo(4790, os.getpid(), "0.0.1", ADMIN, 1.0))
    probes = []
    monkeypatch.setattr(spawn, "probe", lambda url, token="", timeout=0.5: probes.append(url) or ("busy", None, False))
    t0 = time.time()
    assert spawn._attach() == (None, None) and len(probes) == 1 and time.time() - t0 < 1.0


def test_ensure_core_retires_a_stale_core_and_starts_the_current_code(pulsar_home, monkeypatch):
    K.ensure_home()
    stale = ST.CoreInfo(4790, os.getpid(), pulsar.__version__, ADMIN, 1.0)
    ST.write_core(stale)
    monkeypatch.setattr(spawn, "probe", lambda url, token="", timeout=0.5:
                        ("ok", {"service": "pulsar-core", "version": pulsar.__version__, "build": "old"}, False))
    calls = []
    fresh = ST.CoreInfo(4791, 4242, pulsar.__version__, ADMIN, 2.0)
    monkeypatch.setattr(spawn, "_retire", lambda info: calls.append(("retire", info)))
    monkeypatch.setattr(spawn, "_spawn", lambda timeout, prefer, locked=True: calls.append(("spawn", prefer)) or fresh)
    assert spawn.ensure_core() == fresh
    assert calls == [("retire", stale), ("spawn", 0)]


def test_retire_never_stops_an_old_core_that_still_serves_sessions(pulsar_home, monkeypatch):
    """A Core from before /pulsar/retire answers 404: stop it only when no session is live at all."""
    from pulsar.core import client as CL
    K.ensure_home()
    posts = []

    def post(self, path, body, timeout=30.0):
        posts.append(path)
        if path == "/pulsar/retire":
            raise CL.CoreUnavailable("HTTP 404", status=404)
        return {"ok": True}
    monkeypatch.setattr(CL.CoreClient, "post", post)
    old = ST.CoreInfo(4790, 99999999, pulsar.__version__, ADMIN, 1.0)
    sess, _ = _session()
    spawn._retire(old, wait=0)
    assert posts == ["/pulsar/retire"]                                   # a live session: leave it running
    ST.session_path(sess.sid).unlink()
    spawn._retire(old, wait=0)
    assert posts == ["/pulsar/retire", "/pulsar/retire", "/pulsar/shutdown"]


def test_stop_never_empties_the_port_of_live_sessions(pulsar_home, monkeypatch):
    """`pulsarcode stop` retires Core: it keeps its port for the sessions still using it (whose Claude Code
    would otherwise send its next request to whatever takes the port) and exits with the last one."""
    from pulsar.core import client as CL
    K.ensure_home()
    ST.write_core(ST.CoreInfo(4790, os.getpid(), pulsar.__version__, ADMIN, 1.0))
    monkeypatch.setattr(spawn, "probe", lambda url, token="", timeout=0.5: ("ok", {"service": "pulsar-core"}, False))
    posts = []
    monkeypatch.setattr(CL.CoreClient, "post", lambda self, path, body, timeout=30.0: posts.append(path) or {"ok": True})
    assert spawn.stop_core() is True and posts == ["/pulsar/retire"]
    assert spawn.stop_core(force=True) is True and posts[-1] == "/pulsar/shutdown"
    # what the retire request does on the Core's side
    app = make_app(ADMIN, 0)
    c = TestClient(app)
    sess, _ = _session()
    assert c.post("/pulsar/retire", headers=_bearer(ADMIN)).json()["sessions"] == 1
    assert app.state.shutdown_requested is False                     # still serving its session
    ST.session_path(sess.sid).unlink()
    assert c.post("/pulsar/retire", headers=_bearer(ADMIN)).json()["sessions"] == 0
    assert app.state.shutdown_requested is True                      # with none left, at once


# ---------------------------------------------------------------------------
# 5. Core's environment and working directory
# ---------------------------------------------------------------------------

def test_core_env_bypasses_proxies_for_loopback(pulsar_home, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("NO_PROXY", "corp.example, localhost")
    monkeypatch.setenv("no_proxy", "intranet.example")
    monkeypatch.delenv("PULSAR_PROXY_LOCAL", raising=False)
    env = spawn._child_env("pla_x")
    hosts = env["NO_PROXY"].split(",")
    assert hosts == ["corp.example", "localhost", "intranet.example", "127.0.0.1", "::1"]
    if POSIX:
        assert env["no_proxy"] == env["NO_PROXY"]
    assert env["HTTPS_PROXY"] == "http://proxy.example:3128"            # everything else still goes through it
    monkeypatch.delenv("NO_PROXY")
    monkeypatch.delenv("no_proxy")
    assert spawn._child_env("pla_x")["NO_PROXY"] == "127.0.0.1,localhost,::1"
    monkeypatch.setenv("PULSAR_PROXY_LOCAL", "1")                       # the explicit opt-in is respected
    assert "NO_PROXY" not in spawn._child_env("pla_x")


def test_core_env_leaves_out_proxies_httpx_cannot_use(pulsar_home, monkeypatch, capsys):
    monkeypatch.setenv("ALL_PROXY", "socks4://fakeuser:fake-pw-not-real@127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.delenv("all_proxy", raising=False)
    env = spawn._child_env("pla_x")
    assert "ALL_PROXY" not in env and env["HTTPS_PROXY"] == "http://127.0.0.1:9"
    err = capsys.readouterr().err
    assert "ALL_PROXY" in err and "socks4://" in err and "fake-pw" not in err and "fakeuser" not in err
    for ok in ("socks5://127.0.0.1:9", "socks5h://127.0.0.1:9", "http://127.0.0.1:9", "127.0.0.1:9"):
        monkeypatch.setenv("ALL_PROXY", ok)
        assert spawn._child_env("pla_x")["ALL_PROXY"] == ok
    assert ST.proxy_scheme("user:pw@weird") == "http" and ST.proxy_scheme("x y://h") == "unknown"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("proxy", ["socks4://127.0.0.1:9", "socks4a://127.0.0.1:9", "ftp://127.0.0.1:9"])
def test_unsupported_proxy_scheme_never_turns_messages_into_500(pulsar_home, monkeypatch, capsys, proxy, stream):
    # httpx.AsyncClient(trust_env=True) raises ValueError for these schemes; every /v1/messages was a bare 500.
    for var in ST.PROXY_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ALL_PROXY", proxy)
    monkeypatch.setenv("NO_PROXY", "")
    K.ensure_home()
    ST.sessions_dir().mkdir(parents=True, exist_ok=True)
    target = ST.Target(profile="custom-x", provider="custom", model="m", base_url="https://api.example.invalid/v1")
    _, tok = ST.new_session([target], {})
    c = TestClient(make_app(ADMIN, 0), raise_server_exceptions=False)
    c.post("/pulsar/ephemeral-key", json={"profile": "custom-x", "key": "fake-x-ENVONLY-not-a-key"},
           headers=_bearer(ADMIN))
    r = c.post("/v1/messages", headers=_bearer(tok),
               json={"model": "m", "max_tokens": 16, "stream": stream, "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text[:300]
    assert "Could not reach api.example.invalid" in (r.text if stream else json.dumps(r.json()))
    assert os.environ["ALL_PROXY"] == proxy                     # hidden only while the client was built
    assert proxy not in capsys.readouterr().err                 # the engine or the wrapper copes; never echoes it


def test_core_runs_in_the_pulsar_home_with_absolute_paths(pulsar_home, monkeypatch, tmp_path):
    work = tmp_path / "project"
    (work / "certs").mkdir(parents=True)
    (work / "certs" / "ca.pem").write_text("")
    monkeypatch.chdir(str(work))
    monkeypatch.setenv("PULSAR_HOME", "relhome")
    monkeypatch.setenv("SSL_CERT_FILE", "certs/ca.pem")
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", "/abs/extra.pem")
    env = spawn._child_env("pla_x")
    real = os.path.realpath
    assert real(env["PULSAR_HOME"]) == real(str(work / "relhome")) and os.path.isabs(env["PULSAR_HOME"])
    assert real(env["SSL_CERT_FILE"]) == real(str(work / "certs" / "ca.pem"))
    assert env["NODE_EXTRA_CA_CERTS"] == "/abs/extra.pem"
    seen = {}

    class P:
        pid = 99999999
        returncode = 1

        def poll(self):
            return 1

    def fake_popen(argv, **kw):
        seen.update(kw)
        return P()
    monkeypatch.setattr(spawn.subprocess, "Popen", fake_popen)
    monkeypatch.setenv("PULSAR_PORT", str(_free_base()))
    # A relative PULSAR_HOME names a different folder in every project: starting a Core refuses it, as the
    # launcher does, and nothing is started or created.
    with pytest.raises(K.HomeError):
        spawn.ensure_core(timeout=1)
    assert seen == {} and not (work / "relhome").exists()
    # In a home of its own, Core runs there (never in the app folder), and its paths are absolute.
    absolute = tmp_path / "abshome"
    monkeypatch.setenv("PULSAR_HOME", str(absolute))
    with pytest.raises(RuntimeError):
        spawn.ensure_core(timeout=1)
    assert real(seen["cwd"]) == real(str(absolute)) and real(seen["cwd"]) != real(pulsar.ROOT)
    assert seen["env"]["PULSAR_HOME"] == seen["cwd"]


def test_an_empty_pulsar_home_means_the_default_home(monkeypatch, tmp_path):
    # `export PULSAR_HOME=` (a CI template, a dotfile) means the default home, as it does for the launcher:
    # never the current folder, where Core's run/ files would land in whatever project a launch started from.
    # Paths only: nothing is created in the default home.
    monkeypatch.chdir(str(tmp_path))
    monkeypatch.setenv("PULSAR_HOME", "")
    default = Path(os.path.expanduser("~/.pulsarcode"))
    assert K.home() == default
    assert ST.run_dir() == default / "run" and ST.core_file() == default / "run" / "core.json"
    assert ST.sessions_dir() == default / "run" / "sessions" and ST.sessions_dir().is_absolute()


# ---------------------------------------------------------------------------
# 6. Real Cores: an update never keeps old code serving new launches
# ---------------------------------------------------------------------------

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _free_base(span: int = 12) -> int:
    for _ in range(50):
        base = _free_port()
        if base + span < 65535 and all(spawn.port_free(p) for p in range(base, base + span)):
            return base
    raise RuntimeError("no free port range")


def _wait(pred, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


def _one_process(pid: object) -> bool:
    """A number that can name one process of ours: an integer above 1 (0, -1 and the like name groups or every
    process; 1 is the system's own), never a bool."""
    return isinstance(pid, int) and not isinstance(pid, bool) and pid > 1


def _dead(pid: int) -> bool:
    if not _one_process(pid):
        return True                     # never one of our Cores: nothing to wait for
    if POSIX:
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass
    return not ST.pid_alive(pid)


def _reaped(pid: int) -> bool:
    """True once `pid` is nothing of ours to wait for (reaped now, or not our child). Never signals."""
    if not POSIX or not _one_process(pid):
        return True
    try:
        return os.waitpid(pid, os.WNOHANG)[0] == pid
    except OSError:
        return True


def _stop(info: ST.CoreInfo) -> None:
    """Stop a Core a test started through its own admin token, once it proves it holds that token; never by a
    process number read back (by teardown it may belong to another process)."""
    from pulsar.core import client as CL
    if CL.probe(info.url, info.admin_token, timeout=1.0)[0] != "ok":
        _reaped(info.pid)
        return
    try:
        CL.CoreClient(info.url, info.admin_token).post("/pulsar/shutdown", {}, timeout=3)
    except CL.CoreUnavailable:
        return
    _wait(lambda: spawn.port_free(info.port) and _reaped(info.pid), 10)


def test_teardown_never_acts_on_a_number_that_names_no_single_process(monkeypatch):
    # A process number read back from core.json may be anything: -1 or 0 would reach every child of this test
    # (or every process of the user, for a signal), 1 is the system's own. The helpers leave those alone.
    seen = []
    monkeypatch.setattr(os, "waitpid", lambda *a: seen.append(("waitpid",) + a) or (0, 0))
    monkeypatch.setattr(os, "kill", lambda *a: seen.append(("kill",) + a))
    for pid in (-1, 0, 1, True, None, "4242"):
        assert _reaped(pid) and _dead(pid), pid
    assert seen == []
    _stop(ST.CoreInfo(_free_port(), -1, pulsar.__version__, "pla_not-a-core", 0.0))   # nothing answers its port
    assert seen == []


@pytest.fixture
def real_cores(monkeypatch):
    """Real Cores for one test: each CoreInfo recorded here, and the one core.json names, is stopped through
    its admin token at teardown, never by its process number (numbers below 2 are never even waited on)."""
    monkeypatch.setenv("PULSAR_PORT", str(_free_base()))
    monkeypatch.setenv("PULSAR_CORE_GRACE", "8")
    monkeypatch.setenv("PULSAR_CORE_QUIET", "0")
    monkeypatch.setenv("PULSAR_CORE_SCAN", "0.2")
    K.ensure_home()
    started = []
    yield started
    info = ST.read_core()
    seen = set()
    for core in ([info] if info else []) + started[::-1]:
        if (core.port, core.pid, core.admin_token) not in seen:
            seen.add((core.port, core.pid, core.admin_token))
            _stop(core)


@posix_only
def test_update_with_a_live_session_starts_a_new_core_and_retires_the_old(real_cores, monkeypatch):
    old = spawn.ensure_core()
    real_cores.append(old)
    sess, tok = ST.new_session([_target()], {})                    # a session of the old Core (this pid lives)
    real_build = ST.build_id(refresh=True)
    monkeypatch.setattr(ST, "build_id", lambda refresh=False: "an-update-landed")   # the code on disk changed
    assert spawn.attach() is None
    new = spawn.ensure_core()
    real_cores.append(new)
    monkeypatch.setattr(ST, "build_id", lambda refresh=False: real_build)
    assert new.pid != old.pid and new.port != old.port and ST.read_core() == new
    assert spawn.attach() == new
    # the old Core still serves its session (and says it is retiring) ...
    from pulsar.core import client as CL
    assert CL.CoreClient(old.url, tok).get("/v1/models")["data"][0]["id"] == "model-a"
    state, data, _ = CL.probe(old.url, old.admin_token, timeout=2)
    assert state == "ok" and data["retiring"] is True
    time.sleep(1.0)
    assert ST.pid_alive(old.pid)
    # ... and exits once that session ends; the new one stays
    ST.session_path(sess.sid).unlink()
    assert _wait(lambda: _dead(old.pid), 15)
    assert ST.read_core() == new and spawn.verify(new.port, new.admin_token)


@posix_only
def test_update_with_no_session_replaces_the_core_on_the_same_port(real_cores, monkeypatch):
    old = spawn.ensure_core()
    real_cores.append(old)
    real_build = ST.build_id(refresh=True)
    monkeypatch.setattr(ST, "build_id", lambda refresh=False: "an-update-landed")
    new = spawn.ensure_core()
    real_cores.append(new)
    monkeypatch.setattr(ST, "build_id", lambda refresh=False: real_build)
    assert _wait(lambda: _dead(old.pid), 15)
    assert new.pid != old.pid and new.port == old.port and spawn.attach() == new


@posix_only
def test_a_core_that_core_json_no_longer_names_retires_by_itself(real_cores):
    core = spawn.ensure_core()
    real_cores.append(core)
    sess, _ = ST.new_session([_target()], {})
    heir = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        # another launch published its own Core (the heir stands in for it)
        ST.write_core(ST.CoreInfo(_free_port(), heir.pid, pulsar.__version__, "pla_heir", time.time() + 1))
        time.sleep(1.5)
        assert ST.pid_alive(core.pid)                                    # its session still needs it
        ST.session_path(sess.sid).unlink()
        assert _wait(lambda: _dead(core.pid), 15)
        assert ST.read_core().pid == heir.pid                            # never removes the heir's descriptor
    finally:
        heir.kill()
        heir.wait()


@posix_only
def test_core_process_runs_in_the_pulsar_home(real_cores):
    info = spawn.ensure_core()
    real_cores.append(info)
    if not os.path.exists("/proc/%d/cwd" % info.pid):
        pytest.skip("no /proc")
    assert os.path.realpath("/proc/%d/cwd" % info.pid) == os.path.realpath(str(K.home()))
    with open("/proc/%d/environ" % info.pid, "rb") as fh:
        env = dict(kv.split(b"=", 1) for kv in fh.read().split(b"\0") if b"=" in kv)
    assert b"127.0.0.1" in env.get(b"NO_PROXY", b"").split(b",")
