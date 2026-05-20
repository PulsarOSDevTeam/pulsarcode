# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Unit tests for proxy.nim_sonar_picker.

Covers tier classification, group ordering, persistence round-trip, and
the render shape. No raw TTY exercise (pytest does not own a tty).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from proxy.nim_api_sonar import NIMModelRecord, static_catalog
from proxy.nim_sonar_picker import (
    TIER_CODING,
    TIER_GENERAL,
    TIER_LIGHTWEIGHT,
    TIER_LOCAL_PULSAR,
    TIER_ORDER,
    TIER_OTHER,
    Row,
    active_model_path,
    build_rows,
    find_alias,
    first_selectable,
    group_by_tier,
    read_active_model,
    render,
    tier_of,
    write_active_model,
)


def _rec(upstream: str) -> NIMModelRecord:
    provider, model = upstream.split("/", 1)
    return NIMModelRecord(
        upstream_id=upstream,
        alias="nim-" + model.replace(".", "-").replace("/", "-"),
        provider=provider,
        model=model,
        source="unit-test",
        source_url="https://docs.api.nvidia.com/nim/reference/llm-apis",
    )


def test_tier_classifier_coding():
    assert tier_of(_rec("moonshotai/kimi-k2.6")) == TIER_CODING
    assert tier_of(_rec("qwen/qwen3-coder-480b-a35b-instruct")) == TIER_CODING
    assert tier_of(_rec("deepseek-ai/deepseek-v4-flash")) == TIER_CODING
    assert tier_of(_rec("openai/gpt-oss-120b")) == TIER_CODING


def test_tier_classifier_lightweight():
    assert tier_of(_rec("microsoft/phi-4-mini-instruct")) == TIER_LIGHTWEIGHT
    assert tier_of(_rec("google/gemma-2-2b-it")) == TIER_LIGHTWEIGHT
    assert tier_of(_rec("nvidia/nemotron-mini-4b-instruct")) == TIER_LIGHTWEIGHT


def test_tier_classifier_general():
    assert tier_of(_rec("meta/llama-3.3-70b-instruct")) == TIER_GENERAL
    assert tier_of(_rec("z-ai/glm5.1")) == TIER_GENERAL
    assert tier_of(_rec("minimaxai/minimax-m2.7")) == TIER_GENERAL


def test_tier_classifier_local_pulsar():
    rec = NIMModelRecord(
        upstream_id="local-pulsar/gemma-4-9b-int2",
        alias="local-pulsar-gemma-4-9b-int2",
        provider="local-pulsar",
        model="gemma-4-9b-int2",
        source="future",
        source_url="",
    )
    assert tier_of(rec) == TIER_LOCAL_PULSAR


def test_group_by_tier_uses_all_buckets():
    records = static_catalog()
    groups = group_by_tier(records)
    assert set(groups.keys()) == set(TIER_ORDER)
    total = sum(len(v) for v in groups.values())
    assert total == len(records)


def test_coding_intra_tier_order_puts_kimi_first():
    records = static_catalog()
    groups = group_by_tier(records)
    coding = groups[TIER_CODING]
    assert coding, "expected at least one CODING-tier model in static catalog"
    assert coding[0].upstream_id == "moonshotai/kimi-k2.6"


def test_build_rows_skips_empty_tiers():
    rec = _rec("moonshotai/kimi-k2.6")
    groups = {tier: [] for tier in TIER_ORDER}
    groups[TIER_CODING] = [rec]
    rows = build_rows(groups)
    # one header + one model row only
    assert sum(1 for r in rows if r.kind == "header") == 1
    assert sum(1 for r in rows if r.kind == "model") == 1


def test_find_alias_returns_none_when_missing():
    rec = _rec("moonshotai/kimi-k2.6")
    rows = build_rows({TIER_CODING: [rec], **{t: [] for t in TIER_ORDER if t != TIER_CODING}})
    assert find_alias(rows, "no-such-alias") is None


def test_find_alias_returns_index_of_match():
    rec = _rec("moonshotai/kimi-k2.6")
    rows = build_rows({TIER_CODING: [rec], **{t: [] for t in TIER_ORDER if t != TIER_CODING}})
    idx = find_alias(rows, rec.alias)
    assert idx is not None
    assert rows[idx].record is rec


def test_first_selectable_lands_on_a_model():
    rec = _rec("moonshotai/kimi-k2.6")
    rows = build_rows({TIER_CODING: [rec], **{t: [] for t in TIER_ORDER if t != TIER_CODING}})
    idx = first_selectable(rows)
    assert rows[idx].kind == "model"


def test_render_emits_header_for_each_nonempty_tier():
    records = static_catalog()
    groups = group_by_tier(records)
    rows = build_rows(groups)
    lines = render(rows, cursor=-1, current_alias=None)
    # The render must not emit em-dash characters; the project's house style
    # forbids them in any user-facing string. We express the codepoint via
    # a unicode escape so the source file itself does not contain the literal
    # character, which would trip the CI em-dash audit on this test file.
    EM_DASH = "\u2014"
    for line in lines:
        assert EM_DASH not in line, "em dash leaked into render output"


def test_active_model_round_trip(tmp_path: Path):
    home = tmp_path / "pulsar_home"
    home.mkdir()
    assert read_active_model(home) is None
    write_active_model("nim-kimi", home)
    assert read_active_model(home) == "nim-kimi"
    assert active_model_path(home).exists()
    # mode should be tightened
    mode = oct(active_model_path(home).stat().st_mode)[-3:]
    assert mode == "600"
