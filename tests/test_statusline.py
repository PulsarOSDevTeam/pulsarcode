# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""The in-session status line: colour depth, Claude Code's stdin data, sanitizing, a cheap Core query."""

import http.server
import json
import os
import re
import socket
import subprocess
import threading
import time

import pytest

from pulsar import plugin, python_argv, statusline

EVIL = "evil\x1b]52;c;Y3VybCBldmlsLnNoIHwgc2gK\x07\x1b[2J\u202e"
TOKEN = "pls_fake-session-token-not-a-key"
SGR_ONLY = re.compile(r"\x1b(?!\[[0-9;]*m)")       # any ESC that does not start a colour (SGR) sequence


def _clean(line: str) -> None:
    assert not SGR_ONLY.search(line), repr(line)
    assert "\x07" not in line and "\u202e" not in line and "\x9b" not in line


# ---------------------------------------------------------------------------
# colour depth
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("env, depth", [
    ({"COLORTERM": "truecolor"}, 24), ({"COLORTERM": "24bit"}, 24), ({"COLORTERM": "TrueColor"}, 24),
    ({}, 8), ({"COLORTERM": "yes"}, 8), ({"TERM": "xterm-256color"}, 8),
    ({"NO_COLOR": "1", "COLORTERM": "truecolor"}, 0), ({"NO_COLOR": ""}, 8),     # no-color.org: empty means unset
    ({"TERM": "dumb", "COLORTERM": "truecolor"}, 0),
    ({"PULSAR_COLOR": "24"}, 24), ({"PULSAR_COLOR": "256", "COLORTERM": "truecolor"}, 8), ({"PULSAR_COLOR": "8"}, 8),
    ({"PULSAR_COLOR": "0", "COLORTERM": "truecolor"}, 0),
])
def test_color_depth_follows_the_terminal(env, depth):
    assert statusline.color_depth(env) == depth


def test_render_uses_the_depth_it_is_given():
    stats = {"plan": [{"model": "m", "provider": "p", "profile": "x"}], "session": {}}
    assert "38;2;" in statusline.render({}, stats, 24) and "38;5;" not in statusline.render({}, stats, 24)
    assert "38;5;" in statusline.render({}, stats, 8) and "38;2;" not in statusline.render({}, stats, 8)
    assert "\x1b" not in statusline.render({}, stats, 0)
    # 256-colour codes stay inside the 6x6x6 cube
    for code in re.findall(r"38;5;(\d+)", statusline.render({}, stats, 8)):
        assert 16 <= int(code) <= 231


def test_main_honours_colorterm_and_no_color(monkeypatch, capsys):
    monkeypatch.delenv("PULSAR_CORE", raising=False)
    monkeypatch.delenv("PULSAR_COLOR", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr("sys.stdin", _Stdin("{}"))
    monkeypatch.setenv("COLORTERM", "truecolor")
    statusline.main([])
    assert "38;2;" in capsys.readouterr().out
    monkeypatch.delenv("COLORTERM")
    monkeypatch.setattr("sys.stdin", _Stdin("{}"))
    statusline.main([])
    out = capsys.readouterr().out
    assert "38;5;" in out and "38;2;" not in out
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr("sys.stdin", _Stdin("{}"))
    statusline.main([])
    assert "\x1b" not in capsys.readouterr().out


class _Stdin:
    def __init__(self, text):
        self.text = text

    def isatty(self):
        return False

    def read(self, n=-1):
        return self.text


# ---------------------------------------------------------------------------
# richer data from Claude Code's stdin JSON
# ---------------------------------------------------------------------------

def test_context_gauge_from_claude_codes_used_percentage():
    line = statusline.render({"context_window": {"used_percentage": 42.4}}, {"session": {}}, 0)
    assert "ctx" in line and "42%" in line and "▰" in line
    line = statusline.render({"context_window": {"used_percentage": 91}}, {"session": {}}, 24)
    assert "91%" in line and "38;2;255;70;90" in line           # red when nearly full
    ascii_line = statusline.render({"context_window": {"used_percentage": 50}}, None, 0, unicode=False)
    assert "[###---] 50%" in ascii_line
    for bad in ("\x1b[2J", None, [], {"x": 1}, float("nan"), "1e999", -5):
        line = statusline.render({"context_window": {"used_percentage": bad}}, None, 8)
        _clean(line)
        assert "ctx" not in line, bad
    assert "150%" not in statusline.render({"context_window": {"used_percentage": 150}}, None, 0)


def test_cost_falls_back_to_claude_codes_when_core_has_none():
    info = {"cost": {"total_cost_usd": 0.0123}}
    assert "~$0.0123" in statusline.render(info, {"session": {"cost": 0}}, 0)     # Claude Code's estimate
    assert "~$0.0123" in statusline.render(info, None, 0)
    line = statusline.render(info, {"session": {"cost": 0.5}}, 0)
    assert "$0.5000" in line and "~" not in line and "$0.0123" not in line      # Core's own number wins
    assert "$" not in statusline.render({"cost": {"total_cost_usd": "free"}}, {"session": {}}, 0)


def test_display_name_fallback_and_sanitizing():
    assert "My Model" in statusline.render({"model": {"display_name": "My Model", "id": "x"}}, None, 0)
    assert "raw-id" in statusline.render({"model": {"id": "raw-id"}}, None, 0)
    # Core's live model wins over Claude Code's label
    assert "core-model" in statusline.render({"model": {"display_name": "label"}},
                                             {"plan": [{"model": "core-model"}], "session": {}}, 0)
    stats = {"plan": [{"model": EVIL, "provider": EVIL, "profile": EVIL}, {"model": EVIL, "profile": "y"}],
             "session": {"last_model": EVIL, "last_provider": EVIL, "tokens_in": EVIL, "relays": EVIL,
                         "ttft_ewma": EVIL, "cost": EVIL},
             "flights": [{"tps": EVIL, "waiting": False}], "breakers": {EVIL: 1}}
    for depth in (0, 8, 24):
        _clean(statusline.render({"model": {"display_name": EVIL}, "cost": {"total_cost_usd": EVIL},
                                  "context_window": {"used_percentage": EVIL}}, stats, depth))
        _clean(statusline.render({"model": {"display_name": EVIL}}, None, depth))


@pytest.mark.parametrize("stats", [
    {"plan": "x", "session": [], "flights": {"a": 1}}, {"plan": [1, None, "a"], "session": {"tokens_in": "abc"}},
    {"flights": [{"waiting": True, "elapsed": "soon"}]}, {"breakers": [], "plan": [{}, {}]}, [], "text",
])
def test_render_never_raises_on_garbage(stats):
    _clean(statusline.render({"model": "x", "context_window": [], "cost": 3}, stats, 8))


# ---------------------------------------------------------------------------
# Core over one loopback socket
# ---------------------------------------------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    seen: list = []
    reply = (200, {"plan": [{"model": "fake/model", "provider": "fakeprov", "profile": "p"}],
                   "session": {"tokens_in": 1200, "tokens_out": 34}})
    chunked = False
    redirect = False

    def do_GET(self):
        type(self).seen.append((self.path, self.headers.get("Authorization"), self.headers.get("Host")))
        if self.redirect:
            self.send_response(302)
            self.send_header("Location", "http://example.invalid/")
            self.end_headers()
            return
        status, body = self.reply
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if self.chunked:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for part in (raw[:7], raw[7:]):
                self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
            self.wfile.write(b"0\r\n\r\n")
        else:
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    def log_message(self, *a):
        pass


@pytest.fixture
def fake_core():
    _Handler.seen = []
    _Handler.chunked = _Handler.redirect = False
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


def _env(port, **extra):
    env = {"PULSAR_CORE": "http://127.0.0.1:%d" % port, "ANTHROPIC_AUTH_TOKEN": TOKEN}
    env.update(extra)
    return env


def test_core_get_acts_as_the_session(fake_core):
    data = statusline.core_get("/pulsar/stats", _env(fake_core))
    assert data["plan"][0]["model"] == "fake/model"
    path, auth, host = _Handler.seen[-1]
    assert path == "/pulsar/stats" and auth == "Bearer " + TOKEN and host == "127.0.0.1:%d" % fake_core
    # the side-channel token (cloud and direct routes) is preferred
    statusline.core_get("/pulsar/stats", _env(fake_core, PULSAR_SESSION_TOKEN="pls_side-channel-token"))
    assert _Handler.seen[-1][1] == "Bearer pls_side-channel-token"
    _Handler.chunked = True
    assert statusline.core_get("/pulsar/stats", _env(fake_core))["session"]["tokens_out"] == 34
    _Handler.chunked, _Handler.redirect = False, True
    assert statusline.core_get("/pulsar/stats", _env(fake_core)) is None          # never follows a redirect


@pytest.mark.parametrize("env", [
    {"PULSAR_CORE": "http://example.com:80", "ANTHROPIC_AUTH_TOKEN": TOKEN},
    {"PULSAR_CORE": "https://127.0.0.1:4747", "ANTHROPIC_AUTH_TOKEN": TOKEN},
    {"PULSAR_CORE": "http://user:pw@127.0.0.1:4747", "ANTHROPIC_AUTH_TOKEN": TOKEN},
    {"PULSAR_CORE": "http://127.0.0.1:4747/x", "ANTHROPIC_AUTH_TOKEN": TOKEN},
    {"PULSAR_CORE": "http://127.0.0.1:99999", "ANTHROPIC_AUTH_TOKEN": TOKEN},
    {"PULSAR_CORE": "http://127.0.0.1:4747", "ANTHROPIC_AUTH_TOKEN": "sk-ant-fake-not-a-session"},
    {"PULSAR_CORE": "http://127.0.0.1:4747", "ANTHROPIC_AUTH_TOKEN": "pls_fake\r\nX-Evil: 1"},
    {"PULSAR_CORE": "http://127.0.0.1:4747"}, {},
])
def test_session_core_refuses_anything_but_a_loopback_core_and_a_session_token(env, monkeypatch):
    def no_connect(*a, **kw):
        raise AssertionError("must not connect")
    monkeypatch.setattr(socket, "create_connection", no_connect)
    assert statusline.session_core(env) is None
    assert statusline.core_get("/pulsar/stats", env) is None


def test_core_timeout_is_kept(monkeypatch):
    """A listener that never answers costs at most the 0.35 s budget, then the line says core offline."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    try:
        port = srv.getsockname()[1]
        assert statusline.CORE_TIMEOUT == 0.35
        t = time.monotonic()
        assert statusline.core_get("/pulsar/stats", _env(port)) is None
        assert time.monotonic() - t < 1.0
        for k, v in _env(port).items():
            monkeypatch.setenv(k, v)
        monkeypatch.setattr("sys.stdin", _Stdin('{"model": {"display_name": "Kimi"}}'))
        t = time.monotonic()
        import io
        out = io.StringIO()
        monkeypatch.setattr("sys.stdout", out)
        assert statusline.main([]) == 0
        assert time.monotonic() - t < 1.0
        assert "core offline" in out.getvalue() and "Kimi" in out.getvalue()
    finally:
        srv.close()


def _run_entry(env, stdin="{}", extra_flags=()):
    argv = python_argv(module="pulsar.statusline")
    argv[1:1] = list(extra_flags)
    base = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp"),
            "PULSAR_HOME": os.environ["PULSAR_HOME"], "LANG": "C.UTF-8"}
    base.update(env)
    return subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=60, env=base)


def test_entry_point_runs_without_importing_the_cli(fake_core):
    r = _run_entry(_env(fake_core, COLORTERM="truecolor"), '{"context_window": {"used_percentage": 12}}',
                   extra_flags=("-X", "importtime"))
    assert r.returncode == 0, r.stderr
    assert "fake/model" in r.stdout and "12%" in r.stdout and "38;2;" in r.stdout
    imported = {line.rsplit("|", 1)[-1].strip() for line in r.stderr.splitlines() if "import time:" in line}
    assert "pulsar.statusline" in imported or "pulsar" in imported
    for heavy in ("pulsar.cli", "pulsar.catalog", "pulsar.providers", "urllib.request", "http.client", "ssl"):
        assert heavy not in imported, heavy
    # garbage on stdin still exits 0 with a line
    r = _run_entry({"NO_COLOR": "1"}, "\x1b[2J{not json")
    assert r.returncode == 0 and "PulsarCode" in r.stdout


def test_plugin_runs_the_statusline_module_directly(pulsar_home):
    cmd = plugin.settings()["statusLine"]["command"]
    assert "pulsar.statusline" in cmd and " -E " in cmd
    assert plugin.settings()["statusLine"]["refreshInterval"] == 1


# ---------------------------------------------------------------------------
# Hostile chunked bodies and huge numbers
# ---------------------------------------------------------------------------

def _one_shot_server(reply: bytes):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)

    def serve():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.recv(65536)
                    conn.sendall(reply)
                except OSError:
                    pass
    threading.Thread(target=serve, daemon=True).start()
    return srv


@pytest.mark.parametrize("body", [b"-6\r\nxx", b"-4\r\n", b"-2\r\n", b"+2\r\n{}\r\n0\r\n\r\n", b"0x2\r\n{}\r\n0\r\n\r\n",
                                  b"1_0\r\n", b"ffffffff\r\n{}", b"2\r\n{}\r\n" * 20000])
def test_core_get_rejects_hostile_chunk_sizes_within_budget(body):
    srv = _one_shot_server(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + body)
    try:
        t = time.monotonic()
        assert statusline.core_get("/pulsar/stats", _env(srv.getsockname()[1])) is None
        assert time.monotonic() - t < 1.0
    finally:
        srv.close()


def test_dechunk_direct():
    for bad in (b"-6\r\nxx", b"-4\r\n", b"-2\r\n", b" -1 \r\n", b"5\r\nab"):
        assert statusline._dechunk(bad) is None
    assert statusline._dechunk(b"2;ext=1\r\n{}\r\n0\r\n\r\n") == b"{}"


@pytest.mark.parametrize("big", [1e308, -1e308, 1.7e308, "1e308", 10 ** 400])
def test_render_survives_huge_core_numbers(big):
    stats = {"session": {"ttft_ewma": big, "relays": big, "tokens_in": big, "cost": big},
             "flights": [{"waiting": True, "elapsed": big}]}
    line = statusline.render({"model": {"display_name": "Kimi"}, "context_window": {"used_percentage": 40}}, stats, 8)
    _clean(line)
    assert "Kimi" in line and "ctx" in line


# ---------------------------------------------------------------------------
# Provider display names (the same label the picker, WARP and pulsar_switch use)
# ---------------------------------------------------------------------------

def test_static_provider_names_match_the_registry():
    """The status line keeps its own id-to-name table so it never imports the registry on its hot path."""
    from pulsar import providers as P
    assert statusline.PROVIDER_NAMES == {p.id: p.name for p in P.PROVIDERS}
    from pulsar import config as K
    for p in P.PROVIDERS:
        prof = K.Profile(id=p.id, provider=p.id, model="fake/model-1")
        assert statusline.provider_label({"provider": p.id}) == prof.label == p.name


@pytest.mark.parametrize("prov, name", [("groq", "Groq"), ("xai", "xAI"), ("openrouter", "OpenRouter"),
                                        ("nvidia", "NVIDIA NIM"), ("anthropic", "Anthropic")])
def test_named_providers_show_their_display_name(prov, name):
    stats = {"plan": [{"model": "fake/model-1", "provider": prov, "profile": prov}], "session": {}}
    line = statusline.render({}, stats, 0)
    assert "fake/model-1 · " + name in line and "· " + prov + " " not in line + " "
    # the live provider after a switch or a relay, also when the plan no longer lists it
    for plan in ([{"model": "fake/other", "provider": "fakeprov", "profile": "x"},
                  {"model": "fake/model-1", "provider": prov, "profile": prov}],
                 [{"model": "fake/other", "provider": "fakeprov", "profile": "x"}]):
        live = {"plan": plan, "session": {"last_model": "fake/model-1", "last_provider": prov}}
        assert "fake/model-1 · " + name in statusline.render({}, live, 0)


def test_unknown_and_custom_providers_keep_their_labels():
    assert "m · fakeprov" in statusline.render({}, {"plan": [{"model": "m", "provider": "fakeprov"}]}, 0)
    custom = {"plan": [{"model": "m", "provider": "custom", "base_url": "https://fake.example.invalid:8443/v1"}]}
    assert "m · fake.example.invalid:8443" in statusline.render({}, custom, 0)
    assert statusline.provider_label({"provider": "custom"}) == "custom"
    assert statusline.provider_label({}) == "" and statusline.provider_label({"provider": None}) == ""
    _clean(statusline.provider_label({"provider": EVIL}))


# ---------------------------------------------------------------------------
# Window variables: anything but a plain ASCII number counts as unset
# ---------------------------------------------------------------------------

_FUZZ_ENVS = [
    {}, {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "999999999999"}, {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "-5"},
    {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "abc", "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "１２３"},
    {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1"},
    {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "0", "CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE": "99999999999999999999"},
    {"ANTHROPIC_BASE_URL": "\x1b]0;x\x07://"}, {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "²"},
]
_FUZZ_CTXS = [
    {"used_percentage": 50, "context_window_size": "NaN"}, {"used_percentage": 50, "context_window_size": 0},
    {"used_percentage": 50, "context_window_size": -1},
    {"used_percentage": 50, "context_window_size": 1e308, "current_usage": {"input_tokens": 1e308}},
    {"used_percentage": 50, "current_usage": "x"},
    {"used_percentage": 50, "current_usage": {"input_tokens": "nan", "cache_read_input_tokens": -9}},
    {"used_percentage": None},
    {"used_percentage": 50, "context_window_size": 200000, "current_usage": {"input_tokens": 10 ** 30}},
]


@pytest.mark.parametrize("env", _FUZZ_ENVS)
@pytest.mark.parametrize("ctx", _FUZZ_CTXS)
def test_render_survives_hostile_window_variables(env, ctx):
    """64 cases (8 environments x 8 context shapes): never raises, never lets an escape or a newline out."""
    line = statusline.render({"context_window": ctx, "cost": {"total_cost_usd": 1}}, None, depth=0, now=0,
                             unicode=True, environ=env)
    assert "\x1b" not in line and "\n" not in line and "PulsarCode" in line
    assert ("ctx" in line) == (ctx.get("used_percentage") is not None)


# Claude Code (2.1.296: parseInt, regexes with an ASCII-only \d) reads none of these as a window either.
@pytest.mark.parametrize("raw", ["²", "¹²⁸⁰⁰⁰", "１２８０００", "١٢٨٠٠٠", "-5", "0", " ", "", "abc", "0x1f"])
@pytest.mark.parametrize("var", ["CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
                                 "CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE"])
def test_malformed_window_variable_counts_as_unset(var, raw):
    assert statusline._env_int({var: raw}, var) == 0
    ctx = {"context_window": {"context_window_size": 200000, "used_percentage": 50,
                              "current_usage": {"input_tokens": 100000}}}
    assert "ctx ▰▰▰▱▱▱ 50%" in statusline.render(ctx, None, 0, environ={var: raw})


def test_plain_window_variables_still_count():
    assert statusline._env_int({"X": " 128000 "}, "X") == 128000
    assert statusline.real_window(200000, {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "128000"}) == 128000


def test_unicode_digit_window_keeps_the_full_line(monkeypatch, capsys):
    """CLAUDE_CODE_AUTO_COMPACT_WINDOW=² (a project's settings env can set it) used to collapse the line."""
    info = {"model": {"id": "z-ai/glm-5.3", "display_name": "◉ glm-5.3"}, "cost": {"total_cost_usd": 0.013},
            "context_window": {"context_window_size": 200000, "used_percentage": 1,
                               "current_usage": {"input_tokens": 1234, "output_tokens": 42}}}
    for var in ("PULSAR_CORE", "PULSAR_COLOR", "COLORTERM", "ANTHROPIC_BASE_URL", "PULSAR_ASCII"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    lines = {}
    for value in ("128000", "²", "１２８０００"):
        monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", value)
        monkeypatch.setattr("sys.stdin", _Stdin(json.dumps(info)))
        assert statusline.main([]) == 0
        lines[value] = capsys.readouterr().out
    for value, out in lines.items():
        assert "glm-5.3" in out and "ctx" in out and "core offline" in out and "~$0.0130" in out, (value, out)
    assert "1%" in lines["128000"] and "1%" in lines["²"]


# ---------------------------------------------------------------------------
# Two custom endpoints serving the same model id: the label names the one that answered
# ---------------------------------------------------------------------------

TWO_HOSTS = [{"profile": "custom-127-0-0-1-26883", "provider": "custom", "model": "qwen3-coder",
              "base_url": "http://127.0.0.1:26883/v1"},
             {"profile": "custom-127-0-0-1-26884", "provider": "custom", "model": "qwen3-coder",
              "base_url": "http://127.0.0.1:26884/v1"}]


def test_relay_between_same_model_hosts_names_the_profile_that_answered():
    """Core names the profile that answered (last_profile): the label follows it, breakers or not."""
    for answered, host in (("custom-127-0-0-1-26884", "127.0.0.1:26884"), ("custom-127-0-0-1-26883", "127.0.0.1:26883")):
        stats = {"plan": TWO_HOSTS, "breakers": {},
                 "session": {"last_provider": "custom", "last_model": "qwen3-coder", "last_profile": answered}}
        assert "qwen3-coder · " + host + " " in statusline.render({}, stats, 0, now=0)


def test_older_core_without_last_profile_prefers_the_target_whose_breaker_is_closed():
    """The e2e repro (rv2 twohost/unit.py): host A answered 429 (breaker open), the relay to host B answered."""
    stats = {"plan": TWO_HOSTS, "session": {"last_provider": "custom", "last_model": "qwen3-coder", "relays": 1},
             "breakers": {"custom-127-0-0-1-26883|qwen3-coder": {"open_for": 30}}}
    line = statusline.render({}, stats, 0, now=0)
    assert "qwen3-coder · 127.0.0.1:26884 " in line and "26883" not in line
    # no breaker open: the first matching target, as before
    stats["breakers"] = {}
    assert "qwen3-coder · 127.0.0.1:26883 " in statusline.render({}, stats, 0, now=0)


def test_a_last_profile_the_plan_does_not_list_falls_back_to_provider_and_model():
    stats = {"plan": TWO_HOSTS, "breakers": {},
             "session": {"last_provider": "custom", "last_model": "qwen3-coder", "last_profile": "gone"}}
    assert "qwen3-coder · 127.0.0.1:26883 " in statusline.render({}, stats, 0, now=0)
    stats["session"]["last_profile"] = EVIL
    _clean(statusline.render({}, stats, 24, now=0))
