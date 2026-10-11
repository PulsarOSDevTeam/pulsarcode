# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Regressions for the security and stability audits of the launcher, installer and CLI."""

import io
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from pulsar import ROOT, cli, plugin, statusline
from pulsar import config as K
from pulsar import launch as L
from pulsar import notify
from pulsar import providers as P

REPO = Path(__file__).resolve().parent.parent
SESSION_VARS = ("PULSAR_SESSION", "CLAUDECODE", "PULSAR_SESSION_TOKEN")
# Child processes get the real HOME, never a made-up one (on macOS a made-up HOME can raise a Keychain dialog);
# what they write is pointed at test folders through PULSAR_HOME and the other variables each test sets.
REAL_HOME = os.path.expanduser("~")
# install.sh run with a temporary HOME, for the one check that needs PULSAR_HOME to be the HOME itself.
ci_only = pytest.mark.skipif(not os.environ.get("CI"), reason="gives install.sh a temporary HOME: CI runners only")
# A child process running the real CLI cannot be stubbed: when this suite itself runs under Claude Code, the
# human gate refuses update and key there before the behaviour under test. Everywhere else (CI) they run.
outside_claude_code = pytest.mark.skipif(L.claude_ancestor(), reason="the suite runs under Claude Code, where the "
                                         "CLI refuses this command before the part under test")


@pytest.fixture(autouse=True)
def hermetic(monkeypatch):
    """Never touch a real Core, the network or a real session from these tests. They run as a person's own
    terminal would: no session variables, and no Claude Code process above them (the suite itself may run
    under one); the tests of those checks say so themselves."""
    from pulsar.core import spawn
    monkeypatch.setattr(spawn, "ensure_core", lambda *a, **kw: pytest.fail("tests must not spawn Pulsar Core"))
    monkeypatch.setattr(spawn, "attach", lambda *a, **kw: None)
    monkeypatch.setattr(L, "stop_core", lambda force=False: False)
    monkeypatch.setattr(L, "claude_ancestor", lambda *a, **kw: False)
    monkeypatch.setattr(cli, "latest_release", lambda: "")
    monkeypatch.setattr(cli, "_https_get", lambda *a, **kw: pytest.fail("tests must not download anything"))
    for var in SESSION_VARS + ("PULSAR_ALLOW_HTTP", "PULSAR_REF"):
        monkeypatch.delenv(var, raising=False)


def _no_tty(monkeypatch, data=""):
    monkeypatch.setattr(sys, "stdin", io.StringIO(data))


def _shim_env(tmp_path, **extra):
    env = {"HOME": REAL_HOME, "PATH": "/usr/local/bin:/usr/bin:/bin", "PULSAR_HOME": str(tmp_path / "ph"),
           "PULSAR_PYTHON": sys.executable, "PULSAR_OFFLINE": "1", "TERM": "dumb", "LANG": "C.UTF-8"}
    env.update(extra)
    return env


# ---------------------------------------------------------------------------
# 1. update: no shell string, validated ref, same-ref installer, refused inside a session
# ---------------------------------------------------------------------------

def test_model_ids_cannot_smuggle_a_newline():
    assert not P.valid_model_id("model\n") and not P.valid_model_id("model\r") and P.valid_model_id("a/b:c@d")


def test_valid_ref():
    for ok in ("main", "v3.0.0", "v3.1.0-rc.1", "feature/x_y", "a" * 64, "0123456789abcdef0123456789abcdef01234567"):
        assert cli.valid_ref(ok), ok
    for bad in ("v9; touch X; #", "$(touch X)", "`id`", "a b", "-x", "../evil", "a/../../other/repo/main", "a//b",
                "a/", "x.lock", "", "a" * 65, "main\n", "v1|sh", "v1&&id", "'", '"'):
        assert not cli.valid_ref(bad), bad


def test_update_refuses_injection_in_argument_and_env(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli.subprocess, "call", lambda *a, **kw: calls.append((a, kw)) or 0)
    monkeypatch.setattr(cli, "_download_installer", lambda ref: calls.append(ref) or "/nonexistent")
    assert cli.main(["update", "v9; touch X; #"]) == 2
    assert "not a valid tag" in capsys.readouterr().err
    monkeypatch.setenv("PULSAR_REF", "$(touch X)")
    assert cli.main(["update"]) == 1                 # an exported PULSAR_REF is never read (no release here)
    assert "ignores it" in capsys.readouterr().err
    assert cli.main(["update", "$(touch X)"]) == 2
    assert calls == []


def test_update_runs_the_installer_of_that_ref_without_a_shell(monkeypatch, tmp_path):
    calls = []
    script = tmp_path / "inst.sh"
    script.write_text("echo hi\n")
    monkeypatch.setattr(cli, "_download_installer", lambda ref: calls.append(("download", ref)) or str(script))
    monkeypatch.setattr(cli.subprocess, "call",
                        lambda cmd, env=None, cwd=None: calls.append(("run", cmd, env["PULSAR_REF"],
                                                                      env["PULSAR_NO_LAUNCH"])) or 0)
    assert cli.main(["update", "v3.1.0"]) == 0
    assert calls[0] == ("download", "v3.1.0")
    cmd = calls[1][1]
    assert cmd[-1] == str(script) and os.path.isabs(cmd[0])          # never a bare name looked up on PATH
    if os.name != "nt":
        assert len(cmd) == 2 and os.path.basename(cmd[0]) == "bash"
    assert calls[1][2:] == ("v3.1.0", "1")
    assert not script.exists()                       # the temp installer is removed afterwards
    calls.clear()
    monkeypatch.setattr(cli, "latest_release", lambda: "v3.2.0")
    assert cli.main(["update"]) == 0 and calls[0] == ("download", "v3.2.0")


def test_installer_download_is_private_and_from_the_same_ref(monkeypatch):
    urls = []
    monkeypatch.setattr(cli, "_https_get", lambda url, *a, **kw: urls.append(url) or b"#!/bin/bash\necho ok\n")
    path = cli._download_installer("v3.1.0")
    try:
        assert urls == ["https://raw.githubusercontent.com/%s/v3.1.0/install.sh" % cli.REPO] or os.name == "nt"
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert Path(path).read_bytes().startswith(b"#!/bin/bash")
    finally:
        cli._drop_installer(path)
    assert not os.path.exists(os.path.dirname(path))


@outside_claude_code
def test_shim_update_injection_end_to_end(tmp_path):
    """A ref carrying a shell command, through the real shim: neither form runs the injected command.

    update never reads an exported PULSAR_REF; the second case also names a bad ref, so it stops before
    asking GitHub for the latest release."""
    mark = tmp_path / "PWNED"
    for args, extra in ((["update", "v9; touch %s; #" % mark], {}),
                        (["update", "$(touch %s)" % mark], {"PULSAR_REF": "$(touch %s)" % mark})):
        r = subprocess.run([str(REPO / "pulsarcode")] + args, env=_shim_env(tmp_path, **extra), cwd=str(tmp_path),
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        assert r.returncode == 2, r.stderr
        assert not mark.exists()


# ---------------------------------------------------------------------------
# a model inside Claude Code cannot change Pulsar's security posture
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("var", SESSION_VARS)
def test_sensitive_commands_refused_inside_a_session(monkeypatch, capsys, var, pulsar_home):
    st = K.State()
    st.upsert(K.Profile(id="groq", provider="groq", model="m"))
    K.save(st)
    K.set_key("groq", "gsk_keepme0123456789abcdefgh")
    monkeypatch.setenv(var, "1")
    monkeypatch.setattr(cli.subprocess, "call", lambda *a, **kw: pytest.fail("must not run"))
    for argv in (["update", "v3.1.0"], ["upgrade"], ["uninstall", "--yes"], ["logout"], ["logout", "groq"],
                 ["key", "--stdin"], ["key", "--from-env", "PATH"], ["key"], ["pilot", "on", "--yes"]):
        _no_tty(monkeypatch, "gsk_replaced0123456789abcdefgh")
        assert cli.main(argv) == 3, argv
        assert "Claude Code session" in capsys.readouterr().err
    assert K.get_key("groq") == "gsk_keepme0123456789abcdefgh" and pulsar_home.exists()


def test_a_human_on_a_real_terminal_can_confirm(monkeypatch, pulsar_home):
    st = K.State()
    st.upsert(K.Profile(id="groq", provider="groq", model="m"))
    K.save(st)
    K.set_key("groq", "gsk_keepme0123456789abcdefgh")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setattr(cli, "_stdio_is_tty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
    assert cli.main(["logout", "groq"]) == 3                      # anything but the phrase is a refusal
    monkeypatch.setattr("builtins.input", lambda prompt="": "I am human")
    assert cli.main(["logout", "groq"]) == 0 and K.get_key("groq") == ""


def test_a_command_claude_code_started_is_refused_without_the_markers(monkeypatch, capsys, pulsar_home):
    """A shell under Claude Code can unset CLAUDECODE, PULSAR_SESSION and PULSAR_SESSION_TOKEN and fake a
    terminal: it is still a Claude Code descendant, and the confirmation phrase is not even asked for."""
    st = K.State()
    st.upsert(K.Profile(id="groq", provider="groq", model="m"))
    K.save(st)
    K.set_key("groq", "gsk_keepme0123456789abcdefgh")
    monkeypatch.setattr(L, "claude_ancestor", lambda *a, **kw: True)
    monkeypatch.setattr(cli.subprocess, "call", lambda *a, **kw: pytest.fail("must not run"))
    for argv in (["update", "v3.1.0"], ["upgrade"], ["uninstall", "--yes"], ["logout", "groq"], ["key", "--stdin"],
                 ["key", "--from-env", "PATH"]):
        _no_tty(monkeypatch, "gsk_replaced0123456789abcdefgh")
        assert cli.main(argv) == 3, argv
        assert "started by Claude Code" in capsys.readouterr().err, argv
    monkeypatch.setattr(cli, "_stdio_is_tty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("must not ask for the phrase"))
    for env_var in ("", "CLAUDECODE"):
        if env_var:
            monkeypatch.setenv(env_var, "1")
        assert cli.main(["logout", "groq"]) == 3
    assert K.get_key("groq") == "gsk_keepme0123456789abcdefgh" and pulsar_home.exists()


def test_nested_launch_and_switch_from_a_claude_code_process_are_refused(monkeypatch, capsys, pulsar_home):
    from pulsar.core import state as ST
    seen = _stub_launch(monkeypatch)
    _no_tty(monkeypatch)
    monkeypatch.setattr(L, "claude_ancestor", lambda *a, **kw: True)
    assert cli.main(["-p", "run: touch /tmp/owned", "--dangerously-skip-permissions"]) == 3 and seen == {}
    assert "started by Claude Code" in capsys.readouterr().err
    assert cli.main(["-p", "summarize", "--output-format", "json"]) == 0         # flags that widen nothing
    live = [ST.Session(sid="s1", token_sha256="h", plan=[]), ST.Session(sid="s2", token_sha256="h", plan=[])]
    monkeypatch.setattr(ST, "live_sessions", lambda reap=False: live)
    monkeypatch.setattr(ST, "update_plan", lambda sid, plan: pytest.fail("no session may move"))
    for argv in (["switch", "--all", "qwen3"], ["switch", "--session", "s2", "qwen3"]):
        assert cli.main(argv) == 3, argv


def test_switch_and_status_still_work_inside_a_session(monkeypatch, pulsar_home):
    monkeypatch.setenv("CLAUDECODE", "1")
    assert cli.main(["status", "--brief"]) == 0
    assert cli.main(["switch", "--list"]) == 0


# ---------------------------------------------------------------------------
# 4. keys never travel in argv
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["key", "gsk_abcdefghijklmnopqrstuvwxyz"],
    ["key", "http://127.0.0.1:5301/v1 sk-SECRET-custom-1111"],
    ["key", "http://127.0.0.1:5301/v1", "sk-SECRET-custom-1111"],
    ["key", "OPENAI_API_KEY=sk-proj-SECRET0123456789abcdef"],
    ["key", "https://user:SECRETpw@llm.example.com/v1"],
    ["key", "ghp_SECRET0123456789abcdefghij0123456789"],
    ["login", "sk-or-v1-SECRET0123456789abcdef"],
])
def test_key_in_argv_is_refused(monkeypatch, capsys, argv, pulsar_home):
    _no_tty(monkeypatch)
    assert cli.main(argv) == 2
    err = capsys.readouterr().err
    assert "never put a key on the command line" in err and "--stdin" in err
    assert "SECRET" not in err and "abcdefghijklmnop" not in err
    assert K.load_keys() == {} and not K.load().profiles


def test_key_from_stdin_env_and_prompt(monkeypatch, capsys, pulsar_home):
    _no_tty(monkeypatch, "  gsk_abcdefghijklmnopqrstuvwxyz\n")
    assert cli.main(["key"]) == 0                                   # stdin not a TTY: read automatically
    assert K.get_key("groq") == "gsk_abcdefghijklmnopqrstuvwxyz"
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-fromenv0123456789abcdef")
    _no_tty(monkeypatch)
    assert cli.main(["key", "--from-env", "OPENROUTER_API_KEY"]) == 0
    assert K.get_key("openrouter") == "sk-or-v1-fromenv0123456789abcdef"
    assert cli.main(["key", "--from-env", "NOT_SET_ANYWHERE"]) == 2
    assert cli.main(["key", "--from-env", "bad name;id"]) == 2
    # a URL alone in argv, the key on stdin
    _no_tty(monkeypatch, "sk-mine-0123456789abcdef\n")
    assert cli.main(["key", "https://llm.example.com/v1"]) == 0
    assert K.get_key("custom-llm-example-com") == "sk-mine-0123456789abcdef"
    # a local server with its own API key (vLLM --api-key, LM Studio auth)
    _no_tty(monkeypatch, "local-secret-0123456789\n")
    assert cli.main(["key", "http://localhost:8000/v1"]) == 0 and K.get_key("vllm") == "local-secret-0123456789"
    # ambiguous shapes never probe vendors: the operator names the provider
    _no_tty(monkeypatch, "sk-" + "a" * 32 + "\n")
    assert cli.main(["key", "--stdin"]) == 2 and "--provider" in capsys.readouterr().err
    _no_tty(monkeypatch, "sk-" + "a" * 32 + "\n")
    assert cli.main(["key", "--stdin", "--provider", "deepseek"]) == 0 and K.get_key("deepseek") == "sk-" + "a" * 32
    # a URL alone on a terminal prompts (masked) for the key
    monkeypatch.setattr(sys, "stdin", type("T", (io.StringIO,), {"isatty": lambda self: True})())
    import getpass
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "sk-prompted-0123456789ab")
    assert cli.main(["key", "https://other.example.com/v1"]) == 0
    assert K.get_key("custom-other-example-com") == "sk-prompted-0123456789ab"
    assert "user:" not in json.dumps(K.load().to_json())


@outside_claude_code
def test_key_argv_end_to_end_never_reaches_the_process_list(tmp_path):
    secret = "sk-or-v1-ARGVSECRET0123456789abcdef"
    r = subprocess.run([str(REPO / "pulsarcode"), "key", secret], env=_shim_env(tmp_path), cwd=str(tmp_path),
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and secret not in r.stdout + r.stderr
    assert not (tmp_path / "ph" / "keys.json").exists()


def test_key_refuses_a_redirecting_or_plain_http_provider(monkeypatch, capsys, pulsar_home):
    from pulsar import screens as S
    monkeypatch.setattr(S, "probe", lambda link, timeout=12.0: (False, "redirect to http://evil/steal not followed: "
                                                                      "your key stays put", 307))
    _no_tty(monkeypatch, "sk-mine-0123456789abcdef")
    assert cli.main(["key", "https://llm.example.com/v1"]) == 1
    assert K.load_keys() == {}
    monkeypatch.setattr(S, "probe", lambda link, timeout=12.0: pytest.fail("must not contact the host"))
    _no_tty(monkeypatch, "sk-mine-0123456789abcdef")
    assert cli.main(["key", "http://203.0.113.9:8000/v1"]) == 2
    err = capsys.readouterr().err
    assert "plain http" in err and K.load_keys() == {}


def test_help_and_providers_never_suggest_a_key_in_argv(capsys):
    assert cli.main(["help"]) == 0 and cli.main(["providers"]) == 0
    out = capsys.readouterr().out
    assert "<key>" not in out and "--stdin" in out and "--from-env" in out


# ---------------------------------------------------------------------------
# 5. escape sequences from model ids never reach the terminal, env or settings
# ---------------------------------------------------------------------------

EVIL = "evil\x1b]52;c;Y3VybCBldmlsLnNoIHwgc2gK\x07"


def test_statusline_and_notify_strip_control_sequences():
    stats = {"plan": [{"model": EVIL, "provider": "p\x1b]0;title\x07", "profile": "x"}, {"model": "b", "profile": "y"}],
             "session": {"last_model": EVIL}, "flights": [{"tps": "\x1b[2J"}]}
    for depth in (0, 24):
        line = statusline.render({"model": {"display_name": "\x1b]0;x\x07"}}, stats, depth)
        assert "\x1b]" not in line and "\x07" not in line and "\x1b[2J" not in line
    out = notify.session_start_output(stats, {})
    assert out["terminalSequence"].count("\x1b]") == 1 and out["terminalSequence"].count("\x07") == 1
    assert "\x1b]52" not in out["systemMessage"]
    assert "\x1b]52" not in out.get("hookSpecificOutput", {}).get("sessionTitle", "")
    assert "\x1b]52" not in notify._clip(EVIL)


def test_settings_and_picker_never_carry_control_sequences(pulsar_home):
    s = plugin.settings(EVIL, [{"model": EVIL, "provider": "x"}, {"model": "ok/model", "provider": "p\x1b[2J"}])
    dump = json.dumps(s)
    assert "\\u001b" not in dump and "\\u0007" not in dump
    assert [r["model"] for r in s["modelPicker"]["options"]] == ["ok/model"]


def test_config_drops_unprintable_model_ids(pulsar_home):
    K.ensure_home()
    K.config_path().write_text(json.dumps({"version": 2, "active": "groq", "profiles": {
        "groq": {"id": "groq", "provider": "groq", "model": EVIL, "context": 5}},
        "recent": [{"profile": "groq", "model": EVIL}, {"profile": "groq", "model": "ok"}]}))
    st = K.load()
    assert st.profiles["groq"].model == "" and st.recent == [{"profile": "groq", "model": "ok"}]


def test_model_and_switch_reject_unprintable_ids(monkeypatch, pulsar_home):
    st = K.State()
    st.upsert(K.Profile(id="groq", provider="groq", model="m"))
    K.save(st)
    assert cli.main(["model", EVIL]) == 2
    assert cli.main(["switch", "--", EVIL]) == 2
    assert cli.main(["switch", "--", "a b"]) == 2
    assert K.load().current().model == "m"


# ---------------------------------------------------------------------------
# 8. /pulsar:switch: no shell variable, this session only, --all to switch every session
# ---------------------------------------------------------------------------

def test_switch_template_uses_the_tool_not_a_shell():
    body = plugin.COMMANDS["switch.md"]
    assert "allowed-tools: mcp__plugin_pulsar_pulsar__pulsar_switch" in body
    assert "Bash" not in body and "$PULSAR_SESSION" not in body and "```bash" not in body


def test_switch_defaults_to_this_session(monkeypatch, pulsar_home, capsys):
    from pulsar.core import state as ST
    st = K.State()
    st.upsert(K.Profile(id="groq", provider="groq", model="m"))
    K.save(st)
    live = [ST.Session(sid=s, token_sha256="h", plan=[]) for s in ("s1", "s2", "s3")]
    updated = []
    monkeypatch.setattr(ST, "live_sessions", lambda reap=False: live)
    monkeypatch.setattr(ST, "update_plan", lambda sid, plan: updated.append(sid))
    monkeypatch.setenv("PULSAR_SESSION", "s2")
    assert cli.main(["switch", "--", "llama-3.3-70b-versatile"]) == 0
    assert updated == ["s2"]
    monkeypatch.delenv("PULSAR_SESSION")
    updated.clear()
    assert cli.main(["switch", "llama-3.3-70b-versatile"]) == 0
    assert updated == [] and "--all" in capsys.readouterr().out
    assert cli.main(["switch", "--all", "llama-3.3-70b-versatile"]) == 0
    assert updated == ["s1", "s2", "s3"]
    updated.clear()
    assert cli.main(["switch", "--session", "s3", "llama-3.3-70b-versatile"]) == 0 and updated == ["s3"]


# ---------------------------------------------------------------------------
# 9. uninstall only ever deletes a Pulsar home, and only what Pulsar put there
# ---------------------------------------------------------------------------

def test_uninstall_refuses_roots_home_and_unmarked_folders(monkeypatch, tmp_path, capsys):
    fake_home = tmp_path / "me"
    (fake_home / "projects").mkdir(parents=True)
    (fake_home / "config.json").write_text("{}")                   # not a Pulsar config
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))     # your home, as uninstall sees it
    for target in ("/", str(fake_home), str(fake_home / "projects")):
        monkeypatch.setenv("PULSAR_HOME", target)
        assert cli.main(["uninstall", "--yes"]) == 2, target
        assert "Nothing was deleted" in capsys.readouterr().err
    assert (fake_home / "projects").is_dir() and (fake_home / "config.json").exists()
    # even with a marker, your home directory itself is never a Pulsar home
    (fake_home / K.MARKER).write_text("x")
    monkeypatch.setenv("PULSAR_HOME", str(fake_home))
    assert cli.main(["uninstall", "--yes"]) == 2 and (fake_home / "projects").is_dir()


def test_uninstall_removes_only_known_children(monkeypatch, tmp_path, pulsar_home):
    st = K.State()
    st.upsert(K.Profile(id="groq", provider="groq", model="m"))
    K.save(st)
    assert (pulsar_home / K.MARKER).is_file()
    for d in ("app/pulsar", "venv/bin", "run", "plugin", "cache"):
        (pulsar_home / d).mkdir(parents=True, exist_ok=True)
    (pulsar_home / "my-notes.txt").write_text("keep me")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (pulsar_home / "app" / "pulsarcode").write_text("#!/bin/sh\n")
    os.symlink(str(pulsar_home / "app" / "pulsarcode"), str(bin_dir / "pulsarcode"))
    (bin_dir / "pulsarcode.cmd").write_text("someone else's pulsar tool\n")
    _bare_env(monkeypatch, tmp_path / "userhome")               # ~/.local/bin and startup files of the test, not yours
    monkeypatch.setenv("PULSAR_BIN_DIR", str(bin_dir))
    assert cli.main(["uninstall", "--yes"]) == 0
    assert sorted(p.name for p in pulsar_home.iterdir()) == [K.MARKER, "my-notes.txt"]
    assert not os.path.lexists(str(bin_dir / "pulsarcode")) and (bin_dir / "pulsarcode.cmd").exists()


# ---------------------------------------------------------------------------
# 10. plain http only to local or LAN hosts
# ---------------------------------------------------------------------------

def test_local_host_classification():
    for h in ("localhost", "127.0.0.1", "127.8.9.1", "::1", "[::1]", "10.1.2.3", "172.16.0.9", "172.31.255.1",
              "192.168.1.5", "169.254.1.1", "fe80::1", "fd12:3456::1", "nas.local", "0.0.0.0", "::ffff:192.168.1.2"):
        assert P.is_local_host(h), h
    for h in ("203.0.113.7", "8.8.8.8", "172.32.0.1", "100.64.0.1", "example.com", "evil.localdomain.com",
              "2001:4860::8888", ""):
        assert not P.is_local_host(h), h


def test_detection_of_http_endpoints():
    d = P.detect("http://203.0.113.5:11434")
    assert d.kind == "url" and d.best.id == "custom" and not d.best.local and "plain http" in d.warning
    d = P.detect("http://192.168.1.5:11434")
    assert d.kind == "local" and d.best.id == "ollama" and not d.warning
    assert P.detect("http://localhost:11434").best.id == "ollama"
    d = P.detect("http://10.0.0.8:8000/v1 sk-local-0123456789")
    assert d.best.local and not d.warning
    d = P.detect("https://bob:hunter2pw@llm.example.com/v1 sk-x-0123456789")
    assert "hunter2pw" not in d.base_url and d.warning and d.key == "sk-x-0123456789"
    assert P.detect("https://llm.example.com/v1\x1b]0;x\x07").kind == "unknown"
    assert P.plain_http_refusal("http://203.0.113.5/v1", "k") and not P.plain_http_refusal("http://203.0.113.5/v1", "")
    assert not P.plain_http_refusal("http://203.0.113.5/v1", "k", {"PULSAR_ALLOW_HTTP": "1"})
    assert not P.plain_http_refusal("https://203.0.113.5/v1", "k")


# ---------------------------------------------------------------------------
# 12. a non-UTF-8 stdout never crashes the CLI
# ---------------------------------------------------------------------------

def test_cp1252_and_ascii_stdout(monkeypatch, pulsar_home):
    monkeypatch.setattr(L, "find_claude", lambda: None)
    st = K.State()
    st.upsert(K.Profile(id="groq", provider="groq", model="m"))
    K.save(st)
    for enc in ("cp1252", "ascii"):
        out_buf, err_buf = io.BytesIO(), io.BytesIO()
        out = io.TextIOWrapper(out_buf, encoding=enc)
        err = io.TextIOWrapper(err_buf, encoding=enc)
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", err)
        for argv in (["status", "--brief"], ["status"], ["providers"], ["models"], ["help"], ["relay"]):
            assert cli.main(argv) == 0, (enc, argv)
        out.flush()
        text = out_buf.getvalue().decode(enc)
        assert "+ provider" in text


def test_status_with_cp1252_through_a_real_process(tmp_path):
    # The shim runs Python with -E (PYTHONIOENCODING is ignored there), so drive the same entry point
    # through the bootstrap argv with a real cp1252 stdout.
    argv = __import__("pulsar").python_argv("status", "--brief")
    r = subprocess.run(argv, env=_shim_env(tmp_path, PYTHONIOENCODING="cp1252"),
                       cwd=str(tmp_path), stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr.decode("cp1252", "replace")
    assert b"UnicodeEncodeError" not in r.stderr


# ---------------------------------------------------------------------------
# 6. installers: v3 self-test, never `python -m pulsar`, safe from a hostile working directory
# ---------------------------------------------------------------------------

def _self_test_script() -> str:
    text = (REPO / "install.sh").read_text(encoding="utf-8")
    m = re.search(r"self_test\(\) \{.*?<<'PY'\n(.*?)\nPY\n\}", text, re.S)
    assert m, "self_test heredoc not found"
    return m.group(1)


def test_installer_self_test_passes_on_this_tree(tmp_path):
    script = _self_test_script()
    assert "adapter" not in script
    r = subprocess.run([sys.executable, "-I", "-B", "-", str(REPO)], input=script, text=True, capture_output=True,
                       cwd=str(tmp_path), timeout=120)
    assert r.returncode == 0, r.stderr


def test_no_python_dash_m_pulsar_in_launch_paths():
    for name in ("install.sh", "install.ps1", "pulsarcode"):
        text = (REPO / name).read_text(encoding="utf-8")
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        assert not re.search(r"-m\s+pulsar\b", code), name
    boot = __import__("pulsar").BOOT
    assert 'BOOT="%s"' % boot in (REPO / "install.sh").read_text(encoding="utf-8")
    assert '$Boot = "%s"' % boot in (REPO / "install.ps1").read_text(encoding="utf-8")
    assert "uvicorn[standard]" not in (REPO / "requirements.txt").read_text(encoding="utf-8")
    sh = (REPO / "install.sh").read_text(encoding="utf-8")
    assert re.search(r'https://astral\.sh/uv/\$UV_VERSION/install\.sh', sh) and "UV_INSTALLER_SHA256" in sh


def test_shim_ignores_a_hostile_working_directory(tmp_path):
    evil = tmp_path / "evilrepo"
    mark = tmp_path / "pwn.txt"
    payload = "open(%r, 'a').write(__name__ + '\\n')\n" % str(mark)
    (evil / "pulsar").mkdir(parents=True)
    for rel in ("pulsar/__init__.py", "pulsar/__main__.py", "pulsar/cli.py", "typing.py", "runpy.py", "json.py",
                "fastapi.py", "sitecustomize.py"):
        (evil / rel).write_text(payload)
    r = subprocess.run([str(REPO / "pulsarcode"), "version"], env=_shim_env(tmp_path), cwd=str(evil),
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.startswith("pulsarcode "), r.stderr
    assert not mark.exists(), mark.read_text()
    # the hook / status line / MCP command lines the plugin writes are just as safe
    argv = __import__("pulsar").python_argv("statusline")
    r = subprocess.run(argv, cwd=str(evil), input="{}", capture_output=True, text=True, timeout=60,
                       env={"PATH": "/usr/bin:/bin", "HOME": REAL_HOME, "PULSAR_HOME": str(tmp_path / "ph")})
    assert r.returncode == 0 and not mark.exists()
    assert os.path.realpath(ROOT) == os.path.realpath(str(REPO))


# ===========================================================================
# Installers, PYTHONPATH, stdin keys, argv credentials, nested launches and uninstall
# ===========================================================================

import base64  # noqa: E402
import shutil  # noqa: E402
import types  # noqa: E402

needs_bash = pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="needs bash")


def _installer_env(tmp_path, **extra):
    """A scratch environment for install.sh in which curl and wget always fail (no network, ever).

    HOME is the real one: every folder the installer would write to (PULSAR_HOME, PULSAR_BIN_DIR, TMPDIR) is
    a test folder, it adds no PATH line (PULSAR_NO_PATH) and installs no Claude Code (PULSAR_SKIP_CLAUDE), and
    each run here stops before installing anything. tmp_path/home is the folder the runs start in."""
    stubs = tmp_path / "nonet"
    stubs.mkdir(exist_ok=True)
    for tool in ("curl", "wget"):
        f = stubs / tool
        f.write_text("#!/bin/sh\necho 'offline (test stub)' >&2\nexit 7\n")
        f.chmod(0o755)
    for d in ("home", "tmp"):
        (tmp_path / d).mkdir(exist_ok=True)
    env = {"HOME": REAL_HOME, "PATH": "%s:/usr/local/bin:/usr/bin:/bin" % stubs, "TERM": "dumb",
           "LANG": "C.UTF-8", "TMPDIR": str(tmp_path / "tmp"), "PULSAR_HOME": str(tmp_path / "ph"),
           "PULSAR_BIN_DIR": str(tmp_path / "bin"), "PULSAR_SKIP_CLAUDE": "1", "PULSAR_NO_PATH": "1",
           "PULSAR_NO_LAUNCH": "1"}
    env.update(extra)
    return env


def _plant_engine(folder, mark):
    """What another local user (or a cloned repo) can put next to install.sh: a fake engine."""
    (folder / "pulsar").mkdir(parents=True, exist_ok=True)
    (folder / "pulsar" / "__init__.py").write_text("open(%r, 'a').write('planted engine ran\\n')\n" % str(mark))
    (folder / "pulsar" / "__main__.py").write_text("print('pulsarcode 6.6.6')\n")
    (folder / "pulsarcode").write_text("#!/bin/sh\n")
    (folder / "requirements.txt").write_text("pip\n")


def _bash(args, env, cwd, stdin_text=None):
    r = subprocess.run(["bash"] + [str(a) for a in args], env=env, cwd=str(cwd), capture_output=True, text=True,
                       timeout=120, input=stdin_text if stdin_text is not None else "")
    return r.returncode, r.stdout + r.stderr


# --- A/B: install.sh never mistakes the folder it sits in (or the CWD) for the engine -----------------

def test_update_downloads_the_installer_alone_into_a_private_folder(monkeypatch):
    monkeypatch.setattr(cli, "_https_get", lambda url, *a, **kw: b"#!/bin/bash\necho ok\n")
    path = cli._download_installer("v3.1.0")
    folder = os.path.dirname(path)
    try:
        assert os.path.basename(folder).startswith(cli.UPDATE_DIR_PREFIX)
        assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700 and stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert os.listdir(folder) == [os.path.basename(path)]       # nothing can sit next to it
    finally:
        cli._drop_installer(path)
    assert not os.path.exists(folder)


def test_update_forbids_local_sources_and_cleans_up(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_https_get", lambda url, *a, **kw: b"#!/bin/bash\n")
    monkeypatch.setenv("PULSAR_SRC", "/tmp/somewhere")
    monkeypatch.setattr(cli.subprocess, "call", lambda cmd, env=None, cwd=None: seen.update(cmd=cmd, env=env) or 0)
    assert cli.main(["update", "v3.1.0"]) == 0
    assert seen["env"]["PULSAR_NO_LOCAL_SRC"] == "1" and "PULSAR_SRC" not in seen["env"]
    assert not os.path.exists(os.path.dirname(seen["cmd"][-1]))


def test_windows_update_runs_powershell_by_absolute_path(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_download_installer", lambda ref: "C:\\x\\install.ps1")
    monkeypatch.setattr(cli, "_drop_installer", lambda path: None)
    monkeypatch.setattr(cli.subprocess, "call", lambda cmd, env=None, cwd=None: seen.update(cmd=cmd) or 0)
    monkeypatch.setenv("SystemRoot", "D:\\Win")
    real_name = os.name
    monkeypatch.setattr(cli.os, "name", "nt")
    try:
        rc = cli.cmd_update(["v3.1.0"])
    finally:
        monkeypatch.setattr(cli.os, "name", real_name)
    assert rc == 0 and seen["cmd"][0] == "D:\\Win\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"
    for bad in ("", "Windows", "..\\evil", "\\\\share\\x"):
        monkeypatch.setenv("SystemRoot", bad)
        monkeypatch.delenv("windir", raising=False)
        assert cli.windows_powershell().startswith("C:\\Windows\\System32\\"), bad


@needs_bash
def test_installer_ignores_an_engine_planted_next_to_it(tmp_path):
    """An earlier `pulsarcode update` dropped install.sh straight into the shared temp dir."""
    mark = tmp_path / "PWNED"
    env = _installer_env(tmp_path)
    shared = tmp_path / "tmp"
    _plant_engine(shared, mark)
    shutil.copy(str(REPO / "install.sh"), str(shared / "pulsarcode-update-abc.sh"))
    rc, out = _bash([shared / "pulsarcode-update-abc.sh"], env, tmp_path / "home")
    assert rc != 0 and "could not download" in out and "local checkout" not in out, out
    assert not mark.exists() and not (tmp_path / "ph" / "app").exists()
    assert not [p for p in os.listdir(str(shared)) if p.startswith("pulsarcode-src.")]   # temp source cleaned


@needs_bash
def test_piped_installer_never_installs_the_current_folder(tmp_path):
    """`curl ... | bash` inside a repo holding pulsar/ and pulsarcode."""
    mark = tmp_path / "PWNED"
    env = _installer_env(tmp_path)
    repo = tmp_path / "repo"
    _plant_engine(repo, mark)
    script = (REPO / "install.sh").read_text(encoding="utf-8")
    rc, out = _bash([], env, repo, stdin_text=script)
    assert rc != 0 and "could not download" in out and "local checkout" not in out, out
    rc, out = _bash(["-s", "--", "--local"], env, repo, stdin_text=script)
    assert rc != 0 and "--local needs this script as a file" in out and "local checkout" not in out, out
    assert not mark.exists() and not (tmp_path / "ph" / "app").exists()


@needs_bash
def test_installer_local_source_only_on_request_from_your_own_folder(tmp_path):
    mark = tmp_path / "PWNED"
    src = tmp_path / "checkout"
    _plant_engine(src, mark)                       # the run stops before anything is imported
    shutil.copy(str(REPO / "install.sh"), str(src / "install.sh"))
    blocker = tmp_path / "home-is-a-file"
    blocker.write_text("x")                        # PULSAR_HOME that is a file: the run stops right after get_source
    env = _installer_env(tmp_path, PULSAR_HOME=str(blocker))
    rc, out = _bash([src / "install.sh", "--local"], env, tmp_path / "home")
    assert rc != 0 and "local checkout" in out and "is not a folder" in out, out
    rc, out = _bash([src / "install.sh"], env, tmp_path / "home")              # no --local: download, not local
    assert rc != 0 and "local checkout" not in out and "could not download" in out, out
    rc, out = _bash([src / "install.sh"], dict(env, PULSAR_SRC=str(src)), tmp_path / "home")
    assert "local checkout" in out, out
    # `pulsarcode update` sets PULSAR_NO_LOCAL_SRC: neither --local nor PULSAR_SRC can bring a folder in
    rc, out = _bash([src / "install.sh", "--local"], dict(env, PULSAR_NO_LOCAL_SRC="1", PULSAR_SRC=str(src)),
                    tmp_path / "home")
    assert "local checkout" not in out and "could not download" in out, out
    # a world-writable folder (a shared drop box) is never a source
    src.chmod(0o777)
    try:
        rc, out = _bash([src / "install.sh", "--local"], env, tmp_path / "home")
        assert rc != 0 and "refusing to install from" in out and "local checkout" not in out, out
    finally:
        src.chmod(0o755)
    assert not mark.exists()


@needs_bash
def test_installer_never_moves_into_a_foreign_home(tmp_path):
    mark = tmp_path / "PWNED"
    src = tmp_path / "checkout"
    _plant_engine(src, mark)
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "bin").mkdir()
    (proj / "config.json").write_text('{"name": "my project"}')
    proj.chmod(0o755)
    env = _installer_env(tmp_path, PULSAR_HOME=str(proj), PULSAR_SRC=str(src))
    rc, out = _bash([REPO / "install.sh"], env, tmp_path / "home")
    assert rc != 0 and "already holds files Pulsar Code did not create" in out, out
    assert sorted(os.listdir(str(proj))) == ["bin", "config.json", "src"]
    assert stat.S_IMODE(proj.stat().st_mode) == 0o755


@needs_bash
@ci_only
def test_installer_never_moves_into_your_home_directory(tmp_path):
    """PULSAR_HOME naming the HOME itself: on a CI runner, with a temporary HOME (never a person's real one)."""
    mark = tmp_path / "PWNED"
    src = tmp_path / "checkout"
    _plant_engine(src, mark)
    home = tmp_path / "home"
    env = _installer_env(tmp_path, HOME=str(home), PULSAR_HOME=str(home), PULSAR_SRC=str(src))
    rc, out = _bash([REPO / "install.sh"], env, home)
    assert rc != 0 and "is your home directory" in out, out
    assert not mark.exists()


# --- I: PYTHONPATH naming the current folder (empty element) cannot inject code --------------------

def _hostile_folder(tmp_path):
    evil = tmp_path / "evil"
    mark = tmp_path / "pwn.txt"
    (evil / "pulsar").mkdir(parents=True)
    payload = "open(%r, 'a').write(__name__ + ' ran\\n')\nraise ImportError('hostile')\n" % str(mark)
    for rel in ("pulsar/__init__.py", "sitecustomize.py", "usercustomize.py", "functools.py", "runpy.py", "typing.py",
                "json.py", "re.py"):
        (evil / rel).write_text(payload)
    return evil, mark


def test_shim_and_hooks_ignore_a_pythonpath_naming_the_working_directory(tmp_path):
    evil, mark = _hostile_folder(tmp_path)
    env = _shim_env(tmp_path, PYTHONPATH=":/nonexistent")       # `export PYTHONPATH=$PYTHONPATH:/x` when unset
    if os.name != "nt":
        r = subprocess.run([str(REPO / "pulsarcode"), "version"], env=env, cwd=str(evil), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0 and r.stdout.startswith("pulsarcode "), r.stderr
        hook = plugin._py_cmd("statusline")
        r = subprocess.run(["bash", "-c", hook], env=env, cwd=str(evil), input="{}", capture_output=True, text=True,
                           timeout=60)
        assert r.returncode == 0, r.stderr
    argv = plugin.safe_argv("statusline")
    r = subprocess.run(argv, env=env, cwd=str(evil), input="{}", capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert not mark.exists(), mark.read_text()


def test_plugin_commands_and_mcp_server_start_with_dash_e(pulsar_home):
    root = plugin.build_plugin()
    mcp = json.loads((root / ".mcp.json").read_text())["mcpServers"]["pulsar"]
    head = mcp["args"][:mcp["args"].index("-c")]
    assert "-E" in head or "-I" in head
    for event in plugin._hooks().values():
        for h in event:
            assert " -E " in h["hooks"][0]["command"] or " -I " in h["hooks"][0]["command"]
    assert " -E " in plugin.settings()["statusLine"]["command"]
    shim = (REPO / "pulsarcode").read_text(encoding="utf-8")
    assert 'exec "$py" -E -B -c "$boot" "$app" pulsar "$@"' in shim
    sh = (REPO / "install.sh").read_text(encoding="utf-8")
    assert '"$VENV/bin/python" -E -B -c "$BOOT" "$APP" pulsar version' in sh
    ps = (REPO / "install.ps1").read_text(encoding="utf-8")
    starts = [line for line in ps.splitlines() if "-B -c" in line and not line.lstrip().startswith("#")]
    assert len(starts) >= 2 and all("-E -X utf8 -B -c" in line for line in starts), starts
    assert "PYTHONUTF8" not in "\n".join(
        line for line in ps.splitlines() if not line.lstrip().startswith("#"))


# --- M: key --stdin / --from-env never turn a whole .env file into "the key" -----------------------

DOTENV = ("OPENAI_API_KEY=sk-proj-REALKEY0123456789abcdefghij\nAWS_SECRET_ACCESS_KEY=AWSSECRETxyz0123456789\n"
          "DATABASE_URL=postgres://app:DBPASSWORD@db/prod\n")


def test_dotenv_on_stdin_is_refused_and_nothing_is_saved(monkeypatch, capsys, pulsar_home):
    from pulsar import screens as S
    monkeypatch.setattr(S, "probe", lambda link, timeout=12.0: pytest.fail("nothing may be sent"))
    for data in (DOTENV, DOTENV.replace("\n", " "), "sk-proj-REALKEY0123456789abcdefghij AWSSECRETxyz0123456789",
                 "OPENAI_API_KEY=sk-proj-REALKEY0123456789abcdefghij AWS=AWSSECRETxyz0123456789"):
        _no_tty(monkeypatch, data)
        assert cli.main(["key", "--stdin"]) == 2, data
        err = capsys.readouterr().err
        assert "AWSSECRET" not in err and "DBPASSWORD" not in err and "REALKEY" not in err
    monkeypatch.setenv("MYVAR", DOTENV)
    _no_tty(monkeypatch)
    assert cli.main(["key", "--from-env", "MYVAR"]) == 2
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-REALKEY0123456789abcdefghij EXTRA")
    assert cli.main(["key", "--from-env", "OPENAI_API_KEY"]) == 2
    assert K.load_keys() == {} and not K.load().profiles
    d = P.detect(" ".join(DOTENV.split()))
    assert d.kind == "unknown" and not d.key and not d.candidates
    assert P.detect("OPENAI_API_KEY=sk-proj-x0123456789abcdef OTHER=1").kind == "unknown"


def test_single_line_stdin_forms_still_work(monkeypatch, pulsar_home):
    from pulsar import screens as S
    monkeypatch.setattr(S, "probe", lambda link, timeout=12.0: (True, "1 models online", 200))
    for data, pid in (("export GROQ_API_KEY=gsk_abcdefghijklmnopqrstuvwxyz\n", "groq"),
                      ("\n  Bearer sk-or-v1-0123456789abcdef0123456789  \n\n", "openrouter"),
                      ("https://llm.example.com/v1 sk-mine-0123456789abcdef\n", "custom-llm-example-com")):
        _no_tty(monkeypatch, data)
        assert cli.main(["key", "--stdin"]) == 0, data
        assert K.get_key(pid), pid


# --- G: argv secrets in token-only userinfo, query strings; --from-env never echoes -------------------

@pytest.mark.parametrize("argv", [
    ["key", "https://sk-or-v1-SECRETzz0123456789@openrouter.ai/api/v1"],
    ["key", "https://myllm.example.com/v1?api_key=SECRETzz0123456789"],
    ["key", "https://myllm.example.com/v1#SECRETzz0123456789"],
    ["key", "--from-env=sk-or-v1-SECRETzz0123456789abcdef0123456"],
    ["key", "--from-env", "SECRETzz0123456789abcdef"],
    ["key", "--stdin", "--provider", "gsk_SECRETzz0123456789abcdef"],
])
def test_argv_credentials_in_urls_and_flag_values_are_refused_unechoed(monkeypatch, capsys, argv, pulsar_home):
    _no_tty(monkeypatch, "gsk_abcdefghijklmnopqrstuvwxyz")
    assert cli.main(argv) == 2
    captured = capsys.readouterr()
    assert "SECRET" not in captured.err + captured.out
    assert K.load_keys() == {} and not K.load().profiles


def test_from_env_with_a_bad_name_never_echoes_it(monkeypatch, capsys, pulsar_home):
    _no_tty(monkeypatch)
    assert cli.main(["key", "--from-env", "bad name;id"]) == 2
    err = capsys.readouterr().err
    assert "bad name" not in err and "variable name" in err


def test_saved_base_urls_never_keep_a_query_or_fragment(monkeypatch, pulsar_home):
    d = P.detect("https://myllm.example.com/v1?api_key=SECRET0123456789#frag sk-mine-0123456789abcdef")
    assert d.base_url == "https://myllm.example.com/v1" and "SECRET" not in d.base_url and d.warning
    d = P.detect("https://tok-SECRET0123456789@myllm.example.com/v1")
    assert "SECRET" not in d.base_url and d.warning
    assert cli._strip_userinfo("https://u:p@h.example/v1?k=1#x") == "https://h.example/v1"
    from pulsar import screens as S
    monkeypatch.setattr(S, "probe", lambda link, timeout=12.0: (True, "1 models online", 200))
    _no_tty(monkeypatch, "https://myllm.example.com/v1?api_key=SECRET0123456789 sk-mine-0123456789abcdef")
    assert cli.main(["key", "--stdin"]) == 0
    assert "SECRET" not in K.config_path().read_text()


# --- D: launching from inside a session; a key typed as the prompt --------------------------------

def _stub_launch(monkeypatch):
    from pulsar.core import spawn
    from pulsar.core import state as ST
    info = ST.CoreInfo(port=4747, pid=1, version="3", admin_token="pla_x")
    monkeypatch.setattr(spawn, "attach", lambda *a, **kw: info)     # the preload finds a "warm" stub Core
    seen = {}
    monkeypatch.setattr(L, "start_session",
                        lambda *a, **kw: (info, types.SimpleNamespace(sid="s-inner", plan=[]), "pls_inner"))
    monkeypatch.setattr(L, "find_claude", lambda: "/bin/true")
    monkeypatch.setattr(L, "claude_version", lambda b: "2.1.287")
    monkeypatch.setattr(L, "exec_claude", lambda binary, args, env, **k: seen.update(args=args, env=env) or 0)
    st = K.State()
    st.upsert(K.Profile(id="ollama", provider="ollama", model="qwen3"))
    K.save(st)
    return seen


FAKE_BODY = "0123456789abcdef" * 3        # built at runtime: no key-shaped literal in the source


@pytest.mark.parametrize("argv", [
    ["sk-or-v1-" + FAKE_BODY],
    ["--", "key", "gsk_abcdefghijklmnopqrstuvwxyz0123"],
    ["KEY", "sk-or-v1-" + FAKE_BODY[:32]],
    ["-p", "use this key sk-or-v1-%s please" % FAKE_BODY[:32]],
    ["-p", "OPENAI_API_KEY=sk-proj-0123456789abcdef0123"],
    ["--append-system-prompt=nvapi-" + FAKE_BODY[:32], "-p", "hi"],
    ["ghp_0123456789abcdefghij" + FAKE_BODY[:16]],
])
def test_a_key_is_never_forwarded_to_claude_code_as_a_prompt(monkeypatch, capsys, argv, pulsar_home):
    seen = _stub_launch(monkeypatch)
    _no_tty(monkeypatch)
    assert cli.main(argv) == 2
    err = capsys.readouterr().err
    assert "looks like an API key" in err and "Nothing was sent" in err
    assert "0123456789abcdef0123" not in err and seen == {}


def test_ordinary_prompts_still_launch(monkeypatch, pulsar_home):
    seen = _stub_launch(monkeypatch)
    _no_tty(monkeypatch)
    for argv in (["-p", "explain hf_hub_download and sk-learn"], ["-p", "hi"], ["--continue"], ["-p", "ollama"],
                 ["-p", "hf_hub_download"], ["-p", "sk-ant-api"], ["-p", "https://example.com/docs?a=1"]):
        seen.clear()
        assert cli.main(argv) == 0, argv
        assert seen["args"][-len(argv):] == argv


@pytest.mark.parametrize("flag", ["--dangerously-skip-permissions", "--permission-mode=bypassPermissions",
                                  "--allowedTools", "--allowed-tools", "--settings", "--mcp-config", "--plugin-dir",
                                  "--add-dir", "--continue", "-c", "--resume", "--some-future-flag"])
def test_nested_launch_with_widening_flags_needs_a_human(monkeypatch, capsys, flag, pulsar_home):
    seen = _stub_launch(monkeypatch)
    _no_tty(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("PULSAR_SESSION", "s-outer")
    assert cli.main(["-p", "run: touch /tmp/owned", flag, "Bash"]) == 3
    assert "nested Claude Code session" in capsys.readouterr().err and seen == {}


def test_nested_launch_with_safe_flags_keeps_claudes_own_guard(monkeypatch, pulsar_home):
    seen = _stub_launch(monkeypatch)
    _no_tty(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("PULSAR_SESSION", "s-outer")
    assert cli.main(["-p", "summarize", "--output-format", "json", "--max-turns", "2"]) == 0
    assert seen["env"]["CLAUDECODE"] == "1" and seen["env"]["PULSAR_SESSION"] == "s-inner"
    # a human on a real terminal can still confirm a wider nested launch
    seen.clear()
    monkeypatch.setattr(cli, "_stdio_is_tty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "I am human")
    assert cli.main(["--dangerously-skip-permissions", "-p", "x"]) == 0 and seen["args"][-3:] == [
        "--dangerously-skip-permissions", "-p", "x"]


# --- F: the same key copied under another name never reaches Claude Code ---------------------------

def test_build_env_drops_copies_of_provider_keys_by_value():
    from pulsar.core import state as ST
    k1 = "sk-or-v1-ENVONLY-0123456789abcdef0123456789abcdef"
    k2 = "gsk_STOREDGROQ0123456789abcdefABCDEF0123"
    base = {"OPENROUTER_API_KEY": k1, "MY_ROUTER": k1, "OPENROUTER_TOKEN": "Bearer " + k1,
            "ROUTER_B64": base64.b64encode(k1.encode()).decode(), "GROQ_COPY": k2, "PATH": "/usr/bin:/bin",
            "HOME": "/home/u", "GEMINI_API_KEY": "/home/u/gemini.key", "OPENAI_API_KEY": "short", "EDITOR": "vim"}
    sess = ST.Session(sid="s1", token_sha256="h", plan=[])
    env = L.build_env(P.get("groq"), k2, "llama-3.3-70b-versatile", 0, ST.CoreInfo(port=4123, pid=1, version="x",
                                                                                   admin_token="pla"),
                      sess, "pls_tok", base=base, route="core")
    dump = json.dumps(env)
    assert "ENVONLY" not in dump and "STOREDGROQ" not in dump and base["ROUTER_B64"][:20] not in dump
    assert env["PATH"] == "/usr/bin:/bin" and env["HOME"] == "/home/u" and env["EDITOR"] == "vim"


# --- C/J: a mistaken PULSAR_HOME is never adopted, chmodded, marked or emptied ---------------------

def test_ensure_home_refuses_a_foreign_folder(monkeypatch, tmp_path, capsys):
    proj = tmp_path / "proj"
    for d in ("bin", "venv", "src"):
        (proj / d).mkdir(parents=True)
    (proj / "config.json").write_text('{"name": "my project"}')
    (proj / "settings.json").write_text("precious")
    proj.chmod(0o755)
    monkeypatch.setenv("PULSAR_HOME", str(proj))
    with pytest.raises(K.HomeError):
        K.ensure_home()
    assert cli.main(["relay", "off"]) == 2
    assert "did not create" in capsys.readouterr().err
    assert cli.main(["uninstall", "--yes"]) == 2
    # a launch writes nothing there either (no isolated profile, no plugin, no settings.json overwrite)
    monkeypatch.setattr(L, "find_claude", lambda: "/bin/true")
    monkeypatch.setattr(L, "exec_claude", lambda *a, **k: pytest.fail("must not launch"))
    _no_tty(monkeypatch)
    assert cli.main(["-p", "hi"]) == 2
    from pulsar import catalog as C
    C.write_cache("groq", [C.Model("m", "groq")])                  # best effort: silently skipped
    with pytest.raises(K.HomeError):
        plugin.build_plugin()
    assert sorted(os.listdir(str(proj))) == ["bin", "config.json", "settings.json", "src", "venv"]
    assert (proj / "config.json").read_text() == '{"name": "my project"}' and stat.S_IMODE(proj.stat().st_mode) == 0o755
    assert (proj / "settings.json").read_text() == "precious"


def test_ensure_home_never_touches_your_home_even_through_a_symlink(monkeypatch, tmp_path):
    me = tmp_path / "me"
    (me / "projects").mkdir(parents=True)
    me.chmod(0o755)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: me))
    os.symlink(str(me), str(tmp_path / "link"))
    for target in (me, tmp_path / "link"):
        monkeypatch.setenv("PULSAR_HOME", str(target))
        with pytest.raises(K.HomeError):
            K.ensure_home()
    (me / K.MARKER).write_text(K.MARKER_TEXT + "created 2026-01-01T00:00:00Z\n")   # even with a marker
    with pytest.raises(K.HomeError):
        K.ensure_home()
    assert stat.S_IMODE(me.stat().st_mode) == 0o755 and not (me / "cache").exists()


def test_ensure_home_adopts_only_pulsars_own_folders(monkeypatch, tmp_path):
    cases = {
        "fresh": ({}, "created"),
        "empty": ({"": None}, "created"),
        # state an older build wrote: Pulsar's (its config proves it) but not made by this one
        "state": ({"cache/models-groq.json": "{}", "run/x": "", "config.json": '{"active": "", "profiles": {}}'},
                  "adopted"),
        "v1": ({"nim.key": "nvapi-x", "active_model": "nim-kimi", "notes.txt": "mine"}, "adopted"),
        "v2": ({"app/pulsar/__init__.py": "", "venv/pyvenv.cfg": "", "my-notes.txt": "keep"}, "adopted"),
    }
    for name, (files, kind) in cases.items():
        h = tmp_path / name
        for rel, text in files.items():
            if rel == "":
                h.mkdir()
                continue
            (h / rel).parent.mkdir(parents=True, exist_ok=True)
            (h / rel).write_text(text)
        monkeypatch.setenv("PULSAR_HOME", str(h))
        K.ensure_home()
        assert K.marker_kind(h) == kind, name
        assert stat.S_IMODE(h.stat().st_mode) == 0o700


def test_uninstall_distrusts_markers_without_provenance(monkeypatch, tmp_path, capsys):
    proj = tmp_path / "proj"
    for d in ("bin", "venv", "src"):
        (proj / d).mkdir(parents=True)
    (proj / "config.json").write_text('{"version": 2, "active": "", "profiles": {}}')   # what a bad build overwrote
    (proj / K.MARKER).write_text(K.MARKER_TEXT)                                           # marker with no provenance
    monkeypatch.setenv("PULSAR_HOME", str(proj))
    _bare_env(monkeypatch, tmp_path / "userhome")
    assert cli.main(["uninstall", "--yes"]) == 2
    assert "did not create" in capsys.readouterr().err and (proj / "bin").is_dir() and (proj / "src").is_dir()
    with pytest.raises(K.HomeError):
        K.ensure_home()                                  # nor is anything written there any more
    assert K.marker_kind(proj) == "legacy"
    # an adopted older home keeps the user's own files but loses Pulsar's
    old = tmp_path / "old"
    (old / "app" / "pulsar").mkdir(parents=True)
    (old / "app" / "pulsar" / "__init__.py").write_text("")
    (old / "venv").mkdir()
    (old / "notes.txt").write_text("mine")
    monkeypatch.setenv("PULSAR_HOME", str(old))
    K.ensure_home()
    assert K.marker_kind(old) == "adopted"
    assert cli.main(["uninstall", "--yes"]) == 0
    assert sorted(os.listdir(str(old))) == [K.MARKER, "notes.txt"]


# --- switch: from another terminal; never other sessions from inside one --------------------------

def test_switch_from_another_terminal_moves_the_only_running_session(monkeypatch, pulsar_home, capsys):
    from pulsar.core import state as ST
    st = K.State()
    st.upsert(K.Profile(id="groq", provider="groq", model="m"))
    K.save(st)
    live = [ST.Session(sid="solo", token_sha256="h", plan=[])]
    updated = []
    monkeypatch.setattr(ST, "live_sessions", lambda reap=False: live)
    monkeypatch.setattr(ST, "update_plan", lambda sid, plan: updated.append(sid))
    assert cli.main(["switch", "llama-3.3-70b-versatile"]) == 0
    assert updated == ["solo"] and "only running session" in capsys.readouterr().out
    # inside some other Claude Code session the lone session is not moved implicitly ...
    updated.clear()
    monkeypatch.setenv("CLAUDECODE", "1")
    _no_tty(monkeypatch)
    assert cli.main(["switch", "llama-3.3-70b-versatile"]) == 0 and updated == []
    # ... and --all / --session <other> need a human
    for argv in (["switch", "--all", "llama-3.3-70b-versatile"], ["switch", "--session", "solo", "x"]):
        assert cli.main(argv) == 3, argv
    monkeypatch.setenv("PULSAR_SESSION", "solo")
    assert cli.main(["switch", "--session", "solo", "llama-3.3-70b-versatile"]) == 0 and updated == ["solo"]


# --- H: model ids are what providers use, nothing a shell could reinterpret -----------------------

def test_model_id_charset():
    for ok in ("moonshotai/kimi-k2.6", "qwen3:8b", "hf.co/user/repo:Q4_K_M", "claude-sonnet-4-5@20250929",
               "us.anthropic.claude-sonnet-4-5-20250929-v1:0", "@cf/meta/llama-3.1-8b-instruct", "sonnet[1m]",
               "arn:aws:bedrock:us-east-1:123456789012:inference-profile/x", "meta-llama/llama-3.3-70b:free",
               "a" * 200, "gpt-4o+tools", "x=y,z"):
        assert P.valid_model_id(ok), ok
    for bad in ("x';touch${IFS}/tmp/PWN;'", "a b", "a;b", "a|b", "a&b", "$(id)", "`id`", "a'b", 'a"b', "a\\b",
                "~/x", "-rf", "a*", "a?", "#x", "a>b", "a" * 201, "", "evil\x1b]0;x\x07", "sonnet[1m]x", "x[]"):
        assert not P.valid_model_id(bad), bad
    from pulsar.mcp import server as mcp
    for bad in ("x';touch${IFS}/tmp/PWN;'", "a b", "$(id)"):
        assert not mcp._safe_id(bad)              # the switch tool applies the same rule


# --- managed installs: update/uninstall print the package manager's command -----------------------

@pytest.mark.parametrize("managed, want", [
    ("homebrew", ("Homebrew", "brew upgrade pulsarcode", "brew uninstall pulsarcode")),
    ("brew", ("Homebrew", "brew upgrade pulsarcode", "brew uninstall pulsarcode")),
    ("scoop", ("Scoop", "scoop update pulsarcode", "scoop uninstall pulsarcode")),
    ("Scoop", ("Scoop", "scoop update pulsarcode", "scoop uninstall pulsarcode")),
])
def test_install_channel_from_pulsar_managed(managed, want):
    assert cli.install_channel({"PULSAR_MANAGED": managed}, root=str(REPO)) == want


def test_install_channel_from_site_packages(tmp_path):
    sp = "lib/python3.12/site-packages"
    pipx = tmp_path / ".local/share/pipx/venvs/pulsarcode" / sp
    uv = tmp_path / ".local/share/uv/tools/pulsarcode" / sp
    plain = tmp_path / "venv" / sp
    custom_uv = tmp_path / "mytools/pulsarcode" / sp
    for d in (pipx, uv, plain, custom_uv):
        d.mkdir(parents=True)
    assert cli.install_channel({}, root=str(pipx))[1] == "pipx upgrade pulsarcode"
    assert cli.install_channel({}, root=str(uv))[1:] == ("uv tool upgrade pulsarcode", "uv tool uninstall pulsarcode")
    assert cli.install_channel({"UV_TOOL_DIR": str(tmp_path / "mytools")}, root=str(custom_uv))[0] == "uv"
    name, upgrade, remove = cli.install_channel({}, root=str(plain))
    assert name == "pip" and upgrade.endswith("-m pip install --upgrade pulsarcode") and sys.executable in upgrade
    assert cli.install_channel({}, root=str(tmp_path / "app")) == ("", "", "")         # the installer's app dir
    assert cli.install_channel({}, root=str(REPO)) == ("", "", "")                     # a checkout
    name, upgrade, remove = cli.install_channel({"PULSAR_MANAGED": "nix\x1b]0;x\x07"}, root=str(REPO))
    assert name.startswith("nix") and "\x1b" not in name and "\x07" not in name and upgrade == remove == ""


def test_update_of_a_managed_install_prints_the_command_and_installs_nothing(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "_download_installer", lambda ref: pytest.fail("must not download the installer"))
    monkeypatch.setattr(cli.subprocess, "call", lambda *a, **kw: pytest.fail("must not run an installer"))
    monkeypatch.setenv("PULSAR_MANAGED", "homebrew")
    assert cli.main(["update"]) == 1
    assert "brew upgrade pulsarcode" in capsys.readouterr().err
    monkeypatch.setenv("PULSAR_MANAGED", "scoop")
    assert cli.main(["upgrade", "v3.1.0"]) == 1 and "scoop update pulsarcode" in capsys.readouterr().err
    monkeypatch.delenv("PULSAR_MANAGED")
    pipx = tmp_path / "pipx/venvs/pulsarcode/lib/python3.12/site-packages"
    pipx.mkdir(parents=True)
    monkeypatch.setattr(cli, "ROOT", str(pipx))
    assert cli.main(["update"]) == 1 and "pipx upgrade pulsarcode" in capsys.readouterr().err


def test_uninstall_of_a_managed_install_keeps_the_command(monkeypatch, capsys, tmp_path, pulsar_home):
    _bare_env(monkeypatch, tmp_path / "userhome")
    monkeypatch.setenv("PULSAR_MANAGED", "homebrew")
    assert cli.main(["uninstall", "--yes"]) == 0                    # no data yet: only the hint
    assert "brew uninstall pulsarcode" in capsys.readouterr().out and not pulsar_home.exists()
    st = K.State()
    st.upsert(K.Profile(id="groq", provider="groq", model="m"))
    K.save(st)
    assert cli.main(["uninstall", "--yes"]) == 0
    seen = capsys.readouterr()
    assert "brew uninstall pulsarcode" in seen.out + seen.err and not pulsar_home.exists()


# --- uninstall removes a custom BIN_DIR shim and exactly the installer's PATH block ---------------

def _installer_home(tmp_path, pulsar_home):
    """What install.sh leaves: the app, a shim in a custom BIN_DIR, the PATH block in the shell rc."""
    st = K.State()
    st.upsert(K.Profile(id="groq", provider="groq", model="m"))
    K.save(st)
    (pulsar_home / "app").mkdir(exist_ok=True)
    (pulsar_home / "app" / "pulsarcode").write_text("#!/bin/sh\n")
    home = tmp_path / "userhome"
    home.mkdir()
    custom = tmp_path / "tools bin"                                  # PULSAR_BIN_DIR="/x/tools bin" at install time
    custom.mkdir()
    os.symlink(str(pulsar_home / "app" / "pulsarcode"), str(custom / "pulsarcode"))
    return home, custom


def test_uninstall_removes_custom_bin_dir_shim_and_its_rc_block(monkeypatch, tmp_path, pulsar_home):
    home, custom = _installer_home(tmp_path, pulsar_home)
    shared = home / ".local" / "bin"                                   # holds claude too: its PATH line must stay
    shared.mkdir(parents=True)
    (shared / "claude").write_text("#!/bin/sh\n")
    os.symlink(str(pulsar_home / "app" / "pulsarcode"), str(shared / "pulsarcode"))
    zshrc = home / ".zshrc"
    before = "# mine\nexport EDITOR=vim\n"
    zshrc.write_text(before + '\n# Pulsar Code\nexport PATH="%s:$PATH"\n' % custom + "alias ll='ls -l'\n")
    zshrc.chmod(0o640)
    dot = tmp_path / "dotfiles"
    dot.mkdir()
    (dot / "bashrc").write_text('set -o vi\n\n# Pulsar Code\nexport PATH="%s:$PATH"\n' % shared)
    os.symlink(str(dot / "bashrc"), str(home / ".bashrc"))
    fish = home / ".config" / "fish" / "config.fish"
    fish.parent.mkdir(parents=True)
    fish.write_text("set -x A 1\n\n# Pulsar Code\nfish_add_path %s\n# Pulsar Code\n" % custom)
    profile = home / ".profile"
    lookalike = '# Pulsar Code\nexport PATH="%s:$PATH:/extra"\n# Pulsar Code notes\nexport PATH="%s:$PATH"\n' % (
        custom, custom)
    profile.write_text(lookalike)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))    # your home, as uninstall sees it
    monkeypatch.delenv("PULSAR_BIN_DIR", raising=False)              # not set any more at uninstall time
    for name in ("PULSAR_MANAGED", "PULSARCODE_BIN_DIR", "ZDOTDIR", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)                      # no startup file outside the test's home
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert cli.main(["uninstall", "--yes"]) == 0
    assert not os.path.lexists(str(custom / "pulsarcode")) and not os.path.lexists(str(shared / "pulsarcode"))
    assert (shared / "claude").exists()
    assert zshrc.read_text() == before + "alias ll='ls -l'\n"
    assert stat.S_IMODE(zshrc.stat().st_mode) == 0o640
    assert fish.read_text() == "set -x A 1\n# Pulsar Code\n"
    assert profile.read_text() == lookalike                          # not the installer's exact block: untouched
    assert os.path.islink(str(home / ".bashrc"))                      # a symlinked rc stays a symlink
    assert "# Pulsar Code" in (dot / "bashrc").read_text()            # ~/.local/bin still holds claude


def test_rc_block_helpers_are_exact(tmp_path):
    rc = tmp_path / ".zshrc"
    rc.write_text('a\n\n# Pulsar Code\nexport PATH="/x/bin:$PATH"\nb\n\n# Pulsar Code\nexport PATH="/y/bin:$PATH"\n')
    assert cli.rc_blocks(str(tmp_path)) == [(rc, "/x/bin"), (rc, "/y/bin")]
    assert cli.remove_rc_block(rc, "/y/bin") and rc.read_text() == 'a\n\n# Pulsar Code\nexport PATH="/x/bin:$PATH"\nb\n'
    assert not cli.remove_rc_block(rc, "/z/bin")
    assert not cli.remove_rc_block(tmp_path / "missing", "/x/bin")
    rc.write_text('# Pulsar Code\r\nexport PATH="/x/bin:$PATH"\r\nkeep\r\n')
    assert cli.remove_rc_block(rc, "/x/bin") and rc.read_bytes() == b"keep\r\n"


def _bare_env(monkeypatch, home):
    """uninstall sees `home` as your home (its ~/.local/bin and startup files) and finds nothing on PATH. The
    HOME variable itself stays: nothing here runs a child process."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    for name in ("PULSAR_BIN_DIR", "PULSARCODE_BIN_DIR", "PULSAR_MANAGED", "ZDOTDIR", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")


def test_quoted_fish_block_keeps_path_line_when_folder_holds_claude(monkeypatch, tmp_path, pulsar_home):
    home, _ = _installer_home(tmp_path, pulsar_home)
    home = home.rename(tmp_path / "fish home")                       # install.sh quotes this path
    shared = home / ".local" / "bin"
    shared.mkdir(parents=True)
    (shared / "claude").write_text("#!/bin/sh\n")
    os.symlink(str(pulsar_home / "app" / "pulsarcode"), str(shared / "pulsarcode"))
    fish = home / ".config" / "fish" / "config.fish"
    fish.parent.mkdir(parents=True)
    block = "\n# Pulsar Code\nfish_add_path '%s'\n" % shared
    fish.write_text("set -x A 1\n" + block)
    assert cli.rc_blocks(str(home)) == [(fish, str(shared))]           # unquoted
    _bare_env(monkeypatch, home)
    assert cli.main(["uninstall", "--yes"]) == 0
    assert fish.read_text() == "set -x A 1\n" + block                  # claude still lives there
    assert (shared / "claude").exists() and not os.path.lexists(str(shared / "pulsarcode"))


def test_quoted_fish_block_custom_bin_removes_shim_and_line(monkeypatch, tmp_path, pulsar_home):
    home, _ = _installer_home(tmp_path, pulsar_home)
    custom = tmp_path / "fish2" / "my bin"
    custom.mkdir(parents=True)
    os.symlink(str(pulsar_home / "app" / "pulsarcode"), str(custom / "pulsarcode"))
    fish = home / ".config" / "fish" / "config.fish"
    fish.parent.mkdir(parents=True)
    fish.write_text("set -x A 1\n\n# Pulsar Code\nfish_add_path '%s'\n" % custom)
    _bare_env(monkeypatch, home)
    assert cli.main(["uninstall", "--yes"]) == 0
    assert not os.path.lexists(str(custom / "pulsarcode"))
    assert fish.read_text() == "set -x A 1\n"


def test_uninstall_finds_blocks_under_zdotdir_and_xdg_config_home(monkeypatch, tmp_path, pulsar_home):
    home, _ = _installer_home(tmp_path, pulsar_home)
    zbin, fbin = tmp_path / "zbin", tmp_path / "fbin"
    for d in (zbin, fbin):
        d.mkdir()
        os.symlink(str(pulsar_home / "app" / "pulsarcode"), str(d / "pulsarcode"))
    zd = tmp_path / "zd"
    zd.mkdir()
    zshrc = zd / ".zshrc"
    zshrc.write_text('bindkey -e\n\n# Pulsar Code\nexport PATH="%s:$PATH"\n' % zbin)
    xdg = tmp_path / "xdg"
    fish = xdg / "fish" / "config.fish"
    fish.parent.mkdir(parents=True)
    fish.write_text("# Pulsar Code\nfish_add_path %s\n" % fbin)
    _bare_env(monkeypatch, home)
    monkeypatch.setenv("ZDOTDIR", str(zd))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    assert cli.main(["uninstall", "--yes"]) == 0
    assert zshrc.read_text() == "bindkey -e\n" and fish.read_text() == ""
    assert not os.path.lexists(str(zbin / "pulsarcode")) and not os.path.lexists(str(fbin / "pulsarcode"))


def test_rc_blocks_read_bash_login(monkeypatch, tmp_path):
    """install.sh writes into ~/.bash_login when that is the login file bash reads first (no ~/.bash_profile)."""
    for var in ("ZDOTDIR", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    rc = tmp_path / ".bash_login"
    rc.write_text('export A=1\n\n# Pulsar Code\nexport PATH="/x/bin:$PATH"\n')
    assert cli.rc_blocks(str(tmp_path)) == [(rc, "/x/bin")]
    assert cli.remove_rc_block(rc, "/x/bin") and rc.read_text() == "export A=1\n"


def test_rc_blocks_dedupes_zdotdir_equal_to_home(monkeypatch, tmp_path):
    rc = tmp_path / ".zshrc"
    rc.write_text('# Pulsar Code\nexport PATH="/x/bin:$PATH"\n')
    monkeypatch.setenv("ZDOTDIR", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert cli.rc_blocks(str(tmp_path)) == [(rc, "/x/bin")]


def test_uninstall_keeps_line_for_missing_or_relative_bin_dir(monkeypatch, tmp_path, pulsar_home):
    home, _ = _installer_home(tmp_path, pulsar_home)
    zshrc = home / ".zshrc"
    text = ('\n# Pulsar Code\nexport PATH="%s:$PATH"\n\n# Pulsar Code\nexport PATH="rel/bin:$PATH"\n'
            % (tmp_path / "gone"))
    zshrc.write_text(text)
    _bare_env(monkeypatch, home)
    assert cli.main(["uninstall", "--yes"]) == 0
    assert zshrc.read_text() == text


def test_rc_dir_unquotes_only_install_sh_fish_form():
    def d(line):
        return cli._rc_dir(cli._RC_LINE.match(line))
    assert d("fish_add_path '/a b/bin'") == "/a b/bin"
    assert d("fish_add_path /a/bin") == "/a/bin"
    assert d("fish_add_path -g '/a b/bin'") == d("fish_add_path -g /a b/bin") == "/a b/bin"     # the global form
    assert d("fish_add_path 'x'y'") == "'x'y'"                      # not a form install.sh writes: kept as is
    assert d('export PATH="/a b/bin:$PATH"') == "/a b/bin"


def test_remove_rc_block_leaves_read_only_file(tmp_path):
    rc = tmp_path / ".zshrc"                                          # read-only even when run as root
    text = '# Pulsar Code\nexport PATH="/x/bin:$PATH"\n'
    rc.write_text(text)
    rc.chmod(0o444)
    try:
        assert not cli.remove_rc_block(rc, "/x/bin")
        assert rc.read_text() == text
    finally:
        rc.chmod(0o644)


# --- uninstall: a launcher is ours only by its exact home line --------------------------------------

def test_our_shim_needs_the_exact_home_line(tmp_path):
    home = tmp_path / "p"                          # a short home: a substring of many other files
    home.mkdir()
    real = str(home.resolve())
    other = tmp_path / "other-launcher"
    other.write_text('#!/bin/sh\n# pulsarcode launcher of another install\nexec "%s/x/pulsar/bin/python" "$@"\n' % real)
    assert not cli._our_shim(str(other), home)
    ps1 = tmp_path / "pulsarcode.ps1"
    ps1.write_text("# Pulsar Code launcher for PowerShell (written by install.ps1)\r\n# Pulsar Code home: %s\r\n" % real)
    assert cli._our_shim(str(ps1), home)
    cmd = tmp_path / "pulsarcode.cmd"
    cmd.write_text('@echo off\r\nrem Pulsar Code home: "%s"\r\nsetlocal\r\n' % real.replace("%", "%%"))
    assert cli._our_shim(str(cmd), home)
    longer = tmp_path / "pulsarcode.sh"            # another home whose path starts with this one
    longer.write_text("#!/bin/sh\n# Pulsar Code home: %s-old\n" % real)
    assert not cli._our_shim(str(longer), home)


# --- uninstall: what v1 left, and the PATH entries of fish and Windows ------------------------------

def _quiet_uninstall(monkeypatch, tmp_path, shim_dirs=()):
    """uninstall reads only test folders: no real ~/.local/bin, startup file or PATH lookup (HOME untouched)."""
    user = tmp_path / "userhome"
    user.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user))
    for var in ("ZDOTDIR", "XDG_CONFIG_HOME", "PULSAR_MANAGED", "PULSAR_BIN_DIR", "PULSARCODE_BIN_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli, "_shim_dirs", lambda blocks: [str(d) for d in shim_dirs] + [b for _, b in blocks])
    return user


def test_rc_blocks_include_the_v1_installer_blocks(monkeypatch, tmp_path):
    for var in ("ZDOTDIR", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    zshrc = tmp_path / ".zshrc"
    zshrc.write_text('x\n\n# pulsarcode\nexport PATH="/v1/bin:$PATH"\n\n# Local Pulsar / pulsarcode\n'
                     'export PATH="/v0/bin:$PATH"\n')
    fish = tmp_path / ".config" / "fish" / "config.fish"
    fish.parent.mkdir(parents=True)
    fish.write_text('set -x A 1\n\n# pulsarcode\nset -gx PATH "/v1 f/bin" $PATH\n')
    assert cli.rc_blocks(str(tmp_path)) == [(zshrc, "/v1/bin"), (zshrc, "/v0/bin"), (fish, "/v1 f/bin")]
    assert cli.remove_rc_block(zshrc, "/v1/bin")
    assert zshrc.read_text() == 'x\n\n# Local Pulsar / pulsarcode\nexport PATH="/v0/bin:$PATH"\n'
    assert cli.remove_rc_block(fish, "/v1 f/bin") and fish.read_text() == "set -x A 1\n"
    zshrc.write_text('# pulsarcode\nexport PATH="/v1/bin:$PATH:/more"\n')          # not the installer's line
    assert cli.rc_blocks(str(tmp_path)) == []


def test_uninstall_removes_the_files_v1_kept_in_the_home(monkeypatch, tmp_path, pulsar_home, capsys):
    _quiet_uninstall(monkeypatch, tmp_path)
    pulsar_home.mkdir(parents=True)
    (pulsar_home / "nim.key").write_text("nvapi-legacy\n")
    for name in ("onboarding_complete", "nim_sonar_catalog.json", "active_model", "model_history"):
        (pulsar_home / name).write_text("x\n")
    (pulsar_home / "canonical").mkdir()
    K.load()                                                           # the v1 import adopts the home
    assert cli.main(["uninstall", "--yes"]) == 0
    assert not pulsar_home.exists(), sorted(os.listdir(str(pulsar_home)))
    seen = capsys.readouterr()
    assert "did not create" not in seen.out + seen.err


def test_uninstall_says_how_to_drop_a_fish_universal_path(monkeypatch, tmp_path, pulsar_home, capsys):
    st = K.State()
    st.upsert(K.Profile(id="groq", provider="groq", model="m"))
    K.save(st)
    (pulsar_home / "app").mkdir()
    (pulsar_home / "app" / "pulsarcode").write_text("#!/bin/sh\n")
    custom = tmp_path / "my bin"
    custom.mkdir()
    os.symlink(str(pulsar_home / "app" / "pulsarcode"), str(custom / "pulsarcode"))
    user = _quiet_uninstall(monkeypatch, tmp_path)
    fish = user / ".config" / "fish" / "config.fish"
    fish.parent.mkdir(parents=True)
    fish.write_text("# Pulsar Code\nfish_add_path '%s'\n" % custom)
    assert cli.main(["uninstall", "--yes"]) == 0
    assert fish.read_text() == "" and not os.path.lexists(str(custom / "pulsarcode"))
    seen = capsys.readouterr()
    assert "fish_user_paths" in seen.err and str(custom) in seen.err
    # the global form (fish_add_path -g) lives only in config.fish: removed, and nothing more to say
    K.save(st)
    (pulsar_home / "app").mkdir()
    (pulsar_home / "app" / "pulsarcode").write_text("#!/bin/sh\n")
    os.symlink(str(pulsar_home / "app" / "pulsarcode"), str(custom / "pulsarcode"))
    fish.write_text("# Pulsar Code\nfish_add_path -g '%s'\n" % custom)
    assert cli.main(["uninstall", "--yes"]) == 0
    assert fish.read_text() == "" and "fish_user_paths" not in capsys.readouterr().err


def test_windows_user_path_entry_is_removed_exactly():
    without = cli.user_path_without
    expand = lambda s: s.replace("%USERPROFILE%", "C:\\Users\\me")              # noqa: E731
    raw = "C:\\Tools;%USERPROFILE%\\.local\\bin\\;C:\\Users\\me\\.local\\bin;;D:\\x"
    assert without(raw, "C:\\Users\\Me\\.local\\bin", expand) == "C:\\Tools;;D:\\x"
    assert without(raw, "C:\\Users\\me\\.local", expand) is None             # a parent is another entry
    assert without("", "C:\\x", expand) is None

    class Reg:
        HKEY_CURRENT_USER, KEY_READ, KEY_WRITE, REG_SZ, REG_EXPAND_SZ = 1, 2, 4, 1, 2

        def __init__(self, value, kind):
            self.value, self.kind, self.closed = value, kind, 0

        def OpenKey(self, root, sub, reserved, access):
            assert (root, sub) == (1, "Environment") and access & 4
            return "key"

        def QueryValueEx(self, key, name):
            return self.value, self.kind

        def SetValueEx(self, key, name, reserved, kind, value):
            self.value, self.kind = value, kind

        def CloseKey(self, key):
            self.closed += 1
    reg = Reg("%USERPROFILE%\\bin;C:\\Pulsar Bin;C:\\Other", 2)
    assert cli.remove_user_path_entry("C:\\pulsar bin", reg=reg, broadcast=False)
    assert reg.value == "%USERPROFILE%\\bin;C:\\Other" and reg.kind == 2 and reg.closed == 1
    assert not cli.remove_user_path_entry("C:\\pulsar bin", reg=reg, broadcast=False)        # nothing left to remove


def test_windows_uninstall_drops_the_user_path_entry_of_an_emptied_folder(monkeypatch, tmp_path, pulsar_home):
    st = K.State()
    st.upsert(K.Profile(id="groq", provider="groq", model="m"))
    K.save(st)
    alone, shared = tmp_path / "alone", tmp_path / "shared"
    for d in (alone, shared):
        d.mkdir()
        (d / "pulsarcode.cmd").write_text('@echo off\r\nrem Pulsar Code home: "%s"\r\n' % pulsar_home.resolve())
    (shared / "claude.exe").write_text("")
    _quiet_uninstall(monkeypatch, tmp_path, shim_dirs=(alone, shared))
    dropped = []
    monkeypatch.setattr(cli, "_on_windows", lambda: True)
    monkeypatch.setattr(cli, "remove_user_path_entry", lambda d, **kw: dropped.append(d) or True)
    assert cli.main(["uninstall", "--yes"]) == 0
    assert not (alone / "pulsarcode.cmd").exists() and not (shared / "pulsarcode.cmd").exists()
    assert dropped == [str(alone)]                                       # claude.exe still needs the other one
