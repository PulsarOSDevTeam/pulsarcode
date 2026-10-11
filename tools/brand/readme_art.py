#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""README artwork that continues the hero: dividers (the recording continues), the Signal Path
(the relay contract), four feature cards, the provider constellation and the footer strip.

    python tools/brand/readme_art.py                 # write assets/readme/*.v3.svg (+ site copies)
    python tools/brand/readme_art.py --providers-md  # print the "All providers" Markdown table

Every loop runs on the CLI's clock (motion.P1 and harmonics), every count is computed from
pulsar.screens.advertised_providers(), and synthetic content is labelled on the frame.
Python 3.9 compatible.
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import hero  # noqa: E402
import motion as M  # noqa: E402
import svgkit as K  # noqa: E402
from svgkit import n  # noqa: E402

ROOT = M.ROOT
OUT = ROOT / "assets" / "readme"
SITE = ROOT / "docs" / "assets" / "brand"
V = hero.VERSION
DIVIDER_LABELS = ("REPLAY", "EXTRAS", "SIGNAL", "MANUAL")
DIVIDER_ALTS = ("Section: See it run", "Section: Features", "Section: How it works", "Section: Do you need Pulsar Code?")


def T(theme: str) -> Dict[str, object]:
    return M.THEMES[theme]


class Css:
    """Collects base rules, keyframes and the reduced-motion poster overrides."""

    def __init__(self, anim: bool) -> None:
        self.anim = anim
        self.base: List[str] = []
        self.moves: List[str] = []
        self.kf: List[str] = []
        self.rm: List[str] = []
        self._names: Dict[str, str] = {}

    def add(self, rule: str) -> None:
        self.base.append(rule)

    def move(self, rule: str) -> None:
        if self.anim:
            self.moves.append(rule)

    def frames(self, name: str, body: str) -> str:
        """Register keyframes once; identical bodies share a name."""
        if body in self._names:
            return self._names[body]
        self._names[body] = name
        if self.anim:
            self.kf.append("@keyframes %s{%s}" % (name, body))
        return name

    def poster(self, rule: str) -> None:
        self.rm.append(rule)

    def text(self) -> str:
        out = "".join(self.base)
        if not self.anim:
            out += "".join(self.rm)      # a -static file is the designed poster: base + poster overrides
        if self.anim:
            out += "".join(self.moves) + "".join(self.kf) + K.reduced_motion("".join(self.rm))
        return out


def window(t0: float, t1: float, L: float, on: str, off: str, fade_in: float = 0.0, fade_out: float = 0.0,
           ease_in: str = "", ease_out: str = "") -> str:
    """Keyframes body for a state that is `on` during [t0, t1] of a loop of length L, `off` otherwise."""
    p = lambda t: K.pct(t / L * 100)  # noqa: E731
    fr = []
    if t0 > 0:
        fr.append("0%%,%s{%s}" % (p(t0), off + (";animation-timing-function:" + ease_in if ease_in else "")))
    else:
        fr.append("0%%{%s}" % on)
    fr.append("%s{%s}" % (p(min(L, t0 + fade_in)) if t0 > 0 or fade_in else "0%", on))
    if t1 < L:
        fr.append("%s{%s}" % (p(max(t0, t1 - fade_out)), on + (";animation-timing-function:" + ease_out if ease_out else "")))
        fr.append("%s,100%%{%s}" % (p(t1), off))
    else:
        fr.append("100%%{%s}" % on)
    return "".join(fr)


# ---------------------------------------------------------------------------
# Telemetry strips: dividers and footer
# ---------------------------------------------------------------------------


def paper_grid(theme: str, w: int, h: int, y0: float = 0) -> str:
    t = T(theme)
    if theme == "dark":
        d = "".join("M%d %sV%s" % (x, n(y0), n(h)) for x in range(40, w, 40))
        return '<path d="%s" stroke="%s" stroke-opacity="%s" fill="none" shape-rendering="crispEdges"/>' % (d, M.DUST, n(t["grid_op"]))
    minor = "".join("M%d %sV%s" % (x, n(y0), n(h)) for x in range(40, w, 40) if x % 200) + \
        "".join("M0 %dH%d" % (y, w) for y in range(int(y0) + 40, h, 40))
    major = "".join("M%d %sV%s" % (x, n(y0), n(h)) for x in range(200, w, 200))
    return ('<path d="%s" stroke="%s" fill="none" shape-rendering="crispEdges"/>'
            '<path d="%s" stroke="%s" fill="none" shape-rendering="crispEdges"/>' % (minor, t["grid"], major, t["grid_major"]))


def pen_keys(pen: float, main: float, spike: float, heights: Optional[Sequence[float]] = None, period: float = hero.PAPER,
             total: float = hero.PAPER) -> List[Tuple[float, float]]:
    keys = []
    s = 0.0
    while s <= total + 1e-6:
        x = pen + s
        v = -spike * hero.trace_profile(x, main)
        if heights is not None:
            v *= heights[int(((x - main + period / 4) % total) // period) % len(heights)]
        keys.append((s / total * 100, v))
        near = min(abs(((x - c) % period + period / 2) % period - period / 2) for c in (main, main + period / 2))
        s += 1.0 if near < 3 * hero.SIGMA else 8.0
    if keys[-1][0] < 100:
        keys.append((100.0, keys[0][1]))
    return keys


def divider(k: int, theme: str, anim: bool = True) -> str:
    """Divider k (1..4): telemetry paper, a pixel label naming the next section, a pen at x 1180."""
    t = T(theme)
    W, H = 1280, 56
    base, spike, pen, x_label = 42.0, 24.0, 1180.0, 24.0
    css = Css(anim)
    main = pen % hero.PAPER                    # a main spike sits under the pen at local time 0
    period = hero.trace_period_path(main, spike, seed=10 + k)
    label = "%02d / %s" % (k, DIVIDER_LABELS[k - 1])
    lpx = 3.0
    lw = K.pixel_text_width(label, lpx)
    x0 = x_label + lw + 36
    col = M.spectrum_at(k / 4.0, t)
    delay = -k * M.Q
    defs = [
        '<path id="tp" d="%s" fill="none" stroke="%s" stroke-width="1.5" stroke-linejoin="round"/>' % (period, t["trace"]),
        K.linear_user("tf", x0, x0 + 200, [(0, "#FFFFFF", 0), (1, "#FFFFFF", 1)]),
        '<mask id="tm" maskUnits="userSpaceOnUse" x="0" y="0" width="%d" height="%d">%s</mask>' % (
            W, H, K.rect(x0, 0, pen - x0, H, fill="url(#tf)")),
    ]
    body = [K.rect(0, 0, W, H, fill=t["bg"]), paper_grid(theme, W, H)]
    start = math.floor(x0 / hero.PAPER) * hero.PAPER
    copies = int(math.ceil((pen + hero.PAPER - start) / hero.PAPER)) + 1
    uses = "".join('<use href="#tp" x="%s" y="%s"/>' % (n(start + i * hero.PAPER), n(base)) for i in range(copies))
    body.append('<g mask="url(#tm)"><g class="pp">%s</g></g>' % uses)
    body.append('<g class="pen">%s</g>' % K.circle(pen, base - spike, 2.5, fill=t["pen"]))
    ly = (H - 7 * lpx) / 2
    body.append('<path class="lb" d="%s" fill="%s"/>' % (K.pixel_text_path(label, x_label, ly, lpx), col))
    css.add(".pen{translate:0 0}.lb{opacity:1}")
    css.move(".pp{animation:pp %s linear %s infinite}" % (K.ms(M.P1), K.ms(delay)))
    css.frames("pp", "from{translate:0 0}to{translate:-%spx 0}" % n(hero.PAPER))
    keys = pen_keys(pen, main, spike)
    css.move(".pen{animation:pen %s linear %s infinite}" % (K.ms(M.P1), K.ms(delay)))
    css.frames("pen", "".join("%s{translate:0 %spx}" % (K.pct(p), n(v + spike, 1)) for p, v in keys))
    g1, g2 = .12 / M.P1 * 100, .52 / M.P1 * 100
    css.move(".lb{opacity:.7;animation:lb %s linear %s infinite}" % (K.ms(M.P1), K.ms(delay)))
    css.frames("lb", "0%%{opacity:1}%s{opacity:1;animation-timing-function:%s}%s,100%%{opacity:.7}" % (
        K.pct(g1), M.E_SETTLE, K.pct(g2)))
    alt = DIVIDER_ALTS[k - 1]
    return K.svg_doc(W, H, "".join(body), css.text(), "".join(defs), title=alt, label=alt)


FOOTER_DECAY = .55          # amplitude ratio per half rotation (geometric), forced to 0 at 2*P1
FOOTER_XFADE = .3           # s: the loop seam is a cross-fade of the paper, never a rescale of it
FOOTER_PHASE = .25          # the main spike reaches the pen P1/4 after the loop starts: the seam falls between pulses


def footer_amp(tau: float) -> float:
    """Amplitude the pen records at loop time tau (s): 1 before the decay starts, a geometric decay
    (ratio FOOTER_DECAY per half rotation, normalised to reach exactly 0) over [0, 2*P1], then 0."""
    if tau <= 0:
        return 1.0
    span = 2 * M.P1
    if tau >= span:
        return 0.0
    steps = span / M.H
    r_end = FOOTER_DECAY ** steps
    return (FOOTER_DECAY ** (tau / M.H) - r_end) / (1 - r_end)


def footer_envelope() -> List[Tuple[float, float]]:
    """(loop %, amplitude written at the pen) for the footer's 4*P1 loop: geometric decay over 2*P1, then flat."""
    loop = 4 * M.P1
    return [(100.0 * i * M.H / loop, footer_amp(i * M.H)) for i in range(9)] + [(100.0, 0.0)]


def footer_strip(main: float, spike: float, x_from: float, pen: float, x_to: float, seed: int = 9,
                 y_from: Optional[float] = None) -> str:
    """The recorded paper as one path in paper coordinates. Paper x passes the pen at loop time
    (x - pen) / v, so everything left of the pen is history at full height, the decay is laid out over
    the 2*P1 of paper right of the pen, and the rest is a flat line. Spikes already on the paper never
    change: only the pen writes smaller ones as the paper scrolls past it."""
    rng = random.Random(seed)
    v = hero.PAPER / M.P1
    pts: List[Tuple[float, float]] = []
    x = x_from
    x_flat = pen + 2 * hero.PAPER
    while x < x_flat:
        a = footer_amp((x - pen) / v)
        near = min(abs(((x - c) % hero.PAPER + hero.PAPER / 2) % hero.PAPER - hero.PAPER / 2)
                   for c in (main, main + hero.PAPER / 2))
        y = -spike * hero.trace_profile(x, main) * a + rng.uniform(-.45, .45) * a
        pts.append((x, y if (pts or y_from is None) else y_from))
        x += 1.0 if (near < 3.2 * hero.SIGMA and a * spike > .3) else 4.0
    pts.append((x_flat, 0.0))
    # deltas between rounded absolute points: no rounding drift along the strip (the flat end is exactly 0)
    pts = [(round(xx, 1), round(yy, 1)) for xx, yy in pts]
    d = ["M%s %s" % (n(pts[0][0], 1), n(pts[0][1], 1))]
    px, py = pts[0]
    for xx, yy in pts[1:]:
        d.append("l%s %s" % (n(xx - px, 1), n(yy - py, 1)))
        px, py = xx, yy
    d.append("H%s" % n(x_to))
    return "".join(d)


def footer(theme: str, anim: bool = True) -> str:
    """End of transmission: the pen writes pulses that decay geometrically over 2*P1, then a flat line.

    The decay is baked into the paper (a chart recorder cannot shrink what it already wrote): the strip
    scrolls past a fixed pen at 160 px per P1 with the hero's main pulse 1.0 / interpulse .22, and only
    the spikes arriving at the pen get smaller. The 4*P1 loop seam is a FOOTER_XFADE cross-fade between
    two copies of the strip (the incoming copy already moving at paper speed). Left of the decay the two
    copies are identical (the loop is exactly four paper periods), so only the re-acquired signal fades in."""
    t = T(theme)
    W, H = 1280, 120
    base, spike, pen = 52.0, 26.0, 1180.0
    main = (pen + FOOTER_PHASE * hero.PAPER) % hero.PAPER
    loop = 4 * M.P1
    shift = 4 * hero.PAPER                     # paper scrolled in one loop
    x0 = 40.0
    css = Css(anim)
    # history at full height: whole periods of the hero's trace tile, up to the last period boundary
    # before the pen; from there one explicit path carries the decay and the flat line
    start = math.floor((x0 - 20) / hero.PAPER) * hero.PAPER
    seam = math.floor(pen / hero.PAPER) * hero.PAPER
    period = hero.trace_period_path(main, spike, seed=9)
    y0 = float(period[1:].split("l")[0].split()[1])
    x_flat_end = pen + 2 * hero.PAPER
    poster = -(x_flat_end - x0 + 4)            # poster: only the flat line is under the window
    x_to = pen - poster + 20
    tiles = "".join('<use href="#tq" x="%s"/>' % n(start + i * hero.PAPER) for i in range(int((seam - start) / hero.PAPER)))
    defs = [
        '<path id="tq" d="%s"/>' % period,
        '<g id="tp" fill="none" stroke="%s" stroke-width="1.5" stroke-linejoin="round">%s<path d="%s"/></g>' % (
            t["trace"], tiles, footer_strip(main, spike, seam, pen, x_to, y_from=y0)),
        K.linear_user("tf", x0, x0 + 220, [(0, "#FFFFFF", 0), (1, "#FFFFFF", 1)]),
        '<mask id="tm" maskUnits="userSpaceOnUse" x="0" y="0" width="%d" height="%d">%s</mask>' % (
            W, H, K.rect(x0, 0, pen - x0, H, fill="url(#tf)")),
    ]
    body = [K.rect(0, 0, W, H, fill=t["bg"]), paper_grid(theme, W, H)]
    strip = '<use href="#tp" y="%s"/>' % n(base)
    if anim:
        body.append('<g mask="url(#tm)"><g class="pa">%s</g><g class="pb">%s</g></g>' % (strip, strip))
    else:
        body.append('<g mask="url(#tm)"><g class="pa">%s</g></g>' % strip)
    # the mark at the centre, on a bg disc so the line passes behind it
    cx, cy = W / 2, base
    body.append(K.circle(cx, cy, 30, fill=t["bg"]))
    body.append('<g transform="translate(%s %s) scale(.9)">%s</g>' % (n(cx - 32 * .9), n(cy - 32 * .9), mark_body(theme, "f")))
    body.append('<g class="pen">%s</g>' % K.circle(pen, base, 2.5, fill=t["pen"]))
    label = "END OF TRANSMISSION"
    px = 2.0
    lw = K.pixel_text_width(label, px)
    body.append('<path d="%s" fill="%s" opacity=".7"/>' % (K.pixel_text_path(label, cx - lw / 2, 96, px), t["ink"]))
    css.add(".pen{translate:0 0}.pb{opacity:0}")
    css.poster(".pa{translate:%spx 0}" % n(poster))
    v = hero.PAPER / M.P1
    xf = FOOTER_XFADE / loop * 100
    css.move(".pa{animation:pa %s linear infinite}.pb{animation:pb %s linear infinite}" % (K.ms(loop), K.ms(loop)))
    css.frames("pa", "0%%{translate:0 0;opacity:1}%s{opacity:1;animation-timing-function:%s}100%%{translate:-%spx 0;opacity:0}" % (
        K.pct(100 - xf), M.E_SETTLE, n(shift)))
    # the incoming copy shows the loop's first frame, moving at paper speed while it fades in
    css.frames("pb", "0%%,%s{translate:%spx 0;opacity:0;animation-timing-function:%s}100%%{translate:0 0;opacity:1}" % (
        K.pct(100 - xf), n(v * FOOTER_XFADE, 2), M.E_SETTLE))
    # the pen rides what it writes (one sampled track over the whole loop)
    keys = []
    tt = 0.0
    while tt <= loop + 1e-6:
        s = v * tt
        a = footer_amp(tt)
        val = -spike * hero.trace_profile(pen + s, main) * a
        keys.append((tt / loop * 100, val))
        near = min(abs(((pen + s - c) % hero.PAPER + hero.PAPER / 2) % hero.PAPER - hero.PAPER / 2)
                   for c in (main, main + hero.PAPER / 2))
        # sigma is 6 px = 0.19 s of paper: 50 ms keys resolve a spike, linear between them
        tt += .4 if a < 1e-3 else (.05 if near < 3 * hero.SIGMA and a * spike > 1 else .2)
    dedup = [keys[0]]
    for k_ in keys[1:]:
        if abs(k_[1]) < .05 and abs(dedup[-1][1]) < .05 and len(dedup) > 1 and abs(dedup[-2][1]) < .05:
            dedup[-1] = k_
        else:
            dedup.append(k_)
    css.move(".pen{animation:pen %s linear infinite}" % K.ms(loop))
    css.frames("pen", "".join("%s{translate:0 %spx}" % (K.pct(p), n(v_, 1)) for p, v_ in dedup) + "100%{translate:0 0}")
    return K.svg_doc(W, H, "".join(body), css.text(), "".join(defs) + mark_defs(theme, "f"), title="End of transmission",
                     label="End of transmission: the pulse trace flattens beside the Pulsar Code mark")


# ---------------------------------------------------------------------------
# The mark (shared with mark.py)
# ---------------------------------------------------------------------------


def mark_defs(theme: str, p: str = "m") -> str:
    dark = theme == "dark"
    core = M.ICE if dark else "#1B1530"
    cols = T(theme)["spectrum"]
    return (K.radial(p + "g", [(0, core, .9 if dark else .45), (.4, M.BEAM if dark else "#6B3FD1", .3 if dark else .14),
                               (1, M.PLASMA if dark else "#6B3FD1", 0)])
            + K.linear_user(p + "n", 0, 0, [(0, core, 1), (1, M.BEAM if dark else "#0A8FB0", .15)], y1=0, y2=-30)
            + K.linear_user(p + "s", 0, 0, [(0, core, 1), (1, M.PLASMA if dark else "#7A2FF0", .15)], y1=0, y2=30)
            + K.linear_user(p + "r", -26, 26, [(o, c, 1) for o, c in zip(M.SPECTRUM_STOPS, cols)]))  # type: ignore[arg-type]


def mark_body(theme: str, p: str = "m", size: float = 64, ring: bool = True, glow: bool = True) -> str:
    """The mark on a 64 grid: ICE core, one bipolar beam at fx's t = 0 angle, one tilted ring."""
    dark = theme == "dark"
    core = M.ICE if dark else "#1B1530"
    c = 32
    rot = M.A1_0 + 90             # wedges are drawn along -y (north); rotated so north points at A1_0
    out = []
    if glow:
        out.append(K.circle(c, c, 20, fill="url(#%sg)" % p))
    if ring:
        # long axis 90 deg off the beam: two rays plus a ring, never a starburst
        out.append('<g transform="translate(%d %d) rotate(%s)"><ellipse rx="22" ry="%s" fill="none" stroke="url(#%sr)" '
                   'stroke-width="2"/></g>' % (c, c, n(M.A1_0 + 90, 2), n(22 * .42), p))
    beam = ('<g transform="translate(%d %d) rotate(%s)"><path d="M-7 0L-.5 -30H.5L7 0Z" fill="url(#%sn)"/>'
            '<path d="M-7 0L-.5 30H.5L7 0Z" fill="url(#%ss)"/></g>' % (c, c, n(rot, 2), p, p))
    out.append(beam)
    out.append(K.circle(c, c, 7, fill=core))
    if not dark:
        out.append(K.circle(c, c, 9.5, fill="none", stroke="#6B3FD1", stroke_width=1.5))
    return "".join(out)


# ---------------------------------------------------------------------------
# Signal Path: the relay contract, animated (illustrative, time compressed)
# ---------------------------------------------------------------------------


def engine_wires() -> List[str]:
    """Wire names from the engine docstring (chat, responses, native)."""
    import re
    src = (ROOT / "pulsar" / "core" / "engine.py").read_text(encoding="utf-8")
    block = src.split("Three wires per target", 1)[1].split('"""', 1)[0]
    wires = re.findall(r"^\s{2}(\w+)\s{2,}", block, flags=re.M)
    assert wires == ["chat", "responses", "native"], wires
    return wires


def box(x: float, y: float, w: float, h: float, stroke: str, fill: str, cls: str = "", r: float = 8) -> str:
    return K.rect(x + .5, y + .5, w - 1, h - 1, rx=r, fill=fill, stroke=stroke, class_=cls or None)


def eyebrow(x: float, y: float, text: str, fill: str, anchor: str = "start", **kw: object) -> str:
    return ('<text x="%s" y="%s" font-size="11" letter-spacing="2" fill="%s"%s%s>%s</text>'
            % (n(x), n(y), fill, ' text-anchor="%s"' % anchor if anchor != "start" else "", K.attrs(**kw), K.esc(text.upper())))


STREAM = (3.0, 7.8)          # s: token stream window in the Signal Path loop (after the commit, before the fade)


def signal(theme: str, anim: bool = True) -> str:
    t = T(theme)
    dark = theme == "dark"
    W, H = 1280, 320
    L = M.L
    css = Css(anim)
    ink, dim, rule = t["ink"], t["dim"], t["rule"]
    panel = "#0D0B15" if dark else "#FFFFFF"
    route = t["route"]  # type: ignore[assignment]
    adapter = route["adapter"]  # type: ignore[index]
    bad, ok = t["bad"], t["ok"]
    wires = engine_wires()
    e = M.readout_entries()[0]                      # OpenRouter, from the code
    css.add("text{font-family:%s}.s{font-family:%s}" % (M.MONO, M.SANS))
    body = [K.rect(0, 0, W, H, fill=t["bg"])]
    body.append(eyebrow(50, 34, "Illustrative %s time compressed" % M.DOT, dim))
    # geometry (12 columns of 80 px, 20 px gutters, 50 px margins)
    KEY = (50, 64, 180, 56)
    DET = (330, 64, 260, 56)
    CORE = (690, 64, 240, 192)
    T1 = (1050, 64, 180, 56)
    T2 = (1050, 148, 180, 56)
    CC = (50, 200, 280, 56)
    for (x, y, w, h) in (KEY, DET, T1, T2, CC):
        body.append(box(x, y, w, h, rule, panel))
    body.append(box(*CORE, stroke=rule, fill=panel))
    body.append(K.rect(CORE[0] + .5, CORE[1] + .5, CORE[2] - 1, 2, fill="url(#spec)"))

    def label(b: Tuple[int, int, int, int], top: str, main: str, main_fill: str = "", cls: str = "") -> str:
        x, y, w, h = b
        return eyebrow(x + 14, y + 20, top, dim) + (
            '<text x="%s" y="%s" font-size="14" fill="%s"%s>%s</text>' % (
                n(x + 14), n(y + 41), main_fill or ink, ' class="%s"' % cls if cls else "", K.esc(main)) if main else "")
    # KEY chip: typing sk-or- in 4 steps, then bullets
    body.append(label(KEY, "key", ""))
    pre = e["prefix"]
    key_x, key_y = KEY[0] + 14, KEY[1] + 41
    cw = 14 * M.MONO_ADVANCE
    body.append('<g class="xf"><g clip-path="url(#kc)"><text x="%s" y="%s" font-size="14" class="kp" fill="%s">%s</text></g></g>' % (
        n(key_x), n(key_y), ink, K.esc(pre)))
    body.append('<g class="xf"><text x="%s" y="%s" font-size="14" fill="%s" class="kb">%s</text></g>' % (
        n(key_x + len(pre) * cw), n(key_y), M.DUST if dark else "#9C94BC", M.BULLET * 6))
    defs = ['<clipPath id="kc"><rect class="kr" x="%s" y="%s" width="%s" height="20"/></clipPath>' % (
        n(key_x - 1), n(key_y - 15), n(len(pre) * cw + 2))]
    # DETECT
    body.append(label(DET, "detect %s longest prefix wins" % M.DOT, ""))
    dx, dy = DET[0] + 14, DET[1] + 41
    det_line = '<text x="%s" y="%s" font-size="14" class="dt"><tspan fill="%s">%s</tspan><tspan fill="%s">  %s  </tspan><tspan fill="%s">%s</tspan></text>' % (
        n(dx), n(dy), adapter, K.esc(pre), dim, M.ARROW, ink, e["id"])
    body.append('<g class="xf">%s</g>' % det_line)
    # the shorter candidate, struck through
    sx = DET[0] + DET[2] - 60
    body.append('<g class="dc"><text x="%s" y="%s" font-size="14" fill="%s">sk-</text>%s</g>' % (
        n(sx), n(dy), dim, K.rect(sx - 2, dy - 5, 3 * cw + 4, 1.2, fill=bad)))
    # CORE
    cx_, cy_, cw_, ch_ = CORE
    body.append(eyebrow(cx_ + 16, cy_ + 26, "pulsar core", dim))
    body.append('<text x="%s" y="%s" font-size="15" fill="%s">%s</text>' % (n(cx_ + 16), n(cy_ + 50), ink, M.CORE_HOST))
    body.append('<text x="%s" y="%s" font-size="12" fill="%s">token-gated %s loopback</text>' % (
        n(cx_ + 16), n(cy_ + 70), dim, M.DOT))
    # wire chips (from the engine docstring); the OpenRouter route uses the chat wire
    wx = cx_ + 16
    wy = cy_ + 92
    body.append(eyebrow(wx, wy, "wire", dim))
    chip_x = wx
    for w in wires:
        ww = len(w) * 12 * M.MONO_ADVANCE + 16
        active = w == e["wire"].split()[0]
        body.append(K.rect(chip_x, wy + 10, ww, 22, rx=11, fill=(t["chip"] if not active else "none"),
                           stroke=adapter if active else t["chip_stroke"]))
        body.append('<text x="%s" y="%s" font-size="12" fill="%s">%s</text>' % (n(chip_x + 8), n(wy + 25), adapter if active else dim, w))
        chip_x += ww + 6
    body.append('<text x="%s" y="%s" font-size="12" fill="%s" class="s">holds the line with pings</text>' % (
        n(cx_ + 16), n(cy_ + 150), dim))
    body.append('<text x="%s" y="%s" font-size="12" fill="%s" class="s">until a target commits</text>' % (
        n(cx_ + 16), n(cy_ + 166), dim))
    # targets (session plan order, top to bottom)
    body.append(eyebrow(T1[0], T1[1] - 8, "session plan", dim))
    for i, b in enumerate((T1, T2)):
        body.append(label(b, "target %d" % (i + 1), ""))
    body.append('<g class="xf"><g class="t1">%s</g>' % box(*T1, stroke=bad, fill="none", cls="t1e"))
    body.append('<text x="%s" y="%s" font-size="14" fill="%s" class="t1c">503 %s next</text></g>' % (
        n(T1[0] + 14), n(T1[1] + 41), bad, M.ARROW))
    body.append('<g class="t2f">%s</g>' % K.rect(T2[0] + 1, T2[1] + 1, T2[2] - 2, T2[3] - 2, rx=8, fill=ok, opacity=".25"))
    body.append('<g class="xf"><text x="%s" y="%s" font-size="14" fill="%s" class="t2c">200</text>' % (n(T2[0] + 14), n(T2[1] + 41), ok))
    stamp_x, stamp_y = T2[0] + 62, T2[1] + 33
    body.append('<g class="st"><g transform="translate(%s %s) rotate(-6)">%s<text x="0" y="5" font-size="12" '
                'font-weight="700" letter-spacing="2" fill="%s" text-anchor="middle">COMMIT</text></g></g></g>' % (
                    n(stamp_x + 46), n(stamp_y + 4), K.rect(-44, -11, 88, 22, rx=4, fill="none", stroke=ok, stroke_width=1.5), ok))
    # CLAUDE CODE
    body.append(label(CC, "claude code %s unmodified" % M.DOT, ""))
    body.append('<text x="%s" y="%s" font-size="14" fill="%s">&gt; <tspan fill="%s">streaming reply</tspan></text>' % (
        n(CC[0] + 14), n(CC[1] + 41), ink, dim))
    # counter beside Claude Code
    cnt_x = CC[0] + CC[2] - 16
    # arrows / lines
    ln = M.DUST if dark else "#B6ACD6"
    y_top = KEY[1] + 28
    paths = [
        "M%s %sH%s" % (n(KEY[0] + KEY[2]), n(y_top), n(DET[0])),
        "M%s %sH%s" % (n(DET[0] + DET[2]), n(y_top), n(cx_)),
        "M%s %sH%s" % (n(cx_ + cw_), n(y_top), n(T1[0])),
        "M%s %sC%s %s %s %s %s %s" % (n(cx_ + cw_), n(y_top + 20), n(cx_ + cw_ + 70), n(y_top + 20), n(T2[0] - 70),
                                       n(T2[1] + 28), n(T2[0]), n(T2[1] + 28)),
        "M%s %sH%s" % (n(cx_), n(CC[1] + 28), n(CC[0] + CC[2])),
    ]
    body.append('<path d="%s" stroke="%s" fill="none" stroke-width="1.2"/>' % ("".join(paths), ln))
    for ax, ay in ((DET[0], y_top), (cx_, y_top), (T1[0], y_top), (T2[0], T2[1] + 28)):
        body.append('<path d="M%s %sl-7 -4v8z" fill="%s"/>' % (n(ax), n(ay), ln))
    body.append('<path d="M%s %sl7 -4v8z" fill="%s"/>' % (n(CC[0] + CC[2]), n(CC[1] + 28), ln))
    body.append(eyebrow((cx_ + CC[0] + CC[2]) / 2, CC[1] + 18, "stream", dim, "middle"))
    # ping dots (poster: three dots on the stream line, labelled)
    yl = CC[1] + 28
    ping_y = yl
    xs_ping = (cx_ - 60, cx_ - 150, cx_ - 240)
    pings = "".join('<g class="pg pg%d">%s</g>' % (i, K.circle(px, ping_y, 3.5, fill=M.ICE if dark else "#6B3FD1"))
                    for i, px in enumerate(xs_ping))
    body.append(pings)
    body.append('<text x="%s" y="%s" font-size="11" fill="%s" class="pl">ping</text>' % (n(cx_ - 160), n(ping_y + 18), dim))
    # packet (6 px capsule, r 3 head, 24 px spectrum tail)
    pk = ('<g class="pk"><g class="pky"><rect x="-27" y="-3" width="27" height="6" rx="3" fill="url(#tail)"/>%s</g></g>'
          % K.circle(0, 0, 3, fill=M.ICE if dark else "#1B1530"))
    body.append(pk)
    defs.append(K.linear_user("tail", -27, 0, [(0, M.EMBER if dark else "#D94F00", 0), (.5, M.FLARE if dark else "#D6156A", .6),
                                               (1, M.BEAM if dark else "#0A8FB0", 1)]))
    defs.append(K.linear_user("spec", cx_, cx_ + cw_, K.spectrum_pairs(t)))
    # token pills: T2 -> Core -> Claude Code, one steady stream over STREAM (3.0 to 7.8 s): the spacing
    # is computed so the last pill lands at 7.8 s (a fixed 90 ms spacing would end the stream at 5.8 s
    # and leave a third of the loop static)
    n_tok = 14
    v = 240.0                 # px/s along the wires
    xa0, xa1 = T2[0] - 4, cx_ + cw_ + 4
    xb0, xb1 = cx_ - 4, CC[0] + CC[2] + 8
    ta = (xa0 - xa1) / v
    tb = (xb0 - xb1) / v
    hide = .12
    tot = ta + hide + tb
    gap_t = (STREAM[1] - STREAM[0] - tot) / (n_tok - 1)
    pills = []
    for i in range(n_tok):
        c = M.spectrum_at(i / (n_tok - 1), t)
        pills.append('<g class="tk" style="animation-delay:%s"><rect x="-6" y="-2.5" width="12" height="5" rx="2.5" fill="%s"/></g>' % (
            K.ms(STREAM[0] + i * gap_t), c))
    body.append('<g class="tks">%s</g>' % "".join(pills))
    # counter: 0..14 tokens received (steps)
    lh = 18
    stack = "".join('<text x="%s" y="%s" font-size="12" fill="%s" text-anchor="end">%d %s</text>' % (
        n(cnt_x), n(CC[1] + 41 + i * lh), adapter, i, "token" if i == 1 else "tokens") for i in range(1, n_tok + 1))
    defs.append('<clipPath id="cc">%s</clipPath>' % K.rect(cnt_x - 90, CC[1] + 28, 94, lh))
    body.append('<g class="xf"><g clip-path="url(#cc)"><g class="ct">%s</g></g></g>' % stack)
    # ---------------------------------------------------------------- timeline (loop L)
    css.add(".dc{opacity:0}.pk{opacity:0}.tk{opacity:0}.pl{opacity:1}.ct{translate:0 -%spx}" % n(lh * n_tok))
    css.add(".pg{opacity:1}")
    fade = (9.6, L)
    # key typing: reveal width in 4 steps of 100 ms
    steps = [2, 4, 5, 6]
    kw = [n(s * cw + 2) for s in steps]
    css.move(".kr{animation:kr %s linear infinite}" % K.ms(L))
    css.frames("kr", "0%%{width:0;animation-timing-function:steps(1,end)}%s" % "".join(
        "%s{width:%spx;animation-timing-function:steps(1,end)}" % (K.pct((i + 1) * .1 / L * 100), w) for i, w in enumerate(kw)))
    css.move(".kb{animation:kb %s linear infinite}" % K.ms(L))
    css.frames("kb", window(.4, L, L, "opacity:1", "opacity:0", 0, 0, "steps(1,end)"))
    css.move(".kp{animation:kp %s linear infinite}" % K.ms(L))
    css.frames("kp", "0%%,%s{fill:%s}%s{fill:%s;animation-timing-function:%s}%s,100%%{fill:%s}" % (
        K.pct(.45 / L * 100), ink, K.pct(.48 / L * 100), adapter, M.E_SETTLE, K.pct(1.2 / L * 100), adapter))
    css.add(".kp{fill:%s}" % adapter)
    css.move(".dt{animation:dt %s linear infinite}" % K.ms(L))
    css.frames("dt", window(.45, L, L, "opacity:1", "opacity:0", .12, 0, "steps(3,end)"))
    css.move(".dc{animation:dc %s linear infinite}" % K.ms(L))
    css.frames("dc", "0%%,%s{opacity:0}%s{opacity:.9}%s{opacity:.9;animation-timing-function:%s}%s,100%%{opacity:0}" % (
        K.pct(.45 / L * 100), K.pct(.6 / L * 100), K.pct(.75 / L * 100), M.E_SETTLE, K.pct(1.1 / L * 100)))
    # packet: core -> target 1 (E_COMMIT), then the curved re-route to target 2 (x settle, y spring)
    x_out, x_t1, x_t2 = cx_ + cw_ + 2, T1[0] - 2, T2[0] - 2
    y_a, y_b = y_top, T2[1] + 28
    p = lambda s: K.pct(s / L * 100)  # noqa: E731
    css.move(".pk{animation:pk %s linear infinite}" % K.ms(L))
    css.frames("pk", "0%%,%s{opacity:0;translate:%spx %spx;animation-timing-function:%s}%s{opacity:1;translate:%spx %spx}"
               "%s{opacity:1;translate:%spx %spx;animation-timing-function:steps(1,end)}%s{opacity:0;translate:%spx %spx}"
               "%s{opacity:1;translate:%spx %spx;animation-timing-function:%s}%s{opacity:1;translate:%spx %spx}"
               "%s,100%%{opacity:0;translate:%spx %spx}" % (
                   p(.8), n(x_out), n(y_a), M.E_COMMIT, p(.81), n(x_out), n(y_a),
                   p(1.25), n(x_t1), n(y_a), p(1.26), n(x_out), n(y_a + 20),
                   p(2.4), n(x_out), n(y_a + 20), M.E_SETTLE, p(2.9), n(x_t2), n(y_a + 20), p(2.92), n(x_t2), n(y_a + 20)))
    css.add(".pk{translate:%spx %spx}" % (n(x_t2), n(y_a + 20)))
    css.move(".pky{animation:pky %s linear infinite}" % K.ms(L))
    css.frames("pky", "0%%,%s{translate:0 0;%s}%s,100%%{translate:0 %spx}" % (
        p(2.4), M.timing("spring"), p(2.9), n(y_b - y_a - 20)))
    css.add(".pky{translate:0 %spx}" % n(y_b - y_a - 20))
    # target 1 answers 503 at 1.25: BAD edge pulse 120 ms, the box shakes 3 px; the marker stays until the fade
    css.add(".t1e{opacity:0}")
    css.move(".t1e{animation:t1e %s linear infinite}" % K.ms(L))
    css.frames("t1e", "0%%,%s{opacity:0}%s{opacity:1}%s{opacity:1;animation-timing-function:%s}%s,%s{opacity:.35;"
               "animation-timing-function:%s}100%%{opacity:.35}" % (p(1.25), p(1.26), p(1.37), M.E_SETTLE, p(1.8), p(fade[0]),
                                                                  M.E_SETTLE))
    css.add(".t1e{opacity:.35}")
    css.move(".t1{animation:t1 %s linear infinite}" % K.ms(L))
    sh = M.SHAKE
    css.frames("t1", "0%%,%s{translate:0 0}%s%s,100%%{translate:0 0}" % (
        p(1.25), "".join("%s{translate:%spx %spx}" % (p(1.25 + .42 * q / 100), n(x * .5), n(y * .5)) for q, x, y in sh[1:-1]),
        p(1.67)))
    css.move(".t1c{animation:t1c %s linear infinite}" % K.ms(L))
    css.frames("t1c", window(1.25, L, L, "opacity:1", "opacity:0", M.F, 0, "steps(3,end)"))
    # pings 1.25-2.40: three dots, 200 ms apart, Core -> Claude Code
    span = (cx_ - 4) - (CC[0] + CC[2] + 8)
    for i, px in enumerate(xs_ping):
        t0 = 1.3 + i * .2
        dur = .9
        x_start = cx_ - 4 - px
        x_end = (CC[0] + CC[2] + 8) - px
        css.move(".pg%d{animation:pg%d %s linear infinite}" % (i, i, K.ms(L)))
        css.frames("pg%d" % i, "0%%,%s{opacity:0;translate:%spx 0}%s{opacity:1;translate:%spx 0}%s{opacity:1;translate:%spx 0}"
                   "%s,100%%{opacity:0;translate:%spx 0}" % (
                       p(t0), n(x_start), p(t0 + .05), n(x_start - span * .05 / dur), p(t0 + dur - .05),
                       n(x_end + span * .05 / dur), p(t0 + dur), n(x_end)))
    css.move(".pl{animation:pl %s linear infinite}" % K.ms(L))
    css.frames("pl", window(1.25, 2.6, L, "opacity:1", "opacity:0", .15, .2, M.E_SETTLE, M.E_SETTLE))
    # 2.90 target 2 answers 200; COMMIT stamp drops in with the spring; 66 ms impact flash
    css.add(".t2f{opacity:0}")
    css.move(".t2f{animation:t2f %s linear infinite}" % K.ms(L))
    css.frames("t2f", "0%%,%s{opacity:0}%s{opacity:1}%s{opacity:1;animation-timing-function:%s}%s,100%%{opacity:0}" % (
        p(2.9), p(2.91), p(2.97), M.E_IMPACT, p(3.4)))
    css.move(".t2c{animation:t2c %s linear infinite}" % K.ms(L))
    css.frames("t2c", window(2.9, L, L, "opacity:1", "opacity:0", .1, 0, "steps(3,end)"))
    css.add(".st{transform-box:fill-box;transform-origin:50% 50%}")
    css.move(".st{animation:st %s linear infinite}" % K.ms(L))
    css.frames("st", "0%%,%s{opacity:0;scale:1.6;%s}%s,100%%{opacity:1;scale:1}" % (
        p(2.9), M.timing("spring"), p(3.52)))
    # 3.00-7.80: 14 token pills stream right to left: T2 -> Core (hidden inside) -> Claude Code
    ya, yb = T2[1] + 28, CC[1] + 28
    q = lambda s: K.pct(s / L * 100)  # noqa: E731
    css.move(".tk{animation:tk %s linear infinite}" % K.ms(L))
    css.frames("tk", "0%%{opacity:0;translate:%spx %spx}%s{opacity:1}%s{opacity:1;translate:%spx %spx}%s{opacity:0;translate:%spx %spx}"
               "%s{opacity:0;translate:%spx %spx}%s{opacity:1}%s{opacity:1;translate:%spx %spx}%s,100%%{opacity:0;translate:%spx %spx}" % (
                   n(xa0), n(ya), q(.05), q(ta - .02), n(xa1), n(ya), q(ta), n(xa1), n(ya),
                   q(ta + hide), n(xb0), n(yb), q(ta + hide + .03), q(tot - .03), n(xb1), n(yb), q(tot), n(xb1), n(yb)))
    # counter ticks as each pill arrives
    arr = [STREAM[0] + i * gap_t + tot for i in range(n_tok)]
    fr = ["0%{translate:0 0;animation-timing-function:steps(1,end)}"]
    for i, ta_ in enumerate(arr):
        fr.append("%s{translate:0 %spx;animation-timing-function:steps(1,end)}" % (q(ta_), n(-lh * (i + 1))))
    fr.append("100%%{translate:0 %spx}" % n(-lh * n_tok))
    css.move(".ct{animation:ct %s linear infinite}" % K.ms(L))
    css.frames("ct", "".join(fr))
    # the loop as a timeline: what happens, in order (one packet in flight, strictly sequential)
    tx0, tx1, ty = 50.0, 1230.0, 290.0
    tx = lambda s: tx0 + (tx1 - tx0) * s / L  # noqa: E731
    body.append(K.rect(tx0, ty, tx1 - tx0, 1, fill=rule))
    body.append('<g class="xf">%s</g>' % K.rect(tx0, ty - .5, tx1 - tx0, 2, fill="url(#tl)", class_="tl"))
    defs.append(K.linear_user("tl", tx0, tx1, K.spectrum_pairs(t)))
    events = ((0.0, "key", -1), (.45, "detect", 1), (.8, "send", -1), (1.25, "503 %s ping" % M.DOT, 1), (2.4, "retry", -1),
              (2.9, "200 %s commit" % M.DOT, 1), (STREAM[0], "stream", -1), (round(arr[-1], 2), "hold", 1))
    for s_, lab, side in events:
        x = tx(s_)
        body.append(K.rect(x - .5, ty - 4, 1, 9, fill=dim))
        body.append('<text x="%s" y="%s" font-size="10" letter-spacing="1.5" fill="%s">%s</text>' % (
            n(x + 4 if s_ else x), n(ty - 7 if side < 0 else ty + 17), dim, K.esc(lab.upper())))
    body.append('<g class="xf"><g class="ph">%s</g></g>' % K.circle(tx0, ty + .5, 3, fill=M.ICE if dark else "#1B1530"))
    # loop seam: every dynamic element (key text, detect, 503, COMMIT, counter, progress) sits in an .xf
    # group and they all dissolve together, one 450 ms E_SETTLE fade from 9.6 s. Each child is back in its
    # empty start state when the loop wraps, so nothing pops; the schematic and the timeline axis and
    # labels are chrome and never fade.
    css.move(".xf{animation:xf %s linear infinite}" % K.ms(L))
    css.frames("xf", "0%%,%s{opacity:1;animation-timing-function:%s}100%%{opacity:0}" % (p(fade[0]), M.E_SETTLE))
    css.add(".tl{transform-box:fill-box;transform-origin:0 50%%;scale:%s 1}.ph{translate:%spx 0}" % (
        n(2.9 / L, 4), n(tx(2.9) - tx0)))
    css.move(".tl{animation:tl %s linear infinite}.ph{animation:ph %s linear infinite}" % (K.ms(L), K.ms(L)))
    css.frames("tl", "from{scale:0 1}to{scale:1 1}")
    css.frames("ph", "from{translate:0 0}to{translate:%spx 0}" % n(tx1 - tx0))
    # poster (2.90 s): 503 on target 1, COMMIT on target 2, ping dots shown, counter blank
    css.add(".ct{translate:0 0}")
    css.poster(".kr{width:%spx}.pk{opacity:0}" % kw[-1])
    alt = ("Signal Path (illustrative): a masked sk-or- key is matched by its longest prefix, Pulsar Core on 127.0.0.1 "
           "sends the request to target 1, which answers 503 before commit, keeps Claude Code's line open with pings, "
           "tries target 2, which commits with 200, and then streams only from target 2")
    return K.svg_doc(W, H, "".join(body), css.text(), "".join(defs), title="Signal Path", label=alt)


# ---------------------------------------------------------------------------
# Feature cards (600 x 340): Stage, Council, Pilot, Observatory
# ---------------------------------------------------------------------------

CARDS = ("stage", "council", "pilot", "observatory")
CW_, CH_ = 600, 340
PILOT_SCALE = 0.6        # the Pilot card is a miniature desktop: 1 card px = 1/0.6 desktop px


class Card:
    def __init__(self, name: str, theme: str, anim: bool) -> None:
        self.name, self.theme, self.anim = name, theme, anim
        self.t = T(theme)
        self.dark = theme == "dark"
        self.i = CARDS.index(name)
        self.css = Css(anim)
        self.defs: List[str] = []
        self.body: List[str] = []
        self.offset = -self.i * M.Q          # card i starts at -i*Q: the 2x2 grid reads in order
        self._k = 0

    def loop(self, sel: str, name: str, frames: str, timing: str = "linear", delay: float = 0.0) -> None:
        """An L-long infinite animation on `sel`, phase-shifted by the card offset."""
        kname = self.css.frames(name, frames)
        self.css.move("%s{animation:%s %s %s %s infinite}" % (sel, kname, K.ms(M.L), timing, K.ms(delay + self.offset)))

    def uid(self, base: str) -> str:
        self._k += 1
        return "%s%d" % (base, self._k)

    def p(self, s: float) -> str:
        return K.pct(s / M.L * 100)

    def frame(self, eyebrow_text: str, mono_tail: str = "") -> None:
        t = self.t
        self.css.add("text{font-family:%s}.s{font-family:%s}" % (M.MONO, M.SANS))
        self.body.append(K.rect(.5, .5, CW_ - 1, CH_ - 1, rx=14, fill=t["bg"]))
        self.defs.append('<clipPath id="cl">%s</clipPath>' % K.rect(1, 1, CW_ - 2, CH_ - 2, rx=13))
        e = eyebrow(24, 32, eyebrow_text, t["dim"])
        if mono_tail:
            e = e.replace("</text>", '<tspan letter-spacing="0" fill="%s">%s</tspan></text>' % (t["ink"], K.esc(mono_tail)))
        self.eyebrow = e
        self.defs.append(K.linear_user("spec", 24, CW_ - 24, K.spectrum_pairs(t)))

    def finish(self, title: str, label: str) -> str:
        t = self.t
        y = CH_ - 12
        self.body.append(self.eyebrow)
        self.body.append(K.rect(24, y, CW_ - 48, 1, fill=t["rule"], opacity=".6"))
        self.body.append(K.rect(24, y - .5, CW_ - 48, 2, fill="url(#spec)", class_="pg"))

        self.css.add(".pg{transform-box:fill-box;transform-origin:0 50%;scale:1 1}")
        self.loop(".pg", "pg", "from{scale:0 1}to{scale:1 1}")
        # the border is drawn last, outside the clip, so content sliding past the card edge never covers it
        border = K.rect(.5, .5, CW_ - 1, CH_ - 1, rx=14, fill="none", stroke=t["rule"])
        return K.svg_doc(CW_, CH_, '<g clip-path="url(#cl)">%s</g>%s' % ("".join(self.body), border), self.css.text(), "".join(self.defs),
                         title=title, label=label)


STAGE_FLIPS = (3.4, 6.8)     # s: chart -> diagram, diagram -> title
STAGE_FLIP = .18             # s: fold-out half of a flip
STAGE_LEAD = .6              # s: the Stage card opens .6 s into its loop, pane docked, as its bars start


def card_stage(theme: str, anim: bool = True) -> str:
    c = Card("stage", theme, anim)
    c.offset -= STAGE_LEAD
    t, dark = c.t, c.dark
    c.frame("Stage %s the model presents" % M.DOT)
    ink, dim = t["ink"], t["dim"]
    panel = "#0D0B15" if dark else "#FFFFFF"
    # a neutral terminal (prompt glyph only)
    tx, ty, tw, th = 24, 50, 236, 266
    c.body.append(K.rect(tx + .5, ty + .5, tw - 1, th - 1, rx=8, fill=panel, stroke=t["rule"]))
    rng = random.Random(4)
    lines = []
    y = ty + 22
    for k in range(6):
        w = rng.choice((150, 180, 120, 196, 90, 160))
        lines.append(K.rect(tx + 16, y, w, 5, rx=2.5, fill=dim, opacity=".35"))
        y += 13
    c.body.append("".join(lines))
    c.body.append('<text x="%s" y="%s" font-size="11" fill="%s">&gt; <tspan fill="%s">show the routing, on Stage</tspan></text>' % (
        n(tx + 16), n(ty + 118), ink, ink))
    c.body.append(K.rect(tx + 10, ty + 106, tw - 20, 17, rx=3, fill=ink, opacity=".06"))
    y = ty + 142
    for k in range(5):
        w = rng.choice((170, 196, 140, 120, 186))
        c.body.append(K.rect(tx + 16, y, w, 5, rx=2.5, fill=dim, opacity=".35"))
        y += 13
    c.body.append('<text x="%s" y="%s" font-size="11" fill="%s">&gt;</text>' % (n(tx + 16), n(ty + th - 16), ink))
    c.body.append(K.rect(tx + 28, ty + th - 25, 6, 11, fill=ink, opacity=".8", class_="cur"))
    c.css.add(".cur{opacity:.8}")
    c.css.move(".cur{animation:cur %ss steps(2,start) infinite}" % n(M.CURSOR_BLINK[1]))
    c.css.frames("cur", "to{opacity:0}")
    # the pane
    px, py, pw, ph = 274, 50, 302, 266
    pane = [K.rect(px + .5, py + .5, pw - 1, ph - 1, rx=8, fill=panel, stroke=t["rule"])]
    pane.append(K.circle(px + 16, py + 17, 3.5, fill=M.BEAM if dark else "#0A8FB0"))
    pane.append('<text x="%s" y="%s" font-size="11" font-weight="700" fill="%s">STAGE</text>' % (n(px + 26), n(py + 21), M.BEAM if dark else "#0A8FB0"))
    pane.append('<text x="%s" y="%s" font-size="11" fill="%s">a tour</text>' % (n(px + 80), n(py + 21), ink))
    pane.append(K.rect(px + 1, py + 32, pw - 2, 1, fill=t["rule"], opacity=".6"))
    # page counter (3/5 chart, 4/5 diagram, 5/5 title)
    pages = "".join('<text x="%s" y="%s" font-size="11" fill="%s" text-anchor="end">%d/5</text>' % (
        n(px + pw - 14), n(py + 21 + 16 * k), dim, 3 + k) for k in range(3))
    c.defs.append('<clipPath id="pc">%s</clipPath>' % K.rect(px + pw - 50, py + 9, 40, 16))
    pane.append('<g clip-path="url(#pc)"><g class="pn">%s</g></g>' % pages)
    # slide 1: chart (values from the Stage pane screenshot: tokens per second by minute)
    vals = (64, 88, 112, 84, 126, 104, 132)
    cx0, base, bw, gap, hmax = px + 24, py + ph - 26, 30, 8, 150.0
    chart = ['<text x="%s" y="%s" font-size="12" fill="%s" class="s" font-weight="600">Throughput through Core</text>' % (
        n(cx0), n(py + 56), t["spectrum"][0])]  # type: ignore[index]
    chart.append('<text x="%s" y="%s" font-size="10" fill="%s">illustrative, tokens/s</text>' % (n(cx0), n(py + 72), dim))
    chart.append(K.rect(cx0, base + .5, len(vals) * (bw + gap) - gap, 1, fill=t["rule"]))
    bars, labels = [], []
    sp = M.timing("spring")
    # the stagger is baked into each bar's own keyframes (delay 0 inside the L loop), so every loop
    # opens on an empty plot while the pane slides in, then the bars grow one by one
    for k, v in enumerate(vals):
        h = v / 140.0 * (hmax - 40)
        x = cx0 + k * (bw + gap)
        col = M.spectrum_at(k / (len(vals) - 1), t)
        bars.append('<rect x="%s" y="%s" width="%d" height="%s" rx="2" fill="%s" class="br br%d"/>' % (
            n(x), n(base - h), bw, n(h), col, k))
        labels.append('<text x="%s" y="%s" font-size="10" fill="%s" text-anchor="middle" class="bv%d">%d</text>' % (
            n(x + bw / 2), n(base - h - 5), col, k, v))
        tb = .6 + k * .06
        c.loop(".br%d" % k, "br%d" % k, "0%%,%s{scale:1 0;%s}%s,100%%{scale:1 1}" % (c.p(tb), sp, c.p(tb + .62)))
        tl = tb + .32
        c.loop(".bv%d" % k, "bv%d" % k, "0%%,%s{opacity:0;animation-timing-function:steps(3,end)}%s,100%%{opacity:1}" % (
            c.p(tl), c.p(tl + .12)))
    chart.append("".join(bars) + "".join(labels))
    c.css.add(".br{transform-box:fill-box;transform-origin:50% 100%}")
    # slide 2: diagram (boxes and orthogonal arrows drawn with stroke-dashoffset)
    dg = []
    boxes = (("Claude Code", px + 24, py + 62), ("Pulsar Core", px + 24, py + 136), ("provider", px + 176, py + 136))
    for lab, bx, by in boxes:
        dg.append(K.rect(bx + .5, by + .5, 104, 34, rx=6, fill="none", stroke=ink, stroke_opacity=".5"))
        dg.append('<text x="%s" y="%s" font-size="11" fill="%s" text-anchor="middle">%s</text>' % (n(bx + 52), n(by + 21), ink, lab))
    arrows = ["M%s %sV%s" % (n(px + 76), n(py + 96), n(py + 134)),
              "M%s %sH%s" % (n(px + 128), n(py + 153), n(px + 174)),
              "M%s %sV%sH%sV%s" % (n(px + 228), n(py + 172), n(py + 206), n(px + 76), n(py + 172))]
    for k, d in enumerate(arrows):
        dg.append('<path d="%s" fill="none" stroke="%s" stroke-width="1.5" pathLength="100" stroke-dasharray="100" class="ar ar%d"/>'
                  % (d, M.spectrum_at(.3 + k * .3, t), k))
        ta = STAGE_FLIPS[0] + STAGE_FLIP + .25 + k * .25
        c.loop(".ar%d" % k, "ar%d" % k, "0%%,%s{stroke-dashoffset:100;animation-timing-function:%s}%s,100%%{stroke-dashoffset:0}" % (
            c.p(ta), M.E_SETTLE, c.p(ta + .6)))
    dg.append('<text x="%s" y="%s" font-size="10" fill="%s">stream back</text>' % (n(px + 132), n(py + 222), dim))
    c.css.add(".ar{stroke-dashoffset:0}")
    # slide 3: title
    tt = ['<text x="%s" y="%s" font-size="26" font-weight="700" fill="%s" class="s" text-anchor="middle">Pulsar Code</text>' % (
        n(px + pw / 2), n(py + 140), ink),
        K.rect(px + pw / 2 - 60, py + 154, 120, 2, fill="url(#spec)"),
        '<text x="%s" y="%s" font-size="12" fill="%s" text-anchor="middle">a tour, slide 5 of 5</text>' % (n(px + pw / 2), n(py + 178), dim)]
    pane.append('<g class="s1">%s</g><g class="s2">%s</g><g class="s3">%s</g>' % ("".join(chart), "".join(dg), "".join(tt)))
    c.body.append('<g class="pane">%s</g>' % "".join(pane))
    # poster: chart built
    c.css.add(".s2,.s3{opacity:0;scale:0 1}.pn{translate:0 0}")
    # a real flip about the pane's vertical centre line: the old slide folds edge-on (scaleX 1 -> 0, FLIP
    # s, ease-in like a card turning away), the page counter changes at the swap, and the new slide
    # unfolds 0 -> 1 with E_SPRING (one overshoot)
    c.css.add(".s1,.s2,.s3{transform-origin:%spx %spx}" % (n(px + pw / 2), n(py + ph / 2 + 16)))
    # the pane leaves and re-docks within 250 ms around the loop seam
    c.loop(".pane", "pane", "0%%{translate:330px 0;%s}%s{translate:0 0}%s{translate:0 0;animation-timing-function:%s}%s,100%%{translate:330px 0}" % (
        sp, c.p(.62), c.p(M.L - .22), M.E_COMMIT, c.p(M.L - .02)))
    fold = M.E_CHARGE
    f1, f2 = STAGE_FLIPS
    w1, w2 = f1 + STAGE_FLIP, f2 + STAGE_FLIP          # swap instants (slide edge-on)
    c.loop(".s1", "s1", "0%%,%s{opacity:1;scale:1 1;animation-timing-function:%s}%s{opacity:1;scale:0 1;animation-timing-function:steps(1,end)}"
           "%s,100%%{opacity:0;scale:0 1}" % (c.p(f1), fold, c.p(w1), c.p(w1 + .001)))
    c.loop(".s2", "s2", "0%%,%s{opacity:0;scale:0 1;animation-timing-function:steps(1,end)}%s{opacity:1;scale:0 1;%s}%s,%s{opacity:1;scale:1 1;"
           "animation-timing-function:%s}%s{opacity:1;scale:0 1;animation-timing-function:steps(1,end)}%s,100%%{opacity:0;scale:0 1}" % (
               c.p(w1 - .001), c.p(w1), sp, c.p(w1 + .62), c.p(f2), fold, c.p(w2), c.p(w2 + .001)))
    c.loop(".s3", "s3", "0%%,%s{opacity:0;scale:0 1;animation-timing-function:steps(1,end)}%s{opacity:1;scale:0 1;%s}%s,100%%{opacity:1;scale:1 1}" % (
        c.p(w2 - .001), c.p(w2), sp, c.p(w2 + .62)))
    c.loop(".pn", "pn", "0%%,%s{translate:0 0;animation-timing-function:steps(1,end)}%s{translate:0 -16px;animation-timing-function:steps(1,end)}%s,100%%{translate:0 -32px}" % (
        c.p(w1), c.p(w1), c.p(w2)))
    return c.finish("Pulsar Stage", "Stage: the model presents. A Stage pane slides in beside a terminal, builds a bar chart "
                                    "bar by bar, flips to a diagram with arrows, then a title slide")


def card_council(theme: str, anim: bool = True) -> str:
    c = Card("council", theme, anim)
    t, dark = c.t, c.dark
    c.frame("Council %s illustrative" % M.DOT)
    ink, dim = t["ink"], t["dim"]
    L = M.L
    tempos = (.052, .071, .090)
    counts = (54, 45, 39)         # answers of different lengths fill about 70 % of their boxes
    t0 = .5
    colw, gap = 172, 18
    BOX_H = 176
    rng = random.Random(21)
    tok_col = "#C9B8FF" if dark else "#6B3FD1"   # one neutral tint for all three columns: no winner
    finish = []
    for k in range(3):
        x0 = 24 + k * (colw + gap)
        c.body.append(K.rect(x0 + .5, 50.5, colw - 1, BOX_H, rx=8, fill="none", stroke=t["rule"]))
        c.body.append('<text x="%s" y="%s" font-size="12" fill="%s">model %s</text>' % (n(x0 + 14), 72, ink, "ABC"[k]))
        c.body.append(K.rect(x0 + 14, 80, colw - 28, 1, fill=t["rule"], opacity=".6"))
        # tokens flow into rows; each row is revealed token by token by a clip whose width steps
        rows: List[List[Tuple[float, float, float]]] = [[]]
        x, row_w = 0.0, colw - 28
        for j in range(counts[k]):
            w = rng.choice((8, 10, 12, 14, 18, 22, 26))
            if x + w > row_w:
                rows.append([])
                x = 0.0
            rows[-1].append((x, w, t0 + j * tempos[k]))
            x += w + 4
        finish.append(t0 + counts[k] * tempos[k])
        for r, row in enumerate(rows):
            y = 92 + r * 14
            d = "".join("M%s %sh%sv6h%sz" % (n(x0 + 14 + a), n(y), n(w), n(-w)) for a, w, _ in row)
            cid = "r%d%d" % (k, r)
            c.defs.append('<clipPath id="%s"><rect class="%s" x="%s" y="%s" width="%s" height="10"/></clipPath>' % (
                cid, cid, n(x0 + 13), n(y - 2), n(row_w + 2)))
            c.body.append('<path d="%s" fill="%s" fill-opacity="%s" clip-path="url(#%s)"/>' % (
                d, tok_col, ".75" if dark else ".55", cid))
            fr = ["0%%{width:0;animation-timing-function:steps(1,end)}"]
            for a, w, ts in row:
                fr.append("%s{width:%spx;animation-timing-function:steps(1,end)}" % (c.p(ts), n(a + w + 1)))
            fr.append("%s{width:%spx}%s{width:%spx;animation-timing-function:%s}100%%{width:%spx}" % (
                c.p(row[-1][2] + .01), n(row_w + 2), c.p(9.6), n(row_w + 2), M.E_SETTLE, n(row_w + 2)))
            if anim:
                c.loop(".%s" % cid, cid, "".join(fr).replace("%%", "%"))
        # done tick (same for every column: no ranking)
        c.body.append('<text x="%s" y="%s" font-size="11" fill="%s" text-anchor="end" class="dn%d">done</text>' % (
            n(x0 + colw - 14), 72, dim, k))
        c.loop(".dn%d" % k, "dn%d" % k, window(finish[k], 9.6, L, "opacity:1", "opacity:0", .12, .45, "steps(3,end)", M.E_SETTLE))
    c.css.add(".tokfade{opacity:1}")
    # synthesis row across all columns once all three are done
    ts = max(finish) + .2
    sx, sy, sw = 24, 50 + BOX_H + 12, CW_ - 48
    syn = [K.rect(sx + .5, sy + .5, sw - 1, 52, rx=8, fill="none", stroke="url(#spec)", stroke_width=1.2)]
    syn.append('<text x="%s" y="%s" font-size="12" fill="%s"><tspan fill="%s">synthesis:</tspan> where they agree, where they differ</text>' % (
        n(sx + 16), n(sy + 32), ink, dim))
    c.defs.append('<clipPath id="sy"><rect class="sy" x="%s" y="%s" width="%s" height="56"/></clipPath>' % (n(sx - 1), n(sy - 2), n(sw + 2)))
    c.body.append('<g clip-path="url(#sy)">%s</g>' % "".join(syn))
    c.loop(".sy", "sy", "0%%,%s{width:0;animation-timing-function:%s}%s,%s{width:%spx;animation-timing-function:%s}100%%{width:0}" % (
        c.p(ts), M.E_SETTLE, c.p(ts + .8), c.p(9.6), n(sw + 2), M.E_SETTLE))
    return c.finish("Council", "Council (illustrative): three models labelled A, B and C answer side by side at different "
                               "speeds; when all three finish, a synthesis row notes where they agree and where they differ")


def card_pilot(theme: str, anim: bool = True) -> str:
    c = Card("pilot", theme, anim)
    t, dark = c.t, c.dark
    c.frame("Pilot %s opt-in %s off by default" % (M.DOT, M.DOT))
    ink, dim = t["ink"], t["dim"]
    ok_col = t["ok"]
    k = PILOT_SCALE
    L = M.L
    panel = "#0D0B15" if dark else "#FFFFFF"
    # the mock window
    wx, wy, ww, wh = 24, 48, 552, 248
    c.body.append(K.rect(wx + .5, wy + .5, ww - 1, wh - 1, rx=8, fill=panel, stroke=t["rule"]))
    c.body.append(K.rect(wx + 1, wy + 24, ww - 2, 1, fill=t["rule"], opacity=".6"))
    c.body.append('<text x="%s" y="%s" font-size="11" fill="%s">release %s staging</text>' % (n(wx + 14), n(wy + 16), dim, M.DOT))
    c.body.append('<text x="%s" y="%s" font-size="11" fill="%s">Release notes</text>' % (n(wx + 24), n(wy + 54), dim))
    field = (wx + 24, wy + 64, 330, 30)
    c.body.append(K.rect(field[0] + .5, field[1] + .5, field[2] - 1, field[3] - 1, rx=4, fill="none", stroke=t["rule"]))
    c.body.append('<g class="cm">%s</g>' % K.rect(field[0] + 10, field[1] + 9, 2, 12, fill=M.FLARE if dark else "#D6156A", class_="caret"))
    c.body.append('<text x="%s" y="%s" font-size="11" fill="%s">Environment</text>' % (n(wx + 24), n(wy + 122), dim))
    c.body.append(K.rect(wx + 24, wy + 130, 90, 26, rx=4, fill=ink, opacity=".1"))
    c.body.append('<text x="%s" y="%s" font-size="11" fill="%s" text-anchor="middle">staging</text>' % (n(wx + 69), n(wy + 147), ink))
    c.body.append(K.rect(wx + 120, wy + 130, 90, 26, rx=4, fill=ink, opacity=".04"))
    c.body.append('<text x="%s" y="%s" font-size="11" fill="%s" text-anchor="middle">production</text>' % (n(wx + 165), n(wy + 147), dim))
    btn_c = (wx + 300, wy + 196, 100, 30)
    btn_d = (wx + 398, wy + 196, 130, 30)
    c.body.append(K.rect(btn_c[0], btn_c[1], btn_c[2], btn_c[3], rx=4, fill=ink, opacity=".05"))
    c.body.append('<text x="%s" y="%s" font-size="11" fill="%s" text-anchor="middle">Cancel</text>' % (n(btn_c[0] + 50), n(btn_c[1] + 19), dim))
    # light: solid PLASMA-ink with white text (about 6.5:1); the label sits in the left part of the button
    # and Pilot aims at its right third, so "Deploy" stays readable under the cursor and the ripple
    c.body.append(K.rect(btn_d[0], btn_d[1], btn_d[2], btn_d[3], rx=4, fill=M.PLASMA if dark else "#6B3FD1",
                         opacity=".55" if dark else None))
    c.body.append('<text x="%s" y="%s" font-size="11" fill="%s" text-anchor="middle">Deploy</text>' % (
        n(btn_d[0] + 38), n(btn_d[1] + 19), M.ICE if dark else "#FFFFFF"))
    c.body.append('<text x="%s" y="%s" font-size="11" fill="%s" class="s">pilot tour: nothing is clicked %s top-left corner failsafe</text>' % (
        n(24), n(CH_ - 22), dim, M.DOT))
    # path of the cursor: home -> Deploy -> field -> home
    home = (wx + 120, wy + 92 + 90)
    deploy = (btn_d[0] + 100, btn_d[1] + 15)
    fld = (field[0] + 40, field[1] + 15)
    g1 = M.glide_duration(home[0] / k, home[1] / k, deploy[0] / k, deploy[1] / k)
    g2 = M.glide_duration(deploy[0] / k, deploy[1] / k, fld[0] / k, fld[1] / k)
    g3 = M.glide_duration(fld[0] / k, fld[1] / k, home[0] / k, home[1] / k)
    t_g1, t_g2, t_g3 = .6, 3.0, 7.6
    t_aim = t_g1 + g1
    t_click = t_aim + .16 + .1
    # cursor position keyframes (E_GLIDE per glide)
    def at(pt: Tuple[float, float]) -> str:
        return "translate:%spx %spx" % (n(pt[0]), n(pt[1]))
    pos = ("0%%,%s{%s;animation-timing-function:%s}%s,%s{%s;animation-timing-function:%s}%s,%s{%s;animation-timing-function:%s}%s,100%%{%s}"
           % (c.p(t_g1), at(home), M.E_GLIDE, c.p(t_g1 + g1), c.p(t_g2), at(deploy), M.E_GLIDE, c.p(t_g2 + g2), c.p(t_g3), at(fld),
              M.E_GLIDE, c.p(t_g3 + g3), at(home)))
    # cursor (overlay geometry at scale k): dark lens, segmented spectrum ring, inner ring, core, two jets
    r = 15.5 * k
    seg = []
    for s_ in range(24):
        a0 = math.radians(s_ * 15)
        a1 = math.radians(s_ * 15 + 15 - 4.5)
        col = M.spectrum_at(abs(((s_ / 12.0) % 2) - 1), t)
        seg.append('<path d="M%s %sA%s %s 0 0 1 %s %s" stroke="%s"/>' % (
            n(r * math.cos(a0)), n(r * math.sin(a0)), n(r), n(r), n(r * math.cos(a1)), n(r * math.sin(a1)), col))
    jets = []
    for j, (c1, c2) in enumerate(((M.FLARE, M.EMBER), (M.PLASMA, M.BEAM))):
        base_, tip = 7.0 * k, 38.0 * k
        half = 4.4 * k
        jets.append('<g transform="rotate(%s)"><path d="M%s %sL%s 0L%s %sZ" fill="%s"/><path d="M%s %sL%s 0L%s %sZ" fill="%s"/></g>' % (
            -35 + j * 180, n(base_), n(-half), n(tip), n(base_), n(half), c1, n(base_), n(-half * .5), n(tip * .8), n(base_), n(half * .5), c2))
    cur = ('<g class="cu">%s%s<g fill="none" stroke-width="%s">%s</g>%s%s%s</g>' % (
        "".join(jets), K.circle(0, 0, r, fill="#07060B", stroke="#07060B", stroke_width=3.6 * k + 3), n(3.6 * k), "".join(seg),
        K.circle(0, 0, r - 4.6 * k, fill="none", stroke=M.BEAM, stroke_width=1.2), K.circle(0, 0, 5 * k, fill=M.ICE),
        K.circle(0, 0, 1.5 * k, fill="#07060B")))
    # comet trail, as the overlay draws it (overlay._trail): a tapered ribbon over the last TRAIL = 100 ms of
    # the path, half-width (0.3 + 8.5 f^1.8) k from tail (f = 0) to head (f = 1), spectrum from ICE at the
    # head to EMBER at the tail, with an ink outline. Each glide is straight, so the ribbon is one shape
    # rotated to the glide direction and stretched to the distance covered in the last 100 ms.
    TRAIL = .1
    prof = [(i / 20.0, (0.3 + 8.5 * (1 - i / 20.0) ** 1.8) * k) for i in range(21)]  # (x along -dir, half-width)
    rib = "M" + "L".join("%s %s" % (n(-x, 3), n(-w)) for x, w in prof) + "L" + "L".join(
        "%s %s" % (n(-x, 3), n(w)) for x, w in reversed(prof)) + "Z"
    ink_rib = "M" + "L".join("%s %s" % (n(-x, 3), n(-(w * 1.3 + .8))) for x, w in prof) + "L" + "L".join(
        "%s %s" % (n(-x, 3), n(w * 1.3 + .8)) for x, w in reversed(prof)) + "Z"
    c.defs.append('<linearGradient id="cm" x1="1" x2="0">%s</linearGradient>' % K.stops(
        [(0, M.ICE, 1), (.25, M.BEAM, 1), (.5, M.PLASMA, 1), (.75, M.FLARE, 1), (1, M.EMBER, .8)]))
    trail = ('<g class="tlr"><g class="tls"><path d="%s" fill="#0A0911"/><path d="%s" fill="url(#cm)"/></g></g>'
             % (ink_rib, rib))
    glides = ((t_g1, home, deploy, g1), (t_g2, deploy, fld, g2), (t_g3, fld, home, g3))

    def cpos(tt: float) -> Tuple[float, float]:
        cur_ = home
        for g0, a, b, du in glides:
            if tt >= g0:
                e = M.ease_io(min(1.0, (tt - g0) / du))
                cur_ = (a[0] + (b[0] - a[0]) * e, a[1] + (b[1] - a[1]) * e)
        return cur_
    rot, sc = [], ["0%{scale:0 1}"]
    for gi, (g0, a, b, du) in enumerate(glides):
        ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
        rot.append("%s{rotate:%sdeg;animation-timing-function:steps(1,end)}" % ("0%" if gi == 0 else c.p(g0), n(ang, 1)))
        sc.append("%s{scale:0 1}" % c.p(g0))
        tt = g0 + .016
        while tt < g0 + du + TRAIL:
            p_, q_ = cpos(tt), cpos(max(g0, tt - TRAIL))
            sc.append("%s{scale:%s 1}" % (c.p(tt), n(math.hypot(p_[0] - q_[0], p_[1] - q_[1]), 1)))
            tt += .016
        sc.append("%s{scale:0 1}" % c.p(g0 + du + TRAIL))
    sc.append("100%{scale:0 1}")
    c.css.add(".tls{scale:0 1}")
    c.loop(".tlr", "tlr", "".join(rot))
    c.loop(".tls", "tls", "".join(sc))
    # aim corners (viewfinder) at Deploy, snapping in with E_POP
    ax, ay = deploy
    ab = 26
    corners = "".join('<path d="M%s %sv%sh%s" transform="rotate(%d %s %s)"/>' % (
        n(ax - ab), n(ay - ab + 7), n(-7), n(7), q * 90, n(ax), n(ay)) for q in range(4))
    c.body.append('<g class="aim" fill="none" stroke="%s" stroke-width="2">%s</g>' % (M.BEAM if dark else "#0A8FB0", corners))
    c.css.add(".aim{transform-origin:%spx %spx;opacity:0}" % (n(ax), n(ay)))
    c.loop(".aim", "aim", "0%%,%s{opacity:0;scale:1.6;animation-timing-function:%s}%s{opacity:1;scale:1}%s{opacity:1;scale:1}%s,100%%{opacity:0;scale:1}" % (
        c.p(t_aim), M.E_POP, c.p(t_aim + .16), c.p(t_click + .3), c.p(t_click + .5)))
    # click ripple: outer r (10 + 58 ease_out(p)) k over .62 s, stroke 7.5 (1 - p) k; inner ring +70 ms; centre flash
    rmax = (10 + 58) * k
    rip = ('<g class="rp"><circle class="rpc" r="%s" fill="none" stroke="url(#rg)" vector-effect="non-scaling-stroke"/></g>'
           '<g class="ri"><circle class="ric" r="%s" fill="none" stroke="%s" vector-effect="non-scaling-stroke"/></g>'
           '<circle class="rf" r="%s" fill="%s"/>' % (n(rmax), n(41 * k), M.BEAM if dark else "#0A8FB0", n(12 * k), M.ICE if dark else "#1B1530"))
    c.defs.append(K.linear_user("rg", -rmax, rmax, K.spectrum_pairs(t)))
    c.body.append('<g transform="translate(%s %s)">%s</g>' % (n(ax), n(ay), rip))
    p0 = t_click
    c.css.add(".rp,.ri,.rf{opacity:0}")
    c.loop(".rp", "rp", "0%%,%s{opacity:0;scale:%s;animation-timing-function:%s}%s{opacity:1;scale:%s;animation-timing-function:%s}%s{opacity:1;scale:1}%s,100%%{opacity:0;scale:1}" % (
        c.p(p0 - .001), n(10 / 68.0, 4), "steps(1,end)", c.p(p0), n(10 / 68.0, 4), M.E_SHOCK, c.p(p0 + .62), c.p(p0 + .621)))
    c.loop(".rpc", "rpc", "0%%,%s{stroke-width:%spx}%s{stroke-width:0}100%%{stroke-width:0}" % (c.p(p0), n(7.5 * k), c.p(p0 + .62)))
    c.loop(".ri", "ri", "0%%,%s{opacity:0;scale:%s;animation-timing-function:%s}%s{opacity:1;scale:%s;animation-timing-function:%s}%s{opacity:.9;scale:1}%s,100%%{opacity:0;scale:1}" % (
        c.p(p0 + .069), n(5 / 41.0, 4), "steps(1,end)", c.p(p0 + .07), n(5 / 41.0, 4), M.E_SHOCK, c.p(p0 + .69), c.p(p0 + .691)))
    c.loop(".rf", "rf", "0%%,%s{opacity:0;scale:1;animation-timing-function:steps(1,end)}%s{opacity:1;scale:1}%s{opacity:1;scale:0}%s,100%%{opacity:0;scale:0}" % (
        c.p(p0 - .001), c.p(p0), c.p(p0 + .62 * .22), c.p(p0 + .62 * .22 + .001)))
    c.css.add(".rp,.ri{transform-origin:0 0}.rf{transform-box:fill-box;transform-origin:50% 50%}")
    # keycaps ctrl + s at 5.0 (E_POP 160 ms, 50 ms stagger); the overlay shows keycaps, no chip
    t_keys = 5.0
    caps = []
    kx = fld[0] + 140
    for q, (lab, w) in enumerate((("ctrl", 40), ("s", 24))):
        x = kx + q * 48
        col = M.EMBER if q == 0 else M.BEAM
        caps.append('<g class="kc%d" style="transform-origin:%spx %spx">%s<text x="%s" y="%s" font-size="11" font-weight="700" fill="%s" '
                    'text-anchor="middle">%s</text></g>' % (q, n(x + w / 2), n(fld[1] - 30), K.rect(x, fld[1] - 42, w, 22, rx=5, fill=panel,
                                                                                                         stroke=col, stroke_width=1.5),
                                                          n(x + w / 2), n(fld[1] - 27), ink, lab))
        c.css.add(".kc%d{opacity:0}" % q)
        c.loop(".kc%d" % q, "kc%d" % q, "0%%,%s{opacity:0;scale:.6;animation-timing-function:%s}%s{opacity:1;scale:1}%s{opacity:1;scale:1;animation-timing-function:%s}%s,100%%{opacity:0;scale:1}" % (
            c.p(t_keys + q * .05), M.E_POP, c.p(t_keys + q * .05 + .16), c.p(t_keys + 1.3), M.E_SETTLE, c.p(t_keys + 1.5)))
    c.body.append("".join(caps))
    # the overlay's key op: keycaps plus a pulse ring at the cursor (r (12 + 22 ease_out p) k, stroke
    # 3.5 (1 - p) k, BEAM -> PLASMA, 0.4 s) and no chip (overlay.py clears the chip on a key op). The app
    # answers ctrl+s: its title bar reads "saved".
    kr0, kr1 = 12 * k, 34 * k
    c.body.append('<g transform="translate(%s %s)"><g class="kp"><circle class="kpc" r="%s" fill="none" stroke="%s" '
                  'vector-effect="non-scaling-stroke"/></g></g>' % (n(fld[0]), n(fld[1]), n(kr1), M.BEAM if dark else "#0A8FB0"))
    c.css.add(".kp{opacity:0;transform-origin:0 0}")
    c.loop(".kp", "kp", "0%%,%s{opacity:0;scale:%s;animation-timing-function:steps(1,end)}%s{opacity:1;scale:%s;animation-timing-function:%s}"
           "%s{opacity:1;scale:1}%s,100%%{opacity:0;scale:1}" % (c.p(t_keys - .001), n(kr0 / kr1, 3), c.p(t_keys), n(kr0 / kr1, 3),
                                                                 M.E_SHOCK, c.p(t_keys + .4), c.p(t_keys + .401)))
    c.loop(".kpc", "kpc", "0%%,%s{stroke-width:%spx;stroke:%s}%s,100%%{stroke-width:0;stroke:%s}" % (
        c.p(t_keys), n(3.5 * k), M.BEAM if dark else "#0A8FB0", c.p(t_keys + .4), M.PLASMA if dark else "#7A2FF0"))
    sv_x = wx + ww - 14
    c.body.append('<g class="sv">%s<text x="%s" y="%s" font-size="11" fill="%s" text-anchor="end">saved</text></g>' % (
        K.circle(sv_x - 44, wy + 12.5, 3, fill=ok_col), n(sv_x), n(wy + 16), dim))
    c.css.add(".sv{opacity:0}")
    c.loop(".sv", "sv", "0%%,%s{opacity:0;animation-timing-function:%s}%s,%s{opacity:1;animation-timing-function:%s}100%%{opacity:0}" % (
               c.p(t_keys + .25), M.E_SETTLE, c.p(t_keys + .45), c.p(M.L - .45), M.E_SETTLE))
    # scan sweep 6.4 s, 700 ms linear, top to bottom of the window
    c.body.append(K.rect(wx + 1, wy + 25, ww - 2, 2, fill=M.BEAM if dark else "#0A8FB0", opacity=".7", class_="sc"))
    c.css.add(".sc{opacity:0}")
    c.loop(".sc", "sc", "0%%,%s{opacity:0;translate:0 0}%s{opacity:.8;translate:0 0}%s{opacity:.8;translate:0 %spx}%s,100%%{opacity:0;translate:0 %spx}" % (
        c.p(6.4), c.p(6.41), c.p(7.1), n(wh - 28), c.p(7.12), n(wh - 28)))
    # chips (verb + label, as the overlay formats them): CLICK Deploy, TYPE 24 chars
    def chip(cls: str, verb: str, label: str, tone: str, x: float, y: float) -> str:
        vw = len(verb) * 11 * .62
        w = 22 + vw + 8 + len(label) * 11 * .6 + 12
        return ('<g class="%s">%s%s<text x="%s" y="%s" font-size="11" font-weight="700" fill="%s">%s</text>'
                '<text x="%s" y="%s" font-size="11" fill="%s">%s</text></g>' % (
                    cls, K.rect(x, y, w, 24, rx=6, fill=t["chip"], stroke=tone, stroke_opacity=".7"),
                    K.circle(x + 12, y + 12, 4, fill="none", stroke=tone, stroke_width=1.5), n(x + 22), n(y + 16), tone, verb.upper(),
                    n(x + 22 + vw + 8), n(y + 16), ink, K.esc(label[:M.CHIP_LABEL_MAX])))
    chip_click = chip("ch1", "click", "Deploy", M.BEAM if dark else "#0A8FB0", deploy[0] - 150, deploy[1] + 20 - 70)
    chip_type = chip("ch2", "type", "24 chars", M.FLARE if dark else "#D6156A", fld[0] + 30, fld[1] + 22)
    c.body.append(chip_click + chip_type)
    soft = M.timing("soft")
    c.css.add(".ch1{opacity:1}.ch2{opacity:0}")
    c.loop(".ch1", "ch1", "0%%,%s{opacity:0;translate:0 6px;%s}%s{opacity:1;translate:0 0}%s{opacity:1;translate:0 0;animation-timing-function:%s}%s,100%%{opacity:0;translate:0 0}" % (
        c.p(t_click), soft, c.p(t_click + .18), c.p(t_click + 1.6), M.E_SETTLE, c.p(t_click + 1.8)))
    t_type = t_g2 + g2
    # the overlay's type op: caret effect for chars*0.012 + 0.4 s, chip ttl = that + 0.8 s (overlay.py)
    n_chars = 24
    t_dur = max(.6, min(6.0, n_chars * .012 + .4))
    c.loop(".ch2", "ch2", "0%%,%s{opacity:0;translate:0 6px;%s}%s{opacity:1;translate:0 0}%s{opacity:1;translate:0 0;animation-timing-function:%s}%s,100%%{opacity:0;translate:0 0}" % (
        c.p(t_type), soft, c.p(t_type + .18), c.p(t_type + t_dur + .6), M.E_SETTLE, c.p(t_type + t_dur + .8)))
    # the app's field receives the characters: one bullet per keystroke (the text itself is never shown),
    # the caret riding the end of the run; the bullets stay until the loop's closing fade
    bw_ = 11 * M.MONO_ADVANCE
    c.body.append('<g class="fb"><g clip-path="url(#fbc)"><text x="%s" y="%s" font-size="11" fill="%s">%s</text></g></g>' % (
        n(field[0] + 10), n(field[1] + 19), dim, M.BULLET * n_chars))
    c.defs.append('<clipPath id="fbc"><rect class="fbr" x="%s" y="%s" width="%s" height="20"/></clipPath>' % (
        n(field[0] + 9), n(field[1] + 5), n(n_chars * bw_ + 2)))
    c.css.add(".fbr{width:%spx}.fb{opacity:0}" % n(n_chars * bw_ + 2))
    c.loop(".fbr", "fbr", "0%%,%s{width:0;animation-timing-function:steps(%d,end)}%s,100%%{width:%spx}" % (
        c.p(t_type), n_chars, c.p(t_type + t_dur), n(n_chars * bw_ + 2)))
    c.loop(".fb", "fb", "0%%,%s{opacity:1;animation-timing-function:%s}100%%{opacity:0}" % (c.p(M.L - .45), M.E_SETTLE))
    c.css.add(".caret{opacity:0}")
    c.loop(".caret", "caret", window(t_type, t_type + t_dur + .8, L, "opacity:1", "opacity:0", 0, .1))
    c.loop(".cm", "cm", "0%%,%s{translate:0 0;animation-timing-function:steps(%d,end)}%s,100%%{translate:%spx 0}" % (
        c.p(t_type), n_chars, c.p(t_type + t_dur), n(n_chars * bw_ + 1)))
    # the cursor on top: pops in at 0 (E_POP .6 -> 1, 280 ms)
    c.body.append('<g class="cp">%s<g class="cs">%s</g></g>' % (trail, cur))
    c.css.add(".cp{translate:%spx %spx}" % (n(deploy[0]), n(deploy[1])))
    c.loop(".cp", "cp", pos)
    c.loop(".cs", "cs", "0%%{scale:.6;opacity:0;animation-timing-function:%s}%s,100%%{scale:1;opacity:1}" % (M.E_POP, c.p(.28)))
    # poster: ripple and chip at the click
    c.css.poster(".rp{opacity:1;scale:.875}.rpc{stroke-width:%spx}.ch1{opacity:1}.aim{opacity:1}" % n(7.5 * k * .5))
    return c.finish("Pulsar Pilot", "Pilot (opt-in, off by default): a pulsar cursor glides to a Deploy button, aims, and the "
                                    "click ripples with a CLICK Deploy chip; it then types 24 characters, shows the ctrl and s "
                                    "keycaps and scans the window. Nothing is clicked in this tour")


def card_observatory(theme: str, anim: bool = True) -> str:
    c = Card("observatory", theme, anim)
    t, dark = c.t, c.dark
    c.frame("Observatory %s " % M.DOT, "pulsarcode top")
    ink, dim = t["ink"], t["dim"]
    warn, bad = t["warn"], t["bad"]
    # permanent DEMO DATA stamp (every frame and the poster)
    c.body.append('<g>%s<text x="%s" y="%s" font-size="11" font-weight="700" letter-spacing="2" fill="%s" text-anchor="middle">DEMO DATA</text></g>' % (
        K.rect(CW_ - 24 - 96, 17, 96, 22, rx=4, fill="none", stroke=warn, stroke_width=1.2), n(CW_ - 24 - 48), 32, warn))
    # tokens/s sparkline (synthetic): a periodic series on telemetry paper that scrolls left continuously,
    # new data arriving at a pen dot on the right edge; the chart is never blank. One period of data spans
    # the chart width and scrolls by in 4*P1 (a harmonic of the CLI clock).
    sx0, sx1, sy0, sy1 = 24, CW_ - 24, 58, 150
    rng = random.Random(3)
    N = 60
    raw = []
    v = .5
    for k in range(N + 1):
        v = max(.12, min(.95, v + rng.uniform(-.13, .13) + .02 * math.sin(k / 5.0)))
        raw.append(v)
    vals = [raw[k] - (raw[N] - raw[0]) * k / N for k in range(N)]          # periodic: v[N] == v[0]
    dx = (sx1 - sx0) / float(N)
    ys = [sy1 - (sy1 - sy0) * vals[k % N] for k in range(2 * N + 1)]
    pts = [(sx0 + dx * k, ys[k]) for k in range(2 * N + 1)]
    d = "M" + "L".join("%s %s" % (n(x), n(y)) for x, y in pts)
    area = d + "L%s %sL%s %sZ" % (n(pts[-1][0]), n(sy1), n(sx0), n(sy1))
    c.defs.append(K.linear_user("ar", 0, 0, [(0, M.BEAM if dark else "#0A8FB0", .28), (1, M.BEAM if dark else "#0A8FB0", 0)], y1=sy0, y2=sy1))
    # stroke colour is periodic with the data (a palindrome of the spectrum per period), so it scrolls seamlessly
    sp_ = list(t["spectrum"])  # type: ignore[arg-type]
    pal = sp_ + sp_[-2::-1]
    c.defs.append(K.linear_user("sk", sx0, sx1 + (sx1 - sx0), [(.5 * i / (len(pal) - 1) + h, col_, 1)
                                                                 for h in (0, .5) for i, col_ in enumerate(pal)]))
    c.defs.append('<clipPath id="sc">%s</clipPath>' % K.rect(sx0, sy0 - 6, sx1 - sx0, sy1 - sy0 + 8))
    c.body.append('<text x="%s" y="%s" font-size="11" fill="%s">tokens/s</text>' % (n(sx0), n(sy0 - 4), dim))
    c.body.append(K.rect(sx0, sy1 + .5, sx1 - sx0, 1, fill=t["rule"]))
    c.body.append('<g clip-path="url(#sc)"><g class="sp"><path d="%s" fill="url(#ar)"/>'
                  '<path d="%s" fill="none" stroke="url(#sk)" stroke-width="1.8" stroke-linejoin="round"/></g></g>' % (area, d))
    c.body.append('<g class="nw">%s</g>' % K.circle(sx1, 0, 3, fill=M.ICE if dark else "#1B1530"))
    span = 4 * M.P1
    c.css.add(".nw{translate:0 %spx}" % n(ys[0]))
    c.css.move(".sp{animation:sp %s linear %s infinite}.nw{animation:nw %s linear %s infinite}" % (
        K.ms(span), K.ms(c.offset), K.ms(span), K.ms(c.offset)))
    c.css.frames("sp", "from{translate:0 0}to{translate:-%spx 0}" % n(sx1 - sx0))
    c.css.frames("nw", "".join("%s{translate:0 %spx}" % (K.pct(100.0 * k / N), n(ys[k])) for k in range(N + 1)))
    # PROVIDERS panel, as pulsarcode top shows it: breakers are per target (profile|model) and an open
    # breaker means that target is skipped, so its throughput drops to 0 while sessions relay elsewhere.
    # Provider names are ink with a route-coloured dot (red-family text is reserved for status).
    rows = tuple((pid, M.wire_label(M.provider(pid)).replace(" wire", ""), tps)
                 for pid, tps in (("openrouter", "96"), ("groq", "131"), ("ollama", "38")))
    hy = 182
    heads = ("PROVIDER", "WIRE", "TOK/S", "BREAKER")
    xs = (24, 190, 290, 420)
    for x, h in zip(xs, heads):
        c.body.append('<text x="%d" y="%d" font-size="10" letter-spacing="1.5" fill="%s">%s</text>' % (x, hy, dim, h))
    c.body.append(K.rect(24, hy + 8, CW_ - 48, 1, fill=t["rule"], opacity=".6"))
    T_OPEN, T_CLOSE = 4.0, 7.0
    for r, (prov, wire, tps) in enumerate(rows):
        y = hy + 32 + r * 28
        g = M.provider_group(M.provider(prov))
        col = t["route"][g]  # type: ignore[index]
        c.body.append(K.circle(xs[0] + 3, y - 4, 3, fill=col))
        c.body.append('<text x="%d" y="%d" font-size="12" fill="%s">%s</text>' % (xs[0] + 12, y, ink, prov))
        c.body.append('<text x="%d" y="%d" font-size="12" fill="%s">%s</text>' % (xs[1], y, dim, wire))
        if r == 1:
            c.body.append('<text x="%d" y="%d" font-size="12" fill="%s" class="bc">%s</text>'
                          '<text x="%d" y="%d" font-size="12" fill="%s" class="bo">0</text>' % (xs[2], y, ink, tps, xs[2], y, dim))
            c.body.append('<g class="bk"><g class="bc"><text x="%d" y="%d" font-size="12" fill="%s">closed</text></g>'
                          '<g class="bo">%s<text x="%d" y="%d" font-size="12" font-weight="700" fill="%s">OPEN</text></g></g>' % (
                              xs[3], y, dim,
                              K.rect(xs[3] - 6, y - 14, 70, 20, rx=4, fill=bad, opacity=".18"), xs[3], y, bad))
        else:
            c.body.append('<text x="%d" y="%d" font-size="12" fill="%s">%s</text>' % (xs[2], y, ink, tps))
            c.body.append('<text x="%d" y="%d" font-size="12" fill="%s">closed</text>' % (xs[3], y, dim))
    # the feed line while the breaker is open: the target that tripped, and the relay it caused
    target = "groq|%s" % M.default_model(M.provider("groq"))
    c.body.append('<text x="24" y="%d" font-size="11" fill="%s" class="bo">%s %s breaker open %s session a1f3 relays '
                  'to openrouter</text>' % (hy + 32 + len(rows) * 28 + 4, dim, K.esc(target), M.DOT, M.ARROW))
    c.css.add(".bc{opacity:0}.bo{opacity:1}")
    c.loop(".bc", "bc", "0%%,%s{opacity:1;animation-timing-function:steps(1,end)}%s,%s{opacity:0;animation-timing-function:steps(1,end)}%s,100%%{opacity:1}" % (
        c.p(T_OPEN), c.p(T_OPEN), c.p(T_CLOSE), c.p(T_CLOSE)))
    c.loop(".bo", "bo", "0%%,%s{opacity:0;animation-timing-function:steps(1,end)}%s,%s{opacity:1;animation-timing-function:steps(1,end)}%s,100%%{opacity:0}" % (
        c.p(T_OPEN), c.p(T_OPEN), c.p(T_CLOSE), c.p(T_CLOSE)))
    c.loop(".bk", "bk", "0%%,%s{translate:0 0}%s%s,100%%{translate:0 0}" % (
        c.p(T_OPEN), "".join("%s{translate:%spx 0}" % (c.p(T_OPEN + .3 * q / 100.0), n(x * 2 / 6.0)) for q, x, y in M.SHAKE[1:-1]),
        c.p(T_OPEN + .3)))
    return c.finish("Pulsar Observatory", "Observatory (demo data): pulsarcode top shows a tokens per second sparkline and three "
                                          "provider targets; one target's circuit breaker opens, its throughput drops to 0 while "
                                          "a session relays to another provider, then the breaker closes again")


CARD_BUILDERS = {"stage": card_stage, "council": card_council, "pilot": card_pilot, "observatory": card_observatory}


# ---------------------------------------------------------------------------
# Provider constellation (1280 x 520): orbits by how Core reaches each provider
# ---------------------------------------------------------------------------

C_W, C_H = 1280, 520
C_CORE = (640.0, 250.0)
RING_RX = {"local": 118.0, "native": 196.0, "cloud": 268.0, "adapter": 352.0}
RING_RATIO = .62
RING_OFFSET = {"local": -60.0, "native": -15.0, "cloud": 35.0, "adapter": -84.0}
LABEL_SIZE = 12
C_COUNTER = (C_CORE[0] - 66, C_CORE[1] - 14, C_CORE[0] + 66, C_CORE[1] + 46)   # core, count and its caption
C_LEGEND = (16.0, C_H - 34.0, 680.0, C_H - 8.0)
FLARE_WINDOW = 4.0          # degrees either side of the beam at full flare


def text_w(s: str, size: float = LABEL_SIZE) -> float:
    return len(s) * size * .58 + 2


def _overlap(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float], pad: float = 2) -> bool:
    return not (a[2] + pad <= b[0] or b[2] + pad <= a[0] or a[3] + pad <= b[1] or b[3] + pad <= a[1])


def constellation_layout() -> List[Dict[str, object]]:
    """Nodes with positions, screen angles and collision-free label boxes (asserted)."""
    groups = M.provider_groups()
    cx, cy = C_CORE
    nodes: List[Dict[str, object]] = []
    for g in M.GROUP_ORDER:
        ps = groups[g]
        rx = RING_RX[g]
        ry = rx * RING_RATIO
        for k, p in enumerate(ps):
            a = math.radians(RING_OFFSET[g] + 360.0 * k / len(ps))
            x, y = cx + rx * math.cos(a), cy + ry * math.sin(a)
            nodes.append({"id": p.id, "name": p.name, "group": g, "x": x, "y": y, "param": a,
                          "angle": math.degrees(math.atan2(y - cy, x - cx)) % 360})
    obstacles: List[Tuple[float, float, float, float]] = [(n_["x"] - 6, n_["y"] - 6, n_["x"] + 6, n_["y"] + 6) for n_ in nodes]  # type: ignore[operator]
    obstacles.append(C_COUNTER)                                     # core + counter
    obstacles.append(C_LEGEND)                                      # legend
    # the orbit names first (one spoke, clear of every node dot), then the node labels keep RING_PAD away
    for _, _, b_ in ring_labels(nodes).values():
        obstacles.append((b_[0] - RING_PAD, b_[1] - RING_PAD, b_[2] + RING_PAD, b_[3] + RING_PAD))
    placed: List[Tuple[float, float, float, float]] = []
    order = sorted(range(len(nodes)), key=lambda i: (M.GROUP_ORDER.index(nodes[i]["group"]) * -1))  # type: ignore[arg-type]
    for i in order:
        nd = nodes[i]
        x, y, a = nd["x"], nd["y"], nd["param"]  # type: ignore[assignment]
        w = text_w(str(nd["name"]))
        c_, s_ = math.cos(a), math.sin(a)  # type: ignore[arg-type]
        outer = nd["group"] == "adapter"
        cands = []
        radial = (0.0, 18.0, -18.0) if outer else (0.0,)
        for side in (1, -1):                       # outward first, then flip inside
            for rad in radial:
                for dy in (0, -14, 14, -28, 28):
                    ox = side * (9 + rad) * (1 if c_ >= 0 else -1)
                    if abs(c_) > .3:
                        anchor = "start" if ox > 0 else "end"
                        bx = x + ox if anchor == "start" else x + ox - w  # type: ignore[operator]
                        by = y + dy - 9 + side * rad * s_ * .5  # type: ignore[operator]
                    else:
                        anchor = "middle"
                        up = (s_ < 0) == (side > 0)
                        bx = x - w / 2 + dy * 2.2  # type: ignore[operator]
                        by = (y - 22 - abs(rad) * .4) if up else (y + 8 + abs(rad) * .4)  # type: ignore[operator]
                    cands.append((bx, by, anchor))
        for bx, by, anchor in cands:
            box_ = (bx, by, bx + w, by + 14)
            if box_[0] < 6 or box_[2] > C_W - 6 or box_[1] < 40 or box_[3] > C_H - 6:
                continue
            if any(_overlap(box_, o) for o in obstacles + placed):
                continue
            placed.append(box_)
            nd["label"] = box_
            nd["anchor"] = anchor
            break
        else:
            raise AssertionError("no label position for %s" % nd["name"])
    boxes = [nd["label"] for nd in nodes]
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            assert not _overlap(boxes[i], boxes[j], 0), (nodes[i]["name"], nodes[j]["name"])  # type: ignore[arg-type]
    return nodes


RING_NAMES = {"local": "LOCAL", "native": "NATIVE", "cloud": "CLOUD", "adapter": "ADAPTERS"}
RING_LABEL = 9


def ring_label_w(s: str) -> float:
    return len(s) * (RING_LABEL * .6 + 1.5)


RING_PAD = 14.0             # px: node labels keep at least this far from an orbit name
RING_CLEAR = 6.0            # px: least gap between an orbit's name and any node, label, counter or legend


def _box_gap(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]) -> float:
    """Box distance with vertical gaps counted at .6: a caption stacked right above or below a name reads
    as part of it, so vertical neighbours need more room than side-by-side ones."""
    dx = max(b[0] - a[2], a[0] - b[2], 0.0)
    dy = max(b[1] - a[3], a[1] - b[3], 0.0)
    return math.hypot(dx, dy * .6)


def ring_labels(nodes: List[Dict[str, object]]) -> Dict[str, Tuple[float, float, Tuple[float, float, float, float]]]:
    """{group: (centre x, baseline y, knockout box)}. The four orbit names sit on their own orbits along ONE
    spoke from the core, LOCAL to ADAPTERS reading outward like a scale on a chart, each knocking its
    orbit's dashed line out behind it. The spoke is the angle with the most clearance from every node dot,
    the core counter and the legend (asserted >= RING_CLEAR); constellation_layout() then keeps every node
    label RING_PAD away from these names, so a ring name is never read as part of a provider's label."""
    cx, cy = C_CORE
    avoid = [(float(nd["x"]) - 3.2, float(nd["y"]) - 3.2, float(nd["x"]) + 3.2, float(nd["y"]) + 3.2) for nd in nodes]  # type: ignore[arg-type]
    avoid += [C_COUNTER, C_LEGEND]

    def place(deg: float) -> Dict[str, Tuple[float, float, Tuple[float, float, float, float]]]:
        a = math.radians(deg)
        out = {}
        for g in M.GROUP_ORDER:
            rx, ry = RING_RX[g], RING_RX[g] * RING_RATIO
            w = ring_label_w(RING_NAMES[g])
            px_, py_ = cx + rx * math.cos(a), cy + ry * math.sin(a)
            out[g] = (px_, py_ + 3.2, (px_ - w / 2 - 4, py_ - 7, px_ + w / 2 + 4, py_ + 7))
        return out
    best = None
    for deg in range(0, 360):
        cand = place(deg)
        boxes = [b_ for _, _, b_ in cand.values()]
        if any(b_[0] < 8 or b_[2] > C_W - 8 or b_[1] < 40 or b_[3] > C_H - 8 for b_ in boxes):
            continue
        gap = min(_box_gap(b_, o) for b_ in boxes for o in avoid)
        if best is None or gap > best[0]:
            best = (gap, deg)
    assert best is not None and best[0] >= RING_CLEAR, best
    return place(best[1])


def flare_time(angle: float) -> float:
    """First time (s) either end of beam 1 passes `angle` (the constellation clock starts at load)."""
    return ((angle - M.A1_0) % 180.0) / 360.0 * M.P1


def constellation(theme: str, anim: bool = True, site: bool = False) -> str:
    t = T(theme)
    dark = theme == "dark"
    cx, cy = C_CORE
    css = Css(anim and not site)
    nodes = constellation_layout()
    groups = M.provider_groups()
    N = M.provider_count()
    defs = []
    body = [K.rect(0, 0, C_W, C_H, fill=t["bg"])]
    if dark:
        defs.append(K.radial("neb", [(0, "#2A0F4A", .8), (.5, "#150A2A", .45), (1, "#150A2A", 0)], cx, cy, 520))
        body.append(K.rect(0, 0, C_W, C_H, fill="url(#neb)"))
    css.add("text{font-family:%s}.m{font-family:%s}" % (M.SANS, M.MONO))
    orbit_col, orbit_op = (M.DUST, ".55") if dark else ("#CFC6E6", "1")
    rl = ring_labels(nodes)
    defs.append('<mask id="om" maskUnits="userSpaceOnUse" x="0" y="0" width="%d" height="%d">%s%s</mask>' % (
        C_W, C_H, K.rect(0, 0, C_W, C_H, fill="#FFFFFF"),
        "".join(K.rect(b_[0], b_[1], b_[2] - b_[0], b_[3] - b_[1], rx=3, fill="#000000") for _, _, b_ in rl.values())))
    orbits = "".join('<ellipse cx="%s" cy="%s" rx="%s" ry="%s"/>' % (n(cx), n(cy), n(RING_RX[g]), n(RING_RX[g] * RING_RATIO))
                     for g in M.GROUP_ORDER)
    body.append('<g mask="url(#om)" fill="none" stroke="%s" stroke-opacity="%s" stroke-dasharray="2 6">%s</g>' % (
        orbit_col, orbit_op, orbits))
    for g in M.GROUP_ORDER:
        lx, ly, _ = rl[g]
        body.append('<text x="%s" y="%s" font-size="%d" letter-spacing="1.5" fill="%s" class="m" text-anchor="middle">%s</text>' % (
            n(lx + .75), n(ly), RING_LABEL, t["dim"], RING_NAMES[g]))
    # beam 1 (bipolar), CLI clock from 20.05 deg
    R = 430.0
    b1 = t["beam1"]
    defs.append(K.linear_user("cb", 0, R, [(0, b1[0], 1 if dark else .9), (.12, b1[1], .8), (.6, b1[2], .25), (1, b1[2], 0)]))
    wed = "".join('<path d="%s" fill="url(#cb)" opacity="%s"/><path d="%s" fill="url(#cb)" opacity="%s" transform="scale(-1 1)"/>' % (
        K.wedge_path(R, hw * .55), n(op), K.wedge_path(R, hw * .55), n(op * .7)) for hw, op in M.BEAM_WEDGES if dark or hw < 260)
    blend = "screen" if dark else "multiply"
    body.append('<g transform="translate(%s %s)" style="mix-blend-mode:%s;opacity:%s"><g class="beam">%s</g></g>' % (
        n(cx), n(cy), blend, "1" if dark else ".6", wed))
    css.add(".beam{rotate:%sdeg}" % n(M.A1_0, 3))
    css.move(".beam{animation:bm %s linear infinite}" % K.ms(M.P1))
    css.frames("bm", "from{rotate:%sdeg}to{rotate:%sdeg}" % (n(M.A1_0, 3), n(M.A1_0 + 360, 3)))
    # nodes and labels
    rest, hot = t["dim"], t["ink"]
    css.add(".nd{transform-box:fill-box;transform-origin:50%% 50%%}.nl{fill:%s}" % rest)
    hold = 2 * (FLARE_WINDOW / math.degrees(abs(M.W1))) / M.H * 100     # +-4 deg of beam travel
    css.frames("fl", "0%%{scale:1.875;animation-timing-function:steps(1,end)}%s{scale:1.875;animation-timing-function:%s}%s{scale:1.15}18%%,100%%{scale:1}" % (
        K.pct(hold), M.E_IMPACT, K.pct(hold + 6)))
    css.frames("fll", "0%%{fill:%s;animation-timing-function:steps(1,end)}%s{fill:%s;animation-timing-function:%s}18%%,100%%{fill:%s}" % (
        hot, K.pct(hold), hot, M.E_IMPACT, rest))
    css.move(".nd{animation:fl %s linear infinite}.nl{animation:fll %s linear infinite}" % (K.ms(M.H), K.ms(M.H)))
    for nd in nodes:
        col = t["route"][nd["group"]]  # type: ignore[index]
        ang = float(nd["angle"])  # type: ignore[arg-type]
        delay = flare_time(ang) - (FLARE_WINDOW / math.degrees(abs(M.W1))) - M.H
        st = ' style="animation-delay:%s"' % K.ms(delay) if css.anim else ""
        data = ' data-id="%s" data-angle="%s"' % (nd["id"], n(ang, 2)) if site else ""
        bx, by, bx2, by2 = nd["label"]  # type: ignore[misc]
        anchor = nd["anchor"]
        tx = bx if anchor == "start" else bx2 if anchor == "end" else (bx + bx2) / 2
        body.append('<g class="node"%s>%s<text x="%s" y="%s" font-size="%d" class="nl"%s%s>%s</text></g>' % (
            data, '<circle cx="%s" cy="%s" r="3.2" fill="%s" class="nd"%s/>' % (n(nd["x"]), n(nd["y"]), col, st),  # type: ignore[arg-type]
            n(tx), n(by + 11), LABEL_SIZE, ' text-anchor="%s"' % anchor if anchor != "start" else "", st, K.esc(str(nd["name"]))))
    # core and centre counter
    defs.append(K.radial("cg", [(0, M.ICE if dark else "#6B3FD1", .9 if dark else .35), (.35, M.BEAM if dark else "#6B3FD1", .3 if dark else .1),
                                (1, M.PLASMA, 0)]))
    body.append(K.circle(cx, cy, 46, fill="url(#cg)"))
    body.append(K.circle(cx, cy, 5.5, fill=M.ICE if dark else "#1B1530"))
    if not dark:
        body.append(K.circle(cx, cy, 9, fill="none", stroke="#6B3FD1", stroke_width=1.5))
    # the count, then its unit (the site's build cuts the undated legend after "profiles</text>")
    body.append('<text x="%s" y="%s" font-size="15" font-weight="700" fill="%s" text-anchor="middle" class="m">%d</text>'
                '<text x="%s" y="%s" font-size="11" fill="%s" text-anchor="middle" class="m">provider profiles</text>' % (
                    n(cx), n(cy + 30), hot, N, n(cx), n(cy + 44), rest))
    # legend (computed)
    parts = [(M.count_phrase(), hot, None)]
    names = {"local": "local", "native": "native", "cloud": "cloud", "adapter": "adapters"}
    for g in M.GROUP_ORDER:
        parts.append(("%s %d" % (names[g], len(groups[g])), rest, t["route"][g]))  # type: ignore[index]
    x = 24.0
    y = C_H - 18
    for i, (txt, col, dot) in enumerate(parts):
        if i:
            body.append('<text x="%s" y="%s" font-size="12" fill="%s">%s</text>' % (n(x), n(y), rest, M.DOT))
            x += 14
        if dot:
            body.append(K.circle(x + 3.5, y - 4, 3.2, fill=dot))
            x += 11
        body.append('<text x="%s" y="%s" font-size="12" fill="%s">%s</text>' % (n(x), n(y), col, txt))
        x += text_w(txt) + 8
    alt = ("Provider constellation: %d provider profiles on four orbits around Pulsar Core: local %d, native %d, cloud %d, "
           "adapters %d; a beam sweeps at the terminal's speed and each name lights as it passes" % (
               N, len(groups["local"]), len(groups["native"]), len(groups["cloud"]), len(groups["adapter"])))
    svg = K.svg_doc(C_W, C_H, "".join(body), css.text(), "".join(defs), title="Provider constellation", label=alt)
    if site:
        svg = K.prefix_ids(svg, "c-")
    return svg


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


def outputs() -> Dict[Path, str]:
    out: Dict[Path, str] = {}

    def both(stem: str, theme: str, fn: Callable[[str, bool], str]) -> None:
        out[OUT / K.themed_name(stem, theme, False, V)] = fn(theme, True)
        out[OUT / K.themed_name(stem, theme, True, V)] = fn(theme, False)
    for theme in ("dark", "light"):
        for k in range(1, 5):
            both("divider-%d" % k, theme, lambda t, a, k=k: divider(k, t, a))
        both("footer", theme, footer)
        both("signal", theme, signal)
        both("constellation", theme, constellation)
        for name, fn in CARD_BUILDERS.items():
            both("card-%s" % name, theme, fn)
    return out


def site_outputs() -> Dict[Path, str]:
    out = {SITE / K.themed_name("signal", "dark", False, V): signal("dark", True),
           SITE / "constellation-site.svg": constellation("dark", False, site=True)}
    for name, fn in CARD_BUILDERS.items():
        out[SITE / K.themed_name("card-" + name, "dark", False, V)] = fn("dark", True)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--providers-md" in argv:
        sys.stdout.write(providers_md())
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    for path, svg in list(outputs().items()) + list(site_outputs().items()):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(svg, encoding="utf-8")
        print("%-52s %7.1f KB  gzip %5.1f KB" % (path.relative_to(ROOT), len(svg.encode()) / 1024, K.gz_size(svg) / 1024))
    return 0


def providers_md() -> str:
    groups = M.provider_groups()
    names = {"local": "Local engines (no key)", "native": "Anthropic-compatible (native wire)",
             "cloud": "Cloud integrations in Claude Code", "adapter": "Keyed adapters (Pulsar Core translates)"}
    lines = ["| Group | Providers |", "|---|---|"]
    for g in M.GROUP_ORDER:
        lines.append("| %s (%d) | %s |" % (names[g], len(groups[g]), ", ".join(p.name for p in groups[g])))
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
