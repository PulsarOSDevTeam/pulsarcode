# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Regressions found by running the Observatory end to end against the real Claude Code (one test or more per bug)."""
import pytest

from pulsar import fx
from pulsar import observatory as O

T0 = 1_760_000_000.0
INFO = {"port": 4747, "pid": 4242, "version": "3.0.0"}


def _session(sid, plan):
    return {"sid": sid, "label": sid, "cwd": "/work/" + sid, "pid": 100, "plan": plan}


def _target(profile, provider, model):
    return {"profile": profile, "provider": provider, "model": model}


def reducer(breakers, sessions, providers):
    red = O.Reducer()
    red.link(INFO, T0)
    red.stats({"uptime": 20.0, "flights": [], "total": {"requests": 13, "errors": 4}, "sessions": {},
               "providers": providers, "breakers": breakers, "sessions_live": sessions}, T0)
    return red


def three_breakers():
    """The scene those runs used: NVIDIA open on its one target, Groq degraded with two of its three targets open."""
    sessions = [
        _session("leak2", [_target("groqleak", "groq", "openai/gpt-oss-20b"),
                           _target("groqleak", "groq", "qwen/qwen3-32b")]),
        _session("app", [_target("nvidia", "nvidia", "moonshotai/kimi-k3"),
                         _target("openrouter", "openrouter", "z-ai/glm-5.3")]),
        _session("refactor", [_target("cerebras", "cerebras", "zai-glm-4.6-think")]),
        _session("docs", [_target("groq", "groq", "openai/gpt-oss-120b")]),
    ]
    providers = {"openrouter": {"requests": 6, "ttft_ewma": 0.9, "tokens_out": 700},
                 "nvidia": {"requests": 1, "errors": 1},
                 "groq": {"requests": 5, "errors": 3, "ttft_ewma": 1.6, "tokens_in": 16950, "tokens_out": 264},
                 "cerebras": {"requests": 3, "ttft_ewma": 2.1, "tokens_out": 384}}
    breakers = {"nvidia|moonshotai/kimi-k3": {"open_for": 157.0, "fails": 7},
                "groqleak|qwen/qwen3-32b": {"open_for": 315.0, "fails": 9},
                "groqleak|openai/gpt-oss-20b": {"open_for": 30.0, "fails": 3}}
    return reducer(breakers, sessions, providers)


def panel(red, w, h, u=True):
    """The Providers panel content alone, as text rows."""
    obs = O.Observatory(red, unicode=u, motion=False)
    cv = fx.Canvas(w, h)
    obs._draw_providers(cv, (0, 0, w, h), red.view(T0), T0)
    return ["".join(c[0] for c in row).rstrip() for row in cv.cells]


def screen_panel(red, w, h):
    """The Providers panel rows as a full `pulsarcode top` frame draws them."""
    cv = O.Observatory(red, motion=False).frame(w, h, T0)
    rows = ["".join(c[0] for c in row) for row in cv.cells]
    top = next(i for i, line in enumerate(rows) if "PROVIDERS" in line)
    left = rows[top].index("PROVIDERS") - 5
    out = []
    for line in rows[top + 1:]:
        if line[left] != "│":
            break
        out.append(line[left + 1:line.index("│", left + 1)].strip())
    return out


# -- spare Providers rows name what no main row says; top --once lists every breaker ------------------------

def test_spare_row_goes_to_the_degraded_provider_with_two_breakers():
    red = three_breakers()
    assert [(p["id"], p["health"], len(p["breakers"])) for p in red.view(T0).providers][:2] == [
        ("nvidia", "open", 1), ("groq", "degraded", 2)]
    rows = panel(red, 52, 5)                               # the 140x44 panel: four providers and one spare row
    nvidia, groq, sub = rows[0], rows[1], rows[2]
    assert "NVIDIA" in nvidia and "OPEN 2m37s" in nvidia and "kimi-k3" in nvidia and "7 fails" in nvidia
    assert sum("kimi-k3" in r for r in rows) == 1           # the open provider's one breaker is not repeated
    assert "Groq" in groq and "5 req" in groq and "60% err" in groq and "ttft 1.6s" in groq and "OPEN" not in groq
    assert sub.lstrip().startswith("╰") and "qwen/qwen3-32b +1" in sub and "OPEN 5m15s" in sub and "9 fails" in sub
    assert "OpenRouter" in rows[3] and "Cerebras" in rows[4]


def test_without_a_spare_row_the_degraded_row_names_its_breakers_and_keeps_its_request_count():
    rows = panel(three_breakers(), 46, 4)                  # the 100x30 panel: no spare row
    groq = next(r for r in rows if "Groq" in r)
    assert "OPEN 5m15s" in groq and "qwen3-32b" in groq and "+1" in groq and "5 req" in groq
    nvidia = next(r for r in rows if "NVIDIA" in r)
    assert "OPEN 2m37s" in nvidia and "kimi-k3" in nvidia


@pytest.mark.parametrize("size", [(140, 44), (100, 30)])
def test_full_frames_match_the_reference_layouts(size):
    rows = screen_panel(three_breakers(), *size)
    text = "\n".join(rows)
    assert text.count("kimi-k3") == 1 and "qwen" in text and "+1" in text
    groq = next(r for r in rows if r.startswith("● Groq"))
    assert "5 req" in groq


def test_open_provider_with_two_breakers_names_both_without_repeating_either():
    sessions = [_session("a", [_target("groq", "groq", "qwen/qwen3-32b"), _target("groq", "groq", "llama-4")])]
    red = reducer({"groq|qwen/qwen3-32b": {"open_for": 300.0, "fails": 9},
                   "groq|llama-4": {"open_for": 40.0, "fails": 1}}, sessions, {"groq": {"requests": 2, "errors": 2}})
    assert red.view(T0).providers[0]["health"] == "open"
    rows = panel(red, 60, 3)
    assert "OPEN 5m00s" in rows[0] and "qwen/qwen3-32b" in rows[0] and "9 fails" in rows[0]
    assert rows[1].lstrip().startswith("╰") and "llama-4" in rows[1] and "1 fail" in rows[1]
    assert "1 fails" not in rows[1] and "+" not in rows[1]
    assert sum("qwen3-32b" in r for r in rows) == 1
    one = panel(red, 60, 1)                                # no spare row: the main row says how many more
    assert "qwen/qwen3-32b +1" in one[0]


def test_spare_rows_are_dealt_round_by_round_and_the_last_one_counts_the_rest():
    sessions = [_session("a", [_target("groq", "groq", m) for m in ("g/one", "g/two", "g/three", "g/ok")]),
                _session("b", [_target("cerebras", "cerebras", m) for m in ("c/one", "c/two", "c/ok")])]
    breakers = {"groq|g/one": {"open_for": 90.0, "fails": 3}, "groq|g/two": {"open_for": 80.0, "fails": 3},
                "groq|g/three": {"open_for": 70.0, "fails": 3},
                "cerebras|c/one": {"open_for": 60.0, "fails": 2}, "cerebras|c/two": {"open_for": 50.0, "fails": 2}}
    red = reducer(breakers, sessions, {"groq": {"requests": 9, "errors": 3}, "cerebras": {"requests": 4, "errors": 2}})
    assert [p["health"] for p in red.view(T0).providers] == ["degraded", "degraded"]
    rows = panel(red, 60, 4)                               # two spare rows: one each, not both to the first
    assert [r.lstrip()[:1] for r in rows] == ["●", "╰", "●", "╰"]
    assert "g/one +2" in rows[1] and "c/one +1" in rows[3]
    rows = panel(red, 60, 7)                               # room for every breaker: no +N left anywhere
    assert sum(r.lstrip().startswith("╰") for r in rows) == 5 and not any(" +" in r for r in rows)
    ascii_rows = panel(red, 60, 4, u=False)
    assert all(ord(c) < 128 for r in ascii_rows for c in r) and "g/one +2" in ascii_rows[1]


def test_once_lists_every_breaker_and_keeps_a_degraded_providers_stats():
    text = O.summary(three_breakers().view(T0), fx.Ink(0), True)
    prov = text.split("PROVIDERS")[1].split("FEED")[0].splitlines()
    groq = next(i for i, line in enumerate(prov) if "Groq" in line)
    assert "degraded" in prov[groq] and "5 req" in prov[groq] and "60.0% err" in prov[groq]
    assert "OPEN 5m15s" in prov[groq + 1] and "qwen/qwen3-32b" in prov[groq + 1]
    assert "OPEN 30s" in prov[groq + 2] and "openai/gpt-oss-20b" in prov[groq + 2]
    nvidia = next(line for line in prov if "NVIDIA" in line)
    assert "OPEN 2m37s" in nvidia and "7 fails" in nvidia and "moonshotai/kimi-k3" in nvidia
    assert text.count("kimi-k3") >= 1 and sum("OPEN" in line for line in prov) == 3
    ascii_text = O.summary(three_breakers().view(T0), fx.Ink(0), False)
    assert all(ord(c) < 128 for c in ascii_text) and "openai/gpt-oss-20b" in ascii_text


@pytest.mark.parametrize("u", [True, False])
def test_breaker_rows_stay_inside_every_panel_size(u):
    red = three_breakers()
    for w in range(1, 80):
        for h in range(1, 9):
            rows = panel(red, w, h, u)
            assert len(rows) == h and all(len(r) <= w for r in rows)
            assert u or all(ord(c) < 128 for r in rows for c in r)
    obs = O.Observatory(red, unicode=u, motion=False)
    for w, h in ((20, 8), (40, 12), (80, 24), (100, 30), (120, 40), (140, 44), (200, 60)):
        cv = obs.frame(w, h, T0)
        assert cv.w == w and cv.h == h and all(len(row) == w for row in cv.cells)
