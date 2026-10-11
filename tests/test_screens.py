# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
import io
import random
import sys

import pytest

from pulsar import fx
from pulsar import screens as S

SIZES = ((20, 8), (40, 12), (44, 13), (60, 18), (80, 24), (100, 30), (140, 40), (200, 60))
LABELS = ("Claude Code", "Plugin + profile", "Uplink", "Model catalog", "Pulsar Core")


class FakeCaps:
    def __init__(self, tty=True, color=24, unicode=True, motion=True, reduced=False, fx_mode="auto", w=100, h=30):
        self.out = io.StringIO()
        self.tty, self.color, self.unicode, self.motion = tty, color, unicode, motion
        self.reduced, self.fx, self.ci = reduced, fx_mode, False
        self._size = (w, h)

    @property
    def size(self):
        return self._size


def tasks(status="ok"):
    out = []
    for i, label in enumerate(LABELS):
        st = status if i == 2 else "ok"
        out.append(S.Task(label, lambda st=st: (st, "detail %s" % st)))
    return out


def done(ts):
    for t in ts:
        t.status, t.detail = t.fn()
    return ts


def hw_task(box):
    t = S.Task("Hardware", lambda: ("ok", "x"))
    box["hw"] = {"chip": "Apple M3 Max", "physical_cores": 16, "ram_gb": 64.0, "gpus": [], "env": {}}
    t.status, t.detail = "ok", "x"
    return t


def text_of(cv):
    return "\n".join("".join(ch for ch, _, _ in row) for row in cv.cells)


def run_boot(b, w, h, until=4.0, dt=1 / 30, resize=None):
    t = 0.0
    cv = None
    while t < until and not b.finished(t):
        if resize and t > resize[0]:
            w, h = resize[1]
        cv = b.frame(w, h, t)
        assert cv.w == w and cv.h == h
        t += dt
    return t, cv


@pytest.mark.parametrize("w,h", SIZES)
@pytest.mark.parametrize("unicode", (True, False))
def test_boot_frames_render_everywhere(w, h, unicode):
    for quick in (False, True):
        for level in ("full", "lite", "minimal"):
            box = {}
            b = S._Boot(done(tasks()), quick, unicode, random.Random(1), level, hw_task=hw_task(box), hw_box=box)
            t, cv = run_boot(b, w, h)
            assert b.finished(t)
            for row in cv.cells:
                for ch, fg, bold in row:
                    assert isinstance(ch, str) and len(ch) == 1
                    assert fg is None or (len(fg) == 3 and all(0 <= c <= 255 for c in fg))
                    if not unicode:
                        assert ord(ch) < 128, (w, h, quick, level, ch)


@pytest.mark.parametrize("w,h", ((60, 18), (80, 24), (100, 30), (140, 40)))
def test_settled_frame_shows_every_system_and_the_verdict(w, h):
    box = {}
    b = S._Boot(done(tasks()), False, True, random.Random(2), "full", hw_task=hw_task(box), hw_box=box)
    _, cv = run_boot(b, w, h)
    txt = text_of(cv)
    assert "SYSTEMS" in txt and "6/6" in txt and "ALL SYSTEMS NOMINAL" in txt
    for label in LABELS + ("Hardware",):
        assert label in txt
    assert "Apple M3 Max" in txt and "64 GB" in txt          # the HARDWARE row comes from hardware.summary
    assert "T+" in txt and "M models" in txt


def test_verdicts_are_honest():
    b = S._Boot(done(tasks("warn")), True, True, random.Random(3))
    assert b.verdict()[0].startswith("SYSTEMS READY") and "1 ADVISORY" in b.verdict()[0]
    b = S._Boot(done(tasks("fail")), True, False, random.Random(3))
    assert b.verdict()[0] == "1 SYSTEM DOWN"
    _, cv = run_boot(b, 80, 24)
    assert "1 SYSTEM DOWN" in text_of(cv) and "x Uplink" in text_of(cv)


def test_timeline_meets_the_boot_budgets():
    first, quick = S._Timeline(False), S._Timeline(True)
    assert first.min_t + first.flash <= 2.45 and quick.min_t + quick.flash <= 0.8
    for quick_mode, limit in ((False, 2.5), (True, 0.85)):
        b = S._Boot(done(tasks()), quick_mode, True, random.Random(4))
        t, _ = run_boot(b, 100, 30)
        assert t <= limit, (quick_mode, t)


def test_slow_tasks_hold_the_boot_until_they_finish():
    ts = tasks()
    b = S._Boot(ts, True, True, random.Random(5))
    t = 0.0
    while t < 1.5:
        b.frame(80, 24, t)
        assert not b.finished(t)
        t += 1 / 30
    done(ts)
    end, _ = run_boot(b, 80, 24, until=3.0)
    assert end < 3.0


def test_skip_fast_forwards():
    b = S._Boot(done(tasks()), False, True, random.Random(6))
    b.frame(100, 30, 0.1)
    b.skip(0.2)
    b.skip(0.25)                                   # idempotent
    assert b.offset == pytest.approx(b.tl.min_t - 0.2)
    t = 0.2 + b.offset
    frames = 0
    while not b.finished(t):
        b.frame(100, 30, t)
        t += 1 / 30
        frames += 1
    assert frames <= 8


def test_resize_mid_animation():
    box = {}
    b = S._Boot(done(tasks()), False, True, random.Random(7), "full", hw_task=hw_task(box), hw_box=box)
    t = 0.0
    sizes = [(100, 30), (60, 18), (140, 40), (40, 12), (20, 8), (80, 24)]
    i = 0
    while not b.finished(t) and t < 4:
        w, h = sizes[i % len(sizes)]
        cv = b.frame(w, h, t)
        assert (cv.w, cv.h) == (w, h) and b.lay["w"] == w
        i += 1
        t += 0.11
    assert b.finished(t)


def test_calm_mode_is_static():
    box = {}
    b = S._Boot(done(tasks()), False, True, random.Random(8), calm=True, hw_task=hw_task(box), hw_box=box)
    a = b.frame(100, 30, 0.0)
    assert b.nominal_t == 0.0
    c = b.frame(100, 30, 0.2)
    assert a.cells == c.cells                      # nothing moves
    assert b.finished(0.35) and "ALL SYSTEMS NOMINAL" in text_of(c) and "T+" not in text_of(c)


def test_compact_layout_keeps_text_clear_of_sparks():
    b = S._Boot(done(tasks()), False, True, random.Random(9), "full")
    t = 0.0
    while t < 1.6:
        cv = b.frame(60, 18, t)
        t += 1 / 30
    row = "".join(ch for ch, _, _ in cv.cells[b.lay["brand_y"]]).strip()
    assert row.replace(" ", "") == fx.MARK + "PULSARCODE"


def ui_with(caps):
    ui = S.UI(caps)
    ui.rng = random.Random(1)
    return ui


def test_boot_mode_selection(monkeypatch):
    monkeypatch.setenv("PULSAR_FX", "auto")
    assert S._boot_mode(ui_with(FakeCaps())) == "anim"
    assert S._boot_mode(ui_with(FakeCaps(tty=False))) == "plain"
    assert S._boot_mode(ui_with(FakeCaps(color=0))) == "plain"
    assert S._boot_mode(ui_with(FakeCaps(motion=False, reduced=True))) == "calm"
    assert S._boot_mode(ui_with(FakeCaps(motion=False, reduced=True, fx_mode="off"))) == "plain"
    assert S._boot_mode(ui_with(FakeCaps(motion=False))) == "plain"
    monkeypatch.setenv("PULSAR_FX", "plain")
    assert S._boot_mode(ui_with(FakeCaps())) == "plain"


def test_plain_boot_joins_tasks_and_prints_only_problems():
    caps = FakeCaps(tty=False, color=0)
    ts = tasks("warn")
    assert S.boot(ui_with(caps), ts, quick=True) == "go"
    assert all(t.done for t in ts)
    assert caps.out.getvalue() == ""               # not a tty: silent


def test_plain_boot_on_a_tty_uses_ascii_glyphs(monkeypatch):
    monkeypatch.setenv("PULSAR_FX", "plain")
    caps = FakeCaps(tty=True, color=0, unicode=False)
    S.boot(ui_with(caps), tasks("fail"), quick=True)
    out = caps.out.getvalue()
    assert "PULSAR CODE" in out and "x Uplink" in out and all(ord(c) < 128 for c in out)


def test_an_effect_crash_falls_back_to_plain(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("fx bug")
    monkeypatch.setattr(S, "_boot_run", boom)
    monkeypatch.setenv("PULSAR_FX", "auto")
    ts = tasks()
    assert S.boot(ui_with(FakeCaps()), ts) == "go" and all(t.done for t in ts)


def test_keyboard_interrupt_is_never_swallowed(monkeypatch):
    def stop(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(S, "_boot_run", stop)
    monkeypatch.setenv("PULSAR_FX", "auto")
    with pytest.raises(KeyboardInterrupt):
        S.boot(ui_with(FakeCaps()), tasks())


def test_hardware_task_fills_the_box(monkeypatch):
    from pulsar import hardware
    monkeypatch.setattr(hardware, "probe", lambda timeout=1.5, **k: {"cpu": "Xeon", "cores": 4, "ram_gb": 16.0,
                                                                        "gpus": [], "env": {}, "arch": "x86_64"})
    box = {}
    t = S._hardware_task(True, box)
    t.start()
    t.thread.join(2)
    assert t.status == "ok" and box["hw"]["cpu"] == "Xeon" and "16 GB" in t.detail


def test_warp_and_hud_are_silent_off_tty():
    caps = FakeCaps(tty=False, color=0)
    S.warp(ui_with(caps), [("line", fx.ICE)])
    assert caps.out.getvalue() == ""


def test_logo_streak_stays_on_canvas():
    for w, h in ((20, 8), (100, 30)):
        for k in (0.0, 0.3, 0.6, 1.0):
            cv = fx.Canvas(w, h)
            S._logo_streak(cv, True, k, fx.MARK + "  PULSAR CODE")
            S._logo_streak(cv, False, k, "@  PULSAR CODE")
            assert "PULSAR" in text_of(cv) or w < 14


def test_clip_helper():
    assert S._clip("hello", 10, True) == "hello"
    assert S._clip("hello world", 6, True) == "hello…"
    assert S._clip("hello world", 6, False) == "hello~"
    assert S._clip("x", 0, True) == ""


def test_ascii_terminals_never_receive_unicode(monkeypatch):
    caps = FakeCaps(tty=True, color=0, unicode=False)
    S.hud(ui_with(caps), [("NVIDIA NIM  ·  kimi  →  relay", fx.ICE)])
    monkeypatch.setenv("PULSAR_FX", "plain")
    ts = [S.Task("Uplink", lambda: ("warn", "nvidia · no key …"))]
    S.boot(ui_with(caps), ts, quick=True)
    out = caps.out.getvalue()
    assert "NVIDIA NIM  -  kimi  ->  relay" in out and "nvidia - no key ~" in out
    assert all(ord(c) < 128 for c in out)
    assert S._fold("café", False) == "caf?" and S._fold("café", True) == "café"


def test_calm_pending_rows_do_not_animate():
    ts = tasks()
    b = S._Boot(ts, False, True, random.Random(8), calm=True)
    a = b.frame(100, 30, 0.0)
    c = b.frame(100, 30, 0.7)
    assert a.cells == c.cells and not b.finished(0.7)


def test_rows_resolve_in_order_and_details_never_type_early():
    ts = [S.Task(label, lambda i=i: ("ok", "zq%d" % i)) for i, label in enumerate(LABELS)]
    b = S._Boot(done(ts), False, True, random.Random(10))
    t = 0.0
    seen = []
    while t < 1.2:
        txt = text_of(b.frame(100, 30, t))
        for i, r in enumerate(b.rows):
            if r.done_t is not None and t < r.done_t:
                assert "zq%d" % i not in txt, (t, i)
            if "zq%d" % i in txt and i not in seen:
                seen.append(i)
        t += 1 / 60
    assert seen == sorted(seen) and len(seen) == len(LABELS)


def test_uplink_never_shows_a_key_after_a_url_or_in_userinfo():
    from pulsar import screens as S
    shown = S.masked_live("http://127.0.0.1:5399/v1 sk-live-SECRET-abcdef0123456789")
    assert shown.startswith("http://127.0.0.1:5399/v1 ") and "SECRET" not in shown and shown.endswith("6789")
    shown = S.masked_live("https://user:pass@host.example/v1 sk-abc0123456789xyz")
    assert "pass" not in shown and "0123456" not in shown
    assert S.masked_live("vertex my-proj-123 us-east5") == "vertex my-proj-123 us-east5"
    assert S.masked_live("http://localhost:11434") == "http://localhost:11434"


def test_custom_profile_ids_tell_endpoints_on_one_server_apart():
    from pulsar import providers as P

    def pid(url):
        det = P.detect(url)
        return S.profile_id_for(det.best, det.base_url)
    openai, anthropic = pid("http://192.168.1.50:8000/v1"), pid("http://192.168.1.50:8000/anthropic")
    assert openai == "custom-192-168-1-50-8000"                    # the usual /v1 base keeps the id it had
    assert anthropic == "custom-192-168-1-50-8000-anthropic"
    assert pid("https://llm.example.com/v1") == pid("https://llm.example.com") == "custom-llm-example-com"
    assert pid("https://gw.example.com/openai/v1") != pid("https://gw.example.com/groq/v1")
    assert S.profile_id_for(P.get("groq"), "https://api.groq.com/openai/v1") == "groq"



OLLAMA_FOUND = [{"provider_id": "ollama", "base_url": "http://127.0.0.1:11434/v1", "port": 11434,
                 "models": ["qwen3:8b", "gpt-oss:20b"]}]


def _plain_uplink(monkeypatch, typed, found_local):
    from pulsar import local as LOC
    monkeypatch.setenv("PULSAR_OFFLINE", "0")
    monkeypatch.setattr(LOC, "discover_local", lambda timeout=0.25, servers=None: found_local)
    monkeypatch.setattr(S, "probe", lambda link, timeout=12.0: (True, "2 models online", 200))
    said = []

    class FakeUI:
        u = True

        def say(self, text="", *a, **k):
            said.append(text)
    import getpass
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": typed)
    return S._uplink_plain(FakeUI(), []), said


def test_plain_uplink_offers_a_local_server_it_finds(monkeypatch):
    link, said = _plain_uplink(monkeypatch, "", OLLAMA_FOUND)
    assert any("Ollama" in t and "127.0.0.1:11434" in t and "2 models" in t for t in said), said
    assert link is not None and link.provider.id == "ollama" and link.profile_id == "ollama"
    assert link.base_url == "http://127.0.0.1:11434/v1" and link.key == ""
    link, said = _plain_uplink(monkeypatch, "", [])                     # nothing found: Enter still means "none"
    assert link is None and not any("Ollama" in t for t in said)


def test_uplink_discovery_stays_off_offline(monkeypatch):
    from pulsar import local as LOC
    monkeypatch.setenv("PULSAR_OFFLINE", "1")
    monkeypatch.setattr(LOC, "discover_local", lambda *a, **k: pytest.fail("offline: no probe"))
    assert S._local_servers() == []


def test_the_handshake_line_names_the_url_the_key_check_asks():
    from pulsar import providers as P
    native = P.custom_provider("http://127.0.0.1:9000/anthropic", native=True)
    assert S.handshake_url(native, "k") == "http://127.0.0.1:9000/anthropic/v1/models"     # beside /v1/messages
    plain = P.custom_provider("http://127.0.0.1:8000/v1")
    assert S.handshake_url(plain, "k") == "http://127.0.0.1:8000/v1/models"
    groq = P.get("groq")
    assert S.handshake_url(groq, "gsk_x") == groq.base_url.rstrip("/") + "/models"


def test_local_server_line():
    assert S.local_server_line(OLLAMA_FOUND[0]) == "Found Ollama on 127.0.0.1:11434 (2 models)"
    one = dict(OLLAMA_FOUND[0], provider_id="vllm", base_url="http://127.0.0.1:8000/v1", models=["m"])
    assert S.local_server_line(one) == "Found vLLM on 127.0.0.1:8000 (1 model)"
