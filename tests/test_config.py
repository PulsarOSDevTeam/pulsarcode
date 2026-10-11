# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
import json
import os
import stat
from pathlib import Path

import pytest

from pulsar import config as K


def test_state_roundtrip_and_permissions(pulsar_home):
    st = K.load()
    assert st.profiles == {}
    st.upsert(K.Profile(id="groq", provider="groq"))
    st.set_model("groq", "openai/gpt-oss-120b", 128000)
    K.save(st)
    K.set_key("groq", "gsk_secret")
    again = K.load()
    assert again.active == "groq" and again.current().model == "openai/gpt-oss-120b"
    assert again.recent[0] == {"profile": "groq", "model": "openai/gpt-oss-120b"}
    assert K.get_key("groq") == "gsk_secret"
    for f in ("config.json", "keys.json"):
        assert os.stat(pulsar_home / f).st_mode & 0o777 == 0o600
    assert os.stat(pulsar_home).st_mode & 0o777 == 0o700


def test_recent_dedupes_and_caps():
    st = K.State()
    st.upsert(K.Profile(id="a", provider="nvidia"))
    for i in range(30):
        st.push_recent("a", "m%d" % (i % 25))
    st.push_recent("a", "m3")
    assert st.recent[0]["model"] == "m3"
    assert len(st.recent) == K.RECENT_CAP
    assert len({r["model"] for r in st.recent}) == K.RECENT_CAP


def test_upsert_keeps_model_and_remove_switches_active():
    st = K.State()
    st.upsert(K.Profile(id="a", provider="nvidia", model="x"))
    st.upsert(K.Profile(id="a", provider="nvidia"))
    assert st.profiles["a"].model == "x"
    st.upsert(K.Profile(id="b", provider="groq"))
    st.remove("b")
    assert st.active == "a"


def test_custom_profile_resolves():
    p = K.Profile(id="custom-x", provider="custom", base_url="https://x.example/v1")
    prov = p.resolve()
    assert prov.base_url == "https://x.example/v1" and prov.route == "adapter"
    assert p.label == "x.example"
    p = K.Profile(id="ollama", provider="ollama", base_url="http://10.0.0.5:11434/v1")
    assert p.resolve().base_url == "http://10.0.0.5:11434/v1"


def test_stored_base_urls_never_carry_credentials_a_query_or_a_fragment(pulsar_home):
    """config.json can be edited by hand: what `pulsarcode key` never saves is dropped when it is read, and the
    next save writes the clean URL."""
    K.ensure_home()
    K.config_path().write_text(json.dumps({"version": 2, "active": "custom-llm-example-com", "profiles": {
        "custom-llm-example-com": {"id": "custom-llm-example-com", "provider": "custom",
                                   "base_url": "https://alice:s3cret-pass@llm.example.com/v1?api_key=SECRET#frag"},
        "openai": {"id": "openai", "provider": "openai", "base_url": "https://tok-SECRET@gw.example.com/v1?"},
        "vllm": {"id": "vllm", "provider": "vllm", "base_url": "http://127.0.0.1:8000/v1"},
        "odd": {"id": "odd", "provider": "custom", "base_url": 42}}}))
    st = K.load()
    assert st.profiles["custom-llm-example-com"].base_url == "https://llm.example.com/v1"
    assert st.profiles["custom-llm-example-com"].resolve().base_url == "https://llm.example.com/v1"
    assert st.profiles["openai"].base_url == "https://gw.example.com/v1"
    assert st.profiles["vllm"].base_url == "http://127.0.0.1:8000/v1"             # a clean URL stays as it was
    assert st.profiles["odd"].base_url == ""
    K.save(st)
    text = K.config_path().read_text()
    assert "SECRET" not in text and "s3cret" not in text and "alice" not in text


def test_v1_migration(pulsar_home):
    pulsar_home.mkdir(parents=True)
    (pulsar_home / "nim.key").write_text("nvapi-legacy\n")
    (pulsar_home / "active_model").write_text("nim-qwen-qwen3-coder-480b-a35b-instruct\n")
    (pulsar_home / "model_history").write_text("nim-qwen-qwen3-coder-480b-a35b-instruct\nnim-kimi\nnim-bogus\n")
    st = K.load()
    assert st.active == "nvidia"
    assert st.current().model == "qwen/qwen3-coder-480b-a35b-instruct"
    assert K.get_key("nvidia") == "nvapi-legacy"
    models = [r["model"] for r in st.recent]
    assert models[0] == "qwen/qwen3-coder-480b-a35b-instruct" and "moonshotai/kimi-k2.6" in models
    assert json.loads((pulsar_home / "config.json").read_text())["version"] == 2
    assert K.load().current().model == "qwen/qwen3-coder-480b-a35b-instruct"  # idempotent


def test_corrupt_files_are_survivable(pulsar_home):
    pulsar_home.mkdir(parents=True)
    (pulsar_home / "config.json").write_text("{not json")
    (pulsar_home / "keys.json").write_text("[]")
    assert K.load().profiles == {}
    assert K.load_keys() == {}


# ---------------------------------------------------------------------------
# where the home is, and which folders may become one
# ---------------------------------------------------------------------------

def test_empty_pulsar_home_means_the_default(monkeypatch):
    monkeypatch.setenv("PULSAR_HOME", "")
    assert K.home() == Path(os.path.expanduser("~/.pulsarcode"))
    assert K.home().is_absolute()


def test_relative_pulsar_home_is_refused(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PULSAR_HOME", "relhome")
    with pytest.raises(K.HomeError) as ei:
        K.ensure_home()
    assert "relative" in str(ei.value)
    assert os.listdir(str(tmp_path)) == []


def test_a_folder_of_common_names_is_never_claimed(monkeypatch, tmp_path):
    other = tmp_path / "othertool"
    (other / "logs").mkdir(parents=True)
    (other / "cache").mkdir()
    (other / "settings.json").write_text('{"theme": "dark"}')
    other.chmod(0o755)
    monkeypatch.setenv("PULSAR_HOME", str(other))
    with pytest.raises(K.HomeError) as ei:
        K.ensure_home()
    assert "settings.json" in str(ei.value)
    assert sorted(os.listdir(str(other))) == ["cache", "logs", "settings.json"]
    assert K.marker_kind(other) == ""
    if os.name != "nt":
        assert stat.S_IMODE(other.stat().st_mode) == 0o755


def test_only_an_empty_or_new_folder_is_created_by_pulsar(monkeypatch, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PULSAR_HOME", str(empty))
    K.ensure_home()
    assert K.marker_kind(empty) == "created"
    old = tmp_path / "older-build"                       # Pulsar state written before the marker existed
    (old / "cache").mkdir(parents=True)
    (old / "config.json").write_text('{"active": "", "profiles": {}}')
    monkeypatch.setenv("PULSAR_HOME", str(old))
    K.ensure_home()
    assert K.marker_kind(old) == "adopted"


@pytest.mark.skipif(os.name == "nt", reason="POSIX owners and modes")
def test_a_home_owned_by_someone_else_is_refused(monkeypatch, tmp_path):
    theirs = tmp_path / "theirs"
    theirs.mkdir()
    theirs.chmod(0o755)
    monkeypatch.setenv("PULSAR_HOME", str(theirs))
    me = os.getuid()
    monkeypatch.setattr(K.os, "getuid", lambda: me + 1)
    with pytest.raises(K.HomeError) as ei:
        K.ensure_home()
    assert "another user" in str(ei.value)
    assert os.listdir(str(theirs)) == [] and stat.S_IMODE(theirs.stat().st_mode) == 0o755


@pytest.mark.skipif(os.name == "nt", reason="POSIX owners and modes")
def test_a_home_that_stays_writable_by_others_is_refused(monkeypatch, tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    monkeypatch.setenv("PULSAR_HOME", str(shared))
    monkeypatch.setattr(K.os, "chmod", lambda *a, **k: None)      # a filesystem that ignores chmod
    with pytest.raises(K.HomeError) as ei:
        K.ensure_home()
    assert "writable by other users" in str(ei.value)
    assert os.listdir(str(shared)) == []


# ---------------------------------------------------------------------------
# a keyless self-hosted endpoint stays keyless
# ---------------------------------------------------------------------------

def test_keyless_custom_profile_survives_a_reload(pulsar_home):
    st = K.load()
    st.upsert(K.Profile(id="custom-192-168-1-50-8001", provider="custom", base_url="http://192.168.1.50:8001/v1",
                        needs_key=False))
    K.save(st)
    again = K.load().profiles["custom-192-168-1-50-8001"]
    assert again.needs_key is False and again.resolve().needs_key is False
    assert K.Profile(id="c", provider="custom", base_url="https://x.example/v1").resolve().needs_key is True
    raw = json.loads(K.config_path().read_text())
    del raw["profiles"]["custom-192-168-1-50-8001"]["needs_key"]          # a config written before the field
    K.config_path().write_text(json.dumps(raw))
    assert K.load().profiles["custom-192-168-1-50-8001"].resolve().needs_key is True


# ---------------------------------------------------------------------------
# v1 migration: once, said out loud, honouring NVIDIA_NIM_API_BASE
# ---------------------------------------------------------------------------

def _v1_home(pulsar_home):
    pulsar_home.mkdir(parents=True)
    (pulsar_home / "nim.key").write_text("nvapi-legacy\n")
    (pulsar_home / "active_model").write_text("nim-kimi\n")


def test_v1_migration_retires_the_key_file_and_says_so(pulsar_home, capsys):
    _v1_home(pulsar_home)
    st = K.load()
    assert st.active == "nvidia" and K.get_key("nvidia") == "nvapi-legacy"
    assert not (pulsar_home / "nim.key").exists()
    kept = pulsar_home / "nim.key.migrated"
    assert kept.read_text() == "nvapi-legacy\n"
    if os.name != "nt":
        assert stat.S_IMODE(kept.stat().st_mode) == 0o600
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1 and "v1" in err and "nim.key.migrated" in err
    assert "nvapi-legacy" not in err
    # once forgotten, a lost config.json never brings the key back
    K.set_key("nvidia", "")
    K.config_path().unlink()
    assert K.load().profiles == {} and K.get_key("nvidia") == ""
    assert K.forget_v1_key() == ["nim.key.migrated"] and not kept.exists()
    assert K.forget_v1_key() == []


def test_v1_migration_honours_nvidia_nim_api_base(pulsar_home, capsys, monkeypatch):
    monkeypatch.setenv("NVIDIA_NIM_API_BASE", "https://nim.example.com/v1/")
    _v1_home(pulsar_home)
    prof = K.load().profiles["nvidia"]
    assert prof.base_url == "https://nim.example.com/v1"
    assert prof.resolve().base_url == "https://nim.example.com/v1"
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1 and "NVIDIA_NIM_API_BASE" in err and "nim.example.com" in err


@pytest.mark.parametrize("value, want", [
    ("", ""), ("  ", ""), ("not a url", ""), ("ftp://nim.example.com/v1", ""),
    ("https://integrate.api.nvidia.com/v1", ""), ("https://integrate.api.nvidia.com/v1/", ""),
    ("http://192.168.1.9:8000/v1", "http://192.168.1.9:8000/v1"),
    ("https://user:pw@nim.example.com/v1?key=1#x", "https://nim.example.com/v1"),
    ("https://nim.example.com/v1\x1b]0;x\x07", ""),
])
def test_nim_base_override(monkeypatch, value, want):
    monkeypatch.setenv("NVIDIA_NIM_API_BASE", value)
    assert K.nim_base_override() == want


# ---------------------------------------------------------------------------
# a profile used for one launch is never saved as the default
# ---------------------------------------------------------------------------

def test_state_override_is_used_but_never_saved(pulsar_home):
    st = K.State()
    st.upsert(K.Profile(id="groq", provider="groq", model="m"))
    st.upsert(K.Profile(id="local", provider="custom", base_url="http://127.0.0.1:9/v1"), activate=False)
    st.override = "local"
    assert st.current().id == "local" and st.active == "groq"
    st.set_model("local", "qwen3", 32000)                 # choosing the local model keeps the saved default
    assert st.current().id == "local" and st.active == "groq"
    K.save(st)
    assert json.loads(K.config_path().read_text())["active"] == "groq" and "override" not in K.config_path().read_text()
    st.set_model("groq", "other")                          # an explicit choice of another profile wins
    assert st.override == "" and st.current().id == "groq"
    st.override = "local"
    st.remove("local")
    assert st.override == "" and st.current().id == "groq"
