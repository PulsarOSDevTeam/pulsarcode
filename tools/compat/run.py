#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Claude Code compatibility check: the installed `claude` CLI, headless, through Pulsar Core.

    python tools/compat/run.py [--claude PATH] [--out compat.json] [--cases openai,anthropic]
                               [--timeout SECONDS] [--keep]

For each case it starts a scripted provider on loopback (tools/compat/fakes.py: an OpenAI
chat-completions one and an Anthropic Messages one), connects it to a scratch PULSAR_HOME with
`pulsarcode key --stdin` and `pulsarcode model`, exactly as a user would, and runs

    pulsarcode -p "<prompt>" --output-format stream-json --verbose --include-partial-messages
               --allowedTools Bash

under a scrubbed environment (the equivalent of `env -i`: a scratch HOME, no credentials, no
proxy variables). The scripted model asks Claude Code to run a Bash probe (this file's `probe`
subcommand), which reports what Claude Code's session sees: the context window it was given
(CLAUDE_CODE_MAX_CONTEXT_TOKENS) and a count_tokens call to Core with the session's own token.
After the tool result the model streams its final answer.

Cases: openai (a 196608 token window), anthropic, and openai-<window> (the OpenAI fake serving that
window; openai-65536 and openai-1000000 run by default); picker (Claude Code's /model list, read from its
SDK `initialize` answer, for a 131072 model launched with a 1M model on offer: the 1M row must stay and
the Default row must resolve to the launched model) and pins (the provider switches, Anthropic base URLs
and auth skips named in the Claude Code executable must all be pinned in a Pulsar session).

Checks per case: launch, streaming, tool_roundtrip, count_tokens, context_window, context_effective
(the window Claude Code itself works with: the one its final result event reports and the auto-compact
threshold and blocking limit from its debug log and overrides; never above the model's window, and the
largest Claude Code can use below it) and key_isolation. Checks listed in ADVISORY (none at the moment)
are recorded but only fail the run with --strict. The summary is
written to --out as {claude_version, results, ok, advisories, ...} and the exit status is 0 only
when every required check of every case passed (2 for a setup problem such as a missing `claude`).

Run it with a Python that has Pulsar Code's runtime dependencies (requirements.txt). It never
reads your ~/.claude or ~/.pulsarcode and stops every process it starts by PID.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
for _p in (REPO, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fakes  # noqa: E402

CASES = ("openai", "anthropic", "openai-65536", "openai-1000000", "picker", "pins")
OPENAI_MODEL = "compat-chat-model"
OPENAI_CONTEXT = 196_608          # served by the fake catalog: distinct from every built-in default
ANTHROPIC_MODEL = "compat-glm-5"  # no catalog on custom Messages endpoints: Pulsar's table gives the window
WINDOW_CASE = re.compile(r"^openai-(\d{4,8})$")   # openai-<window>: the OpenAI fake serving that window
CHECKS = ("launch", "streaming", "tool_roundtrip", "count_tokens", "context_window", "context_effective",
          "key_isolation")
ADVISORY: Tuple[str, ...] = ()
# picker: the OpenAI fake serves a small model and a 1M one; Claude Code's own /model list (its SDK
# `initialize` answer) must keep the 1M row and resolve the Default row to the launched model as given.
PICKER_CHECKS = ("picker_default", "picker_long_row")
PICKER_WINDOW = 131_072
LONG_MODEL, LONG_WINDOW = "compat-long-model", 1_000_000
INIT_REQUEST = json.dumps({"type": "control_request", "request_id": "compat-init",
                           "request": {"subtype": "initialize"}})
# pins: every provider switch, Anthropic base URL and auth skip the Claude Code binary names must be pinned
# in a Pulsar session (pulsar.plugin.pinned_names). These CLAUDE_CODE_USE_* switches do not route the model.
PIN_CHECKS = ("pins",)
NOT_ROUTING = frozenset(("CLAUDE_CODE_USE_POWERSHELL_TOOL", "CLAUDE_CODE_USE_COWORK_PLUGINS",
                         "CLAUDE_CODE_USE_CCR_V2", "CLAUDE_CODE_USE_NATIVE_FILE_SEARCH"))
_ENV_NAME = re.compile(rb"(?<![A-Za-z0-9_])(CLAUDE_CODE_USE_[A-Z0-9_]+|[A-Z][A-Z0-9_]*_BASE_URL|"
                       rb"CLAUDE_CODE_SKIP_[A-Z0-9_]+_AUTH)(?![A-Za-z0-9_])")
COMPACT_VARS = ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
                "CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE")


def case_window(case: str) -> int:
    """The window an openai-<window> case serves (0 for any other case name)."""
    m = WINDOW_CASE.match(case)
    n = int(m.group(1)) if m else 0
    return n if 1024 <= n <= 10_000_000 else 0


def known_case(case: str) -> bool:
    return case in CASES or case_window(case) > 0


# ---------------------------------------------------------------------------
# pins: the routing variables in the Claude Code binary against Pulsar's pin list
# ---------------------------------------------------------------------------

def binary_env_names(path: str, chunk: int = 1 << 22) -> List[str]:
    """CLAUDE_CODE_USE_*, *_BASE_URL and CLAUDE_CODE_SKIP_*_AUTH names found in a Claude Code executable or
    script (read in overlapping chunks, never executed)."""
    found = set()
    tail = b""
    try:
        with open(os.path.realpath(path), "rb") as fh:
            while True:
                block = fh.read(chunk)
                if not block:
                    break
                data = tail + block
                found.update(m.decode("ascii") for m in _ENV_NAME.findall(data))
                tail = data[-256:]
    except OSError:
        return []
    return sorted(found)


def must_pin(name: str) -> bool:
    """A name that routes or authenticates Claude Code's model traffic: a provider switch, an Anthropic base
    URL or an auth skip."""
    if name.startswith("CLAUDE_CODE_USE_"):
        return name not in NOT_ROUTING
    return name.startswith("CLAUDE_CODE_SKIP_") or (name.startswith("ANTHROPIC_") and name.endswith("BASE_URL"))


def pin_coverage(names: List[str]) -> Dict[str, Any]:
    """{"ok", "detail", "missing", "project_only"}: routing names of the binary that Pulsar does not pin.

    project_only: other *_BASE_URL names, pinned only when a project's settings set them (the plugin's
    pattern rule), listed for information."""
    from pulsar import plugin
    pinned = set(plugin.pinned_names())
    must = [n for n in names if must_pin(n)]
    missing = [n for n in must if n not in pinned]
    project_only = [n for n in names if not must_pin(n) and n.endswith("_BASE_URL") and n not in pinned]
    ok = bool(must) and not missing
    if not must:
        detail = "no provider switch found in the Claude Code executable (unreadable or an unknown layout)"
    elif missing:
        detail = "not pinned in a Pulsar session (add them to pulsar.launch.SCRUB): %s" % ", ".join(missing)
    else:
        detail = "all %d provider switches, Anthropic base URLs and auth skips are pinned" % len(must)
    if project_only:
        detail += "; pinned when a project sets them: %s" % ", ".join(project_only)
    return {"ok": ok, "detail": detail, "missing": missing, "project_only": project_only}


def run_pins(claude: str) -> Dict[str, Any]:
    cov = pin_coverage(binary_env_names(claude))
    check = {"ok": cov["ok"], "detail": cov["detail"]}
    if "pins" in ADVISORY:
        check["advisory"] = True
    return {"case": "pins", "ok": case_ok({"pins": check}), "checks": {"pins": check},
            "missing": cov["missing"]}


# ---------------------------------------------------------------------------
# picker: Claude Code's /model list for a session launched on a small model, with a 1M row on offer
# ---------------------------------------------------------------------------

def picker_models(stdout: str) -> List[Dict[str, Any]]:
    """The model rows of Claude Code's answer to the SDK `initialize` request ([] when there is none)."""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "control_response":
            resp = (ev.get("response") or {}).get("response") or {}
            models = resp.get("models") if isinstance(resp, dict) else None
            if isinstance(models, list):
                return [m for m in models if isinstance(m, dict)]
    return []


def evaluate_picker(models: List[Dict[str, Any]], small: str, long: str) -> Dict[str, Dict[str, Any]]:
    """picker_default: the Default row resolves to the launched model as given (no "[1m]" upgrade);
    picker_long_row: the 1M model's "[1m]" row is still listed."""
    checks: Dict[str, Dict[str, Any]] = {}
    default = next((m for m in models if m.get("value") == "default"), None)
    resolved = str((default or {}).get("resolvedModel", ""))
    checks["picker_default"] = {"ok": resolved == small, "detail": "Default resolves to %s (launched: %s)" % (
        resolved or "nothing", small)}
    want = long + "[1m]"
    rows = [str(m.get("value", "")) for m in models if m.get("value") != "default"]
    checks["picker_long_row"] = {"ok": want in rows, "detail": "rows: %s (want %s)" % (", ".join(rows) or "none",
                                                                                       want)}
    for name in checks:
        if name in ADVISORY:
            checks[name]["advisory"] = True
    return checks


def run_picker(pulsar: List[str], env: Dict[str, str], project: str, timeout: float, log_dir: str,
               strict: bool = False) -> Dict[str, Any]:
    nonce = secrets.token_hex(6)
    upstream = fakes.OpenAIUpstream(OPENAI_MODEL, nonce, "true", context=PICKER_WINDOW,
                                    extra_models=[(LONG_MODEL, LONG_WINDOW)])
    entry: Dict[str, Any] = {"case": "picker", "wire": upstream.dialect, "model": OPENAI_MODEL}
    with upstream:
        for name, argv, stdin in [("key", pulsar + ["key", "--stdin"], upstream.base_url + " " + fakes.FAKE_KEY),
                                  ("model", pulsar + ["model", OPENAI_MODEL], None)]:
            rc, out, err = run(argv, env, project, 120, stdin)
            if rc != 0:
                entry.update(ok=False, error="pulsarcode %s failed (%d): %s" % (name, rc, (err or out).strip()[-400:]))
                entry["checks"] = {c: {"ok": False, "detail": "setup failed"} for c in PICKER_CHECKS}
                return entry
        started = time.time()
        rc, out, err = run(pulsar + ["-p", "--input-format", "stream-json", "--output-format", "stream-json",
                                     "--verbose"], env, project, timeout, INIT_REQUEST + "\n")
        entry["seconds"] = round(time.time() - started, 1)
    with open(os.path.join(log_dir, "picker.stdout.jsonl"), "w", encoding="utf-8") as f:
        f.write(out)
    with open(os.path.join(log_dir, "picker.stderr.txt"), "w", encoding="utf-8") as f:
        f.write(err)
    checks = evaluate_picker(picker_models(out), OPENAI_MODEL, LONG_MODEL)
    entry["checks"] = checks
    entry["ok"] = case_ok(checks, strict)
    if not entry["ok"] and err.strip():
        entry["stderr_tail"] = err.strip()[-600:]
    return entry


# ---------------------------------------------------------------------------
# probe: runs inside Claude Code's Bash tool
# ---------------------------------------------------------------------------

def probe() -> int:
    """Print what this Claude Code session sees, as `COMPAT_PROBE {json}`. Never prints a token."""
    env = os.environ
    out: Dict[str, Any] = {
        "context": env.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS", ""),
        "model": env.get("ANTHROPIC_MODEL", ""),
        "compact": {k: env[k] for k in COMPACT_VARS if k in env},
        "base_url": env.get("ANTHROPIC_BASE_URL", ""),
        "has_session_token": bool(env.get("ANTHROPIC_AUTH_TOKEN")),
        "provider_key_visible": any("fake-key-not-real" in v for v in env.values()),
    }
    count: Dict[str, Any] = {"status": 0}
    base, token = env.get("ANTHROPIC_BASE_URL", "").rstrip("/"), env.get("ANTHROPIC_AUTH_TOKEN", "")
    if base and token:
        body = json.dumps({"model": env.get("ANTHROPIC_MODEL") or "compat",
                           "messages": [{"role": "user", "content": "Pulsar compat probe: count these words."}]})
        req = urllib.request.Request(base + "/v1/messages/count_tokens", data=body.encode(), method="POST",
                                     headers={"content-type": "application/json",
                                              "authorization": "Bearer " + token,
                                              "anthropic-version": "2023-06-01"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=20) as resp:
                count["status"] = resp.status
                data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
                count["input_tokens"] = data.get("input_tokens") if isinstance(data, dict) else None
        except urllib.error.HTTPError as exc:
            count["status"] = exc.code
        except (OSError, ValueError) as exc:
            count["error"] = type(exc).__name__
    out["count_tokens"] = count
    print(fakes.PROBE_TAG + " " + json.dumps(out, sort_keys=True))
    return 0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def free_port() -> int:
    """A free loopback port to start Core on (Core moves up from it by itself if it is taken)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def claude_version(binary: str, env: Dict[str, str]) -> str:
    try:
        out = subprocess.run([binary, "--version"], env=env, stdin=subprocess.DEVNULL, capture_output=True,
                             text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r"\d+\.\d+\.\d+[\w.-]*", out or "")
    return m.group(0) if m else (out or "").strip()[:80]


def base_env(claude: str, home: str, pulsar_home: str, port: int) -> Dict[str, str]:
    """The whole environment Pulsar and Claude Code get: nothing is inherited from the caller."""
    path = [os.path.dirname(claude)]
    node = shutil.which("node")
    if node:
        path.append(os.path.dirname(node))
    path += ["/usr/local/bin", "/usr/bin", "/bin"]
    seen: List[str] = []
    for p in path:
        if p and p not in seen:
            seen.append(p)
    return {
        "HOME": home, "PATH": os.pathsep.join(seen), "PULSAR_HOME": pulsar_home,
        "PULSAR_PYTHON": sys.executable, "PULSAR_CLAUDE_BIN": claude, "PULSAR_PORT": str(port),
        "PULSAR_RELAY": "off", "PULSAR_FX": "off", "PULSAR_NOTIFY": "off", "PULSAR_HAPTICS": "off",
        "NO_COLOR": "1", "TERM": "dumb", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TMPDIR": os.path.join(home, "tmp"),
    }


def run(argv: List[str], env: Dict[str, str], cwd: str, timeout: float,
        stdin: Optional[str] = None) -> Tuple[int, str, str]:
    """Run argv in its own process group; on timeout the whole group is killed by its PID."""
    proc = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        out, err = proc.communicate()
        return 124, out, err + "\n[compat] timed out after %ds" % timeout


def pulsar_state(env: Dict[str, str]) -> Dict[str, Any]:
    """Profiles as Pulsar itself reads them, plus its context guess for the native model."""
    code = ("import json,sys; sys.path.insert(0, sys.argv[1]); from pulsar import config as K, catalog as C;"
            "s=K.load(); print(json.dumps({'active': s.active, 'profiles': {k: {'model': p.model, "
            "'context': p.context, 'native': p.native} for k, p in s.profiles.items()}, "
            "'guess': C.guess_context(sys.argv[2])}))")
    rc, out, err = run([sys.executable, "-E", "-c", code, REPO, ANTHROPIC_MODEL], env, REPO, 60)
    if rc != 0:
        raise RuntimeError("could not read the scratch Pulsar state: " + err.strip()[-300:])
    return json.loads(out)


def stop_core(pulsar: List[str], env: Dict[str, str], pulsar_home: str) -> None:
    """`pulsarcode stop`, then make sure the Core this run started is gone (by its recorded PID)."""
    pid = 0
    try:
        with open(os.path.join(pulsar_home, "run", "core.json"), encoding="utf-8") as f:
            pid = int(json.load(f).get("pid") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        pid = 0
    run(pulsar + ["stop"], env, REPO, 30)
    if pid > 0:
        for _ in range(30):
            try:
                os.kill(pid, 0)
            except OSError:
                return
            time.sleep(0.1)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# reading Claude Code's stream-json transcript
# ---------------------------------------------------------------------------

def parse_transcript(stdout: str) -> Dict[str, Any]:
    """What a `--output-format stream-json --verbose --include-partial-messages` run showed."""
    t: Dict[str, Any] = {"text_deltas": 0, "streamed_text": "", "tool_uses": [], "tool_results": [],
                         "result": None, "init": None, "events": 0}
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        t["events"] += 1
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init":
            t["init"] = ev
        elif kind == "stream_event":
            inner = ev.get("event") or {}
            delta = inner.get("delta") or {} if isinstance(inner, dict) else {}
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                t["text_deltas"] += 1
                t["streamed_text"] += str(delta.get("text", ""))
        elif kind in ("assistant", "user"):
            content = (ev.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    t["tool_uses"].append(str(block.get("name", "")))
                elif block.get("type") == "tool_result":
                    t["tool_results"].append(fakes.text_of(block.get("content")))
        elif kind == "result":
            t["result"] = ev
    return t


def context_windows(result: Optional[Dict[str, Any]]) -> List[int]:
    """contextWindow values Claude Code reports per model in its final result event, if any."""
    usage = (result or {}).get("modelUsage")
    out = []
    if isinstance(usage, dict):
        for v in usage.values():
            if isinstance(v, dict) and isinstance(v.get("contextWindow"), int):
                out.append(v["contextWindow"])
    return out


# How Claude Code sizes its context (Claude Code 2.1.288; pulsar/launch.py explains how Pulsar sets it).
CLAUDE_WINDOW, CLAUDE_LONG_WINDOW = 200_000, 1_000_000
OUTPUT_RESERVE, COMPACT_BUFFER = 20_000, 13_000
PREFIX_ROOM = 30_000    # Claude Code's fixed prompt prefix through Pulsar (~26.5k tokens) plus one turn
_EFFECTIVE_WINDOW = re.compile(r"autocompact: tokens=\S+ level=\w+ effectiveWindow=(\d+)")


def window_target(window: int) -> int:
    """The window Claude Code should work with for a model of `window` tokens: that window, or the largest
    one Claude Code can represent below it (its 1M and 200k windows). Never more than the model has."""
    if window >= CLAUDE_LONG_WINDOW:
        return CLAUDE_LONG_WINDOW
    return CLAUDE_WINDOW if window >= CLAUDE_WINDOW else window


def compact_threshold(window: int, reserve: int = OUTPUT_RESERVE) -> int:
    """Where auto-compact belongs for `window`: Claude Code's own rule (window minus the answer reserve
    minus 13k), scaled down for windows too small for those fixed amounts, and never below PREFIX_ROOM:
    under Claude Code's fixed prompt prefix every turn would be a failed compaction."""
    return max(window - min(reserve, window // 5) - min(COMPACT_BUFFER, window // 8), PREFIX_ROOM)


def claude_context(result: Optional[Dict[str, Any]], debug: str, compact: Dict[str, Any]) -> Dict[str, int]:
    """The context Claude Code works with, from its own numbers: the window its result event reports, the
    auto-compact window its debug log states (`effectiveWindow`, already minus the answer reserve), and
    the overrides it was given. Its threshold rule: min(floor(W * pct / 100), W - 13k)."""
    usage = (result or {}).get("modelUsage")
    outs = [v["maxOutputTokens"] for v in (usage.values() if isinstance(usage, dict) else [])
            if isinstance(v, dict) and isinstance(v.get("maxOutputTokens"), int) and v["maxOutputTokens"] > 0]
    reserve = min(max(outs) if outs else OUTPUT_RESERVE, OUTPUT_RESERVE)
    reported = max(context_windows(result) or [0])
    logged = [int(n) for n in _EFFECTIVE_WINDOW.findall(debug or "")]
    base = max(logged) if logged else reported - reserve
    threshold = base - COMPACT_BUFFER
    try:
        pct = float(compact.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE") or 0)
    except (TypeError, ValueError):
        pct = 0.0
    if 0 < pct <= 100:
        threshold = min(int(base * pct / 100), threshold)
    try:
        blocking = int(compact.get("CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE") or 0)
    except (TypeError, ValueError):
        blocking = 0
    return {"reported": reported, "compact_window": base + reserve, "threshold": threshold,
            "blocking": blocking if blocking > 0 else reported - reserve - 3000, "reserve": reserve}


def effective_ok(window: int, ctx: Dict[str, int]) -> bool:
    """Claude Code never works with more than the model's window, and with as much of it as it can.

    Below 200k Claude Code reports 200k for a model it handles as a Claude model (it has no smaller window
    for one), so there the compaction threshold and the blocking limit carry the window."""
    target = window_target(window)
    if not window or ctx["reported"] not in (target, max(target, CLAUDE_WINDOW)):
        return False
    want = compact_threshold(target, ctx["reserve"])
    return want * 0.99 <= ctx["threshold"] <= want and ctx["threshold"] < ctx["blocking"] < target


def evaluate(case: str, rc: int, transcript: Dict[str, Any], upstream: Any, expected_ctx: int,
             stored_ctx: int, nonce: str, debug: str = "") -> Dict[str, Dict[str, Any]]:
    """One {"ok", "detail"} per check in CHECKS (advisory ones also carry "advisory": true)."""
    checks: Dict[str, Dict[str, Any]] = {}
    res = transcript.get("result") or {}
    answer = "COMPAT-OK " + nonce
    final = str(res.get("result", "")) if isinstance(res, dict) else ""

    def put(name: str, ok: bool, detail: str) -> None:
        checks[name] = {"ok": bool(ok), "detail": detail}
        if name in ADVISORY:
            checks[name]["advisory"] = True

    put("launch", rc == 0 and bool(res) and not res.get("is_error") and answer in final,
        "exit %d, result event %s, final answer %s" % (
            rc, "present" if res else "missing", "matches" if answer in final else repr(final[:120])))

    main = [e for e in upstream.log if e.get("action") in ("tool", "final")]
    put("streaming", transcript["text_deltas"] >= 3 and answer in transcript["streamed_text"]
        and bool(main) and all(e.get("stream") for e in main),
        "%d text deltas reached Claude Code; upstream turns streamed: %s" % (
            transcript["text_deltas"], ",".join(str(e.get("stream")) for e in main) or "none"))

    probe = upstream.probe or {}
    put("tool_roundtrip", upstream.tool_calls >= 1 and upstream.tool_results >= 1 and bool(probe)
        and "Bash" in transcript["tool_uses"]
        and any(fakes.PROBE_TAG in r for r in transcript["tool_results"]),
        "upstream asked for %d tool call(s), got %d tool result(s); Claude Code ran %s" % (
            upstream.tool_calls, upstream.tool_results, ",".join(transcript["tool_uses"]) or "no tool"))

    count = probe.get("count_tokens") or {}
    n = count.get("input_tokens")
    put("count_tokens", count.get("status") == 200 and isinstance(n, int) and n > 0,
        "POST /v1/messages/count_tokens with the session token: status %s, input_tokens %s" % (
            count.get("status"), n))

    told = str(probe.get("context", ""))
    put("context_window", bool(expected_ctx) and told == str(expected_ctx) and stored_ctx == expected_ctx,
        "model window %d, Pulsar stored %d, Claude Code told (CLAUDE_CODE_MAX_CONTEXT_TOKENS) %s" % (
            expected_ctx, stored_ctx, told or "nothing"))

    ctx = claude_context(res, debug, probe.get("compact") or {})
    put("context_effective", effective_ok(expected_ctx, ctx),
        "model window %d (best Claude Code can use: %d): Claude Code reports %s, auto-compacts at %d "
        "(window %d), refuses to send past %d; expected threshold %d" % (
            expected_ctx, window_target(expected_ctx), ctx["reported"] or "nothing", ctx["threshold"],
            ctx["compact_window"], ctx["blocking"], compact_threshold(window_target(expected_ctx), ctx["reserve"])))

    put("key_isolation", bool(probe) and probe.get("has_session_token") is True
        and probe.get("provider_key_visible") is False
        and bool(re.match(r"^http://127\.0\.0\.1:\d+$", str(probe.get("base_url", "")))),
        "Claude Code sees a loopback Core URL and a session token; provider key in its environment: %s" % (
            probe.get("provider_key_visible")))
    return checks


# ---------------------------------------------------------------------------
# one case
# ---------------------------------------------------------------------------

def case_ok(checks: Dict[str, Dict[str, Any]], strict: bool = False) -> bool:
    """Every required check passed (and the advisory ones too with strict)."""
    return bool(checks) and all(c.get("ok") is True for c in checks.values() if strict or not c.get("advisory"))


def run_case(case: str, pulsar: List[str], env: Dict[str, str], project: str, timeout: float,
             log_dir: str, strict: bool = False) -> Dict[str, Any]:
    if case == "pins":
        return run_pins(env.get("PULSAR_CLAUDE_BIN", ""))
    if case == "picker":
        return run_picker(pulsar, env, project, timeout, log_dir, strict)
    nonce = secrets.token_hex(6)
    probe_cmd = "%s %s probe" % (shlex.quote(sys.executable), shlex.quote(os.path.abspath(__file__)))
    window = OPENAI_CONTEXT if case == "openai" else case_window(case)
    if window:
        upstream = fakes.OpenAIUpstream(OPENAI_MODEL, nonce, probe_cmd, context=window)
        model = OPENAI_MODEL
    else:
        upstream = fakes.AnthropicUpstream(ANTHROPIC_MODEL, nonce, probe_cmd)
        model = ANTHROPIC_MODEL
    entry: Dict[str, Any] = {"case": case, "wire": upstream.dialect, "model": model}
    with upstream:
        steps = [("key", pulsar + ["key", "--stdin"], upstream.base_url + " " + fakes.FAKE_KEY),
                 ("model", pulsar + ["model", model], None)]
        for name, argv, stdin in steps:
            rc, out, err = run(argv, env, project, 120, stdin)
            if rc != 0:
                entry.update(ok=False, error="pulsarcode %s failed (%d): %s" % (name, rc, (err or out).strip()[-400:]))
                entry["checks"] = {c: {"ok": False, "detail": "setup failed"} for c in CHECKS}
                return entry
        state = pulsar_state(env)
        prof = state["profiles"].get(state["active"], {})
        expected = window or int(state.get("guess") or 0)
        prompt = "%s %s: run the Pulsar compatibility probe, then report what it printed." % (fakes.MARKER, nonce)
        debug_file = os.path.join(log_dir, case + ".debug.txt")     # its auto-compact decisions (context_effective)
        argv = pulsar + ["-p", prompt, "--output-format", "stream-json", "--verbose",
                         "--include-partial-messages", "--allowedTools", "Bash", "--max-turns", "4",
                         "--debug-file", debug_file]
        started = time.time()
        rc, out, err = run(argv, env, project, timeout)
        entry["seconds"] = round(time.time() - started, 1)
        with open(os.path.join(log_dir, case + ".stdout.jsonl"), "w", encoding="utf-8") as f:
            f.write(out)
        with open(os.path.join(log_dir, case + ".stderr.txt"), "w", encoding="utf-8") as f:
            f.write(err)
        transcript = parse_transcript(out)
        try:
            with open(debug_file, encoding="utf-8", errors="replace") as f:
                debug = f.read()
        except OSError:
            debug = ""
        checks = evaluate(case, rc, transcript, upstream, expected, int(prof.get("context") or 0), nonce, debug)
    entry["checks"] = checks
    entry["upstream_requests"] = len(upstream.log)
    entry["ok"] = case_ok(checks, strict)
    if not entry["ok"] and err.strip():
        entry["stderr_tail"] = err.strip()[-600:]
    return entry


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------

def summarize(claude_version: str, results: List[Dict[str, Any]], channel: str = "",
              strict: bool = False) -> Dict[str, Any]:
    from pulsar import __version__ as pulsar_version  # the checkout this file lives in
    ok = bool(results) and all(r.get("ok") is True for r in results)
    advisories = ["%s/%s: %s" % (r.get("case"), name, c.get("detail", ""))
                  for r in results for name, c in (r.get("checks") or {}).items()
                  if c.get("advisory") and not c.get("ok")]
    out: Dict[str, Any] = {"claude_version": claude_version, "results": results, "ok": ok,
                           "advisories": advisories, "strict": strict, "pulsar_version": pulsar_version,
                           "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if channel:
        out["claude_channel"] = channel
    return out


def write_summary(path: str, summary: Dict[str, Any]) -> None:
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".compat-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, sort_keys=False)
        f.write("\n")
    os.replace(tmp, path)


def report(summary: Dict[str, Any], stream: Any = None) -> None:
    stream = stream or sys.stdout
    stream.write("Claude Code %s through Pulsar Code %s\n" % (summary.get("claude_version") or "?",
                                                              summary.get("pulsar_version", "?")))
    for r in summary["results"]:
        stream.write("  %-10s %s\n" % (r["case"], "PASS" if r.get("ok") else "FAIL"))
        if r.get("error"):
            stream.write("    error: %s\n" % r["error"])
        for name, c in (r.get("checks") or {}).items():
            mark = "ok" if c["ok"] else ("--" if c.get("advisory") else "!!")
            stream.write("    [%s] %-17s %s%s\n" % (mark, name, c["detail"],
                                                     "  (advisory)" if c.get("advisory") and not c["ok"] else ""))
    stream.write("compat: %s\n" % ("PASS" if summary["ok"] else "FAIL"))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: List[str]) -> int:
    if argv[:1] == ["probe"]:
        return probe()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--claude", default=os.environ.get("PULSAR_CLAUDE_BIN") or shutil.which("claude") or "",
                    help="the Claude Code executable to test (default: $PULSAR_CLAUDE_BIN, else claude on PATH)")
    ap.add_argument("--out", default="compat.json", help="summary file (default: compat.json)")
    ap.add_argument("--cases", default=",".join(CASES),
                    help="comma list of: %s (openai-<window> serves any window)" % ", ".join(CASES))
    ap.add_argument("--timeout", type=float, default=240, help="seconds per Claude Code run (default 240)")
    ap.add_argument("--channel", default="", help="label recorded in the summary (stable, latest, ...)")
    ap.add_argument("--strict", action="store_true", help="advisory checks fail the run too")
    ap.add_argument("--keep", action="store_true", help="keep the scratch folder and print its path")
    ap.add_argument("--work", default="", help="parent folder for the scratch folder (default: the system temp)")
    ap.add_argument("--env", action="append", default=[], metavar="NAME=VALUE",
                    help="add one variable to the scrubbed environment (repeatable), e.g. PULSAR_PLUGIN=off")
    ns = ap.parse_args(argv)

    cases = [c.strip() for c in ns.cases.split(",") if c.strip()]
    bad = [c for c in cases if not known_case(c)]
    claude = os.path.abspath(ns.claude) if ns.claude and os.path.sep in ns.claude else shutil.which(ns.claude or "")
    extra: Dict[str, str] = {}
    for item in ns.env:
        name, sep, value = item.partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            sys.stderr.write("compat: --env takes NAME=VALUE\n")
            write_summary(ns.out, summarize("", [{"case": "setup", "ok": False,
                                                  "error": "bad --env %r (expected NAME=VALUE)" % item}], ns.channel))
            return 2
        extra[name] = value
    if bad or not cases:
        sys.stderr.write("compat: unknown case(s) %s; choose from %s\n" % (", ".join(bad), ", ".join(CASES)))
        write_summary(ns.out, summarize("", [{"case": "setup", "ok": False,
                                              "error": "unknown case(s): %s" % (", ".join(bad) or "(none given)")}],
                                        ns.channel))
        return 2
    if not claude or not os.path.isfile(claude):
        sys.stderr.write("compat: Claude Code not found; pass --claude PATH or put claude on PATH\n")
        write_summary(ns.out, summarize("", [{"case": "setup", "ok": False, "error": "claude not found"}],
                                        ns.channel))
        return 2

    if ns.work:
        os.makedirs(ns.work, exist_ok=True)
    work = tempfile.mkdtemp(prefix="pulsar-compat-", dir=ns.work or None)
    home, pulsar_home = os.path.join(work, "home"), os.path.join(work, "pulsarhome")
    project, logs = os.path.join(work, "project"), os.path.join(work, "logs")
    for d in (home, os.path.join(home, "tmp"), project, logs):
        os.makedirs(d, mode=0o700, exist_ok=True)
    with open(os.path.join(project, "README.md"), "w", encoding="utf-8") as f:
        f.write("# compat\n\nScratch project for the Pulsar Code compatibility check.\n")
    env = base_env(claude, home, pulsar_home, free_port())
    env.update(extra)
    pulsar = ["/bin/sh", os.path.join(REPO, "pulsarcode")]
    version = claude_version(claude, env)
    results: List[Dict[str, Any]] = []
    try:
        for case in cases:
            try:
                results.append(run_case(case, pulsar, env, project, ns.timeout, logs, ns.strict))
            except Exception as exc:  # a harness failure is a failed case, never a missing summary
                results.append({"case": case, "ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
    finally:
        stop_core(pulsar, env, pulsar_home)
        if ns.keep:
            sys.stderr.write("compat: scratch folder kept at %s\n" % work)
        else:
            shutil.rmtree(work, ignore_errors=True)
    summary = summarize(version, results, ns.channel, ns.strict)
    write_summary(ns.out, summary)
    report(summary)
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
