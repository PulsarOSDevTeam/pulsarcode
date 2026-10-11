# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
import json

import pytest

from pulsar import responses as R
from pulsar import translate as T
from pulsar.translate import sse

TOOLS = [{"name": "Bash", "description": "run", "input_schema": {
    "type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"],
    "additionalProperties": False, "$schema": "http://json-schema.org/draft-07/schema#"}},
    {"name": "Read", "description": "read", "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}}}},
    {"type": "web_search_20250305", "name": "web_search"}]


def body(messages, **kw):
    b = {"model": "claude-sonnet", "max_tokens": 32000, "messages": messages}
    b.update(kw)
    return b


def blocks(events):
    """Reassemble content blocks from the event stream, validating Anthropic framing."""
    out = {}
    open_idx = None
    last = -1
    for name, p in events:
        if name == "content_block_start":
            assert open_idx is None, "two blocks open at once"
            assert p["index"] == last + 1, "indexes must be consecutive"
            open_idx = last = p["index"]
            out[open_idx] = dict(p["content_block"], _text="", _json="", _sig=None)
        elif name == "content_block_delta":
            assert p["index"] == open_idx
            d = p["delta"]
            if d["type"] == "text_delta":
                out[open_idx]["_text"] += d["text"]
            elif d["type"] == "thinking_delta":
                out[open_idx]["_text"] += d["thinking"]
            elif d["type"] == "input_json_delta":
                out[open_idx]["_json"] += d["partial_json"]
            elif d["type"] == "signature_delta":
                out[open_idx]["_sig"] = d["signature"]
        elif name == "content_block_stop":
            assert p["index"] == open_idx
            open_idx = None
    assert open_idx is None
    assert events[0][0] == "message_start" and [e[0] for e in events[-2:]] == ["message_delta", "message_stop"]
    for e in events:
        sse(e)  # every event serializes
    return [out[i] for i in sorted(out)]


def run(events, model="gpt-6.1-sol", notice=None, emit_thinking=True, tools=None):
    tr = R.ResponsesStreamTranslator(model, input_tokens=50, emit_thinking=emit_thinking, tools=tools)
    ev = tr.start()
    for e in events:
        ev += tr.feed(e)
    ev += tr.finish(notice)
    return tr, ev


def completed(usage=None, status="completed", reason=None):
    resp = {"id": "resp_1", "object": "response", "status": status,
            "usage": usage or {"input_tokens": 1200, "input_tokens_details": {"cached_tokens": 1000},
                               "output_tokens": 42, "output_tokens_details": {"reasoning_tokens": 30}}}
    if reason:
        resp["incomplete_details"] = {"reason": reason}
    return {"type": "response.completed" if status == "completed" else "response.incomplete", "response": resp}


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------

def test_basic_request_shape():
    b = body([{"role": "user", "content": "hi"}], system=[{"type": "text", "text": "You are Claude Code."}],
             tools=TOOLS, tool_choice={"type": "auto"}, temperature=1, stop_sequences=["\n\nHuman:"],
             metadata={"user_id": "secret-user"})
    r = R.build_request(b, "gpt-4.1", "openai", stream=True)
    assert r["model"] == "gpt-4.1" and r["stream"] is True and r["store"] is False
    assert r["instructions"] == "You are Claude Code."
    assert r["input"] == [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}]
    assert r["max_output_tokens"] == 32000 and r["temperature"] == 1.0
    assert "stop" not in r and "metadata" not in r and "user" not in r      # no stop on /responses; no user ids leak
    names = [t["name"] for t in r["tools"]]
    assert names == ["Bash", "Read"]                                       # server tools have no portable form
    bash = r["tools"][0]
    assert bash["type"] == "function" and bash["strict"] is False and "$schema" not in bash["parameters"]
    assert bash["parameters"]["required"] == ["command"]
    assert r["tool_choice"] == "auto"
    assert "reasoning" not in r and "include" not in r


def test_reasoning_family_params():
    b = body([{"role": "user", "content": "go"}], temperature=0.2, top_p=0.9,
             thinking={"type": "adaptive", "display": "omitted"}, output_config={"effort": "high"})
    r = R.build_request(b, "gpt-6.1-sol", "openai", stream=True)
    assert "temperature" not in r and "top_p" not in r
    assert r["reasoning"] == {"effort": "high"}                            # display omitted: no summary
    assert r["include"] == ["reasoning.encrypted_content"]
    b["thinking"] = {"type": "enabled", "budget_tokens": 8000}
    b["output_config"] = {"effort": "max"}
    r = R.build_request(b, "gpt-6.1-sol", "openai", stream=True)
    assert r["reasoning"] == {"effort": "max", "summary": "auto"}
    assert R.build_request(b, "gpt-5.5-pro", "openai", stream=True)["reasoning"]["effort"] == "xhigh"
    assert R.build_request(b, "o3-pro", "openai", stream=True)["reasoning"]["effort"] == "high"
    b.pop("output_config")
    assert R.build_request(b, "o3-pro", "openai", stream=True)["reasoning"]["effort"] == "medium"
    b["thinking"] = {"type": "disabled"}
    r = R.build_request(b, "gpt-5.3-codex", "openai", stream=False)
    assert "reasoning" not in r and "include" not in r
    assert not R.is_reasoning_family("gpt-5-chat-latest") and not R.is_reasoning_family("gpt-4o")
    assert R.is_reasoning_family("openai/gpt-6-luna") and R.is_reasoning_family("o4-mini")


def test_caps_and_tool_choice_variants():
    b = body([{"role": "user", "content": "x"}], tools=TOOLS,
             tool_choice={"type": "tool", "name": "Bash", "disable_parallel_tool_use": True})
    r = R.build_request(b, "gpt-4.1", "openai", stream=False, max_output=16384, heal={"max_tokens_cap": 8000})
    assert r["max_output_tokens"] == 8000
    assert r["tool_choice"] == {"type": "function", "name": "Bash"} and r["parallel_tool_calls"] is False
    assert R.tool_choice_to_responses({"type": "any"}) == "required"
    assert R.tool_choice_to_responses({"type": "none"}) == "none"
    assert R.tool_choice_to_responses(None) is None


def test_tool_round_trip_items():
    msgs = [
        {"role": "user", "content": "list files"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Sure."},
            {"type": "tool_use", "id": "toolu_A", "name": "Bash", "input": {"command": "ls"}},
            {"type": "tool_use", "id": "toolu_B", "name": "Read", "input": {"file_path": "a"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_A", "content": "a\nb"},
            {"type": "tool_result", "tool_use_id": "toolu_B", "content": [{"type": "text", "text": "boom"}],
             "is_error": True},
            {"type": "text", "text": "thanks"}]},
    ]
    _, items = R.input_items(body(msgs))
    kinds = [(i["type"], i.get("role")) for i in items]
    assert kinds == [("message", "user"), ("message", "assistant"), ("function_call", None), ("function_call", None),
                     ("function_call_output", None), ("function_call_output", None), ("message", "user")]
    assert items[1]["content"] == "Sure."
    assert items[2] == {"type": "function_call", "call_id": "toolu_A", "name": "Bash", "arguments": '{"command":"ls"}'}
    assert items[4] == {"type": "function_call_output", "call_id": "toolu_A", "output": "a\nb"}
    assert items[5]["output"] == "Error: boom"
    assert all(not k.startswith("_") for i in items for k in i)


def test_interrupted_and_orphaned_tool_calls_are_repaired():
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}},
                                          {"type": "tool_use", "id": "t2", "name": "Bash", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
                                     {"type": "tool_result", "tool_use_id": "ghost", "content": "late"},
                                     {"type": "text", "text": "stop, do something else"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "dup"}]},
    ]
    _, items = R.input_items(body(msgs))
    outs = [i for i in items if i["type"] == "function_call_output"]
    assert [(o["call_id"], o["output"]) for o in outs] == [
        ("t1", "ok"), ("t2", "(no result: the tool call was interrupted)")]
    texts = [c["text"] for i in items if i["type"] == "message" for c in i["content"]]
    assert "Tool result (ghost):\nlate" in texts and "stop, do something else" in texts
    assert "dup" not in json.dumps(items)
    # every call is answered before the conversation moves on
    for idx, item in enumerate(items):
        if item["type"] == "function_call":
            nxt = [i for i in items[idx + 1:] if i["type"] != "function_call"][0]
            assert nxt["type"] == "function_call_output"


def test_system_roles_and_unknown_roles():
    msgs = [{"role": "system", "content": "extra rules"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]},
            {"role": "system", "content": "mid note"},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
            {"role": "ctx", "content": "odd role"}]
    instructions, items = R.input_items(body(msgs, system="base"))
    assert instructions == "base\n\nextra rules"
    seq = [(i["type"], i.get("role")) for i in items]
    # the mid-conversation note never splits a call from its output
    assert seq == [("message", "user"), ("function_call", None), ("function_call_output", None),
                   ("message", "developer"), ("message", "user")]
    _, items = R.input_items(body(msgs, system="base"), developer=False)
    note = [i for i in items if i["type"] == "message" and "mid note" in json.dumps(i)][0]
    assert note["role"] == "user" and "<system-reminder>" in note["content"][0]["text"]


def test_images_documents_and_tool_images():
    img = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAA"}}
    pdf = {"type": "document", "title": "spec", "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBE"}}
    txt = {"type": "document", "title": "notes", "source": {"type": "text", "media_type": "text/plain", "data": "hello"}}
    msgs = [{"role": "user", "content": [{"type": "text", "text": "see"}, img, pdf, txt]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [img]}]}]
    _, items = R.input_items(body(msgs))
    parts = items[0]["content"]
    assert parts[1] == {"type": "input_image", "image_url": "data:image/png;base64,AAA", "detail": "auto"}
    assert parts[2] == {"type": "input_file", "filename": "spec.pdf", "file_data": "data:application/pdf;base64,JVBE"}
    assert parts[3]["text"] == "[document: notes]\nhello"
    out = [i for i in items if i["type"] == "function_call_output"][0]
    assert "attached in the next message" in out["output"]
    follow = items[-1]
    assert follow["role"] == "user" and follow["content"][1]["type"] == "input_image"
    _, items = R.input_items(body(msgs), vision=False)
    assert "input_image" not in json.dumps(items) and "input_file" not in json.dumps(items)


def test_empty_conversation_still_has_input():
    r = R.build_request(body([]), "gpt-4.1", "openai", stream=False)
    assert r["input"][-1]["role"] == "user"


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------

def test_stream_text_only_with_usage():
    tr, ev = run([
        {"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}},
        {"type": "response.output_item.added", "output_index": 0, "item": {"type": "message", "id": "msg_1"}},
        {"type": "response.content_part.added", "item_id": "msg_1", "output_index": 0, "content_index": 0},
        {"type": "response.output_text.delta", "item_id": "msg_1", "output_index": 0, "delta": "Hel"},
        {"type": "response.output_text.delta", "item_id": "msg_1", "output_index": 0, "delta": "lo"},
        {"type": "response.output_text.done", "item_id": "msg_1", "text": "Hello"},
        {"type": "response.output_item.done", "output_index": 0, "item": {"type": "message", "id": "msg_1"}},
        completed(),
    ], model="gpt-4.1")
    b = blocks(ev)
    assert len(b) == 1 and b[0]["type"] == "text" and b[0]["_text"] == "Hello"
    assert ev[0][1]["message"]["usage"]["input_tokens"] == 50
    delta = ev[-2][1]
    assert delta["delta"]["stop_reason"] == "end_turn"
    assert delta["usage"] == {"output_tokens": 42, "input_tokens": 200, "cache_read_input_tokens": 1000}
    assert tr.usage_in == 200 and tr.cache_read == 1000 and tr.usage_out == 42 and tr.cost == 0.0
    assert tr.done and tr.visible and tr.active and tr.response_id == "resp_1"


def test_stream_reasoning_summary_then_text():
    events = [
        {"type": "response.output_item.added", "output_index": 0, "item": {"type": "reasoning", "id": "rs_1"}},
        {"type": "response.reasoning_summary_part.added", "item_id": "rs_1", "summary_index": 0},
        {"type": "response.reasoning_summary_text.delta", "item_id": "rs_1", "summary_index": 0, "delta": "Plan "},
        {"type": "response.reasoning_summary_text.delta", "item_id": "rs_1", "summary_index": 0, "delta": "A."},
        {"type": "response.reasoning_summary_part.added", "item_id": "rs_1", "summary_index": 1},
        {"type": "response.reasoning_summary_text.delta", "item_id": "rs_1", "summary_index": 1, "delta": "Then B."},
        {"type": "response.output_item.done", "output_index": 0,
         "item": {"type": "reasoning", "id": "rs_1", "encrypted_content": "ENC123", "summary": []}},
        {"type": "response.output_item.added", "output_index": 1, "item": {"type": "message", "id": "msg_1"}},
        {"type": "response.output_text.delta", "item_id": "msg_1", "output_index": 1, "delta": "Done."},
        completed(),
    ]
    _, ev = run(events)
    b = blocks(ev)
    assert [x["type"] for x in b] == ["thinking", "text"]
    assert b[0]["_text"] == "Plan A.\n\nThen B."
    assert b[0]["_sig"].startswith(R.CARRY_PREFIX) and b[0]["_sig"].endswith(":rs_1:ENC123")
    # Hidden reasoning (display omitted) is still carried, in an empty thinking block.
    _, ev = run(events, emit_thinking=False)
    b = blocks(ev)
    assert [x["type"] for x in b] == ["thinking", "text"] and b[0]["_text"] == "" and b[0]["_sig"]
    # Without encrypted content there is nothing to carry and nothing to show.
    plain = [e for e in events if e["type"] != "response.output_item.done"]
    _, ev = run(plain, emit_thinking=False)
    assert [x["type"] for x in blocks(ev)] == ["text"]


def test_reasoning_marks_activity_before_any_token():
    tr = R.ResponsesStreamTranslator("gpt-6.1-sol")
    tr.start()
    tr.feed({"type": "response.created", "response": {"id": "r"}})
    assert not tr.active
    tr.feed({"type": "response.output_item.added", "output_index": 0, "item": {"type": "reasoning", "id": "rs_1"}})
    assert tr.active and not tr.visible


def test_stream_raw_reasoning_text():
    _, ev = run([{"type": "response.reasoning_text.delta", "item_id": "rs_1", "delta": "think"},
                 {"type": "response.output_text.delta", "item_id": "m", "delta": "ok"}, completed()], model="gpt-oss-120b")
    b = blocks(ev)
    assert [x["type"] for x in b] == ["thinking", "text"] and b[0]["_sig"] == ""


def test_stream_single_function_call():
    tr, ev = run([
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "Bash", "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "output_index": 0, "delta": '{"comm'},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "output_index": 0, "delta": 'and":"ls"}'},
        {"type": "response.function_call_arguments.done", "item_id": "fc_1", "output_index": 0,
         "arguments": '{"command":"ls"}'},
        {"type": "response.output_item.done", "output_index": 0,
         "item": {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "Bash",
                  "arguments": '{"command":"ls"}', "status": "completed"}},
        completed(),
    ], tools=TOOLS)
    b = blocks(ev)
    assert len(b) == 1 and b[0]["type"] == "tool_use" and b[0]["name"] == "Bash"
    assert b[0]["id"].startswith("toolu_") and b[0]["id"] != "call_1"
    assert json.loads(b[0]["_json"]) == {"command": "ls"}
    assert ev[-2][1]["delta"]["stop_reason"] == "tool_use" and tr.tools_emitted == 1


def test_stream_parallel_function_calls_text_first_and_unique_ids():
    tr, ev = run([
        {"type": "response.output_item.added", "output_index": 0, "item": {"type": "message", "id": "m"}},
        {"type": "response.output_text.delta", "item_id": "m", "output_index": 0, "delta": "Checking both."},
        {"type": "response.output_item.added", "output_index": 1,
         "item": {"type": "function_call", "id": "fc_1", "call_id": "c1", "name": "Read", "arguments": ""}},
        {"type": "response.output_item.added", "output_index": 2,
         "item": {"type": "function_call", "id": "fc_2", "call_id": "c2", "name": "read", "arguments": ""}},
        # interleaved argument streams
        {"type": "response.function_call_arguments.delta", "item_id": "fc_2", "output_index": 2, "delta": '{"file_path":'},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "output_index": 1, "delta": '{"file_path":"a"}'},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_2", "output_index": 2, "delta": '"b"}'},
        {"type": "response.output_item.done", "output_index": 2,
         "item": {"type": "function_call", "id": "fc_2", "call_id": "c2", "name": "read"}},
        {"type": "response.output_item.done", "output_index": 1,
         "item": {"type": "function_call", "id": "fc_1", "call_id": "c1", "name": "Read"}},
        completed(),
    ], tools=TOOLS)
    b = blocks(ev)
    assert [x["type"] for x in b] == ["text", "tool_use", "tool_use"]
    assert [json.loads(x["_json"]) for x in b[1:]] == [{"file_path": "b"}, {"file_path": "a"}]
    assert [x["name"] for x in b[1:]] == ["Read", "Read"]            # case-repaired against the tool list
    assert b[1]["id"] != b[2]["id"] and tr.tools_emitted == 2
    assert ev[-2][1]["delta"]["stop_reason"] == "tool_use"


def test_stream_calls_without_item_ids_are_keyed_by_output_index():
    _, ev = run([
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"type": "function_call", "call_id": "c1", "name": "Bash", "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '{"command":"pwd"}'},
        {"type": "response.output_item.done", "output_index": 0,
         "item": {"type": "function_call", "call_id": "c1", "name": "Bash"}},
        completed(),
    ])
    b = blocks(ev)
    assert len(b) == 1 and json.loads(b[0]["_json"]) == {"command": "pwd"}


def test_stream_incomplete_max_tokens_never_runs_a_truncated_call():
    tr, ev = run([
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"type": "function_call", "id": "fc_1", "call_id": "c1", "name": "Write", "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "output_index": 0,
         "delta": '{"path": "x", "body": "abc'},
        completed(usage={"input_tokens": 10, "output_tokens": 99}, status="incomplete", reason="max_output_tokens"),
    ])
    b = blocks(ev)
    # a repaired {"path": "x", "body": "abc"} would silently write a truncated file
    assert all(x["type"] != "tool_use" for x in b) and tr.tools_emitted == 0
    assert "Write" in "".join(x.get("_text", "") + x.get("text", "") for x in b if x["type"] == "text") or \
        "Write" in json.dumps(ev)
    # Only the tool call was cut (withheld, with a note): the turn ends, as on the chat wire.
    assert ev[-2][1]["delta"]["stop_reason"] == "end_turn" and "output token limit" in b[0]["_text"]
    assert ev[-2][1]["usage"] == {"output_tokens": 99, "input_tokens": 10}


def test_stream_call_cut_by_the_output_cap_ends_the_turn_with_a_note():
    # What OpenAI sends when max_output_tokens runs out mid-call: the item is done with status "incomplete",
    # then response.incomplete says why. Same outcome as the chat wire (test_truncated_tool_json_is_never_run).
    partial = '{"file_path": "/tmp/x/out.txt", "content": "line one\\nline two (cut he'
    cut = [
        {"type": "response.output_item.added", "output_index": 1,
         "item": {"type": "function_call", "id": "fc_1", "call_id": "c1", "name": "Write", "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "output_index": 1, "delta": partial},
        {"type": "response.output_item.done", "output_index": 1, "item": {
            "type": "function_call", "id": "fc_1", "call_id": "c1", "name": "Write", "arguments": partial,
            "status": "incomplete"}}]
    text = [{"type": "response.output_text.delta", "item_id": "m", "output_index": 0, "delta": "Writing the file now."}]
    tr, ev = run(text + cut + [completed(status="incomplete", reason="max_output_tokens")], tools=TOOLS)
    b = blocks(ev)
    assert all(x["type"] != "tool_use" for x in b) and tr.tools_emitted == 0 and tr.cut_calls == ["Write"]
    assert b[0]["_text"].startswith("Writing the file now.\n\n")
    assert "Write" in b[0]["_text"] and "not run" in b[0]["_text"] and "output token limit" in b[0]["_text"]
    assert "line two" not in json.dumps(ev)             # no trace of the cut arguments reaches Claude Code
    assert [p["delta"]["stop_reason"] for n, p in ev if n == "message_delta"] == ["end_turn"]
    # Not the output cap (a content filter): the generic note, and the turn ends too.
    _, ev = run(cut + [completed(status="incomplete", reason="content_filter")])
    assert "output token limit" not in json.dumps(ev) and "not run" in json.dumps(ev)
    assert ev[-2][1]["delta"]["stop_reason"] == "end_turn"
    # The model went on writing after the cut call, and the cap cut that text: the reply itself was cut.
    tr, ev = run(cut + text + [completed(status="incomplete", reason="max_output_tokens")])
    b = blocks(ev)
    assert b[0]["_text"].index("not run") < b[0]["_text"].index("Writing the file now.")
    assert tr.cut_calls == ["Write"] and ev[-2][1]["delta"]["stop_reason"] == "max_tokens"
    # A finished call before the cut one still runs; the cut one never does.
    done = [{"type": "response.output_item.done", "output_index": 0, "item": {
        "type": "function_call", "id": "fc_0", "call_id": "c0", "name": "Bash", "arguments": '{"command": "ls"}'}}]
    tr, ev = run(done + cut + [completed(status="incomplete", reason="max_output_tokens")], tools=TOOLS)
    uses = [x for x in blocks(ev) if x["type"] == "tool_use"]
    assert len(uses) == 1 and json.loads(uses[0]["_json"]) == {"command": "ls"} and tr.cut_calls == ["Write"]
    assert ev[-2][1]["delta"]["stop_reason"] != "max_tokens"


def test_stream_cut_connection_and_incomplete_item_never_run():
    tr, ev = run([
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"type": "function_call", "id": "fc_1", "call_id": "c1", "name": "Bash", "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "output_index": 0,
         "delta": '{"command": "git push origin f'},
    ], notice="stream was cut")
    assert not any(e[0] == "content_block_start" and e[1]["content_block"]["type"] == "tool_use" for e in ev)
    assert ev[-2][1]["delta"]["stop_reason"] == "end_turn" and tr.cut_calls == ["Bash"]
    tr2, ev2 = run([
        {"type": "response.output_item.done", "output_index": 0, "item": {
            "type": "function_call", "id": "fc_2", "name": "Bash", "status": "incomplete",
            "arguments": '{"command": "ls"}'}},
        completed()])
    assert tr2.tools_emitted == 0 and "not run" in json.dumps(ev2)


def test_stream_incomplete_text():
    _, ev = run([{"type": "response.output_text.delta", "item_id": "m", "delta": "partial"},
                 completed(status="incomplete", reason="max_output_tokens")])
    assert ev[-2][1]["delta"]["stop_reason"] == "max_tokens"


def _cut_by_the_cap(wire, first_call, item_done=False):
    """One scenario on either wire: text or a finished Bash call, then a Write call cut by the output cap.
    Returns (stop_reason, tool_use names, visible text) for the stream and the non-stream path."""
    cut_args = '{"file_path": "x", "content": "ab'
    if wire == "chat":
        def ch(delta, finish=None):
            return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        head = ch({"tool_calls": [{"index": 0, "id": "a", "function": {"name": "Bash", "arguments": '{"command":"ls"}'}}]}) \
            if first_call else ch({"content": "Writing it."})
        chunks = [head, ch({"tool_calls": [{"index": 1, "id": "b", "function": {"name": "Write", "arguments": cut_args}}]}),
                  ch({}, "length")]
        tr = T.StreamTranslator("m", input_tokens=50)
        ev = tr.start()
        for c in chunks:
            ev += tr.feed(c)
        ev += tr.finish()
        msg = {"choices": [{"finish_reason": "length", "message": {"content": None if first_call else "Writing it.",
                                                                   "tool_calls": ([{"id": "a", "function": {
                                                                       "name": "Bash", "arguments": '{"command":"ls"}'}}]
                                                                                  if first_call else []) + [
                                                                       {"id": "b", "function": {"name": "Write",
                                                                                                "arguments": cut_args}}]}}]}
        whole = T.to_anthropic_message(msg, "m")
    else:
        events = ([{"type": "response.output_item.done", "output_index": 0, "item": {
            "type": "function_call", "id": "fc_0", "call_id": "c0", "name": "Bash", "arguments": '{"command":"ls"}',
            "status": "completed"}}] if first_call else
                  [{"type": "response.output_text.delta", "item_id": "m", "output_index": 0, "delta": "Writing it."}])
        events += [{"type": "response.output_item.added", "output_index": 1, "item": {
            "type": "function_call", "id": "fc_1", "call_id": "c1", "name": "Write", "arguments": ""}},
                   {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "output_index": 1,
                    "delta": cut_args}]
        if item_done:
            events.append({"type": "response.output_item.done", "output_index": 1, "item": {
                "type": "function_call", "id": "fc_1", "call_id": "c1", "name": "Write", "arguments": cut_args,
                "status": "incomplete"}})
        events.append(completed(status="incomplete", reason="max_output_tokens"))
        _, ev = run(events)
        output = ([{"type": "function_call", "call_id": "c0", "name": "Bash", "arguments": '{"command":"ls"}',
                    "status": "completed"}] if first_call else
                  [{"type": "message", "content": [{"type": "output_text", "text": "Writing it."}]}])
        output.append({"type": "function_call", "call_id": "c1", "name": "Write", "arguments": cut_args,
                       "status": "incomplete"})
        whole = R.to_anthropic_message({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
                                        "output": output}, "m")
    b = blocks(ev)
    streamed = (ev[-2][1]["delta"]["stop_reason"], [x["name"] for x in b if x["type"] == "tool_use"],
                "".join(x["_text"] for x in b if x["type"] == "text"))
    plain = (whole["stop_reason"], [x["name"] for x in whole["content"] if x["type"] == "tool_use"],
             "".join(x.get("text", "") for x in whole["content"] if x["type"] == "text"))
    return streamed, plain


@pytest.mark.parametrize("first_call", [False, True], ids=["text first", "finished call first"])
def test_a_reply_cut_by_the_cap_inside_a_tool_call_ends_the_same_way_on_both_wires(first_call):
    # The visible text is whole: max_tokens would make Claude Code resume the same reply again and again.
    want_stop, want_calls = ("tool_use", ["Bash"]) if first_call else ("end_turn", [])
    for wire, item_done in (("chat", False), ("responses", False), ("responses", True)):
        for stop, calls, text in _cut_by_the_cap(wire, first_call, item_done):
            assert (stop, calls) == (want_stop, want_calls), (wire, item_done)
            assert "Write" in text and "output token limit" in text and "not run" in text, (wire, item_done)


def test_stream_failed_response_adds_notice():
    tr, ev = run([
        {"type": "response.output_text.delta", "item_id": "m", "delta": "Start"},
        {"type": "response.failed", "response": {"id": "r", "status": "failed",
                                                 "error": {"code": "server_error", "message": "The server had an error"}}},
    ])
    assert tr.failure["status"] == 500 and tr.failure["code"] == "server_error" and tr.done
    b = blocks(ev)
    assert b[0]["_text"] == "Start\n\nThe provider reported an error: The server had an error"
    assert ev[-2][1]["delta"]["stop_reason"] == "end_turn"


def test_a_failed_response_never_shows_a_key_or_a_terminal_escape():
    # The error text lands in Claude Code's transcript (and the next provider's prompt) and on the terminal.
    key, custom = "sk-proj-AbCdEfGhIjKlMnOpQrStUvWx", "custom-secret-0123456789abcdef"
    message = "upstream failed for key %s and %s \x1b]52;c;ZXZpbA==\x07 then\nretry" % (key, custom)
    data = {"status": "failed", "output": [{"type": "reasoning", "id": "rs", "summary": []}],
            "error": {"message": message}}
    text = R.to_anthropic_message(data, "m", secrets=[custom])["content"][0]["text"]
    assert text.startswith("The provider reported an error: upstream failed for key ") and text.endswith("then retry")
    assert key not in text and custom not in text and "\x1b" not in text and "\x07" not in text
    long = R.to_anthropic_message(dict(data, error={"message": "x" * 5000}), "m")["content"][0]["text"]
    assert len(long) < 600
    _, ev = run([{"type": "response.failed", "response": {"status": "failed", "error": {"message": message}}}])
    shown = "".join(x["_text"] for x in blocks(ev))
    assert "upstream failed for key" in shown and key not in shown and "\x1b" not in shown and "\x07" not in shown


def test_stream_error_event_and_custom_notice():
    tr, ev = run([{"type": "error", "code": "rate_limit_exceeded", "message": "Slow down", "param": None}],
                 notice="Pulsar notice")
    assert tr.failure["status"] == 429
    assert blocks(ev)[0]["_text"] == "Pulsar notice"
    assert R.error_of({"type": "error", "code": "context_length_exceeded", "message": "too long"})[0] == 400
    assert "context_length_exceeded" in R.error_of({"type": "error", "code": "context_length_exceeded",
                                                    "message": "x"})[1]
    assert R.error_of({"type": "response.output_text.delta"}) is None


def test_stream_empty_and_unknown_events():
    tr, ev = run([{"type": "response.web_search_call.in_progress"}, {"type": "response.audio.delta"}, "junk"])
    assert [x["type"] for x in blocks(ev)] == ["text"] and not tr.active
    assert R.ResponsesStreamTranslator.ping() == ("ping", {"type": "ping"})


def test_malformed_numbers_and_shapes_never_end_the_stream():
    # A gateway that relays Responses events with usage figures as strings, Infinity or a junk summary index.
    text = {"type": "response.output_text.delta", "item_id": "m", "delta": "ok"}
    part = {"type": "response.reasoning_summary_part.added", "item_id": "rs", "summary_index": "first"}
    for usage in ({"input_tokens": "n/a", "output_tokens": 3}, {"input_tokens": 5, "output_tokens": float("inf")},
                  {"input_tokens": 5, "input_tokens_details": {"cached_tokens": "x"}, "output_tokens": "²"},
                  {"input_tokens_details": "junk", "output_tokens": "9" * 5000}, "junk"):
        tr, ev = run([text, part, {"type": "response.completed", "response": {"status": "completed", "usage": usage}}])
        assert [(x["type"], x["_text"]) for x in blocks(ev)] == [("text", "ok")] and tr.done
        msg = R.to_anthropic_message({"status": "completed", "usage": usage, "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]}, "m", 7)
        assert msg["content"] == [{"type": "text", "text": "ok"}]
        assert all(isinstance(v, int) for v in msg["usage"].values())
    tr, ev = run([text, {"type": "response.output_item.added", "item": "junk"},
                  {"type": "response.output_item.done", "item": ["junk"]}, {"type": "response.created", "response": 5},
                  {"type": "response.function_call_arguments.delta", "output_index": [1], "delta": "{}"},
                  {"type": "response.incomplete", "response": {"incomplete_details": "junk"}}])
    b = blocks(ev)                     # the nameless call never finished: withheld with a note, never run
    assert [x["type"] for x in b] == ["text"] and b[0]["_text"].startswith("ok") and tr.tools_emitted == 0
    assert R.to_anthropic_message({"status": "incomplete", "incomplete_details": "junk", "output": "junk"},
                                  "m")["content"] == [{"type": "text", "text": ""}]
    assert R.error_of({"type": "error", "status": "²", "code": "rate_limit_exceeded"})[0] == 429
    assert R.error_of({"type": "response.failed", "response": "junk"})[0] == 500


def test_stream_refusal_is_visible_text():
    _, ev = run([{"type": "response.refusal.delta", "item_id": "m", "delta": "I can't help with that."}, completed()])
    assert blocks(ev)[0]["_text"] == "I can't help with that."


def test_carried_reasoning_round_trip_same_model_only():
    _, ev = run([
        {"type": "response.output_item.done", "output_index": 0,
         "item": {"type": "reasoning", "id": "rs_9", "encrypted_content": "gAAAA==", "summary": []}},
        {"type": "response.output_item.added", "output_index": 1,
         "item": {"type": "function_call", "id": "fc_1", "call_id": "c1", "name": "Bash", "arguments": "{}"}},
        {"type": "response.output_item.done", "output_index": 1,
         "item": {"type": "function_call", "id": "fc_1", "call_id": "c1", "name": "Bash", "arguments": "{}"}},
        completed(),
    ], emit_thinking=False)
    b = blocks(ev)
    thinking = {"type": "thinking", "thinking": "", "signature": b[0]["_sig"]}
    tool = {"type": "tool_use", "id": b[1]["id"], "name": "Bash", "input": {}}
    msgs = [{"role": "user", "content": "go"}, {"role": "assistant", "content": [thinking, tool]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": b[1]["id"], "content": "ok"}]}]
    thinking_on = {"type": "adaptive", "display": "omitted"}
    r = R.build_request(body(msgs, thinking=thinking_on), "gpt-6.1-sol", "openai", stream=True)
    kinds = [i["type"] for i in r["input"]]
    assert kinds == ["message", "reasoning", "function_call", "function_call_output"]
    assert r["input"][1] == {"type": "reasoning", "id": "rs_9", "encrypted_content": "gAAAA==", "summary": []}
    assert r["input"][2]["call_id"] == b[1]["id"]
    # another model (or a heal) never receives it
    other = R.build_request(body(msgs, thinking=thinking_on), "gpt-6-luna", "openai", stream=True)
    assert "reasoning" not in [i["type"] for i in other["input"]]
    healed = R.build_request(body(msgs, thinking=thinking_on), "gpt-6.1-sol", "openai", stream=True,
                             heal={"drop_carry": True})
    assert "reasoning" not in [i["type"] for i in healed["input"]] and "include" not in healed
    # Anthropic signatures are never forwarded either
    msgs[1]["content"][0] = {"type": "thinking", "thinking": "x", "signature": "EqQBCkYIARgC"}
    r = R.build_request(body(msgs, thinking=thinking_on), "gpt-6.1-sol", "openai", stream=True)
    assert "reasoning" not in [i["type"] for i in r["input"]]


def test_lonely_reasoning_is_dropped():
    sig = R.CARRY_PREFIX + R._tag("gpt-6.1-sol") + ":rs_1:ENC"
    msgs = [{"role": "user", "content": "go"},
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": sig}]},
            {"role": "user", "content": "again"}]
    r = R.build_request(body(msgs, thinking={"type": "adaptive"}), "gpt-6.1-sol", "openai", stream=True)
    assert [i["type"] for i in r["input"]] == ["message", "message"]


def test_strip_carried_for_native_relays():
    sig = R.CARRY_PREFIX + "abcd1234:rs_1:ENC"
    b = body([{"role": "user", "content": "go"},
              {"role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": sig},
                                                {"type": "thinking", "thinking": "t", "signature": ""},
                                                {"type": "thinking", "thinking": "real", "signature": "EqQB"},
                                                {"type": "text", "text": "hi"}]},
              {"role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": sig}]}])
    out = R.strip_carried(b)
    assert out["messages"][1]["content"] == [{"type": "thinking", "thinking": "real", "signature": "EqQB"},
                                             {"type": "text", "text": "hi"}]
    assert out["messages"][2]["content"][0]["type"] == "text"
    assert b["messages"][1]["content"][0]["signature"] == sig            # input untouched


# ---------------------------------------------------------------------------
# Non-stream
# ---------------------------------------------------------------------------

def test_to_anthropic_message():
    data = {"id": "resp_1", "object": "response", "status": "completed", "output": [
        {"type": "reasoning", "id": "rs_1", "encrypted_content": "E", "summary": [{"type": "summary_text", "text": "hm"}]},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Hi "},
                                                             {"type": "output_text", "text": "there"}]},
        {"type": "function_call", "id": "fc_1", "call_id": "c1", "name": "bash", "arguments": '{"command":"ls"}'},
    ], "usage": {"input_tokens": 500, "input_tokens_details": {"cached_tokens": 100}, "output_tokens": 20}}
    msg = R.to_anthropic_message(data, "gpt-6.1-sol", 77, tools=TOOLS)
    kinds = [b["type"] for b in msg["content"]]
    assert kinds == ["thinking", "text", "tool_use"]
    assert msg["content"][0]["thinking"] == "hm" and msg["content"][0]["signature"].startswith(R.CARRY_PREFIX)
    assert msg["content"][1]["text"] == "Hi there"
    assert msg["content"][2]["name"] == "Bash" and msg["content"][2]["input"] == {"command": "ls"}
    assert msg["stop_reason"] == "tool_use"
    assert msg["usage"] == {"input_tokens": 400, "output_tokens": 20, "cache_read_input_tokens": 100}
    data["output"][2]["arguments"] = '{"command":"rm -rf ./build'          # cut off: never runnable
    msg = R.to_anthropic_message(data, "gpt-6.1-sol", 77, tools=TOOLS)
    assert [b["type"] for b in msg["content"]] == ["thinking", "text", "text"]
    assert "not run" in msg["content"][2]["text"] and msg["stop_reason"] == "end_turn"
    inc = R.to_anthropic_message({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
                                  "output": []}, "m", 77)
    assert inc["stop_reason"] == "max_tokens" and inc["content"] == [{"type": "text", "text": ""}]
    assert inc["usage"]["input_tokens"] == 77


def test_non_stream_call_cut_by_the_output_cap_ends_the_turn_with_a_note():
    said = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Writing it."}]}
    cut = {"type": "function_call", "id": "fc_1", "call_id": "c1", "name": "Write", "status": "incomplete",
           "arguments": '{"file_path": "x", "content": "line one (cut he'}
    data = {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": [said, cut]}
    msg = R.to_anthropic_message(data, "m", tools=TOOLS)
    assert all(b["type"] != "tool_use" for b in msg["content"]) and msg["stop_reason"] == "end_turn"
    note = msg["content"][-1]["text"]
    assert "Write" in note and "not run" in note and "output token limit" in note and "cut he" not in json.dumps(msg)
    # The cap cut text after the call: the reply itself was cut.
    msg = R.to_anthropic_message(dict(data, output=[cut, said]), "m", tools=TOOLS)
    assert msg["stop_reason"] == "max_tokens" and "not run" in msg["content"][0]["text"]
    failed = R.to_anthropic_message({"status": "failed", "error": {"message": "nope"}, "output": []}, "m")
    assert "nope" in failed["content"][0]["text"]


# ---------------------------------------------------------------------------
# Healing
# ---------------------------------------------------------------------------

def err(message, param=None, code=None):
    return json.dumps({"error": {"message": message, "type": "invalid_request_error", "param": param, "code": code}})


@pytest.mark.parametrize("message,param,flag", [
    ("Unsupported parameter: 'temperature' is not supported with this model.", "temperature", "drop_temperature"),
    ("Unsupported parameter: 'top_p' is not supported with this model.", "top_p", "drop_top_p"),
    ("Your organization must be verified to generate reasoning summaries.", "reasoning.summary", "drop_summary"),
    ("Unsupported parameter: 'reasoning.effort' is not supported with this model.", "reasoning.effort", "effort_cap"),
    ("Item 'rs_1' of type 'reasoning' was provided without its required following item.", "input", "drop_carry"),
    ("The encrypted content for item rs_1 could not be verified.", None, "drop_carry"),
    ("Unsupported parameter: 'parallel_tool_calls' is not supported with this model.", "parallel_tool_calls",
     "drop_parallel"),
    ("Invalid value for 'tool_choice'.", "tool_choice", "drop_tool_choice"),
    ("Unknown parameter: 'store'.", "store", "drop_store"),
    ("Invalid schema for function 'Bash': schema must be a JSON Schema of 'type: \"object\"'.", "tools[0].parameters",
     "strict_schema"),
    ("Invalid 'input[0].content[1]': image inputs are not supported by this model.", None, "no_vision"),
    ("Role 'developer' is not supported for this model.", None, "no_developer"),
])
def test_heal_flags(message, param, flag):
    req = {"tools": [{}], "max_output_tokens": 32000}
    new = R.heal_from_error(400, err(message, param), req, {})
    assert new is not None and flag in new


def test_heal_max_output_tokens_cap_and_escalation():
    new = R.heal_from_error(400, err("max_output_tokens must be less than or equal to 16384", "max_output_tokens"),
                            {"max_output_tokens": 32000}, {})
    assert new == {"max_tokens_cap": 16384}
    first = R.heal_from_error(400, err("Unsupported value: 'xhigh'", "reasoning.effort"), {}, {})
    assert first == {"effort_cap": "high"}
    second = R.heal_from_error(400, err("Unsupported value: 'high'", "reasoning.effort"), {}, first)
    assert second["drop_reasoning"] is True
    r = R.build_request(body([{"role": "user", "content": "x"}], output_config={"effort": "max"}), "gpt-6.1-sol",
                        "openai", stream=True, heal=first)
    assert r["reasoning"]["effort"] == "high"


def test_max_output_tokens_limit_is_never_read_from_a_model_id():
    # A cap read out of "o4-mini-2025-04-16" (2025) or "qwen3-coder-480b" (480) would be stored for a week.
    cases = [("max_output_tokens is too large for model qwen/qwen3-coder-480b-a35b-instruct; at most 8192", 8192),
             ("This model (o4-mini-2025-04-16) supports at most 16384 output tokens.", 16384),
             ("max_tokens must be at most 16384 for qwen3-coder-480b-a35b-instruct", 16384),
             ("max_output_tokens must be in range [1, 16384] for deepseek-v4", 16384)]
    for message, cap in cases:
        assert R.heal_from_error(400, err(message, "max_output_tokens"), {"max_output_tokens": 32000}, {}) == {
            "max_tokens_cap": cap}, message
        assert T.heal_from_error(400, err(message), {"max_tokens": 32000}, {})["max_tokens_cap"] == cap, message


def test_heal_uses_the_shared_overflow_detector():
    # Overflow is Core's to report ("prompt is too long", so Claude Code compacts): nothing is learned from it.
    for text in ("the request exceeds the available context size, try increasing it",
                 "This model's maximum prompt length is 131072 but the request contains 150000 tokens.",
                 "Input validation error: `inputs` tokens + `max_new_tokens` must be <= 4096. Given: 5000 `inputs` "
                 "tokens and 1000 `max_new_tokens`",
                 "context size has been exceeded", "Your input exceeds the context window of this model."):
        assert T.context_overflow(400, text), text
        assert R.heal_from_error(400, err(text), {"max_output_tokens": 32000, "tools": [{}]}, {}) is None, text
    # An output cap error is not an overflow, whatever words it uses: it heals the cap.
    for text in ("max_output_tokens exceeds the model's output token limit of 16384",
                 "max_output_tokens exceeds the maximum allowed value of 16384"):
        assert R.heal_from_error(400, err(text, "max_output_tokens"), {"max_output_tokens": 32000}, {}) == {
            "max_tokens_cap": 16384}, text


def test_heal_gives_up_on_overflow_and_other_statuses():
    assert R.heal_from_error(400, err("Your input exceeds the context window of this model.",
                                      code="context_length_exceeded"), {}, {}) is None
    assert R.heal_from_error(401, err("bad key"), {}, {}) is None
    assert R.heal_from_error(500, err("temperature"), {}, {}) is None
    assert R.heal_from_error(400, err("something nobody understands"), {}, {}) is None
    # an already-applied fix is not "new"
    assert R.heal_from_error(400, err("Unsupported parameter: 'temperature'", "temperature"), {},
                             {"drop_temperature": True}) is None


def test_build_request_accepts_translate_call_signature():
    b = body([{"role": "user", "content": "x"}])
    r = R.build_request(b, "gpt-4.1", "openai", stream=True, max_output=0, usage_in_stream=True,
                        strict_schema=False, heal={}, vision=True, tool_id_style="mistral9")
    assert "stream_options" not in r and r["stream"] is True
