# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""End-to-end regression tests for what a launch hands Claude Code: the settings pins, the plugin, notifications
and the status line."""
import codecs
import json
import os
import stat

import pytest

from pulsar import config as K
from pulsar import launch as L
from pulsar import notify, plugin, statusline
from pulsar import providers as P
from pulsar.core import state as ST

TOKEN = "pls_fakefakefakefake0000"


def core():
    return ST.CoreInfo(port=4123, pid=1, version="x", admin_token="pla_fake-admin-not-real")


def session():
    return ST.Session(sid="sfake1", token_sha256="h", plan=[])


def core_env(context=131_072, model="openai/gpt-oss-120b", base=None):
    return L.build_env(P.get("nvidia"), "nvapi-fake-not-real", model, context, core(), session(), TOKEN,
                       base=base if base is not None else {"PATH": "/bin"})


@pytest.fixture(autouse=True)
def keep_umask():
    old = os.umask(0o022)                  # earlier tests may have run the launcher's private_umask
    saved = L._USER_UMASK
    yield
    os.umask(old)
    L._USER_UMASK = saved


# --- 1. project settings cannot re-route a Pulsar session ----------------------------------------------------

def test_session_settings_pin_endpoint_token_proxies_and_key_helper(pulsar_home):
    env = core_env(base={"PATH": "/bin", "HTTPS_PROXY": "http://corp.example:3128", "NODE_OPTIONS": ""})
    path = plugin.write_settings("openai/gpt-oss-120b", session_id="sfake1", env=env)
    data = json.loads(path.read_text())
    pins = data["env"]
    assert data["apiKeyHelper"] == ""
    assert pins["ANTHROPIC_BASE_URL"] == pins["PULSAR_CORE"] == "http://127.0.0.1:4123"
    assert pins["ANTHROPIC_AUTH_TOKEN"] == pins["ANTHROPIC_AUTH_TOKEN"] == TOKEN
    assert pins["ANTHROPIC_API_KEY"] == "" and pins["PULSAR_SESSION"] == "sfake1"
    assert pins["HTTPS_PROXY"] == "http://corp.example:3128"           # the user's own proxy stays
    assert pins["HTTP_PROXY"] == pins["ALL_PROXY"] == pins["NODE_OPTIONS"] == ""
    assert "127.0.0.1" in pins["NO_PROXY"].split(",") and pins["no_proxy"] == pins["NO_PROXY"]
    for var in L.MODEL_VARS:
        assert pins[var] == "openai/gpt-oss-120b"
    assert pins["PULSAR_PILOT"] == "" and pins["PULSAR_HOME"] == str(K.home())
    assert stat.S_IMODE(os.stat(str(path)).st_mode) == 0o600
    assert "nvapi-fake-not-real" not in path.read_text() and "pla_fake-admin" not in path.read_text()


def test_provider_keys_are_never_written_into_the_pins():
    env = L.build_env(P.get("anthropic"), "sk-ant-fake-not-real-0000", "", 0, None, base={"PATH": "/bin"})
    pins = plugin.session_env(env)
    assert "ANTHROPIC_API_KEY" not in pins and "sk-ant-fake" not in json.dumps(pins)
    assert pins["ANTHROPIC_BASE_URL"] == ""                              # Claude Code's default endpoint


def test_exec_pins_the_pulsar_settings_file_and_warns_about_the_project(pulsar_home, tmp_path, monkeypatch):
    env = core_env()
    args = plugin.claude_args("openai/gpt-oss-120b", session_id="sfake1") + ["-p", "hi"]
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "settings.json").write_text(json.dumps({
        "env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:59999", "PULSAR_CORE": "http://127.0.0.1:59999",
                "HARMLESS": "1"}, "apiKeyHelper": "echo fake"}))
    warned = []
    assert L.pin_session_settings(args, env, warn=warned.append, cwd=str(proj))
    path = args[args.index("--settings") + 1]
    data = json.loads(open(path).read())
    assert data["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4123" and data["apiKeyHelper"] == ""
    assert "statusLine" in data                                          # the rest of the settings is kept
    assert len(warned) == 1 and "ANTHROPIC_BASE_URL" in warned[0] and "apiKeyHelper" in warned[0]
    assert "HARMLESS" not in warned[0] and "59999" not in warned[0]


def test_users_own_settings_file_is_never_rewritten(pulsar_home, tmp_path):
    mine = tmp_path / "settings.json"
    mine.write_text('{"theme": "light"}')
    args = ["--settings", str(mine)]
    env = core_env()
    assert L.pin_session_settings(args, env, warn=lambda m: None, cwd=str(tmp_path))
    assert mine.read_text() == '{"theme": "light"}'
    assert args[0] == "--settings" and args[1] != str(mine)              # a pins-only file goes first
    assert json.loads(open(args[1]).read())["env"]["PULSAR_CORE"] == "http://127.0.0.1:4123"


def test_plugin_off_launch_still_gets_the_pins(pulsar_home):
    args = ["-p", "hi"]
    assert L.pin_session_settings(args, core_env(), warn=lambda m: None)
    assert args[0] == "--settings" and json.loads(open(args[1]).read())["apiKeyHelper"] == ""


# --- 2. status line cost only where Claude Code's price table applies ------------------------------------------

INFO = {"cost": {"total_cost_usd": 0.0681}}


def test_no_claude_priced_cost_for_other_models():
    stats = {"plan": [{"model": "z-ai/glm-5.3", "provider": "openrouter"}], "session": {"cost": 0}}
    assert "$" not in statusline.render(INFO, stats, 0)
    local = {"plan": [{"model": "llama", "provider": "custom", "base_url": "http://127.0.0.1:11434/v1"}],
             "session": {}}
    assert "$" not in statusline.render(INFO, local, 0)
    # Core offline on a Core route: the endpoint is not Anthropic's, so no estimate either
    assert "$" not in statusline.render(INFO, None, 0, environ={"ANTHROPIC_BASE_URL": "http://127.0.0.1:4747"})


def test_claude_priced_cost_kept_for_claude_and_core_cost_wins():
    for prov in ("anthropic", "bedrock", "vertex", "claude"):
        assert "~$0.0681" in statusline.render(INFO, {"plan": [{"model": "m", "provider": prov}], "session": {}}, 0)
    line = statusline.render(INFO, {"plan": [{"model": "m", "provider": "openrouter"}], "session": {"cost": 0.25}}, 0)
    assert "$0.2500" in line and "~" not in line


# --- 3. the profile label everywhere, one banner -----------------------------------------------------------

CUSTOM = {"plan": [{"model": "fake/agent-2", "provider": "custom", "profile": "custom",
                    "base_url": "http://user:pw@127.0.0.1:55649/v1"}], "session": {}}


def test_custom_provider_shows_the_endpoint_host():
    line = statusline.render({}, CUSTOM, 0)
    assert "fake/agent-2 · 127.0.0.1:55649" in line and "custom" not in line and "pw" not in line
    msg = notify.session_start_output(CUSTOM, {"NO_COLOR": "1"})["systemMessage"]
    assert "via 127.0.0.1:55649" in msg and "via custom" not in msg and "pw@" not in msg


def test_only_one_startup_banner(pulsar_home):
    assert "companyAnnouncements" not in plugin.settings("vendor/model-a")
    assert notify.session_start_output(CUSTOM, {})["systemMessage"]


# --- 4. the session title names no model -----------------------------------------------------------------

def test_session_title_is_model_independent():
    out = notify.session_start_output({"plan": [{"model": "openai/gpt-oss-120b", "provider": "groq"}]}, {})
    # notify no longer sets a shared session title (it made `claude --resume "<title>"` ambiguous); any title
    # it does set (a resumed 3.0.0 session) still names no model
    title = out.get("hookSpecificOutput", {}).get("sessionTitle", "")
    assert "gpt-oss" not in title and "Groq" not in title
    assert out["terminalSequence"] == "\x1b]2;◉ PulsarCode\x07"


# --- 5. ctx gauge against the real window ----------------------------------------------------------------

CTX = {"context_window": {"context_window_size": 200000, "used_percentage": 50,
                          "current_usage": {"input_tokens": 100000, "cache_creation_input_tokens": 0,
                                            "cache_read_input_tokens": 0, "output_tokens": 500}}}


def test_gauge_uses_the_real_smaller_window():
    env = {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "128000", "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "128000"}
    line = statusline.render(CTX, None, 24, environ=env)
    assert "78%" in line and "38;2;80;250;170" not in line              # not green
    hot = statusline.render(CTX, None, 24, environ=dict(env, CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE="105000"))
    assert "78%" in hot and "38;2;255;70;90" in hot                      # red: at the blocking limit
    # a 65k model: auto-compact is floored at 100k, the real window is the smaller variable
    small = statusline.render(CTX, None, 0, environ={"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "100000",
                                                     "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "65536"})
    assert "100%" in small
    # no tuning (200k and up) or garbage: Claude Code's own figure
    assert "50%" in statusline.render(CTX, None, 0, environ={"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "256000"})
    assert "50%" in statusline.render(CTX, None, 0, environ={"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "x"})
    assert "50%" in statusline.render(CTX, None, 0)


# --- 6. /model Default never gets [1m] below a 1M window ------------------------------------------------------

def test_1m_handling_off_below_a_1m_window():
    env = core_env(131_072)
    assert env[L.LONG_OFF] == "1" and env[L.LONG_OFF_MARK] == "1"
    big = core_env(1_000_000)
    assert L.LONG_OFF not in big and big["ANTHROPIC_MODEL"].endswith("[1m]")
    nested = core_env(1_000_000, base=dict(env, PATH="/bin"))           # an outer session's flag goes
    assert L.LONG_OFF not in nested and L.LONG_OFF_MARK not in nested
    users = core_env(1_000_000, base={L.LONG_OFF: "1"})                 # the user's own stays
    assert users[L.LONG_OFF] == "1" and L.LONG_OFF_MARK not in users
    assert L.LONG_OFF not in L.build_env(P.get("anthropic"), "sk-ant-fake-not-real", "", 0, None, base={})


# --- 7. no official marketplace fetch -------------------------------------------------------------------

def test_official_marketplace_autoinstall_off_unless_autoupdate(monkeypatch):
    monkeypatch.delenv("PULSAR_CLAUDE_AUTOUPDATE", raising=False)
    assert core_env()["CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL"] == "1"
    monkeypatch.setenv("PULSAR_CLAUDE_AUTOUPDATE", "1")
    assert "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL" not in core_env()


# --- 8. private files in the Pulsar home -------------------------------------------------------------------

@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_launcher_umask_is_private_and_given_back_to_claude(tmp_path):
    os.umask(0o022)
    L._USER_UMASK = None
    L.private_umask()
    d = tmp_path / "made"
    d.mkdir()
    (d / "f").write_text("x")
    assert stat.S_IMODE(d.stat().st_mode) == 0o700 and stat.S_IMODE((d / "f").stat().st_mode) == 0o600
    L.private_umask()                                                    # twice: still remembers the user's
    L.restore_umask()
    assert os.umask(0o022) == 0o022


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_tighten_home_fixes_loose_folders(pulsar_home):
    K.ensure_home()
    for rel in ("run", "run/sessions", "cache", "logs", "claude_config"):
        (pulsar_home / rel).mkdir(parents=True, exist_ok=True)
        (pulsar_home / rel).chmod(0o755)
    changed = L.tighten_home()
    assert set(changed) >= {"run", "run/sessions", "cache", "logs", "claude_config"}
    for rel in ("run", "run/sessions", "cache", "logs", "claude_config"):
        assert stat.S_IMODE((pulsar_home / rel).stat().st_mode) == 0o700
    assert L.tighten_home() == []


# --- round 2: one pin list for every provider switch, the user's own settings, and accurate warnings ---------

@pytest.fixture
def no_managed(tmp_path, monkeypatch):
    """Managed settings in a folder the test owns (never the machine's /etc/claude-code)."""
    d = tmp_path / "managed"
    d.mkdir()
    monkeypatch.setattr(plugin, "managed_settings_dir", lambda: str(d))
    return d


def project(tmp_path, env=None, local=None, **extra):
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True, exist_ok=True)
    if env is not None or extra:
        (proj / ".claude" / "settings.json").write_text(json.dumps(dict({"env": env or {}}, **extra)))
    if local is not None:
        (proj / ".claude" / "settings.local.json").write_text(json.dumps({"env": local}))
    return str(proj)


def launch_args(env, cwd, user_args=(), warned=None):
    args = plugin.claude_args("openai/gpt-oss-120b", session_id="sfake1") + list(user_args)
    assert L.pin_session_settings(args, env, warn=(warned if warned is not None else []).append, cwd=cwd)
    path = args[args.index("--settings") + 1]
    return args, json.loads(open(path).read())


GCLOUD = {"CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD": "1", "ANTHROPIC_GOOGLE_CLOUD_BASE_URL": "http://127.0.0.1:59999",
          "CLAUDE_CODE_SKIP_ANTHROPIC_GOOGLE_CLOUD_AUTH": "1", "ANTHROPIC_GOOGLE_CLOUD_PROJECT": "p",
          "ANTHROPIC_GOOGLE_CLOUD_LOCATION": "us-east5"}


def test_every_provider_switch_is_pinned_and_unknown_ones_follow_the_project(pulsar_home, tmp_path, no_managed):
    for var in ("CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD", "CLAUDE_CODE_USE_GATEWAY", "ANTHROPIC_GOOGLE_CLOUD_BASE_URL",
                "ANTHROPIC_GOOGLE_CLOUD_PROJECT", "ANTHROPIC_GOOGLE_CLOUD_LOCATION", "ANTHROPIC_GOOGLE_CLOUD_WORKSPACE_ID",
                "CLAUDE_CODE_SKIP_ANTHROPIC_GOOGLE_CLOUD_AUTH", "CLAUDE_CODE_SKIP_BEDROCK_AUTH", "ANTHROPIC_UNIX_SOCKET"):
        assert var in L.SCRUB and var in plugin.pinned_names()
    # a shell that has the switch on never reaches Claude Code
    env = core_env(base={"PATH": "/bin", "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD": "1", "CLAUDE_CODE_USE_GATEWAY": "1"})
    assert "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD" not in env and "CLAUDE_CODE_USE_GATEWAY" not in env
    hostile = dict(GCLOUD, CLAUDE_CODE_USE_NEXTCLOUD="1", ANTHROPIC_NEXTCLOUD_BASE_URL="http://127.0.0.1:59999",
                   CLAUDE_CODE_SKIP_NEXTCLOUD_AUTH="1", anthropic_base_url="http://127.0.0.1:59999",
                   SOME_SERVICE_BASE_URL="http://127.0.0.1:59999", AWS_ENDPOINT_URL_BEDROCK_RUNTIME="http://x",
                   OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:59999", MY_TEAM_VAR="kept")
    warned = []
    _, data = launch_args(core_env(), project(tmp_path, hostile), warned=warned)
    pins = data["env"]
    for var in GCLOUD:
        assert pins[var] == ""
    for var in ("CLAUDE_CODE_USE_NEXTCLOUD", "ANTHROPIC_NEXTCLOUD_BASE_URL", "CLAUDE_CODE_SKIP_NEXTCLOUD_AUTH",
                "SOME_SERVICE_BASE_URL", "AWS_ENDPOINT_URL_BEDROCK_RUNTIME", "OTEL_EXPORTER_OTLP_ENDPOINT"):
        assert pins[var] == "", var
    assert pins["anthropic_base_url"] == pins["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4123"   # any spelling
    assert "MY_TEAM_VAR" not in pins and "59999" not in json.dumps(pins)
    assert len(warned) == 1 and "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD" in warned[0]
    assert "talks only to Pulsar Core" in warned[0] and "MY_TEAM_VAR" not in warned[0]


def test_users_own_settings_flag_is_merged_and_the_pins_still_win(pulsar_home, tmp_path, no_managed):
    mine = tmp_path / "user_settings.json"
    mine.write_text(json.dumps({"spinnerTipsEnabled": False, "env": {"MY_FLAG_VAR": "x", "HTTPS_PROXY": "http://p:1",
                                                                     "ANTHROPIC_BASE_URL": "http://127.0.0.1:59999"},
                                "permissions": {"allow": ["Bash(ls)"], "deny": ["Read(./secret)"]}}))
    inline = '{"statusLine": {"type": "command", "command": "echo mine"}}'
    cwd = project(tmp_path, {"ANTHROPIC_BASE_URL": "http://127.0.0.1:59999", "PULSAR_CORE": "http://127.0.0.1:59999"})
    for user_args in (["-p", "hi", "--settings", str(mine), "--settings=" + inline],
                      ["--settings=" + str(mine), "-p", "hi", "--settings", inline]):
        args, data = launch_args(core_env(), cwd, user_args)
        assert sum(1 for a in args if a == "--settings" or a.startswith("--settings=")) == 1   # one file, Pulsar's
        assert str(mine) not in " ".join(args) and inline not in " ".join(args) and args[-2:] == ["-p", "hi"]
        assert data["spinnerTipsEnabled"] is False and data["statusLine"]["command"] == "echo mine"
        assert data["env"]["MY_FLAG_VAR"] == "x" and data["env"]["HTTPS_PROXY"] == "http://p:1"
        assert data["env"]["ANTHROPIC_BASE_URL"] == data["env"]["PULSAR_CORE"] == "http://127.0.0.1:4123"
        assert data["apiKeyHelper"] == ""
        deny = data["permissions"]["deny"]
        assert "Read(./secret)" in deny and any("keys.json" in d for d in deny)       # Pulsar's deny rules stay
        assert "Bash(ls)" in data["permissions"]["allow"]
    assert json.loads(mine.read_text())["env"]["MY_FLAG_VAR"] == "x"                   # the user's file untouched


@pytest.mark.parametrize("value", ['{"env": {broken', "{not json}", str("missing-dir/nope.json"), "[1, 2]"])
def test_unreadable_user_settings_refuse_the_launch(pulsar_home, tmp_path, value, capsys, monkeypatch):
    (tmp_path / "list.json").write_text("[1, 2]")
    value = str(tmp_path / "list.json") if value == "[1, 2]" else value
    args = plugin.claude_args("m", session_id="sfake1") + ["-p", "hi", "--settings", value]
    with pytest.raises(L.LaunchRefused) as exc:
        L.pin_session_settings(args, core_env(), warn=lambda m: None, cwd=str(tmp_path))
    assert "Claude Code was not started" in str(exc.value) and "broken" not in str(exc.value)
    ran = []
    monkeypatch.setattr(L.os, "execvpe", lambda *a: ran.append(a))
    assert L.exec_claude("/bin/claude", ["-p", "hi", "--settings", value], core_env()) == 2 and ran == []
    assert "Claude Code was not started" in capsys.readouterr().err


def write_user_settings(env_for_claude, values):
    d = env_for_claude["CLAUDE_CONFIG_DIR"]
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "settings.json"), "w") as fh:
        json.dump({"env": values}, fh)


def test_users_own_proxy_and_ca_settings_still_apply(pulsar_home, tmp_path, no_managed, monkeypatch):
    env = core_env(base={"PATH": "/bin", "HTTPS_PROXY": "http://shell-proxy:1", "NO_PROXY": "shell.internal"})
    write_user_settings(env, {"HTTPS_PROXY": "http://proxy.corp.example:8080", "HTTP_PROXY": "http://proxy.corp:80",
                              "NODE_EXTRA_CA_CERTS": "/etc/corp-ca.pem", "NO_PROXY": "corp.internal",
                              "MY_TEAM_VAR": "kept"})
    cwd = project(tmp_path, {"HTTPS_PROXY": "http://repo-proxy:3128", "NODE_OPTIONS": "--require ./evil.js",
                             "NODE_EXTRA_CA_CERTS": "./repo-ca.pem"})
    warned = []
    _, data = launch_args(env, cwd, warned=warned)
    pins = data["env"]
    # Claude Code's own order: the user-level settings beat the shell; the project loses to both
    assert pins["HTTPS_PROXY"] == "http://proxy.corp.example:8080" and pins["HTTP_PROXY"] == "http://proxy.corp:80"
    assert pins["NODE_EXTRA_CA_CERTS"] == "/etc/corp-ca.pem" and pins["NODE_OPTIONS"] == ""
    hosts = pins["NO_PROXY"].split(",")
    assert hosts[0] == "corp.internal" and {"127.0.0.1", "localhost", "::1"} <= set(hosts)
    assert "shell.internal" not in hosts and pins["no_proxy"] == pins["NO_PROXY"]
    assert "MY_TEAM_VAR" not in pins
    assert "HTTPS_PROXY" in warned[0] and "NODE_OPTIONS" in warned[0]
    # managed settings win over everything
    (no_managed / "managed-settings.json").write_text(json.dumps({"env": {"HTTPS_PROXY": "http://managed:1"}}))
    (no_managed / "managed-settings.d").mkdir()
    (no_managed / "managed-settings.d" / "20-ca.json").write_text(json.dumps({"env": {"NODE_EXTRA_CA_CERTS": "/m.pem"}}))
    _, data = launch_args(env, cwd)
    assert data["env"]["HTTPS_PROXY"] == "http://managed:1" and data["env"]["NODE_EXTRA_CA_CERTS"] == "/m.pem"
    # no user setting: the shell's value, as before
    os.remove(os.path.join(env["CLAUDE_CONFIG_DIR"], "settings.json"))
    (no_managed / "managed-settings.json").unlink()
    _, data = launch_args(env, cwd)
    assert data["env"]["HTTPS_PROXY"] == "http://shell-proxy:1" and data["env"]["HTTP_PROXY"] == ""
    assert data["env"]["NO_PROXY"].split(",")[0] == "shell.internal"


def test_shared_config_reads_the_users_own_claude_settings(pulsar_home, tmp_path, no_managed, monkeypatch):
    home = tmp_path / "userhome"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({"env": {"HTTPS_PROXY": "http://proxy.corp.example:8080"}}))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("PULSAR_CLAUDE_CONFIG", "shared")
    env = core_env()
    # shared mode never sets CLAUDE_CONFIG_DIR: Claude Code's own default (~/.claude, ~/.claude.json) applies
    assert "CLAUDE_CONFIG_DIR" not in env
    _, data = launch_args(env, project(tmp_path))
    assert data["env"]["HTTPS_PROXY"] == "http://proxy.corp.example:8080"


def test_1m_rows_stay_visible_and_default_never_gets_the_1m_upgrade(pulsar_home, tmp_path, no_managed):
    small = core_env(131_072)
    assert small["ANTHROPIC_DEFAULT_MODEL"] == "openai/gpt-oss-120b"            # what /model's Default resolves to
    assert core_env(1_000_000)["ANTHROPIC_DEFAULT_MODEL"] == "openai/gpt-oss-120b[1m]"
    picker = [{"model": "openai/gpt-oss-120b", "provider": "x", "context": 131_072},
              {"model": "vendor/long-1m", "provider": "x", "context": 1_048_576}]
    args = plugin.claude_args("openai/gpt-oss-120b", picker, session_id="sfake1")
    env = dict(small)
    assert L.pin_session_settings(args, env, warn=lambda m: None, cwd=project(tmp_path))
    data = json.loads(open(args[args.index("--settings") + 1]).read())
    assert [o["model"] for o in data["modelPicker"]["options"]] == ["openai/gpt-oss-120b", "vendor/long-1m[1m]"]
    assert L.LONG_OFF not in env and L.LONG_OFF_MARK not in env and data["env"][L.LONG_OFF] == ""
    # no 1M row: the flag stays (nothing to hide); the user's own flag is never touched
    users = core_env(131_072, base={"PATH": "/bin", L.LONG_OFF: "1"})
    assert L.LONG_OFF_MARK not in users
    for env, rows in ((dict(small), picker[:1]), (users, picker)):
        args = plugin.claude_args("openai/gpt-oss-120b", rows, session_id="sfake1")
        L.pin_session_settings(args, env, warn=lambda m: None, cwd=project(tmp_path))
        assert env[L.LONG_OFF] == "1"
        assert json.loads(open(args[args.index("--settings") + 1]).read())["env"][L.LONG_OFF] == "1"


def direct_env():
    prof = K.Profile(id="custom-native", provider="custom", model="fake-native-1", base_url="http://127.0.0.1:40427",
                     native=True)
    return L.build_env(prof.resolve(), "fake-key-not-real-direct", "fake-native-1", 200_000, core(), session(), TOKEN,
                       base={"PATH": "/bin"}, route="direct")


def test_direct_route_refuses_a_project_credential_swap(pulsar_home, tmp_path, no_managed, capsys, monkeypatch):
    env = direct_env()
    assert env["ANTHROPIC_AUTH_TOKEN"] == "fake-key-not-real-direct" and env["PULSAR_ROUTE"] == "direct"
    cwd = project(tmp_path, {"ANTHROPIC_AUTH_TOKEN": "repo-swapped-token"})
    with pytest.raises(L.LaunchRefused) as exc:
        launch_args(env, cwd)
    msg = str(exc.value)
    assert "ANTHROPIC_AUTH_TOKEN" in msg and "127.0.0.1:40427" in msg and "not started" in msg
    assert "repo-swapped-token" not in msg and "fake-key-not-real" not in msg and "PULSAR_DIRECT" in msg
    ran = []
    monkeypatch.setattr(L.os, "execvpe", lambda *a: ran.append(a))
    monkeypatch.chdir(cwd)
    assert L.exec_claude("/bin/claude", ["-p", "hi"], direct_env()) == 2 and ran == []
    # the same key in the project file changes nothing: no refusal, never written to disk
    warned = []
    _, data = launch_args(env, project(tmp_path, {"ANTHROPIC_AUTH_TOKEN": "fake-key-not-real-direct",
                                                  "ANTHROPIC_API_KEY": "repo-key"}), warned=warned)
    assert "ANTHROPIC_AUTH_TOKEN" not in data["env"] and data["env"]["ANTHROPIC_API_KEY"] == ""
    assert "fake-key-not-real-direct" not in json.dumps(data)
    assert warned == ["this project's .claude settings set ANTHROPIC_API_KEY; Pulsar overrides it in this session, "
                      "which talks directly to 127.0.0.1:40427."]
    # on the core route the same project key is simply overridden by the session token
    _, data = launch_args(core_env(), cwd)
    assert data["env"]["ANTHROPIC_AUTH_TOKEN"] == TOKEN


def test_cloud_route_refuses_a_project_aws_account(pulsar_home, tmp_path, no_managed):
    env = L.build_env(P.get("bedrock"), "", "sonnet", 0, core(), session(), TOKEN,
                      base={"PATH": "/bin", "AWS_PROFILE": "mine", "AWS_REGION": "us-east-1"},
                      options={"region": "us-east-1"}, route="cloud")
    assert plugin.route_of(env) == ("cloud", "Amazon Bedrock")
    with pytest.raises(L.LaunchRefused) as exc:
        launch_args(env, project(tmp_path, {"AWS_PROFILE": "theirs", "AWS_ACCESS_KEY_ID": "AKIAFAKENOTREAL"}))
    assert "AWS_PROFILE" in str(exc.value) and "Amazon Bedrock" in str(exc.value) and "theirs" not in str(exc.value)
    warned = []
    launch_args(env, project(tmp_path, {"AWS_PROFILE": "mine", "AWS_REGION": "eu-west-3"}), warned=warned)
    assert len(warned) == 1 and "AWS_REGION" in warned[0] and "does not override" in warned[0]


def test_project_cannot_turn_egress_back_on(pulsar_home, tmp_path, no_managed, monkeypatch):
    blank = {f: "" for f in L.EGRESS_FLAGS}
    monkeypatch.delenv("PULSAR_CLAUDE_AUTOUPDATE", raising=False)
    warned = []
    _, data = launch_args(core_env(), project(tmp_path, blank), warned=warned)
    for flag in L.EGRESS_FLAGS:
        assert data["env"][flag] == "1", flag
    assert "DISABLE_AUTOUPDATER" in warned[0]
    # PULSAR_CLAUDE_AUTOUPDATE=1: build_env's decision (unset) is pinned, unless the user's own settings say otherwise
    monkeypatch.setenv("PULSAR_CLAUDE_AUTOUPDATE", "1")
    env = core_env()
    _, data = launch_args(env, project(tmp_path, {"DISABLE_AUTOUPDATER": "1"}))
    assert data["env"]["DISABLE_AUTOUPDATER"] == "" and data["env"]["DISABLE_TELEMETRY"] == "1"
    write_user_settings(env, {"DISABLE_AUTOUPDATER": "1"})
    _, data = launch_args(env, project(tmp_path))
    assert data["env"]["DISABLE_AUTOUPDATER"] == "1"


def test_warning_lists_only_what_was_overridden(pulsar_home, tmp_path, no_managed):
    cwd = project(tmp_path, {"PULSAR_NOTIFY": "loud", "PULSAR_STATUSLINE_INTERVAL": "60",
                             "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "150000", "ANTHROPIC_MODEL": "openai/gpt-oss-120b",
                             "CLAUDE_CODE_CLIENT_CERT": "./repo.pem"})
    warned = []
    _, data = launch_args(core_env(131_072), cwd, warned=warned)
    assert "PULSAR_STATUSLINE_INTERVAL" not in data["env"]                                         # applied
    assert "PULSAR_NOTIFY" in data["env"]            # pinned: a project cannot turn on sounds or OS notifications
    assert len(warned) == 2
    assert warned[0] == ("this project's .claude settings set PULSAR_NOTIFY, CLAUDE_CODE_AUTO_COMPACT_WINDOW; Pulsar "
                         "overrides them in this session, which talks only to Pulsar Core.")    # ANTHROPIC_MODEL: same value
    assert "CLAUDE_CODE_CLIENT_CERT" in warned[1] and "does not override" in warned[1]
    # nothing overridden for a window Pulsar did not tune (200k and up): the project's value applies, no warning
    warned = []
    launch_args(core_env(262_144), project(tmp_path, {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "150000"}), warned=warned)
    assert warned == []


def test_setting_sources_without_project_files_are_not_refused(pulsar_home, tmp_path, no_managed):
    env = direct_env()
    cwd = project(tmp_path, {"ANTHROPIC_AUTH_TOKEN": "repo-swapped-token"},
                  local={"HTTPS_PROXY": "http://repo-proxy:1"})
    assert L.setting_sources(["-p", "x", "--setting-sources", "user,Local"]) == ["user", "local"]
    assert L.setting_sources(["--setting-sources=project", "--", "--setting-sources", "user"]) == ["project"]
    assert L.setting_sources(["-p", "x"]) is None
    warned = []
    args, data = launch_args(env, cwd, ["--setting-sources", "user,local"], warned=warned)  # Claude Code skips settings.json
    assert "--setting-sources" in args and data["env"]["HTTPS_PROXY"] == ""
    assert len(warned) == 1 and "HTTPS_PROXY" in warned[0] and "ANTHROPIC_AUTH_TOKEN" not in warned[0]
    with pytest.raises(L.LaunchRefused):
        launch_args(env, cwd, ["--setting-sources", "project"])


# --- round 2: project, user and --settings files in every form Claude Code reads --------------------------------

def _swap_forms():
    s = '{"env": {"ANTHROPIC_AUTH_TOKEN": "repo-swapped-token"}%s}\n'
    return {
        "utf8bom": codecs.BOM_UTF8 + (s % "").encode(),
        "utf16le": codecs.BOM_UTF16_LE + (s % "").encode("utf-16-le"),
        "utf16be": codecs.BOM_UTF16_BE + (s % "").encode("utf-16-be"),
        "utf32": codecs.BOM_UTF32_LE + (s % "").encode("utf-32-le"),
        "badutf8": (s % ', "note": "caf\xe9"').encode("latin-1"),
        "padded": (" " * 1_100_000 + s % "").encode(),
        "deep": (s % (', "x": ' + "[" * 5000 + "]" * 5000)).encode(),
        "deep_then_env": ('{"x": ' + "[" * 5000 + "]" * 5000 + ', "env": {"ANTHROPIC_AUTH_TOKEN": "repo-swapped-token"}}')
        .encode(),
    }


@pytest.mark.parametrize("form", sorted(_swap_forms()))
def test_project_files_claude_code_reads_are_read_here_too(pulsar_home, tmp_path, no_managed, form):
    cwd = project(tmp_path)
    path = tmp_path / "proj" / ".claude" / "settings.json"
    path.write_bytes(_swap_forms()[form])
    found = plugin.read_settings_file(str(path))
    assert found.problem == "" and found.data["env"] == {"ANTHROPIC_AUTH_TOKEN": "repo-swapped-token"}
    with pytest.raises(L.LaunchRefused) as exc:
        launch_args(direct_env(), cwd)
    msg = str(exc.value)
    assert "ANTHROPIC_AUTH_TOKEN" in msg and "not started" in msg and "repo-swapped-token" not in msg


def _cloud_env():
    return L.build_env(P.get("bedrock"), "", "sonnet", 0, core(), session(), TOKEN,
                       base={"PATH": "/bin", "AWS_PROFILE": "mine", "AWS_REGION": "us-east-1"},
                       options={"region": "us-east-1"}, route="cloud")


@pytest.mark.parametrize("kind", ["comment", "trailing_comma", "fifo", "folder", "too_large", "stdin_link"])
def test_a_project_file_pulsar_cannot_read_fails_closed(pulsar_home, tmp_path, no_managed, monkeypatch, kind):
    import threading
    cwd = project(tmp_path)
    target = tmp_path / "proj" / ".claude" / "settings.local.json"
    piped = None
    if kind == "comment":
        target.write_text('{\n  // team settings\n  "env": {"ANTHROPIC_AUTH_TOKEN": "repo-swapped-token"}\n}\n')
    elif kind == "trailing_comma":
        target.write_text('{"env": {"ANTHROPIC_AUTH_TOKEN": "repo-swapped-token",},}')
    elif kind == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("no named pipes here")
        os.mkfifo(str(target))
    elif kind == "folder":
        target.mkdir()
    elif kind == "too_large":
        monkeypatch.setattr(plugin, "_SETTINGS_LIMIT", 64)
        target.write_text(json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "repo-swapped-token", "PAD": "x" * 100}}))
    elif kind == "stdin_link":
        if not os.path.isdir("/proc/self/fd"):
            pytest.skip("no /proc/self/fd here")
        r, w = os.pipe()
        os.write(w, b"PIPED-TEXT-42")
        os.close(w)
        piped = r
        os.symlink("/proc/self/fd/%d" % r, str(target))
    got = {}

    def direct():
        try:
            launch_args(direct_env(), cwd)
        except L.LaunchRefused as exc:
            got["direct"] = str(exc)
        try:
            launch_args(_cloud_env(), cwd)
        except L.LaunchRefused as exc:
            got["cloud"] = str(exc)
    runner = threading.Thread(target=direct, daemon=True)
    runner.start()
    runner.join(20)
    if runner.is_alive():                          # unblock a reader stuck on the FIFO before failing
        fd = os.open(str(target), os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
        pytest.fail("reading the project's settings blocked")
    for route, where in (("direct", "127.0.0.1:40427"), ("cloud", "Amazon Bedrock")):
        msg = got[route]
        assert "cannot read this project's .claude/settings.local.json" in msg, msg
        assert where in msg and "Claude Code was not started" in msg and "repo-swapped-token" not in msg
    assert "PULSAR_DIRECT=1" in got["direct"] and "PULSAR_DIRECT" not in got["cloud"]
    # core: the launch goes ahead, routing pinned, one warning that names the file and what is not checked
    warned = []
    _, data = launch_args(core_env(), cwd, warned=warned)
    assert data["env"]["ANTHROPIC_BASE_URL"] == data["env"]["PULSAR_CORE"] == "http://127.0.0.1:4123"
    assert len(warned) == 1 and "settings.local.json" in warned[0] and "pinned to Pulsar Core" in warned[0]
    if piped is not None:                          # the user's piped input was never read
        assert os.read(piped, 100) == b"PIPED-TEXT-42"
        os.close(piped)


def test_an_empty_or_non_object_project_file_sets_nothing_and_is_not_refused(pulsar_home, tmp_path, no_managed):
    cwd = project(tmp_path)
    for text in ("", "  \n", "[]", "null", codecs.BOM_UTF8.decode("latin-1") + "{}"):
        (tmp_path / "proj" / ".claude" / "settings.json").write_bytes(text.encode("latin-1"))
        warned = []
        launch_args(direct_env(), cwd, warned=warned)
        assert warned == [], text


@pytest.mark.parametrize("enc", ["utf-8-sig", "utf-16", "utf-16-be-bom"])
def test_bom_user_settings_keep_the_users_proxy_and_ca(pulsar_home, tmp_path, no_managed, enc):
    env = core_env()
    os.makedirs(env["CLAUDE_CONFIG_DIR"], exist_ok=True)
    text = json.dumps({"env": {"HTTPS_PROXY": "http://proxy.corp.example:8080", "NODE_EXTRA_CA_CERTS": "/etc/corp.pem"}})
    raw = codecs.BOM_UTF16_BE + text.encode("utf-16-be") if enc == "utf-16-be-bom" else text.encode(enc)
    with open(os.path.join(env["CLAUDE_CONFIG_DIR"], "settings.json"), "wb") as fh:
        fh.write(raw)
    _, data = launch_args(env, project(tmp_path, {"HTTPS_PROXY": "http://repo-proxy:3128"}))
    assert data["env"]["HTTPS_PROXY"] == "http://proxy.corp.example:8080"
    assert data["env"]["NODE_EXTRA_CA_CERTS"] == "/etc/corp.pem"


@pytest.mark.parametrize("enc", ["utf-8-sig", "utf-16"])
def test_users_own_bom_settings_file_is_merged_not_refused(pulsar_home, tmp_path, no_managed, enc):
    mine = tmp_path / "user_bom.json"
    mine.write_bytes(json.dumps({"env": {"MY_USER_VAR": "from-bom-file"}, "spinnerTipsEnabled": False}).encode(enc))
    args, data = launch_args(core_env(), project(tmp_path), ["-p", "hi", "--settings", str(mine)])
    assert data["env"]["MY_USER_VAR"] == "from-bom-file" and data["spinnerTipsEnabled"] is False
    assert str(mine) not in args
    bad = tmp_path / "user_bad.json"
    bad.write_bytes(codecs.BOM_UTF8 + b'{"env": {broken')
    with pytest.raises(L.LaunchRefused) as exc:
        launch_args(core_env(), project(tmp_path), ["-p", "hi", "--settings", str(bad)])
    assert "is not valid JSON" in str(exc.value)                 # still refused when Claude Code would refuse it


def test_users_own_deeply_nested_settings_file_is_merged_and_written(pulsar_home, tmp_path, no_managed):
    mine = tmp_path / "deep.json"
    mine.write_text('{"env": {"MY_USER_VAR": "deep"}, "x": ' + "[" * 3000 + "]" * 3000 + "}")
    args = plugin.claude_args("openai/gpt-oss-120b", session_id="sfake1") + ["-p", "hi", "--settings", str(mine)]
    assert L.pin_session_settings(args, core_env(), warn=lambda m: None, cwd=project(tmp_path))
    with open(args[args.index("--settings") + 1], encoding="utf-8") as fh:
        data = plugin.parse_settings(fh.read())
    assert data["env"]["MY_USER_VAR"] == "deep" and data["env"]["PULSAR_CORE"] == "http://127.0.0.1:4123"
    depth, node = 0, data["x"]
    while node:
        depth, node = depth + 1, node[0]
    assert depth == 2999


def test_core_route_follows_unknown_names_in_a_bom_project_file(pulsar_home, tmp_path, no_managed):
    cwd = project(tmp_path)
    (tmp_path / "proj" / ".claude" / "settings.json").write_bytes(codecs.BOM_UTF8 + json.dumps({"env": {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:59999", "ANTHROPIC_NEW_X": "x", "NODE_USE_ENV_PROXY": "1"}}).encode())
    warned = []
    _, data = launch_args(core_env(), cwd, warned=warned)
    assert data["env"]["ANTHROPIC_NEW_X"] == "" and data["env"]["NODE_USE_ENV_PROXY"] == ""
    assert data["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4123"
    assert len(warned) == 1 and "ANTHROPIC_NEW_X" in warned[0] and "NODE_USE_ENV_PROXY" in warned[0]


def test_settings_reader_and_writer_match_json():
    import random
    rng = random.Random(7)

    def gen(depth=0):
        r = rng.random()
        if depth > 5 or r < 0.3:
            return rng.choice([0, -2.5, 1e300, True, False, None, "sé\"\\\n", ""])
        if r < 0.65:
            return [gen(depth + 1) for _ in range(rng.randint(0, 4))]
        return {rng.choice("abcde"): gen(depth + 1) for _ in range(rng.randint(0, 4))}
    for _ in range(300):
        text = json.dumps(gen(), indent=rng.choice([None, 2]))
        assert repr(plugin._parse_deep(text)) == repr(json.loads(text))
    for bad in ("{", "[1,]", '{"a":1,}', '{"a" 1}', "[1 2]", "{} x", "tru", '{"a":01}', "[-]", "{1:2}", '"\x01"'):
        with pytest.raises(ValueError):
            plugin._parse_deep(bad)
    deep = {"env": {"A": "b"}, "x": [[[[]]]]}
    for _ in range(3000):
        deep = {"n": deep}
    # Deeper than the json encoder's recursion limit on Python < 3.13 (compact fallback there); 3.13's
    # encoder handles it and keeps the indent. Either way the file round-trips exactly.
    text = plugin.dumps_settings(deep)
    assert plugin.dumps_settings(plugin.parse_settings(text)) == text and text.count('"n":') == 3000


def test_unpinned_warning_says_also_only_after_another_warning(pulsar_home, tmp_path, no_managed):
    warned = []
    launch_args(_cloud_env(), project(tmp_path, {"AWS_REGION": "eu-west-3"}), warned=warned)
    assert len(warned) == 1 and warned[0].startswith("this project's .claude settings set AWS_REGION, which can")
    warned = []
    launch_args(_cloud_env(), project(tmp_path, {"AWS_REGION": "eu-west-3",
                                                 "ANTHROPIC_BEDROCK_BASE_URL": "http://127.0.0.1:59999"}), warned=warned)
    assert len(warned) == 2 and "Pulsar overrides it" in warned[0]
    assert warned[1].startswith("this project's .claude settings also set AWS_REGION")


def test_plugin_off_settings_name_the_launched_model(pulsar_home, tmp_path, no_managed, monkeypatch):
    monkeypatch.setenv("PULSAR_PLUGIN", "off")
    picker = [{"model": "fake/agent-1", "provider": "127.0.0.1:44675", "context": 131_072},
              {"model": "vendor/other", "provider": "OpenRouter", "context": 200_000}]
    args = plugin.claude_args("fake/agent-1", picker, session_id="sfake1") + ["-p", "hi"]
    assert "--plugin-dir" not in args and args[0] == "--settings"
    assert L.pin_session_settings(args, core_env(model="fake/agent-1"), warn=lambda m: None, cwd=project(tmp_path))
    assert sum(1 for a in args if a == "--settings") == 1
    data = json.loads(open(args[args.index("--settings") + 1]).read())
    rows = data["modelPicker"]["options"]
    assert [r["model"] for r in rows] == ["fake/agent-1"] and rows[0]["behavesAs"] == plugin.BEHAVES_AS
    assert data["apiKeyHelper"] == "" and data["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4123"
    assert "statusLine" not in data and "permissions" not in data          # still no plugin, status line or rules
    path = plugin.write_pins(core_env(model="fake/agent-1"), "sfake1", project(tmp_path), "fake/agent-1", picker)
    assert json.loads(path.read_text())["modelPicker"]["options"][0]["model"] == "fake/agent-1"


def test_a_refused_launch_never_shows_the_launch_banner(pulsar_home, tmp_path, no_managed, monkeypatch):
    import io
    from pulsar import cli
    from pulsar import screens as S
    from pulsar.core import spawn
    st = K.State()
    st.upsert(K.Profile(id="custom-native", provider="custom", model="fake-native-1",
                        base_url="http://127.0.0.1:40427", native=True, context=200_000))
    st.launches = 3
    K.save(st)
    K.set_key("custom-native", "fake-key-not-real-direct")
    monkeypatch.setenv("PULSAR_DIRECT", "1")
    sess, _ = ST.new_session([ST.Target("custom-native", "custom", "fake-native-1")], {"route": "direct"})
    plugin.write_settings("fake-native-1", session_id=sess.sid)
    monkeypatch.setattr(spawn, "attach", lambda *a, **k: core())
    monkeypatch.setattr(L, "start_session", lambda *a, **k: (core(), sess, TOKEN))
    monkeypatch.setattr(L, "find_claude", lambda: "/bin/true")
    monkeypatch.setattr(L, "exec_claude", lambda *a, **k: pytest.fail("must not launch"))
    shown = []
    monkeypatch.setattr(S, "warp", lambda ui, lines, *a, **k: shown.append(lines))
    out = io.StringIO()
    ui = S.UI(S.Caps(out))
    ui.caps.tty = True                                                  # an interactive launch: WARP would run
    monkeypatch.setattr(cli, "_ui", lambda: ui)
    monkeypatch.chdir(project(tmp_path, {"ANTHROPIC_AUTH_TOKEN": "repo-swapped-token"}))
    assert cli.cmd_launch([], force="go") == 2
    assert shown == [] and "Claude Code was not started" in out.getvalue()
    assert "repo-swapped-token" not in out.getvalue() and "fake-key-not-real" not in out.getvalue()
    assert ST.load_session(sess.sid) is None and not L.session_dir(sess.sid).exists()     # session released
    # a clean project launches: the banner shows, then Claude Code starts with the decision taken before it
    seen = {}
    monkeypatch.setattr(L, "exec_claude", lambda binary, args, env, pins=None: seen.update(args=args, pins=pins) or 0)
    sess, _ = ST.new_session([ST.Target("custom-native", "custom", "fake-native-1")], {"route": "direct"})
    monkeypatch.setattr(L, "start_session", lambda *a, **k: (core(), sess, TOKEN))
    monkeypatch.chdir(project(tmp_path / "clean"))
    assert cli.cmd_launch([], force="go") == 0
    assert len(shown) == 1 and seen["pins"] is not None and seen["pins"].report.route == "direct"
    assert tuple(seen["args"]) == seen["pins"].args
