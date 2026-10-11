# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""The context window Claude Code works with for a Pulsar model (pulsar.launch, pulsar.plugin).

Claude Code handles every Pulsar model as BEHAVES_AS (claude-sonnet-4-5): 200k, or 1M with "[1m]".
Below 200k the auto-compact variables carry the real window. tools/compat/run.py checks the result
against the real Claude Code binary (context_effective); these tests pin the arithmetic.
"""

import pytest

from pulsar import launch as L
from pulsar import plugin
from pulsar import providers as P


def core():
    from pulsar.core import state as ST
    return ST.CoreInfo(port=4123, pid=1, version="x", admin_token="pla_admin")


def session():
    from pulsar.core import state as ST
    return ST.Session(sid="s1", token_sha256="h", plan=[])


def core_env(context, model="vendor/model-x", base=None):
    return L.build_env(P.get("nvidia"), "nvapi-x", model, context, core(), session(), "pls_tok", base=base or {})


def claude_threshold(env, reserve=L.OUTPUT_RESERVE, reported=L.CLAUDE_WINDOW):
    """Where Claude Code 2.1.288 auto-compacts given these variables (its own rule, see tools/compat/run.py)."""
    window = min(reported, max(L.COMPACT_FLOOR, int(env.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") or reported)))
    base = window - reserve
    threshold = base - L.COMPACT_BUFFER
    pct = float(env.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE") or 0)
    if 0 < pct <= 100:
        threshold = min(int(base * pct / 100), threshold)
    return threshold


# --- the model id: [1m] for 1M windows --------------------------------------------------------------------

@pytest.mark.parametrize("model, context, long_ok, want", [
    ("kimi-k2", 1_000_000, True, "kimi-k2[1m]"),
    ("vendor/model-x", 2_000_000, True, "vendor/model-x[1m]"),       # above 1M: used as 1M, never more
    ("kimi-k2", 999_999, True, "kimi-k2"),
    ("kimi-k2", 262_144, True, "kimi-k2"),
    ("kimi-k2", 1_000_000, False, "kimi-k2"),                          # Claude Code talks to the provider itself
    ("claude-sonnet-4-5", 1_000_000, True, "claude-sonnet-4-5"),       # Claude Code knows Claude's windows
    ("kimi-k2[1m]", 1_000_000, True, "kimi-k2[1m]"),
    ("Kimi-K2[1M]", 1_000_000, True, "Kimi-K2[1M]"),
    ("", 1_000_000, True, ""),
])
def test_claude_model_id(model, context, long_ok, want):
    assert L.claude_model_id(model, context, long_ok) == want


def test_core_route_gives_1m_models_the_1m_id_everywhere():
    env = core_env(1_048_576)
    assert {env[v] for v in L.MODEL_VARS} == {"vendor/model-x[1m]"}
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "1048576"
    assert not set(L.COMPACT_VARS) & set(env) and L.COMPACT_MARK not in env


def test_direct_route_never_gets_the_1m_id():
    env = L.build_env(P.get("deepseek"), "sk-ds", "deepseek-chat", 1_000_000, None, base={})
    assert env["ANTHROPIC_MODEL"] == "deepseek-chat"


def test_core_clean_model_strips_the_suffix_again():
    from pulsar.core import engine
    assert engine.clean_model("vendor/model-x[1m]") == "vendor/model-x"


# --- below 200k: auto-compact at the real window ------------------------------------------------------------

@pytest.mark.parametrize("context", [200_000, 262_144, 1_000_000, 0])
def test_no_compact_tuning_at_or_above_claude_window(context):
    assert L.compact_env(context) == {}


def test_compact_env_values():
    assert L.compact_env(131_072) == {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "131072",
                                      "CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE": "108072"}
    assert L.compact_env(65_536) == {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "100000",
                                     "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "55.29",
                                     "CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE": "50381"}
    assert L.compact_env(32_768) == {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "100000",
                                     "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "37.5",
                                     "CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE": "31384"}


# Claude Code's fixed prompt prefix (system prompt and tools) measured through Pulsar with 2.1.288: the first
# two requests of a session were 26563 and 28430 tokens, its own estimate of the prefix ~23684.
REAL_PREFIX = 26_563
SECOND_TURN = 28_430


@pytest.mark.parametrize("context", [32_000, 32_768, 40_960, 49_152, 65_536])
@pytest.mark.parametrize("reserve", [L.OUTPUT_RESERVE, 8192, 4096])
def test_small_windows_stay_above_claude_codes_fixed_prefix(context, reserve):
    # Regression: for 32768 compaction sat at 20480 and the blocking limit at 23552, under the prefix, so a
    # 32k model failed after its first turn ("compaction cannot help", then "Prompt is too long").
    env = L.compact_env(context, {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(reserve)} if reserve != L.OUTPUT_RESERVE
                        else {})
    threshold = claude_threshold(env, reserve)
    blocking = int(env["CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE"])
    assert SECOND_TURN < threshold < blocking < context
    assert threshold >= L.PREFIX_ROOM * 0.99 and L.PREFIX_ROOM > REAL_PREFIX   # pct is rounded down


def test_windows_too_small_for_the_prefix_are_left_untuned():
    for context in (4096, 16_384, 31_999):
        assert L.compact_env(context) == {}
        assert L.COMPACT_MARK not in core_env(context)


@pytest.mark.parametrize("context", [32_000, 32_768, 65_536, 99_999, 100_000, 103_000,
                                     128_000, 131_072, 196_608, 199_999])
@pytest.mark.parametrize("reserve", [L.OUTPUT_RESERVE, 8192, 4096])
def test_claude_never_compacts_or_sends_past_the_window(context, reserve):
    env = L.compact_env(context, {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(reserve)} if reserve != L.OUTPUT_RESERVE
                        else {})
    threshold = claude_threshold(env, reserve)
    want = max(context - min(reserve, context // 5) - min(L.COMPACT_BUFFER, context // 8), L.PREFIX_ROOM)
    assert want * 0.99 <= threshold <= want
    assert threshold < int(env["CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE"]) < context


def test_answer_reserve_shrinks_with_the_window_and_is_whole_from_100k():
    assert L.compact_limits(65_536) == (65_536 - 13_107 - 8192, 65_536 - 13_107 - 2048)
    assert L.compact_limits(100_000) == (100_000 - 20_000 - 12_500, 100_000 - 20_000 - 3000)
    assert L.compact_limits(150_000) == (150_000 - 20_000 - 13_000, 150_000 - 20_000 - 3000)


def test_claude_reserve_larger_than_assumed_still_compacts_in_time():
    env = L.compact_env(65_536, {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "4096"})
    assert claude_threshold(env, reserve=L.OUTPUT_RESERVE) < claude_threshold(env, reserve=4096) <= 65_536


def test_users_own_safer_values_are_kept_and_larger_ones_capped():
    mine = L.compact_env(65_536, {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "500000",
                                  "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "30",
                                  "CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE": "40000"})
    assert mine == {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "100000", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "30",
                    "CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE": "40000"}
    odd = L.compact_env(131_072, {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "junk",
                                  "CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE": "-5"})
    assert odd == L.compact_env(131_072)
    assert L.compact_env(131_072, {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "120000"})[
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "120000"
    pct = L.compact_env(65_536, {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "nan"})
    assert pct["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "55.29"


def test_build_env_sets_and_marks_the_compact_variables():
    env = core_env(65_536)
    assert env["ANTHROPIC_MODEL"] == "vendor/model-x"
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "100000"
    assert env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "55.29"
    assert env["CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE"] == "50381"
    assert env[L.COMPACT_MARK] == ",".join(sorted(L.COMPACT_VARS))
    assert core_env(None)["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == str(L.DEFAULT_CONTEXT)  # unknown: 128k


def test_nested_launch_drops_the_outer_sessions_tuning():
    outer = dict(core_env(32_768), PATH="/bin")
    inner = core_env(1_000_000, base=outer)
    assert not set(L.COMPACT_VARS) & set(inner) and L.COMPACT_MARK not in inner
    assert inner["ANTHROPIC_MODEL"] == "vendor/model-x[1m]"
    again = core_env(131_072, base=outer)
    assert again["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "131072"      # not the outer 100000
    # A value the user exported (no marker) is theirs and stays when it is the safer one.
    users = core_env(131_072, base={"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "110000"})
    assert users["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "110000"
    cloud = L.build_env(P.get("anthropic"), "sk-ant-x", "", 0, None, base=outer)
    assert not set(L.COMPACT_VARS) & set(cloud)


# --- the /model picker ------------------------------------------------------------------------------------

ROWS = [{"model": "vendor/big", "provider": "V", "context": 1_000_000},
        {"model": "vendor/mid", "provider": "V", "context": 262_144},
        {"model": "vendor/small", "provider": "V", "context": "65536"},
        {"model": "vendor/odd", "provider": "V", "context": "n/a"}]


def test_picker_rows_carry_the_window_and_keep_behaves_as():
    rows = plugin.model_picker(ROWS)["options"]
    assert [r["model"] for r in rows] == ["vendor/big[1m]", "vendor/mid", "vendor/small", "vendor/odd"]
    assert all(r["behavesAs"] == plugin.BEHAVES_AS for r in rows)
    assert rows[0]["label"] == "◉ big" and "1M ctx" in rows[0]["description"]
    assert [r["model"] for r in plugin.model_picker(ROWS, long_ok=False)["options"]][0] == "vendor/big"


def test_settings_only_use_the_1m_id_on_the_core_route(monkeypatch):
    monkeypatch.delenv("PULSAR_DIRECT", raising=False)
    assert plugin.settings("m", ROWS)["modelPicker"]["options"][0]["model"] == "vendor/big[1m]"
    assert plugin.settings("m", ROWS, anthropic=True)["modelPicker"]["options"][0]["model"] == "vendor/big"
    monkeypatch.setenv("PULSAR_DIRECT", "1")
    assert plugin.settings("m", ROWS)["modelPicker"]["options"][0]["model"] == "vendor/big"
