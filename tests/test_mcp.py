# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
import io
import json

from pulsar.core.client import CoreUnavailable
from pulsar.mcp.server import Server


class FakeCore:
    def __init__(self, fail=False):
        self.posts = []
        self.fail = fail

    def post(self, path, body, timeout=30.0):
        if self.fail:
            raise CoreUnavailable("down")
        self.posts.append((path, body))
        if path == "/pulsar/council":
            return {"answers": [{"model": "a/b", "provider": "x", "ok": True, "text": "four", "seconds": 1.2},
                                {"model": "c/d", "provider": "y", "ok": False, "error": "429", "seconds": 0.3}]}
        return {"ok": True, "viewers": 1}

    def get(self, path, timeout=5.0):
        if path == "/pulsar/stage/viewer":
            return {"url": "http://127.0.0.1:1/stage?t=v", "sid": "s1", "viewers": 1}
        return {"plan": [{"model": "m1", "provider": "nvidia"}, {"model": "m2", "provider": "groq"}],
                "session": {"requests": 3, "relays": 1, "errors": 0, "tokens_in": 10, "tokens_out": 5},
                "breakers": {"nvidia|m1": {"open_for": 9.5}}}


def rpc(srv, method, params=None, mid=1):
    return srv.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})


def test_initialize_negotiates_version():
    srv = Server(FakeCore())
    r = rpc(srv, "initialize", {"protocolVersion": "2025-06-18"})
    assert r["result"]["protocolVersion"] == "2025-06-18"
    assert r["result"]["serverInfo"]["name"] == "pulsar"
    r = rpc(srv, "initialize", {"protocolVersion": "2025-11-25"})
    assert r["result"]["protocolVersion"] == "2025-11-25"
    r = rpc(srv, "initialize", {"protocolVersion": "1999-01-01"})
    assert r["result"]["protocolVersion"] == "2025-11-25"
    assert srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_tools_list_has_core_tools_and_valid_schemas():
    tools = rpc(Server(FakeCore()), "tools/list")["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"stage_present", "stage_show", "stage_clear", "council", "pulsar_status"} <= names
    for t in tools:
        assert t["inputSchema"]["type"] == "object" and t["description"]
    assert not any(n.startswith("pilot_") for n in names)  # off unless enabled


def test_tools_that_send_data_away_are_not_read_only():
    tools = {t["name"]: t for t in rpc(Server(FakeCore()), "tools/list")["result"]["tools"]}
    # council sends the prompt to other providers: never announced as read-only (a client could auto-allow it)
    assert tools["council"]["annotations"]["readOnlyHint"] is False
    assert tools["council"]["annotations"]["openWorldHint"] is True
    assert tools["pulsar_status"]["annotations"]["readOnlyHint"] is True


def test_stage_present_posts_normalized_deck():
    core = FakeCore()
    srv = Server(core)
    srv.viewer_opened = True
    r = rpc(srv, "tools/call", {"name": "stage_present", "arguments": {
        "title": "Plan", "slides": [{"title": "One", "body": "- a\n- b"}, {"title": "Two", "code": {"text": "x=1"}}]}})
    assert r["result"]["isError"] is False
    assert "deck 'Plan' (2 slides, the first on screen)" in r["result"]["content"][0]["text"]
    path, body = core.posts[0]
    assert path == "/pulsar/stage" and body["scene"]["slides"][1]["layout"] == "code"


def test_no_viewer_pops_up_without_a_terminal(monkeypatch):
    from pulsar.stage import launcher as LA

    class Core(FakeCore):
        def get(self, path, timeout=5.0):
            if path == "/pulsar/stage/viewer":
                return {"url": "http://127.0.0.1:1/stage?t=v", "sid": "s1", "viewers": 0}
            return super().get(path, timeout)
    opened = []
    # everything the launcher could start is recorded instead: nothing opens on the machine running the tests
    monkeypatch.setattr(LA, "_run", lambda argv, timeout=4.0, detach=False: opened.append(argv) or 0)
    monkeypatch.setattr(LA, "_spawn", lambda argv: opened.append(argv) or True)
    monkeypatch.setattr(LA, "open_browser", lambda url, env=None, platform=None: opened.append([url]) or True)
    monkeypatch.setattr(LA, "gui_plausible", lambda env=None, platform=None: True)
    monkeypatch.setenv("PULSAR_STAGE_VIEWER", "browser")
    monkeypatch.setattr(LA, "has_terminal", lambda: False)        # a scripted run: cron, CI, no terminal
    r = rpc(Server(Core()), "tools/call", {"name": "stage_show", "arguments": {"kind": "card", "body": "hi"}})
    assert not opened and "pulsarcode stage" in r["result"]["content"][0]["text"]
    monkeypatch.setattr(LA, "has_terminal", lambda: True)
    r = rpc(Server(Core()), "tools/call", {"name": "stage_show", "arguments": {"kind": "card", "body": "hi"}})
    assert opened == [["http://127.0.0.1:1/stage?t=v"]]
    assert "opened in your browser" in r["result"]["content"][0]["text"]


def test_stage_show_validation_error_is_tool_error():
    srv = Server(FakeCore())
    srv.viewer_opened = True
    r = rpc(srv, "tools/call", {"name": "stage_show", "arguments": {"kind": "chart"}})
    assert r["result"]["isError"] is True and "series" in r["result"]["content"][0]["text"]


def test_stage_show_refuses_a_scene_with_nothing_in_it():
    # A weaker model sent a progress scene with a title and no steps: Stage showed an empty panel and answered
    # "shown", so the model told the user it had worked. Every kind needs its content, and the error says which.
    core = FakeCore()
    srv = Server(core)
    srv.viewer_opened = True
    for args, word in (({"kind": "progress", "title": "Build"}, "steps"), ({"kind": "timeline"}, "events"),
                       ({"kind": "table", "columns": ["a"]}, "rows"), ({"kind": "markdown"}, "body")):
        r = rpc(srv, "tools/call", {"name": "stage_show", "arguments": args})
        assert r["result"]["isError"] is True and word in r["result"]["content"][0]["text"], args
    assert core.posts == []                                     # nothing reached the stage


def test_council_says_when_only_the_sessions_own_model_answered():
    # One answer from the model that is already answering was retold to the user as "the council's verdict".
    class Core(FakeCore):
        def post(self, path, body, timeout=30.0):
            return {"answers": [{"model": "m1", "provider": "nvidia", "ok": True, "text": "four", "seconds": 1.0}]}
    srv = Server(Core())
    srv._own_plan = lambda: [{"model": "m1", "provider": "nvidia", "base_url": ""}]
    text = rpc(srv, "tools/call", {"name": "council", "arguments": {"prompt": "2+2?"}})["result"]["content"][0]["text"]
    assert text.startswith("Only this session's own model answered") and "### m1" in text
    srv._own_plan = lambda: [{"model": "other", "provider": "nvidia", "base_url": ""}]
    text = rpc(srv, "tools/call", {"name": "council", "arguments": {"prompt": "2+2?"}})["result"]["content"][0]["text"]
    assert text.startswith("### m1")


def test_core_down_and_missing_core():
    srv = Server(FakeCore(fail=True))
    srv.viewer_opened = True
    r = rpc(srv, "tools/call", {"name": "stage_show", "arguments": {"kind": "card", "body": "hi"}})
    assert r["result"]["isError"] is True and "unavailable" in r["result"]["content"][0]["text"]
    r = rpc(Server(None), "tools/call", {"name": "pulsar_status", "arguments": {}})
    assert r["result"]["isError"] is True and "pulsarcode" in r["result"]["content"][0]["text"]


def test_council_formats_answers():
    r = rpc(Server(FakeCore()), "tools/call", {"name": "council", "arguments": {"prompt": "2+2?"}})
    text = r["result"]["content"][0]["text"]
    assert "### a/b" in text and "four" in text and "ERROR: 429" in text


def test_status_text():
    text = rpc(Server(FakeCore()), "tools/call", {"name": "pulsar_status", "arguments": {}})["result"]["content"][0]["text"]
    assert "1. m1" in text and "relays 1" in text and "Cooling down" in text


def test_unknown_method_and_tool():
    srv = Server(FakeCore())
    assert rpc(srv, "nope/nope")["error"]["code"] == -32601
    assert rpc(srv, "tools/call", {"name": "ghost"})["error"]["code"] == -32602
    assert rpc(srv, "ping")["result"] == {}


def test_serve_loop_parse_error_and_order():
    out = io.StringIO()
    srv = Server(FakeCore(), out=out)
    srv.serve(io.StringIO('not json\n{"jsonrpc":"2.0","id":7,"method":"ping"}\n'))
    lines = [json.loads(x) for x in out.getvalue().splitlines()]
    assert lines[0]["error"]["code"] == -32700 and lines[1]["id"] == 7


def test_switch_moves_only_this_session(pulsar_home, monkeypatch):
    from pulsar import catalog as C
    from pulsar import config as K
    from pulsar.core import state as ST
    st = K.load()
    st.profiles["nvidia"] = K.Profile(id="nvidia", provider="nvidia", model="moonshotai/kimi-k3")
    st.profiles["groq"] = K.Profile(id="groq", provider="groq", model="openai/gpt-oss-120b")
    st.active = "nvidia"
    K.save(st)
    C.write_cache("groq", [C.Model("qwen/qwen3-coder", "groq", context=262144)])
    mine, _ = ST.new_session([ST.Target("nvidia", "nvidia", "moonshotai/kimi-k3")])
    other, _ = ST.new_session([ST.Target("nvidia", "nvidia", "moonshotai/kimi-k3")])
    monkeypatch.setenv("PULSAR_SESSION", mine.sid)
    srv = Server(FakeCore(), out=io.StringIO())
    listing = rpc(srv, "tools/call", {"name": "pulsar_switch", "arguments": {}})["result"]
    assert "qwen/qwen3-coder" in listing["content"][0]["text"]
    res = rpc(srv, "tools/call", {"name": "pulsar_switch", "arguments": {"model": "qwen/qwen3-coder"}})["result"]
    assert not res.get("isError") and "qwen/qwen3-coder" in res["content"][0]["text"]
    plan = ST.load_session(mine.sid).plan
    assert (plan[0].profile, plan[0].model, plan[0].context) == ("groq", "qwen/qwen3-coder", 262144)
    assert ST.load_session(other.sid).plan[0].model == "moonshotai/kimi-k3"
    bad = rpc(srv, "tools/call", {"name": "pulsar_switch", "arguments": {"model": "x\x1b]52;c;AAAA\x07"}})["result"]
    assert bad.get("isError")
    monkeypatch.delenv("PULSAR_SESSION")
    assert rpc(srv, "tools/call", {"name": "pulsar_switch", "arguments": {"model": "a"}})["result"].get("isError")


def test_switch_follows_the_relay_rules(pulsar_home, monkeypatch):
    from pulsar import config as K
    from pulsar.core import state as ST
    st = K.load()
    st.profiles["nvidia"] = K.Profile(id="nvidia", provider="nvidia", model="moonshotai/kimi-k3")
    st.profiles["groq"] = K.Profile(id="groq", provider="groq", model="openai/gpt-oss-120b")
    st.profiles["ollama"] = K.Profile(id="ollama", provider="ollama", model="qwen3")
    st.active = "nvidia"
    K.save(st)
    plan = [ST.Target("nvidia", "nvidia", "moonshotai/kimi-k3"), ST.Target("groq", "groq", "openai/gpt-oss-120b")]
    sess, _ = ST.new_session(plan)
    piloted, _ = ST.new_session(plan, {"pilot": True, "relay": False})
    srv = Server(FakeCore(), out=io.StringIO())
    monkeypatch.setenv("PULSAR_SESSION", sess.sid)
    res = rpc(srv, "tools/call", {"name": "pulsar_switch", "arguments": {"model": "ollama:qwen3"}})["result"]
    assert not res.get("isError")
    # a local model never relays to the cloud providers the session had
    assert [t.profile for t in ST.load_session(sess.sid).plan] == ["ollama"]
    assert "the relay chain is empty" in res["content"][0]["text"]
    # a session with Pilot armed never gets a relay
    monkeypatch.setenv("PULSAR_SESSION", piloted.sid)
    res = rpc(srv, "tools/call", {"name": "pulsar_switch", "arguments": {"model": "groq:openai/gpt-oss-120b"}})
    assert not res["result"].get("isError")
    assert [t.profile for t in ST.load_session(piloted.sid).plan] == ["groq"]


def test_switch_refuses_a_target_core_would_never_send_to(pulsar_home, monkeypatch):
    # The switch gives Core's own reason at once, and the session keeps its plan: a URL that carries a user name
    # or password, or a key that would cross plain http to a host outside this machine and LAN.
    from pulsar import config as K
    from pulsar.core import state as ST
    monkeypatch.delenv("PULSAR_ALLOW_HTTP", raising=False)
    st = K.State(active="nvidia")
    for prof in (K.Profile(id="nvidia", provider="nvidia", model="moonshotai/kimi-k3"),
                 K.Profile(id="custom-auth", provider="custom", model="m1",
                           base_url="https://alice:s3cret-pass@llm.example.com/v1"),
                 K.Profile(id="custom-plain", provider="custom", model="m2", base_url="http://llm.example.com/v1"),
                 K.Profile(id="custom-here", provider="custom", model="m3", base_url="http://127.0.0.1:8000/v1")):
        st.profiles[prof.id] = prof
        K.set_key(prof.id, "zz-switch-key-0123456789")
    monkeypatch.setattr(K, "load", lambda: st)          # the profiles exactly as stored, whatever loading checks
    plan = [ST.Target("nvidia", "nvidia", "moonshotai/kimi-k3")]
    sess, _ = ST.new_session(plan)
    opted, _ = ST.new_session(plan, {"allow_http": True})
    srv = Server(FakeCore(), out=io.StringIO())

    def switch(sid, spec):
        monkeypatch.setenv("PULSAR_SESSION", sid)
        res = rpc(srv, "tools/call", {"name": "pulsar_switch", "arguments": {"model": spec}})["result"]
        return res.get("isError"), res["content"][0]["text"]
    refused, text = switch(sess.sid, "custom-auth:m1")
    assert refused and "user name or password" in text and "s3cret" not in text and "alice" not in text
    refused, text = switch(sess.sid, "custom-plain:m2")
    assert refused and "plain http" in text and "PULSAR_ALLOW_HTTP=1" in text
    assert [t.profile for t in ST.load_session(sess.sid).plan] == ["nvidia"]
    # a model server on this machine, and a session whose launch opted in to plain http, are switched to
    assert switch(sess.sid, "custom-here:m3")[0] is False
    assert switch(opted.sid, "custom-plain:m2")[0] is False
    assert ST.load_session(opted.sid).plan[0].profile == "custom-plain"
    assert switch(opted.sid, "custom-auth:m1")[0] is True          # credentials in a URL: never, opted in or not


def test_structured_results_follow_the_negotiated_protocol():
    srv = Server(FakeCore(), out=io.StringIO())
    rpc(srv, "initialize", {"protocolVersion": "2025-11-25"})
    tools = {t["name"]: t for t in rpc(srv, "tools/list")["result"]["tools"]}
    assert "outputSchema" in tools["council"] and "outputSchema" in tools["stage_present"]
    res = rpc(srv, "tools/call", {"name": "council", "arguments": {"prompt": "2+2?"}})["result"]
    assert res["structuredContent"]["answers"][0] == {"model": "a/b", "provider": "x", "provider_name": "x",
                                                      "ok": True, "text": "four", "error": "", "seconds": 1.2}
    st = rpc(srv, "tools/call", {"name": "pulsar_status", "arguments": {}})["result"]["structuredContent"]
    assert st["route"][0] == {"model": "m1", "provider": "nvidia", "provider_name": "NVIDIA NIM"} and st["relays"] == 1
    old = Server(FakeCore(), out=io.StringIO())
    rpc(old, "initialize", {"protocolVersion": "2025-03-26"})
    assert all("outputSchema" not in t for t in rpc(old, "tools/list")["result"]["tools"])
    assert "structuredContent" not in rpc(old, "tools/call", {"name": "council", "arguments": {"prompt": "x"}})["result"]


def test_council_reports_progress_when_asked():
    import time

    class Slow(FakeCore):
        def post(self, path, body, timeout=30.0):
            time.sleep(0.35)
            return super().post(path, body, timeout)

    out = io.StringIO()
    srv = Server(Slow(), out=out)
    srv.progress_every = 0.1
    res = rpc(srv, "tools/call", {"name": "council", "arguments": {"prompt": "x"}, "_meta": {"progressToken": 7}})
    assert not res["result"]["isError"]
    notes = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    prog = [n for n in notes if n.get("method") == "notifications/progress"]
    assert prog and prog[0]["params"]["progressToken"] == 7 and "council" in prog[0]["params"]["message"]


# ---------------------------------------------------------------------------
# pulsar_switch refuses what `pulsarcode switch` refuses
# ---------------------------------------------------------------------------

def _switch_setup(monkeypatch, extra, route="core"):
    from pulsar import config as K
    from pulsar.core import state as ST
    st = K.load()
    st.profiles["groq"] = K.Profile(id="groq", provider="groq", model="openai/gpt-oss-120b")
    for prof in extra:
        st.profiles[prof.id] = prof
    st.active = "groq"
    K.save(st)
    K.set_key("groq", "gsk_fake-key-not-real-0123456789")
    for prof in extra:
        K.set_key(prof.id, "fake-key-not-real-0123456789abcdef")
    sess, _ = ST.new_session([ST.Target("groq", "groq", "openai/gpt-oss-120b")], {"route": route})
    monkeypatch.setenv("PULSAR_SESSION", sess.sid)
    return sess


def _call_switch(model):
    srv = Server(FakeCore(), out=io.StringIO())
    return rpc(srv, "tools/call", {"name": "pulsar_switch", "arguments": {"model": model}})["result"]


def test_switch_never_sends_a_key_to_a_placeholder_resource(pulsar_home, monkeypatch):
    import pytest
    from pulsar import catalog as C
    from pulsar import config as K
    from pulsar.core import state as ST
    monkeypatch.delenv("PULSAR_OFFLINE", raising=False)
    monkeypatch.setattr(C, "_get_json", lambda *a, **k: pytest.fail("no request may leave for the placeholder"))
    sess = _switch_setup(monkeypatch, [K.Profile(id="azure", provider="azure", base_url="")])
    C.write_cache("groq", [C.Model("openai/gpt-oss-120b", "groq")])      # groq answers from its cache
    for spec in ("azure:gpt-4o", "gpt-4o"):
        res = _call_switch(spec)
        assert res["isError"], spec
    res = _call_switch("azure:gpt-4o")
    assert "no resource URL" in res["content"][0]["text"] and "Nothing was sent" in res["content"][0]["text"]
    assert [t.profile for t in ST.load_session(sess.sid).plan] == ["groq"]


def test_switch_refuses_a_cloud_target_for_a_core_session(pulsar_home, monkeypatch):
    from pulsar import config as K
    from pulsar.core import state as ST
    bedrock = K.Profile(id="bedrock", provider="bedrock", model="sonnet",
                        base_url="https://bedrock-runtime.eu-west-3.amazonaws.com")
    sess = _switch_setup(monkeypatch, [bedrock])
    res = _call_switch("bedrock:sonnet")
    assert res["isError"]
    text = res["content"][0]["text"]
    assert "Claude Code itself" in text and "pulsarcode use bedrock" in text and "Nothing was changed" in text
    after = ST.load_session(sess.sid)
    assert [t.profile for t in after.plan] == ["groq"] and not after.side_channel


def test_switch_with_an_empty_model_and_offline_catalogs(pulsar_home, monkeypatch):
    from pulsar import catalog as C
    from pulsar import config as K
    from pulsar.core import state as ST
    sess = _switch_setup(monkeypatch, [K.Profile(id="xai", provider="xai", model="grok-4.7")])
    res = _call_switch("groq:")
    assert res["isError"] and "Name a model after groq:" in res["content"][0]["text"]
    monkeypatch.setattr(C, "load_catalog", lambda *a, **k: C.CatalogResult([], "seed", "unreachable: refused"))
    res = _call_switch("grok-build-1")
    text = res["content"][0]["text"]
    assert res["isError"] and "groq (unreachable)" in text and "xai (unreachable)" in text
    assert "<provider>:grok-build-1" in text and "groq:grok-build-1" not in text
    assert ST.load_session(sess.sid).plan[0].model == "openai/gpt-oss-120b"


# ---------------------------------------------------------------------------
# round 2: a cloud or direct session is never "switched"; status and council name providers like the status line
# ---------------------------------------------------------------------------

def test_switch_refuses_a_cloud_or_direct_session(pulsar_home, monkeypatch):
    from pulsar import config as K
    from pulsar.core import state as ST
    bedrock = K.Profile(id="bedrock", provider="bedrock", model="sonnet",
                        base_url="https://bedrock-runtime.eu-west-3.amazonaws.com")
    for route, plan0, name in (("cloud", ST.Target("bedrock", "bedrock", "sonnet"), "Amazon Bedrock is used by Claude Code"),
                               ("direct", ST.Target("custom-x", "custom", "m", "http://127.0.0.1:40427", True),
                                "talks to 127.0.0.1:40427 directly")):
        _switch_setup(monkeypatch, [bedrock], route=route)
        sess, _ = ST.new_session([plan0], {"route": route})
        monkeypatch.setenv("PULSAR_SESSION", sess.sid)
        res = _call_switch("groq:openai/gpt-oss-120b")
        text = res["content"][0]["text"]
        assert res["isError"] and name in text and "Pulsar cannot change its model" in text, text
        assert "Nothing was changed" in text and "pulsarcode model groq:openai/gpt-oss-120b" in text
        assert [t.key() for t in ST.load_session(sess.sid).plan] == [plan0.key()]
        listing = rpc(Server(FakeCore(), out=io.StringIO()), "tools/call",
                      {"name": "pulsar_switch", "arguments": {}})["result"]["content"][0]["text"]
        assert "/model" in listing


class LabelCore(FakeCore):
    def post(self, path, body, timeout=30.0):
        if path == "/pulsar/council":
            return {"answers": [{"model": "fake/agent-1", "provider": "custom", "ok": True, "text": "hi", "seconds": 1},
                                {"model": "z-ai/glm-5.3", "provider": "openrouter", "ok": False, "error": "429",
                                 "seconds": 0.2}]}
        return super().post(path, body, timeout)

    def get(self, path, timeout=5.0):
        if path == "/pulsar/stats":
            return {"plan": [{"model": "fake/agent-1", "provider": "custom", "profile": "custom-127-0-0-1-26893",
                              "base_url": "http://127.0.0.1:26893/v1"},
                             {"model": "z-ai/glm-5.3", "provider": "openrouter", "profile": "openrouter"}],
                    "session": {}}
        return super().get(path, timeout)


def test_status_and_council_name_providers_as_the_status_line_does(pulsar_home, monkeypatch):
    from pulsar.core import state as ST
    sess, _ = ST.new_session([ST.Target("custom-127-0-0-1-26893", "custom", "fake/agent-1", "http://127.0.0.1:26893/v1"),
                              ST.Target("openrouter", "openrouter", "z-ai/glm-5.3")])
    monkeypatch.setenv("PULSAR_SESSION", sess.sid)
    srv = Server(LabelCore(), out=io.StringIO())
    rpc(srv, "initialize", {"protocolVersion": "2025-11-25"})
    res = rpc(srv, "tools/call", {"name": "pulsar_status", "arguments": {}})["result"]
    text = res["content"][0]["text"]
    assert "1. fake/agent-1  127.0.0.1:26893" in text and "2. z-ai/glm-5.3  OpenRouter" in text
    assert "  custom" not in text and "openrouter" not in text
    route = res["structuredContent"]["route"]
    assert [(r["provider"], r["provider_name"]) for r in route] == [("custom", "127.0.0.1:26893"),
                                                                    ("openrouter", "OpenRouter")]
    res = rpc(srv, "tools/call", {"name": "council", "arguments": {"prompt": "2+2?"}})["result"]
    text = res["content"][0]["text"]
    assert "### fake/agent-1 (127.0.0.1:26893, 1s)" in text and "### z-ai/glm-5.3 (OpenRouter, 0.2s)" in text
    answers = res["structuredContent"]["answers"]
    assert [(a["provider"], a["provider_name"]) for a in answers] == [("custom", "127.0.0.1:26893"),
                                                                      ("openrouter", "OpenRouter")]
