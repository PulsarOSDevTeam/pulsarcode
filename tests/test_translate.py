# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
from pulsar import translate as T


def body(messages, **kw):
    return {"model": "m", "max_tokens": 32000, "messages": messages, **kw}


def test_system_and_simple_turns():
    out = T.messages_to_openai(body([{"role": "user", "content": "hi"}],
                                    system=[{"type": "text", "text": "A"}, {"type": "text", "text": "B"}]))
    assert out[0] == {"role": "system", "content": "A\n\nB"}
    assert out[1] == {"role": "user", "content": "hi"}


def test_tool_roundtrip_and_ordering():
    msgs = [
        {"role": "user", "content": "run it"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "hmm", "signature": "x"},
            {"type": "text", "text": "ok"},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
            {"type": "tool_use", "id": "t2", "name": "Read", "input": {"path": "a"}},
        ]},
        {"role": "user", "content": [
            {"type": "text", "text": "also this"},
            {"type": "tool_result", "tool_use_id": "t2", "content": [{"type": "text", "text": "file"}]},
            {"type": "tool_result", "tool_use_id": "t1", "content": "out", "is_error": True},
        ]},
    ]
    out = T.messages_to_openai(body(msgs))
    roles = [m["role"] for m in out]
    assert roles == ["user", "assistant", "tool", "tool", "user"]
    assert out[1]["content"] == "ok"
    assert [c["id"] for c in out[1]["tool_calls"]] == ["t1", "t2"]
    assert out[1]["tool_calls"][0]["function"]["arguments"] == '{"command":"ls"}'
    assert out[2] == {"role": "tool", "tool_call_id": "t2", "content": "file"}
    assert out[3]["content"].startswith("Error: out")
    assert out[4] == {"role": "user", "content": "also this"}


def test_interrupted_tool_calls_are_patched():
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]},
        {"role": "user", "content": "never mind"},
    ]
    out = T.messages_to_openai(body(msgs))
    assert [m["role"] for m in out] == ["user", "assistant", "tool", "user"]
    assert out[2]["tool_call_id"] == "t1" and "interrupted" in out[2]["content"]


def test_orphan_tool_result_becomes_text():
    msgs = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "ghost", "content": "x"}]}]
    out = T.messages_to_openai(body(msgs))
    assert out == [{"role": "user", "content": "Tool result (ghost):\nx"}]


def test_mid_conversation_system_is_folded_without_splitting_tools():
    msgs = [
        {"role": "system", "content": "early note"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]},
        {"role": "system", "content": "late note"},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "r"}]},
    ]
    out = T.messages_to_openai(body(msgs, system="S"))
    assert out[0] == {"role": "system", "content": "S\n\nearly note"}
    roles = [m["role"] for m in out]
    assert roles == ["system", "user", "assistant", "tool", "user"]
    assert "late note" in out[-1]["content"] and "<system-reminder>" in out[-1]["content"]
    assert all("_soft" not in m for m in out)


def test_images_become_image_url_parts():
    img = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
    out = T.messages_to_openai(body([{"role": "user", "content": [{"type": "text", "text": "see"}, img]}]))
    parts = out[0]["content"]
    assert parts[0] == {"type": "text", "text": "see"}
    assert parts[1]["image_url"]["url"] == "data:image/png;base64,AAAA"
    out = T.messages_to_openai(body([{"role": "user", "content": [img]}]), vision=False)
    assert "not forwarded" in out[0]["content"]


def test_consecutive_user_text_merges():
    out = T.messages_to_openai(body([{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]))
    assert out == [{"role": "user", "content": "a\n\nb"}]


def test_schema_sanitizer():
    schema = {"$schema": "x", "type": "object", "additionalProperties": False,
              "properties": {"u": {"type": ["string", "null"], "format": "uri"}, "n": {"type": "integer", "minimum": 1}}}
    loose = T.sanitize_schema(schema)
    assert "$schema" not in loose and loose["additionalProperties"] is False
    strict = T.sanitize_schema(schema, strict=True)
    assert "additionalProperties" not in strict
    assert strict["properties"]["u"] == {"type": "string"}
    assert strict["properties"]["n"]["minimum"] == 1


def test_tools_and_choice():
    tools = [{"name": "Bash", "description": "d", "input_schema": {"type": "object", "properties": {}}},
             {"type": "web_search_20250305", "name": "web_search"}]
    out = T.tools_to_openai(tools)
    assert len(out) == 1 and out[0]["function"]["name"] == "Bash"
    assert T.tool_choice_to_openai({"type": "any"}) == "required"
    assert T.tool_choice_to_openai({"type": "tool", "name": "Bash"})["function"]["name"] == "Bash"
    assert T.tool_choice_to_openai(None) is None


def test_build_request_caps_and_reasoning_family():
    b = body([{"role": "user", "content": "x"}], temperature=0.2, top_p=0.9, stop_sequences=["a"] * 6)
    r = T.build_request(b, "meta/llama", "nvidia", stream=True, max_output=16384)
    assert r["max_tokens"] == 16384 and r["temperature"] == 0.2 and len(r["stop"]) == 4
    assert "stream_options" not in r
    r = T.build_request(b, "gpt-5", "openai", stream=True, usage_in_stream=True)
    assert "max_tokens" not in r and r["max_completion_tokens"] == 32000
    assert "temperature" not in r and "top_p" not in r
    assert r["stream_options"] == {"include_usage": True}


def test_heal_from_error():
    req = {"max_tokens": 32000, "temperature": 1.0, "tools": [1], "stream_options": {}}
    h = T.heal_from_error(400, '{"error":{"message":"max_tokens must be <= 8192"}}', req, {})
    assert h == {"max_tokens_cap": 8192}
    h = T.heal_from_error(400, "max_tokens is too large", req, {})
    assert h["max_tokens_cap"] == 16000
    h = T.heal_from_error(400, "Unsupported parameter: 'max_tokens' ... use 'max_completion_tokens'", req, {})
    assert h == {"max_completion_tokens": True}
    assert T.heal_from_error(400, "unknown field stream_options", req, {}) == {"drop_stream_options": True}
    assert T.heal_from_error(400, "temperature not supported", req, {}) == {"drop_temperature": True}
    assert T.heal_from_error(400, "Invalid schema for function", req, {}) == {"strict_schema": True}
    assert T.heal_from_error(400, "unrelated", req, {}) is None
    assert T.heal_from_error(500, "max_tokens", req, {}) is None
    once = {"drop_temperature": True}
    assert T.heal_from_error(400, "temperature not supported", req, once) is None


def test_build_request_applies_heal():
    b = body([{"role": "user", "content": "x"}], temperature=0.3)
    r = T.build_request(b, "m", "groq", stream=False, heal={"max_tokens_cap": 4096, "drop_temperature": True})
    assert r["max_tokens"] == 4096 and "temperature" not in r


def test_repair_json():
    assert T.parse_arguments('{"a": 1') == {"a": 1}
    assert T.parse_arguments('{"a": "unterminated') == {"a": "unterminated"}
    assert T.parse_arguments('```json\n{"a": [1, 2]}\n```') == {"a": [1, 2]}
    assert T.parse_arguments("") == {}
    assert T.parse_arguments("[1,2]") == {"value": [1, 2]}
    assert T.parse_arguments("garbage{") == {"_raw": "garbage{"}


def test_nonstream_response():
    data = {"choices": [{"message": {"content": "<think>x</think>Answer", "tool_calls": [
        {"id": "c1", "function": {"name": "Bash", "arguments": '{"command":"ls"}'}}]}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 9, "completion_tokens": 3}}
    msg = T.to_anthropic_message(data, "m")
    assert msg["content"][0] == {"type": "text", "text": "Answer"}
    assert msg["content"][1]["input"] == {"command": "ls"}
    assert msg["stop_reason"] == "tool_use"
    assert msg["usage"] == {"input_tokens": 9, "output_tokens": 3}


def test_estimate_tokens_grows_with_input():
    small = T.estimate_tokens(body([{"role": "user", "content": "x"}]))
    big = T.estimate_tokens(body([{"role": "user", "content": "x" * 4000}]))
    assert big > small + 900


# ---------------------------------------------------------------------------
# v3: request fields, caching, ids and healing
# ---------------------------------------------------------------------------

import json  # noqa: E402

BASH_TOOL = {"name": "Bash", "description": "run", "input_schema": {
    "type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}


def cached_body():
    return body([
        {"role": "user", "content": [{"type": "text", "text": "q1", "cache_control": {"type": "ephemeral"}}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "files", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "and now?", "cache_control": {"type": "ephemeral", "ttl": "1h"}}]},
    ], system=[{"type": "text", "text": "static", "cache_control": {"type": "ephemeral"}},
               {"type": "text", "text": "dynamic"}], tools=[BASH_TOOL])


def markers(req):
    return [p.get("cache_control") for m in req["messages"] if isinstance(m["content"], list)
            for p in m["content"] if "cache_control" in p]


def test_openrouter_anthropic_and_google_keep_cache_breakpoints():
    for model in ("anthropic/claude-sonnet-4.5", "google/gemini-2.5-pro"):
        r = T.build_request(cached_body(), model, "openrouter", stream=True)
        sys_msg = r["messages"][0]
        assert sys_msg["role"] == "system" and sys_msg["content"][0] == {
            "type": "text", "text": "static", "cache_control": {"type": "ephemeral"}}
        assert sys_msg["content"][1] == {"type": "text", "text": "dynamic"}
        tool_msg = next(m for m in r["messages"] if m["role"] == "tool")
        assert tool_msg["content"] == [{"type": "text", "text": "files", "cache_control": {"type": "ephemeral"}}]
        last = r["messages"][-1]
        assert last["content"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
        assert len(markers(r)) == 4


def test_cache_markers_are_stripped_elsewhere_and_on_request():
    for model, provider, cache in (("meta/llama", "nvidia", None), ("openai/gpt-5", "openrouter", None),
                                   ("anthropic/claude-sonnet-4.5", "openrouter", False)):
        r = T.build_request(cached_body(), model, provider, stream=True, cache=cache)
        assert "cache_control" not in json.dumps(r)
        assert r["messages"][0] == {"role": "system", "content": "static\n\ndynamic"}
    r = T.build_request(cached_body(), "anthropic/x", "openrouter", stream=True, heal={"drop_cache": True})
    assert "cache_control" not in json.dumps(r)
    r = T.build_request(cached_body(), "my-model", "custom", stream=True, cache=True)
    assert markers(r)


def test_at_most_four_breakpoints_keep_first_and_latest():
    msgs = [{"role": "user" if i % 2 == 0 else "assistant",
             "content": [{"type": "text", "text": "m%d" % i, "cache_control": {"type": "ephemeral"}}]} for i in range(7)]
    r = T.build_request(body(msgs, system=[{"type": "text", "text": "S", "cache_control": {"type": "ephemeral"}}]),
                        "anthropic/x", "openrouter", stream=False)
    kept = [p["text"] for m in r["messages"] if isinstance(m["content"], list)
            for p in m["content"] if "cache_control" in p]
    assert kept == ["S", "m4", "m5", "m6"]


def test_cache_marker_on_an_image_moves_to_the_nearest_text_part():
    img = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA"},
           "cache_control": {"type": "ephemeral"}}
    r = T.build_request(body([{"role": "user", "content": [{"type": "text", "text": "look"}, img]}]),
                        "google/gemini-2.5-flash", "openrouter", stream=False)
    parts = r["messages"][0]["content"]
    assert parts[0]["cache_control"] == {"type": "ephemeral"} and "cache_control" not in parts[1]


def test_anthropic_only_fields_are_never_forwarded():
    b = body([{"role": "user", "content": "x"}], tools=[BASH_TOOL], metadata={"user_id": "user_abc_account_123"},
             top_k=40, thinking={"type": "enabled", "budget_tokens": 2048}, service_tier="auto",
             tool_choice={"type": "auto", "disable_parallel_tool_use": True})
    for provider in ("nvidia", "groq", "openai", "custom", "openrouter"):
        r = T.build_request(b, "some/model", provider, stream=True)
        blob = json.dumps(r)
        assert "user_abc_account_123" not in blob and "metadata" not in r and "user" not in r
        assert "top_k" not in r and "thinking" not in r and "service_tier" not in r
        assert r["parallel_tool_calls"] is False and r["tool_choice"] == "auto"


def test_parallel_tool_calls_only_with_tools():
    b = body([{"role": "user", "content": "x"}], tool_choice={"type": "auto", "disable_parallel_tool_use": True})
    assert "parallel_tool_calls" not in T.build_request(b, "m", "groq", stream=True)
    b["tools"] = [BASH_TOOL]
    assert "parallel_tool_calls" not in T.build_request(b, "m", "groq", stream=True,
                                                        heal={"drop_parallel_tool_calls": True})
    b["tool_choice"] = {"type": "auto"}
    assert "parallel_tool_calls" not in T.build_request(b, "m", "groq", stream=True)


def test_openrouter_reasoning_is_conservative():
    b = body([{"role": "user", "content": "x"}], thinking={"type": "enabled", "budget_tokens": 4000})
    assert T.build_request(b, "qwen/qwen3-235b", "openrouter", stream=True)["reasoning"] == {"enabled": True}
    assert "reasoning" not in T.build_request(b, "anthropic/claude-x", "openrouter", stream=True)
    assert "reasoning" not in T.build_request(b, "qwen/qwen3-235b", "openrouter", stream=True,
                                              heal={"drop_reasoning": True})
    assert "reasoning" not in T.build_request(b, "qwen/qwen3-235b", "together", stream=True)
    b["thinking"] = {"type": "disabled"}
    assert "reasoning" not in T.build_request(b, "qwen/qwen3-235b", "openrouter", stream=True)


def test_duplicate_tool_ids_in_history_are_made_unique_upstream():
    msgs = [{"role": "user", "content": "go"}]
    for cmd in ("ls", "pwd"):
        msgs.append({"role": "assistant", "content": [{"type": "tool_use", "id": "call_0", "name": "Bash",
                                                       "input": {"command": cmd}}]})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_0", "content": cmd + "!"}]})
    out = T.messages_to_openai(body(msgs))
    calls = [m["tool_calls"][0]["id"] for m in out if m.get("tool_calls")]
    results = [m["tool_call_id"] for m in out if m["role"] == "tool"]
    assert calls[0] != calls[1] and results == calls
    assert [m["content"] for m in out if m["role"] == "tool"] == ["ls!", "pwd!"]


def test_mistral_nine_character_ids_stay_paired():
    msgs = [{"role": "user", "content": "go"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_01ABCdef", "name": "Bash", "input": {}},
                                              {"type": "tool_use", "id": "toolu_02XYZ", "name": "Bash", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_02XYZ", "content": "b"},
                                         {"type": "tool_result", "tool_use_id": "toolu_01ABCdef", "content": "a"}]}]
    r = T.build_request(body(msgs), "mistral-large-latest", "mistral", stream=True, tool_id_style="mistral9")
    ids = [c["id"] for c in r["messages"][1]["tool_calls"]]
    assert all(len(i) == 9 and i.isalnum() for i in ids) and ids[0] != ids[1]
    assert [m["tool_call_id"] for m in r["messages"] if m["role"] == "tool"] == [ids[1], ids[0]]
    assert T.mistral_id("abcDEF123") == "abcDEF123" and T.mistral_id("x") == T.mistral_id("x")
    healed = T.build_request(body(msgs), "m", "custom", stream=True, heal={"tool_id_style": "mistral9"})
    assert all(len(c["id"]) == 9 for c in healed["messages"][1]["tool_calls"])


def test_mistral_family_folds_user_text_after_tool_results():
    msgs = [{"role": "user", "content": "go"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "out"},
                                         {"type": "text", "text": "<system-reminder>x</system-reminder>"}]}]
    for kw in ({"tool_id_style": "mistral9"}, {"heal": {"fold_after_tool": True}}):
        r = T.build_request(body(msgs), "devstral", "custom", stream=True, **kw)
        assert [m["role"] for m in r["messages"]] == ["user", "assistant", "tool"]
        assert r["messages"][-1]["content"] == "out\n\n<system-reminder>x</system-reminder>"
    plain = T.build_request(body(msgs), "llama", "custom", stream=True)
    assert [m["role"] for m in plain["messages"]] == ["user", "assistant", "tool", "user"]


def test_prompted_tools_for_hosts_without_native_tool_calling():
    msgs = [{"role": "user", "content": "go"},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"},
                                              {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "a.py"},
                                         {"type": "text", "text": "next"}]}]
    r = T.build_request(body(msgs, system="SYS", tools=[BASH_TOOL], tool_choice={"type": "any"}), "gemma3", "ollama",
                        stream=True, heal={"prompt_tools": True})
    assert "tools" not in r and "tool_choice" not in r
    sys_text = r["messages"][0]["content"]
    assert sys_text.startswith("SYS\n\n# Tools") and '"name":"Bash"' in sys_text and "<tool_call>" in sys_text
    assert "must call at least one function" in sys_text
    roles = [m["role"] for m in r["messages"]]
    assert roles == ["system", "user", "assistant", "user"] and "tool" not in roles
    assert r["messages"][2]["content"] == 'ok\n\n<tool_call>\n{"name":"Bash","arguments":{"command":"ls"}}\n</tool_call>'
    assert r["messages"][3]["content"] == "<tool_response>\na.py\n</tool_response>\n\nnext"
    assert not any("tool_calls" in m for m in r["messages"])


def test_alternating_templates_and_missing_system_role():
    msgs = [{"role": "assistant", "content": "hello"}, {"role": "user", "content": "a"}, {"role": "user", "content": "b"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "r"}]}]
    r = T.build_request(body(msgs, system="SYS"), "gemma", "custom", stream=True, heal={"alternate": True})
    roles = [m["role"] for m in r["messages"]]
    assert roles == ["user", "assistant", "user", "assistant", "user"]
    assert r["messages"][0]["content"].startswith("SYS")
    r = T.build_request(body([{"role": "user", "content": "q"}], system="SYS"), "gemma", "custom", stream=True,
                        heal={"no_system": True})
    assert r["messages"] == [{"role": "user", "content": "SYS\n\nq"}]


def test_heal_from_error_v3_fixes():
    req = {"max_tokens": 32000, "tools": [1], "stream_options": {}, "parallel_tool_calls": False, "reasoning": {}}
    cases = [
        ('{"error":"registry.ollama.ai/library/gemma3:latest does not support tools"}', {"prompt_tools": True}),
        ('"auto" tool choice requires --enable-auto-tool-choice and --tool-call-parser to be set', {"prompt_tools": True}),
        ("tools param requires --jinja flag", {"prompt_tools": True}),
        ("This model does not support tool use.", {"prompt_tools": True}),
        ("Error: function calling is not enabled for this deployment", {"prompt_tools": True}),
        ("Tool call id was toolu_abc but must be a-z, A-Z, 0-9, with a length of 9.", {"tool_id_style": "mistral9"}),
        ("Unexpected role 'user' after role 'tool'", {"fold_after_tool": True}),
        ("Conversation roles must alternate user/assistant/user/assistant/...", {"alternate": True}),
        ("System role not supported", {"no_system": True}),
        ("messages.0.content.0.cache_control: Extra inputs are not permitted", {"drop_cache": True}),
        ("parallel_tool_calls is not supported by this model", {"drop_parallel_tool_calls": True}),
        ("reasoning is not supported for this model", {"drop_reasoning": True}),
    ]
    for text, want in cases:
        assert T.heal_from_error(400, text, req, {}) == want, text
        assert T.heal_from_error(400, text, req, want) is None, text   # never the same fix twice
    no_tools = {"max_tokens": 100}
    assert T.heal_from_error(400, "does not support tools", no_tools, {}) is None


def test_context_overflow_detection_across_vendors():
    overflow = [
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
    for status, text in overflow:
        assert T.context_overflow(status, text), text
    fine = [
        (400, "This model's maximum context length is 32768 tokens. However, you requested 40000 tokens "
              "(8000 in the messages, 32000 in the completion)."),
        (422, "Input validation error: `inputs` tokens + `max_new_tokens` must be <= 4096. Given: 3000 `inputs` "
              "tokens and 2000 `max_new_tokens`"),
        (413, "Request too large for model in organization on tokens per minute (TPM): Limit 6000, Requested 9000"),
        (400, "max_tokens is too large for model qwen/qwen3-coder-480b-a35b-instruct; at most 8192"),
        (400, "max_output_tokens exceeds the model's output token limit of 16384"),
        (429, "maximum context length"),
        (400, "temperature is not supported"),
        (400, ""),
    ]
    for status, text in fine:
        assert not T.context_overflow(status, text), text


def test_parse_arguments_v3_repairs():
    assert T.parse_arguments(json.dumps(json.dumps({"a": 1}))) == {"a": 1}       # double-encoded
    assert T.parse_arguments('{"a": 1}</tool_call>') == {"a": 1}               # trailing junk
    assert T.parse_arguments("{'a': True, 'b': None}") == {"a": True, "b": None}  # python literal
    assert T.parse_arguments('{"a": 1,}') == {"a": 1}                           # trailing comma
    assert T.parse_arguments('{"a": 1, "b": ') == {"a": 1}                      # dangling member
    assert T.parse_arguments("null") == {} and T.parse_arguments(None) == {}
    assert T.parse_arguments({"x": 1}) == {"x": 1}
    assert T.parse_arguments('{"a": "x\\') == {"a": "x"}


def test_nonstream_mints_fresh_ids_and_reports_cache():
    data = {"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "call_0", "function": {"name": "Bash", "arguments": '{"command":"ls"}'}},
        {"id": "call_0", "function": {"name": "Bash", "arguments": '{"command":"pwd"}'}}]}, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 9, "prompt_tokens_details": {"cached_tokens": 60}}}
    msg = T.to_anthropic_message(data, "m", tools=[BASH_TOOL])
    ids = [b["id"] for b in msg["content"]]
    assert len(set(ids)) == 2 and all(i.startswith("toolu_") and i != "call_0" for i in ids)
    assert [b["input"] for b in msg["content"]] == [{"command": "ls"}, {"command": "pwd"}]
    assert msg["usage"] == {"input_tokens": 40, "output_tokens": 9, "cache_read_input_tokens": 60}
    assert msg["stop_reason"] == "tool_use"


def test_nonstream_legacy_function_call_refusal_and_choice_zero():
    data = {"choices": [{"index": 0, "message": {"function_call": {"name": "Bash", "arguments": '{"command":"ls"}'}},
                         "finish_reason": "function_call"}]}
    msg = T.to_anthropic_message(data, "m")
    assert msg["content"][0]["name"] == "Bash" and msg["stop_reason"] == "tool_use"
    data = {"choices": [{"index": 1, "message": {"content": "other"}},
                        {"index": 0, "message": {"content": None, "refusal": "No."}}]}
    msg = T.to_anthropic_message(data, "m")
    assert msg["content"] == [{"type": "text", "text": "No."}]
    assert T.to_anthropic_message({}, "m")["content"] == [{"type": "text", "text": ""}]
    assert T.to_anthropic_message({"choices": [{"message": {"content": [{"type": "text", "text": "a"},
                                                                       {"type": "text", "text": "b"}]}}]},
                                  "m")["content"] == [{"type": "text", "text": "ab"}]


def test_nonstream_think_without_opening_tag():
    msg = T.to_anthropic_message({"choices": [{"message": {"content": "plan steps</think>\n\nAnswer"}}]}, "m")
    assert msg["content"] == [{"type": "text", "text": "Answer"}]


def test_engine_compatible_signatures():
    import inspect
    assert "tool_id_style" in inspect.signature(T.build_request).parameters
    assert "cache" in inspect.signature(T.build_request).parameters
    T.StreamTranslator("m", tools=[BASH_TOOL], input_tokens=3, emit_thinking=False)
    T.to_anthropic_message({"choices": []}, "m", 3, tools=[BASH_TOOL])
    tr = T.StreamTranslator("m")
    for attr in ("active", "visible", "tools_emitted", "out_chars", "usage_out", "usage_in", "cache_read", "cost"):
        assert hasattr(tr, attr)
