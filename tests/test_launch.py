# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
import io
import json
import os

import pytest

from pulsar import cli
from pulsar import config as K
from pulsar import launch as L
from pulsar import providers as P


def core():
    from pulsar.core import state as ST
    return ST.CoreInfo(port=4123, pid=1, version="x", admin_token="pla_admin")


@pytest.fixture(autouse=True)
def no_real_core(monkeypatch):
    """No test in this file may start (or find) a real Pulsar Core: spawning one would outlive pytest."""
    from pulsar.core import spawn
    spawned = []

    def refuse(*a, **kw):
        spawned.append(1)
        raise RuntimeError("tests must not spawn Pulsar Core")
    monkeypatch.setattr(spawn, "ensure_core", refuse)
    monkeypatch.setattr(spawn, "attach", lambda *a, **kw: None)
    for var in ("PULSAR_SESSION", "CLAUDECODE", "PULSAR_SESSION_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    yield spawned


def test_env_through_core_scrubs_and_points_local():
    from pulsar.core import state as ST
    base = {"ANTHROPIC_API_KEY": "real", "CLAUDE_CODE_OAUTH_TOKEN": "oauth", "CLAUDECODE": "1",
            "HTTPS_PROXY": "http://corp:3128", "NO_PROXY": "corp.local", "PATH": "/bin", "PULSAR_SESSION": "old"}
    sess = ST.Session(sid="s1", token_sha256="h", plan=[])
    env = L.build_env(P.get("nvidia"), "nvapi-x", "moonshotai/kimi-k2.6", 256000, core(), sess, "pls_tok", base=base)
    assert "ANTHROPIC_API_KEY" not in env and "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert env["CLAUDECODE"] == "1"     # an outer session's marker stays: Claude Code's nested-session guard applies
    assert env["ANTHROPIC_BASE_URL"] == env["PULSAR_CORE"] == "http://127.0.0.1:4123"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "pls_tok" and env["PULSAR_SESSION"] == "s1"
    assert "nvapi-x" not in json.dumps(env) and "pla_admin" not in json.dumps(env)  # keys stay in Core
    assert env["ANTHROPIC_MODEL"] == env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "moonshotai/kimi-k2.6"
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "256000"
    assert env["NO_PROXY"].split(",")[:2] == ["corp.local", "127.0.0.1"]
    assert env["DISABLE_TELEMETRY"] == "1" and env["HTTPS_PROXY"] == "http://corp:3128"


def test_env_anthropic_direct():
    env = L.build_env(P.get("anthropic"), "sk-ant-x", "", 0, None, base={"ANTHROPIC_BASE_URL": "x"})
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-x"
    assert "ANTHROPIC_BASE_URL" not in env and "ANTHROPIC_MODEL" not in env
    assert "CLAUDE_CODE_MAX_CONTEXT_TOKENS" not in env


def test_env_vendor_direct():
    env = L.build_env(P.get("deepseek"), "sk-ds", "deepseek-chat", 0, None, base={})
    assert env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-ds"
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == str(L.DEFAULT_CONTEXT)


def test_seed_config_and_key_approval(tmp_path):
    d = tmp_path / "cc"
    L.seed_claude_config(d)
    data = json.loads((d / ".claude.json").read_text())
    assert data["hasCompletedOnboarding"] is True
    L.seed_claude_config(d, approve_key="sk-ant-" + "x" * 40)
    data = json.loads((d / ".claude.json").read_text())
    assert data["customApiKeyResponses"]["approved"] == ["x" * 20]
    (d / ".claude.json").write_text('{"hasCompletedOnboarding": true, "theme": "light", "keep": 1}')
    L.seed_claude_config(d)
    assert json.loads((d / ".claude.json").read_text())["theme"] == "light"


NOTICE = "autoModeClassifierBillingNoticeAcknowledgedAt"


def test_billing_notice_is_dismissed_only_when_asked(monkeypatch, tmp_path):
    d = tmp_path / "cc"
    L.seed_claude_config(d)
    data = json.loads((d / ".claude.json").read_text())
    assert data["hasCompletedOnboarding"] is True and NOTICE not in data       # seeding alone never dismisses it
    L.acknowledge_gateway_notice(d)
    first = json.loads((d / ".claude.json").read_text())[NOTICE]
    assert first > 0 and json.loads((d / ".claude.json").read_text())["hasCompletedOnboarding"] is True
    L.acknowledge_gateway_notice(d)
    assert json.loads((d / ".claude.json").read_text())[NOTICE] == first      # renewed only after 6 h
    monkeypatch.setenv("PULSAR_CLAUDE_CONFIG", "shared")                      # your own profile: never written
    mine = tmp_path / "mine"
    mine.mkdir()
    L.acknowledge_gateway_notice(mine)
    assert not (mine / ".claude.json").exists()


def test_legacy_commands_cleaned(tmp_path):
    cmds = tmp_path / "commands"
    cmds.mkdir()
    (cmds / "sonar.md").write_text("old\nManaged by pulsarcode.\n")
    (cmds / "pulsar.md").write_text("v2\nManaged by pulsarcode.\n")
    (cmds / "pick.md").write_text("user owned")
    L.install_commands(tmp_path)
    assert not (cmds / "sonar.md").exists() and not (cmds / "pulsar.md").exists()
    assert (cmds / "pick.md").read_text() == "user owned"


# What 2.0.0 wrote into a shared ~/.claude/commands (the only release that wrote into your own profile).
V2_PULSAR_MD = """---
description: Pulsar Code status, active model and the models you can switch to.
allowed-tools: Bash(pulsarcode:*)
---

Run and show the output:

```bash
pulsarcode status --brief && pulsarcode models --top 25
```

Switch live for this session with the built-in `/model <id>`. Make it stick for future launches with `/pulsar-model <id>`. To change provider or key, quit and run `pulsarcode key`.

Managed by pulsarcode.
"""


def test_shared_config_removes_only_untouched_files_pulsar_wrote(monkeypatch, tmp_path):
    monkeypatch.setenv("PULSAR_CLAUDE_CONFIG", "shared")
    cmds = tmp_path / "commands"
    cmds.mkdir()
    (cmds / "pulsar.md").write_text(V2_PULSAR_MD)
    edited = "my own /model\nManaged by pulsarcode.\n"
    (cmds / "model.md").write_text(edited)
    (cmds / "pulsar-model.md").write_text(V2_PULSAR_MD.replace("status", "my status"))
    kept = L.install_commands(tmp_path)
    assert not (cmds / "pulsar.md").exists()                     # exactly what 2.0.0 shipped: ours to remove
    assert (cmds / "model.md").read_text() == edited             # your own file stays, whatever it says
    assert (cmds / "pulsar-model.md").is_file()
    assert sorted(kept) == ["model.md", "pulsar-model.md"]


def test_skills_seeding_copies_the_named_skills_into_the_isolated_profile(monkeypatch, tmp_path, capsys):
    first, second = tmp_path / "skills-a", tmp_path / "skills-b"
    (first / "alpha").mkdir(parents=True)
    (first / "alpha" / "SKILL.md").write_text("alpha v1")
    (second / "alpha").mkdir(parents=True)
    (second / "alpha" / "SKILL.md").write_text("shadowed")
    (second / "beta" / "scripts").mkdir(parents=True)
    (second / "beta" / "SKILL.md").write_text("beta")
    (second / "beta" / "scripts" / "run.sh").write_text("echo hi")
    secret = tmp_path / "secret.txt"
    secret.write_text("not for the profile")
    try:
        os.symlink(str(secret), str(second / "beta" / "link.txt"))
    except (OSError, NotImplementedError):
        pass                                                     # no symlinks here (Windows without the right)
    monkeypatch.setenv("PULSAR_SKILLS", "alpha, beta,../etc,missing")
    monkeypatch.setenv("PULSAR_SKILLS_DIRS", os.pathsep.join([str(first), str(second)]))
    profile = tmp_path / "profile"
    L.seed_claude_config(profile)
    skills = profile / "skills"
    assert (skills / "alpha" / "SKILL.md").read_text() == "alpha v1"          # the first folder that has it
    assert (skills / "beta" / "scripts" / "run.sh").read_text() == "echo hi"
    assert not (skills / "beta" / "link.txt").exists()                       # links are never followed
    assert sorted(p.name for p in skills.iterdir()) == ["alpha", "beta"]
    err = capsys.readouterr().err
    assert "missing" in err and "../etc" in err
    # an edited source reaches the profile at the next launch
    (first / "alpha" / "SKILL.md").write_text("alpha v2")
    L.seed_claude_config(profile)
    assert (skills / "alpha" / "SKILL.md").read_text() == "alpha v2"
    # the default folder is ~/.claude/skills
    home = tmp_path / "home"
    (home / ".claude" / "skills" / "gamma").mkdir(parents=True)
    (home / ".claude" / "skills" / "gamma" / "SKILL.md").write_text("gamma")
    monkeypatch.setattr(L.Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("PULSAR_SKILLS_DIRS")
    monkeypatch.setenv("PULSAR_SKILLS", "gamma")
    L.seed_claude_config(profile)
    assert (skills / "gamma" / "SKILL.md").read_text() == "gamma"
    # nothing named: nothing copied; shared mode: your own profile is never seeded
    monkeypatch.delenv("PULSAR_SKILLS")
    other = tmp_path / "other"
    L.seed_claude_config(other)
    assert not (other / "skills").exists()
    monkeypatch.setenv("PULSAR_SKILLS", "gamma")
    monkeypatch.setenv("PULSAR_CLAUDE_CONFIG", "shared")
    shared = tmp_path / "shared"
    L.seed_claude_config(shared)
    assert not shared.exists()


def test_shared_config_never_moves_claude_codes_config_home(monkeypatch, pulsar_home):
    from pulsar import plugin
    from pulsar.core import state as ST
    sess = ST.Session(sid="s1", token_sha256="h", plan=[])
    mine = str(pulsar_home.parent / "my-claude")
    monkeypatch.setenv("PULSAR_CLAUDE_CONFIG", "shared")
    env = L.build_env(P.get("groq"), "k", "m", 0, core(), sess, "pls_tok", base={"PATH": "/bin"})
    assert "CLAUDE_CONFIG_DIR" not in env
    assert "CLAUDE_CONFIG_DIR" not in plugin.session_env(env)     # an empty pin would empty the config home
    env = L.build_env(P.get("groq"), "k", "m", 0, core(), sess, "pls_tok",
                      base={"PATH": "/bin", "CLAUDE_CONFIG_DIR": mine})
    assert env["CLAUDE_CONFIG_DIR"] == mine and plugin.session_env(env)["CLAUDE_CONFIG_DIR"] == mine
    # an outer Pulsar session's isolated profile is not "yours"
    env = L.build_env(P.get("groq"), "k", "m", 0, core(), sess, "pls_tok",
                      base={"PATH": "/bin", "CLAUDE_CONFIG_DIR": str(pulsar_home / "claude_config")})
    assert "CLAUDE_CONFIG_DIR" not in env
    # isolated (the default): Pulsar's own profile, pinned
    monkeypatch.delenv("PULSAR_CLAUDE_CONFIG")
    env = L.build_env(P.get("groq"), "k", "m", 0, core(), sess, "pls_tok",
                      base={"PATH": "/bin", "CLAUDE_CONFIG_DIR": mine})
    assert env["CLAUDE_CONFIG_DIR"] == str(pulsar_home / "claude_config")
    assert plugin.session_env(env)["CLAUDE_CONFIG_DIR"] == env["CLAUDE_CONFIG_DIR"]


def test_relay_plan_auto_and_explicit(pulsar_home):
    st = K.State()
    st.upsert(K.Profile(id="nvidia", provider="nvidia", model="a"))
    st.upsert(K.Profile(id="groq", provider="groq", model="b"))
    st.upsert(K.Profile(id="ollama", provider="ollama", model="c"))
    st.upsert(K.Profile(id="openai", provider="openai", model=""))
    st.active = "nvidia"
    keys = {"groq": "gsk", "nvidia": "nv"}
    chain = L.relay_plan(st, st.profiles["nvidia"], keys.get)
    assert [t.profile for t in chain] == ["groq"]  # local and model-less profiles are skipped
    st.prefs["relay"] = "off"
    assert L.relay_plan(st, st.profiles["nvidia"], keys.get) == []
    st.prefs["relay"] = ["ollama:qwen", "groq:"]
    assert [(t.profile, t.model) for t in L.relay_plan(st, st.profiles["nvidia"], keys.get)] == [("ollama", "qwen"), ("groq", "b")]


def test_relay_env_off_overrides_a_saved_chain(pulsar_home, monkeypatch):
    st = K.State()
    st.upsert(K.Profile(id="nvidia", provider="nvidia", model="a"))
    st.upsert(K.Profile(id="groq", provider="groq", model="b"))
    st.active = "nvidia"
    st.prefs["relay"] = "auto"
    keys = {"groq": "gsk", "nvidia": "nv"}
    assert [t.profile for t in L.relay_plan(st, st.profiles["nvidia"], keys.get)] == ["groq"]
    monkeypatch.setenv("PULSAR_RELAY", "off")
    assert L.relay_plan(st, st.profiles["nvidia"], keys.get) == []


def _relay_profiles():
    st = K.State()
    st.upsert(K.Profile(id="nvidia", provider="nvidia", model="a"))
    st.upsert(K.Profile(id="groq", provider="groq", model="b"))
    st.upsert(K.Profile(id="ollama", provider="ollama", model="c"))
    st.upsert(K.Profile(id="custom-vpn", provider="custom", base_url="http://10.1.2.3:8000/v1", model="d"))
    st.upsert(K.Profile(id="custom-cloud", provider="custom", base_url="https://llm.example.com/v1", model="e"))
    st.upsert(K.Profile(id="openai", provider="openai", base_url="http://192.168.1.9:8000/v1", model="f"))
    st.active = "nvidia"
    return st, {"nvidia": "nv", "groq": "gsk", "custom-cloud": "sk-x", "openai": "sk-y"}.get


def test_auto_relay_only_between_cloud_registry_providers(pulsar_home):
    st, keys = _relay_profiles()
    # a cloud provider from the registry relays to other cloud registry providers only: never to a local
    # server, a private-range endpoint, a registry entry pointed at a LAN host, or a custom endpoint
    assert [t.profile for t in L.relay_plan(st, st.profiles["nvidia"], keys)] == ["groq"]
    # a local or custom primary never relays automatically, to anything
    for pid in ("ollama", "custom-vpn", "custom-cloud", "openai"):
        assert L.relay_plan(st, st.profiles[pid], keys) == [], pid
    # an explicit `pulsarcode relay <chain>` still works, from any primary
    st.prefs["relay"] = ["groq:b", "custom-cloud:e"]
    assert [t.profile for t in L.relay_plan(st, st.profiles["ollama"], keys)] == ["groq", "custom-cloud"]


def test_pilot_sessions_never_relay(monkeypatch, pulsar_home):
    from pulsar.core import spawn
    from pulsar.core.client import CoreClient
    st, keys = _relay_profiles()
    st.prefs["pilot"] = True
    K.save(st)
    info = _core(4801, "pla_one")
    monkeypatch.setattr(spawn, "ensure_core", lambda *a, **kw: info)
    monkeypatch.setattr(spawn, "attach", lambda *a, **kw: info)
    monkeypatch.setattr(CoreClient, "post", lambda self, path, body, timeout=30.0: {"ok": True})
    monkeypatch.delenv("PULSAR_PILOT", raising=False)
    for relay in ("auto", ["groq:b"]):
        st.prefs["relay"] = relay
        K.save(st)
        _, sess, _ = L.start_session(st, st.profiles["nvidia"], keys, warn=lambda m: None)
        assert sess.flags["pilot"] is True and sess.flags["relay"] is False, relay
        assert [t.profile for t in sess.plan] == ["nvidia"], relay
        assert L.relay_plan(st, st.profiles["nvidia"], keys, pilot=True) == []
    # Pilot off: the chain is back
    st.prefs["pilot"] = False
    st.prefs["relay"] = "auto"
    K.save(st)
    _, sess, _ = L.start_session(st, st.profiles["nvidia"], keys, warn=lambda m: None)
    assert sess.flags["relay"] is True and [t.profile for t in sess.plan] == ["nvidia", "groq"]


def test_switch_plan_follows_the_relay_rules(pulsar_home):
    from pulsar.core import state as ST
    st, keys = _relay_profiles()
    plan = [L.target_for(st.profiles["nvidia"]), L.target_for(st.profiles["groq"])]
    ollama = L.target_for(st.profiles["ollama"], "qwen3")
    # to a local model: the cloud relays go (a local primary never relays automatically)
    assert [t.profile for t in L.switch_plan(st, plan, ollama)] == ["ollama"]
    # between cloud registry models: the previous ones stay as relays
    groq = L.target_for(st.profiles["groq"], "other-model")
    assert [(t.profile, t.model) for t in L.switch_plan(st, plan, groq)] == [
        ("groq", "other-model"), ("nvidia", "a"), ("groq", "b")]
    # the custom endpoint is never kept as an automatic relay
    mixed = plan + [L.target_for(st.profiles["custom-cloud"])]
    assert "custom-cloud" not in [t.profile for t in L.switch_plan(st, mixed, groq)]
    # an explicit chain is kept whatever the new primary is
    st.prefs["relay"] = ["groq:b"]
    assert [t.profile for t in L.switch_plan(st, plan, ollama)] == ["ollama", "groq"]
    # a session with Pilot armed never gets a relay
    assert [t.profile for t in L.switch_plan(st, plan, groq, pilot=True)] == ["groq"]
    assert all(isinstance(t, ST.Target) for t in L.switch_plan(st, plan, groq))


def test_print_mode_detection():
    assert cli.is_print_mode(["-p", "hi"]) and cli.is_print_mode(["--print"])
    assert not cli.is_print_mode(["--continue"])


def test_cli_model_and_use(capsys, pulsar_home):
    st = K.State()
    st.upsert(K.Profile(id="groq", provider="groq", model="a"))
    st.upsert(K.Profile(id="nvidia", provider="nvidia", model="b"))
    K.save(st)
    assert cli.main(["use", "groq"]) == 0
    assert K.load().active == "groq"
    assert cli.main(["model", "llama-3.3-70b-versatile"]) == 0
    assert K.load().current().model == "llama-3.3-70b-versatile"
    assert cli.main(["model", "nvidia:moonshotai/kimi-k2.6"]) == 0
    st = K.load()
    assert st.active == "nvidia" and st.current().model == "moonshotai/kimi-k2.6"
    capsys.readouterr()
    assert cli.main(["model"]) == 0
    assert capsys.readouterr().out.strip() == "moonshotai/kimi-k2.6"


def test_cli_key_noninteractive_offline(pulsar_home, capsys, monkeypatch):
    import io
    monkeypatch.setattr(L, "claude_ancestor", lambda *a, **kw: False)     # a person's terminal, not Claude Code's
    monkeypatch.setattr("sys.stdin", io.StringIO("gsk_abcdefghijklmnopqrstuvwxyz\n"))
    rc = cli.main(["key", "--stdin"])
    assert rc == 0
    st = K.load()
    assert st.active == "groq" and K.get_key("groq") == "gsk_abcdefghijklmnopqrstuvwxyz"


def test_cli_misc(capsys):
    assert cli.main(["version"]) == 0 and "3." in capsys.readouterr().out
    assert cli.main(["help"]) == 0 and "pulsarcode key" in capsys.readouterr().out
    assert cli.main(["providers"]) == 0 and "NVIDIA NIM" in capsys.readouterr().out
    assert cli.main(["models", "--json"]) == 0


def test_headless_launch_without_provider_explains(monkeypatch, capsys, no_real_core):
    monkeypatch.setattr(L, "find_claude", lambda: "/bin/true")
    monkeypatch.setattr(L, "claude_version", lambda b: "x")
    rc = cli.main(["-p", "hi"])
    assert rc == 2
    assert "no provider connected" in capsys.readouterr().err
    assert no_real_core == []          # the preload never tried to ignite Core without a provider


def test_env_cloud_routes_keep_claude_codes_own_auth():
    from pulsar.core import state as ST
    sess = ST.Session(sid="s9", token_sha256="h", plan=[])
    base = {"AWS_REGION": "eu-west-3", "ANTHROPIC_BASE_URL": "http://stale", "ANTHROPIC_AUTH_TOKEN": "stale",
            "AWS_BEARER_TOKEN_BEDROCK": "old", "PATH": "/bin"}
    env = L.build_env(P.get("bedrock"), "ABSKkey", "sonnet", 0, core(), sess, "pls_tok", base=base)
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1" and env["AWS_BEARER_TOKEN_BEDROCK"] == "ABSKkey"
    assert env["AWS_REGION"] == "eu-west-3" and env["ANTHROPIC_MODEL"] == "sonnet"
    assert "ANTHROPIC_BASE_URL" not in env and "ANTHROPIC_AUTH_TOKEN" not in env
    assert "ANTHROPIC_DEFAULT_HAIKU_MODEL" not in env and "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS" not in env
    assert env["PULSAR_CORE"] == "http://127.0.0.1:4123" and env["PULSAR_SESSION_TOKEN"] == "pls_tok"
    env = L.build_env(P.get("vertex"), "", "opus", 0, None, base={"PATH": "/bin"},
                      options={"project": "acme-prod-1", "region": "us-east5"})
    assert env["CLAUDE_CODE_USE_VERTEX"] == "1" and env["ANTHROPIC_VERTEX_PROJECT_ID"] == "acme-prod-1"
    assert env["CLOUD_ML_REGION"] == "us-east5" and "PULSAR_SESSION_TOKEN" not in env
    env = L.build_env(P.get("claude"), "sk-ant-oat01-x", "", 0, None, base={"ANTHROPIC_API_KEY": "k"})
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-x" and "ANTHROPIC_API_KEY" not in env


def test_session_client_prefers_side_channel_token():
    from pulsar.core.client import CoreClient
    c = CoreClient.from_session_env({"PULSAR_CORE": "http://127.0.0.1:1", "PULSAR_SESSION_TOKEN": "pls_side",
                                     "ANTHROPIC_AUTH_TOKEN": "sk-ant-oat-x"})
    assert c is not None and c.token == "pls_side"
    assert CoreClient.from_session_env({"PULSAR_CORE": "http://127.0.0.1:1", "ANTHROPIC_AUTH_TOKEN": "nope"}) is None


def test_relay_plan_skips_cloud_routes(pulsar_home):
    st = K.load()
    for pid, prov, model in (("nvidia", "nvidia", "a"), ("bedrock", "bedrock", "sonnet"), ("groq", "groq", "b")):
        st.profiles[pid] = K.Profile(id=pid, provider=prov, model=model)
    keys = {"nvidia": "k", "groq": "k", "bedrock": ""}
    assert [t.profile for t in L.relay_plan(st, st.profiles["nvidia"], keys.get)] == ["groq"]
    st.prefs["relay"] = ["bedrock:opus", "groq:b"]
    assert [t.profile for t in L.relay_plan(st, st.profiles["nvidia"], keys.get)] == ["groq"]


# ---------------------------------------------------------------------------
# security and stability regressions
# ---------------------------------------------------------------------------

SHELL_KEYS = {
    "OPENROUTER_API_KEY": "sk-or-v1-ENVONLY-9999999999999999",
    "OPENAI_API_KEY": "sk-proj-ENVONLY-openai-123456789",
    "GEMINI_API_KEY": "AIzaENVONLYgemini0123456789abcdef",
    "GOOGLE_API_KEY": "AIzaENVONLYgoogle0123456789abcdef",
    "HF_TOKEN": "hf_ENVONLYhuggingface012345",
    "HUGGINGFACE_API_KEY": "hf_ENVONLYhuggingface2-6789",
    "MISTRAL_API_KEY": "fake-mistral-ENVONLY-not-a-key",
    "GROQ_API_KEY": "gsk_ENVONLYgroq0123456789abcdef",
    "DEEPSEEK_API_KEY": "sk-ENVONLYdeepseek0123456789abcd",
    "AZURE_OPENAI_API_KEY": "fake-azure-ENVONLY-not-a-key",
    "AWS_BEARER_TOKEN_BEDROCK": "ABSKENVONLYbedrock0123456789abcdef",
    "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-ENVONLY-0123456789",
    "MY_RENAMED_NIM_KEY": "nvapi-PRIMARYKEY-0123456789abcdef",
}


def test_core_route_env_carries_no_provider_key_at_all():
    from pulsar.core import state as ST
    base = dict(SHELL_KEYS, PATH="/bin", HOME="/home/u", AWS_PROFILE="dev")
    sess = ST.Session(sid="s1", token_sha256="h", plan=[])
    env = L.build_env(P.get("nvidia"), "nvapi-PRIMARYKEY-0123456789abcdef", "moonshotai/kimi-k2.6", 0,
                      core(), sess, "pls_tok", base=base, route="core")
    dump = json.dumps(env)
    for var, value in SHELL_KEYS.items():
        assert value not in dump, var
    assert env["ANTHROPIC_AUTH_TOKEN"] == "pls_tok" and env["PATH"] == "/bin" and env["AWS_PROFILE"] == "dev"
    for var in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "HF_TOKEN", "HUGGINGFACE_API_KEY",
                "MISTRAL_API_KEY", "NVIDIA_NIM_API_KEY"):
        assert var in L.provider_key_vars()
    # explicit opt-out keeps them for tools you run inside Claude Code
    env = L.build_env(P.get("nvidia"), "nvapi-x", "m", 0, core(), sess, "pls_tok",
                      base=dict(base, PULSAR_KEEP_ENV_KEYS="1"), route="core")
    assert env["OPENAI_API_KEY"] == SHELL_KEYS["OPENAI_API_KEY"]


def test_cloud_routes_keep_only_their_own_credential():
    base = dict(SHELL_KEYS, PATH="/bin", AWS_REGION="eu-west-3", AWS_ACCESS_KEY_ID="AKIAEXAMPLE")
    others = [SHELL_KEYS["MY_RENAMED_NIM_KEY"]]          # what cli passes: every other connected profile's key
    env = L.build_env(P.get("bedrock"), "ABSKmine0123456789abcdefgh", "sonnet", 0, None, base=base, route="cloud",
                      secrets=others)
    assert env["AWS_BEARER_TOKEN_BEDROCK"] == "ABSKmine0123456789abcdefgh" and env["AWS_ACCESS_KEY_ID"] == "AKIAEXAMPLE"
    dump = json.dumps(env)
    for var, value in SHELL_KEYS.items():
        assert value not in dump, var
    env = L.build_env(P.get("claude"), "sk-ant-oat01-mine-0123456789", "", 0, None, base=base, route="cloud",
                      secrets=others)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-mine-0123456789"
    assert SHELL_KEYS["OPENROUTER_API_KEY"] not in json.dumps(env)


def test_env_refuses_control_sequences_in_model_ids():
    with pytest.raises(ValueError):
        L.build_env(P.get("nvidia"), "k", "evil\x1b]52;c;eA==\x07", 0, core(), None, "pls_tok", base={})


def test_plain_http_public_endpoint_never_gets_a_key(monkeypatch):
    monkeypatch.delenv("PULSAR_ALLOW_HTTP", raising=False)
    public = P.custom_provider("http://203.0.113.7:8000/v1")
    lan = P.custom_provider("http://192.168.1.7:8000/v1")
    assert L.transport_problem(public, "sk-secret-123456789") and not L.transport_problem(public, "")
    assert not L.transport_problem(lan, "sk-secret-123456789") and lan.local and not public.local
    with pytest.raises(ValueError):
        L.build_env(public, "sk-secret-123456789", "m", 0, core(), None, "pls_tok", base={}, route="core")
    assert not L.transport_problem(public, "sk-secret-123456789", {"PULSAR_ALLOW_HTTP": "1"})
    st = K.State()
    st.upsert(K.Profile(id="nvidia", provider="nvidia", model="a"))
    st.upsert(K.Profile(id="custom-pub", provider="custom", base_url="http://203.0.113.7:8000/v1", model="b"))
    st.active = "nvidia"
    assert L.relay_plan(st, st.profiles["nvidia"], {"nvidia": "k", "custom-pub": "sk-secret-123456789"}.get) == []


def test_windows_child_wait_survives_ctrl_c():
    import signal
    import sys
    before = signal.getsignal(signal.SIGINT)

    class Proc:
        calls = 0

        def wait(self):
            Proc.calls += 1
            assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN   # the launcher ignores Ctrl+C meanwhile
            if Proc.calls == 1:
                raise KeyboardInterrupt
            return 7
    assert L.run_child(["claude"], {}, popen=lambda argv, env: Proc()) == 7
    assert Proc.calls == 2 and signal.getsignal(signal.SIGINT) == before
    # and with a real child that sends SIGINT to the launcher while it runs. The launcher is a separate
    # Python here, never pytest itself: a regression must fail this test, not interrupt the whole run.
    # POSIX only: on Windows, os.kill(pid, SIGINT) is TerminateProcess; a real Ctrl+C there reaches every
    # process on the console, which the KeyboardInterrupt case above already covers.
    if os.name != "nt":
        import subprocess
        import textwrap
        harness = textwrap.dedent('''
            import signal, sys
            from pulsar import launch as L
            before = signal.getsignal(signal.SIGINT)
            child = "import os,signal,time; os.kill(os.getppid(), signal.SIGINT); time.sleep(0.3); raise SystemExit(5)"
            code = L.run_child([sys.executable, "-c", child], None)
            sys.exit(code if signal.getsignal(signal.SIGINT) == before else 3)
        ''')
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.run([sys.executable, "-c", harness], cwd=root, capture_output=True, text=True, timeout=60)
        assert out.returncode == 5, out.stderr[-2000:]


def test_launch_rejects_public_http_before_registering(monkeypatch, capsys, no_real_core):
    monkeypatch.delenv("PULSAR_ALLOW_HTTP", raising=False)
    monkeypatch.setattr(L, "find_claude", lambda: "/bin/true")
    monkeypatch.setattr(L, "claude_version", lambda b: "x")
    monkeypatch.setattr(L, "exec_claude", lambda *a, **k: pytest.fail("must not launch"))
    st = K.State()
    st.upsert(K.Profile(id="custom-pub", provider="custom", base_url="http://203.0.113.7:8000/v1", model="m"))
    K.save(st)
    K.set_key("custom-pub", "sk-secret-123456789")
    started = []
    monkeypatch.setattr(L, "start_session", lambda *a, **kw: started.append(1))
    assert cli.main(["-p", "hi"]) == 2
    assert "plain http" in capsys.readouterr().err and started == []


def test_settings_keep_claude_codes_file_tools_away_from_pulsar_secrets(pulsar_home):
    from pulsar import plugin
    perms = plugin.settings("m")["permissions"]
    root = plugin._rule_path(str(pulsar_home.resolve()))
    assert root.startswith("//")
    assert "Read(%s/keys.json)" % root in perms["deny"] and "Read(%s/run/**)" % root in perms["deny"]
    assert "Edit(%s/keys.json)" % root in perms["deny"] and "Edit(%s/run/**)" % root in perms["deny"]
    assert all(r.startswith("mcp__plugin_pulsar_pulsar__") for r in perms["allow"])
    assert "mcp__plugin_pulsar_pulsar__pulsar_switch" not in perms["allow"]     # the model must ask
    # council sends the prompt to other providers: Claude Code asks unless the user typed /pulsar:council
    assert "mcp__plugin_pulsar_pulsar__council" not in perms["allow"]
    assert "allowed-tools: mcp__plugin_pulsar_pulsar__council" in plugin.COMMANDS["council.md"]
    # Claude Code 2.1.287 warns at launch about Write(...) deny rules (Edit(...) covers every edit tool)
    assert not any(r.startswith("Write(") for r in perms["deny"])


def test_deny_rules_cover_every_secret_and_leave_the_profile_editable(pulsar_home, tmp_path, monkeypatch):
    from pulsar import plugin
    K.ensure_home()
    rules = plugin.settings("m")["permissions"]["deny"]
    root = plugin._rule_path(str(pulsar_home.resolve()))
    for rel in ("keys.json", "nim.key", "nim.key.migrated", "config.json", "settings.json", "run/**", "logs/**",
                "claude_config/.credentials.json"):
        assert "Read(%s/%s)" % (root, rel) in rules, rel
    for rel in ("keys.json", "nim.key", "nim.key.migrated", "config.json", "run/**", "plugin/**", "logs/**",
                "claude_config/settings.json", "claude_config/.claude.json"):
        assert "Edit(%s/%s)" % (root, rel) in rules, rel
    # Claude Code keeps auto memory and your user-level skills in the isolated profile: not fenced off
    assert "Edit(%s/**)" % root not in rules
    assert not [r for r in rules if r.endswith("/claude_config/**)") or r.endswith("/claude_config)")]
    # a home reached through a link: the spelling given and the resolved one
    link = tmp_path / "home-link"
    try:
        os.symlink(str(pulsar_home), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks here")
    monkeypatch.setenv("PULSAR_HOME", str(link))
    rules = plugin.settings("m")["permissions"]["deny"]
    assert "Read(%s/keys.json)" % plugin._rule_path(str(link)) in rules and "Read(%s/keys.json)" % root in rules


def test_deny_rule_spelling_on_windows():
    from pulsar import plugin
    assert plugin._rule_path("C:\\Users\\me\\.pulsarcode", windows=True) == "//c/Users/me/.pulsarcode"
    assert plugin._rule_path("D:/data/pulsar/", windows=True) == "//d/data/pulsar"
    assert plugin._rule_path("\\\\server\\share\\pulsar", windows=True) == ""      # no rule spelling for UNC paths
    assert plugin._rule_path("/home/me/.pulsarcode", windows=False) == "//home/me/.pulsarcode"
    assert plugin._rule_path("relative/home", windows=False) == ""


def test_commit_attribution_is_opt_in(monkeypatch, pulsar_home):
    from pulsar import plugin
    monkeypatch.delenv("PULSAR_ATTRIBUTION", raising=False)
    # nothing in your commits or PRs by default (Claude Code's own line would name the wrong model)
    assert plugin.settings("vendor/model-a")["attribution"] == {"commit": "", "pr": ""}
    assert "attribution" not in plugin.settings("claude-sonnet-4-5", anthropic=True)    # Claude itself: Claude Code's own
    monkeypatch.setenv("PULSAR_ATTRIBUTION", "on")
    att = plugin.settings("vendor/model-a")["attribution"]
    assert "Pulsar Code (vendor/model-a)" in att["commit"] and "vendor/model-a" in att["pr"]
    for off in ("off", "0", "", "no"):
        monkeypatch.setenv("PULSAR_ATTRIBUTION", off)
        assert plugin.settings("vendor/model-a")["attribution"] == {"commit": "", "pr": ""}, off


def test_project_settings_cannot_change_what_session_code_does(monkeypatch, pulsar_home, tmp_path):
    from pulsar import plugin
    from pulsar.core import state as ST
    sess = ST.Session(sid="s1", token_sha256="h", plan=[])
    env = L.build_env(P.get("groq"), "k", "m", 0, core(), sess, "pls_tok", base={"PATH": "/bin"})
    pins = plugin.session_env(env)
    for var in ("PULSAR_PILOT_PYTHON", "PULSAR_PILOT_OVERLAY", "PULSAR_FX", "PULSAR_NOTIFY", "PULSAR_STAGE_VIEWER",
                "PULSAR_ALLOW_HTTP", "PULSAR_CLAUDE_BIN", "PULSAR_CA_BUNDLE"):
        assert pins[var] == "", var
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "settings.local.json").write_text(json.dumps({"env": {
        "PULSAR_PILOT_OVERLAY": "0", "PULSAR_PILOT_PYTHON": "./evil", "PULSAR_SEED": "7", "CLAUDE_CONFIG_DIR": "/x"}}))
    warned = []
    args = plugin.claude_args("m", session_id="s1") + ["-p", "hi"]
    assert L.pin_session_settings(args, env, warn=warned.append, cwd=str(proj))
    assert len(warned) == 1, warned
    assert "PULSAR_PILOT_OVERLAY" in warned[0] and "PULSAR_PILOT_PYTHON" in warned[0] and "CLAUDE_CONFIG_DIR" in warned[0]
    assert "PULSAR_SEED" not in warned[0]                                   # not pinned: it is not called ignored
    # shared mode without a CLAUDE_CONFIG_DIR of yours: that one cannot be pinned, and the warning says it applies
    monkeypatch.setenv("PULSAR_CLAUDE_CONFIG", "shared")
    env = L.build_env(P.get("groq"), "k", "m", 0, core(), sess, "pls_tok", base={"PATH": "/bin"})
    warned = []
    args = plugin.claude_args("m", session_id="s1") + ["-p", "hi"]
    assert L.pin_session_settings(args, env, warn=warned.append, cwd=str(proj))
    assert len(warned) == 2 and "CLAUDE_CONFIG_DIR" not in warned[0] and "CLAUDE_CONFIG_DIR" in warned[1]
    assert "applies" in warned[1]


def test_every_pulsar_variable_session_code_reads_is_pinned_or_cosmetic():
    import re
    from pulsar import plugin
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pulsar")
    seen = set()
    for sub in ("mcp", "pilot", "stage"):
        for name in os.listdir(os.path.join(root, sub)):
            if name.endswith(".py"):
                with open(os.path.join(root, sub, name), encoding="utf-8") as fh:
                    seen.update(re.findall(r'"(PULSAR_[A-Z0-9_]+)"', fh.read()))
    for name in ("notify.py", "statusline.py", "term.py", "fx.py"):
        with open(os.path.join(root, name), encoding="utf-8") as fh:
            seen.update(re.findall(r'"(PULSAR_[A-Z0-9_]+)"', fh.read()))
    pinned = set(plugin.pinned_names())
    assert not sorted(seen - pinned - set(plugin.COSMETIC_VARS))


# ---------------------------------------------------------------------------
# Pilot wiring: prefs -> PULSAR_PILOT in the launched session
# ---------------------------------------------------------------------------

def _pilot_prefs(on):
    st = K.load()
    st.prefs["pilot"] = on
    K.save(st)


def _sess():
    from pulsar.core import state as ST
    return ST.Session(sid="s1", token_sha256="h", plan=[])


def test_pilot_on_in_prefs_reaches_the_session_env(monkeypatch, pulsar_home):
    from pulsar.pilot import safety
    monkeypatch.delenv("PULSAR_PILOT", raising=False)
    _pilot_prefs(True)
    env = L.build_env(P.get("groq"), "k", "m", 0, core(), _sess(), "pls_tok", base={"PATH": "/bin"})
    assert env["PULSAR_PILOT"] == "1" and env[L.PILOT_AUTO] == "1"
    assert {"PULSAR_PILOT": env["PULSAR_PILOT"]} == safety.session_env()        # exactly what safety says
    # and the MCP server started with that environment registers the pilot tools
    from pulsar.mcp.server import Server
    monkeypatch.setenv("PULSAR_PILOT", env["PULSAR_PILOT"])
    assert any(n.startswith("pilot_") for n in Server(None, out=io.StringIO()).tools)


def test_pilot_off_scrubs_an_inherited_flag(monkeypatch, pulsar_home):
    from pulsar.pilot import safety
    monkeypatch.delenv("PULSAR_PILOT", raising=False)
    _pilot_prefs(False)
    assert safety.session_env() == {}
    # an outer Pulsar session exported PULSAR_PILOT=1 (marked as the launcher's own): dropped
    base = {"PATH": "/bin", "PULSAR_PILOT": "1", L.PILOT_AUTO: "1"}
    env = L.build_env(P.get("groq"), "k", "m", 0, core(), _sess(), "pls_tok", base=base)
    assert "PULSAR_PILOT" not in env and L.PILOT_AUTO not in env
    from pulsar.mcp.server import Server
    monkeypatch.delenv("PULSAR_PILOT", raising=False)
    assert not any(n.startswith("pilot_") for n in Server(None, out=io.StringIO()).tools)
    # inside a Claude Code session a typed PULSAR_PILOT=1 is not trusted (the model could have set it)
    for marker in ("CLAUDECODE", "PULSAR_SESSION", "PULSAR_SESSION_TOKEN"):
        env = L.build_env(P.get("groq"), "k", "m", 0, core(), _sess(), "pls_tok",
                          base={"PATH": "/bin", "PULSAR_PILOT": "1", marker: "x"})
        assert "PULSAR_PILOT" not in env, marker
        assert L.pilot_ignored({"PULSAR_PILOT": "1", marker: "x"}, env)


def test_pilot_set_explicitly_for_this_launch_is_kept(monkeypatch, pulsar_home):
    monkeypatch.delenv("PULSAR_PILOT", raising=False)
    monkeypatch.setattr(L, "_human_terminal", lambda: True)            # a person typed PULSAR_PILOT=1 pulsarcode
    monkeypatch.setattr(L, "claude_ancestor", lambda *a, **kw: False)
    _pilot_prefs(False)
    env = L.build_env(P.get("groq"), "k", "m", 0, core(), _sess(), "pls_tok", base={"PATH": "/bin", "PULSAR_PILOT": "1"})
    assert env["PULSAR_PILOT"] == "1" and L.PILOT_AUTO not in env
    assert not L.pilot_ignored({"PULSAR_PILOT": "1"}, env)
    # PULSAR_PILOT=0 keeps it off even when prefs arm it, also inside a session and over an inherited marker
    _pilot_prefs(True)
    for base in ({"PULSAR_PILOT": "0"}, {"PULSAR_PILOT": "0", "CLAUDECODE": "1"},
                 {"PULSAR_PILOT": "0", L.PILOT_AUTO: "1"}):
        env = L.build_env(P.get("groq"), "k", "m", 0, core(), _sess(), "pls_tok", base=dict(base, PATH="/bin"))
        assert env["PULSAR_PILOT"] == "0", base
    # armed in prefs: a nested launch inside a session still gets it from prefs
    env = L.build_env(P.get("groq"), "k", "m", 0, core(), _sess(), "pls_tok",
                      base={"PATH": "/bin", "PULSAR_PILOT": "1", L.PILOT_AUTO: "1", "CLAUDECODE": "1"})
    assert env["PULSAR_PILOT"] == "1"


def test_pilot_request_without_a_terminal_is_not_honoured(monkeypatch, pulsar_home):
    """The model's Bash tool can unset CLAUDECODE, PULSAR_SESSION and PULSAR_SESSION_TOKEN; without a
    TTY its PULSAR_PILOT=1 is still only a request, and prefs say off."""
    monkeypatch.delenv("PULSAR_PILOT", raising=False)
    monkeypatch.setattr(L, "_human_terminal", lambda: False)
    _pilot_prefs(False)
    env = L.build_env(P.get("groq"), "k", "m", 0, core(), _sess(), "pls_tok", base={"PATH": "/bin", "PULSAR_PILOT": "1"})
    assert "PULSAR_PILOT" not in env
    assert L.pilot_ignored({"PULSAR_PILOT": "1"}, env)
    # a terminal does not help inside a session
    monkeypatch.setattr(L, "_human_terminal", lambda: True)
    assert L.pilot_env({"PULSAR_PILOT": "1", "CLAUDECODE": "1"}) == {}
    # prefs on: armed regardless (with the moment it was armed)
    monkeypatch.setattr(L, "_human_terminal", lambda: False)
    _pilot_prefs(True)
    got = L.pilot_env({"PULSAR_PILOT": "1"})
    assert float(got.pop(L.PILOT_ARMED_AT)) > 0
    assert got == {"PULSAR_PILOT": "1", L.PILOT_AUTO: "1"}
    # and PULSAR_PILOT=0 always counts
    assert L.pilot_env({"PULSAR_PILOT": "0"}) == {"PULSAR_PILOT": "0"}


def test_pilot_request_from_inside_claude_code_is_not_honoured(monkeypatch, pulsar_home):
    """A shell that unset the session markers and faked a terminal is still a Claude Code descendant."""
    monkeypatch.delenv("PULSAR_PILOT", raising=False)
    monkeypatch.setattr(L, "_human_terminal", lambda: True)
    _pilot_prefs(False)
    monkeypatch.setattr(L, "claude_ancestor", lambda *a, **kw: True)
    assert L.pilot_env({"PULSAR_PILOT": "1"}) == {}
    monkeypatch.setattr(L, "claude_ancestor", lambda *a, **kw: False)
    assert L.pilot_env({"PULSAR_PILOT": "1"})["PULSAR_PILOT"] == "1"


def test_claude_ancestor_walks_the_process_tree():
    me = 4000
    native = {me: (3000, "zsh -c pulsarcode pilot on"), 3000: (2000, "/bin/zsh"), 2000: (1000, "claude --continue"),
              1000: (1, "/usr/bin/login")}
    assert L.claude_ancestor(native, me)
    npm = {**native, 2000: (1000, "node /usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js")}
    assert L.claude_ancestor(npm, me)
    assert L.claude_ancestor({**native, 2000: (1000, "CLAUDE")}, me)         # the process title Claude Code sets
    terminal = {**native, 2000: (1000, "/Applications/Terminal.app/Contents/MacOS/Terminal")}
    assert not L.claude_ancestor(terminal, me)
    assert not L.claude_ancestor({me: (me, "loop")}, me) and not L.claude_ancestor({}, me)
    # a desktop app named Claude is not Claude Code
    assert not L.claude_ancestor({**native, 2000: (1000, "/Applications/Claude.app/Contents/MacOS/Claude")}, me)


def test_human_terminal_false_for_pipes(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert not L._human_terminal()


def test_pilot_off_after_pilot_off_command(monkeypatch, pulsar_home):
    import time as _t
    monkeypatch.delenv("PULSAR_PILOT", raising=False)
    st = K.load()
    st.prefs.update(pilot=False, pilot_off_at=_t.time() + 60)
    K.save(st)
    assert L.pilot_env({"PATH": "/bin", "PULSAR_PILOT": "1", L.PILOT_AUTO: "1"}) == {}


def test_pilot_off_survives_an_mcp_server_restart(monkeypatch, pulsar_home):
    from pulsar.pilot import safety
    t = 1_000_000.0
    off = {"pilot": False, "pilot_off_at": t + 5}
    armed = {"PULSAR_PILOT": "1", "PULSAR_PILOT_ARMED_AT": "%.3f" % t}
    # armed at t, `pulsarcode pilot off` at t+5, then Claude Code restarts the MCP server at t+10: still off
    assert safety.state(env=armed, prefs=off, started=t + 10)[0] is False
    # a session armed after the switch-off is on
    assert safety.state(env=dict(armed, PULSAR_PILOT_ARMED_AT="%.3f" % (t + 6)), prefs=off, started=t + 10)[0]
    # the launcher exports the arm time with Pilot, and a project cannot change it
    monkeypatch.delenv("PULSAR_PILOT", raising=False)
    _pilot_prefs(True)
    env = L.build_env(P.get("groq"), "k", "m", 0, core(), _sess(), "pls_tok", base={"PATH": "/bin",
                                                                                 "PULSAR_PILOT_ARMED_AT": "1"})
    from pulsar import plugin
    assert abs(float(env["PULSAR_PILOT_ARMED_AT"]) - __import__("time").time()) < 60
    assert plugin.session_env(env)["PULSAR_PILOT_ARMED_AT"] == env["PULSAR_PILOT_ARMED_AT"]
    _pilot_prefs(False)
    env = L.build_env(P.get("groq"), "k", "m", 0, core(), _sess(), "pls_tok", base={"PATH": "/bin",
                                                                                 "PULSAR_PILOT_ARMED_AT": "1"})
    assert "PULSAR_PILOT_ARMED_AT" not in env


def test_launch_session_flag_and_note_for_an_ignored_pilot(monkeypatch, capsys, pulsar_home):
    from pulsar.core import spawn
    from pulsar.core import state as ST
    info = ST.CoreInfo(port=4747, pid=1, version="3", admin_token="pla_x")
    monkeypatch.setattr(spawn, "attach", lambda *a, **kw: info)
    seen = {}
    monkeypatch.setattr(L, "start_session", lambda *a, **kw: (info, ST.Session(sid="s-in", token_sha256="h", plan=[]),
                                                                "pls_inner"))
    monkeypatch.setattr(L, "find_claude", lambda: "/bin/true")
    monkeypatch.setattr(L, "claude_version", lambda b: "2.1.287")
    monkeypatch.setattr(L, "exec_claude", lambda binary, args, env, **k: seen.update(args=args, env=env) or 0)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    st = K.State()
    st.upsert(K.Profile(id="ollama", provider="ollama", model="qwen3"))
    K.save(st)
    monkeypatch.setenv("PULSAR_PILOT", "1")
    monkeypatch.setenv("CLAUDECODE", "1")
    assert cli.main(["-p", "hi"]) == 0
    out = capsys.readouterr()
    assert "PULSAR_PILOT" not in seen["env"] and "PULSAR_PILOT=1 was ignored" in out.out + out.err


# ---------------------------------------------------------------------------
# Stage pane: a second --plugin-dir when Claude Code supports function hooks
# ---------------------------------------------------------------------------

def _plugin_dirs(args):
    return [args[i + 1] for i, a in enumerate(args) if a == "--plugin-dir"]


def test_stage_pane_plugin_dir_follows_the_detected_claude_version(monkeypatch, tmp_path, pulsar_home):
    from pulsar import plugin
    from pulsar.stage import pane
    fn = tmp_path / "fnplug"
    fn.mkdir()
    monkeypatch.delenv("PULSAR_STAGE_PANE", raising=False)
    monkeypatch.setattr(pane, "plugin_dir", lambda: fn)
    assert _plugin_dirs(plugin.claude_args("m", claude_version="2.1.287"))[1:] == [str(fn)]
    assert _plugin_dirs(plugin.claude_args("m", claude_version="2.1.300 (Claude Code)"))[1:] == [str(fn)]
    for old in ("2.1.286", "", "garbage", "1.0.0"):
        assert len(_plugin_dirs(plugin.claude_args("m", claude_version=old))) == 1, old
    monkeypatch.setattr(pane, "plugin_dir", lambda: None)              # PULSAR_STAGE_PANE=off, or not shipped
    assert len(_plugin_dirs(plugin.claude_args("m", claude_version="2.1.287"))) == 1


def test_stage_pane_env_switch_off(monkeypatch, pulsar_home):
    from pulsar import plugin
    from pulsar.stage import pane
    monkeypatch.delenv("PULSAR_STAGE_PANE", raising=False)
    if pane.package_plugin_dir() is None:
        pytest.skip("the pane plugin is not shipped in this tree")
    assert len(_plugin_dirs(plugin.claude_args("m", claude_version="2.1.287"))) == 2
    monkeypatch.setenv("PULSAR_STAGE_PANE", "off")
    assert len(_plugin_dirs(plugin.claude_args("m", claude_version="2.1.287"))) == 1


def test_stage_pane_never_loads_from_the_package_folder(monkeypatch, tmp_path, pulsar_home):
    """Claude Code writes its generated types into a --plugin-dir: never into the package itself."""
    from pulsar.stage import pane
    monkeypatch.delenv("PULSAR_STAGE_PANE", raising=False)
    if pane.package_plugin_dir() is None:
        pytest.skip("the pane plugin is not shipped in this tree")
    assert pane.plugin_dir() is None and not pulsar_home.exists()        # no home yet: no pane, nothing made
    K.ensure_home()
    d = pane.plugin_dir()
    assert d == pulsar_home / "cache" / "fnplug" and d != pane.package_plugin_dir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link_home = tmp_path / "linked-home"
    link_home.mkdir()
    try:
        (link_home / "cache").symlink_to(elsewhere)
    except (OSError, NotImplementedError):
        return
    monkeypatch.setenv("PULSAR_HOME", str(link_home))
    assert pane.plugin_dir() is None and not list(elsewhere.iterdir())


def test_launch_threads_the_detected_version_and_session(monkeypatch, pulsar_home):
    from pulsar import plugin
    from pulsar.core import spawn
    from pulsar.core import state as ST
    info = ST.CoreInfo(port=4747, pid=1, version="3", admin_token="pla_x")
    monkeypatch.setattr(spawn, "attach", lambda *a, **kw: info)
    monkeypatch.setattr(L, "start_session", lambda *a, **kw: (info, ST.Session(sid="s-launch", token_sha256="h",
                                                                                 plan=[]), "pls_inner"))
    monkeypatch.setattr(L, "find_claude", lambda: "/bin/true")
    monkeypatch.setattr(L, "claude_version", lambda b: "2.1.299")
    seen = {}
    monkeypatch.setattr(L, "exec_claude", lambda binary, args, env, **k: seen.update(args=args) or 0)
    real = plugin.claude_args
    monkeypatch.setattr(plugin, "claude_args", lambda *a, **kw: seen.update(kw=kw) or real(*a, **kw))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    st = K.State()
    st.upsert(K.Profile(id="ollama", provider="ollama", model="qwen3"))
    K.save(st)
    assert cli.main(["-p", "hi"]) == 0
    assert seen["kw"]["claude_version"] == "2.1.299" and seen["kw"]["session_id"] == "s-launch"
    settings = seen["args"][seen["args"].index("--settings") + 1]
    assert settings == str(L.session_dir("s-launch") / "settings.json")


# ---------------------------------------------------------------------------
# Per-session settings; a shared, immutable plugin folder
# ---------------------------------------------------------------------------

def _settings_of(args):
    return args[args.index("--settings") + 1]


def test_concurrent_launches_get_their_own_settings(pulsar_home, monkeypatch):
    import os
    import stat
    import threading
    from pulsar import plugin
    monkeypatch.setenv("PULSAR_ATTRIBUTION", "on")          # the commit line names each session's own model
    K.ensure_home()          # the home exists before two launches race (first-run creation is config.py's)
    results = {}

    def launch(sid, model):
        results[sid] = plugin.claude_args(model, [{"model": model, "provider": "p"}], session_id=sid)
    threads = [threading.Thread(target=launch, args=("sA", "vendor/model-a")),
               threading.Thread(target=launch, args=("sB", "vendor/model-b"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    a, b = _settings_of(results["sA"]), _settings_of(results["sB"])
    assert a != b and a == str(L.session_dir("sA") / "settings.json")
    sa, sb = json.loads(open(a).read()), json.loads(open(b).read())
    assert "vendor/model-a" in sa["attribution"]["commit"] and "vendor/model-b" in sb["attribution"]["commit"]
    assert [o["model"] for o in sa["modelPicker"]["options"]] == ["vendor/model-a"]
    for p in (a, b):
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(os.path.dirname(p)).st_mode) == 0o700
    # one plugin folder for both, and each session records which one it loaded
    assert _plugin_dirs(results["sA"]) == _plugin_dirs(results["sB"])
    assert (L.session_dir("sA") / "plugin-dir").read_text().strip() == _plugin_dirs(results["sA"])[0]
    # no session (direct route without Core): the shared file
    assert _settings_of(plugin.claude_args("m")) == str(pulsar_home / "settings.json")


def test_plugin_folder_is_immutable_and_content_addressed(monkeypatch, pulsar_home):
    import os
    from pulsar import plugin
    monkeypatch.delenv("PULSAR_NOTIFY", raising=False)
    root = plugin.build_plugin()
    assert root.parent == pulsar_home / "plugin" and root.name.startswith(L.__version__ + "-")
    files = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
    assert ".mcp.json" in files and "hooks/hooks.json" in files and "commands/switch.md" in files
    stamps = {f: os.stat(str(root / f)).st_mtime_ns for f in files}
    assert plugin.build_plugin() == root
    assert {f: os.stat(str(root / f)).st_mtime_ns for f in files} == stamps        # never rewritten
    # other content (notifications off) gets its own folder; the first one is untouched
    monkeypatch.setenv("PULSAR_NOTIFY", "off")
    other = plugin.build_plugin()
    assert other != root and "Stop" not in json.loads((other / "hooks/hooks.json").read_text())["hooks"]
    assert "Stop" in json.loads((root / "hooks/hooks.json").read_text())["hooks"]
    # a damaged copy is repaired with the same bytes
    (other / "commands" / "stage.md").unlink()
    assert plugin.build_plugin() == other and (other / "commands" / "stage.md").is_file()
    assert not [p for p in (pulsar_home / "plugin").iterdir() if p.name.startswith(".build-")]


def test_plugin_copies_are_collected_only_when_unused(monkeypatch, pulsar_home):
    import os
    import time as _t
    from pulsar import plugin
    from pulsar.core import state as ST
    root = plugin.build_plugin()
    base = root.parent
    old, used = base / "2.9.0-0123456789ab", base / "2.9.1-0123456789ab"
    for d in (old, used):
        (d / "hooks").mkdir(parents=True)
        (d / "hooks" / "hooks.json").write_text("{}")
        week = _t.time() - 7 * 86400
        os.utime(str(d), (week, week))
    legacy = base / ".mcp.json"
    legacy.write_text("{}")
    # a live session that loaded `used`
    sess, _ = ST.new_session([], {}, label="x")
    L.session_dir(sess.sid).mkdir(parents=True, exist_ok=True)
    (L.session_dir(sess.sid) / "plugin-dir").write_text(str(used))
    removed = plugin.collect_plugins(base, keep=root)
    assert removed == [old.name] and used.is_dir() and legacy.exists() and root.is_dir()
    # once no session runs, the old copy and the pre-3.0 flat layout go too
    ST.session_path(sess.sid).unlink()
    assert sorted(plugin.collect_plugins(base, keep=root)) == sorted([used.name, ".mcp.json"])
    assert root.is_dir() and not used.exists() and not legacy.exists()


def test_session_folders_of_dead_sessions_are_reaped(pulsar_home):
    import os
    import time as _t
    from pulsar.core import state as ST
    live, _ = ST.new_session([], {}, label="live")                          # pid = this process
    dead, _ = ST.new_session([], {}, label="dead")
    dead.pid, dead.created = 0, _t.time() - 3600
    ST.save_session(dead)
    young, _ = ST.new_session([], {}, label="young")
    young.pid = 0
    ST.save_session(young)                                                 # dead pid but within the grace
    for s in (live, dead, young):
        (L.session_dir(s.sid)).mkdir(parents=True, exist_ok=True)
        (L.session_dir(s.sid) / "settings.json").write_text("{}")
    orphan = ST.sessions_dir() / "s0123456789ab"
    orphan.mkdir()
    old = _t.time() - 3600
    os.utime(str(orphan), (old, old))
    fresh_orphan = ST.sessions_dir() / "sfresh"
    fresh_orphan.mkdir()
    removed = L.reap_session_dirs()
    assert sorted(removed) == sorted([dead.sid, orphan.name])
    assert L.session_dir(live.sid).is_dir() and L.session_dir(young.sid).is_dir() and fresh_orphan.is_dir()
    assert ST.session_path(live.sid).exists()                             # session files are Core's to reap


# ---------------------------------------------------------------------------
# start_session: env-only keys, and Core proven again before Claude Code gets its URL
# ---------------------------------------------------------------------------

FAKE_GROQ = "fake-groq-ENVONLY-not-a-key"
FAKE_NIM = "fake-nim-ENVONLY-not-a-key"


def _two_profiles():
    st = K.State()
    st.upsert(K.Profile(id="nvidia", provider="nvidia", model="a"))
    st.upsert(K.Profile(id="groq", provider="groq", model="b"))
    st.active = "nvidia"
    K.save(st)
    return st


def _core(port, token):
    from pulsar.core import state as ST
    return ST.CoreInfo(port=port, pid=1, version=L.__version__, admin_token=token)


def test_start_session_warns_once_when_core_does_not_take_an_env_key(monkeypatch):
    from pulsar.core import spawn
    from pulsar.core.client import CoreClient, CoreUnavailable
    st = _two_profiles()
    info = _core(4801, "pla_one")
    monkeypatch.setattr(spawn, "ensure_core", lambda *a, **kw: info)
    monkeypatch.setattr(spawn, "attach", lambda *a, **kw: info)
    posts = []

    def post(self, path, body, timeout=30.0):
        posts.append((self.base, body["profile"]))
        if body["profile"] == "groq":
            raise CoreUnavailable("HTTP 500: echo " + body["key"], status=500)
        return {"ok": True}
    monkeypatch.setattr(CoreClient, "post", post)
    monkeypatch.setattr(L.time, "sleep", lambda s: None)
    warnings = []
    keys = {"nvidia": FAKE_NIM, "groq": FAKE_GROQ}
    got, sess, token = L.start_session(st, st.profiles["nvidia"], keys.get, warn=warnings.append)
    assert got == info and token.startswith("pls_")
    assert posts.count((info.url, "groq")) == 2 and posts.count((info.url, "nvidia")) == 1   # one retry
    assert len(warnings) == 1 and "groq" in warnings[0] and "Relays to groq" in warnings[0]
    assert FAKE_GROQ not in warnings[0] and FAKE_NIM not in warnings[0]
    # nothing failed: no warning at all
    monkeypatch.setattr(CoreClient, "post", lambda self, path, body, timeout=30.0: {"ok": True})
    warnings.clear()
    L.start_session(st, st.profiles["nvidia"], keys.get, warn=warnings.append)
    assert warnings == []


def test_start_session_reverifies_core_after_registering(monkeypatch):
    from pulsar.core import spawn
    from pulsar.core.client import CoreClient
    st = _two_profiles()
    first, second = _core(4801, "pla_one"), _core(4802, "pla_two")
    monkeypatch.setattr(spawn, "ensure_core", lambda *a, **kw: first)
    posts = []
    monkeypatch.setattr(CoreClient, "post", lambda self, path, body, timeout=30.0: posts.append(self.base) or {})
    # Core was replaced meanwhile (ours exited, a peer launch started another): use the proven new one
    monkeypatch.setattr(spawn, "attach", lambda *a, **kw: second)
    keys = {"nvidia": FAKE_NIM, "groq": FAKE_GROQ}
    got, _, _ = L.start_session(st, st.profiles["nvidia"], keys.get, warn=lambda m: None)
    assert got == second and posts.count(second.url) == 2          # env keys handed to the new Core too
    # nothing proven on run/core.json: start or attach one on the same port; none can be had: refuse
    calls = []

    def ensure(*a, **kw):
        calls.append(kw)
        if len(calls) > 1:
            raise RuntimeError("the process on port 4801 is not this user's Pulsar Core")
        return first
    monkeypatch.setattr(spawn, "ensure_core", ensure)
    monkeypatch.setattr(spawn, "attach", lambda *a, **kw: None)
    with pytest.raises(RuntimeError):
        L.start_session(st, st.profiles["nvidia"], keys.get, warn=lambda m: None)
    assert calls[1] == {"prefer_port": 4801}


def test_a_session_records_its_own_plain_http_choice(monkeypatch):
    from pulsar.core import spawn
    from pulsar.core import state as ST
    st = _two_profiles()
    info = _core(4801, "pla_one")
    monkeypatch.setattr(spawn, "ensure_core", lambda *a, **kw: info)
    monkeypatch.setattr(spawn, "attach", lambda *a, **kw: info)
    monkeypatch.delenv("PULSAR_ALLOW_HTTP", raising=False)
    _, sess, _ = L.start_session(st, st.profiles["nvidia"], {}.get, warn=lambda m: None)
    assert sess.flags["allow_http"] is False
    # this launch's PULSAR_ALLOW_HTTP=1, whatever the environment Core was started with
    monkeypatch.setenv("PULSAR_ALLOW_HTTP", "1")
    _, sess, _ = L.start_session(st, st.profiles["nvidia"], {}.get, warn=lambda m: None)
    assert sess.flags["allow_http"] is True and ST.load_session(sess.sid).flags["allow_http"] is True


def test_a_session_records_the_route_claude_code_takes(monkeypatch):
    from pulsar.core import spawn
    st = _two_profiles()
    info = _core(4801, "pla_one")
    monkeypatch.setattr(spawn, "ensure_core", lambda *a, **kw: info)
    monkeypatch.setattr(spawn, "attach", lambda *a, **kw: info)
    _, sess, _ = L.start_session(st, st.profiles["nvidia"], {}.get, warn=lambda m: None)
    assert sess.flags["route"] == "core" and not sess.side_channel
    # Claude Code talks to its provider itself: Core grants the token only the side channel
    _, sess, _ = L.start_session(st, st.profiles["nvidia"], {}.get, warn=lambda m: None, route="direct")
    assert sess.flags["route"] == "direct" and sess.side_channel


def test_stop_core_passes_force_through(monkeypatch):
    from pulsar.core import spawn
    seen = []
    monkeypatch.setattr(spawn, "stop_core", lambda force=False: seen.append(force) or True)
    assert L.stop_core() is True and L.stop_core(force=True) is True
    assert seen == [False, True]                   # the default retires Core: live sessions keep its port


def test_start_session_reaps_dead_session_folders(monkeypatch):
    from pulsar.core import spawn
    st = _two_profiles()
    info = _core(4801, "pla_one")
    monkeypatch.setattr(spawn, "ensure_core", lambda *a, **kw: info)
    monkeypatch.setattr(spawn, "attach", lambda *a, **kw: info)
    reaped = []
    monkeypatch.setattr(L, "reap_session_dirs", lambda *a, **kw: reaped.append(1) or [])
    L.start_session(st, st.profiles["nvidia"], {}.get, warn=lambda m: None)
    assert reaped == [1]


# ---------------------------------------------------------------------------
# Windows: an npm claude.cmd never lets cmd.exe read arguments; WSL ignores Windows' claude
# ---------------------------------------------------------------------------

NPM_SHIM = r'''@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0

IF EXIST "%dp0%\node.exe" (
  SET "_prog=%dp0%\node.exe"
) ELSE (
  SET "_prog=node"
  SET PATHEXT=%PATHEXT:;.JS;=;%
)

endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\node_modules\@anthropic-ai\claude-code\cli.js" %*
'''


def _npm_layout(tmp_path, with_cli=True, with_node=True):
    npm = tmp_path / "npm"
    npm.mkdir()
    (npm / "claude.cmd").write_text(NPM_SHIM)
    if with_node:
        (npm / "node.exe").write_text("")
    if with_cli:
        cli_js = npm / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
        cli_js.parent.mkdir(parents=True)
        cli_js.write_text("")
    return npm


def test_windows_npm_cmd_runs_node_directly(monkeypatch, tmp_path):
    monkeypatch.setattr(L, "_nt", lambda: True)
    npm = _npm_layout(tmp_path)
    argv = L.claude_argv(str(npm / "claude.cmd"), ["-p", "fix&calc", "50%PATH%"])
    assert argv == [str(npm / "node.exe"), str(npm / "node_modules/@anthropic-ai/claude-code/cli.js"),
                    "-p", "fix&calc", "50%PATH%"]


def test_windows_cmd_only_refuses_metacharacters(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(L, "_nt", lambda: True)
    monkeypatch.setenv("PATH", "")
    npm = _npm_layout(tmp_path, with_cli=False)
    cmd = str(npm / "claude.cmd")
    for bad in ("fix&calc", "a|b", "x^y", "50%PATH%", "<in", "out>", 'say "hi"', "line\nnext"):
        with pytest.raises(ValueError) as exc:
            L.claude_argv(cmd, ["-p", bad])
        assert "cmd.exe" in str(exc.value) and "Nothing was run" in str(exc.value)
    assert L.claude_argv(cmd, ["-p", "explain this repo", "--continue"]) == [cmd, "-p", "explain this repo",
                                                                                 "--continue"]
    ran = []
    monkeypatch.setattr(L, "run_child", lambda argv, env: ran.append(argv) or 0)
    assert L.exec_claude(cmd, ["-p", "a&b"], {}) == 2 and ran == []
    assert "cmd.exe" in capsys.readouterr().err
    assert L.exec_claude(cmd, ["-p", "ok"], {}) == 0 and ran == [[cmd, "-p", "ok"]]


def test_windows_prefers_the_native_claude_exe(monkeypatch, tmp_path):
    monkeypatch.setattr(L, "_nt", lambda: True)
    monkeypatch.delenv("PULSAR_CLAUDE_BIN", raising=False)
    npm = _npm_layout(tmp_path)
    native = tmp_path / "native"
    native.mkdir()
    (native / "claude.exe").write_text("")
    monkeypatch.setattr(L.Path, "home", classmethod(lambda cls: tmp_path / "nohome"))
    monkeypatch.setenv("PATH", os.pathsep.join([str(npm), str(native)]))
    assert L.find_claude() == str(native / "claude.exe")
    monkeypatch.setenv("PATH", str(npm))
    assert L.find_claude() == str(npm / "claude.cmd")
    # an exe that is just the shim's node is never mistaken for Claude Code
    shim_exe_only = tmp_path / "only"
    shim_exe_only.mkdir()
    (shim_exe_only / "claude.cmd").write_text(NPM_SHIM)
    (shim_exe_only / "node.exe").write_text("")
    with pytest.raises(ValueError):
        L.claude_argv(str(shim_exe_only / "claude.cmd"), ["a&b"])


def test_claude_lookup_never_uses_relative_path_entries(monkeypatch, tmp_path):
    monkeypatch.delenv("PULSAR_CLAUDE_BIN", raising=False)
    proj = tmp_path / "proj"
    proj.mkdir()
    evil = proj / "claude"
    evil.write_text("#!/bin/sh\necho owned\n")
    evil.chmod(0o755)
    monkeypatch.chdir(str(proj))
    monkeypatch.setattr(L.Path, "home", classmethod(lambda cls: tmp_path / "nohome"))
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", "bin-rel", "/nonexistent-pulsar-test"]))
    found = L.find_claude()
    assert found is None or os.path.realpath(found) != os.path.realpath(str(evil))


def test_wsl_ignores_a_windows_claude_and_says_how_to_install_linux(monkeypatch, tmp_path, capsys):
    import re as _re
    monkeypatch.delenv("PULSAR_CLAUDE_BIN", raising=False)
    mnt = tmp_path / "mnt" / "c" / "Users" / "me" / "AppData" / "Roaming" / "npm"
    mnt.mkdir(parents=True)
    win = mnt / "claude"
    win.write_text("#!/bin/sh\n")
    win.chmod(0o755)
    monkeypatch.setattr(L, "_WINDOWS_DRIVE", _re.compile("^" + _re.escape(str(tmp_path / "mnt")) + "/[a-z](/|$)"))
    monkeypatch.setattr(L.Path, "home", classmethod(lambda cls: tmp_path / "nohome"))
    monkeypatch.setenv("PATH", os.pathsep.join(["/nonexistent-pulsar-test", str(mnt)]))
    monkeypatch.setattr(L, "is_wsl", lambda environ=None: False)
    assert L.find_claude() == str(win)                    # plain Linux: a claude is a claude
    monkeypatch.setattr(L, "is_wsl", lambda environ=None: True)
    assert L.find_claude() is None and L.wsl_windows_claude() == str(win)
    linux = tmp_path / "linuxbin"
    linux.mkdir()
    (linux / "claude").write_text("#!/bin/sh\n")
    (linux / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(mnt), str(linux)]))
    assert L.find_claude() == str(linux / "claude")
    # the launcher explains instead of using it
    monkeypatch.setenv("PATH", str(mnt))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert cli.main(["-p", "hi"]) == 127
    err = capsys.readouterr()
    text = err.out + err.err
    # the path is shown through safe(..., 120): long temp paths (macOS runners) are shortened there
    assert "WSL" in text and "curl -fsSL https://claude.ai/install.sh | bash" in text
    assert cli.safe(str(win), 120) in text


def test_is_wsl_detection(monkeypatch):
    monkeypatch.setattr(L, "_nt", lambda: False)
    monkeypatch.setattr(L.sys, "platform", "linux")
    assert L.is_wsl({"WSL_DISTRO_NAME": "Ubuntu"}) and L.is_wsl({"WSL_INTEROP": "/run/WSL/1_interop"})
    monkeypatch.setattr(L.sys, "platform", "darwin")
    assert not L.is_wsl({"WSL_DISTRO_NAME": "Ubuntu"})


def test_many_launches_building_the_plugin_at_once_agree(pulsar_home):
    import threading
    from pulsar import plugin
    K.ensure_home()
    roots, errors = [], []

    def build():
        try:
            roots.append(plugin.build_plugin())
        except Exception as exc:          # pragma: no cover - reported below
            errors.append(exc)
    threads = [threading.Thread(target=build) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(set(roots)) == 1
    root = roots[0]
    assert all((root / rel).read_text(encoding="utf-8") == text for rel, text in plugin.plugin_files().items())
    assert [p.name for p in (pulsar_home / "plugin").iterdir()] == [root.name]       # no stray temp folders


# ---------------------------------------------------------------------------
# Direct and cloud sessions: a side channel only (the session token never drives /v1/messages)
# ---------------------------------------------------------------------------

def _direct_state():
    st = K.State()
    st.upsert(K.Profile(id="deepseek", provider="deepseek", model="deepseek-chat"))
    st.upsert(K.Profile(id="openai", provider="openai", model="gpt-4o"))
    st.active = "deepseek"
    K.save(st)
    return st


def _fake_core(monkeypatch):
    from pulsar.core import spawn
    from pulsar.core.client import CoreClient
    info = _core(4801, "pla_one")
    monkeypatch.setattr(spawn, "ensure_core", lambda *a, **kw: info)
    monkeypatch.setattr(spawn, "attach", lambda *a, **kw: info)
    handed = []
    monkeypatch.setattr(CoreClient, "post", lambda self, path, body, timeout=30.0: handed.append(body["profile"]) or {})
    return handed


def test_default_route_matches_the_launcher(monkeypatch):
    deepseek = K.Profile(id="deepseek", provider="deepseek", model="m")
    monkeypatch.delenv("PULSAR_DIRECT", raising=False)
    assert L.default_route(deepseek) == "core"
    assert L.default_route(deepseek, {"PULSAR_DIRECT": "1"}) == "direct"
    assert L.default_route(K.Profile(id="groq", provider="groq", model="m"), {"PULSAR_DIRECT": "1"}) == "core"
    assert L.default_route(K.Profile(id="bedrock", provider="bedrock", model="sonnet")) == "cloud"


@pytest.mark.parametrize("route", ["direct", "cloud", "core"])
def test_direct_session_token_is_refused_on_the_gateway(monkeypatch, pulsar_home, route):
    import httpx
    from starlette.testclient import TestClient

    from pulsar.core import state as ST
    from pulsar.core.server import make_app
    st = _direct_state()
    if route == "cloud":
        st.upsert(K.Profile(id="bedrock", provider="bedrock", model="sonnet"))
        st.active = "bedrock"
    handed = _fake_core(monkeypatch)
    keys = {"deepseek": "sk-fake-deepseek-not-real-0000", "openai": "sk-proj-fake-key-not-real-0000",
            "bedrock": "ABSK-fake-not-real-0000"}
    if route == "direct":
        monkeypatch.setenv("PULSAR_DIRECT", "1")                  # the default route, as cli.cmd_launch decides it
        _, sess, token = L.start_session(st, st.profiles["deepseek"], keys.get, warn=lambda m: None)
    else:
        monkeypatch.delenv("PULSAR_DIRECT", raising=False)
        _, sess, token = L.start_session(st, st.current(), keys.get, warn=lambda m: None, route=route)
    assert sess.flags["route"] == route
    calls = []
    up = lambda request: calls.append(request.url.host) or httpx.Response(500)   # noqa: E731
    c = TestClient(make_app(ST.ADMIN_TOKEN_PREFIX + "admin-test", 0, transport=httpx.MockTransport(up)))
    hdr = {"authorization": "Bearer " + token, "anthropic-version": "2023-06-01"}
    body = {"model": "gpt-4o", "max_tokens": 16, "stream": False, "messages": [{"role": "user", "content": "hi"}]}
    if route == "core":
        assert [t.profile for t in sess.plan] == ["deepseek", "openai"] and sess.flags["relay"] is True
        return
    assert [t.profile for t in sess.plan] == [st.current().id]   # the primary only: no relay to other keys
    assert sess.flags["relay"] is False and sess.side_channel
    assert c.post("/v1/messages", json=body, headers=hdr).status_code == 403
    assert c.post("/v1/messages/count_tokens", json=body, headers=hdr).status_code == 403
    assert c.get("/pulsar/stats", headers=hdr).status_code == 200
    assert calls == []
    assert "openai" not in handed and ("bedrock" not in handed)   # no other key, and none for a cloud target


def test_relay_plan_skips_a_placeholder_azure_profile(pulsar_home):
    st = K.State()
    st.upsert(K.Profile(id="nvidia", provider="nvidia", model="a"))
    st.upsert(K.Profile(id="azure", provider="azure", model="gpt-4o"))                      # never got a URL
    st.upsert(K.Profile(id="azure-2", provider="azure", model="gpt-4o",
                        base_url="https://YOUR-RESOURCE.openai.azure.com/openai/v1"))
    st.upsert(K.Profile(id="groq", provider="groq", model="b"))
    st.active = "nvidia"
    keys = {"nvidia": "nv", "azure": "fake-azure-not-real", "azure-2": "fake-azure-not-real", "groq": "gsk"}
    assert [t.profile for t in L.relay_plan(st, st.profiles["nvidia"], keys.get)] == ["groq"]
    st.prefs["relay"] = ["azure:gpt-4o", "azure-2:gpt-4o", "groq:b"]
    assert [t.profile for t in L.relay_plan(st, st.profiles["nvidia"], keys.get)] == ["groq"]
    # a real resource URL relays as before
    st.upsert(K.Profile(id="azure", provider="azure", model="gpt-4o",
                        base_url="https://contoso.openai.azure.com/openai/v1"))
    assert [t.profile for t in L.relay_plan(st, st.profiles["nvidia"], keys.get)] == ["azure", "groq"]
    assert L.placeholder_url(P.get("foundry")) and not L.placeholder_url(P.get("groq"))
