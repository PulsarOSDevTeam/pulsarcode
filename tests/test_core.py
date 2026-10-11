# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Pulsar Core against scripted upstreams (httpx.MockTransport). No sockets."""
import asyncio
import json
import re

import httpx
import pytest
from starlette.testclient import TestClient

from pulsar import config as K
from pulsar.core import state as ST
from pulsar.core.engine import Tuning, detect_loop
from pulsar.core.server import make_app

ADMIN = ST.ADMIN_TOKEN_PREFIX + "admin-test"


def sse(*chunks):
    return ("".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n").encode()


def text_chunk(text, finish=None):
    return {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": finish}]}


def anth_stream(text):
    evs = [("message_start", {"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant",
                                                                    "content": [], "model": "x", "stop_reason": None,
                                                                    "usage": {"input_tokens": 77, "output_tokens": 1}}}),
           ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
           ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}),
           ("content_block_stop", {"type": "content_block_stop", "index": 0}),
           ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}}),
           ("message_stop", {"type": "message_stop"})]
    return "".join("event: %s\ndata: %s\n\n" % (n, json.dumps(d)) for n, d in evs).encode()


class Upstream:
    """Routes by host; each host has a script of (status, body) consumed in order (last one repeats)."""

    def __init__(self, scripts):
        self.scripts = {h: list(s) for h, s in scripts.items()}
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        body = json.loads(request.content) if request.content else None
        self.calls.append((host, request.url.path, dict(request.headers), body))
        script = self.scripts.get(host)
        if not script:
            raise httpx.ConnectError("no route to " + host)
        status, payload = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(payload, Exception):
            raise payload
        if isinstance(payload, bytes):
            return httpx.Response(status, content=payload, headers={"content-type": "text/event-stream"})
        return httpx.Response(status, json=payload)


def setup(tmp, plan, keys=None, flags=None):
    K.ensure_home()
    for prof, key in (keys or {}).items():
        K.set_key(prof, key)
    sess, token = ST.new_session(plan, flags or {})
    return sess, token


def client(up, port=0, ping_interval=0.05):
    tun = Tuning(header_timeout=5, header_timeout_reasoning=5, first_token_timeout=5,
                 first_token_timeout_reasoning=5, ping_interval=ping_interval, retries=0)
    return TestClient(make_app(ADMIN, port, transport=httpx.MockTransport(up), tuning=tun))


def events(resp):
    out = []
    for block in resp.text.strip().split("\n\n"):
        name, data = "", None
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
        if name:
            out.append((name, data))
    return out


def text_of(evs):
    return "".join((p.get("delta") or {}).get("text", "") for n, p in evs if n == "content_block_delta")


def req(model="claude-sonnet-x", stream=True, **kw):
    return {"model": model, "max_tokens": 1000, "stream": stream, "messages": [{"role": "user", "content": "hi"}], **kw}


NV = ST.Target("nvidia", "nvidia", "moonshotai/kimi-k3")
GROQ = ST.Target("groq", "groq", "openai/gpt-oss-120b")
DS = ST.Target("deepseek", "deepseek", "deepseek-chat")


def test_unknown_token_rejected(pulsar_home):
    c = client(Upstream({}))
    r = c.post("/v1/messages", json=req(), headers={"authorization": "Bearer pls_nope"})
    assert r.status_code == 401


def test_stream_routes_to_primary_and_maps_claude_names(pulsar_home):
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-k"})
    up = Upstream({"integrate.api.nvidia.com": [(200, sse(text_chunk("Hel"), text_chunk("lo", "stop")))]})
    r = client(up).post("/v1/messages", json=req(), headers={"authorization": "Bearer " + tok})
    assert text_of(events(r)) == "Hello"
    host, path, headers, body = up.calls[0]
    assert body["model"] == "moonshotai/kimi-k3" and headers["authorization"] == "Bearer nvapi-k"


def test_relay_on_429_to_next_target(pulsar_home):
    _, tok = setup(pulsar_home, [NV, GROQ], {"nvidia": "nvapi-k", "groq": "gsk_k"})
    up = Upstream({"integrate.api.nvidia.com": [(429, {"error": {"message": "slow"}})],
                   "api.groq.com": [(200, sse(text_chunk("relayed", "stop")))]})
    c = client(up)
    r = c.post("/v1/messages", json=req(), headers={"authorization": "Bearer " + tok})
    evs = events(r)
    assert text_of(evs) == "relayed"
    assert [n for n, _ in evs].count("message_start") == 1
    assert [h for h, *_ in up.calls] == ["integrate.api.nvidia.com", "api.groq.com"]
    # breaker now open for nvidia: next request goes straight to groq
    c.post("/v1/messages", json=req(), headers={"authorization": "Bearer " + tok})
    assert up.calls[-1][0] == "api.groq.com"
    stats = c.get("/pulsar/stats", headers={"authorization": "Bearer " + ADMIN}).json()
    assert stats["total"]["relays"] >= 1 and stats["breakers"]


def test_all_targets_fail_gives_notice(pulsar_home):
    _, tok = setup(pulsar_home, [NV, GROQ], {"nvidia": "nvapi-k", "groq": "gsk_k"})
    up = Upstream({"integrate.api.nvidia.com": [(503, {"error": {"message": "down"}})],
                   "api.groq.com": [(401, {"error": {"message": "bad key"}})]})
    text = text_of(events(client(up).post("/v1/messages", json=req(), headers={"authorization": "Bearer " + tok})))
    assert "Pulsar Code" in text and "rejected the API key" in text and "2 relay targets" in text


def test_native_passthrough_is_verbatim_with_real_usage(pulsar_home):
    _, tok = setup(pulsar_home, [DS], {"deepseek": "sk-ds"})
    up = Upstream({"api.deepseek.com": [(200, anth_stream("native hi"))]})
    c = client(up)
    r = c.post("/v1/messages", json=req(thinking={"type": "adaptive"}),
               headers={"authorization": "Bearer " + tok, "anthropic-version": "2023-06-01"})
    evs = events(r)
    assert text_of(evs) == "native hi"
    assert evs[0][0] == "message_start" and evs[0][1]["message"]["usage"]["input_tokens"] == 77
    host, path, headers, body = up.calls[0]
    assert path == "/anthropic/v1/messages" and headers["authorization"] == "Bearer sk-ds"
    assert body["model"] == "deepseek-chat" and body["thinking"] == {"type": "adaptive"}


def test_native_heals_unknown_field(pulsar_home):
    _, tok = setup(pulsar_home, [DS], {"deepseek": "sk-ds"})
    up = Upstream({"api.deepseek.com": [(400, {"error": {"message": "Unknown field: context_management"}}),
                                        (200, anth_stream("ok"))]})
    r = client(up).post("/v1/messages", json=req(context_management={"edits": []}),
                        headers={"authorization": "Bearer " + tok})
    assert text_of(events(r)) == "ok"
    assert "context_management" in up.calls[0][3] and "context_management" not in up.calls[1][3]


def test_native_midstream_drop_is_closed_cleanly(pulsar_home):
    _, tok = setup(pulsar_home, [DS], {"deepseek": "sk-ds"})
    partial = anth_stream("part").split(b"event: content_block_stop")[0]
    up = Upstream({"api.deepseek.com": [(200, partial)]})
    evs = events(client(up).post("/v1/messages", json=req(), headers={"authorization": "Bearer " + tok}))
    names = [n for n, _ in evs]
    assert names[-1] == "message_stop" and names.count("content_block_stop") == 2
    assert "cut off" in text_of(evs)


OVERFLOW_TEXT = re.compile(r"^prompt is too long: \d+ tokens > \d+ maximum$")


def is_overflow_error(r):
    """Anthropic's own context-overflow answer, the one Claude Code compacts on: a real HTTP 400
    invalid_request_error whose message reads "prompt is too long: N tokens > M maximum"."""
    if r.status_code != 400:
        return False
    body = r.json()
    return (body.get("type") == "error" and body["error"]["type"] == "invalid_request_error"
            and OVERFLOW_TEXT.match(body["error"]["message"]) is not None)


def test_context_overflow_becomes_anthropic_error(pulsar_home):
    # Before the first byte, a streaming request gets the same real HTTP 400 as a non-streaming one: after an
    # SSE error event Claude Code re-sends the whole request without streaming before it compacts.
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-k"})
    up = Upstream({"integrate.api.nvidia.com": [(400, {"error": {"message": "maximum context length is 8192 tokens"}})]})
    c = client(up, ping_interval=30)          # no ping goes out first: nothing was sent, so a real HTTP 400
    for stream in (True, False):
        r = c.post("/v1/messages", json=req(stream=stream), headers={"authorization": "Bearer " + tok})
        assert r.status_code == 400 and r.json()["error"]["type"] == "invalid_request_error"
        assert r.json()["error"]["message"].startswith("prompt is too long")

    async def slow(request):                  # a ping already went out: the overflow ends the SSE stream
        await asyncio.sleep(0.3)
        return up(request)
    evs = events(client(slow).post("/v1/messages", json=req(), headers={"authorization": "Bearer " + tok}))
    assert evs[0][0] == "ping" and evs[-1][0] == "error"
    assert evs[-1][1]["error"]["message"].startswith("prompt is too long")


# One wording per vendor (OpenAI, Anthropic-style, Gemini, llama.cpp, vLLM, TGI, ...): the corpus of
# tests/test_translate.py::test_context_overflow_detection_across_vendors.
VENDOR_OVERFLOWS = [
    (400, "This model's maximum context length is 32768 tokens. However, your messages resulted in 40000 tokens."),
    (400, '{"error":{"code":"context_length_exceeded","message":"..."}}'),
    (400, "prompt is too long: 250000 tokens > 200000 maximum"),
    (400, "The input token count (1200000) exceeds the maximum number of tokens allowed (1048576)."),
    (400, "the request exceeds the available context size, try increasing it"),
    (400, "Prompt contains 40000 tokens and 0 draft tokens, too large for model with 32768 maximum context length"),
    (400, "This model's maximum prompt length is 131072 but the request contains 150000 tokens."),
    (400, "Invalid request: Your request exceeded model token limit: 8192"),
    (413, "Input is too long for requested model."),
    (422, "Input validation error: `inputs` tokens + `max_new_tokens` must be <= 4096. Given: 5000 `inputs` tokens"),
    (500, "context size has been exceeded"),
]


@pytest.mark.parametrize("status,message", VENDOR_OVERFLOWS)
def test_every_vendor_overflow_becomes_prompt_is_too_long(pulsar_home, status, message):
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-k"})
    up = Upstream({"integrate.api.nvidia.com": [(status, {"error": {"message": message}})]})
    c = client(up, ping_interval=30)
    for stream in (True, False):
        r = c.post("/v1/messages", json=req(stream=stream), headers={"authorization": "Bearer " + tok})
        assert r.status_code == 400 and r.json()["error"]["type"] == "invalid_request_error", (stream, r.text[:300])
        assert r.json()["error"]["message"].startswith("prompt is too long"), stream


def test_a_rate_limit_that_names_tokens_is_not_an_overflow(pulsar_home):
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-k"})
    tpm = "Request too large for model in organization on tokens per minute (TPM): Limit 6000, Requested 9000"
    up = Upstream({"integrate.api.nvidia.com": [(413, {"error": {"message": tpm}})]})
    r = client(up, ping_interval=30).post("/v1/messages", json=req(stream=False),
                                          headers={"authorization": "Bearer " + tok})
    assert r.status_code == 200 and "prompt is too long" not in r.text and "HTTP 413" in r.text


@pytest.mark.parametrize("message", [
    "max_tokens is too large for model qwen/qwen3-coder-480b-a35b-instruct; at most 8192",
    "max_output_tokens exceeds the model's output token limit of 8192",
    "The completion exceeds the maximum output length of 8192 tokens",
])
def test_an_output_cap_error_heals_and_is_never_an_overflow(pulsar_home, message):
    # The answer budget was too big, not the prompt: a smaller max_tokens is sent, Claude Code never compacts.
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-k"})
    for stream in (True, False):
        up = Upstream({"integrate.api.nvidia.com": [(400, {"error": {"message": message}}),
                                                    (200, sse(text_chunk("fits", "stop")) if stream else
                                                     {"choices": [{"message": {"content": "fits"}, "finish_reason": "stop"}]})]})
        r = client(up, ping_interval=30).post("/v1/messages", json=req(stream=stream, max_tokens=32000),
                                              headers={"authorization": "Bearer " + tok})
        assert r.status_code == 200 and "fits" in r.text and "prompt is too long" not in r.text, stream
        assert up.calls[-1][3]["max_tokens"] <= 8192, stream


def test_overflow_message_states_a_limit_it_knows():
    from pulsar import translate as T
    from pulsar.core.engine import overflow_message
    body = req()
    est = T.estimate_tokens(body)
    assert overflow_message(body, 8192) == "prompt is too long: 8193 tokens > 8192 maximum"
    for detail, limit in (("This model's maximum context length is 65536 tokens.", 65536),
                          ("prompt is too long: 250000 tokens > 200000 maximum", 200000),
                          ("exceeds the maximum number of tokens allowed (1048576).", 1048576),
                          ("too large for model with 32768 maximum context length", 32768),
                          ("This model's maximum prompt length is 131072 but the request contains", 131072),
                          ("Your request exceeded model token limit: 8192", 8192),
                          ("`inputs` tokens + `max_new_tokens` must be <= 4096. Given: 5000", 4096)):
        assert overflow_message(body, 0, detail) == "prompt is too long: %d tokens > %d maximum" % (limit + 1, limit)
    # the target's known window comes first; with neither, the estimate (never a figure from elsewhere)
    assert overflow_message(body, 8192, "maximum context length is 65536").endswith("> 8192 maximum")
    assert overflow_message(body, 0, "context size has been exceeded") == \
        "prompt is too long: %d tokens > %d maximum" % (est, est - 1)


def test_hot_swap_by_rewriting_session_file(pulsar_home):
    sess, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-k", "groq": "gsk_k"})
    up = Upstream({"integrate.api.nvidia.com": [(200, sse(text_chunk("a", "stop")))],
                   "api.groq.com": [(200, sse(text_chunk("b", "stop")))]})
    c = client(up)
    c.post("/v1/messages", json=req(), headers={"authorization": "Bearer " + tok})
    import os
    import time
    ST.update_plan(sess.sid, [GROQ])
    p = ST.session_path(sess.sid)
    os.utime(p, (time.time() + 2, time.time() + 2))
    r = c.post("/v1/messages", json=req(), headers={"authorization": "Bearer " + tok})
    assert text_of(events(r)) == "b" and up.calls[-1][0] == "api.groq.com"


def test_live_model_switch_from_claude_code(pulsar_home):
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-k"})
    up = Upstream({"integrate.api.nvidia.com": [(200, sse(text_chunk("x", "stop")))]})
    client(up).post("/v1/messages", json=req(model="z-ai/glm-5.3"), headers={"authorization": "Bearer " + tok})
    assert up.calls[0][3]["model"] == "z-ai/glm-5.3"


def test_loop_guard():
    step = {"role": "assistant", "content": [{"type": "tool_use", "id": "x", "name": "Bash", "input": {"c": "ls"}}]}
    res = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "same"}]}
    body = {"messages": [{"role": "user", "content": "go"}] + [step, res] * 4}
    assert detect_loop(body)
    body2 = {"messages": [{"role": "user", "content": "go"}] + [step, res] * 2}
    assert detect_loop(body2) is None


def test_loop_guard_same_reply_to_the_same_message():
    reply = {"role": "assistant", "content": [{"type": "text", "text": "Done."}]}

    def said(text, k):
        return {"role": "user", "content": [{"type": "text", "text": "<system-reminder>turn %d</system-reminder>" % k},
                                            {"type": "text", "text": text}]}
    four = [{"role": "user", "content": "go"}] + [m for k in range(4) for m in (reply, said("continue", k))]
    assert detect_loop({"messages": four})                         # reminders differ, what was said does not
    assert detect_loop({"messages": four[:-2]}) is None            # three times is not yet a loop
    asked = [{"role": "user", "content": "go"}] + [m for k in range(4) for m in (reply, said("q%d" % k, k))]
    assert detect_loop({"messages": asked}) is None                # same answer, different questions
    worked = [{"role": "user", "content": "go"}]
    for k in range(4):
        worked += [{"role": "assistant", "content": [{"type": "tool_use", "id": "i%d" % k, "name": "Bash",
                                                      "input": {"c": "date"}}]},
                   {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "i%d" % k, "content": str(k)}]},
                   reply, said("continue", k)]
    assert detect_loop({"messages": worked}) is None               # its tool results differ: real progress


def test_loop_guard_sees_through_claude_codes_system_turns():
    # For a model id Claude Code does not know (no behavesAs row), it sends a role:"system" message after every
    # human message: [user, system, assistant, user, system, ...]. The loop is the same one.
    reply = {"role": "assistant", "content": [{"type": "text", "text": "Same reply."}]}

    def turn(text, k):
        return [{"role": "user", "content": text},
                {"role": "system", "content": "<total_tokens>%d tokens left</total_tokens>" % (15000000 - k)}]
    first = [{"role": "user", "content": [{"type": "text", "text": "<system-reminder>\nctx\n</system-reminder>"},
                                          {"type": "text", "text": "continue"}]},
             {"role": "system", "content": "# Environment\n..."}]
    four = first + [m for k in range(4) for m in [reply] + turn("continue", k)]
    assert detect_loop({"messages": four})
    assert detect_loop({"messages": four[:-3]}) is None            # three times is not yet a loop
    asked = first + [m for k in range(4) for m in [reply] + turn("q%d" % k, k)]
    assert detect_loop({"messages": asked}) is None                # same answer, different questions
    # Within a turn too: the same tool call with the same result, system turns in between.
    call = {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "Bash", "input": {"c": "ls"}}]}
    result = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "same"}]}
    same_call = first + [m for _ in range(4) for m in (call, result, {"role": "system", "content": "s"})]
    assert detect_loop({"messages": same_call})
    # A reply in progress (the last message is the model's) answers no human message: not the cross-turn rule.
    assert detect_loop({"messages": four + [reply]}) is None


def test_loop_guard_on_claude_codes_real_body_for_an_unlisted_model(pulsar_home):
    # The body shape Claude Code 2.1.287 sends for an unlisted model on the 6th `claude -p --continue continue`.
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-k"})
    up = Upstream({})
    reply = {"role": "assistant", "content": [{"type": "text", "text": "Same reply."}]}
    tokens = {"role": "system", "content": "<total_tokens>15000000 tokens left</total_tokens>"}
    messages = [{"role": "user", "content": [{"type": "text", "text": "<system-reminder>\nx\n</system-reminder>"},
                                             {"type": "text", "text": "continue"}]},
                {"role": "system", "content": "# Environment"}]
    for k in range(5):
        messages += [reply, {"role": "user", "content": "continue" if k < 4 else [{"type": "text", "text": "continue"}]},
                     tokens]
    h = {"authorization": "Bearer " + tok}
    c = client(up)
    assert "runaway loop" in text_of(events(c.post("/v1/messages", json=req(messages=messages), headers=h)))
    r = c.post("/v1/messages", json=req(messages=messages, stream=False), headers=h)
    assert r.status_code == 200 and "runaway loop" in r.json()["content"][0]["text"]
    assert not up.calls


def test_loop_guard_answers_without_calling_upstream(pulsar_home):
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-k"})
    up = Upstream({})
    # the same call with the same result four times since the last human message (detect_loop)
    step = {"role": "assistant", "content": [{"type": "tool_use", "id": "x", "name": "Bash", "input": {"c": "ls"}}]}
    res = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "same"}]}
    body = req(messages=[{"role": "user", "content": "go"}] + [step, res] * 4)
    text = text_of(events(client(up).post("/v1/messages", json=body, headers={"authorization": "Bearer " + tok})))
    assert "runaway loop" in text and not up.calls


def test_guards_host_origin_and_body(pulsar_home):
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-k"})
    c = client(Upstream({}), port=4747)
    h = {"authorization": "Bearer " + tok, "host": "evil.example:4747"}
    assert c.post("/v1/messages", json=req(), headers=h).status_code == 421
    h = {"authorization": "Bearer " + tok, "host": "127.0.0.1:4747", "origin": "https://evil.example"}
    assert c.post("/v1/messages", json=req(), headers=h).status_code == 403
    assert c.get("/healthz", headers={"host": "127.0.0.1:4747"}).status_code == 200


def test_stage_bus_roundtrip(pulsar_home):
    sess, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-k"})
    c = client(Upstream({}))
    h = {"authorization": "Bearer " + tok}
    r = c.post("/pulsar/stage", json={"scene": {"slides": [{"title": "One"}, {"title": "Two", "body": "- a"}]}}, headers=h)
    assert r.status_code == 200 and r.json()["ok"]
    bad = c.post("/pulsar/stage", json={"scene": {"kind": "chart"}}, headers=h)
    assert bad.status_code == 400 and "series" in bad.json()["error"]["message"]
    scenes = c.get("/pulsar/stage/scenes", headers=h).json()["scenes"]
    assert scenes[-1]["scene"]["slides"][1]["layout"] == "bullets"
    view = ST.view_token(ADMIN, "")
    assert c.get("/stage?t=" + view).status_code == 200
    assert c.get("/stage?t=nope").status_code == 401
    assert c.post("/pulsar/stage", json={"scene": {"title": "x"}}).status_code == 401


def test_council_fans_out(pulsar_home):
    # The default council is the session's other models (plan[1:]): the primary is the model asking.
    cer = ST.Target("cerebras", "cerebras", "zai-glm-4.6")
    st = K.load()
    st.upsert(K.Profile(id="nvidia", provider="nvidia", model="moonshotai/kimi-k3"))
    st.upsert(K.Profile(id="groq", provider="groq", model="openai/gpt-oss-120b"))
    st.upsert(K.Profile(id="cerebras", provider="cerebras", model="zai-glm-4.6"))
    K.save(st)
    # the default council is the session's other models: the primary (Cerebras here) is the one asking
    _, tok = setup(pulsar_home, [cer, NV, GROQ], {"nvidia": "nvapi-k", "groq": "gsk_k"})
    ok = {"choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}], "usage": {}}
    up = Upstream({"integrate.api.nvidia.com": [(200, ok)], "api.cerebras.ai": [(200, ok)],
                   "api.groq.com": [(500, {"error": {"message": "x"}})]})
    r = client(up).post("/pulsar/council", json={"prompt": "2+2?"}, headers={"authorization": "Bearer " + tok})
    ans = {a["provider"]: a for a in r.json()["answers"]}
    assert ans["nvidia"]["ok"] and ans["nvidia"]["text"] == "answer"
    assert not ans["groq"]["ok"]
    assert "cerebras" not in ans and sorted(h for h, *_ in up.calls) == ["api.groq.com", "integrate.api.nvidia.com"]


def test_council_of_one_connected_model_asks_two_of_its_providers_picks(pulsar_home):
    # With one connected provider the default council was only the model already answering, retold to the user
    # as "the council's verdict". Two of the provider's recommended models join it, each from another vendor
    # (a second model of the same lab is not a second opinion): same profile, same key.
    from pulsar import catalog as C
    st = K.load()
    st.upsert(K.Profile(id="nvidia", provider="nvidia", model="moonshotai/kimi-k3"))
    K.save(st)
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-k"})
    C.write_cache("nvidia", [C.Model(m, "nvidia") for m in (
        "google/gemma-4-31b-it", "moonshotai/kimi-k3", "moonshotai/kimi-k2.6", "z-ai/glm-5", "deepseek-ai/deepseek-v4")])
    ok = {"choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}], "usage": {}}
    up = Upstream({"integrate.api.nvidia.com": [(200, ok), (200, ok), (200, ok)]})
    r = client(up).post("/pulsar/council", json={"prompt": "2+2?"}, headers={"authorization": "Bearer " + tok})
    assert sorted(a["model"] for a in r.json()["answers"]) == ["deepseek-ai/deepseek-v4", "moonshotai/kimi-k3", "z-ai/glm-5"]
    assert {h for h, *_ in up.calls} == {"integrate.api.nvidia.com"}             # no other provider was asked


class Counted(httpx.AsyncByteStream):
    """An upstream body of `total` bytes (`unit` repeated) in `chunk`-byte pieces; `sent` counts what Core read."""

    def __init__(self, unit, total, chunk=16384, delay=0.0):
        self.piece = (unit * (chunk // len(unit) + 1))[:chunk]
        self.total, self.delay, self.sent = total, delay, 0

    async def __aiter__(self):
        while self.sent < self.total:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.sent += len(self.piece)
            yield self.piece

    async def aclose(self):
        pass


def counted(status, unit, total, ctype="application/json", **kw):
    body = Counted(unit, total, **kw)
    return body, lambda request: httpx.Response(status, stream=body, headers={"content-type": ctype})


def test_upstream_bodies_are_read_with_a_cap(pulsar_home, monkeypatch):
    import pulsar.core.engine as E
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-k", "deepseek": "sk-ds"})
    h = {"authorization": "Bearer " + tok}
    huge = 16 << 20                                    # far beyond every cap below
    body, up = counted(500, b"x", huge)                # an error answer: only its head is read
    r = client(up).post("/v1/messages", json=req(stream=False), headers=h)
    assert r.status_code == 200 and "having trouble (HTTP 500)" in r.text
    assert body.sent <= E.MAX_ERROR_BODY + (64 << 10)
    for name in ("MAX_REPLY_BODY", "MAX_LINE", "MAX_EVENT"):
        monkeypatch.setattr(E, name, 256 << 10)
    body, up = counted(200, b"x", huge)                # a whole answer larger than the cap
    r = client(up).post("/v1/messages", json=req(stream=False), headers=h)
    assert r.status_code == 200 and "larger than" in r.text and body.sent <= (512 << 10)
    body, up = counted(200, b"x", huge, "text/event-stream")      # a stream line that never ends
    evs = events(client(up).post("/v1/messages", json=req(), headers=h))
    assert evs[-1][0] == "message_stop" and "cut off" in text_of(evs) and body.sent <= (512 << 10)
    _, dtok = setup(pulsar_home, [DS], {"deepseek": "sk-ds"})
    body, up = counted(200, b'data: {"type": "ping"}\n', huge, "text/event-stream")   # an event that never ends
    evs = events(client(up).post("/v1/messages", json=req(), headers={"authorization": "Bearer " + dtok}))
    assert evs[-1][0] == "message_stop" and body.sent <= (512 << 10)


def test_council_timeout_covers_the_whole_answer(pulsar_home):
    from pulsar.core.engine import Engine
    from pulsar.core.stats import Stats
    body, up = counted(200, b" ", 120, chunk=1, delay=0.05)        # headers at once, then a byte every 50 ms
    eng = Engine(Stats(), lambda p: "gsk_k", pulsar_home, transport=httpx.MockTransport(up))

    async def run():
        try:
            return await eng.ask(GROQ, "q", timeout=0.5)
        finally:
            await eng.aclose()
    ans = asyncio.run(run())
    assert not ans["ok"] and ans["error"] == "TimeoutError" and body.sent < 120


def test_council_answers_an_unknown_provider_as_one_error(pulsar_home):
    st = K.load()
    st.upsert(K.Profile(id="future", provider="provider-from-a-newer-release", model="m"))
    st.upsert(K.Profile(id="groq", provider="groq", model=GROQ.model))
    K.save(st)
    _, tok = setup(pulsar_home, [GROQ], {"groq": "gsk_k"})
    ok = {"choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}], "usage": {}}
    r = client(Upstream({"api.groq.com": [(200, ok)]})).post(
        "/pulsar/council", json={"prompt": "q", "models": ["future:m", "groq:" + GROQ.model]},
        headers={"authorization": "Bearer " + tok})
    assert r.status_code == 200
    ans = r.json()["answers"]
    assert [a["ok"] for a in ans] == [False, True] and "unknown provider" in ans[0]["error"]


def test_stats_scoping(pulsar_home):
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-k"})
    c = client(Upstream({"integrate.api.nvidia.com": [(200, sse(text_chunk("x", "stop")))]}))
    c.post("/v1/messages", json=req(), headers={"authorization": "Bearer " + tok})
    mine = c.get("/pulsar/stats", headers={"authorization": "Bearer " + tok}).json()
    assert mine["session"]["requests"] == 1 and "sessions" not in mine
    assert c.get("/pulsar/stats").status_code == 401
    assert "nvapi-k" not in json.dumps(c.get("/pulsar/stats", headers={"authorization": "Bearer " + ADMIN}).json())


@pytest.mark.parametrize("flags", [{"relay": False}])
def test_relay_can_be_disabled(pulsar_home, flags):
    _, tok = setup(pulsar_home, [NV, GROQ], {"nvidia": "nvapi-k", "groq": "gsk_k"}, flags)
    up = Upstream({"integrate.api.nvidia.com": [(429, {"error": {"message": "slow"}})],
                   "api.groq.com": [(200, sse(text_chunk("relayed", "stop")))]})
    text = text_of(events(client(up).post("/v1/messages", json=req(), headers={"authorization": "Bearer " + tok})))
    assert "rate-limiting" in text and all(h == "integrate.api.nvidia.com" for h, *_ in up.calls)


def test_a_session_with_pilot_armed_never_relays(pulsar_home):
    # Pilot sends screenshots of the user's screen: they go only to the model the user chose.
    _, tok = setup(pulsar_home, [NV, GROQ], {"nvidia": "nvapi-k", "groq": "gsk_k"}, {"relay": True, "pilot": True})
    up = Upstream({"integrate.api.nvidia.com": [(429, {"error": {"message": "slow"}})],
                   "api.groq.com": [(200, sse(text_chunk("relayed", "stop")))]})
    c = client(up)
    for stream in (True, False):
        r = c.post("/v1/messages", json=req(stream=stream), headers={"authorization": "Bearer " + tok})
        assert "rate-limiting" in r.text and "relayed" not in r.text, stream
    assert up.calls and all(h == "integrate.api.nvidia.com" for h, *_ in up.calls)


def test_openai_heal_is_persisted_per_model(pulsar_home):
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-k"})
    up = Upstream({"integrate.api.nvidia.com": [(400, {"error": {"message": "max_tokens must be <= 8192"}}),
                                                (200, sse(text_chunk("fine", "stop")))]})
    c = client(up)
    big = req(max_tokens=32000)
    assert text_of(events(c.post("/v1/messages", json=big, headers={"authorization": "Bearer " + tok}))) == "fine"
    assert [b["max_tokens"] for *_, b in up.calls] == [16384, 8192]
    heals = json.loads((pulsar_home / "cache" / "heals-nvidia.json").read_text())
    assert heals["moonshotai/kimi-k3"]["max_tokens_cap"] == 8192
    c.post("/v1/messages", json=big, headers={"authorization": "Bearer " + tok})
    assert up.calls[-1][3]["max_tokens"] == 8192


def test_single_target_transient_retry(pulsar_home):
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-k"})
    up = Upstream({"integrate.api.nvidia.com": [(503, {"error": {"message": "busy"}}), (200, sse(text_chunk("ok", "stop")))]})
    tun = Tuning(header_timeout=5, header_timeout_reasoning=5, first_token_timeout=5,
                 first_token_timeout_reasoning=5, ping_interval=0.05, retries=1)
    c = TestClient(make_app(ADMIN, 0, transport=httpx.MockTransport(up), tuning=tun))
    import pulsar.core.engine as E
    orig = E.Breakers.fail
    E.Breakers.fail = lambda self, key, f: (orig(self, key, f), 0.01)[1]
    try:
        text = text_of(events(c.post("/v1/messages", json=req(), headers={"authorization": "Bearer " + tok})))
    finally:
        E.Breakers.fail = orig
    assert text == "ok" and len(up.calls) == 2


@pytest.mark.parametrize("status,needle", [(404, "does not serve"), (402, "out of credits"), (503, "having trouble"),
                                           (429, "rate-limiting")])
def test_notice_matrix(pulsar_home, status, needle):
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-k"})
    up = Upstream({"integrate.api.nvidia.com": [(status, {"error": {"message": "upstream says no"}})]})
    text = text_of(events(client(up).post("/v1/messages", json=req(), headers={"authorization": "Bearer " + tok})))
    assert needle in text and "NVIDIA NIM" in text and "upstream says no" in text


def test_non_stream_tools_and_models_and_count(pulsar_home):
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-k"})
    body = {"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "Bash", "arguments": '{"command":"pwd"}'}}]},
        "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 3, "completion_tokens": 4}}
    up = Upstream({"integrate.api.nvidia.com": [(200, body)]})
    c = client(up)
    h = {"authorization": "Bearer " + tok}
    msg = c.post("/v1/messages", json=req(stream=False, tools=[{"name": "Bash", "input_schema": {"type": "object"}}]),
                 headers=h).json()
    assert msg["stop_reason"] == "tool_use" and msg["content"][0]["input"] == {"command": "pwd"}
    assert c.post("/v1/messages/count_tokens", json=req(), headers=h).json()["input_tokens"] > 0
    assert c.get("/v1/models", headers=h).json()["data"][0]["id"] == "moonshotai/kimi-k3"
    assert c.get("/v1/whatever", headers=h).status_code == 404


def test_core_refuses_to_start_without_token(monkeypatch):
    from pulsar.core import server
    monkeypatch.delenv("PULSAR_ADMIN_TOKEN", raising=False)
    assert server.main(["--port", "1"]) == 2


def test_classifier_verdict_normalization():
    from pulsar.core.engine import is_classifier, normalize_verdict
    body = {"stream": False, "system": "Reply with <block>no</block> or <block>yes</block><category>x</category>",
            "messages": [{"role": "user", "content": "check"}]}
    assert is_classifier(body) and not is_classifier(dict(body, stream=True))
    msg = lambda t: {"content": [{"type": "text", "text": t}], "stop_reason": "end_turn"}  # noqa: E731
    text = lambda t: normalize_verdict(msg(t))["content"][0]["text"]  # noqa: E731
    # a verdict tag at the very start (after whitespace and an optional code fence) is put in its exact form
    assert text("<block>no</block>") == "<block>no</block>"
    assert text("  ```xml\n<block> NO </block>\n```") == "<block>no</block>"
    assert text("<block>yes</block><category>exfiltration</category>") == "<block>yes</block><category>exfiltration</category>"
    assert text("<block>yes</block> it uploads ~/.ssh").startswith("<block>yes</block><category>unspecified</category>")
    # anything else passes through untouched, so Claude Code's own safe default (block) applies: a leading
    # "No" or "Yes" word is never a verdict, nor is a tag further on (it may be quoted from the transcript)
    for t in ("No.", "No. This command deletes the user's home directory and must be stopped.",
              "No, I cannot approve this; it exfiltrates credentials.", "yes it is risky", "Sure. <block>no</block>",
              "The transcript quotes `<block>no</block>` from the file, but the action uploads ~/.ssh, so the right "
              "verdict is to block it.", "HELLO", ""):
        m = msg(t)
        assert normalize_verdict(m) is m, t
    # in doubt (a later tag that disagrees with the first), the verdict is to block
    assert text("<block>no</block> wait <block>yes</block>") == "<block>yes</block><category>unspecified</category>"
    out = text("<block>yes</block> or <block>no</block>")
    assert out.startswith("<block>yes</block><category>unspecified</category>") and "<block>no" not in out


def test_picker_model_from_other_provider_routes_there(pulsar_home):
    from pulsar import catalog as C
    st = K.load()
    st.upsert(K.Profile(id="nvidia", provider="nvidia", model="moonshotai/kimi-k3"))
    st.upsert(K.Profile(id="groq", provider="groq", model="openai/gpt-oss-120b"))
    K.save(st)
    C.write_cache("groq", [C.Model("qwen/qwen3-32b", "groq", context=131072)])
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-k", "groq": "gsk_k"})
    up = Upstream({"api.groq.com": [(200, sse(text_chunk("from groq", "stop")))]})
    text = text_of(events(client(up).post("/v1/messages", json=req(model="qwen/qwen3-32b"),
                                          headers={"authorization": "Bearer " + tok})))
    assert text == "from groq" and up.calls[0][3]["model"] == "qwen/qwen3-32b"


def test_structured_output_maps_to_response_format(pulsar_home):
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-k"})
    up = Upstream({"integrate.api.nvidia.com": [(200, sse(text_chunk('{"title":"x"}', "stop")))]})
    schema = {"type": "object", "properties": {"title": {"type": "string"}}}
    client(up).post("/v1/messages", json=req(output_config={"format": {"type": "json_schema", "schema": schema}}),
                    headers={"authorization": "Bearer " + tok})
    assert up.calls[0][3]["response_format"]["json_schema"]["schema"] == schema


CALL = '<tool_call>{"name": "Bash", "arguments": {"command": "ls"}}</tool_call>'
BASH = [{"name": "Bash", "description": "run", "input_schema": {"type": "object",
                                                                 "properties": {"command": {"type": "string"}}}}]
RESCUED = [("tool_use", "Bash", {"command": "ls"})]


def blocks_of(resp, stream):
    """(type, name or text, input) for each content block of a reply, streamed or not."""
    if not stream:
        return [(b["type"], b.get("name") or b.get("text"), b.get("input")) for b in resp.json()["content"]]
    out = []
    for n, p in events(resp):
        if n == "content_block_start":
            out.append([p["content_block"]["type"], p["content_block"].get("name") or "", None])
        elif n == "content_block_delta" and p["delta"]["type"] == "text_delta":
            out[-1][1] += p["delta"]["text"]
        elif n == "content_block_delta" and p["delta"]["type"] == "input_json_delta":
            out[-1][2] = json.loads(p["delta"]["partial_json"])
    return [tuple(b) for b in out]


def answer(stream, delta):
    """One reply carrying `delta` (content and/or reasoning_content), as a stream or a whole completion."""
    if stream:
        return sse({"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}, text_chunk("", "stop"))
    return {"choices": [{"message": dict({"role": "assistant", "content": ""}, **delta), "finish_reason": "stop"}]}


def test_text_calls_are_rescued_where_the_host_needs_it(pulsar_home):
    # Only Core knows the host. A custom endpoint, and any request whose tools went into the prompt, are searched for
    # tool calls sent as text whatever the model id; elsewhere a model that calls tools natively is never searched.
    custom = ST.Target("custom-gw", "custom", "gpt-4o", "https://llm.example.com/v1")
    _, tok = setup(pulsar_home, [custom], {"custom-gw": "zz-gateway-key-0123456789"})
    for stream in (True, False):
        r = client(Upstream({"llm.example.com": [(200, answer(stream, {"content": CALL}))]})).post(
            "/v1/messages", json=req(stream=stream, tools=BASH), headers={"authorization": "Bearer " + tok})
        assert blocks_of(r, stream) == RESCUED, stream
    gemini = ST.Target("openrouter", "openrouter", "google/gemini-2.5-pro")
    _, tok = setup(pulsar_home, [gemini], {"openrouter": "sk-or-k"})
    no_tools = (400, {"error": {"message": "No endpoints found that support tool use; the model does not support tools"}})
    h = {"authorization": "Bearer " + tok}
    for stream, script in ((True, [no_tools]), (False, [])):           # the second request starts from the learned heal
        up = Upstream({"openrouter.ai": script + [(200, answer(stream, {"content": CALL}))]})
        r = client(up).post("/v1/messages", json=req(stream=stream, tools=BASH), headers=h)
        sent = up.calls[-1][3]
        assert "tools" not in sent and "<tool_call>" in json.dumps(sent["messages"]), stream    # tools in the prompt
        assert blocks_of(r, stream) == RESCUED, stream
    gpt = ST.Target("openai", "openai", "gpt-5.5")
    _, tok = setup(pulsar_home, [gpt], {"openai": "sk-proj-k"})
    h = {"authorization": "Bearer " + tok}
    for stream in (True, False):
        up = Upstream({"api.openai.com": [(200, answer(stream, {"content": CALL}))]})
        r = client(up).post("/v1/messages", json=req(stream=stream, tools=BASH), headers=h)
        assert "tools" in up.calls[-1][3] and blocks_of(r, stream) == [("text", CALL, None)], stream


def test_reasoning_is_searched_for_calls_only_on_local_servers_and_custom_endpoints(pulsar_home):
    # Some local servers' reasoning parsers swallow the whole reply, tool call included; elsewhere reasoning is
    # never searched: with thinking not requested it becomes the visible answer, as written.
    ollama = ST.Target("ollama", "ollama", "qwen3")
    _, tok = setup(pulsar_home, [ollama])
    for stream in (True, False):
        r = client(Upstream({"127.0.0.1": [(200, answer(stream, {"reasoning_content": CALL}))]})).post(
            "/v1/messages", json=req(stream=stream, tools=BASH), headers={"authorization": "Bearer " + tok})
        assert blocks_of(r, stream) == RESCUED, stream
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-k"})
    for stream in (True, False):
        r = client(Upstream({"integrate.api.nvidia.com": [(200, answer(stream, {"reasoning_content": CALL}))]})).post(
            "/v1/messages", json=req(stream=stream, tools=BASH), headers={"authorization": "Bearer " + tok})
        assert blocks_of(r, stream) == [("text", CALL, None)], stream


def resp_sse(*events):
    return "".join("event: %s\ndata: %s\n\n" % (e["type"], json.dumps(e)) for e in events).encode()


OPENAI = ST.Target("openai", "openai", "gpt-6.1-codex")


def test_responses_wire_streams_text_and_tool_calls(pulsar_home):
    _, tok = setup(pulsar_home, [OPENAI], {"openai": "sk-proj-k"})
    item = {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "Bash", "arguments": ""}
    stream = resp_sse(
        {"type": "response.created", "response": {"id": "resp_1"}},
        {"type": "response.output_text.delta", "item_id": "m1", "delta": "On it."},
        {"type": "response.output_item.added", "output_index": 1, "item": item},
        {"type": "response.function_call_arguments.delta", "output_index": 1, "item_id": "fc_1", "delta": "{\"command\":"},
        {"type": "response.function_call_arguments.delta", "output_index": 1, "item_id": "fc_1", "delta": "\"ls\"}"},
        {"type": "response.output_item.done", "output_index": 1, "item": dict(item, arguments="{\"command\":\"ls\"}")},
        {"type": "response.completed", "response": {"usage": {"input_tokens": 50, "output_tokens": 9}}})
    up = Upstream({"api.openai.com": [(200, stream)]})
    tools = [{"name": "Bash", "description": "run", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}}}]
    r = client(up).post("/v1/messages", json=req(tools=tools), headers={"authorization": "Bearer " + tok})
    evs = events(r)
    host, path, headers, body = up.calls[0]
    assert path == "/v1/responses" and body["model"] == "gpt-6.1-codex" and body["store"] is False
    assert headers["authorization"] == "Bearer sk-proj-k"
    assert text_of(evs).startswith("On it.")
    starts = [p["content_block"] for n, p in evs if n == "content_block_start"]
    assert any(b["type"] == "tool_use" and b["name"] == "Bash" for b in starts)
    stop = [p for n, p in evs if n == "message_delta"][-1]
    assert stop["delta"]["stop_reason"] == "tool_use"


def test_responses_wire_failure_event_overflow_becomes_error(pulsar_home):
    _, tok = setup(pulsar_home, [OPENAI], {"openai": "sk-proj-k"})
    stream = resp_sse({"type": "response.failed", "response": {"error": {
        "code": "context_length_exceeded", "message": "Your input exceeds the context window of this model."}}})
    up = Upstream({"api.openai.com": [(200, stream)]})
    r = client(up).post("/v1/messages", json=req(), headers={"authorization": "Bearer " + tok})
    evs = events(r)
    assert any(n == "error" and "prompt is too long" in p["error"]["message"] for n, p in evs)


def test_responses_failed_body_is_a_redacted_notice_never_a_reply(pulsar_home):
    key = "zz-fake-responses-key-0123456789"          # no known key shape: only the target's own key masks it
    _, tok = setup(pulsar_home, [OPENAI], {"openai": key})
    failed = {"id": "resp_1", "object": "response", "status": "failed",
              "output": [{"type": "reasoning", "id": "rs_1", "summary": []}],
              "error": {"code": "server_error", "message": "upstream failed for key %s \x1b]52;c;Y3VybA==\x07" % key}}
    for stream in (False, True):
        up = Upstream({"api.openai.com": [(200, failed)]})
        r = client(up).post("/v1/messages", json=req(stream=stream), headers={"authorization": "Bearer " + tok})
        assert r.status_code == 200, stream
        assert key not in r.text and "\x1b" not in r.text and "\\u001b" not in r.text, stream
        assert "having trouble (HTTP 500)" in r.text and "provider reported an error" not in r.text, stream
    over = dict(failed, error={"code": "context_length_exceeded", "message": "Your input exceeds the context window."})
    c = client(Upstream({"api.openai.com": [(200, over)]}), ping_interval=30)
    r = c.post("/v1/messages", json=req(stream=False), headers={"authorization": "Bearer " + tok})
    assert r.status_code == 400 and r.json()["error"]["message"].startswith("prompt is too long")
    evs = events(c.post("/v1/messages", json=req(), headers={"authorization": "Bearer " + tok}))
    assert evs[-1][0] == "error" and evs[-1][1]["error"]["message"].startswith("prompt is too long")


def test_a_failed_responses_body_with_nothing_rendered_is_a_notice_too(pulsar_home):
    from pulsar.core.engine import _to_message
    key = "zz-fake-responses-key-0123456789"          # no known key shape: only the target's own key masks it
    _, tok = setup(pulsar_home, [OPENAI], {"openai": key})
    # its only output part is of a kind Pulsar Code does not render: there is no answer, so it is a failure
    failed = {"id": "resp_1", "object": "response", "status": "failed",
              "output": [{"type": "message", "content": [{"type": "summary_text", "text": "partial"}]}],
              "error": {"code": "server_error", "message": "upstream failed for key %s" % key}}
    for body in (failed, dict(failed, output=5)):
        for stream in (False, True):
            r = client(Upstream({"api.openai.com": [(200, body)]})).post(
                "/v1/messages", json=req(stream=stream), headers={"authorization": "Bearer " + tok})
            assert r.status_code == 200 and key not in r.text, stream
            assert "having trouble (HTTP 500)" in r.text and "provider reported an error" not in r.text, stream
    # wherever the provider's error text is rendered, the target's own key is masked in it too
    msg = json.dumps(_to_message(dict(failed, output=[]), OPENAI.model, req(), "responses", secrets=[key]))
    assert "provider reported an error" in msg and key not in msg


def test_an_event_the_translator_cannot_read_still_ends_the_reply_cleanly(pulsar_home, monkeypatch):
    # Whatever a translator fails on (a shape nobody foresaw) ends the reply with a notice and message_stop on
    # both OpenAI wires, never with a stream that stops mid-message.
    from pulsar import responses as R
    from pulsar import translate as T

    def breaking(real):
        def feed(self, chunk):
            if "BOOM" in json.dumps(chunk):
                raise ValueError("an unforeseen event shape")
            return real(self, chunk)
        return feed
    monkeypatch.setattr(T.StreamTranslator, "feed", breaking(T.StreamTranslator.feed))
    monkeypatch.setattr(R.ResponsesStreamTranslator, "feed", breaking(R.ResponsesStreamTranslator.feed))
    chat = sse(text_chunk("Hel"), text_chunk("BOOM"), text_chunk("lo", "stop"))
    responses = resp_sse({"type": "response.output_text.delta", "item_id": "m1", "delta": "Hel"},
                         {"type": "response.output_text.delta", "item_id": "m1", "delta": "BOOM"},
                         {"type": "response.output_text.delta", "item_id": "m1", "delta": "lo"},
                         {"type": "response.completed", "response": {"usage": {"input_tokens": 5, "output_tokens": 2}}})
    for target, key, host, body in ((NV, "nvapi-k", "integrate.api.nvidia.com", chat),
                                    (OPENAI, "sk-proj-k", "api.openai.com", responses)):
        _, tok = setup(pulsar_home, [target], {target.profile: key})
        c = client(Upstream({host: [(200, body)]}))
        evs = events(c.post("/v1/messages", json=req(), headers={"authorization": "Bearer " + tok}))
        names = [n for n, _ in evs]
        assert names[-1] == "message_stop" and names.count("message_stop") == 1, target.provider
        assert names.count("content_block_start") == names.count("content_block_stop"), target.provider
        text = text_of(evs)
        assert text.startswith("Hel") and "cut off" in text and "Hello" not in text, target.provider
        stats = c.get("/pulsar/stats", headers={"authorization": "Bearer " + ADMIN}).json()
        assert stats["total"]["errors"] == 1 and not stats["flights"], target.provider


SUP = "²"      # a superscript two: str.isdigit() accepts it, int() refuses it


def test_odd_numbers_in_an_upstream_error_never_hide_it(pulsar_home):
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-k"})
    h = {"authorization": "Bearer " + tok}
    bad = {"error": {"code": SUP, "message": "odd gateway trouble"}}
    evs = events(client(Upstream({"integrate.api.nvidia.com": [(200, sse(text_chunk("Hi"), bad))]})).post(
        "/v1/messages", json=req(), headers=h))
    assert evs[-1][0] == "message_stop" and "odd gateway trouble" in text_of(evs)
    for body in (bad, {"error": {"code": ["x"], "status": SUP, "message": "odd gateway trouble"}}):
        r = client(Upstream({"integrate.api.nvidia.com": [(200, body)]})).post("/v1/messages", json=req(stream=False),
                                                                             headers=h)
        assert r.status_code == 200 and "odd gateway trouble" in r.text and "HTTP 502" in r.text


def test_odd_usage_figures_on_the_native_wire_never_cut_a_reply(pulsar_home):
    # Usage figures and block indexes are Core's own bookkeeping: whatever shape a gateway sends them in, the
    # reply arrives whole (stream, a stream answered with one JSON body, non-stream, an auto-mode check, council).
    st = K.load()
    st.upsert(K.Profile(id="deepseek", provider="deepseek", model="deepseek-chat"))
    K.save(st)
    _, tok = setup(pulsar_home, [DS], {"deepseek": "sk-ds"})
    h = {"authorization": "Bearer " + tok}
    odd = {"input_tokens": SUP, "output_tokens": [1], "cache_read_input_tokens": {"n": 1}}
    evs = [("message_start", {"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant",
                                                                    "content": [], "model": "x", "usage": odd}}),
           ("content_block_start", {"type": "content_block_start", "index": "first",
                                    "content_block": {"type": "text", "text": ""}}),
           ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                    "delta": {"type": "text_delta", "text": "native text"}}),
           ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": ["not", "an", "object"]}),
           ("content_block_stop", {"type": "content_block_stop", "index": 0}),
           ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": SUP}),
           ("message_stop", {"type": "message_stop"})]
    stream = "".join("event: %s\ndata: %s\n\n" % (n, json.dumps(d)) for n, d in evs).encode()
    got = events(client(Upstream({"api.deepseek.com": [(200, stream)]})).post("/v1/messages", json=req(), headers=h))
    assert [d for _, d in got] == [d for _, d in evs]                   # forwarded as sent, nothing cut or added
    msg = {"id": "m", "type": "message", "role": "assistant", "model": "deepseek-chat", "stop_reason": "end_turn",
           "content": [{"type": "text", "text": "whole answer"}], "usage": odd}
    c = client(Upstream({"api.deepseek.com": [(200, msg)]}))
    got = events(c.post("/v1/messages", json=req(), headers=h))
    assert text_of(got) == "whole answer" and got[-1][0] == "message_stop"
    r = c.post("/v1/messages", json=req(stream=False), headers=h)
    assert r.status_code == 200 and r.json()["content"][0]["text"] == "whole answer"
    verdict = dict(msg, content=[{"type": "text", "text": 5}], usage=SUP)
    check = req(stream=False, system="Answer <block>no</block> or <block>yes</block><category>x</category>")
    r = client(Upstream({"api.deepseek.com": [(200, verdict)]})).post("/v1/messages", json=check, headers=h)
    assert r.status_code == 200
    r = client(Upstream({"api.deepseek.com": [(200, verdict)]})).post(
        "/pulsar/council", json={"prompt": "q", "models": ["deepseek:deepseek-chat"]}, headers=h)
    assert r.status_code == 200 and r.json()["answers"][0]["ok"] and r.json()["answers"][0]["text"] == "5"


def test_responses_wire_non_stream_and_chat_wire_for_other_models(pulsar_home):
    gpt = ST.Target("openai", "openai", "gpt-5.5")
    _, tok = setup(pulsar_home, [gpt], {"openai": "sk-proj-k"})
    up = Upstream({"api.openai.com": [(200, {"choices": [{"message": {"role": "assistant", "content": "chat ok"},
                                                          "finish_reason": "stop"}]})]})
    r = client(up).post("/v1/messages", json=req(stream=False), headers={"authorization": "Bearer " + tok})
    assert r.json()["content"][0]["text"] == "chat ok" and up.calls[0][1] == "/v1/chat/completions"
    _, tok2 = setup(pulsar_home, [OPENAI], {"openai": "sk-proj-k"})
    up2 = Upstream({"api.openai.com": [(200, {"status": "completed", "output": [
        {"type": "message", "content": [{"type": "output_text", "text": "responses ok"}]}],
        "usage": {"input_tokens": 3, "output_tokens": 2}})]})
    r2 = client(up2).post("/v1/messages", json=req(stream=False), headers={"authorization": "Bearer " + tok2})
    assert r2.json()["content"][0]["text"] == "responses ok" and up2.calls[0][1] == "/v1/responses"


def test_openrouter_anthropic_models_go_native_without_carried_reasoning(pulsar_home):
    orr = ST.Target("openrouter", "openrouter", "anthropic/claude-sonnet-5")
    _, tok = setup(pulsar_home, [orr], {"openrouter": "sk-or-k"})
    up = Upstream({"openrouter.ai": [(200, anth_stream("native via openrouter"))]})
    body = req(messages=[{"role": "user", "content": "hi"},
                         {"role": "assistant", "content": [
                             {"type": "thinking", "thinking": "x", "signature": "pulsar-rs1:abc:rs_1:zzz"},
                             {"type": "text", "text": "earlier"}]},
                         {"role": "user", "content": "go on"}])
    r = client(up).post("/v1/messages", json=body, headers={"authorization": "Bearer " + tok,
                                                              "anthropic-beta": "secret-beta"})
    assert "native via openrouter" in text_of(events(r))
    host, path, headers, sent = up.calls[0]
    assert path == "/api/v1/messages" and headers["authorization"] == "Bearer sk-or-k"
    assert "anthropic-beta" not in headers          # betas only ever go to Anthropic itself
    assert all(b.get("type") != "thinking" for m in sent["messages"] if isinstance(m["content"], list)
               for b in m["content"])


def test_azure_uses_api_key_header(pulsar_home):
    az = ST.Target("azure", "azure", "my-gpt-deploy", "https://acme.openai.azure.com/openai/v1")
    _, tok = setup(pulsar_home, [az], {"azure": "az-secret"})
    up = Upstream({"acme.openai.azure.com": [(200, sse(text_chunk("azure ok", "stop")))]})
    r = client(up).post("/v1/messages", json=req(), headers={"authorization": "Bearer " + tok})
    assert text_of(events(r)) == "azure ok"
    headers = up.calls[0][2]
    assert headers.get("api-key") == "az-secret" and "authorization" not in headers


class _Body:
    """Upstream answering 200 with a fixed body and content type (a provider that ignored stream=true)."""

    def __init__(self, body, ctype="application/json"):
        self.body, self.ctype, self.calls = body, ctype, []

    def __call__(self, request):
        self.calls.append(json.loads(request.content))
        return httpx.Response(200, content=self.body, headers={"content-type": self.ctype})


@pytest.mark.parametrize("payload,ctype,needle", [
    (json.dumps({"choices": [{"message": {"role": "assistant", "content": "whole answer"}, "finish_reason": "stop"}]}),
     "application/json", "whole answer"),
    (json.dumps({"error": {"message": "model overloaded", "code": 503}}), "application/json", "HTTP 503"),
    ("<html><body>Welcome to nginx</body></html>", "text/html", "not a model stream"),
    ("", "application/json", "empty body"),
])
def test_stream_request_answered_without_sse_is_never_silent(pulsar_home, payload, ctype, needle):
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-k"})
    up = _Body(payload.encode(), ctype)
    r = client(up).post("/v1/messages", json=req(), headers={"authorization": "Bearer " + tok})
    evs = events(r)
    assert needle in text_of(evs)
    assert evs[-1][0] == "message_stop" and [n for n, _ in evs].count("message_start") == 1


def test_native_json_body_on_stream_is_replayed_as_stream(pulsar_home):
    k = ST.Target("kimi", "custom", "k3", "https://anth.example/anthropic", native=True)
    _, tok = setup(pulsar_home, [k], {"kimi": "sk-x"})
    msg = {"id": "m1", "type": "message", "role": "assistant", "model": "k3", "stop_reason": "tool_use",
           "content": [{"type": "text", "text": "calling"}, {"type": "tool_use", "id": "toolu_1", "name": "Bash",
                                                              "input": {"command": "ls"}}],
           "usage": {"input_tokens": 10, "output_tokens": 4}}
    up = _Body(json.dumps(msg).encode())
    r = client(up).post("/v1/messages", json=req(), headers={"authorization": "Bearer " + tok})
    evs = events(r)
    assert text_of(evs) == "calling"
    assert any(n == "content_block_start" and p["content_block"]["type"] == "tool_use" for n, p in evs)
    assert [p for n, p in evs if n == "message_delta"][0]["delta"]["stop_reason"] == "tool_use"


def test_non_stream_garbage_relays_instead_of_crashing(pulsar_home):
    _, tok = setup(pulsar_home, [NV, GROQ], {"nvidia": "nvapi-k", "groq": "gsk_k"})
    up = Upstream({"integrate.api.nvidia.com": [(200, [1, 2, 3])],
                   "api.groq.com": [(200, {"choices": [{"message": {"role": "assistant", "content": "groq ok"},
                                                        "finish_reason": "stop"}]})]})
    r = client(up).post("/v1/messages", json=req(stream=False), headers={"authorization": "Bearer " + tok})
    assert r.status_code == 200 and r.json()["content"][0]["text"] == "groq ok"


def test_upstream_echoing_the_key_is_redacted_everywhere(pulsar_home):
    secret = "nvapi-SECRETSECRET1234567890"
    _, tok = setup(pulsar_home, [NV], {"nvidia": secret})
    up = Upstream({"integrate.api.nvidia.com": [(401, {"error": {"message": "Invalid API key provided: " + secret}})]})
    c = client(up)
    r = c.post("/v1/messages", json=req(), headers={"authorization": "Bearer " + tok})
    assert secret not in r.text and "rejected the API key" in text_of(events(r))
    stats = c.get("/pulsar/stats", headers={"authorization": "Bearer " + ADMIN}).text
    assert secret not in stats


def test_max_tokens_too_large_for_context_retries_once_without_persisting(pulsar_home):
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-k"})
    err = {"error": {"message": "'max_tokens' is too large: 32000. This model's maximum context length is 32768 tokens "
                                "and your request has 30000 input tokens (32000 > 32768 - 30000)."}}
    up = Upstream({"integrate.api.nvidia.com": [(400, err), (200, sse(text_chunk("fits", "stop")))]})
    r = client(up).post("/v1/messages", json=req(max_tokens=32000), headers={"authorization": "Bearer " + tok})
    assert text_of(events(r)) == "fits"
    sent = up.calls[1][3]
    assert (sent.get("max_tokens") or sent.get("max_completion_tokens")) == 32768 - 30000 - 64
    assert not list((pulsar_home / "cache").glob("heals-*.json")) or \
        "max_tokens_cap" not in (pulsar_home / "cache" / "heals-nvidia.json").read_text()


def test_real_overflow_is_not_healed(pulsar_home):
    _, tok = setup(pulsar_home, [NV], {"nvidia": "nvapi-k"})
    err = {"error": {"message": "This model's maximum context length is 32768 tokens. However, you requested 40000 "
                                "tokens (39000 in the messages, 1000 in the completion)."}}
    up = Upstream({"integrate.api.nvidia.com": [(400, err)]})
    r = client(up, ping_interval=30).post("/v1/messages", json=req(), headers={"authorization": "Bearer " + tok})
    assert r.status_code == 400 and r.json()["error"]["message"].startswith("prompt is too long")
    assert len(up.calls) == 1


def test_heals_expire(pulsar_home):
    from pulsar.core.engine import Engine, HEAL_TTL
    from pulsar.core.stats import Stats
    eng = Engine(Stats(), lambda p: "", pulsar_home)
    eng.heal_put("p", "m", {"drop_tools": True})
    assert eng.heal_get("p", "m") == {"drop_tools": True}
    eng._heals["p"]["m"]["_t"] -= HEAL_TTL + 1
    assert eng.heal_get("p", "m") == {}
    assert eng.heal_reset() == 1 and eng.heal_get("p", "m") == {}


PLAIN = ST.Target("custom-plain", "custom", "m1", "http://llm.example.com/v1")
PLAIN_KEY = "zz-plain-http-key-0123456789"


def test_a_key_never_crosses_plain_http_to_a_public_host(pulsar_home, monkeypatch):
    monkeypatch.delenv("PULSAR_ALLOW_HTTP", raising=False)
    st = K.load()
    st.upsert(K.Profile(id="custom-plain", provider="custom", model="m1", base_url=PLAIN.base_url))
    K.save(st)
    ok = {"choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}], "usage": {}}
    up = Upstream({"llm.example.com": [(200, sse(text_chunk("sent", "stop")))], "api.groq.com": [(200, ok)]})
    c = client(up)
    _, tok = setup(pulsar_home, [GROQ], {"groq": "gsk_k", "custom-plain": PLAIN_KEY})
    ans = c.post("/pulsar/council", json={"prompt": "q", "models": ["custom-plain:m1"]},
                 headers={"authorization": "Bearer " + tok}).json()["answers"]
    assert not ans[0]["ok"] and "plain http" in ans[0]["error"]
    # a plan that routes there: refused before anything is sent, with a readable notice
    _, tok = setup(pulsar_home, [PLAIN], {"custom-plain": PLAIN_KEY})
    text = text_of(events(c.post("/v1/messages", json=req(), headers={"authorization": "Bearer " + tok})))
    assert "plain http" in text and "PULSAR_ALLOW_HTTP=1" in text and PLAIN_KEY not in text
    assert not [call for call in up.calls if call[0] == "llm.example.com"]
    # the session's own choice at launch decides; Core's environment only when the session file does not say
    monkeypatch.setenv("PULSAR_ALLOW_HTTP", "1")
    _, tok = setup(pulsar_home, [PLAIN], {"custom-plain": PLAIN_KEY}, {"allow_http": False})
    assert "plain http" in text_of(events(c.post("/v1/messages", json=req(), headers={"authorization": "Bearer " + tok})))
    assert not [call for call in up.calls if call[0] == "llm.example.com"]
    _, tok = setup(pulsar_home, [PLAIN], {"custom-plain": PLAIN_KEY}, {"allow_http": True})
    assert text_of(events(c.post("/v1/messages", json=req(), headers={"authorization": "Bearer " + tok}))) == "sent"
    assert up.calls[-1][0] == "llm.example.com"


def test_a_base_url_carrying_credentials_is_never_used(pulsar_home):
    from pulsar.core.engine import _host
    assert _host("https://alice:s3cret-pass@llm2.example.com:8443/v1") == "llm2.example.com:8443"
    t = ST.Target("custom-auth", "custom", "m2", "https://alice:s3cret-pass@llm2.example.com/v1")
    _, tok = setup(pulsar_home, [t], {"custom-auth": "zz-real-key-0123456789"})
    up = Upstream({"llm2.example.com": [(401, {"error": {"message": "no"}})]})
    text = text_of(events(client(up).post("/v1/messages", json=req(), headers={"authorization": "Bearer " + tok})))
    assert "s3cret" not in text and "alice" not in text and "llm2.example.com" in text
    assert up.calls == []        # never sent: httpx would have put alice's password in place of the key


TUNING_VARS = ("PULSAR_HEADER_TIMEOUT", "PULSAR_HEADER_TIMEOUT_REASONING", "PULSAR_FIRST_TOKEN_TIMEOUT",
               "PULSAR_FIRST_TOKEN_TIMEOUT_REASONING", "PULSAR_PING_INTERVAL", "PULSAR_RETRIES",
               "PULSAR_NIM_STREAM_HEADER_TIMEOUT", "PULSAR_NIM_STREAM_HEADER_TIMEOUT_REASONING",
               "PULSAR_NIM_FIRST_TOKEN_TIMEOUT", "PULSAR_NIM_FIRST_TOKEN_TIMEOUT_REASONING",
               "PULSAR_NIM_STREAM_PING_INTERVAL")


def test_tuning_refuses_unusable_values_and_reads_the_v1_names(monkeypatch):
    for var in TUNING_VARS:
        monkeypatch.delenv(var, raising=False)
    for bad in ("0", "-5", "nan", "inf", "soon"):        # 0 would make every upstream request time out at once
        monkeypatch.setenv("PULSAR_HEADER_TIMEOUT", bad)
        monkeypatch.setenv("PULSAR_PING_INTERVAL", bad)
        assert (Tuning().header_timeout, Tuning().ping_interval) == (180, 5), bad
    monkeypatch.delenv("PULSAR_HEADER_TIMEOUT")
    monkeypatch.delenv("PULSAR_PING_INTERVAL")
    for var, val in (("PULSAR_NIM_STREAM_HEADER_TIMEOUT", "240"), ("PULSAR_NIM_STREAM_HEADER_TIMEOUT_REASONING", "600"),
                     ("PULSAR_NIM_FIRST_TOKEN_TIMEOUT", "100"), ("PULSAR_NIM_FIRST_TOKEN_TIMEOUT_REASONING", "900"),
                     ("PULSAR_NIM_STREAM_PING_INTERVAL", "3")):
        monkeypatch.setenv(var, val)
    t = Tuning()
    assert (t.header_timeout, t.header_timeout_reasoning, t.first_token_timeout, t.first_token_timeout_reasoning,
            t.ping_interval) == (240, 600, 100, 900, 3)
    monkeypatch.setenv("PULSAR_FIRST_TOKEN_TIMEOUT_REASONING", "500")      # the v3 name wins over the v1 one
    assert Tuning().first_token_timeout_reasoning == 500
    monkeypatch.setenv("PULSAR_RETRIES", "0")                              # no retries is a real choice
    assert Tuning().retries == 0
