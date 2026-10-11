# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Pulsar Observatory (`pulsarcode top`): reducer, rendering, keys, one-shot output, live link."""

import http.server
import io
import json
import random
import threading
import time
import unicodedata
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from pulsar import observatory as O
from pulsar.core import state as ST

T0 = 1_760_000_000.0
INFO = {"port": 4747, "pid": 4242, "version": "3.0.0"}
SECRET_URL = "https://alice:hunter2pass@llm.example.com:8443/v1?api_key=QUERYSECRET123"
PLAN_A = [{"profile": "custom-dead", "provider": "custom", "model": "dead/model-x", "base_url": SECRET_URL},
          {"profile": "custom-llm-example-com", "provider": "custom", "model": "fake/coder-1",
           "base_url": "http://127.0.0.1:8123/v1"}]
PLAN_B = [{"profile": "nvidia", "provider": "nvidia", "model": "moonshotai/kimi-k3"},
          {"profile": "openrouter", "provider": "openrouter", "model": "z-ai/glm-5.3"}]
SESS_A = {"sid": "s1a2b3c4d5e6f", "label": "", "cwd": "/work/app", "plan": PLAN_A, "pid": 100}
SESS_B = {"sid": "s9f8e7d6c5b4a", "label": "refactor", "cwd": "/work/other", "plan": PLAN_B, "pid": 101}


def snap(flights=(), sessions_live=None, breakers=None, total=None, uptime=100.0):
    out = {"uptime": uptime, "flights": list(flights),
           "total": total or {"requests": 3, "errors": 1, "relays": 1, "tokens_in": 9000, "tokens_out": 700,
                              "cache_read": 4000, "cost": 0.0123, "ttft_ewma": 0.9},
           "sessions": {SESS_A["sid"]: {"requests": 2, "tokens_in": 6000, "tokens_out": 500, "relays": 1},
                        SESS_B["sid"]: {"requests": 1, "tokens_in": 3000, "tokens_out": 200, "errors": 1}},
           "providers": {"custom": {"requests": 2, "tokens_out": 500, "ttft_ewma": 0.8},
                         "nvidia": {"requests": 1, "errors": 1, "ttft_ewma": 2.2}},
           "breakers": breakers if breakers is not None else {}}
    if sessions_live is not None:
        out["sessions_live"] = sessions_live
    return out


def flight(rid, sid, tokens, waiting=False, model="fake/coder-1", provider="custom", elapsed=2.0, tps=40.0):
    return {"rid": rid, "sid": sid, "model": model, "provider": provider, "elapsed": elapsed, "waiting": waiting,
            "tokens": tokens, "tps": tps, "relays": 0}


def busy_reducer(now=T0 + 60):
    """A reducer that has seen a little of everything."""
    red = O.Reducer()
    t = T0
    red.link(INFO, t)
    red.stats(snap(sessions_live=[SESS_A, SESS_B],
                   breakers={"custom-dead|dead/model-x": {"open_for": 30.0, "fails": 3}}), t)
    rng = random.Random(4)
    n = 0
    while t < now - 1:
        t += 1.0
        n += 1
        rid = "r%03d" % n
        sid = SESS_A["sid"] if n % 2 else SESS_B["sid"]
        red.link(INFO, t)
        red.event("begin", {"rid": rid, "sid": sid, "model": "fake/coder-1", "provider": "custom", "t": t}, t)
        if n % 5 == 0:
            red.event("relay", {"rid": rid, "sid": sid, "frm": "custom-dead|dead/model-x",
                                "to": "custom|fake/coder-1", "reason": "network", "t": t}, t)
        red.event("tick", snap(flights=[flight(rid, sid, rng.randint(10, 300))]), t + 0.5)
        ok = n % 7 != 0
        red.event("end", {"rid": rid, "sid": sid, "ok": ok, "ttft": rng.uniform(0.2, 5.0), "dur": 1.5,
                          "tokens_out": rng.randint(300, 600), "model": "fake/coder-1", "provider": "custom",
                          "error": "" if ok else "HTTP 429: slow down", "t": t + 0.9}, t + 0.9)
    red.event("heal", {"profile": "nvidia", "model": "moonshotai/kimi-k3", "fix": ["max_tokens"], "t": t}, t)
    red.event("loop", {"sid": SESS_B["sid"], "detail": "Bash x4", "t": t}, t)
    red.event("stage", {"sid": SESS_A["sid"], "kind": "deck", "title": "Tour", "t": t}, t)
    red.event("council", {"sid": SESS_B["sid"], "models": ["a/one", "b/two"], "t": t}, t)
    red.link(INFO, now)
    red.event("tick", snap(flights=[flight("live1", SESS_B["sid"], 120, model="moonshotai/kimi-k3", provider="nvidia"),
                                    flight("live2", SESS_A["sid"], 0, waiting=True)],
                           breakers={"custom-dead|dead/model-x": {"open_for": 12.0, "fails": 3}}), now)
    red.stats(snap(flights=[flight("live1", SESS_B["sid"], 150, model="moonshotai/kimi-k3", provider="nvidia"),
                            flight("live2", SESS_A["sid"], 0, waiting=True)], sessions_live=[SESS_A, SESS_B],
                   breakers={"custom-dead|dead/model-x": {"open_for": 12.0, "fails": 3}}), now)
    return red


# ---------------------------------------------------------------------------
# text helpers
# ---------------------------------------------------------------------------

def test_clean_strips_controls_bidi_and_wide_glyphs():
    assert O.clean("ok\x1b[31mred\x1b]0;title\x07\x9b2J") == "ok[31mred]0;title2J"
    assert O.clean("a\u202eb\u200bc\ufeffd") == "abcd"
    assert O.clean("e\u0301") == "e"
    assert O.clean("模型") == "??"
    assert O.clean(None) == "" and O.clean(123) == "123"
    assert len(O.clean("x" * 10000, 50)) == 50


def test_ascii_only_maps_typography():
    assert O.ascii_only("a · b → c … d › e") == "a . b -> c .. d > e"
    assert O.ascii_only("é") == "?"


def test_number_and_time_formats():
    assert [O.human(n) for n in (0, 999, 1000, 3456, 99_999, 999_999, 1_250_000, 12_300_000, 2e9)] == \
        ["0", "999", "1.0k", "3.5k", "100k", "1.00M", "1.25M", "12.3M", "2.0B"]
    assert O.rate(0) == "0.0" and O.rate(4.25) == "4.2" and O.rate(42.4) == "42" and O.rate(12_345) == "12k"
    assert [O.dur(s) for s in (0.25, 3.44, 42, 125, 7300, 90000)] == ["250ms", "3.4s", "42s", "2m05s", "2h01m", "1d01h"]
    assert O.money(0) == "$0" and O.money(0.01234) == "$0.0123" and O.money(3.5) == "$3.50"
    assert O.fit("abcdef", 4) == "abc…" and O.fit("ab", 4) == "ab" and O.fit("abc", 0) == ""
    assert O.fit_left("/very/long/path", 8) == "…ng/path"
    assert O.short_model("vendor/a-very-long-model-name-x", 10) == "a-very-long-model-name-x"
    assert O.pct([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.5) == 5 and O.pct([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.95) == 10
    assert O.pct([], 0.5) == 0.0
    assert O._host(SECRET_URL) == "llm.example.com:8443"
    assert O._host("not a url") == "" and O._host(None) == ""


def test_series_spreads_over_seconds_and_stays_bounded():
    s = O.Series(10)
    s.spread(100.5, 102.5, 40)                       # 10/s over 100.5..102.5
    assert s.b == {100: 10.0, 101: 20.0, 102: 10.0}
    assert s.values(103.2, 4) == [0.0, 10.0, 20.0, 10.0]
    s.add(103.1, 6)
    assert s.rate(104.0, 2) == pytest.approx(8.0)
    for t in range(200, 400):
        s.add(t, 1)
    assert len(s.b) <= 10 + 16 + 1


# ---------------------------------------------------------------------------
# reducer
# ---------------------------------------------------------------------------

def test_throughput_counts_each_token_once_across_ticks_and_end():
    red = O.Reducer()
    red.link(INFO, T0)
    red.stats(snap(), T0)
    red.event("begin", {"rid": "r1", "sid": "s1", "model": "m", "provider": "custom"}, T0 + 0.2)
    red.event("tick", snap(flights=[flight("r1", "s1", 100)]), T0 + 1.0)
    red.event("tick", snap(flights=[flight("r1", "s1", 300)]), T0 + 2.0)
    red.event("tick", snap(flights=[flight("r1", "s1", 280)]), T0 + 3.0)    # estimates can wobble down
    red.event("end", {"rid": "r1", "sid": "s1", "ok": True, "ttft": 0.4, "dur": 2.9, "tokens_out": 350,
                      "model": "m", "provider": "custom"}, T0 + 3.1)
    assert sum(red.tps.b.values()) == pytest.approx(350)
    assert sum(red.tps_sid["s1"].b.values()) == pytest.approx(350)
    assert sum(red.rps.b.values()) == 1
    assert [x["v"] for x in red.ttft] == [0.4]
    assert "r1" not in red.flights and "r1" not in red.seen
    v = red.view(T0 + 4.5, verbose=True)
    assert v.feed[-1]["kind"] == "done" and "350 tok" in O.entry_text(v.feed[-1])
    assert all(e["kind"] != "done" for e in red.view(T0 + 4.5).feed)


def test_tokens_generated_before_watching_are_not_throughput():
    red = O.Reducer()
    red.link(INFO, T0)
    red.event("tick", snap(flights=[flight("old", "s1", 5000)]), T0)
    red.event("tick", snap(flights=[flight("old", "s1", 5100)]), T0 + 1)
    assert sum(red.tps.b.values()) == pytest.approx(100)


def test_events_feed_counts_and_relay_marks_failing_provider():
    red = busy_reducer()
    v = red.view(T0 + 61)
    kinds = {e["kind"] for e in red.feed}
    assert {"link", "join", "relay", "heal", "loop", "stage", "council", "error", "breaker"} <= kinds
    assert v.totals["heals"] == 1 and v.totals["loops"] == 1 and v.totals["stage"] == 1 and v.totals["council"] == 1
    assert v.totals["relays"] >= 11
    relay = [e for e in red.feed if e["kind"] == "relay"][-1]
    assert O.entry_text(relay) == "dead/model-x -> fake/coder-1  network"
    assert red.last_relay[SESS_A["sid"]]["from_provider"] == "custom"
    assert False in [ok for _, ok in red.results["custom"]]
    sess = {s["sid"]: s for s in v.sessions}
    assert sess[SESS_A["sid"]]["plan"][0]["state"] == "open"
    assert sess[SESS_B["sid"]]["flights"][0]["tokens"] == 150
    assert sess[SESS_A["sid"]]["flights"][0]["waiting"]
    assert v.p50 > 0 and v.p95 >= v.p50 and len(v.ttft) > 10
    assert v.tps_now > 0 and len(v.tps_hist) == O.HISTORY


def test_end_kinds_cancel_overflow_and_loop_guard():
    red = O.Reducer()
    red.link(INFO, T0)
    for rid, err in (("a", "client went away"), ("b", "context overflow"), ("c", "loop guard"), ("d", "HTTP 500")):
        red.event("begin", {"rid": rid, "sid": "s1", "model": "m", "provider": "p"}, T0)
        red.event("end", {"rid": rid, "sid": "s1", "ok": False, "error": err, "model": "m", "provider": "p"}, T0 + 1)
    kinds = [e["kind"] for e in red.feed]
    assert kinds.count("cancel") == 1 and kinds.count("overflow") == 1 and kinds.count("error") == 1
    assert "loop" not in kinds                    # the loop event itself reports it
    assert [ok for _, ok in red.results["p"]] == [False]      # only the real failure counts against p
    assert [e["kind"] for e in red.view(T0 + 2).feed if e["kind"] in ("cancel", "error")] == ["error"]
    assert sum(red.eps.b.values()) == 1


def test_breaker_countdown_health_and_single_notice():
    red = O.Reducer()
    red.link(INFO, T0)
    br = {"custom-dead|dead/model-x": {"open_for": 20.0, "fails": 2}}
    red.stats(snap(sessions_live=[SESS_A], breakers=br), T0)
    red.event("tick", snap(breakers={"custom-dead|dead/model-x": {"open_for": 19.0, "fails": 2}}), T0 + 1)
    assert [e["kind"] for e in red.feed].count("breaker") == 1
    v = red.view(T0 + 5)
    prov = {p["id"]: p for p in v.providers}["custom"]
    # SESS_A's other custom target (fake/coder-1) still works: one open breaker among them is degraded, and its
    # countdown is shown all the same.
    assert prov["health"] == "degraded" and prov["breakers"][0]["left"] == pytest.approx(15.0)
    assert prov["breakers"][0]["span"] == pytest.approx(20.0)
    both = O.Reducer()
    both.link(INFO, T0)
    both.stats(snap(sessions_live=[SESS_A], breakers=dict(br, **{
        "custom-llm-example-com|fake/coder-1": {"open_for": 30.0, "fails": 2}})), T0)
    custom = {p["id"]: p for p in both.view(T0 + 5).providers}["custom"]
    assert custom["health"] == "open" and [b["left"] for b in custom["breakers"]] == pytest.approx([25.0, 15.0])
    red.event("tick", snap(breakers={"custom-dead|dead/model-x": {"open_for": 40.0, "fails": 3}}), T0 + 6)
    assert [e["kind"] for e in red.feed].count("breaker") == 1            # re-armed, but noted once a minute
    assert {p["id"]: p for p in red.view(T0 + 7).providers}["custom"]["breakers"][0]["span"] == pytest.approx(40.0)
    red.event("tick", snap(breakers={"custom-dead|dead/model-x": {"open_for": 80.0, "fails": 4}}), T0 + 70)
    assert [e["kind"] for e in red.feed].count("breaker") == 2
    red.event("tick", snap(breakers={}), T0 + 50)
    assert {p["id"]: p for p in red.view(T0 + 50).providers}["custom"]["health"] in ("ok", "idle")


def test_session_join_and_leave_keep_a_stable_order():
    red = O.Reducer()
    red.link(INFO, T0)
    red.stats(snap(sessions_live=[SESS_A]), T0)
    red.stats(snap(sessions_live=[SESS_B, SESS_A]), T0 + 2)
    assert list(red.live) == [SESS_A["sid"], SESS_B["sid"]]
    red.stats(snap(sessions_live=[SESS_B]), T0 + 4)
    notes = [(e["kind"], e["sid"]) for e in red.feed if e["kind"] in ("join", "left")]
    assert notes == [("join", ""), ("join", SESS_B["sid"]), ("left", SESS_A["sid"])]


def test_link_states_and_a_new_core_starts_over():
    red = O.Reducer()
    assert red.state(T0) == "idle"
    red.link(INFO, T0)
    red.stats(snap(flights=[flight("r", "s1", 10)]), T0)
    assert red.state(T0 + 1) == "live"
    red.step(T0 + O.LIVE_WINDOW + 0.1)
    assert red.state(T0 + O.LIVE_WINDOW + 0.1) == "lost" and not red.flights
    assert red.feed[-1]["kind"] == "lost"
    assert red.state(T0 + O.LOST_HOLD + 1) == "idle"
    red.link(INFO, T0 + 20)
    assert red.state(T0 + 20) == "live" and red.feed[-1]["kind"] == "link"
    assert red.total["requests"] == 3
    red.link(dict(INFO, pid=9999), T0 + 21)
    assert red.total == {} and red.core["pid"] == 9999
    red.down("gone", T0 + 22, no_core=True)
    assert red.state(T0 + 22) == "idle"


def test_secrets_never_reach_view_or_json():
    red = O.Reducer()
    red.link(INFO, T0)
    red.stats(snap(sessions_live=[SESS_A]), T0)
    leaked = "sk-proj-" + "A" * 40
    admin = "pla_" + "B" * 40
    red.event("end", {"rid": "x", "sid": SESS_A["sid"], "ok": False, "model": "m", "provider": "custom",
                      "error": "Invalid API key provided: " + leaked}, T0 + 1)
    red.event("relay", {"rid": "y", "sid": SESS_A["sid"], "frm": "a|b", "to": "c|d", "reason": "bearer " + admin}, T0 + 1)
    red.event("tick", {"total": {"last_error": "authorization: Bearer " + leaked}}, T0 + 1)
    blob = json.dumps(red.to_json(T0 + 2))
    obs = O.Observatory(red)
    screen = "\n".join("".join(c[0] for c in row) for row in obs.frame(200, 60, T0 + 2).cells)
    for text in (blob, screen):
        assert leaked not in text and admin not in text
        assert "hunter2" not in text and "QUERYSECRET" not in text and "alice" not in text
    assert "base_url" not in blob


def test_garbage_input_never_raises():
    red = O.Reducer()
    rng = random.Random(9)
    junk = [None, 1, -1, 1e308, float("nan"), "x", "\x1b[2J", [], {}, [1, 2], {"a": {"b": None}}, True]
    keys = ["rid", "sid", "model", "provider", "tokens", "tokens_out", "ttft", "dur", "ok", "error", "frm", "to",
            "reason", "flights", "total", "sessions", "providers", "breakers", "sessions_live", "uptime", "plan",
            "fix", "models", "kind", "title", "detail", "open_for", "fails", "t", "elapsed", "waiting", "tps"]
    names = ["tick", "begin", "end", "relay", "heal", "loop", "stage", "council", "nope", None, 3]
    for i in range(3000):
        data = {rng.choice(keys): rng.choice(junk) for _ in range(rng.randint(0, 8))}
        if rng.random() < 0.3:
            data["flights"] = [{rng.choice(keys): rng.choice(junk) for _ in range(5)}, rng.choice(junk)]
        if rng.random() < 0.2:
            data["sessions_live"] = [{"sid": "s%d" % rng.randint(0, 3), "plan": [rng.choice(junk), {"model": 5}]},
                                     rng.choice(junk)]
        if rng.random() < 0.2:
            data["breakers"] = {"p|m": {"open_for": rng.choice(junk), "fails": rng.choice(junk)}, "x": rng.choice(junk)}
        red.link(rng.choice([INFO, None, {"pid": "x"}]), T0 + i * 0.01)
        if rng.random() < 0.5:
            red.event(rng.choice(names), data if rng.random() < 0.9 else rng.choice(junk), T0 + i * 0.01)
        else:
            red.stats(data, T0 + i * 0.01)
    obs = O.Observatory(red)
    for w, h in ((80, 24), (200, 60), (40, 12)):
        obs.frame(w, h, T0 + 31)
    json.dumps(red.to_json(T0 + 31))


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

SIZES = [(20, 8), (33, 9), (40, 12), (50, 16), (60, 20), (79, 23), (80, 24), (81, 22), (100, 30), (120, 40),
         (129, 31), (130, 32), (160, 48), (200, 60), (240, 70)]


def assert_canvas_ok(cv, w, h, u):
    assert cv.w == w and cv.h == h and len(cv.cells) == h
    for row in cv.cells:
        assert len(row) == w
        for ch, fg, bold in row:
            assert isinstance(ch, str) and len(ch) == 1 and ord(ch) >= 32 and not (0x7F <= ord(ch) <= 0x9F)
            assert unicodedata.east_asian_width(ch) not in ("W", "F")
            if not u:
                assert ord(ch) < 128, repr(ch)
            assert fg is None or (len(fg) == 3 and all(0 <= c <= 255 for c in fg))


def text_of(cv):
    return "\n".join("".join(c[0] for c in row) for row in cv.cells)


@pytest.mark.parametrize("u", [True, False])
@pytest.mark.parametrize("motion", [True, False])
def test_renders_every_size_and_state_inside_the_canvas(u, motion):
    now = T0 + 60
    busy = busy_reducer(now)
    lost = busy_reducer(now)
    lost.step(now + 5)
    scenarios = [("idle", O.Reducer(), now), ("busy", busy, now), ("lost", lost, now + 5)]
    empty = O.Reducer()
    empty.link(INFO, now)
    empty.stats(snap(sessions_live=[], total={}), now)
    scenarios.append(("empty", empty, now))
    for name, red, at in scenarios:
        obs = O.Observatory(red, unicode=u, motion=motion, seed=1)
        for w, h in SIZES:
            for k in range(3):
                cv = obs.frame(w, h, at + k * 0.37)
                assert_canvas_ok(cv, w, h, u)
        if name == "busy":
            obs.focus = SESS_B["sid"]
            obs.help = True
            obs.set_paused(True, at)
            for w, h in SIZES:
                assert_canvas_ok(obs.frame(w, h, at + 1), w, h, u)


def test_dashboard_shows_the_essentials():
    now = T0 + 60
    red = busy_reducer(now)
    obs = O.Observatory(red, motion=False)
    big = text_of(obs.frame(200, 60, now))
    for word in ("PULSAR OBSERVATORY", "TOTALS", "SESSIONS", "THROUGHPUT", "ROUTES", "PROVIDERS", "LATENCY", "FEED",
                 "refactor", "STREAM", "WAIT", "OPEN", "RELAY", "core :4747", "LIVE", "CORE"):
        assert word in big, word
    small = text_of(obs.frame(80, 24, now))
    for word in ("SESSIONS", "THROUGHPUT", "PROVIDERS", "FEED", "refactor"):
        assert word in small, word
    tiny = text_of(obs.frame(30, 9, now))
    assert "PULSAR" in tiny and "tok/s" in tiny


def test_idle_screen_says_how_to_start():
    obs = O.Observatory(O.Reducer(), motion=False)
    text = text_of(obs.frame(120, 40, T0))
    assert "no Core running: start a session with pulsarcode" in text
    assert "O B S E R V A T O R Y" in text
    red = O.Reducer()
    red.down("connection refused", T0, no_core=False, info={"port": 4747})
    text = text_of(O.Observatory(red, motion=False).frame(120, 40, T0))
    assert "Pulsar Core on :4747 is not answering yet" in text


def test_layout_rects_stay_on_screen_and_do_not_overlap():
    for w in range(20, 241, 7):
        for h in range(8, 75, 5):
            for ns, npv, want in ((0, 0, None), (2, 4, 15), (9, 9, 60)):
                rects = O.layout(w, h, ns, npv, want)
                cells = set()
                for name, (x, y, rw, rh) in rects.items():
                    assert x >= 0 and y >= 2 and x + rw <= w and y + rh <= h - 1, (w, h, name)
                    area = {(xx, yy) for xx in range(x, x + rw) for yy in range(y, y + rh)}
                    assert not (cells & area), (w, h, name)
                    cells |= area


def test_routes_panel_only_when_the_left_column_has_room():
    assert "routes" in O.layout(200, 60, 2, 4, 15)
    assert "routes" not in O.layout(200, 60, 2, 4, 60)
    assert "routes" not in O.layout(80, 24, 2, 4, 9)
    assert "routes" not in O.layout(200, 60, 2, 4, None)


def test_idle_cards_fall_back_to_core_aggregates_and_names_win_over_counters():
    red = O.Reducer()
    red.link(INFO, T0)
    sessions = {SESS_B["sid"]: {"requests": 115, "tokens_in": 9_000_000, "tokens_out": 50_000, "relays": 7,
                                "errors": 2, "last_at": T0 - 30, "last_model": "z-ai/glm-5.3"}}
    snapshot = snap(sessions_live=[SESS_B])
    snapshot["sessions"] = sessions
    red.stats(snapshot, T0)
    obs = O.Observatory(red, motion=False)
    text = text_of(obs.frame(120, 40, T0))
    assert "idle · last answer 30s ago" in text and "idle · no requests yet" not in text
    narrow = text_of(obs.frame(80, 24, T0))
    assert "refactor" in narrow


def test_area_chart_big_digits_and_bars():
    from pulsar.fx import Canvas
    cv = Canvas(6, 3)
    crests = O.area_chart(cv, 0, 0, 6, 3, [1.0] * 12)
    assert crests == [0] * 6 and all(cv.get(x, y)[0] == "⣿" for x in range(6) for y in range(3))
    cv = Canvas(4, 2)
    O.area_chart(cv, 0, 0, 4, 2, [0.0, 0.0, 0.01, 0.5, 0, 0, 0, 0])
    assert cv.get(0, 1)[0] == " " and cv.get(1, 1)[0] != " " and cv.get(1, 0)[0] == " "
    cv = Canvas(4, 2)
    O.area_chart(cv, 0, 0, 4, 2, [1.0, 0.5, 0.0, 0.0], u=False)
    assert cv.get(0, 0)[0] == "#" and cv.get(1, 1)[0] in "#:." and cv.get(2, 1)[0] == " "
    assert O.big_width("136") == 11 and O.big_width("") == 0
    cv = Canvas(20, 3)
    assert O.big_text(cv, 0, 0, "1.5k") == O.big_width("1.5k")
    assert O.resample([1.0] * 120, 30, 0.5) == pytest.approx([1.0] * 30)
    assert len(O.resample([0.0, 4.0], 200, 0.0)) == 200 and O.resample([], 3, 0) == [0.0, 0.0, 0.0]


# ---------------------------------------------------------------------------
# keys
# ---------------------------------------------------------------------------

def test_keys_pause_clear_focus_verbose_help_quit():
    now = T0 + 60
    red = busy_reducer(now)
    obs = O.Observatory(red, motion=False)
    obs.frame(120, 40, now)
    assert obs.key("p", now) and obs.paused
    frozen = obs.frozen
    red.event("heal", {"profile": "p", "model": "m"}, now + 1)
    obs.frame(120, 40, now + 1)
    assert obs.frozen is frozen and all(e["id"] != red.seq for e in obs.frozen.feed)
    obs.key("p", now + 2)
    assert not obs.paused and obs.frozen is None
    obs.key("c", now + 2)
    assert not red.feed
    seen = []
    for _ in range(3):
        obs.key("s", now + 2)
        seen.append(obs.focus)
    assert seen == [SESS_A["sid"], SESS_B["sid"], None]
    obs.key("S", now + 2)
    assert obs.focus == SESS_B["sid"]
    assert "focus" in text_of(obs.frame(120, 40, now + 2))
    obs.key("v", now + 2)
    assert obs.verbose
    obs.key("?", now + 2)
    assert obs.help and "KEYS" in text_of(obs.frame(120, 40, now + 2))
    assert obs.key("esc", now + 2) and not obs.help
    for k in ("q", "ctrl-c", "esc"):
        assert obs.key(k, now) is False


def test_focus_follows_a_session_that_ends():
    now = T0 + 60
    red = busy_reducer(now)
    obs = O.Observatory(red, motion=False, focus=SESS_B["sid"])
    obs.frame(120, 40, now)
    assert obs.focus == SESS_B["sid"]
    red.stats(snap(sessions_live=[SESS_A]), now + 1)
    obs.frame(120, 40, now + 1)
    assert obs.focus is None


# ---------------------------------------------------------------------------
# one-shot output and the command
# ---------------------------------------------------------------------------

def test_cli_args_help_and_errors(capsys):
    assert O.cmd_top(["--help"]) == 0 and "pulsarcode top" in capsys.readouterr().out
    assert O.cmd_top(["--bogus"]) == 2 and "unknown option" in capsys.readouterr().err
    assert O.cmd_top(["--sid"]) == 2
    assert O.parse_args(["--sid", "s1", "--json"]) == {"once": False, "json": True, "sid": "s1", "help": False}
    assert O.parse_args(["--sid=s2", "--plain"])["once"] is True


def test_json_and_text_without_a_core(capsys):
    assert O.cmd_top(["--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["core"]["running"] is False and data["sessions"] == []
    assert O.cmd_top(["--once"]) == 1
    assert "no Pulsar Core is running" in capsys.readouterr().out
    assert O.cmd_top([]) == 1                       # not a tty: the one-shot summary
    assert "PULSAR OBSERVATORY" in capsys.readouterr().out


def test_summary_text_mentions_everything_and_honors_ascii():
    red = busy_reducer()
    from pulsar import fx
    text = O.summary(red.view(T0 + 61), fx.Ink(0), True)
    for word in ("PULSAR OBSERVATORY", "core :4747", "SESSIONS", "refactor", "PROVIDERS", "OPEN", "FEED", "tok/s"):
        assert word in text, word
    assert "\x1b" not in text
    ascii_text = O.summary(red.view(T0 + 61), fx.Ink(0), False)
    assert all(ord(c) < 128 for c in ascii_text)
    colored = O.summary(red.view(T0 + 61), fx.Ink(24), True)
    assert "\x1b[" in colored


class FakeCore(http.server.ThreadingHTTPServer):
    """A stand-in Pulsar Core: proves the admin token on /healthz, serves stats and a short event stream."""

    daemon_threads = True

    def __init__(self, token: str) -> None:
        self.token = token
        self.auth_seen = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send_json(self, code, obj):
                out = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def do_GET(self):
                u = urlsplit(self.path)
                if u.path == "/healthz":
                    nonce = parse_qs(u.query).get("nonce", [""])[0]
                    reply = {"status": "ok", "service": "pulsar-core", "version": "3.0.0"}
                    if nonce:
                        reply["proof"] = ST.core_proof(outer.token, nonce)
                    return self.send_json(200, reply)
                auth = self.headers.get("authorization") or ""
                outer.auth_seen.append(auth)
                if auth != "Bearer " + outer.token:
                    return self.send_json(401, {"error": "token required"})
                if u.path == "/pulsar/stats":
                    return self.send_json(200, snap(flights=[flight("r9", SESS_B["sid"], 64)],
                                                    sessions_live=[SESS_A, SESS_B]))
                if u.path == "/pulsar/events":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for name, data in (("begin", {"rid": "r9", "sid": SESS_B["sid"], "model": "m", "provider": "nvidia"}),
                                       ("relay", {"rid": "r9", "sid": SESS_B["sid"], "frm": "nvidia|m",
                                                  "to": "openrouter|z-ai/glm-5.3", "reason": "HTTP 429"}),
                                       ("tick", snap(flights=[flight("r9", SESS_B["sid"], 64)]))):
                        self.wfile.write(("event: %s\ndata: %s\n\n" % (name, json.dumps(data))).encode())
                        self.wfile.flush()
                    time.sleep(0.3)
                    return
                self.send_json(404, {})

        super().__init__(("127.0.0.1", 0), H)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def port(self):
        return self.server_address[1]


@pytest.fixture
def fake_core(pulsar_home):
    token = ST.new_token(ST.ADMIN_TOKEN_PREFIX)
    core = FakeCore(token)
    ST.write_core(ST.CoreInfo(core.port, 4242, "3.0.0", token, time.time()))
    yield core
    core.shutdown()
    core.server_close()


def test_once_json_and_text_against_a_core(fake_core, capsys):
    assert O.cmd_top(["--json"]) == 0
    raw = capsys.readouterr().out
    data = json.loads(raw)
    assert data["core"]["running"] and data["core"]["port"] == fake_core.port and data["core"]["pid"] == 4242
    assert [s["sid"] for s in data["sessions"]] == [SESS_A["sid"], SESS_B["sid"]]
    assert data["sessions"][1]["flights"][0]["tokens"] == 64
    assert data["totals"]["requests"] == 3 and data["throughput"]["tokens_per_s"] == 40.0
    assert fake_core.token not in raw and "hunter2" not in raw and "QUERYSECRET" not in raw
    assert O.cmd_top(["--once"]) == 0
    out = capsys.readouterr().out
    assert "core :%d" % fake_core.port in out and "refactor" in out and fake_core.token not in out


def test_link_threads_follow_events_and_stats(fake_core):
    red = O.Reducer()
    link = O.Link(poll=0.2).start()
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            link.drain(red, time.time())
            if red.live and any(e["kind"] == "relay" for e in red.feed):
                break
            time.sleep(0.05)
    finally:
        link.stop()
    assert red.state(time.time()) == "live"
    assert set(red.live) == {SESS_A["sid"], SESS_B["sid"]}
    assert red.last_relay[SESS_B["sid"]]["to"] == "z-ai/glm-5.3"
    assert set(fake_core.auth_seen) == {"Bearer " + fake_core.token}


def test_link_reports_no_core_and_keeps_trying(pulsar_home):
    red = O.Reducer()
    link = O.Link(poll=0.1).start()
    try:
        time.sleep(0.3)
        link.drain(red, time.time())
    finally:
        link.stop()
    assert red.no_core and red.state(time.time()) == "idle"


def test_link_never_trusts_a_port_that_cannot_prove_the_token(pulsar_home):
    impostor = FakeCore("pla_" + "x" * 43)                        # holds a different token
    real = ST.new_token(ST.ADMIN_TOKEN_PREFIX)
    ST.write_core(ST.CoreInfo(impostor.port, 4242, "3.0.0", real, time.time()))
    try:
        assert O.once(as_json=True, out=io.StringIO()) == 1
        assert impostor.auth_seen == []                           # the admin token never left
    finally:
        impostor.shutdown()
        impostor.server_close()


def test_module_has_no_em_dash():
    src = Path(O.__file__).read_text(encoding="utf-8")
    assert chr(0x2014) not in src
