# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""tools/compat: the scripted upstreams, transcript parsing, checks and summary (no Claude Code needed)."""

import importlib.util
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMPAT = ROOT / "tools" / "compat"
WORKFLOW = ROOT / ".github" / "workflows" / "compat.yml"


def _load(name):
    sys.path.insert(0, str(COMPAT))
    spec = importlib.util.spec_from_file_location("compat_" + name, str(COMPAT / (name + ".py")))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fakes = _load("fakes")
compat = _load("run")
NONCE = "abc123"
PROBE_CMD = "python run.py probe"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post(url, body, key=fakes.FAKE_KEY, header="authorization"):
    headers = {"content-type": "application/json"}
    if key:
        headers[header] = ("Bearer " + key) if header == "authorization" else key
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    with OPENER.open(req, timeout=10) as resp:
        return resp.status, resp.headers.get("content-type", ""), resp.read().decode()


def get(url, key=fakes.FAKE_KEY):
    req = urllib.request.Request(url, headers={"authorization": "Bearer " + key} if key else {})
    with OPENER.open(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode())


def sse_data(text):
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ") and line[6:] != "[DONE]"]


PROBE_OUT = fakes.PROBE_TAG + ' {"context": "196608", "count_tokens": {"status": 200, "input_tokens": 18}}\n'


# ---------------------------------------------------------------------------
# OpenAI chat-completions fake
# ---------------------------------------------------------------------------

def test_openai_fake_catalog_and_auth():
    with fakes.OpenAIUpstream("m1", NONCE, PROBE_CMD, context=196608) as up:
        status, data = get(up.base_url + "/models")
        assert status == 200 and data["data"][0] == {"id": "m1", "object": "model", "owned_by": "compat",
                                                     "context_length": 196608}
        with pytest.raises(urllib.error.HTTPError) as exc:
            get(up.base_url + "/models", key="sk-something-else")
        assert exc.value.code == 401
        with pytest.raises(urllib.error.HTTPError) as exc:
            post(up.base_url + "/chat/completions", {"messages": []}, key="")
        assert exc.value.code == 401


def test_openai_fake_scripted_tool_turn_then_streamed_answer():
    tools = [{"type": "function", "function": {"name": "Bash"}}, {"type": "function", "function": {"name": "Read"}}]
    with fakes.OpenAIUpstream("m1", NONCE, PROBE_CMD) as up:
        user = {"role": "user", "content": [{"type": "text", "text": fakes.MARKER + " " + NONCE + ": go"}]}
        status, ctype, text = post(up.base_url + "/chat/completions",
                                   {"model": "m1", "stream": True, "messages": [user], "tools": tools})
        assert status == 200 and ctype.startswith("text/event-stream")
        chunks = sse_data(text)
        calls = [c for ch in chunks for choice in ch["choices"] for c in choice["delta"].get("tool_calls", [])]
        assert calls[0]["function"]["name"] == "Bash"
        args = json.loads("".join(c["function"]["arguments"] for c in calls))
        assert args["command"] == PROBE_CMD
        assert chunks[-2]["choices"][0]["finish_reason"] == "tool_calls" or any(
            ch["choices"] and ch["choices"][0]["finish_reason"] == "tool_calls" for ch in chunks)
        assert "data: [DONE]" in text
        assert up.tool_calls == 1 and up.probe is None

        tool_msg = {"role": "tool", "tool_call_id": "call_compat_1", "content": PROBE_OUT}
        status, _, text = post(up.base_url + "/chat/completions",
                               {"model": "m1", "stream": True, "messages": [user, {"role": "assistant"}, tool_msg],
                                "tools": tools})
        pieces = [ch["choices"][0]["delta"].get("content") for ch in sse_data(text) if ch["choices"]]
        words = [p for p in pieces if p]
        assert "".join(words).endswith("COMPAT-OK " + NONCE) and len(words) >= 3
        assert up.tool_results == 1 and up.probe["context"] == "196608"
        assert [e["action"] for e in up.log if "action" in e] == ["tool", "final"]
        assert all(e["stream"] for e in up.log if "action" in e)


def test_openai_fake_side_requests_get_plain_ok():
    with fakes.OpenAIUpstream("m1", NONCE, PROBE_CMD) as up:
        # No Bash tool declared (a title request): never a tool call, even with the marker.
        _, _, text = post(up.base_url + "/chat/completions",
                          {"stream": False, "messages": [{"role": "user", "content": fakes.MARKER}]})
        body = json.loads(text)
        assert body["choices"][0]["message"]["content"] == "ok"
        assert up.tool_calls == 0 and up.log[-1]["action"] == "plain"


# ---------------------------------------------------------------------------
# Anthropic Messages fake
# ---------------------------------------------------------------------------

def events(text):
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


def test_anthropic_fake_tool_use_stream_then_answer():
    tools = [{"name": "Bash", "input_schema": {}}, {"name": "Read", "input_schema": {}}]
    with fakes.AnthropicUpstream("m2", NONCE, PROBE_CMD) as up:
        assert up.base_url.endswith("/anthropic")
        user = {"role": "user", "content": [{"type": "text", "text": fakes.MARKER + " " + NONCE}]}
        _, ctype, text = post(up.base_url + "/v1/messages", {"model": "m2", "stream": True, "messages": [user],
                                                             "tools": tools}, header="x-api-key")
        assert ctype.startswith("text/event-stream")
        evs = events(text)
        assert evs[0]["type"] == "message_start" and evs[-1]["type"] == "message_stop"
        start = next(e for e in evs if e["type"] == "content_block_start")
        assert start["content_block"]["type"] == "tool_use" and start["content_block"]["name"] == "Bash"
        partial = "".join(e["delta"]["partial_json"] for e in evs if e["type"] == "content_block_delta")
        assert json.loads(partial)["command"] == PROBE_CMD
        assert next(e for e in evs if e["type"] == "message_delta")["delta"]["stop_reason"] == "tool_use"

        result = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_compat_1",
                                               "content": [{"type": "text", "text": PROBE_OUT}]}]}
        _, _, text = post(up.base_url + "/v1/messages",
                          {"model": "m2", "stream": True, "messages": [user, {"role": "assistant", "content": []},
                                                                       result], "tools": tools})
        deltas = [e["delta"]["text"] for e in events(text) if e["type"] == "content_block_delta"]
        assert len(deltas) >= 3 and "".join(deltas).endswith("COMPAT-OK " + NONCE)
        assert up.probe["count_tokens"]["input_tokens"] == 18


def test_anthropic_fake_count_tokens_models_and_auth():
    with fakes.AnthropicUpstream("m2", NONCE, PROBE_CMD) as up:
        status, _, text = post(up.base_url + "/v1/messages/count_tokens", {"messages": []})
        assert status == 200 and json.loads(text) == {"input_tokens": 42}
        assert get(up.base_url + "/v1/models")[1]["data"][0]["id"] == "m2"
        with pytest.raises(urllib.error.HTTPError) as exc:
            post(up.base_url + "/v1/messages", {"messages": []}, key="sk-ant-other")
        assert exc.value.code == 401
        _, _, text = post(up.base_url + "/v1/messages", {"model": "m2", "messages": [{"role": "user", "content": "hi"}]})
        assert json.loads(text)["content"] == [{"type": "text", "text": "ok"}]


def test_parse_probe_and_text_of():
    assert fakes.parse_probe("noise\n" + PROBE_OUT)["context"] == "196608"
    assert fakes.parse_probe("no probe here") is None
    assert fakes.parse_probe(fakes.PROBE_TAG + " {not json}\n") is None
    assert fakes.text_of([{"type": "text", "text": "a"}, {"type": "tool_result", "content": "b"}, "c"]) == "a\nb\nc"
    assert fakes.text_of(None) == ""


# ---------------------------------------------------------------------------
# the probe subcommand
# ---------------------------------------------------------------------------

def test_probe_counts_tokens_and_reports_env(monkeypatch, capsys):
    for var in list(compat.os.environ):
        if var.startswith(("ANTHROPIC_", "CLAUDE_CODE_")):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "204800")
    with fakes.AnthropicUpstream("m2", NONCE, PROBE_CMD) as up:
        monkeypatch.setenv("ANTHROPIC_BASE_URL", up.base_url)
        # The fake stands in for Core's count_tokens; it only accepts the fake marker as a token.
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "fake-key-not-real-session")
        assert compat.main(["probe"]) == 0
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "pls_session_token_for_test")
        compat.probe()
    first, second = capsys.readouterr().out.strip().splitlines()
    assert "fake-key-not-real-session" not in first and "pls_session_token_for_test" not in second
    probe = fakes.parse_probe(first + "\n")
    assert probe["context"] == "204800" and probe["has_session_token"] is True
    assert probe["count_tokens"] == {"status": 200, "input_tokens": 42}
    assert probe["provider_key_visible"] is True          # the marker is in the environment here
    other = fakes.parse_probe(second + "\n")
    assert other["count_tokens"] == {"status": 401} and other["provider_key_visible"] is False


def test_probe_without_core_reports_nothing_counted(monkeypatch, capsys):
    for var in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_MAX_CONTEXT_TOKENS"):
        monkeypatch.delenv(var, raising=False)
    compat.probe()
    probe = fakes.parse_probe(capsys.readouterr().out)
    assert probe["count_tokens"] == {"status": 0} and probe["context"] == "" and probe["has_session_token"] is False


# ---------------------------------------------------------------------------
# transcript parsing, checks, summary
# ---------------------------------------------------------------------------

def transcript_lines(answer, window=200000):
    lines = [{"type": "system", "subtype": "init", "model": "m1"},
             {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]}},
             {"type": "user", "message": {"content": [{"type": "tool_result", "content": PROBE_OUT}]}}]
    for piece in fakes.final_words(NONCE):
        lines.append({"type": "stream_event", "event": {"type": "content_block_delta",
                                                        "delta": {"type": "text_delta", "text": piece}}})
    lines.append({"type": "result", "is_error": False, "result": answer,
                  "modelUsage": {"m1": {"contextWindow": window, "maxOutputTokens": 32000}}})
    return "\n".join(json.dumps(x) for x in lines) + "\nnot json\n"


class _Up:
    def __init__(self, probe, tool_calls=1, tool_results=1):
        self.probe = probe
        self.tool_calls = tool_calls
        self.tool_results = tool_results
        self.log = [{"action": "tool", "stream": True}, {"action": "final", "stream": True},
                    {"action": "plain", "stream": False}]


GOOD_PROBE = {"context": "196608", "base_url": "http://127.0.0.1:4747", "has_session_token": True,
              "provider_key_visible": False, "count_tokens": {"status": 200, "input_tokens": 18},
              "compact": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "196608",
                          "CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE": "173608"}}
# What Claude Code's debug log says about auto-compact for a 196608 window (minus its 20k answer reserve).
DEBUG = "2026-10-03T00:00:00Z [DEBUG] autocompact: tokens=[REDACTED] level=ok effectiveWindow=176608\n"


def test_parse_transcript():
    t = compat.parse_transcript(transcript_lines("done COMPAT-OK " + NONCE))
    assert t["text_deltas"] == len(fakes.final_words(NONCE))
    assert t["streamed_text"].endswith("COMPAT-OK " + NONCE)
    assert t["tool_uses"] == ["Bash"] and fakes.PROBE_TAG in t["tool_results"][0]
    assert t["init"]["model"] == "m1" and t["result"]["result"].endswith(NONCE)
    assert compat.context_windows(t["result"]) == [200000]
    assert compat.context_windows(None) == []


def test_evaluate_all_green():
    t = compat.parse_transcript(transcript_lines("COMPAT-OK " + NONCE))
    checks = compat.evaluate("openai", 0, t, _Up(dict(GOOD_PROBE)), 196608, 196608, NONCE, DEBUG)
    assert set(checks) == set(compat.CHECKS)
    assert all(c["ok"] for c in checks.values()), checks
    assert "advisory" not in checks["context_effective"]       # required since the window is set
    assert "auto-compacts at 163608" in checks["context_effective"]["detail"]


@pytest.mark.parametrize("change, failing", [
    ({"context": "200000"}, "context_window"),
    ({"count_tokens": {"status": 401}}, "count_tokens"),
    ({"count_tokens": {"status": 200, "input_tokens": 0}}, "count_tokens"),
    ({"provider_key_visible": True}, "key_isolation"),
    ({"base_url": "https://api.example.com"}, "key_isolation"),
])
def test_evaluate_flags_the_broken_check(change, failing):
    t = compat.parse_transcript(transcript_lines("COMPAT-OK " + NONCE))
    checks = compat.evaluate("openai", 0, t, _Up(dict(GOOD_PROBE, **change)), 196608, 196608, NONCE, DEBUG)
    assert [k for k, c in checks.items() if not c["ok"]] == [failing]


def test_evaluate_launch_stream_tool_and_effective_window_failures():
    t = compat.parse_transcript(transcript_lines("something else", window=200000))
    checks = compat.evaluate("openai", 1, t, _Up(None, tool_results=0), 196608, 196608, NONCE)
    bad = {k for k, c in checks.items() if not c["ok"]}
    assert {"launch", "tool_roundtrip", "count_tokens", "context_window", "context_effective",
            "key_isolation"} <= bad
    assert not compat.case_ok(checks)
    # The window Pulsar 3.0.0 gave Claude Code before the fix (behavesAs only: 200k, no auto-compact
    # override) fails context_effective, which now fails the case.
    old = compat.evaluate("openai", 0, compat.parse_transcript(transcript_lines("COMPAT-OK " + NONCE, 200000)),
                          _Up(dict(GOOD_PROBE, compact={})), 196608, 196608, NONCE)
    assert [k for k, c in old.items() if not c["ok"]] == ["context_effective"]
    assert "auto-compacts at 167000" in old["context_effective"]["detail"]
    assert not compat.case_ok(old)
    assert not compat.case_ok({})


def test_advisory_checks_only_fail_with_strict(monkeypatch):
    monkeypatch.setattr(compat, "ADVISORY", ("context_effective",))
    good = compat.evaluate("openai", 0, compat.parse_transcript(transcript_lines("COMPAT-OK " + NONCE)),
                           _Up(dict(GOOD_PROBE, compact={})), 196608, 196608, NONCE)
    assert not good["context_effective"]["ok"] and good["context_effective"]["advisory"] is True
    assert compat.case_ok(good) and not compat.case_ok(good, strict=True)


def test_no_streaming_when_upstream_turn_was_not_streamed():
    up = _Up(dict(GOOD_PROBE))
    up.log[1]["stream"] = False
    t = compat.parse_transcript(transcript_lines("COMPAT-OK " + NONCE))
    assert not compat.evaluate("openai", 0, t, up, 196608, 196608, NONCE)["streaming"]["ok"]


def test_summary_written_atomically_with_required_keys(tmp_path):
    adv = {"ok": False, "detail": "200000", "advisory": True}
    results = [{"case": "openai", "ok": True, "checks": {"launch": {"ok": True, "detail": ""}, "context_effective": adv}},
               {"case": "anthropic", "ok": True, "checks": {"launch": {"ok": True, "detail": ""}}}]
    summary = compat.summarize("2.1.288", results, "latest")
    assert summary["ok"] is True and summary["claude_version"] == "2.1.288"
    assert summary["claude_channel"] == "latest" and summary["advisories"] == ["openai/context_effective: 200000"]
    assert re.match(r"\d+\.\d+\.\d+", summary["pulsar_version"])
    out = tmp_path / "sub" / "compat.json"
    compat.write_summary(str(out), summary)
    assert json.loads(out.read_text(encoding="utf-8")) == summary
    assert [p.name for p in out.parent.iterdir()] == ["compat.json"]
    assert compat.summarize("x", [])["ok"] is False
    assert compat.summarize("x", results + [{"case": "setup", "ok": False}])["ok"] is False


def test_report_marks_advisories(capsys):
    summary = compat.summarize("2.1.288", [{"case": "openai", "ok": True, "checks": {
        "launch": {"ok": True, "detail": "fine"},
        "context_effective": {"ok": False, "detail": "200000", "advisory": True}}}])
    compat.report(summary)
    out = capsys.readouterr().out
    assert "[ok] launch" in out and "[--] context_effective" in out and "(advisory)" in out
    assert out.rstrip().endswith("compat: PASS")


def test_main_without_claude_writes_a_failed_summary(tmp_path, capsys):
    out = tmp_path / "compat.json"
    rc = compat.main(["--claude", str(tmp_path / "missing" / "claude"), "--out", str(out)])
    assert rc == 2
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["ok"] is False and data["results"][0]["case"] == "setup"


@pytest.mark.parametrize("args, needle", [
    (["--cases", "nope"], "unknown case(s): nope"),
    (["--cases", ","], "unknown case(s): (none given)"),
    (["--env", "1BAD=x"], "bad --env"),
    (["--env", "NOEQUALS"], "bad --env"),
])
def test_main_bad_arguments_still_write_a_failed_summary(tmp_path, capsys, args, needle):
    out = tmp_path / "sub" / "bad.json"
    assert compat.main(["--claude", str(tmp_path / "missing")] + args + ["--out", str(out),
                                                                        "--channel", "x"]) == 2
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["ok"] is False and data["claude_channel"] == "x"
    assert data["results"][0]["case"] == "setup" and needle in data["results"][0]["error"]


def test_readme_matches_run_behaviour():
    readme = (COMPAT / "README.md").read_text(encoding="utf-8")
    assert "`$PULSAR_CLAUDE_BIN`, else `claude` on `PATH`" in readme
    assert "After all cases, Core is stopped" in readme and "(kept with `--keep`)" in readme
    steps = readme.split("## What a run does", 1)[1].split("After all cases", 1)[0]
    assert "Core is stopped" not in steps


def test_base_env_inherits_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key-not-real-env")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:1")
    env = compat.base_env("/opt/x/bin/claude", str(tmp_path), str(tmp_path / "ph"), 5555)
    assert "OPENAI_API_KEY" not in env and "HTTPS_PROXY" not in env
    assert env["HOME"] == str(tmp_path) and env["PULSAR_PORT"] == "5555" and env["PULSAR_RELAY"] == "off"
    assert env["PATH"].split(":")[0] == "/opt/x/bin"


# ---------------------------------------------------------------------------
# the workflow
# ---------------------------------------------------------------------------

def _pins(path):
    return set(re.findall(r"uses: (\S+@[0-9a-f]{40}) # v\d", path.read_text(encoding="utf-8")))


def test_compat_workflow_shape():
    yaml = pytest.importorskip("yaml")
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    on = wf.get("on", wf.get(True))
    cron = on["schedule"][0]["cron"]
    assert cron.split()[0] != "0" and "workflow_dispatch" in on
    assert wf["permissions"] == {"contents": "read"}
    job = wf["jobs"]["compat"]
    assert job["runs-on"] == "ubuntu-latest" and job["strategy"]["matrix"]["channel"] == ["stable", "latest"]
    steps = job["steps"]
    setup = next(s for s in steps if s.get("uses", "").startswith("actions/setup-python@"))
    assert setup["with"]["python-version"] == "3.12"
    assert any("@anthropic-ai/claude-code@$CHANNEL" in s.get("run", "") for s in steps)
    assert any("tools/compat/run.py" in s.get("run", "") for s in steps)
    upload = next(s for s in steps if s.get("uses", "").startswith("actions/upload-artifact@"))
    assert upload["if"] == "always()" and upload["with"]["path"] == "compat.json"
    for step in steps:
        for expr in re.findall(r"\$\{\{([^}]*)\}\}", step.get("run", "")):
            assert not re.search(r"github\.|inputs\.|secrets\.|matrix\.", expr), expr


def test_compat_workflow_reuses_the_existing_pins():
    text = WORKFLOW.read_text(encoding="utf-8")
    refs = re.findall(r"uses: (\S+)", text)
    assert refs and all(re.match(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$", r) for r in refs)
    known = _pins(ROOT / ".github" / "workflows" / "test.yml") | _pins(ROOT / ".github" / "workflows" / "release.yml")
    assert _pins(WORKFLOW) <= known and len(_pins(WORKFLOW)) == len(refs)


def test_compat_files_follow_the_wording_rules():
    for path in [WORKFLOW, COMPAT / "README.md", COMPAT / "run.py", COMPAT / "fakes.py", Path(__file__)]:
        text = path.read_text(encoding="utf-8")
        assert chr(0x2014) not in text, path
        assert " ".join(("free", "claude", "code")) not in text.lower(), path


# ---------------------------------------------------------------------------
# window cases and the effective window
# ---------------------------------------------------------------------------

def test_window_cases():
    assert compat.case_window("openai-65536") == 65536 and compat.case_window("openai-1000000") == 1_000_000
    assert compat.case_window("openai") == 0 and compat.case_window("openai-12") == 0
    assert compat.case_window("openai-99999999") == 0 and compat.case_window("anthropic-65536") == 0
    assert compat.known_case("openai-131072") and compat.known_case("anthropic")
    assert not compat.known_case("openai-x") and not compat.known_case("nope")
    # The defaults run a small and a large window besides the two providers.
    assert {"openai-65536", "openai-1000000"} <= set(compat.CASES)
    assert all(compat.known_case(c) for c in compat.CASES)


@pytest.mark.parametrize("window, target", [
    (32_768, 32_768), (131_072, 131_072), (200_000, 200_000), (262_144, 200_000),
    (999_999, 200_000), (1_000_000, 1_000_000), (2_000_000, 1_000_000)])
def test_window_target_never_exceeds_the_model(window, target):
    assert compat.window_target(window) == target <= window


def _result(window):
    return {"modelUsage": {"m": {"contextWindow": window, "maxOutputTokens": 32000}}}


@pytest.mark.parametrize("window, reported, debug, compact, ok", [
    # 1M: the [1m] id, nothing else to log.
    (1_000_000, 1_000_000, "", {}, True),
    (2_000_000, 1_000_000, "", {}, True),
    (1_000_000, 200_000, "", {}, False),                 # what 3.0.0 gave Claude Code: far too early
    # 256k: 200k is the best below it; 1M would be above the window.
    (262_144, 200_000, "", {}, True),
    (262_144, 1_000_000, "", {}, False),
    # 64k: Claude Code reports 200k but compacts at the real window's threshold.
    (65_536, 200_000, "effectiveWindow=80000", {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "55.29",
                                                "CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE": "50381"}, True),
    (65_536, 200_000, "effectiveWindow=80000", {"CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE": "50381"}, False),
    (65_536, 200_000, "effectiveWindow=80000", {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "55.29"}, False),
    (65_536, 200_000, "", {}, False),
    # 32k: compaction and blocking above Claude Code's ~26.5k fixed prompt prefix. What the first 3.0.0 fix
    # set (20480 and 23552) left both under it: every turn a failed compaction.
    (32_768, 200_000, "effectiveWindow=80000", {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "37.5",
                                                "CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE": "31384"}, True),
    (32_768, 200_000, "effectiveWindow=80000", {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "25.6",
                                                "CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE": "23552"}, False),
    (32_768, 200_000, "effectiveWindow=80000", {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "37.5",
                                                "CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE": "29000"}, False),
    (131_072, 200_000, "effectiveWindow=111072", {"CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE": "108072"}, True),
    (131_072, 200_000, "effectiveWindow=180000", {"CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE": "108072"}, False),
    (131_072, 131_072, "", {}, True),                    # a Claude Code that reports the real window
    (131_072, 196_608, "", {}, False),
])
def test_effective_window(window, reported, debug, compact, ok):
    log = ("x [DEBUG] autocompact: tokens=[REDACTED] level=ok " + debug + "\n") if debug else ""
    ctx = compat.claude_context(_result(reported), log, compact)
    assert compat.effective_ok(window, ctx) is ok, ctx


def test_claude_context_reads_claude_codes_own_numbers():
    log = "autocompact: tokens=[REDACTED] level=warn effectiveWindow=80000\n"
    ctx = compat.claude_context(_result(200_000), log, {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "55.29",
                                                        "CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE": "50381"})
    assert ctx == {"reported": 200_000, "compact_window": 100_000, "threshold": 44_232, "blocking": 50_381,
                   "reserve": 20_000}
    bare = compat.claude_context(None, "", {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "junk"})
    assert bare["reported"] == 0 and not compat.effective_ok(65_536, bare)
    assert not compat.effective_ok(0, compat.claude_context(_result(200_000), "", {}))


def test_probe_reports_the_compact_overrides(monkeypatch, capsys):
    for var in list(compat.COMPACT_VARS) + ["ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "100000")
    monkeypatch.setenv("ANTHROPIC_MODEL", "m[1m]")
    assert compat.probe() == 0
    found = fakes.parse_probe(capsys.readouterr().out)
    assert found["compact"] == {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "100000"} and found["model"] == "m[1m]"


def test_main_accepts_window_cases(tmp_path):
    out = tmp_path / "c.json"
    assert compat.main(["--claude", str(tmp_path / "missing"), "--cases", "openai-65536", "--out", str(out)]) == 2
    assert json.loads(out.read_text(encoding="utf-8"))["results"][0]["error"] == "claude not found"
    assert compat.main(["--claude", str(tmp_path / "missing"), "--cases", "openai-1", "--out", str(out)]) == 2
    assert "unknown case(s): openai-1" in json.loads(out.read_text(encoding="utf-8"))["results"][0]["error"]


# ---------------------------------------------------------------------------
# pins: provider switches in the Claude Code binary against Pulsar's pin list
# ---------------------------------------------------------------------------

FAKE_BINARY = (b"\x00\x7fELF..." + b"x" * 5000 + b'var iq=["CLAUDE_CODE_USE_BEDROCK","CLAUDE_CODE_USE_VERTEX",'
               b'"CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD","CLAUDE_CODE_USE_GATEWAY"];a.CLAUDE_CODE_USE_POWERSHELL_TOOL;'
               b'function f(){return a.ANTHROPIC_GOOGLE_CLOUD_BASE_URL??a.ANTHROPIC_BASE_URL}'
               b'"CLAUDE_CODE_SKIP_ANTHROPIC_GOOGLE_CLOUD_AUTH",VOICE_STREAM_BASE_URL,XCLAUDE_CODE_USE_NOPE_Y'
               + b"y" * 5000)


def test_binary_env_names_reads_routing_names_across_chunks(tmp_path):
    exe = tmp_path / "claude"
    exe.write_bytes(FAKE_BINARY)
    names = compat.binary_env_names(str(exe), chunk=97)          # names straddle chunk borders
    assert names == sorted(["ANTHROPIC_BASE_URL", "ANTHROPIC_GOOGLE_CLOUD_BASE_URL",
                            "CLAUDE_CODE_SKIP_ANTHROPIC_GOOGLE_CLOUD_AUTH", "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD",
                            "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_GATEWAY", "CLAUDE_CODE_USE_POWERSHELL_TOOL",
                            "CLAUDE_CODE_USE_VERTEX", "VOICE_STREAM_BASE_URL"])
    assert compat.binary_env_names(str(tmp_path / "missing")) == []


def test_pin_coverage_flags_a_provider_switch_pulsar_does_not_pin(tmp_path, monkeypatch):
    from pulsar import plugin
    exe = tmp_path / "claude"
    exe.write_bytes(FAKE_BINARY)
    names = compat.binary_env_names(str(exe))
    cov = compat.pin_coverage(names)
    assert cov["ok"] and cov["missing"] == [] and cov["project_only"] == ["VOICE_STREAM_BASE_URL"]
    # the pin list of round 1 (no Claude Platform on Google Cloud, no gateway): the check fails and names them
    old = [n for n in plugin.pinned_names() if "GOOGLE_CLOUD" not in n and "GATEWAY" not in n]
    monkeypatch.setattr(plugin, "pinned_names", lambda: old)
    cov = compat.pin_coverage(names)
    assert not cov["ok"] and cov["missing"] == ["ANTHROPIC_GOOGLE_CLOUD_BASE_URL",
                                                "CLAUDE_CODE_SKIP_ANTHROPIC_GOOGLE_CLOUD_AUTH",
                                                "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD", "CLAUDE_CODE_USE_GATEWAY"]
    assert "CLAUDE_CODE_USE_GATEWAY" in cov["detail"] and "POWERSHELL" not in cov["detail"]
    assert not compat.pin_coverage([])["ok"]                     # nothing found is a failure, not a pass
    entry = compat.run_case("pins", [], {"PULSAR_CLAUDE_BIN": str(exe)}, str(tmp_path), 1, str(tmp_path))
    assert entry["case"] == "pins" and entry["ok"] is False and list(entry["checks"]) == ["pins"]


# ---------------------------------------------------------------------------
# picker: Claude Code's /model list keeps the 1M row and Default is the launched model
# ---------------------------------------------------------------------------

def _init_answer(models):
    return "\n".join([json.dumps({"type": "system", "subtype": "init"}),
                      json.dumps({"type": "control_response", "response": {
                          "subtype": "success", "request_id": "compat-init", "response": {"models": models}}})])


DEFAULT_OK = {"value": "default", "resolvedModel": "compat-chat-model", "description": "currently compat-chat-model"}
ROWS = [{"value": "compat-chat-model", "resolvedModel": "compat-chat-model"},
        {"value": "compat-long-model[1m]", "resolvedModel": "compat-long-model[1m]"}]


@pytest.mark.parametrize("models, failing", [
    ([DEFAULT_OK] + ROWS, []),
    # round 1: CLAUDE_CODE_DISABLE_1M_CONTEXT=1 hid every 1M row
    ([DEFAULT_OK] + ROWS[:1], ["picker_long_row"]),
    # before it: Default upgraded the 131k model to a 1M window
    ([dict(DEFAULT_OK, resolvedModel="compat-chat-model[1m]")] + ROWS, ["picker_default"]),
    ([], ["picker_default", "picker_long_row"]),
])
def test_evaluate_picker(models, failing):
    got = compat.picker_models(_init_answer(models))
    assert got == models
    checks = compat.evaluate_picker(got, compat.OPENAI_MODEL, compat.LONG_MODEL)
    assert set(checks) == set(compat.PICKER_CHECKS)
    assert [k for k, c in checks.items() if not c["ok"]] == failing
    assert compat.picker_models("not json\n{}") == []


def test_openai_fake_serves_extra_catalog_rows():
    with fakes.OpenAIUpstream("m1", NONCE, PROBE_CMD, context=131072, extra_models=[("m-long", 1000000)]) as up:
        _, data = get(up.base_url + "/models")
    assert [(m["id"], m["context_length"]) for m in data["data"]] == [("m1", 131072), ("m-long", 1000000)]


def test_new_cases_are_known_and_documented():
    assert {"picker", "pins"} <= set(compat.CASES) and all(compat.known_case(c) for c in compat.CASES)
    readme = (COMPAT / "README.md").read_text(encoding="utf-8")
    for word in ("`picker`", "`pins`", "picker_default", "picker_long_row", "initialize"):
        assert word in readme, word
