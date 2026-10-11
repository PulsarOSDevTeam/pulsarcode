# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
import json

from pulsar import translate as T
from pulsar.translate import StreamTranslator, sse


def chunk(delta=None, finish=None, usage=None):
    c = {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage:
        c["usage"] = usage
    return c


def run(chunks, notice=None, emit_thinking=True):
    tr = StreamTranslator("m", input_tokens=50, emit_thinking=emit_thinking)
    ev = tr.start()
    for c in chunks:
        ev += tr.feed(c)
    ev += tr.finish(notice)
    return tr, ev


def blocks(events):
    """Reassemble content blocks from the event stream, validating the framing."""
    out = {}
    open_idx = None
    for name, p in events:
        if name == "content_block_start":
            assert open_idx is None, "two blocks open at once"
            open_idx = p["index"]
            out[open_idx] = dict(p["content_block"], _text="", _json="")
        elif name == "content_block_delta":
            assert p["index"] == open_idx
            d = p["delta"]
            if d["type"] == "text_delta":
                out[open_idx]["_text"] += d["text"]
            elif d["type"] == "thinking_delta":
                out[open_idx]["_text"] += d["thinking"]
            elif d["type"] == "input_json_delta":
                out[open_idx]["_json"] += d["partial_json"]
        elif name == "content_block_stop":
            assert p["index"] == open_idx
            open_idx = None
    assert open_idx is None
    return [out[i] for i in sorted(out)]


def test_framing_text_only():
    tr, ev = run([chunk({"content": "Hel"}), chunk({"content": "lo"}), chunk(finish="stop",
                                                                             usage={"completion_tokens": 7, "prompt_tokens": 99})])
    names = [e[0] for e in ev]
    assert names[0] == "message_start" and names[-2:] == ["message_delta", "message_stop"]
    assert ev[0][1]["message"]["usage"]["input_tokens"] == 50
    b = blocks(ev)
    assert len(b) == 1 and b[0]["type"] == "text" and b[0]["_text"] == "Hello"
    delta = ev[-2][1]
    assert delta["delta"]["stop_reason"] == "end_turn"
    assert delta["usage"] == {"output_tokens": 7, "input_tokens": 99}


def test_reasoning_then_text_then_tools_with_streamed_args():
    _, ev = run([
        chunk({"reasoning_content": "think "}),
        chunk({"reasoning_content": "more"}),
        chunk({"content": "Doing it."}),
        chunk({"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "Bash", "arguments": ""}}]}),
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"command": '}}]}),
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"ls"}'}}]}),
        chunk({"tool_calls": [{"index": 1, "id": "c2", "function": {"name": "Read", "arguments": '{"file_path":"a"}'}}]}),
        chunk(finish="tool_calls"),
    ])
    b = blocks(ev)
    assert [x["type"] for x in b] == ["thinking", "text", "tool_use", "tool_use"]
    assert b[0]["_text"] == "think more"
    # ids handed to Claude Code are always minted fresh; upstream ids only tell calls apart
    assert b[2]["id"].startswith("toolu_") and b[2]["id"] != "c1"
    assert b[2]["name"] == "Bash" and json.loads(b[2]["_json"]) == {"command": "ls"}
    assert b[3]["id"].startswith("toolu_") and b[3]["id"] != b[2]["id"]
    assert json.loads(b[3]["_json"]) == {"file_path": "a"}
    assert ev[-2][1]["delta"]["stop_reason"] == "tool_use"


def test_thinking_suppressed_when_not_requested():
    _, ev = run([chunk({"reasoning_content": "secret"}), chunk({"content": "hi"})], emit_thinking=False)
    b = blocks(ev)
    assert [x["type"] for x in b] == ["text"]


def test_a_reply_that_arrives_only_as_reasoning_is_the_answer_when_thinking_is_off():
    # A NIM-hosted model streamed its whole answer as reasoning_content: Claude Code received an empty reply.
    for tools in (None, [BASH]):
        tr = StreamTranslator("moonshotai/kimi-k3", input_tokens=50, emit_thinking=False, tools=tools)
        ev = tr.start()
        for part in ("PULSAR ", "CODE ", "ONLINE"):
            ev += tr.feed(chunk({"reasoning_content": part}))
        ev += tr.feed(chunk(finish="stop")) + tr.finish()
        assert [(x["type"], x["_text"]) for x in blocks(ev)] == [("text", "PULSAR CODE ONLINE")]
        assert ev[-2][1]["delta"]["stop_reason"] == "end_turn"
    _, ev = run([chunk({"reasoning_content": "the answer"}), chunk({"content": "\n"})], emit_thinking=False)
    assert [(x["type"], x["_text"].strip()) for x in blocks(ev)] == [("text", "the answer")]
    # The non-streamed path goes through the same finish(), <think> sections included.
    msg = T.to_anthropic_message({"choices": [{"message": {"reasoning_content": "PULSAR CODE ONLINE"},
                                               "finish_reason": "stop"}]}, "m")
    assert msg["content"] == [{"type": "text", "text": "PULSAR CODE ONLINE"}] and msg["stop_reason"] == "end_turn"
    msg = T.to_anthropic_message({"choices": [{"message": {"content": "<think>PULSAR CODE ONLINE</think>"}}]}, "m",
                                 tools=[BASH])
    assert msg["content"] == [{"type": "text", "text": "PULSAR CODE ONLINE"}]


def test_reasoning_stays_hidden_next_to_an_answer_a_call_or_a_notice():
    call_chunk = chunk({"tool_calls": [{"index": 0, "id": "c", "function": {"name": "Bash",
                                                                          "arguments": '{"command":"ls"}'}}]})
    for extra, notice in (([chunk({"content": "hi"})], None), ([call_chunk], None), ([], "upstream fell over")):
        tr = StreamTranslator("m", input_tokens=50, emit_thinking=False, tools=[BASH])
        ev = tr.start() + tr.feed(chunk({"reasoning_content": "secret plan"}))
        for c in extra:
            ev += tr.feed(c)
        ev += tr.finish(notice)
        assert "secret plan" not in json.dumps(ev)
    msg = T.to_anthropic_message({"choices": [{"message": {"content": "<think>secret plan</think>Answer"}}]}, "m")
    assert msg["content"] == [{"type": "text", "text": "Answer"}]
    # Thinking requested: the thinking block carries it, nothing is copied into the text.
    _, ev = run([chunk({"reasoning_content": "shown as thinking"})])
    assert [(x["type"], x["_text"]) for x in blocks(ev)] == [("thinking", "shown as thinking")]


def test_inline_think_tags_split_across_chunks():
    _, ev = run([chunk({"content": "<th"}), chunk({"content": "ink>plan</thi"}), chunk({"content": "nk>\nAnswer <b>"})])
    b = blocks(ev)
    assert b[0]["type"] == "thinking" and b[0]["_text"] == "plan"
    assert b[1]["type"] == "text" and b[1]["_text"] == "Answer <b>"


def test_tool_calls_without_index_split_by_id():
    _, ev = run([
        chunk({"tool_calls": [{"id": "a", "function": {"name": "X", "arguments": "{}"}}]}),
        chunk({"tool_calls": [{"id": "b", "function": {"name": "Y", "arguments": "{}"}}]}),
        chunk(finish="stop"),
    ])
    b = blocks(ev)
    assert [x["name"] for x in b] == ["X", "Y"]
    assert ev[-2][1]["delta"]["stop_reason"] == "tool_use"


def test_truncated_tool_json_is_never_run():
    # Repairing this into {"path": "x", "body": "abc"} would write a cut-off file (reproduced with real
    # Claude Code); a truncated `rm -rf ./build/cache && ...` would become `rm -rf ./build`.
    tr, ev = run([chunk({"tool_calls": [{"index": 0, "id": "c", "function": {"name": "Write",
                                                                            "arguments": '{"path": "x", "body": "abc'}}]}),
                  chunk(finish="length")])
    b = blocks(ev)
    assert all(x["type"] != "tool_use" for x in b) and tr.tools_emitted == 0 and tr.cut_calls == ["Write"]
    assert "Write" in b[0]["_text"] and "not run" in b[0]["_text"] and "output token limit" in b[0]["_text"]
    # Only the tool call was cut (dropped above, with the note): the turn ends (see StreamTranslator.finish).
    assert ev[-2][1]["delta"]["stop_reason"] == "end_turn"
    # A reply cut inside its text still reports max_tokens.
    _, ev2 = run([chunk({"content": "a long answer"}), chunk(finish="length")])
    assert ev2[-2][1]["delta"]["stop_reason"] == "max_tokens"


def test_cut_connection_mid_call_is_never_run_but_finished_calls_are():
    tr, ev = run([chunk({"tool_calls": [{"index": 0, "id": "a", "function": {"name": "Bash",
                                                                            "arguments": '{"command": "ls"}'}}]}),
                  chunk({"tool_calls": [{"index": 1, "id": "b", "function": {"name": "Bash",
                                                                            "arguments": '{"command": "git push origin f'}}]})],
                 notice="stream was cut")
    b = blocks(ev)
    uses = [x for x in b if x["type"] == "tool_use"]
    assert len(uses) == 1 and json.loads(uses[0]["_json"]) == {"command": "ls"}
    assert tr.cut_calls == ["Bash"]


def test_cut_before_any_arguments_is_not_run_but_a_clean_no_arg_call_is():
    tr, _ = run([chunk({"tool_calls": [{"index": 0, "id": "a", "function": {"name": "Bash"}}]})], notice="cut")
    assert tr.tools_emitted == 0
    tr2, ev2 = run([chunk({"tool_calls": [{"index": 0, "id": "a", "function": {"name": "Bash", "arguments": ""}}]}),
                    chunk(finish="tool_calls")])
    assert tr2.tools_emitted == 1 and ev2[-2][1]["delta"]["stop_reason"] == "tool_use"


def test_non_stream_truncated_arguments_are_never_run():
    msg = T.to_anthropic_message({"choices": [{"finish_reason": "length", "message": {"role": "assistant", "tool_calls": [
        {"id": "x", "type": "function", "function": {"name": "Bash", "arguments": '{"command": "rm -rf ./build'}}]}}]}, "m")
    assert all(b["type"] != "tool_use" for b in msg["content"]) and "not run" in msg["content"][0]["text"]


def test_notice_on_empty_stream():
    _, ev = run([], notice="upstream fell over")
    b = blocks(ev)
    assert b == [{"type": "text", "text": "", "_text": "upstream fell over", "_json": ""}]
    assert ev[-2][1]["delta"]["stop_reason"] == "end_turn"


def test_empty_stream_still_has_a_block():
    _, ev = run([])
    assert [x["type"] for x in blocks(ev)] == ["text"]


def test_activity_flags():
    tr = StreamTranslator("m")
    tr.start()
    tr.feed(chunk({"reasoning_content": "x"}))
    assert tr.active and not tr.visible
    tr.feed(chunk({"content": "y"}))
    assert tr.visible


def test_sse_format():
    s = sse(("ping", {"type": "ping"}))
    assert s == 'event: ping\ndata: {"type": "ping"}\n\n'


# ---------------------------------------------------------------------------
# v3: open-model reliability on the stream path
# ---------------------------------------------------------------------------

BASH = {"name": "Bash", "input_schema": {"type": "object", "properties": {
    "command": {"type": "string"}, "timeout": {"type": "number"}}, "required": ["command"]}}
READ = {"name": "Read", "input_schema": {"type": "object", "properties": {
    "file_path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["file_path"]}}


def run_tools(chunks, tools=(BASH, READ), notice=None):
    tr = StreamTranslator("m", tools=list(tools), input_tokens=50, emit_thinking=True)
    ev = tr.start()
    for c in chunks:
        ev += tr.feed(c)
    ev += tr.finish(notice)
    return tr, ev


def call(index=None, id=None, name=None, args=None):
    out = {"function": {}}
    if index is not None:
        out["index"] = index
    if id is not None:
        out["id"] = id
    if name is not None:
        out["function"]["name"] = name
    if args is not None:
        out["function"]["arguments"] = args
    return chunk({"tool_calls": [out]})


def test_reused_upstream_ids_never_reach_claude_code():
    # llama.cpp / Ollama builds that answer "call_0" on every turn made Claude Code loop:
    # the repeat id was marked "[Tool use interrupted]". Each turn must get fresh ids.
    seen = set()
    for _turn in range(3):
        _, ev = run([chunk({"tool_calls": [{"index": 0, "id": "call_0", "function": {
            "name": "Bash", "arguments": '{"command":"ls"}'}}]}), chunk(finish="tool_calls")])
        ids = [x["id"] for x in blocks(ev) if x["type"] == "tool_use"]
        assert len(ids) == 1 and ids[0].startswith("toolu_") and ids[0] not in seen
        seen.update(ids)


def test_parallel_calls_sharing_one_upstream_id_and_index_are_split():
    _, ev = run_tools([call(0, "call_0", "Bash", '{"command":"ls"}'),
                       call(0, "call_0", "Read", '{"file_path":"a"}'), chunk(finish="tool_calls")])
    b = blocks(ev)
    assert [(x["name"], json.loads(x["_json"])) for x in b] == [("Bash", {"command": "ls"}), ("Read", {"file_path": "a"})]
    assert b[0]["id"] != b[1]["id"]
    assert ev[-2][1]["delta"]["stop_reason"] == "tool_use"


def test_same_tool_twice_in_one_slot_is_two_calls():
    _, ev = run_tools([call(0, "call_0", "Bash", '{"command":"ls"}'), call(0, "call_0", "Bash", '{"command":"pwd"}')])
    assert [json.loads(x["_json"]) for x in blocks(ev)] == [{"command": "ls"}, {"command": "pwd"}]


def test_concatenated_argument_objects_become_separate_calls():
    _, ev = run_tools([call(0, "c", "Bash", '{"command":"l'), call(0, None, None, 's"}, {"comm'),
                       call(0, None, None, 'and":"pwd"}')])
    assert [json.loads(x["_json"]) for x in blocks(ev)] == [{"command": "ls"}, {"command": "pwd"}]


def test_a_verbatim_recap_of_the_arguments_is_not_a_second_call():
    _, ev = run_tools([call(0, "c", "Bash", '{"command":'), call(0, None, None, '"ls"}'),
                       call(0, None, None, '{"command":"ls"}')])
    assert len(blocks(ev)) == 1


def test_tool_name_arriving_in_a_later_chunk():
    _, ev = run_tools([call(0, "c", None, '{"file_path":'), call(0, None, "Read", '"x"}')])
    b = blocks(ev)
    assert b[0]["name"] == "Read" and json.loads(b[0]["_json"]) == {"file_path": "x"}


def test_nameless_call_is_named_by_its_arguments():
    _, ev = run_tools([call(0, "c", None, '{"file_path":"x","limit":"3"}')])
    b = blocks(ev)
    assert b[0]["name"] == "Read" and json.loads(b[0]["_json"]) == {"file_path": "x", "limit": 3}


def test_index_gaps_and_interleaved_fragments():
    _, ev = run_tools([call(1, "a", "Bash", '{"command":'), call(3, "b", "Read", '{"file_path":'),
                       call(1, None, None, '"ls"}'), call(3, None, None, '"a"}')])
    assert [(x["name"], json.loads(x["_json"])) for x in blocks(ev)] == [
        ("Bash", {"command": "ls"}), ("Read", {"file_path": "a"})]


def test_unstable_ids_on_continuation_chunks():
    no_index = [chunk({"tool_calls": [{"id": "x1", "function": {"name": "Bash", "arguments": '{"comm'}}]}),
                chunk({"tool_calls": [{"id": "x2", "function": {"arguments": 'and":"ls"}'}}]})]
    same_index = [call(0, "x1", "Bash", '{"comm'), call(0, "x2", None, 'and":"ls"}')]
    late_index = [chunk({"tool_calls": [{"id": "x1", "function": {"name": "Bash", "arguments": '{"comm'}}]}),
                  call(0, None, None, 'and":"ls"}')]
    for chunks in (no_index, same_index, late_index):
        b = blocks(run_tools(chunks)[1])
        assert len(b) == 1 and json.loads(b[0]["_json"]) == {"command": "ls"}


def test_legacy_function_call_deltas():
    _, ev = run_tools([chunk({"function_call": {"name": "Bash", "arguments": ""}}),
                       chunk({"function_call": {"arguments": '{"command":"ls"}'}}), chunk(finish="function_call")])
    b = blocks(ev)
    assert b[0]["name"] == "Bash" and json.loads(b[0]["_json"]) == {"command": "ls"}
    assert ev[-2][1]["delta"]["stop_reason"] == "tool_use"


def test_arguments_sent_as_objects_or_double_encoded():
    for args in ({"command": "ls"}, json.dumps(json.dumps({"command": "ls"}))):
        b = blocks(run_tools([call(0, "c", "Bash", args)])[1])
        assert json.loads(b[0]["_json"]) == {"command": "ls"}


def test_text_after_tool_calls_keeps_order_and_drops_whitespace():
    _, ev = run_tools([call(0, "c", "Bash", '{"command":"ls"}'), chunk({"content": "\n"}), chunk({"content": "Running."})])
    assert [(x["type"], x["_text"]) for x in blocks(ev)] == [("tool_use", ""), ("text", "Running.")]


def test_text_arriving_inside_open_tool_arguments_waits_for_the_call():
    _, ev = run_tools([call(0, "c", "Bash", '{"command":'), chunk({"content": "Note"}), call(0, None, None, '"ls"}')])
    b = blocks(ev)
    assert [x["type"] for x in b] == ["tool_use", "text"]
    assert json.loads(b[0]["_json"]) == {"command": "ls"} and b[1]["_text"] == "Note"


def test_only_choice_zero_is_used():
    _, ev = run([{"choices": [{"index": 1, "delta": {"content": "NO"}}, {"index": 0, "delta": {"content": "yes"}}]},
                 {"choices": [{"index": 0, "delta": {"content": "!"}}, {"index": 1, "delta": {"content": "NO"}}]}])
    assert blocks(ev)[0]["_text"] == "yes!"


def test_empty_and_malformed_deltas_are_ignored():
    _, ev = run([{"choices": [{"delta": None}]}, {"choices": []}, {"choices": None}, "junk", {},
                 {"choices": [{"delta": {"content": None, "tool_calls": None, "reasoning": None}}]},
                 {"choices": [{"delta": {"tool_calls": [None, {"index": 0}]}}]},
                 chunk({"content": "ok"})])
    assert [(x["type"], x["_text"]) for x in blocks(ev)] == [("text", "ok")]


def test_refusal_and_content_part_arrays():
    _, ev = run([chunk({"refusal": "I can't help with that."})])
    assert blocks(ev)[0]["_text"] == "I can't help with that."
    # Magistral-style content parts: thinking parts carry nested text parts
    _, ev = run([chunk({"content": [{"type": "thinking", "thinking": [{"type": "text", "text": "hmm"}]},
                                    {"type": "text", "text": "Answer"}]})])
    assert [(x["type"], x["_text"]) for x in blocks(ev)] == [("thinking", "hmm"), ("text", "Answer")]


def test_cached_tokens_and_cost_are_reported():
    tr, ev = run([chunk({"content": "hi"}),
                  {"choices": [], "usage": {"prompt_tokens": 1000, "completion_tokens": 5, "cost": 0.0012,
                                            "prompt_tokens_details": {"cached_tokens": 800}}}])
    assert tr.cache_read == 800 and tr.usage_in == 200 and abs(tr.cost - 0.0012) < 1e-12
    assert ev[-2][1]["usage"] == {"output_tokens": 5, "input_tokens": 200, "cache_read_input_tokens": 800}


def test_vendor_usage_shapes():
    # DeepSeek: prompt_cache_hit_tokens; Kimi: usage inside the choice; Groq: x_groq.usage; total_cost
    tr, _ = run([chunk({"content": "x"}, usage={"prompt_tokens": 100, "completion_tokens": 3, "prompt_cache_hit_tokens": 64})])
    assert (tr.cache_read, tr.usage_in) == (64, 36)
    tr, _ = run([{"choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": "stop",
                               "usage": {"prompt_tokens": 9, "completion_tokens": 2, "cached_tokens": 4}}]}])
    assert (tr.cache_read, tr.usage_in, tr.usage_out) == (4, 5, 2)
    tr, _ = run([{"choices": [{"delta": {"content": "x"}}], "x_groq": {"usage": {"prompt_tokens": 7, "completion_tokens": 1}}}])
    assert (tr.usage_in, tr.usage_out) == (7, 1)
    tr, _ = run([chunk({"content": "x"}, usage={"prompt_tokens": 1, "completion_tokens": 1, "total_cost": "0.5"})])
    assert tr.cost == 0.5
    tr, ev = run([chunk({"content": "x"}, usage={"prompt_tokens": 10, "completion_tokens": 1})])
    assert tr.cache_read == 0 and tr.cost == 0.0 and "cache_read_input_tokens" not in ev[-2][1]["usage"]


def test_stop_reason_never_claims_tool_use_without_a_tool():
    _, ev = run([chunk({"content": "hi"}), chunk(finish="tool_calls")])
    assert ev[-2][1]["delta"]["stop_reason"] == "end_turn"
    _, ev = run([chunk({"content": "hi"}), chunk(finish="model_length")])
    assert ev[-2][1]["delta"]["stop_reason"] == "max_tokens"


def test_tool_argument_chars_count_as_output():
    tr, _ = run_tools([call(0, "c", "Bash", '{"command":"' + "x" * 400 + '"}')])
    assert tr.out_chars >= 400


def test_malformed_numbers_never_end_the_stream():
    # json.loads accepts Infinity and NaN; gateways send figures as strings; "²".isdigit() is True.
    for usage in ({"completion_tokens": float("inf"), "prompt_tokens": "²"}, {"prompt_tokens": "9" * 5000},
                  {"completion_tokens": "n/a", "cost": "inf"}, {"cost": 10 ** 400}, {"total_cost": float("nan")},
                  {"prompt_tokens": 9, "prompt_tokens_details": {"cached_tokens": float("-inf")}}):
        tr, ev = run([chunk({"content": "ok"}, usage=usage)])
        assert [(x["type"], x["_text"]) for x in blocks(ev)] == [("text", "ok")] and ev[-1][0] == "message_stop"
        assert tr.cost == 0.0 and isinstance(ev[-2][1]["usage"]["output_tokens"], int)
    _, ev = run_tools([chunk({"tool_calls": [{"index": "²", "id": "a", "function": {
        "name": "Bash", "arguments": '{"command":"ls"}'}}]})])
    assert [(x["name"], json.loads(x["_json"])) for x in blocks(ev)] == [("Bash", {"command": "ls"})]
    assert T._int(" 12 ") == 12 and T._int(3.9) == 3 and T._int(-4) == 0 and T._float("0.5") == 0.5
