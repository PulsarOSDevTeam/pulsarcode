# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""End-to-end regression tests for Pulsar Core: live model switches, providers, redaction, notices, council and
the observatory, as Claude Code meets them. Every key here is an obvious fake."""

import io
import json
import urllib.parse
from dataclasses import replace

import httpx
import pytest

from pulsar import catalog as C
from pulsar import config as K
from pulsar import observatory as O
from pulsar import providers as P
from pulsar import translate as T
from pulsar.core import engine as EN
from pulsar.core import server as SV
from pulsar.core import spawn
from pulsar.core import state as ST
from pulsar.redact import redact
from tests.test_core import ADMIN, GROQ, NV, Upstream, client, events, req, setup, sse, text_chunk, text_of

FAKE = "fake-key-not-real-0123456789"
SLASHED = "fake/key+not/real-0123456789"
DS = ST.Target("deepseek", "deepseek", "deepseek-chat")
XAI = ST.Target("xai", "xai", "grok-4.7")


def anth_events(*evs):
    return "".join("event: %s\ndata: %s\n\n" % (e["type"], json.dumps(e)) for e in evs).encode()


START = {"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant", "content": [],
                                               "model": "x", "stop_reason": None,
                                               "usage": {"input_tokens": 5, "output_tokens": 1}}}


def bearer(tok):
    return {"authorization": "Bearer " + tok}


# ---------------------------------------------------------------------------
# Live model switch: the id Claude Code keeps sending is not a pick
# ---------------------------------------------------------------------------

def test_switch_takes_effect_although_claude_code_sends_the_launch_model(pulsar_home):
    sess, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-fake", "groq": "gsk_fake"})
    up = Upstream({"integrate.api.nvidia.com": [(200, sse(text_chunk("nv", "stop")))],
                   "api.groq.com": [(200, sse(text_chunk("groq", "stop")))]})
    c = client(up)
    assert text_of(events(c.post("/v1/messages", json=req(model=NV.model), headers=bearer(tok)))) == "nv"
    assert ST.update_plan(sess.sid, [GROQ, NV])
    r = c.post("/v1/messages", json=req(model=NV.model + "[1m]"), headers=bearer(tok))
    assert text_of(events(r)) == "groq"
    assert up.calls[-1][0] == "api.groq.com" and up.calls[-1][3]["model"] == GROQ.model


def test_a_real_pick_still_wins_and_a_later_switch_beats_it(pulsar_home):
    sess, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-fake", "groq": "gsk_fake"})
    up = Upstream({"integrate.api.nvidia.com": [(200, sse(text_chunk("nv", "stop")))],
                   "api.groq.com": [(200, sse(text_chunk("groq", "stop")))]})
    c = client(up)
    c.post("/v1/messages", json=req(model="z-ai/glm-5.3"), headers=bearer(tok))     # a /model pick
    assert up.calls[-1][3]["model"] == "z-ai/glm-5.3"
    assert ST.update_plan(sess.sid, [GROQ])
    c.post("/v1/messages", json=req(model="z-ai/glm-5.3"), headers=bearer(tok))     # Claude Code still sends it
    assert up.calls[-1][0] == "api.groq.com" and up.calls[-1][3]["model"] == GROQ.model


def test_model_picks_unit():
    picks = ST.ModelPicks()
    a = ST.Session("s1", "h", [ST.Target("p", "nvidia", "model-a")])
    assert picks.explicit(a, "model-a") == ""
    b = ST.Session("s1", "h", [ST.Target("q", "groq", "model-b")])
    assert picks.explicit(b, "model-a[1m]") == ""
    assert picks.explicit(b, "model-c") == "model-c"
    assert picks.explicit(b, "model-a") == ""


# ---------------------------------------------------------------------------
# A base_url override carries native traffic with it
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pid,base,model,want", [
    ("openrouter", "http://127.0.0.1:6101/api/v1", "anthropic/claude-sonnet-4.5", "http://127.0.0.1:6101/api/v1/messages"),
    ("vercel", "https://gw.corp.example/v1", "anthropic/claude-sonnet-4.5", "https://gw.corp.example/v1/messages"),
    ("requesty", "https://gw.corp.example/requesty/v1", "anthropic/claude-sonnet-4.5",
     "https://gw.corp.example/requesty/v1/messages"),
    ("deepseek", "http://127.0.0.1:5822/v1", "deepseek-chat", "http://127.0.0.1:5822/anthropic/v1/messages"),
    ("moonshot", "https://gw.corp.example/moonshot/v1", "kimi-k3", "https://gw.corp.example/moonshot/anthropic/v1/messages"),
])
def test_native_traffic_follows_the_configured_host(pid, base, model, want):
    t = ST.Target(pid, pid, model, base)
    prov = EN.provider_for(t)
    r = EN.Resolved(t, prov, "k", model, prov.native_for(model))
    assert r.url == want
    assert EN._host(r.url) == EN._host(base)


def test_override_back_to_default_and_regions_unchanged():
    assert replace(P.get("openrouter"), base_url="https://openrouter.ai/api/v1").native_base() == "https://openrouter.ai/api"
    assert replace(P.get("moonshot"), base_url="https://api.moonshot.cn/v1").native_base() == "https://api.moonshot.cn/anthropic"
    assert all(P.BY_ID[p.id] == p for p in P.PROVIDERS)


def test_underivable_native_base_never_falls_back_to_a_public_host():
    deepinfra = replace(P.get("deepinfra"), base_url="https://gw.corp.example/v1")
    assert not deepinfra.native_for("anything") and deepinfra.anthropic_native_url == ""
    zai = replace(P.get("zai"), base_url="https://gw.corp.example/glm")
    assert EN._host(zai.native_base()) == "gw.corp.example"


# ---------------------------------------------------------------------------
# Redaction: escaped and partial echoes of the key
# ---------------------------------------------------------------------------

def test_redact_escaped_and_partial_keys():
    assert SLASHED not in redact("Invalid API key: " + SLASHED.replace("/", "\\/"), [SLASHED]).replace("\\/", "/")
    esc = "".join("\\u%04x" % ord(c) for c in FAKE)
    assert "0123" not in redact("bad key " + esc, [FAKE]).replace(FAKE[:4] + "…" + FAKE[-4:], "")
    assert FAKE[:-3] not in redact("Invalid API key: " + FAKE[:-3], [FAKE])
    assert FAKE[5:] not in redact("key ..." + FAKE[5:], [FAKE])
    assert redact("short fake-key-n here", [FAKE]) == "short fake-key-n here"     # 10 chars: not a fragment


@pytest.mark.parametrize("key", [
    "vck_" + "a1B2" * 6, "vapi_" + "a1B2" * 6, "cpk_" + "a1b2" * 8, "csk_" + "a1B2" * 6, "tgp_" + "a1B2" * 6,
    "sk_" + "a1B2" * 6, "0123456789abcdef0123456789abcdef.AbCdEfGhIjKlMnOp", "ya29.a0" + "AbCd" * 10])
def test_registry_key_shapes_are_masked_without_the_key(key):
    # stats, the event bus and `pulsarcode top --json` redact by shape only (they do not hold the key)
    assert key not in redact("upstream echoed %s here" % key)


def test_shape_masking_leaves_ordinary_words_alone():
    text = "unknown field task_management_settings_value; see disk_cache_key_lookup"
    assert redact(text) == text


def test_notices_drop_bidi_and_zero_width_controls_before_redaction():
    f = EN.Failure("http", 400, json.dumps({"error": {"message": "bad \u202eevil\u200b text \x1b[2J"}}))
    note = EN.notice(P.get("nvidia"), "m", f)
    assert not any(c in note for c in "\u202e\u200b\x1b") and "evil" in note
    split = FAKE[:10] + "\u200b" + FAKE[10:]           # a hidden character must not carry the key past redaction
    note = EN.notice(P.get("nvidia"), "m", EN.Failure("http", 401, "Invalid key " + split), secrets=[FAKE])
    assert FAKE not in note.replace("\u200b", "")


@pytest.mark.parametrize("echo", [
    lambda k: '{"error":{"message":"Invalid API key: %s"}}' % k.replace("/", "\\/"),
    lambda k: '{"error":{"message":"bad key %s"}}' % "".join("\\u%04x" % ord(c) for c in k),
    lambda k: json.dumps({"error": {"message": "Invalid API key: %s" % k[:-3]}}),
])
def test_key_never_reaches_notice_or_stats(pulsar_home, echo):
    _, tok = setup(pulsar_home, [NV], {"nvidia": SLASHED})

    def handler(request):
        return httpx.Response(401, content=echo(SLASHED).encode(), headers={"content-type": "application/json"})
    c = client(handler)
    text = text_of(events(c.post("/v1/messages", json=req(), headers=bearer(tok))))
    stats = json.dumps(c.get("/pulsar/stats", headers=bearer(ADMIN)).json())
    for blob in (text, stats):
        assert "key+not" not in blob and "not/real-012" not in blob


# ---------------------------------------------------------------------------
# Native SSE error event
# ---------------------------------------------------------------------------

def test_native_sse_error_becomes_a_redacted_notice_and_a_clean_end(pulsar_home):
    _, tok = setup(pulsar_home, [DS], {"deepseek": FAKE})
    body = anth_events(START,
                       {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                       {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi"}},
                       {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded for key " + FAKE}},
                       {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "LATE"}},
                       {"type": "message_stop"})
    up = Upstream({"api.deepseek.com": [(200, body)]})
    r = client(up).post("/v1/messages", json=req(), headers=bearer(tok))
    evs = events(r)
    names = [n for n, _ in evs]
    assert "error" not in names and names[-1] == "message_stop" and names.count("message_stop") == 1
    text = text_of(evs)
    assert FAKE not in r.text and "LATE" not in text and "Overloaded" in text and len(up.calls) == 1
    assert [p["delta"]["stop_reason"] for n, p in evs if n == "message_delta"] == ["end_turn"]


def test_native_sse_overflow_is_prompt_is_too_long_with_the_stated_limit(pulsar_home):
    _, tok = setup(pulsar_home, [DS], {"deepseek": FAKE})
    body = anth_events({"type": "error", "error": {"type": "invalid_request_error", "message":
                        "This model's maximum context length is 65536 tokens. However, you requested 70000 tokens."}})
    up = Upstream({"api.deepseek.com": [(200, body)]})
    r = client(up, ping_interval=30).post("/v1/messages", json=req(), headers=bearer(tok))
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["type"] == "invalid_request_error" and err["message"].startswith("prompt is too long")
    assert err["message"].endswith("> 65536 maximum")


# ---------------------------------------------------------------------------
# Cloud and direct sessions: the token is a side channel only
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("plan,flags", [
    ([ST.Target("bedrock", "bedrock", "sonnet"), GROQ], {}),
    ([ST.Target("vertex", "vertex", "sonnet"), GROQ], {}),
    ([DS, GROQ], {"route": "direct"}),
])
def test_side_channel_token_cannot_drive_the_gateway(pulsar_home, plan, flags):
    _, tok = setup(pulsar_home, plan, {"groq": "gsk_fake", "deepseek": FAKE}, flags)
    up = Upstream({"api.groq.com": [(200, sse(text_chunk("leak", "stop")))],
                   "api.deepseek.com": [(200, sse(text_chunk("leak", "stop")))]})
    c = client(up)
    assert c.post("/v1/messages", json=req(), headers=bearer(tok)).status_code == 403
    assert c.post("/v1/messages", json=req(stream=False), headers=bearer(tok)).status_code == 403
    assert c.post("/v1/messages/count_tokens", json=req(), headers=bearer(tok)).status_code == 403
    assert not up.calls
    assert c.get("/pulsar/stats", headers=bearer(tok)).status_code == 200
    assert c.get("/pulsar/stage/scenes", headers=bearer(tok)).status_code == 200


# ---------------------------------------------------------------------------
# Council
# ---------------------------------------------------------------------------

def _profiles():
    st = K.load()
    st.upsert(K.Profile(id="groq", provider="groq", model=GROQ.model))
    st.upsert(K.Profile(id="xai", provider="xai", model=XAI.model))
    st.upsert(K.Profile(id="nvidia", provider="nvidia", model=NV.model))
    K.save(st)


ANSWER = {"choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}], "usage": {}}


def test_council_bare_id_goes_to_the_provider_that_lists_it(pulsar_home):
    _profiles()
    C.write_cache("groq", [C.Model(GROQ.model, "groq")])
    C.write_cache("xai", [C.Model("grok-build-1", "xai")])
    _, tok = setup(pulsar_home, [GROQ, XAI], {"groq": "gsk_fake", "xai": "xai-fakefakefake"})
    up = Upstream({"api.x.ai": [(200, ANSWER)], "api.groq.com": [(200, ANSWER)]})
    r = client(up).post("/pulsar/council", json={"prompt": "q", "models": ["grok-build-1", "no-such-model-1"]},
                        headers=bearer(tok)).json()
    ans = {a["model"]: a for a in r["answers"]}
    assert ans["grok-build-1"]["provider"] == "xai" and ans["grok-build-1"]["ok"]
    assert not ans["no-such-model-1"]["ok"]
    assert [h for h, *_ in up.calls] == ["api.x.ai"]


def test_council_default_is_the_other_models(pulsar_home):
    _profiles()
    _, tok = setup(pulsar_home, [GROQ, XAI, NV], {"groq": "gsk_fake", "xai": "xai-fakefakefake", "nvidia": "nvapi-fake"})
    up = Upstream({"api.x.ai": [(200, ANSWER)], "api.groq.com": [(200, ANSWER)],
                   "integrate.api.nvidia.com": [(200, ANSWER)]})
    r = client(up).post("/pulsar/council", json={"prompt": "q"}, headers=bearer(tok)).json()
    assert sorted(a["provider"] for a in r["answers"]) == ["nvidia", "xai"]
    one = ST.Session("s", "h", [GROQ])
    assert SV._council_targets(one, None) == [GROQ]


# ---------------------------------------------------------------------------
# Loop guard
# ---------------------------------------------------------------------------

def test_same_short_answer_to_different_questions_is_not_a_loop():
    msgs = []
    for q in ("one", "two", "three", "four", "five"):
        msgs.append({"role": "user", "content": [{"type": "text", "text": q}]})
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": "OK."}]})
    assert EN.detect_loop({"messages": msgs[:-1]}) is None
    step = {"role": "assistant", "content": [{"type": "tool_use", "id": "x", "name": "Bash", "input": {"c": "ls"}}]}
    res = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "same"},
                                       {"type": "text", "text": "<system-reminder>noise</system-reminder>"}]}
    assert EN.detect_loop({"messages": [{"role": "user", "content": "go"}] + [step, res] * 4})


# ---------------------------------------------------------------------------
# Output-cap truncation of a tool call
# ---------------------------------------------------------------------------

def _ck(delta=None, finish=None):
    return {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}


def test_length_cut_tool_call_ends_the_turn_once():
    tr = T.StreamTranslator("m", tools=[{"name": "Write", "input_schema": {"type": "object"}}])
    ev = tr.start()
    ev += tr.feed(_ck({"content": "Writing the file."}))
    ev += tr.feed(_ck({"tool_calls": [{"index": 0, "id": "c", "function": {"name": "Write",
                                                                           "arguments": '{"path": "x", "body": "ab'}}]}))
    ev += tr.feed(_ck(finish="length"))
    ev += tr.finish()
    stop = [p["delta"]["stop_reason"] for n, p in ev if n == "message_delta"]
    text = "".join(p["delta"].get("text", "") for n, p in ev if n == "content_block_delta")
    assert stop == ["end_turn"] and "output token limit" in text and "not run" in text
    tr2 = T.StreamTranslator("m")
    ev2 = tr2.start() + tr2.feed(_ck({"content": "a long answer"})) + tr2.feed(_ck(finish="length")) + tr2.finish()
    assert [p["delta"]["stop_reason"] for n, p in ev2 if n == "message_delta"] == ["max_tokens"]


# ---------------------------------------------------------------------------
# Notices: relay count, web pages
# ---------------------------------------------------------------------------

def test_single_target_retries_are_not_relay_targets(pulsar_home):
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-fake"})
    up = Upstream({"integrate.api.nvidia.com": [(503, {"error": {"message": "down"}})]})
    tun = EN.Tuning(header_timeout=5, header_timeout_reasoning=5, first_token_timeout=5,
                    first_token_timeout_reasoning=5, ping_interval=0.02, retries=2)
    from starlette.testclient import TestClient
    c = TestClient(SV.make_app(ADMIN, 0, transport=httpx.MockTransport(up), tuning=tun))
    text = text_of(events(c.post("/v1/messages", json=req(), headers=bearer(tok))))
    assert "relay targets" not in text and "Retried 2 times" in text and len(up.calls) == 3
    p = P.get("nvidia")
    two = EN.notice(p, "m", EN.Failure("http", 503), tried=2, retried=1)
    assert "2 relay targets were tried and the last one retried once" in two


def test_html_page_says_check_the_base_url(pulsar_home):
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-fake"})
    page = b"<!DOCTYPE html><html><body><h1>Welcome to nginx!</h1></body></html>"
    up = Upstream({"integrate.api.nvidia.com": [(200, page)]})
    text = text_of(events(client(up).post("/v1/messages", json=req(), headers=bearer(tok))))
    assert "web page instead of the API" in text and "/v1" in text and "Retry shortly" not in text


# ---------------------------------------------------------------------------
# Classifier verdicts on Anthropic-compatible vendors
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("answer,want", [
    ("```\n<block>no</block>\n```", "<block>no</block>"),
    ("No, this is fine.", "No, this is fine."),      # not a verdict: Claude Code's own safe default applies
])
def test_classifier_verdict_normalized_on_native_non_anthropic(pulsar_home, answer, want):
    _, tok = setup(pulsar_home, [DS], {"deepseek": FAKE})
    msg = {"id": "m", "type": "message", "role": "assistant", "model": "deepseek-chat", "stop_reason": "end_turn",
           "content": [{"type": "text", "text": answer}], "usage": {"input_tokens": 1, "output_tokens": 1}}
    up = Upstream({"api.deepseek.com": [(200, msg)]})
    body = req(stream=False, system="Answer <block>no</block> or <block>yes</block><category>x</category>")
    out = client(up).post("/v1/messages", json=body, headers=bearer(tok)).json()
    assert out["content"][0]["text"] == want


# ---------------------------------------------------------------------------
# Context overflow before anything was sent: a real HTTP 400
# ---------------------------------------------------------------------------

def test_overflow_before_first_byte_is_a_real_http_400(pulsar_home):
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-fake"})
    up = Upstream({"integrate.api.nvidia.com": [(400, {"error": {"message": "maximum context length is 8192 tokens"}})]})
    r = client(up).post("/v1/messages", json=req(), headers=bearer(tok))
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["type"] == "invalid_request_error" and err["message"].startswith("prompt is too long")


# ---------------------------------------------------------------------------
# No endpoint URLs in stats, the bus or `top --json`
# ---------------------------------------------------------------------------

def test_errors_recorded_without_urls(pulsar_home):
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-fake"})
    msg = "upstream https://integrate.api.nvidia.com/v1/chat/completions failed, see http://10.20.30.40:8000/v1/x"
    up = Upstream({"integrate.api.nvidia.com": [(500, {"error": {"message": msg}})]})
    c = client(up)
    c.post("/v1/messages", json=req(), headers=bearer(tok))
    stats = json.dumps(c.get("/pulsar/stats", headers=bearer(ADMIN)).json())
    assert "10.20.30.40" not in stats and "integrate.api.nvidia.com/v1" not in stats and "<url>" in stats
    assert O.scrub_urls("x http://a.example/b y") == "x <url> y"


def test_redirect_detail_names_no_url(pulsar_home):
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-fake"})

    def handler(request):
        return httpx.Response(302, headers={"location": "https://evil.example/collect?k=1"})
    c = client(handler)
    text = text_of(events(c.post("/v1/messages", json=req(), headers=bearer(tok))))
    stats = json.dumps(c.get("/pulsar/stats", headers=bearer(ADMIN)).json())
    assert "https://evil.example" not in text + stats and "collect" not in text + stats


# ---------------------------------------------------------------------------
# The admin token never sits in Core's environment
# ---------------------------------------------------------------------------

def test_admin_token_goes_on_stdin(monkeypatch):
    assert "PULSAR_ADMIN_TOKEN" not in spawn._child_env("pla_fake")

    class Pipe(io.BytesIO):
        def close(self):
            self.seen = self.getvalue()
            super().close()

    class Proc:
        stdin = Pipe()
    spawn._hand_token(Proc, "pla_fake")
    assert Proc.stdin.seen == b"pla_fake\n"
    monkeypatch.delenv("PULSAR_ADMIN_TOKEN", raising=False)
    monkeypatch.setattr(SV.sys, "stdin", io.StringIO("pla_fake\n"))
    assert SV._token_from_stdin() == "pla_fake"


# ---------------------------------------------------------------------------
# Observatory
# ---------------------------------------------------------------------------

T0 = 1_760_000_000.0
PLAN = [{"profile": "groq-a", "provider": "groq", "model": "m1"}, {"profile": "groq-b", "provider": "groq", "model": "m2"}]
SESS = {"sid": "s32d19dcb1c55", "label": "refactor", "cwd": "/w", "plan": PLAN, "pid": 1}


def _snap(breakers):
    return {"uptime": 10.0, "flights": [], "total": {"requests": 11}, "sessions": {SESS["sid"]: {"requests": 11}},
            "providers": {"groq": {"requests": 11, "errors": 4, "ttft_ewma": 0.5,
                                   "last_error": "upstream http://127.0.0.1:5832/v1/chat/completions failed"}},
            "breakers": breakers, "sessions_live": [SESS]}


def test_provider_open_only_when_every_target_is_open():
    red = O.Reducer()
    red.link({"port": 4747, "pid": 1, "version": "3"}, T0)
    red.stats(_snap({"groq-a|m1": {"open_for": 60.0, "fails": 4}}), T0)
    prov = red.view(T0 + 1).providers[0]
    assert prov["health"] == "degraded"
    red.stats(_snap({"groq-a|m1": {"open_for": 60.0, "fails": 4}, "groq-b|m2": {"open_for": 60.0, "fails": 1}}), T0 + 2)
    assert red.view(T0 + 3).providers[0]["health"] == "open"
    data = json.dumps(red.to_json(T0 + 3))
    assert "127.0.0.1:5832" not in data and "<url>" in data


def test_focus_shows_the_label_and_empty_texts():
    red = O.Reducer()
    red.link({"port": 4747, "pid": 1, "version": "3"}, T0)
    red.stats(_snap({}), T0)
    obs = O.Observatory(red, motion=False, focus=SESS["sid"])
    cv = obs.frame(160, 48, T0 + 1)
    screen = "\n".join("".join(cv.get(x, y)[0] or " " for x in range(cv.w)) for y in range(cv.h))
    assert "focus refactor" in screen and "focus " + SESS["sid"] not in screen
    assert "no samples since top started" in screen
    obs.verbose = True
    v = red.view(T0 + 1, None, verbose=True)
    assert v.verbose


# ---------------------------------------------------------------------------
# Round 2: long keys (RecursionError in redact), faults before the first byte
# ---------------------------------------------------------------------------

LONG = "eyJfake-not-real-" + "A1b2C3d4" * 148          # a 1201-character JWT-style bearer token, fake


def test_upstream_401_with_a_long_key_gives_the_401_notice(pulsar_home):
    _, tok = setup(pulsar_home, [NV], {"nvidia": LONG})
    echo = {"error": {"message": "Incorrect API key provided: %s (Authorization: Bearer %s)" % (LONG, LONG[:700]),
                      "code": "invalid_api_key"}}
    up = Upstream({"integrate.api.nvidia.com": [(401, echo)]})
    c = client(up)
    r = c.post("/v1/messages", json=req(), headers=bearer(tok))
    assert r.status_code == 200
    text = text_of(events(r))
    assert "rejected the API key (HTTP 401)" in text and len(up.calls) == 1
    assert "A1b2C3d4A1b2" not in r.text
    r = c.post("/v1/messages", json=req(stream=False), headers=bearer(tok))
    assert r.status_code == 200 and "rejected the API key (HTTP 401)" in json.dumps(r.json())
    stats = json.dumps(c.get("/pulsar/stats", headers=bearer(ADMIN)).json())
    assert "A1b2C3d4A1b2" not in stats


def test_council_answers_with_a_long_key(pulsar_home):
    _profiles()
    _, tok = setup(pulsar_home, [GROQ, XAI], {"groq": "gsk_fake", "xai": LONG})
    up = Upstream({"api.x.ai": [(200, {"choices": [{"message": {"content": "answer, not " + LONG[:640]},
                                                    "finish_reason": "stop"}], "usage": {}})]})
    r = client(up).post("/pulsar/council", json={"prompt": "q"}, headers=bearer(tok))
    assert r.status_code == 200
    ans = r.json()["answers"]
    assert [a["provider"] for a in ans] == ["xai"] and ans[0]["ok"] and ans[0]["text"].startswith("answer, not ")
    assert "A1b2C3d4A1b2" not in r.text


def test_a_fault_before_the_first_byte_is_a_notice_not_a_500(pulsar_home, monkeypatch, capsys):
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-fake"})

    async def broken(self, sess, body, incoming):
        raise RuntimeError("boom near nvapi-fakefakefake0123")
        yield ""                                          # an async generator, as Engine.stream is

    monkeypatch.setattr(EN.Engine, "stream", broken)
    up = Upstream({})
    r = client(up).post("/v1/messages", json=req(), headers=bearer(tok))
    assert r.status_code == 200
    evs = events(r)
    assert "internal error (RuntimeError)" in text_of(evs) and [n for n, _ in evs][-1] == "message_stop"
    err = capsys.readouterr().err
    assert "RuntimeError" in err and "nvapi-fakefakefake0123" not in err


# ---------------------------------------------------------------------------
# Round 2: a 200 JSON error object maps its error type like the SSE form
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body,want", [
    ({"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key " + FAKE}},
     "rejected the API key (HTTP 401)"),
    ({"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}, "rate-limiting this key (HTTP 429)"),
    ({"error": {"message": "Incorrect API key provided: " + FAKE, "code": "invalid_api_key"}},
     "rejected the API key (HTTP 401)"),
    ({"error": {"message": "something odd"}}, "having trouble (HTTP 502)"),
])
def test_native_200_json_error_object_maps_its_type(pulsar_home, body, want):
    _, tok = setup(pulsar_home, [DS], {"deepseek": FAKE})
    up = Upstream({"api.deepseek.com": [(200, body)]})
    c = client(up)
    r = c.post("/v1/messages", json=req(), headers=bearer(tok))
    assert want in text_of(events(r)) and FAKE not in r.text
    r = c.post("/v1/messages", json=req(stream=False), headers=bearer(tok))
    assert want in json.dumps(r.json()) and FAKE not in r.text


def test_fallback_error_status_unit():
    assert EN._error_status({"type": "authentication_error"}, 502) == 401
    assert EN._error_status({"code": 429, "type": "authentication_error"}, 502) == 429
    assert EN._error_status({"status": "503"}, 502) == 503
    assert EN._error_status({"type": "server_error"}, 502) == 502
    assert EN._error_status("plain text", 502) == 502


# ---------------------------------------------------------------------------
# Round 2: Azure placeholders never reach Core's wire
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("target", [
    ST.Target("azure", "azure", "gpt-6"),
    ST.Target("azure", "azure", "gpt-6", "https://YOUR-RESOURCE.openai.azure.com/openai/v1"),
    ST.Target("azure", "azure", "gpt-6", "https://your-resource.openai.azure.com/openai/v1/"),
    ST.Target("foundry", "foundry", "claude-sonnet-4-5"),
    ST.Target("foundry", "foundry", "claude-sonnet-4-5", "https://YOUR-RESOURCE.services.ai.azure.com/anthropic"),
])
def test_placeholder_azure_target_is_skipped_and_relays(pulsar_home, target):
    _, tok = setup(pulsar_home, [target, GROQ], {target.profile: FAKE, "groq": "gsk_fake"}, {"relay": True})
    up = Upstream({"api.groq.com": [(200, sse(text_chunk("groq answered", "stop")))]})
    r = client(up).post("/v1/messages", json=req(), headers=bearer(tok))
    assert text_of(events(r)) == "groq answered"
    assert [h for h, *_ in up.calls] == ["api.groq.com"]
    eng = EN.Engine(EN.Stats(), lambda _p: FAKE, pulsar_home)
    with pytest.raises(KeyError):
        eng.resolve(target, "", primary=True)


def test_real_azure_resource_still_resolves(pulsar_home):
    eng = EN.Engine(EN.Stats(), lambda _p: FAKE, pulsar_home)
    r = eng.resolve(ST.Target("azure", "azure", "gpt-6", "https://contoso-fake.openai.azure.com/openai/v1"), "", True)
    assert EN._host(r.url) == "contoso-fake.openai.azure.com"


def test_council_with_a_placeholder_profile_answers_without_sending(pulsar_home):
    st = K.load()
    st.upsert(K.Profile(id="azure", provider="azure", model="gpt-6"))
    K.save(st)
    _, tok = setup(pulsar_home, [GROQ], {"groq": "gsk_fake", "azure": FAKE})
    up = Upstream({})
    r = client(up).post("/pulsar/council", json={"prompt": "q", "models": ["azure:gpt-6"]}, headers=bearer(tok))
    assert r.status_code == 200
    ans = r.json()["answers"]
    assert len(ans) == 1 and not ans[0]["ok"] and "resource URL" in ans[0]["error"] and not up.calls


# ---------------------------------------------------------------------------
# Round 2: a same-host base_url keeps the vendor's native endpoint
# ---------------------------------------------------------------------------

def test_same_host_base_url_keeps_the_registry_native_url():
    t = ST.Target("deepseek", "deepseek", "deepseek-chat", "https://api.deepseek.com/beta")
    prov = EN.provider_for(t)
    r = EN.Resolved(t, prov, "k", t.model, prov.native_for(t.model))
    assert r.url == "https://api.deepseek.com/anthropic/v1/messages"
    moved = EN.provider_for(ST.Target("deepseek", "deepseek", "deepseek-chat", "https://gw.corp.example/beta"))
    assert EN._host(moved.native_base()) == "gw.corp.example"


@pytest.mark.parametrize("prov", [p for p in P.PROVIDERS if p.route != "cloud" and not p.needs_url
                                  and (p.native_url or p.anthropic_native_url)], ids=lambda p: p.id)
def test_same_host_other_path_never_derives_a_new_native_path(prov):
    split = urllib.parse.urlsplit(prov.base_url)
    moved = replace(prov, base_url=split.scheme + "://" + split.netloc + "/beta")
    if not prov.regions:
        assert (moved.native_url, moved.anthropic_native_url) == (prov.native_url, prov.anthropic_native_url)
    else:       # an unknown regional URL may drop native routing, but never invents a path
        assert moved.native_base() in ("", prov.native_base())


# ---------------------------------------------------------------------------
# Round 2: the Providers title counts every open breaker; a degraded row names its breaker
# ---------------------------------------------------------------------------

def _breaker_view(now):
    plan = [{"profile": "nvidia", "provider": "nvidia", "model": "moonshotai/kimi-k3"},
            {"profile": "groq-a", "provider": "groq", "model": "openai/gpt-oss-20b"},
            {"profile": "groq-b", "provider": "groq", "model": "openai/gpt-oss-120b"},
            {"profile": "cerebras", "provider": "cerebras", "model": "c1"},
            {"profile": "openrouter", "provider": "openrouter", "model": "z-ai/glm-5.3"}]
    sess = {"sid": "s7197fbebc9fb", "label": "app", "cwd": "/w", "plan": plan, "pid": 1}
    red = O.Reducer()
    red.link({"port": 4747, "pid": 1, "version": "3"}, now)
    red.stats({"uptime": 10.0, "flights": [], "total": {"requests": 18}, "sessions": {sess["sid"]: {"requests": 18}},
               "providers": {"nvidia": {"requests": 3, "errors": 3}, "groq": {"requests": 5, "errors": 3},
                             "cerebras": {"requests": 2}, "openrouter": {"requests": 8}},
               "breakers": {"nvidia|moonshotai/kimi-k3": {"open_for": 157.0, "fails": 6},
                            "groq-a|openai/gpt-oss-20b": {"open_for": 60.0, "fails": 2}},
               "sessions_live": [sess]}, now)
    return red


@pytest.mark.parametrize("h,sub_rows", [(4, 0), (5, 1), (6, 1), (9, 1)])
def test_providers_title_and_degraded_row_name_the_open_breakers(h, sub_rows):
    # NVIDIA's one breaker is named on its own OPEN row, so a spare row never repeats it: the spare row goes to
    # the degraded Groq's breaker (tests/test_e2e_observatory.py); without one, Groq's row names it inline.
    from pulsar import fx
    now = T0 + 1
    red = _breaker_view(now)
    obs = O.Observatory(red, motion=False)
    v = red.view(now)
    health = {p["id"]: p["health"] for p in v.providers}
    assert health["nvidia"] == "open" and health["groq"] == "degraded"
    assert obs._meta("providers", v) == "4 · 2 breakers open"
    cv = fx.Canvas(80, h)
    obs._draw_providers(cv, (0, 0, 80, h), v, now)
    rows = ["".join(c[0] or " " for c in row) for row in cv.cells]
    assert "NVIDIA" in rows[0] and "OPEN" in rows[0] and "kimi-k3" in rows[0]
    assert sum("kimi-k3" in row for row in rows) == 1
    assert "Groq" in rows[1] and "5 req" in rows[1]
    assert sum(1 for row in rows if row.lstrip().startswith(("╰", "`"))) == sub_rows
    named = rows[1] if not sub_rows else rows[2]
    assert named.lstrip().startswith(("╰", "`")) or not sub_rows
    assert "OPEN 1m00s" in named and "gpt-oss-20b" in named
    assert sum("gpt-oss-20b" in row for row in rows) == 1


# ---------------------------------------------------------------------------
# Round 3: the chat wire maps an in-stream error chunk like a 200 JSON body
# ---------------------------------------------------------------------------

AUTH_ERR = {"error": {"message": "Incorrect API key provided: fake****6789.", "type": "invalid_request_error",
                      "param": None, "code": "invalid_api_key"}}
OVF_ERR = {"error": {"message": "This model's maximum context length is 8192 tokens. However, your messages resulted "
                                "in 16000 tokens. Please reduce the length of the messages.",
                     "type": "invalid_request_error", "param": "messages", "code": "context_length_exceeded"}}


def _err_sse(*chunks):
    return "".join("data: " + json.dumps(c) + "\n\n" for c in chunks).encode()


@pytest.mark.parametrize("payload", [AUTH_ERR, _err_sse(AUTH_ERR)], ids=["json200", "sse"])
def test_chat_auth_error_reads_401_as_json_body_and_as_sse_chunk(pulsar_home, payload):
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-fake"})
    up = Upstream({"integrate.api.nvidia.com": [(200, payload)]})
    r = client(up).post("/v1/messages", json=req(), headers=bearer(tok))
    text = text_of(events(r))
    assert r.status_code == 200 and "rejected the API key (HTTP 401)" in text and "HTTP 500" not in text
    assert len(up.calls) == 1


def test_chat_error_chunk_mid_stream_keeps_its_code():
    assert EN._error_status({"code": "context_length_exceeded", "type": "server_error"}, 500) == 400
    assert EN._error_status({"code": "invalid_api_key"}, 500) == 401
    assert EN._error_status({"code": "rate_limit_exceeded"}, 500) == 429
    assert EN._error_status({"code": "something_new"}, 500) == 500


def test_chat_auth_error_after_text_ends_with_the_401_notice(pulsar_home):
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-fake"})
    up = Upstream({"integrate.api.nvidia.com": [(200, _err_sse(text_chunk("Hello "), AUTH_ERR))]})
    evs = events(client(up).post("/v1/messages", json=req(), headers=bearer(tok)))
    assert [n for n, _ in evs][0] == "message_start" and [n for n, _ in evs][-1] == "message_stop"
    text = text_of(evs)
    assert text.startswith("Hello ") and "rejected the API key (HTTP 401)" in text


# ---------------------------------------------------------------------------
# Round 3: a context overflow as a 200 body (JSON or a first SSE chunk) is Anthropic's prompt-too-long
# ---------------------------------------------------------------------------

def _is_overflow_400(r):
    return (r.status_code == 400 and r.json()["error"]["type"] == "invalid_request_error"
            and r.json()["error"]["message"].startswith("prompt is too long: "))


@pytest.mark.parametrize("payload", [OVF_ERR, _err_sse(OVF_ERR)], ids=["json200", "sse"])
def test_chat_overflow_in_a_200_body_becomes_prompt_too_long(pulsar_home, payload):
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-fake"})
    up = Upstream({"integrate.api.nvidia.com": [(200, payload)]})
    c = client(up)
    assert _is_overflow_400(c.post("/v1/messages", json=req(), headers=bearer(tok)))
    assert len(up.calls) == 1                       # answered at once: Claude Code needs no non-streaming retry
    if isinstance(payload, dict):
        assert _is_overflow_400(c.post("/v1/messages", json=req(stream=False), headers=bearer(tok)))


def test_chat_overflow_after_pings_is_an_error_event(pulsar_home):
    """Pings already went out, so no HTTP status can change: the overflow is an in-stream error event."""
    import asyncio

    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(0.3)
            yield _err_sse(OVF_ERR)

    async def upstream(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Slow())

    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-fake"})
    r = client(upstream).post("/v1/messages", json=req(), headers=bearer(tok))
    evs = events(r)
    assert r.status_code == 200 and evs[0][0] == "ping"
    assert evs[-1][0] == "error" and evs[-1][1]["error"]["message"].startswith("prompt is too long")
    assert "message_start" not in [n for n, _ in evs]


NATIVE_OVF = {"type": "error", "error": {"type": "invalid_request_error",
                                         "message": "prompt is too long: 250000 tokens > 200000 maximum"}}


@pytest.mark.parametrize("payload", [NATIVE_OVF, anth_events(NATIVE_OVF)], ids=["json200", "sse"])
def test_native_overflow_in_a_200_body_becomes_prompt_too_long(pulsar_home, payload):
    _, tok = setup(pulsar_home, [DS], {"deepseek": FAKE})
    up = Upstream({"api.deepseek.com": [(200, payload)]})
    c = client(up)
    r = c.post("/v1/messages", json=req(), headers=bearer(tok))
    assert _is_overflow_400(r) and r.json()["error"]["message"] == "prompt is too long: 250000 tokens > 200000 maximum"
    if isinstance(payload, dict):
        assert _is_overflow_400(c.post("/v1/messages", json=req(stream=False), headers=bearer(tok)))


def test_native_overflow_in_another_vendors_words_is_rephrased(pulsar_home):
    """Claude Code reads "prompt is too long: N tokens > M maximum"; a vendor's own wording is not that."""
    own = {"type": "error", "error": {"type": "invalid_request_error",
                                      "message": "This model's maximum context length is 131072 tokens " + FAKE}}
    _, tok = setup(pulsar_home, [DS], {"deepseek": FAKE})
    for payload in (own, anth_events(own)):
        up = Upstream({"api.deepseek.com": [(200, payload)]})
        r = client(up).post("/v1/messages", json=req(), headers=bearer(tok))
        assert _is_overflow_400(r) and FAKE not in r.text


# ---------------------------------------------------------------------------
# Round 3: the fault notice names a log only when Core writes one; non-streaming faults are not a 500
# ---------------------------------------------------------------------------

async def _broken_stream(self, sess, body, incoming):
    raise RuntimeError("boom near nvapi-fakefakefake0123")
    yield ""                                              # an async generator, as Engine.stream is


async def _broken_plain(self, sess, body, incoming):
    raise RuntimeError("boom near nvapi-fakefakefake0123")


def _fault_texts(c, tok):
    r = c.post("/v1/messages", json=req(), headers=bearer(tok))
    assert r.status_code == 200
    stream_text = text_of(events(r))
    r = c.post("/v1/messages", json=req(stream=False), headers=bearer(tok))
    assert r.status_code == 200 and r.json()["type"] == "message"
    return stream_text, r.json()["content"][0]["text"]


def test_fault_notice_without_a_log_says_how_to_record_one(pulsar_home, monkeypatch, capsys):
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-fake"})
    monkeypatch.setattr(EN.Engine, "stream", _broken_stream)
    monkeypatch.setattr(EN.Engine, "plain", _broken_plain)
    for text in _fault_texts(client(Upstream({})), tok):
        assert "internal error (RuntimeError)" in text and "PULSAR_LOG=1 pulsarcode" in text
        assert "`pulsarcode stop`" in text and "core.log" in text and "The details are in" not in text
        assert "nvapi-fakefakefake0123" not in text
    err = capsys.readouterr().err
    assert err.count("RuntimeError") >= 2 and "nvapi-fakefakefake0123" not in err


def test_fault_notice_points_at_the_log_core_writes(pulsar_home, monkeypatch):
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-fake"})
    monkeypatch.setattr(EN.Engine, "stream", _broken_stream)
    monkeypatch.setattr(EN.Engine, "plain", _broken_plain)
    log = ST.run_dir().parent / "logs" / "core.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(str(log), "a") as fh:                       # what spawn does with PULSAR_LOG=1
        monkeypatch.setattr(SV.sys, "stderr", fh)
        texts = _fault_texts(client(Upstream({})), tok)
    for text in texts:
        assert "internal error (RuntimeError)" in text and "The details are in " in text
        assert text.rstrip(".").endswith("core.log") and "PULSAR_LOG" not in text
    written = log.read_text()
    assert written.count("RuntimeError") >= 2 and "nvapi-fakefakefake0123" not in written


BAD_IMAGE = [{"role": "user", "content": [{"type": "image", "source": "abc"}, {"type": "text", "text": "hi"}]}]
BAD_RESULT = [{"role": "user", "content": "q"},
              {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]},
              {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                            "content": [{"type": "document", "source": ["abc"]}]}]}]


@pytest.mark.parametrize("messages,where", [(BAD_IMAGE, "messages.0.content.0.source"),
                                            (BAD_RESULT, "messages.2.content.0.content.0.source")])
@pytest.mark.parametrize("stream", [True, False])
def test_a_malformed_media_source_is_a_400_in_both_modes(pulsar_home, messages, where, stream):
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-fake"})
    up = Upstream({"integrate.api.nvidia.com": [(200, sse(text_chunk("never", "stop")))]})
    r = client(up).post("/v1/messages", json=req(stream=stream, messages=messages), headers=bearer(tok))
    assert r.status_code == 400 and r.json()["error"]["type"] == "invalid_request_error"
    assert r.json()["error"]["message"].startswith(where) and not up.calls


def test_translation_never_faults_on_a_malformed_media_source():
    body = {"messages": [{"role": "user", "content": [{"type": "image", "source": "abc"},
                                                      {"type": "document", "source": 5, "title": None},
                                                      {"type": "text", "text": "hi"}]},
                         {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "R", "input": {}}]},
                         {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                                       "content": [{"type": "image", "source": "abc"}]}]}]}
    out = json.dumps(T.build_request(body, "m", "nvidia", stream=True))
    assert "not forwarded to this model" in out and "[document omitted: document]" in out


# ---------------------------------------------------------------------------
# Round 3: stats name the profile that answered (two custom endpoints, one model id)
# ---------------------------------------------------------------------------

HOST_A = ST.Target("custom-a", "custom", "qwen3-coder", "https://a.example.invalid/v1")
HOST_B = ST.Target("custom-b", "custom", "qwen3-coder", "https://b.example.invalid/v1")


@pytest.mark.parametrize("stream", [True, False])
def test_stats_name_the_profile_that_answered_after_a_relay(pulsar_home, stream):
    from pulsar import statusline as SL
    _, tok = setup(pulsar_home, [HOST_A, HOST_B], {"custom-a": FAKE, "custom-b": FAKE}, {"relay": True})
    ok = sse(text_chunk("from B", "stop")) if stream else {"choices": [{"message": {"content": "from B"},
                                                                         "finish_reason": "stop"}], "usage": {}}
    up = Upstream({"a.example.invalid": [(429, {"error": {"message": "slow down"}})],
                   "b.example.invalid": [(200, ok)]})
    c = client(up)
    r = c.post("/v1/messages", json=req(stream=stream), headers=bearer(tok))
    assert "from B" in r.text and [h for h, *_ in up.calls] == ["a.example.invalid", "b.example.invalid"]
    stats = c.get("/pulsar/stats", headers=bearer(tok)).json()
    assert stats["session"]["last_profile"] == "custom-b" and stats["session"]["last_provider"] == "custom"
    assert "qwen3-coder · b.example.invalid " in SL.render({}, stats, 0, now=0)


def test_stats_profile_unit():
    st = EN.Stats()
    st.begin("r1", "s1", "qwen3-coder", "custom", profile="custom-a")
    st.relay("r1", "custom-a|qwen3-coder", "custom|qwen3-coder", "HTTP 429", profile="custom-b")
    st.end("r1", ok=True)
    assert st.by_session["s1"].last_profile == "custom-b"
    st.begin("r2", "s1", "m", "groq")                     # callers that name no profile still work
    st.end("r2", ok=True)
    assert st.snapshot("s1")["session"]["last_profile"] == ""
