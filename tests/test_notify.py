# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Hook pings: terminal notifications through Claude Code's terminalSequence, opt-in OS notifiers, haptics."""

import io
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time

import pytest

from pulsar import notify, python_argv

EVIL = "evil\x1b]52;c;Y3VybCBldmlsLnNoIHwgc2gK\x07\x1b\\\x9b2J;semi\u202e"
BEL, ESC = "\x07", "\x1b"


def pulsar_version() -> str:
    import pulsar
    return pulsar.__version__


def cc_allows(seq: str) -> bool:
    """Port of Claude Code 2.1.287's terminalSequence allowlist (E$n / T$n in its bundle).

    Only BEL and OSC 0/1/2/9/99/777 terminated by BEL or ST, at most 4096 bytes, no ESC inside a
    payload, and an OSC 9 body may not begin with a digit unless it is the 9;4 progress form.
    """
    if not seq or len(seq.encode("utf-8")) > 4096:
        return False
    i = 0
    while i < len(seq):
        if seq[i] == BEL:
            i += 1
            continue
        if seq[i] != ESC or seq[i + 1:i + 2] != "]":
            return False
        j, end, width = i + 2, -1, 0
        while j < len(seq):
            if seq[j] == BEL:
                end, width = j, 1
                break
            if seq[j] == ESC and seq[j + 1:j + 2] == "\\":
                end, width = j, 2
                break
            if seq[j] == ESC:
                return False
            j += 1
        if end < 0:
            return False
        body = seq[i + 2:end]
        ps, _, payload = body.partition(";")
        if not ps.isdigit() or int(ps) not in (0, 1, 2, 9, 99, 777):
            return False
        payload = "".join(ch for ch in payload if ord(ch) >= 32 and ord(ch) != 127 and not 128 <= ord(ch) <= 159)
        if int(ps) == 9 and not re.match(r"^4;[0-4](;(100|\d{1,2})?)?$", payload) and \
                re.match(r"^[\s\u180e\u200b]*[+-]?\d", payload):
            return False
        i = end + width
    return True


def test_the_allowlist_port_matches_what_claude_code_documents():
    assert cc_allows("\x07") and cc_allows("\x1b]9;hello\x07") and cc_allows("\x1b]777;notify;a;b\x1b\\")
    assert cc_allows("\x1b]9;4;1;50\x07")
    assert not cc_allows("\x1b]52;c;eA==\x07") and not cc_allows("\x1b[2J") and not cc_allows("\x1b]9;12 failed\x07")
    assert not cc_allows("x" * 5000) and not cc_allows("\x1b]9;a\x1bb\x07") and not cc_allows("")


# ---------------------------------------------------------------------------
# hook JSON: terminalSequence for Notification (attention) and Stop (done)
# ---------------------------------------------------------------------------

def test_attention_rings_and_notifies_through_the_terminal():
    out = notify.hook_output("attention", {"message": "Claude needs your permission to use Bash", "cwd": "/w/proj"},
                             "on", {})
    seq = out["terminalSequence"]
    assert seq.startswith(BEL) and "\x1b]9;PulsarCode · proj: Claude needs your permission to use Bash\x07" in seq
    assert cc_allows(seq)
    assert set(out) == {"terminalSequence"}                 # no systemMessage, no decision: never blocks the turn


def test_done_notifies_without_a_bell_unless_loud():
    seq = notify.hook_output("done", {}, "on", {})["terminalSequence"]
    assert BEL not in seq.replace("\x1b]9;", "").rstrip(BEL) and seq.startswith("\x1b]9;") and "Your turn" in seq
    assert cc_allows(seq)
    loud = notify.hook_output("done", {}, "loud", {})["terminalSequence"]
    assert loud.startswith(BEL) and cc_allows(loud)


@pytest.mark.parametrize("mode", ["off", "0", "none", "OFF".lower(), "false"])
def test_off_means_nothing(mode):
    assert notify.hook_output("attention", {"message": "x"}, mode, {}) == {}
    assert notify.plan("attention", {"message": "x"}, mode, {"TERMUX_VERSION": "1"}) == []


def test_bell_and_os_modes_skip_the_terminal_notification():
    assert notify.hook_output("attention", {}, "bell", {}) == {"terminalSequence": BEL}
    assert notify.hook_output("done", {}, "bell", {}) == {}
    assert notify.hook_output("attention", {}, "os", {}) == {"terminalSequence": BEL}
    assert notify.hook_output("done", {}, "os", {}) == {}
    assert notify.hook_output("session-end", {}, "on", {}) == {}


@pytest.mark.parametrize("env, style", [
    ({}, "9"), ({"TERM_PROGRAM": "iTerm.app"}, "9"), ({"TERM_PROGRAM": "WezTerm"}, "9"),
    ({"TERM": "xterm-ghostty"}, "9"), ({"WT_SESSION": "x"}, "9"),
    ({"TERM": "xterm-kitty"}, "99"), ({"KITTY_WINDOW_ID": "1"}, "99"),
    ({"TERM": "foot"}, "777"), ({"TERM": "rxvt-unicode-256color"}, "777"), ({"VTE_VERSION": "7600"}, "777"),
    ({"PULSAR_NOTIFY_OSC": "777", "TERM": "xterm-kitty"}, "777"), ({"PULSAR_NOTIFY_OSC": "52"}, "9"),
])
def test_the_escape_matches_the_terminal(env, style):
    assert notify.osc_style(env) == style
    seq = notify.osc_notification("Pulsar Code", "hello; world", env)
    assert seq.startswith("\x1b]%s;" % style) and cc_allows(seq)
    if style == "777":
        assert seq == "\x1b]777;notify;Pulsar Code;hello, world\x07"      # `;` separates the fields
    if style == "99":
        assert "o=unfocused" in seq                                     # kitty: only while unfocused


@pytest.mark.parametrize("env", [{}, {"TERM": "xterm-kitty"}, {"TERM": "foot"}])
def test_hook_payloads_cannot_smuggle_escapes(env):
    payload = {"message": EVIL, "cwd": "/tmp/" + EVIL}
    for event in ("attention", "done"):
        seq = notify.hook_output(event, payload, "on", env)["terminalSequence"]
        assert cc_allows(seq), repr(seq)
        assert "\x1b]52" not in seq and "\x9b" not in seq and "\u202e" not in seq
        assert seq.count("\x1b") == 1 + seq.count("\x1b\\")    # one escape (our OSC), its ST at most
    # a message starting with a digit never becomes an OSC 9 control command
    seq = notify.osc_notification("4;1;50", "x", {})
    assert cc_allows(seq) and seq.startswith("\x1b]9;PulsarCode: ")
    assert cc_allows(notify.osc_notification("Pulsar Code", "x" * 10000, {}))


# ---------------------------------------------------------------------------
# OS notifiers are opt-in; haptics are fire-and-forget
# ---------------------------------------------------------------------------

def test_os_notifiers_only_with_pulsar_notify_os(monkeypatch):
    monkeypatch.setattr(notify, "_which", lambda name, env=None: "/usr/bin/" + name)
    monkeypatch.setattr(sys, "platform", "linux")
    env = {"DISPLAY": ":0"}
    assert notify.plan("attention", {"message": "m"}, "on", env) == []
    cmds = notify.plan("attention", {"message": "m"}, "os", env)
    assert cmds and cmds[0][0] == "/usr/bin/notify-send" and cmds[0][-1] == "m"
    monkeypatch.setattr(sys, "platform", "darwin")
    cmds = notify.plan("done", {"cwd": "/w/café"}, "os", {})
    assert cmds[0][:2] == ["/usr/bin/osascript", "-e"] and "on run argv" in cmds[0]
    # the texts are arguments of the run handler, not AppleScript literals (which have no \u escapes)
    assert cmds[0][-2:] == ["PulsarCode · café", "Done. Your turn."]
    assert not any("\\u" in part for part in cmds[0])
    # the title comes first, so osascript stops reading options before a message that starts with "-"
    cmds = notify.plan("attention", {"message": '-e do shell script "id"', "cwd": "/w"}, "os", {})
    assert cmds[0][cmds[0].index("end run") + 1:] == ["PulsarCode · w", '-e do shell script "id"']


def test_termux_vibrates_by_default_and_notifies_only_on_request(monkeypatch):
    tb = "/data/data/com.termux/files/usr/bin/"
    monkeypatch.setattr(notify, "_which", lambda name, env=None: tb + name)
    env = {"TERMUX_VERSION": "0.118"}
    assert notify.plan("attention", {"message": "m"}, "on", env) == [[tb + "termux-vibrate", "-d", "70"]]
    assert notify.plan("done", {}, "on", env) == [[tb + "termux-vibrate", "-d", "25"]]
    cmds = notify.plan("attention", {"message": "m"}, "os", env)
    assert cmds[0][0] == tb + "termux-vibrate" and cmds[1][0] == tb + "termux-notification"
    assert notify.plan("attention", {}, "bell", env) == []


def test_helpers_never_resolve_through_relative_path_entries(tmp_path, monkeypatch):
    # A project folder with its own executable termux-vibrate; hooks run with cwd = project.
    proj = tmp_path / "proj"
    proj.mkdir()
    evil = proj / "termux-vibrate"
    evil.write_text("#!/bin/sh\ntouch PWNED\n")
    evil.chmod(0o755)
    good = tmp_path / "bin"
    good.mkdir()
    monkeypatch.chdir(proj)
    for path in ("", ":", ".", "." + os.pathsep + "rel", "bin"):
        env = {"TERMUX_VERSION": "0.118", "PATH": path}
        assert notify.plan("done", {}, "on", env) == [], path
    real = good / "termux-vibrate"
    real.write_text("#!/bin/sh\nexit 0\n")
    real.chmod(0o755)
    env = {"TERMUX_VERSION": "0.118", "PATH": os.pathsep.join(["", ".", str(good)])}
    assert notify.plan("done", {}, "on", env) == [[str(real), "-d", "25"]]


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(notify.__file__)))


def test_main_does_not_run_project_helper(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    evil = proj / "termux-vibrate"
    evil.write_text("#!/bin/sh\ntouch PWNED\n")
    evil.chmod(0o755)
    # the real HOME and an explicit PULSAR_HOME: a made-up HOME can make the system credential store ask the
    # person at the screen for a password
    env = {"TERMUX_VERSION": "0.118", "PATH": os.pathsep + "/usr/bin:/bin", "HOME": os.path.expanduser("~"),
           "PULSAR_HOME": str(tmp_path / "ph")}
    r = subprocess.run([sys.executable, "-E", "-B", "-c", "import sys; sys.path.insert(0, %r); "
                        "from pulsar import notify; sys.exit(notify.main(['done']))" % _ROOT],
                       input='{"cwd": "/x"}', capture_output=True, text=True, cwd=str(proj), env=env, timeout=20)
    assert r.returncode == 0
    time.sleep(0.3)
    assert not (proj / "PWNED").exists()


def test_spawn_detached_never_waits_and_holds_no_pipe(monkeypatch):
    seen = {}

    class Popen:
        def __init__(self, cmd, **kw):
            seen.update(cmd=cmd, **kw)

        def wait(self, *a, **kw):
            raise AssertionError("never waited for")
    monkeypatch.setattr(subprocess, "Popen", Popen)
    assert notify.spawn_detached(["termux-vibrate", "-d", "25"])
    assert seen["stdout"] == seen["stderr"] == seen["stdin"] == subprocess.DEVNULL
    assert seen.get("start_new_session") is True or os.name == "nt"
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: (_ for _ in ()).throw(OSError("missing")))
    assert notify.spawn_detached(["nope"]) is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell stub")
def test_a_hanging_termux_vibrate_does_not_block_the_stop_hook(tmp_path):
    """The audit's case: termux-vibrate hangs (Termux:API app missing); the hook still returns at once."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    pidfile = tmp_path / "vibrate.pid"
    stub = bindir / "termux-vibrate"
    stub.write_text("#!/bin/sh\necho $$ > %s\nexec sleep 30\n" % pidfile)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    env = {"PATH": "%s:/usr/bin:/bin" % bindir, "HOME": os.path.expanduser("~"), "PULSAR_HOME": str(tmp_path / "ph"),
           "TERMUX_VERSION": "0.118", "LANG": "C.UTF-8"}
    t = time.monotonic()
    r = subprocess.run(python_argv("done", module="pulsar.notify"), input="{}", capture_output=True, text=True,
                       env=env, timeout=60)
    took = time.monotonic() - t
    try:
        assert r.returncode == 0 and took < 5.0, (took, r.stderr)
        for _ in range(100):                      # the stub did start, detached
            if pidfile.exists() and pidfile.read_text().strip():
                break
            time.sleep(0.05)
        assert pidfile.exists()
        assert json.loads(r.stdout)["terminalSequence"].startswith("\x1b]9;")
    finally:
        if pidfile.exists() and pidfile.read_text().strip():
            try:
                os.kill(int(pidfile.read_text().strip()), signal.SIGKILL)
            except (OSError, ValueError):
                pass


# ---------------------------------------------------------------------------
# entry point and the SessionStart banner
# ---------------------------------------------------------------------------

def _main(monkeypatch, capsys, argv, stdin="{}", **env):
    for k in ("PULSAR_NOTIFY", "PULSAR_CORE", "NO_COLOR", "COLORTERM", "TERM", "TERM_PROGRAM", "KITTY_WINDOW_ID",
              "VTE_VERSION", "WT_SESSION", "TERMUX_VERSION", "PULSAR_NOTIFY_OSC"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    class In(io.StringIO):
        def isatty(self):
            return False
    monkeypatch.setattr(sys, "stdin", In(stdin))
    rc = notify.main(argv)
    return rc, capsys.readouterr().out


def test_main_always_exits_zero_and_prints_hook_json(monkeypatch, capsys):
    rc, out = _main(monkeypatch, capsys, ["attention"], json.dumps({"message": "Waiting for input"}))
    assert rc == 0 and json.loads(out)["terminalSequence"].startswith(BEL + "\x1b]9;")
    rc, out = _main(monkeypatch, capsys, ["attention"], "\x1b[2J{garbage", PULSAR_NOTIFY="off")
    assert rc == 0 and out == ""
    rc, out = _main(monkeypatch, capsys, ["done"], "[1, 2]")
    assert rc == 0 and "Your turn" in json.loads(out)["terminalSequence"]
    monkeypatch.setattr(notify, "hook_output", lambda *a, **kw: 1 / 0)
    rc, out = _main(monkeypatch, capsys, ["attention"])
    assert rc == 0


def test_entry_point_runs_without_importing_the_cli(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~"), "PULSAR_HOME": str(tmp_path / "ph"),
           "LANG": "C.UTF-8"}
    argv = python_argv("attention", module="pulsar.notify")
    argv[1:1] = ["-X", "importtime"]
    r = subprocess.run(argv, input='{"message": "hi"}', capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0 and json.loads(r.stdout)["terminalSequence"].startswith(BEL)
    imported = {line.rsplit("|", 1)[-1].strip() for line in r.stderr.splitlines() if "import time:" in line}
    assert "pulsar.cli" not in imported and "urllib.request" not in imported and "subprocess" not in imported


@pytest.mark.parametrize("env, want", [
    ({"COLORTERM": "truecolor"}, "38;2;"), ({}, "38;5;"), ({"TERM": "xterm-256color"}, "38;5;"),
    ({"NO_COLOR": "1"}, None), ({"NO_COLOR": ""}, "38;5;"),
])
def test_session_banner_honours_colour_depth(env, want):
    out = notify.session_start_output({"plan": [{"model": "a/b", "provider": "p"}, {"model": "c"}]}, env)
    msg = out["systemMessage"]
    if want is None:
        assert "\x1b" not in msg
    else:
        assert want in msg and ({"38;2;", "38;5;"} - {want}).pop() not in msg
    assert out["terminalSequence"] == "\x1b]2;◉ PulsarCode\x07" and cc_allows(out["terminalSequence"])


def test_session_banner_sets_the_persona_beside_the_name_the_model_and_the_commands():
    import pulsar
    plan = {"plan": [{"model": "a/b", "provider": "groq"}, {"model": "c/d", "provider": "groq"}]}
    lines = notify.session_start_output(plan, {"NO_COLOR": "1"}, {"source": "startup"})["systemMessage"].split("\n")
    # The first line follows Claude Code's own "SessionStart:startup says:" label, so the block starts on the
    # second line, where every row gets the same indent.
    assert lines[0] == "◉ uplink ready"
    assert [row[:17] for row in lines[1:4]] == list(notify.PERSONA)
    assert lines[1][20:] == "PulsarCode " + pulsar.__version__ and lines[2][20:] == "a/b via Groq"
    assert lines[3][20:] == "/pulsar:status  /pulsar:stage  /pulsar:switch"
    assert lines[4:] == ["relay c/d", notify.RESUME_HINT]
    assert max(len(row) for row in lines) + 5 <= 80          # Claude Code indents the message by 5
    # the same session going on (/clear, a compaction): one line, no persona
    for source in ("clear", "compact"):
        msg = notify.session_start_output(plan, {"NO_COLOR": "1"}, {"source": source})["systemMessage"]
        assert msg.startswith("◉ PulsarCode ") and notify.PERSONA[1] not in msg and msg.endswith("\n" + notify.RESUME_HINT)
    # a model line too long to sit beside the persona goes below it, whole
    long = "accounts/fireworks/models/" + "x" * 40
    lines = notify.session_start_output({"plan": [{"model": long, "provider": "groq"}]}, {"NO_COLOR": "1"})["systemMessage"].split("\n")
    assert lines[2][20:] == "via Groq" and lines[4] == long


def test_the_persona_is_three_even_rows_and_never_claude_codes_mascot():
    demo = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "demo")
    sys.path.insert(0, demo)
    try:
        import svgterm
    finally:
        sys.path.remove(demo)
    assert len(notify.PERSONA) == 3 and {len(row) for row in notify.PERSONA} == {17}
    assert svgterm.mascot_lines("\n".join(notify.PERSONA)) == []     # the recording checks never take it for the mascot
    plain = re.compile(r"\x1b\[[0-9;]*m")
    for depth in (24, 8):
        assert [plain.sub("", row) for row in notify.persona(depth)] == list(notify.PERSONA)   # colour moves no cell
    assert notify.persona(0) == list(notify.PERSONA)
    # the core and the model name are bold in the terminal's own colour, never the brand's near-white
    assert "236;246;255" not in "".join(notify.persona(24))
    assert "236;246;255" not in notify.session_start_output({"plan": [{"model": "a/b", "provider": "groq"}]},
                                                             {"COLORTERM": "truecolor"})["systemMessage"]


def test_session_banner_is_sanitized_and_survives_garbage():
    out = notify.session_start_output({"plan": [{"model": EVIL, "provider": EVIL}, {"model": EVIL}]}, {})
    assert cc_allows(out["terminalSequence"])
    assert not re.search(r"\x1b(?!\[[0-9;]*m)", out["systemMessage"]) and "\x07" not in out["systemMessage"]
    assert "sessionTitle" not in out.get("hookSpecificOutput", {})   # no session title (see the resume tests below)
    note = out["hookSpecificOutput"]["additionalContext"]        # the model is told which model it is, in a safe alphabet
    assert note.startswith("You are ") and "\x1b" not in note and "\n" not in note and len(note) < 700
    legacy = notify.session_start_output({}, {}, {"session_title": "◉ Pulsar Code", "session_id": "abcdef12-0000",
                                                  "cwd": "/w/" + EVIL})
    title = legacy["hookSpecificOutput"]["sessionTitle"]
    assert not _UNSAFE.search(title) and title.endswith(" · abcdef12")
    for stats in (None, [], {"plan": "x"}, {"plan": [None, 3]}):
        assert notify.session_start_output(stats, {"ANTHROPIC_MODEL": "m"})["systemMessage"]
    for payload in (None, [], "x", {"session_id": EVIL, "session_title": "◉ Pulsar Code"}, {"session_title": 3}):
        out = notify.session_start_output({}, {}, payload)
        assert out["systemMessage"] and "sessionTitle" not in out.get("hookSpecificOutput", {})


_UNSAFE = re.compile("[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


# ---------------------------------------------------------------------------
# resuming: Claude Code prints `claude --resume <title or id>` at /exit
# ---------------------------------------------------------------------------

SID = "c41d0c8c-0ed3-41de-a9e4-c95213699ac3"


@pytest.mark.parametrize("source", ["startup", "resume", "clear", "compact"])
def test_sessions_get_no_shared_title_so_the_exit_hint_names_the_session_id(source):
    """The e2e case: every session was titled "◉ Pulsar Code", so /exit printed `claude --resume "◉ Pulsar Code"`,
    a title that matched every Pulsar session. Without a session title Claude Code prints the session id."""
    payload = {"session_id": SID, "cwd": "/w/proj", "source": source, "hook_event_name": "SessionStart"}
    out = notify.session_start_output({"plan": [{"model": "fake/m", "provider": "groq"}]}, {}, payload)
    assert "sessionTitle" not in json.dumps(out)
    assert out["hookSpecificOutput"]["additionalContext"].startswith("You are fake/m (served by Groq)")
    # a Claude model keeps Claude Code's own introduction
    claude = notify.session_start_output({"plan": [{"model": "claude-sonnet-5-5", "provider": "anthropic"}]}, {}, payload)
    assert "hookSpecificOutput" not in claude
    assert out["terminalSequence"] == "\x1b]2;◉ PulsarCode\x07" and cc_allows(out["terminalSequence"])
    # a title the user picked with /rename survives a resume
    out = notify.session_start_output({}, {}, dict(payload, session_title="my feature"))
    assert "hookSpecificOutput" not in out


def test_banner_names_pulsarcode_resume_and_continue_not_claude():
    """`claude --resume <id>` cannot work: the session lives in Pulsar's isolated profile and bare claude skips
    Pulsar Core. The banner says what does, on a line of its own that fits 80 columns."""
    for env in ({"NO_COLOR": "1"}, {"COLORTERM": "truecolor"}):
        msg = notify.session_start_output({"plan": [{"model": "fake/m"}]}, env, {"session_id": SID})["systemMessage"]
        plain = re.sub(r"\x1b\[[0-9;]*m", "", msg)
        hint = plain.split("\n")[-1]
        assert hint == notify.RESUME_HINT
        assert "pulsarcode --continue" in hint and "pulsarcode --resume <id>" in hint and "not claude" in hint
        assert len(hint) + 5 <= 80                              # Claude Code indents the message by 5
        assert plain.split("\n")[0] == "◉ uplink ready" and plain.split("\n")[1].endswith("PulsarCode " + pulsar_version())


def test_a_session_titled_by_pulsar_3_0_0_gets_a_unique_title_on_resume():
    """A hook cannot clear a title, so an old session keeps "◉ Pulsar Code" unless it is given a unique one."""
    a = notify.session_start_output({}, {}, {"session_id": SID, "cwd": "/w/proj", "source": "resume",
                                            "session_title": "◉ Pulsar Code"})
    b = notify.session_start_output({}, {}, {"session_id": "0f1e2d3c-aaaa-bbbb-cccc-000000000000", "cwd": "/w/proj",
                                            "source": "resume", "session_title": "◉ Pulsar Code"})
    ta, tb = a["hookSpecificOutput"]["sessionTitle"], b["hookSpecificOutput"]["sessionTitle"]
    assert a["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert ta == "◉ Pulsar Code · proj · c41d0c8c" and tb == "◉ Pulsar Code · proj · 0f1e2d3c" and ta != tb
    assert notify.legacy_session_title({"session_id": SID, "session_title": "◉ Pulsar Code"}) == \
        "◉ Pulsar Code · c41d0c8c"
    for payload in ({"session_id": SID, "session_title": "◉ Pulsar Code · proj · c41d0c8c"},
                    {"session_id": SID, "session_title": "◉ pulsar code"}, {"session_id": SID},
                    {"session_title": "◉ Pulsar Code"}, {"session_id": "bad id;rm", "session_title": "◉ Pulsar Code"}):
        assert notify.legacy_session_title(payload) is None, payload


def test_main_hands_the_hook_input_to_the_banner(monkeypatch, capsys):
    from pulsar import statusline
    monkeypatch.setattr(statusline, "core_get", lambda *a, **kw: None)
    stdin = json.dumps({"session_id": SID, "cwd": "/w/proj", "source": "startup", "hook_event_name": "SessionStart"})
    rc, out = _main(monkeypatch, capsys, ["session-start"], stdin, NO_COLOR="1")
    data = json.loads(out)
    assert rc == 0 and "hookSpecificOutput" not in data and data["systemMessage"].endswith(notify.RESUME_HINT)
    stdin = json.dumps({"session_id": SID, "cwd": "/w/proj", "source": "resume", "session_title": "◉ Pulsar Code"})
    rc, out = _main(monkeypatch, capsys, ["session-start"], stdin, NO_COLOR="1")
    assert rc == 0 and json.loads(out)["hookSpecificOutput"]["sessionTitle"] == "◉ Pulsar Code · proj · c41d0c8c"


def test_plugin_hooks_run_the_notify_module_directly(pulsar_home, monkeypatch):
    from pulsar import plugin
    monkeypatch.delenv("PULSAR_NOTIFY", raising=False)
    hooks = plugin._hooks()
    assert set(hooks) == {"SessionStart", "Notification", "Stop"}
    for event in hooks.values():
        cmd = event[0]["hooks"][0]["command"]
        assert "pulsar.notify" in cmd and " -E " in cmd
    monkeypatch.setenv("PULSAR_NOTIFY", "off")
    assert set(plugin._hooks()) == {"SessionStart"}


@pytest.mark.parametrize("prov, name", [("groq", "Groq"), ("xai", "xAI"), ("openrouter", "OpenRouter"),
                                        ("zai", "Z.ai GLM")])
def test_session_banner_names_the_provider_like_the_picker(prov, name):
    """The banner says `via Groq`, as WARP, the picker and pulsar_switch do, never the raw registry id."""
    out = notify.session_start_output({"plan": [{"model": "fake/model-1", "provider": prov, "profile": prov},
                                                {"model": "fake/model-2", "provider": "xai"}]}, {"NO_COLOR": "1"})
    msg = out["systemMessage"]
    assert "fake/model-1 via " + name + "\n" in msg and "\nrelay fake/model-2\n" in msg and "via " + prov + " " not in msg
    custom = {"plan": [{"model": "fake/model-1", "provider": "custom", "base_url": "http://127.0.0.1:18931/v1"}]}
    assert "via 127.0.0.1:18931" in notify.session_start_output(custom, {"NO_COLOR": "1"})["systemMessage"]
    unknown = notify.session_start_output({"plan": [{"model": "m", "provider": "fakeprov"}]}, {"NO_COLOR": "1"})
    assert "m via fakeprov" in unknown["systemMessage"]
