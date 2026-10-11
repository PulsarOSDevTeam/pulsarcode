# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
import io
import random

import pytest

from pulsar import fx
from pulsar import screens as S
from pulsar.term import parse_keys


def test_wordmark_geometry():
    widths = {len(r) for r in fx.WORDMARK}
    assert widths == {fx.WORDMARK_W} and fx.WORDMARK_W <= 52
    assert fx.WORDMARK_H == 7 and "C  O  D  E" in fx.WORDMARK[-1]
    assert len({len(r) for r in fx.WORDMARK_ASCII}) == 1


def test_gradient_endpoints_and_triangle():
    assert fx.grad(fx.SPECTRUM, 0) == fx.EMBER
    assert fx.grad(fx.SPECTRUM, 1) == fx.BEAM
    assert fx.tri(0.25) == 0.25 and fx.tri(1.75) == 0.25
    assert fx.rgb256((255, 255, 255)) == 231 and 16 <= fx.rgb256(fx.FLARE) <= 231


def test_ink_depths():
    assert fx.Ink(0).paint("x", fx.BEAM, True) == "x"
    assert "38;2;54;226;255" in fx.Ink(24).paint("x", fx.BEAM)
    assert "38;5;" in fx.Ink(8).paint("x", fx.BEAM)


def test_diff_renderer_only_repaints_changes():
    out = io.StringIO()
    scr = fx.Screen(out, 24)
    cv = fx.Canvas(20, 5)
    cv.text(0, 0, "hello", fx.ICE)
    scr.present(cv)
    first = out.getvalue()
    out.truncate(0)
    out.seek(0)
    cv2 = fx.Canvas(20, 5)
    cv2.text(0, 0, "hello", fx.ICE)
    cv2.put(10, 3, "#", fx.FLARE)
    scr.present(cv2)
    second = out.getvalue()
    assert "hello" in first and "hello" not in second and "#" in second
    assert len(second) < len(first)


def test_effects_draw_at_many_sizes_without_errors():
    rng = random.Random(1)
    for w, h in ((20, 8), (80, 24), (200, 60)):
        cv = fx.Canvas(w, h)
        rain = fx.Rain(w, h, rng)
        for _ in range(20):
            rain.update(1 / 30)
            rain.draw(cv)
        fx.rings(cv, w // 2, h // 2, 0.4)
        fx.sweep(cv, w // 2, h // 2, 4, 1.0)
        fx.Stars(w, h, rng).draw(cv, 1.0)
        fx.warp(cv, 0.3, rng, fx.warp_stars(rng))
        reveal = fx.reveal_map(True, 0.0, 1.0, rng)
        fx.draw_logo(cv, 1, 1, True, 0.5, shimmer=10, reveal=reveal, rng=rng)
        cv.gbox(0, 0, w, h, fx.SPECTRUM, 0.3)


def test_haptics():
    out = io.StringIO()
    h = fx.Haptics(out, "bell")
    h.error()
    assert out.getvalue() == "\a" and h.flash == 1.0
    assert [h.offset() for _ in range(7)] == list(fx.Haptics.SHAKE) + [0]
    h2 = fx.Haptics(io.StringIO(), "off")
    h2.confirm()
    assert h2.flash == 0


def test_parse_keys():
    assert parse_keys("\x1b[A\x1b[B\r") == ["up", "down", "enter"]
    assert parse_keys("\x1b[200~nvapi-xyz\x1b[201~") == ["paste:nvapi-xyz"]
    assert parse_keys("a") == ["a"]
    assert parse_keys("abc") == ["paste:abc"]
    assert parse_keys("\x1b") == ["esc"]
    assert parse_keys("\x1b[5~\x1b[6~\x7f\x03") == ["pgup", "pgdn", "backspace", "ctrl-c"]
    assert parse_keys("\x1b[1;5A") == []  # unknown CSI swallowed


def test_masked_live_reveals_prefix_only():
    m = S.masked_live("sk-or-v1-supersecretvalue1234")
    assert m.startswith("sk-or-") and m.endswith("1234") and "secret" not in m
    assert S.masked_live("ollama") == "ollama"
    assert S.masked_live("http://localhost:11434") == "http://localhost:11434"


def test_wrap():
    assert S.wrap("aaa bbb ccc", 7) == ["aaa bbb", "ccc"]


def test_no_em_dash_in_ui_copy():
    import pulsar.screens as scr_mod
    import inspect
    assert chr(0x2014) not in inspect.getsource(scr_mod)
    assert chr(0x2014) not in "".join(fx.WORDMARK)


# ---------------------------------------------------------------------------
# v3: color depth, 16-color ink, effects, frame budget, terminal guards
# ---------------------------------------------------------------------------

def _ascii_only(cv):
    return all(ord(ch) < 128 for row in cv.cells for ch, _, _ in row)


def _non_blank(cv):
    return sum(1 for row in cv.cells for ch, _, _ in row if ch != " ")


def test_rgb16_maps_the_brand_by_hue_family():
    assert fx.rgb16(fx.EMBER) == 9 and fx.rgb16(fx.BAD) == 9          # bright red
    assert fx.rgb16(fx.FLARE) == 13 and fx.rgb16(fx.PLASMA) == 12      # bright magenta, bright blue
    assert fx.rgb16(fx.BEAM) == 14 and fx.rgb16(fx.OK) == 10 and fx.rgb16(fx.WARN) == 11
    assert fx.rgb16(fx.ICE) == 15 and fx.rgb16(fx.ASH) == 7
    assert fx.rgb16(fx.NIGHT) == 8 and fx.rgb16(fx.DUST) == 8         # dim tones stay visible: dark gray
    assert fx.rgb16((0, 0, 0)) == 0 and fx.rgb16(fx.scale(fx.EMBER, 0.4)) == 1
    assert all(0 <= fx.rgb16(c) <= 15 for c in fx.ANSI16)


def test_ink_16_color_codes():
    ink = fx.Ink(4)
    assert ink.paint("x", fx.BEAM) == "\x1b[0;96mx\x1b[0m"
    assert ink.code(fx.scale(fx.EMBER, 0.4), True) == "\x1b[0;1;31m"
    assert ink.code(fx.ICE, bg=fx.scale(fx.PLASMA, 0.5)) == "\x1b[0;97;44m"
    assert ink.code(fx.ICE, bg=fx.FLARE).endswith(";105m")
    assert "38;2;" not in ink.gradient("PULSAR") and "38;5;" not in ink.gradient("PULSAR")


def test_rgb256_prefers_the_gray_ramp_for_desaturated_tones():
    assert 232 <= fx.rgb256(fx.NIGHT) <= 255 and 232 <= fx.rgb256((128, 128, 128)) <= 255
    assert fx.rgb256((255, 255, 255)) == 231 and fx.rgb256((0, 0, 0)) == 16
    assert fx.rgb256((255, 0, 0)) == 196 and fx.rgb256((0, 255, 255)) == 51


def test_detect_color_depths():
    from pulsar.term import detect_color
    assert detect_color({"TERM": "xterm-256color"}, tty=False) == 0
    assert detect_color({"TERM": "xterm-256color", "NO_COLOR": "1"}) == 0
    assert detect_color({"TERM": "dumb"}) == 0
    assert detect_color({"TERM": "xterm-256color", "COLORTERM": "truecolor"}, is_win=False) == 24
    assert detect_color({"TERM": "xterm-256color"}, is_win=False) == 8
    assert detect_color({"TERM": "xterm-kitty"}, is_win=False) == 24
    assert detect_color({"TERM": "xterm", "WT_SESSION": "x"}, is_win=False) == 24
    assert detect_color({"TERM": "linux"}, is_win=False) == 4
    assert detect_color({"TERM": "xterm-color"}, is_win=False) == 4
    assert detect_color({"TERM": "xterm"}, is_win=False) == 8
    assert detect_color({}, is_win=True) == 24
    assert detect_color({"TERM": "linux", "PULSAR_COLOR": "24"}, is_win=False) == 24
    assert detect_color({"PULSAR_COLOR": "16"}, tty=False) == 4
    assert detect_color({"PULSAR_COLOR": "256", "NO_COLOR": "1"}) == 8
    assert detect_color({"PULSAR_COLOR": "0", "COLORTERM": "truecolor"}) == 0


def test_detect_unicode():
    from pulsar.term import detect_unicode
    assert detect_unicode({}, "utf-8", is_win=False) is True
    assert detect_unicode({}, "ascii", is_win=False) is False
    assert detect_unicode({"TERM": "linux"}, "utf-8", is_win=False) is False
    assert detect_unicode({"TERM": "linux", "PULSAR_ASCII": "0"}, "utf-8", is_win=False) is True
    assert detect_unicode({"PULSAR_ASCII": "1"}, "utf-8", is_win=False) is False


def test_caps_flags_follow_the_environment(monkeypatch):
    from pulsar.term import Caps
    monkeypatch.setenv("PULSAR_REDUCED_MOTION", "1")
    monkeypatch.setenv("PULSAR_FX", "auto")
    caps = Caps(io.StringIO())
    assert caps.reduced is True and caps.motion is False and caps.fx == "auto" and caps.color == 0
    w, h = caps.size
    assert w >= 20 and h >= 8


def test_glitch_bursts_stay_inside_their_box():
    rng = random.Random(3)
    g = fx.Glitch(rng, [(0.5, 0.1, 1.0)])
    assert g.level(0.4) == 0 and g.level(0.55) > 0 and g.level(0.58) <= 1.0 and g.level(0.7) == 0
    for unicode in (True, False):
        cv = fx.Canvas(40, 10)
        for y in range(10):
            cv.text(0, y, "#" * 40, fx.ASH)
        box = (10, 2, 30, 8)
        before = [row[:] for row in cv.cells]
        for _ in range(30):
            fx.glitch(cv, box, 1.0, rng, unicode)
        for y in range(10):
            for x in range(40):
                if not (box[0] <= x < box[2] and box[1] <= y < box[3]):
                    assert cv.cells[y][x] == before[y][x]
        if not unicode:
            assert _ascii_only(cv)
    cv = fx.Canvas(30, 3)
    cv.text(5, 1, "P U L S A R", fx.EMBER)
    n0 = _non_blank(cv)
    for _ in range(20):
        fx.glitch(cv, (0, 0, 30, 3), 1.0, rng, True, ghost=False)
    assert _non_blank(cv) <= n0                      # no fringes, no noise: text stays legible
    fx.glitch(fx.Canvas(1, 1), (-5, -5, 50, 50), 1.0, rng)    # clipped, never raises


def test_scanline_brightens_and_draws_its_line():
    cv = fx.Canvas(20, 6)
    cv.text(0, 2, "abc", fx.DUST)
    fx.scanline(cv, 2.0, (0, 0, 20, 6), fx.BEAM, 1.0, True)
    ch, fg, _ = cv.get(0, 2)
    assert ch == "a" and sum(fg) > sum(fx.DUST)
    assert cv.get(10, 2)[0] == "─" and cv.get(10, 4) == fx.BLANK
    cv2 = fx.Canvas(20, 6)
    fx.scanline(cv2, 3.0, None, fx.BEAM, 1.0, False)
    assert cv2.get(5, 3)[0] == "-"
    fx.scanline(cv2, 99.0)                              # off canvas: nothing, no error


def test_beams_light_only_blank_cells_and_respect_masks():
    for unicode in (True, False):
        cv = fx.Canvas(80, 24)
        cv.text(0, 0, "KEEP", fx.ICE)
        b = fx.Beams(80, 24, 40, 12)
        b.draw(cv, 1.0, amount=1.0, unicode=unicode, mask=lambda x, y: x < 10)
        assert cv.get(0, 0)[0] == "K" and cv.get(3, 0)[0] == "P"
        lit = _non_blank(cv) - 4
        assert lit > 40
        assert all(cv.cells[y][x] == fx.BLANK for y in range(1, 24) for x in range(10))
        if not unicode:
            assert _ascii_only(cv)
    rays = {fx.ray_glyph(a) for a in (0.0, 1.57, 0.7, -0.7)}
    assert rays == {"─", "│", "╲", "╱"}
    cv = fx.Canvas(10, 4)
    fx.Beams(10, 4, 5, 2, region=(0, 0, 3, 4)).draw(cv, 0.3)
    assert all(cv.cells[y][x] == fx.BLANK for y in range(4) for x in range(3, 10))
    fx.Beams(80, 24, 40, 12).draw(fx.Canvas(80, 24), 1.0, amount=0.0)


def test_particles_fall_fade_and_die():
    rng = random.Random(5)
    p = fx.Particles(rng, gravity=30.0, cap=50)
    p.burst(20, 10, 80, life=(0.2, 0.3))
    assert len(p) == 50                                 # capped
    vy0 = sum(q[3] for q in p.p)
    p.update(0.05)
    assert sum(q[3] for q in p.p) > vy0 * 0.9           # gravity pulls down (+y)
    cv = fx.Canvas(40, 20)
    p.draw(cv, True)
    p.draw(cv, False, mask=lambda x, y: True)
    for _ in range(20):
        p.update(0.05)
    assert len(p) == 0
    p.emit(0, 0, 1, 1, 0.5, fx.BEAM)
    cv = fx.Canvas(5, 5)
    p.draw(cv, False)
    assert _ascii_only(cv)


def test_bar_fill_shimmer_and_ascii():
    cv = fx.Canvas(12, 3)
    fx.bar(cv, 1, 0, 10, 0.0)
    assert [cv.get(x, 0)[0] for x in range(1, 11)] == ["─"] * 10
    fx.bar(cv, 1, 1, 10, 1.0, t=0.3, flash=0.5)
    assert [cv.get(x, 1)[0] for x in range(1, 11)] == ["━"] * 10
    fx.bar(cv, 1, 2, 10, 0.55, unicode=False)
    row = "".join(cv.get(x, 2)[0] for x in range(1, 11))
    assert row.startswith("=====") and row.endswith(".") and all(ord(c) < 128 for c in row)
    fx.bar(cv, 0, 0, 0, 0.5)
    fx.bar(cv, 0, 0, 4, 7.0)                             # clamped


def test_frame_budget_degrades_and_recovers():
    b = fx.FrameBudget("full")
    assert b.fps == 30 and b.on("beams") and b.on("wake") and b.count(100) == 100
    for _ in range(30):
        b.record(0.08)                                   # 80 ms frames: far over budget
    assert b.quality < 0.3 and b.slow > 0 and not b.on("wake") and b.count(100) < 30
    for _ in range(400):
        b.record(0.001)
    assert b.quality == pytest.approx(1.0)
    lite = fx.FrameBudget("lite")
    assert lite.fps == 24 and lite.on("beams") and not lite.on("wake")
    mini = fx.FrameBudget("minimal")
    assert not mini.on("rain") and not mini.on("beams") and not mini.on("particles")
    assert fx.FrameBudget("bogus").level == "full"
    b.retarget("minimal")
    assert b.level == "minimal" and b.quality <= 0.1 and b.fps == 15
    b.begin()
    assert 0 <= b.remaining() <= b.period and b.end() >= 0


def test_flare_shockwave_and_decode_text_are_safe_everywhere():
    rng = random.Random(1)
    for w, h in ((1, 1), (10, 4), (80, 24)):
        for unicode in (True, False):
            cv = fx.Canvas(w, h)
            fx.flare(cv, w // 2, h // 2, 1.0, unicode)
            fx.flare(cv, w // 2, h // 2, 0.7, unicode, vertical=False, length=3)
            fx.shockwave(cv, w / 2, h / 2, 8, 0.9, unicode)
            done = fx.decode_text(cv, 0, 0, "PULSAR CODE", 5.0, 0.0, 0.5, rng, unicode=unicode)
            assert done
            if not unicode:
                assert _ascii_only(cv)
    cv = fx.Canvas(20, 1)
    assert fx.decode_text(cv, 0, 0, "ABC", 0.1, 0.0, 1.0, rng) is False


def test_logo_glow_and_lead():
    cv = fx.Canvas(60, 10)
    fx.draw_logo(cv, 1, 1, True, 0.0, glow=1.0)
    assert cv.get(1, 1)[1] == fx.ICE
    cv = fx.Canvas(60, 10)
    reveal = fx.reveal_map(True, 1.0, 0.5, random.Random(2), jitter=0.0)
    fx.draw_logo(cv, 1, 1, True, 0.0, reveal=reveal, lead=0.3)
    assert _non_blank(cv) == 0                          # nothing shows before start - lead


def test_screen_resume_forces_a_full_repaint_and_close_is_idempotent():
    from pulsar import term
    out = io.StringIO()
    with fx.Screen(out, 24) as scr:
        assert scr in fx._LIVE_SCREENS and term._SCREEN_SEQ
        cv = fx.Canvas(10, 2)
        cv.text(0, 0, "abc")
        scr.present(cv)
        n = len(out.getvalue())
        scr.present(cv)
        assert "abc" not in out.getvalue()[n:]
        term.RESUME_EPOCH += 1
        scr.present(cv)
        assert "abc" in out.getvalue()[n:]
    assert scr not in fx._LIVE_SCREENS and not term._SCREEN_SEQ
    scr.close()
    assert out.getvalue().endswith(fx.Screen.LEAVE)


def test_register_screen_round_trip():
    from pulsar import term
    undo = term.register_screen("L", "E")
    assert ("L", "E") in term._SCREEN_SEQ
    undo()
    undo()
    assert ("L", "E") not in term._SCREEN_SEQ


def test_haptics_pulse_is_silent():
    out = io.StringIO()
    h = fx.Haptics(out, "bell")
    h.pulse(fx.OK, 0.6)
    assert h.flash == 0.6 and h.flash_color == fx.OK and out.getvalue() == ""


def test_new_effects_draw_at_many_sizes():
    rng = random.Random(9)
    for w, h in ((1, 1), (20, 8), (40, 12), (80, 24), (200, 60)):
        cv = fx.Canvas(w, h)
        fx.Beams(w, h, w / 2, h / 2, step=2).draw(cv, 0.7, wake=False)
        p = fx.Particles(rng)
        p.burst(w / 2, h / 2, 30)
        p.update(0.1)
        p.draw(cv)
        fx.Glitch(rng, [(0, 1, 1)]).apply(cv, (0, 0, w, h), 0.5)
        fx.scanline(cv, h / 2)
        fx.bar(cv, 0, h - 1, w, 0.4, 1.0)
        cv.gbox(0, 0, w, h, fx.SPECTRUM, 0.2, draw=0.5)
        cv.clear(-3, -3, w + 6, h + 6)
        assert _non_blank(cv) == 0


@pytest.mark.skipif(not hasattr(__import__("signal"), "SIGTSTP"), reason="POSIX job control only")
def test_ctrl_z_hands_the_terminal_back_and_takes_it_again(monkeypatch):
    import os
    import signal
    from pulsar import term
    r, w = os.pipe()
    kb = term.Keyboard(io.StringIO())
    kb._out_fd = w
    undo = term.register_screen("<leave>", "<enter>")
    stopped = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: stopped.append(sig))
    prev = signal.getsignal(signal.SIGTSTP)
    epoch = term.RESUME_EPOCH
    try:
        kb._on_suspend(signal.SIGTSTP, None)
        assert stopped == [signal.SIGTSTP] and term.RESUME_EPOCH == epoch + 1
        assert signal.getsignal(signal.SIGTSTP) == kb._on_suspend
    finally:
        signal.signal(signal.SIGTSTP, prev)
        undo()
        os.close(w)
    data = os.read(r, 4096).decode()
    os.close(r)
    assert data.index("\x1b[?2004l") < data.index("<leave>") < data.index("<enter>") < data.index("\x1b[?2004h")
