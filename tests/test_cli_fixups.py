# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Regression tests for the CLI: providers list, demo labelling, update, help, stop, switch, relay, local endpoints
and v1 upgrades."""

import os
import sys
import types

import pytest

from pulsar import cli
from pulsar import config as K
from pulsar import launch as L
from pulsar import local as LOC
from pulsar import providers as P
from pulsar import screens as S

SESSION_VARS = ("PULSAR_SESSION", "CLAUDECODE", "PULSAR_SESSION_TOKEN")


@pytest.fixture(autouse=True)
def hermetic(monkeypatch):
    """No network, no Core, not inside a Claude Code session or under a Claude Code process (the suite itself
    may run under one), no package manager."""
    from pulsar.core import spawn
    monkeypatch.setattr(spawn, "ensure_core", lambda *a, **kw: pytest.fail("tests must not spawn Pulsar Core"))
    monkeypatch.setattr(spawn, "attach", lambda *a, **kw: None)
    monkeypatch.setattr(L, "stop_core", lambda force=False: False)
    monkeypatch.setattr(L, "claude_ancestor", lambda *a, **kw: False)
    monkeypatch.setattr(cli, "_https_get", lambda *a, **kw: pytest.fail("tests must not download anything"))
    monkeypatch.setattr(cli, "install_channel", lambda *a, **kw: ("", "", ""))
    for var in SESSION_VARS + ("PULSAR_REF", "PULSAR_RELAY", "PULSARCODE_LOCAL_ENDPOINT", "PULSAR_LOCAL_ENDPOINT",
                               "PULSARCODE_PREFER_LOCAL"):
        monkeypatch.delenv(var, raising=False)


def _connect(*profiles):
    state = K.load()
    for prof in profiles:
        state.upsert(prof)
    K.save(state)


def _stub_launch(monkeypatch):
    """Claude Code, Core and the session replaced: returns what start_session and exec_claude received."""
    from pulsar.core import spawn
    from pulsar.core import state as ST
    info = ST.CoreInfo(port=4747, pid=1, version="3", admin_token="pla_x")
    monkeypatch.setattr(spawn, "attach", lambda *a, **kw: info)      # the preload finds a "warm" stub Core
    seen = {}

    def start(state, prof, key_of, label="", warn=None, **kw):
        seen["profile"] = prof.id
        seen.update(kw)
        return info, types.SimpleNamespace(sid="s-test", plan=[]), "pls_test"
    monkeypatch.setattr(L, "start_session", start)
    monkeypatch.setattr(L, "find_claude", lambda: "/bin/true")
    monkeypatch.setattr(L, "claude_version", lambda b: "2.1.287")
    monkeypatch.setattr(L, "exec_claude", lambda binary, args, env, **kw: seen.update(args=args, env=env) or 0)
    return seen


# ---------------------------------------------------------------------------
# 1. providers: advertised list only, plus whatever the user already connected
# ---------------------------------------------------------------------------

def test_providers_lists_only_advertised_profiles(capsys):
    assert cli.main(["providers"]) == 0
    out = capsys.readouterr().out
    for p in S.advertised_providers():
        assert p.name in out, p.id
    for pid in S.UNADVERTISED:
        assert P.get(pid).name not in out
    assert "subscription" not in out.lower()
    assert "connected (not in the list above)" not in out


def test_providers_still_shows_a_connected_unadvertised_profile(capsys):
    _connect(K.Profile("claude", "claude", model="sonnet"), K.Profile("groq", "groq", model="m"))
    assert cli.main(["providers"]) == 0
    out = capsys.readouterr().out
    head, _, tail = out.partition("connected (not in the list above)")
    assert tail, out
    assert P.get("claude").name not in head and P.get("claude").name in tail
    assert "pulsarcode use claude" in tail
    assert P.get("groq").name in head and P.get("groq").name not in tail   # advertised: stays in the main list


def test_providers_marks_a_connected_advertised_profile(capsys, monkeypatch):
    monkeypatch.setattr(cli, "glyph", lambda uni, plain, stream=None: plain)
    _connect(K.Profile("groq", "groq", model="m"))
    cli.main(["providers"])
    lines = capsys.readouterr().out.splitlines()
    groq = next(line for line in lines if P.get("groq").name in line)
    other = next(line for line in lines if P.get("openrouter").name in line)
    assert groq.lstrip().startswith("*") and other.lstrip().startswith("o")


# ---------------------------------------------------------------------------
# 2. demo: labelled explicitly, never by argv detection
# ---------------------------------------------------------------------------

def test_demo_passes_demo_true_explicitly(monkeypatch):
    seen = {}
    monkeypatch.setattr(sys, "argv", ["pulsarcode"])          # argv detection would say "not demo"
    monkeypatch.setattr(S, "boot", lambda ui, tasks, quick=False, demo=None: seen.update(demo=demo, quick=quick) or "go")
    monkeypatch.setattr(S, "warp", lambda ui, lines, duration=0.55: seen.update(warp=lines))
    assert cli.cmd_demo(["--quick"]) == 0
    assert seen["demo"] is True and seen["quick"] is True and seen["warp"]


# ---------------------------------------------------------------------------
# 3. update: no release tag -> stop, never a silent install of main, never backwards by accident
# ---------------------------------------------------------------------------

def _record_installs(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_download_installer", lambda ref: calls.append(ref) or "/nonexistent/install.sh")
    monkeypatch.setattr(cli, "_drop_installer", lambda path: None)
    monkeypatch.setattr(cli.subprocess, "call",
                        lambda cmd, env=None, cwd=None: calls.append(("run", env["PULSAR_REF"])) or 0)
    return calls


def test_update_without_a_release_stops_instead_of_installing_main(monkeypatch, capsys):
    calls = _record_installs(monkeypatch)
    monkeypatch.setattr(cli, "latest_release", lambda: "")
    assert cli.main(["update"]) == 1
    assert calls == []
    err = capsys.readouterr().err
    assert "Nothing was installed" in err
    assert "pulsarcode update main" in err and "pulsarcode update vX.Y.Z" in err
    assert "installing" not in err


def test_update_latest_release_is_installed(monkeypatch):
    calls = _record_installs(monkeypatch)
    monkeypatch.setattr(cli, "latest_release", lambda: "v3.0.1")
    assert cli.main(["upgrade"]) == 0
    assert calls == ["v3.0.1", ("run", "v3.0.1")]


@pytest.mark.parametrize("ref", ["main", "v3.0.0"])
def test_update_explicit_ref_still_works_without_the_release_api(monkeypatch, ref):
    calls = _record_installs(monkeypatch)
    monkeypatch.setattr(cli, "latest_release", lambda: pytest.fail("an explicit ref must not ask GitHub"))
    assert cli.main(["update", ref]) == 0
    assert calls == [ref, ("run", ref)]
    calls.clear()
    monkeypatch.setenv("PULSAR_REF", "v9.9.9")              # an exported pin never replaces the ref you name
    assert cli.main(["update", ref]) == 0
    assert calls == [ref, ("run", ref)]


def test_update_ignores_an_exported_pulsar_ref(monkeypatch, capsys):
    calls = _record_installs(monkeypatch)
    monkeypatch.setenv("PULSAR_REF", "main")                # a pin left in a dotfile
    monkeypatch.setattr(cli, "latest_release", lambda: "v3.0.1")
    assert cli.main(["update"]) == 0
    assert calls == ["v3.0.1", ("run", "v3.0.1")]
    err = capsys.readouterr().err
    assert "PULSAR_REF" in err and "pulsarcode update <ref>" in err


def test_update_refuses_a_release_older_than_this_one(monkeypatch, capsys):
    calls = _record_installs(monkeypatch)
    monkeypatch.setattr(cli, "__version__", "3.2.0")
    monkeypatch.setattr(cli, "latest_release", lambda: "v3.1.0")    # an older release marked Latest
    assert cli.main(["update"]) == 1 and calls == []
    err = capsys.readouterr().err
    assert "v3.1.0" in err and "older than" in err and "Nothing was installed" in err
    assert "pulsarcode update v3.1.0 --allow-downgrade" in err
    assert cli.main(["update", "v3.1.0"]) == 2 and calls == []
    assert cli.main(["update", "v3.2.0-rc.1"]) == 2 and calls == []      # a pre-release of this version is older
    assert cli.main(["update", "v3.1.0", "--allow-downgrade"]) == 0
    assert calls == ["v3.1.0", ("run", "v3.1.0")]
    calls.clear()
    assert cli.main(["update", "--allow-downgrade"]) == 0 and calls == ["v3.1.0", ("run", "v3.1.0")]
    calls.clear()
    assert cli.main(["update", "v3.2.0"]) == 0 and calls == ["v3.2.0", ("run", "v3.2.0")]      # a reinstall
    calls.clear()
    assert cli.main(["update", "v3.1.0", "main"]) == 2 and calls == []                      # one ref only


@pytest.mark.parametrize("tag", ["v1.0.11", "v2.0.0", "v0.2.0", "v2.9.9-rc.1", "1.0.11"])
def test_update_never_installs_a_release_before_v3(monkeypatch, capsys, tag):
    calls = _record_installs(monkeypatch)
    monkeypatch.setattr(cli, "latest_release", lambda: tag)
    assert cli.main(["update"]) == 1
    assert cli.main(["update", tag]) == 2
    assert cli.main(["update", tag, "--allow-downgrade"]) == 2
    assert calls == []
    err = capsys.readouterr().err
    assert "v3.0.0" in err and "Nothing was installed" in err


def test_version_key_orders_releases():
    key = cli.version_key
    assert key("v3.0.0") == key("3.0.0") and key("v3") == key("v3.0.0")
    assert key("v3.0.0-rc.1") < key("v3.0.0") < key("v3.0.1") < key("v3.1.0") < key("v10.0.0")
    assert key("v3.1.0.dev1") < key("v3.1.0a1") < key("v3.1.0-beta.2") < key("v3.1.0rc1") < key("v3.1.0")
    for ref in ("main", "feature/x", "a1b2c3d", "release-2026", "1.x"):
        assert key(ref) is None, ref


@pytest.mark.skipif(os.name == "nt", reason="POSIX shells")
def test_update_runs_bash_by_absolute_path_from_its_private_folder(monkeypatch, tmp_path):
    clone = tmp_path / "clone"                  # a cloned repository that ships its own tools
    clone.mkdir()
    for tool in ("bash", "curl", "python3", "tar"):
        f = clone / tool
        f.write_text("#!/bin/sh\necho planted >&2\n")
        f.chmod(0o755)
    monkeypatch.chdir(clone)
    monkeypatch.setenv("PATH", os.pathsep.join(["/nonexistent-first", "", ".", "node_modules/.bin", str(clone),
                                                os.environ.get("PATH") or "/usr/bin:/bin"]))
    monkeypatch.setattr(cli, "_https_get", lambda url, *a, **kw: b"#!/bin/bash\n")
    seen = {}
    monkeypatch.setattr(cli.subprocess, "call",
                        lambda cmd, env=None, cwd=None: seen.update(cmd=cmd, env=env, cwd=cwd) or 0)
    assert cli.main(["update", "v3.1.0"]) == 0
    bash = seen["cmd"][0]
    assert os.path.isabs(bash) and os.path.basename(bash) == "bash"
    assert os.path.realpath(os.path.dirname(bash)) != os.path.realpath(str(clone))
    assert seen["cwd"] and os.path.realpath(seen["cwd"]) == os.path.realpath(os.path.dirname(seen["cmd"][-1]))
    entries = seen["env"]["PATH"].split(os.pathsep)
    assert entries and all(e and os.path.isabs(e) for e in entries)
    assert os.path.realpath(str(clone)) not in [os.path.realpath(e) for e in entries]


@pytest.mark.skipif(os.name == "nt" or not os.access("/bin/bash", os.X_OK), reason="POSIX with /bin/bash")
def test_update_runs_the_system_bash_before_any_bash_on_path(monkeypatch, tmp_path):
    tools = tmp_path / "proj" / "node_modules" / ".bin"       # an absolute PATH entry inside a project
    tools.mkdir(parents=True)
    planted = tools / "bash"
    planted.write_text("#!/bin/sh\necho planted >&2\n")
    planted.chmod(0o755)
    monkeypatch.chdir(tmp_path / "proj")
    monkeypatch.setenv("PATH", os.pathsep.join([str(tools), "/usr/bin", "/bin"]))
    monkeypatch.setattr(cli, "_https_get", lambda url, *a, **kw: b"#!/bin/bash\n")
    seen = {}
    monkeypatch.setattr(cli.subprocess, "call", lambda cmd, env=None, cwd=None: seen.update(cmd=cmd) or 0)
    assert cli.main(["update", "v3.1.0"]) == 0
    assert seen["cmd"][0] == "/bin/bash"


# ---------------------------------------------------------------------------
# switch: the running sessions keep only the relays relay_plan's rules allow
# ---------------------------------------------------------------------------

def test_switch_follows_the_relay_rules(monkeypatch):
    from pulsar.core import state as ST
    _connect(K.Profile("nvidia", "nvidia", model="a"), K.Profile("groq", "groq", model="b"),
             K.Profile("ollama", "ollama", model="qwen3"))
    nvidia, groq = (L.target_for(K.load().profiles[pid]) for pid in ("nvidia", "groq"))
    live = [ST.Session(sid="s1", token_sha256="h", plan=[nvidia, groq]),
            ST.Session(sid="s2", token_sha256="h", plan=[nvidia, groq], flags={"pilot": True})]
    plans = {}
    monkeypatch.setattr(ST, "live_sessions", lambda reap=False: live)
    monkeypatch.setattr(ST, "update_plan",
                        lambda sid, plan: plans.update({sid: [(t.profile, t.model) for t in plan]}) or True)
    # to a local model: the cloud relays go (a local primary never relays automatically)
    assert cli.main(["switch", "--all", "ollama:qwen3"]) == 0
    assert plans == {"s1": [("ollama", "qwen3")], "s2": [("ollama", "qwen3")]}
    # between cloud models the earlier ones stay as relays, but never in a session with Pilot armed
    assert cli.main(["switch", "--all", "groq:other"]) == 0
    assert plans["s1"] == [("groq", "other"), ("nvidia", "a"), ("groq", "b")]
    assert plans["s2"] == [("groq", "other")]


# ---------------------------------------------------------------------------
# relay: PULSAR_RELAY=off is reported over a saved choice
# ---------------------------------------------------------------------------

def test_relay_status_reports_the_env_override(monkeypatch, capsys):
    _connect(K.Profile("groq", "groq", model="m"))
    assert cli.main(["relay", "auto"]) == 0
    capsys.readouterr()
    monkeypatch.setenv("PULSAR_RELAY", "off")
    assert cli.main(["relay"]) == 0
    assert "relay: off (PULSAR_RELAY=off for this shell; saved: auto)" in capsys.readouterr().out
    assert cli.main(["relay", "auto"]) == 0
    assert "PULSAR_RELAY=off is set in this shell" in capsys.readouterr().out


def test_relay_says_when_a_session_relays(monkeypatch, capsys):
    monkeypatch.delenv("PULSAR_PILOT", raising=False)
    _connect(K.Profile("groq", "groq", model="m"), K.Profile("ollama", "ollama", model="qwen3"))
    assert cli.main(["relay"]) == 0
    out = capsys.readouterr().out
    assert "relay: auto" in out and "only from a cloud provider to the other cloud providers" in out
    assert "custom endpoint never relays" in out and "Pilot armed never relays" in out
    assert cli.main(["relay", "auto"]) == 0
    out = capsys.readouterr().out
    assert "only from a cloud provider" in out and "Pilot armed never relays" in out
    assert cli.main(["relay", "groq:m"]) == 0                     # an explicit chain: from any model
    out = capsys.readouterr().out
    assert "only from a cloud provider" not in out and "Pilot armed never relays" in out
    assert cli.main(["relay", "off"]) == 0 and "never relays" not in capsys.readouterr().out
    st = K.load()                                                 # Pilot on: this launch would run alone
    st.prefs.update(relay="auto", pilot=True)
    K.save(st)
    assert cli.main(["relay"]) == 0 and "Pilot is on" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 5. help text: nothing public about the subscription route, and true to the code
# ---------------------------------------------------------------------------

def test_help_never_mentions_the_subscription_route(capsys):
    assert cli.main(["help"]) == 0
    out = capsys.readouterr().out.lower()
    for word in ("subscription", "setup-token", "oauth", "pro or max", "sk-ant-oat"):
        assert word not in out, word
    assert "pulsarcode update [ref]" in out
    assert "pilot on" in out and "switching other" in out     # every human-gated command is named


def test_help_names_only_real_subcommands(capsys):
    import re
    cli.main(["help"])
    out = capsys.readouterr().out
    for sub in re.findall(r"^  pulsarcode ([a-z]+)", out, re.M):
        assert sub in cli.SUBCOMMANDS, sub


def test_help_names_every_notify_mode_and_stop_force(capsys):
    from pulsar import notify
    assert cli.main(["help"]) == 0
    out = capsys.readouterr().out
    assert "PULSAR_NOTIFY=off|bell|os|loud" in out and "PULSAR_NOTIFY=loud" in notify.__doc__
    assert "pulsarcode stop [--force]" in out


# ---------------------------------------------------------------------------
# stop: Core waits for its live sessions unless --force
# ---------------------------------------------------------------------------

def _live_sessions(monkeypatch, n):
    from pulsar.core import state as ST
    live = [ST.Session(sid="s%d" % i, token_sha256="h", plan=[]) for i in range(n)]
    monkeypatch.setattr(ST, "live_sessions", lambda reap=False: live)


def test_stop_waits_for_live_sessions_unless_forced(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(L, "stop_core", lambda force=False: calls.append(force) or True)
    _live_sessions(monkeypatch, 2)
    assert cli.main(["stop"]) == 0
    out = capsys.readouterr().out
    assert calls == [False] and "2 live sessions end" in out and "pulsarcode stop --force" in out
    assert "shutting down" not in out
    _live_sessions(monkeypatch, 1)
    assert cli.main(["stop"]) == 0 and "1 live session ends" in capsys.readouterr().out
    assert cli.main(["stop", "--force"]) == 0
    assert calls[-1] is True and "is shutting down" in capsys.readouterr().out
    _live_sessions(monkeypatch, 0)
    assert cli.main(["stop"]) == 0
    assert calls[-1] is False and "is shutting down" in capsys.readouterr().out
    monkeypatch.setattr(L, "stop_core", lambda force=False: False)
    assert cli.main(["stop"]) == 0 and "was not running" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# a self-hosted endpoint that takes no key keeps working after the first launch
# ---------------------------------------------------------------------------

def test_a_keyless_self_hosted_endpoint_relaunches(monkeypatch):
    seen = _stub_launch(monkeypatch)
    det = P.detect("http://192.168.1.50:8001/v1")
    assert det.best.id == "custom" and not det.best.needs_key
    link = S.Link(det.best, "", S.profile_id_for(det.best, det.base_url), base_url=det.base_url)
    cli.save_link(K.load(), link)
    for prompt in ("hello", "again"):                  # every later launch, not just the first one
        seen.clear()
        assert cli.main(["-p", prompt]) == 0
        assert seen["profile"] == link.profile_id and seen["args"][-2:] == ["-p", prompt]
    assert K.load().current().resolve().needs_key is False


# ---------------------------------------------------------------------------
# v1 users: the key file is forgotten with the key, NVIDIA_NIM_API_BASE and PULSAR_NIM_* still count
# ---------------------------------------------------------------------------

def test_logout_nvidia_forgets_the_v1_key_files(pulsar_home, capsys):
    pulsar_home.mkdir(parents=True)
    (pulsar_home / "nim.key").write_text("nvapi-legacy\n")
    assert K.load().active == "nvidia" and (pulsar_home / "nim.key.migrated").exists()
    (pulsar_home / "nim.key").write_text("nvapi-again\n")            # a v1 reinstall wrote it again
    assert cli.main(["logout", "nvidia"]) == 0
    assert not (pulsar_home / "nim.key").exists() and not (pulsar_home / "nim.key.migrated").exists()
    assert K.get_key("nvidia") == "" and "nvidia" not in K.load().profiles
    K.config_path().unlink()
    assert K.load().profiles == {}                                    # nothing left to bring the key back
    # a key file an earlier logout left behind is forgotten too
    (pulsar_home / "nim.key.migrated").write_text("nvapi-left-over\n")
    capsys.readouterr()
    assert cli.main(["logout", "nvidia"]) == 0 and not (pulsar_home / "nim.key.migrated").exists()
    assert "nim.key.migrated" in capsys.readouterr().out


def test_the_v1_key_copy_is_offered_for_removal_once(monkeypatch, pulsar_home, capsys):
    pulsar_home.mkdir(parents=True)
    (pulsar_home / "nim.key").write_text("nvapi-legacy\n")
    st = K.load()                                       # the v1 import: keys.json holds the key, a copy stays
    kept = pulsar_home / "nim.key.migrated"
    assert kept.exists() and K.get_key("nvidia") == "nvapi-legacy"
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or "n")
    cli.offer_to_forget_v1_key(st)
    assert len(asked) == 1 and "nim.key.migrated" in asked[0] and kept.exists()
    cli.offer_to_forget_v1_key(K.load())                # asked once: the answer stands
    assert len(asked) == 1
    st = K.load()
    st.prefs.pop("v1_key_asked")
    K.save(st)
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or "")       # Enter: yes
    cli.offer_to_forget_v1_key(K.load())
    assert len(asked) == 2 and not kept.exists() and K.get_key("nvidia") == "nvapi-legacy"
    assert "nvapi-legacy" not in capsys.readouterr().out
    cli.offer_to_forget_v1_key(K.State())               # nothing left, or no key in keys.json: nothing asked
    assert len(asked) == 2


class _TerminalCaps:
    """A terminal without colour: the plain boot, no animation."""
    def __init__(self):
        import io
        self.out = io.StringIO()
        self.tty, self.color, self.unicode, self.motion = True, 0, False, False
        self.reduced, self.fx, self.ci = False, "plain", False
        self.size = (80, 24)


def test_a_launch_offers_it_only_on_a_terminal(monkeypatch):
    _stub_launch(monkeypatch)
    _groq_saved()
    calls = []
    monkeypatch.setattr(cli, "offer_to_forget_v1_key", lambda state: calls.append(state.active))
    assert cli.main(["-p", "hi"]) == 0 and calls == []                 # print mode: never a question
    monkeypatch.setattr(cli, "_ui", lambda: S.UI(_TerminalCaps()))
    monkeypatch.setattr(cli, "_stdio_is_tty", lambda: True)
    assert cli.main(["hi"]) == 0 and calls == ["groq"]
    monkeypatch.setenv("CLAUDECODE", "1")                               # inside a session: never
    assert cli.main(["hi"]) == 0 and calls == ["groq"]


def test_an_adopted_nvidia_key_honours_nvidia_nim_api_base(monkeypatch, capsys):
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "nvapi-" + "x" * 30)
    monkeypatch.setenv("NVIDIA_NIM_API_BASE", "http://192.168.1.9:8000/v1")
    st = K.load()
    assert cli.adopt_env(st, announce=True)
    prof = st.profiles["nvidia"]
    assert prof.base_url == "http://192.168.1.9:8000/v1" and prof.resolve().base_url == "http://192.168.1.9:8000/v1"
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1 and "NVIDIA_NIM_API_BASE" in err and "192.168.1.9:8000" in err
    st = K.State()
    assert cli.adopt_env(st) and st.profiles["nvidia"].base_url == "http://192.168.1.9:8000/v1"
    assert capsys.readouterr().err == ""                                  # said once, by the launch that saves it
    monkeypatch.delenv("NVIDIA_NIM_API_BASE")
    st = K.State()
    assert cli.adopt_env(st, announce=True) and st.profiles["nvidia"].base_url == ""
    assert capsys.readouterr().err == ""


def test_v1_tuning_names_are_read_as_fallbacks(monkeypatch):
    for _old, new in cli.V1_TUNING:
        monkeypatch.setenv(new, "x")
        monkeypatch.delenv(new)                                           # removed again after the test
    monkeypatch.setenv("PULSAR_NIM_FIRST_TOKEN_TIMEOUT", "300")
    monkeypatch.setenv("PULSAR_NIM_STREAM_HEADER_TIMEOUT", "240")
    monkeypatch.setenv("PULSAR_HEADER_TIMEOUT_REASONING", "500")         # the v3 name always wins
    monkeypatch.setenv("PULSAR_NIM_STREAM_HEADER_TIMEOUT_REASONING", "999")
    monkeypatch.setenv("PULSAR_NIM_STREAM_PING_INTERVAL", "")
    assert cli.main(["version"]) == 0
    assert os.environ["PULSAR_FIRST_TOKEN_TIMEOUT"] == "300" and os.environ["PULSAR_HEADER_TIMEOUT"] == "240"
    assert os.environ["PULSAR_HEADER_TIMEOUT_REASONING"] == "500" and "PULSAR_PING_INTERVAL" not in os.environ
    from pulsar.core import engine
    assert engine.Tuning().first_token_timeout == 300.0 and engine.Tuning().header_timeout == 240.0


# ---------------------------------------------------------------------------
# v1 subcommand words: a one-line hint, never a prompt
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("word", ["local", "nim", "nvidia", "kimi", "gemma", "qwen", "sonar-pick"])
def test_retired_v1_words_are_refused_not_sent_as_a_prompt(monkeypatch, capsys, word):
    sent = []
    monkeypatch.setattr(cli, "cmd_launch", lambda args, force="": sent.append(args) or 0)
    assert cli.main([word, "--build"]) == 2
    out = capsys.readouterr()
    assert sent == [] and out.out == ""
    lines = out.err.strip().splitlines()
    assert len(lines) == 1 and ("pulsarcode %s" % word) in lines[0], lines
    assert cli.main(["--", word]) == 0 and sent == [[word]]          # `--` still hands any word to Claude Code


# ---------------------------------------------------------------------------
# the isolated profile is known by the mode, not by a folder name
# ---------------------------------------------------------------------------

def _launch_and_watch(monkeypatch):
    """Launch headless; return the claude_args keywords and the boot tasks."""
    from pulsar import plugin
    got, made = {}, []
    real_args, real_task = plugin.claude_args, S.Task
    monkeypatch.setattr(plugin, "claude_args", lambda *a, **kw: got.update(kw) or real_args(*a, **kw))
    monkeypatch.setattr(S, "Task", lambda label, fn: made.append(real_task(label, fn)) or made[-1])
    assert cli.main(["-p", "hi"]) == 0
    return got, next(t.detail for t in made if t.label == "Plugin + profile")


def test_the_profile_is_isolated_by_mode_not_by_folder_name(monkeypatch, pulsar_home, tmp_path):
    assert "pulsarcode" not in str(pulsar_home)                       # e.g. PULSAR_HOME=/opt/pc
    _stub_launch(monkeypatch)
    _connect(K.Profile("groq", "groq", model="m"))
    K.set_key("groq", "gsk_" + "x" * 30)
    got, detail = _launch_and_watch(monkeypatch)
    assert got["isolated"] is True and detail.startswith("isolated")
    mine = tmp_path / "my-claude"                                      # shared mode, with the user's own dir
    mine.mkdir()
    monkeypatch.setenv("PULSAR_CLAUDE_CONFIG", "shared")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(mine))
    got, detail = _launch_and_watch(monkeypatch)
    assert got["isolated"] is False and detail.startswith("shared")


def test_shared_mode_names_the_command_files_it_left_in_place(monkeypatch, tmp_path, capsys):
    _stub_launch(monkeypatch)
    _connect(K.Profile("groq", "groq", model="m"))
    K.set_key("groq", "gsk_" + "x" * 30)
    mine = tmp_path / "my-claude"
    (mine / "commands").mkdir(parents=True)
    edited = "my own /model\nManaged by pulsarcode.\n"
    (mine / "commands" / "model.md").write_text(edited)
    monkeypatch.setenv("PULSAR_CLAUDE_CONFIG", "shared")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(mine))
    assert cli.main(["-p", "hi"]) == 0
    lines = [line for line in capsys.readouterr().err.splitlines() if "model.md" in line]
    assert len(lines) == 1 and str(mine / "commands") in lines[0], lines
    assert (mine / "commands" / "model.md").read_text() == edited                 # left exactly as it was
    assert cli.main(["-p", "hi"]) == 0 and "model.md" not in capsys.readouterr().err    # said once



# ---------------------------------------------------------------------------
# PULSARCODE_LOCAL_ENDPOINT: a self-hosted endpoint, preferred at launch while it answers
# ---------------------------------------------------------------------------

def _groq_saved():
    _connect(K.Profile("groq", "groq", model="m"))
    K.set_key("groq", "gsk_" + "x" * 30)


def _endpoint(monkeypatch, url, answers, var="PULSARCODE_LOCAL_ENDPOINT"):
    monkeypatch.setenv(var, url)
    probes = []
    monkeypatch.setattr(LOC, "endpoint_answers", lambda base, timeout=1.5: probes.append(base) or answers)
    return probes


def test_a_local_endpoint_that_answers_is_used_and_the_default_kept(monkeypatch):
    seen = _stub_launch(monkeypatch)
    _groq_saved()
    probes = _endpoint(monkeypatch, "http://127.0.0.1:8081/v1/", True)
    assert cli.main(["-p", "hi"]) == 0
    assert probes == ["http://127.0.0.1:8081/v1"] and seen["profile"] == "custom-127-0-0-1-8081"
    st = K.load()
    assert st.active == "groq"                                         # the saved default is untouched
    prof = st.profiles["custom-127-0-0-1-8081"]
    assert prof.base_url == "http://127.0.0.1:8081/v1" and prof.needs_key is False and not prof.native


def test_a_local_endpoint_that_does_not_answer_leaves_the_launch_alone(monkeypatch, capsys):
    seen = _stub_launch(monkeypatch)
    _groq_saved()
    _endpoint(monkeypatch, "http://127.0.0.1:8081/v1", False)
    assert cli.main(["-p", "hi"]) == 0 and seen["profile"] == "groq"
    err = capsys.readouterr().err
    assert "PULSARCODE_LOCAL_ENDPOINT" in err and "does not answer" in err and "Groq" in err
    st = K.load()                                       # chosen as the saved provider (pulsarcode use / pick)
    st.active = "custom-127-0-0-1-8081"
    K.save(st)
    assert cli.main(["-p", "hi"]) == 0 and seen["profile"] == "custom-127-0-0-1-8081"
    err = capsys.readouterr().err
    assert "does not answer" in err and "Groq" not in err


def test_pick_and_models_see_the_local_endpoint_without_asking_it(monkeypatch, capsys):
    _groq_saved()
    monkeypatch.setenv("PULSARCODE_LOCAL_ENDPOINT", "http://127.0.0.1:8081/v1")
    monkeypatch.setattr(LOC, "endpoint_answers", lambda *a, **k: pytest.fail("only a launch asks it"))
    assert cli.main(["models", "--json"]) == 0
    assert "custom-127-0-0-1-8081" in K.load().profiles and K.load().active == "groq"
    seen = {}
    monkeypatch.setattr(cli, "run_picker", lambda ui, state, preloaded=None, **kw: seen.update(ids=list(state.profiles)))
    cli.main(["pick"])
    assert "custom-127-0-0-1-8081" in seen["ids"]


def test_an_anthropic_endpoint_through_the_short_alias(monkeypatch):
    seen = _stub_launch(monkeypatch)
    _groq_saved()
    _endpoint(monkeypatch, "http://192.168.1.50:9000/anthropic", True, var="PULSAR_LOCAL_ENDPOINT")
    assert cli.main(["-p", "hi"]) == 0 and seen["profile"] == "custom-192-168-1-50-9000-anthropic"
    prof = K.load().profiles["custom-192-168-1-50-9000-anthropic"]
    assert prof.native and prof.resolve().route == "native"


def test_a_local_endpoint_alone_is_enough_to_launch(monkeypatch):
    seen = _stub_launch(monkeypatch)
    _endpoint(monkeypatch, "http://127.0.0.1:8081/v1", True)
    assert cli.main(["-p", "hi"]) == 0 and seen["profile"] == "custom-127-0-0-1-8081"


@pytest.mark.parametrize("value", ["0", "off", "none", "false", ""])
def test_the_local_endpoint_kill_switch_means_no_network(monkeypatch, value):
    seen = _stub_launch(monkeypatch)
    _groq_saved()
    monkeypatch.setenv("PULSARCODE_LOCAL_ENDPOINT", value)
    monkeypatch.setenv("PULSAR_LOCAL_ENDPOINT", "http://127.0.0.1:8081/v1")     # the first name set decides
    monkeypatch.setattr(LOC, "endpoint_answers", lambda *a, **k: pytest.fail("no network when it is off"))
    assert cli.main(["-p", "hi"]) == 0 and seen["profile"] == "groq"
    assert not [p for p in K.load().profiles if p.startswith("custom-")]


def test_no_local_endpoint_means_no_network_call(monkeypatch):
    seen = _stub_launch(monkeypatch)
    _groq_saved()
    monkeypatch.setattr(LOC, "endpoint_answers", lambda *a, **k: pytest.fail("nothing to ask"))
    monkeypatch.setattr(LOC, "discover_local", lambda *a, **k: pytest.fail("no discovery at launch"))
    assert cli.main(["-p", "hi"]) == 0 and seen["profile"] == "groq"


def test_prefer_local_off_keeps_the_saved_provider_without_asking(monkeypatch):
    seen = _stub_launch(monkeypatch)
    _groq_saved()
    _endpoint(monkeypatch, "http://127.0.0.1:8081/v1", True)
    monkeypatch.setenv("PULSARCODE_PREFER_LOCAL", "0")
    monkeypatch.setattr(LOC, "endpoint_answers", lambda *a, **k: pytest.fail("not preferred: nothing to ask"))
    assert cli.main(["-p", "hi"]) == 0 and seen["profile"] == "groq"
    assert "custom-127-0-0-1-8081" in K.load().profiles                     # still there for pick and use


def test_a_local_endpoint_that_is_not_a_url_is_ignored(monkeypatch, capsys):
    seen = _stub_launch(monkeypatch)
    _groq_saved()
    _endpoint(monkeypatch, "localhost:8081", True)
    monkeypatch.setattr(LOC, "endpoint_answers", lambda *a, **k: pytest.fail("not a URL: nothing to ask"))
    assert cli.main(["-p", "hi"]) == 0 and seen["profile"] == "groq"
    assert "not an http(s) URL" in capsys.readouterr().err


def test_the_billing_notice_is_dismissed_only_for_sessions_through_core(monkeypatch, pulsar_home):
    import json
    _stub_launch(monkeypatch)
    _groq_saved()
    assert cli.main(["-p", "hi"]) == 0
    profile = pulsar_home / "claude_config" / ".claude.json"
    assert json.loads(profile.read_text()).get("autoModeClassifierBillingNoticeAcknowledgedAt")
    # Anthropic straight from Claude Code (PULSAR_DIRECT=1): the notice may concern your own bill
    profile.write_text("{}")
    _connect(K.Profile("anthropic", "anthropic", model="claude-sonnet-4-5"))
    K.set_key("anthropic", "sk-ant-api03-" + "x" * 40)
    monkeypatch.setenv("PULSAR_DIRECT", "1")
    assert cli.main(["-p", "hi"]) == 0
    data = json.loads(profile.read_text())
    assert data["hasCompletedOnboarding"] is True and "autoModeClassifierBillingNoticeAcknowledgedAt" not in data


def test_the_launch_tells_core_which_route_the_session_takes(monkeypatch):
    seen = _stub_launch(monkeypatch)
    _groq_saved()
    assert cli.main(["-p", "hi"]) == 0 and seen["route"] == "core"
    # Claude Code talking to Anthropic itself: the session token is a side channel only
    _connect(K.Profile("anthropic", "anthropic", model="claude-sonnet-4-5"))
    K.set_key("anthropic", "sk-ant-api03-" + "x" * 40)
    monkeypatch.setenv("PULSAR_DIRECT", "1")
    assert cli.main(["-p", "hi"]) == 0 and seen["route"] == "direct"


def test_print_mode_never_opens_the_stage_viewer_by_itself(monkeypatch):
    seen = _stub_launch(monkeypatch)
    _groq_saved()
    monkeypatch.delenv("PULSAR_STAGE_AUTO", raising=False)
    assert cli.main(["-p", "hi"]) == 0 and seen["env"]["PULSAR_STAGE_AUTO"] == "0"
    assert cli.main(["hi"]) == 0 and "PULSAR_STAGE_AUTO" not in seen["env"]     # an interactive launch: unchanged
    monkeypatch.setenv("PULSAR_STAGE_AUTO", "1")                                # your own choice stays
    assert cli.main(["-p", "hi"]) == 0 and seen["env"]["PULSAR_STAGE_AUTO"] == "1"


def test_a_self_hosted_primary_never_relays_to_a_cloud_provider(monkeypatch):
    """The two primaries Pulsar sets up for a self-hosted model: the configured local endpoint, and the NVIDIA
    profile that NVIDIA_NIM_API_BASE points at a server of your own (this machine or the local network)."""
    _connect(K.Profile("groq", "groq", model="m"), K.Profile("openrouter", "openrouter", model="x/y"))
    st = K.load()

    def keys(pid):
        return "k-0123456789abcdef"
    assert [t.profile for t in L.relay_plan(st, st.profiles["groq"], keys)] == ["openrouter"]     # cloud to cloud
    monkeypatch.setenv("PULSARCODE_LOCAL_ENDPOINT", "http://127.0.0.1:8081/v1")
    local = cli.local_endpoint_profile(st)
    local.model = "qwen3"
    assert L.relay_plan(st, local, keys) == []
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "nvapi-" + "x" * 30)
    monkeypatch.setenv("NVIDIA_NIM_API_BASE", "http://127.0.0.1:8000/v1")
    fresh = K.State()
    assert cli.adopt_env(fresh)
    nim = fresh.profiles["nvidia"]
    nim.model = "m"
    st.upsert(nim, activate=False)
    assert L.relay_plan(st, st.profiles["nvidia"], keys) == []
    assert "nvidia" not in [t.profile for t in L.relay_plan(st, st.profiles["groq"], keys)]


def test_the_boot_counts_only_the_relays_the_launch_will_have(monkeypatch):
    _stub_launch(monkeypatch)
    _groq_saved()
    _connect(K.Profile("openrouter", "openrouter"))                     # no model chosen: never a relay
    st = K.load()
    st.active = "groq"
    K.save(st)
    made = []
    real_task = S.Task
    monkeypatch.setattr(S, "Task", lambda label, fn: made.append(real_task(label, fn)) or made[-1])
    assert cli.main(["-p", "hi"]) == 0
    detail = next(t.detail for t in made if t.label == "Uplink")
    assert "relay" not in detail, detail
