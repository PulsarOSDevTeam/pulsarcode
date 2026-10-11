#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""The README hero: a pulsar ignites, its shockwave strikes the PULSAR wordmark cell by cell, then
the star keeps time on the CLI's real clock (fx.Beams) while a pen records every pulse.

    python tools/brand/hero.py                   # write every variant (see OUTPUTS)
    python tools/brand/hero.py --layout social   # print the social layout (for capture.py)

Variants: dark and light (chart-recorder plate), static posters of both, the social layout and the
site-inline copy (ids and classes prefixed "h-", no XML prolog). Pure CSS @keyframes: no script, no
SMIL, no fonts for brand text, no external references. Python 3.9 compatible.

The comments name the stages of the animation: S1 ignition (impact flash, shake, shockwaves, the
beams growing), S2 strike (each cell lit as the shockwave reaches it, each letter kicked), S3 glint
(beam 1 crossing the letters), S4 pulse (bloom, streak and wash at each main pulse) and S5 decode
(CODE). capture.py --accept checks the result.
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import motion as M  # noqa: E402
import svgkit as K  # noqa: E402
from svgkit import n  # noqa: E402

ROOT = M.ROOT
BRAND = ROOT / "assets" / "brand"
SITE = ROOT / "docs" / "assets" / "brand"
VERSION = "v3"

ALT = ("Pulsar Code: a pulsar ignites and lights the PULSAR CODE wordmark; a readout shows a masked key prefix "
       "(or a local engine's name) detected as its provider and routed through Pulsar Core on 127.0.0.1")


class Layout:
    def __init__(self, name: str, w: int, h: int, core: Tuple[float, float], cell: Tuple[float, float],
                 origin: Tuple[float, float], code_y: float, readout: Optional[Tuple[float, float]],
                 tele: Optional[Dict[str, float]], dolly_origin: Tuple[float, float], shake: float = 1.0,
                 code_px: float = 3.0, code_pitch: float = 30.0, glint_op: float = 0.0) -> None:
        self.name, self.w, self.h = name, w, h
        self.cx, self.cy = core
        self.cw, self.ch = cell
        self.ox, self.oy = origin
        self.code_y = code_y
        self.readout = readout
        self.tele = tele
        self.dolly_origin = dolly_origin
        self.shake = shake
        self.code_px, self.code_pitch = code_px, code_pitch
        self.glint_op = glint_op          # 0: the theme's S3 value

    @property
    def wm_w(self) -> float:
        return M.WORDMARK_COLS * self.cw

    @property
    def wm_h(self) -> float:
        return M.WORDMARK_ROWS * self.ch

    def theta_pen(self) -> float:
        """Degrees from the core to the pen (README), or to the wordmark centre (social: no pen)."""
        if self.tele:
            return math.degrees(math.atan2(self.tele["base"] - self.cy, self.tele["pen"] - self.cx))
        return math.degrees(math.atan2(self.oy + self.wm_h * .5 - self.cy, self.ox + self.wm_w * .42 - self.cx))

    def first_pulse(self) -> float:
        """Hero time (s) of the first main pulse: beam 1's north end crossing theta_pen."""
        return M.CLOCK0 + M.first_crossing(self.theta_pen())


README = Layout("readme", 1280, 440, (250, 190), (15, 30), (470, 96), 272, (470, 334),
                {"x0": 40, "pen": 1180, "base": 412, "spike": 34, "y0": 372, "y1": 430}, (640, 220))
# social: a single still, so the glint (S3) is printed at full strength to read mid-letter at link-preview size
SOCIAL = Layout("social", 1280, 640, (300, 196), (14, 28), (484, 118), 280, None, None, (640, 260), shake=1.3, glint_op=1.0)
LAYOUTS = {"readme": README, "social": SOCIAL}

PAPER = 160.0            # px of telemetry paper per P1
SIGMA = 6.0              # px, spike width
INTERPULSE = 0.22
PEN_DOWN = M.CLOCK0 + .6   # s: the pen starts inking, between pulses (the last main pulse before the clock
                           # origin is 0.09 s earlier, so a reveal at the origin would slice that spike)
STRIKE_R = 1250.0
STREAK_RX = 520.0        # S4 anamorphic streak half-length (px)
IGN_R = 720.0            # S1 impact: radius of the local flash lift around the core (px)
IGN_BLOOM = 190.0        # S1 impact: core bloom radius, kept inside the gap before the first letter
OVERLAP = 0.9
KICK = 7.0
HIT_STRIPS = 3           # S2 strike resolution inside a cell (animated variants only)
# S2 `hit` keyframe offsets (%): HOT held until the first, WARM at the second, then E_COOL to the cell's
# own fill. Light (wet ink drying) uses 6 / 35. Dark deliberately cools faster (HOT for the 2-frame
# impact, WARM by 110 ms): with 6 / 35 the near-core cells are still WARM (near white) at 600 ms, so the
# hot-to-colour gradient that capture.py's check A3 looks for at 600 ms could not exist on the dark
# plate. See tools/brand/README.md.
HIT_KEYS = {"dark": (100 / 30.0 / .62, 11 / .62), "light": (6.0, 35.0)}


def strike_time(d: float) -> float:
    """Shockwave arrival (s) at distance d: the inverse of cubic-out over 1100 ms after ignition."""
    k = min(0.999, d / STRIKE_R)
    u = 1 - (1 - k) ** (1 / 3.0)
    return M.IGNITION + 1.1 * u


def cell_classes(theme: Dict[str, object], q: int = 16) -> List[str]:
    return [M.spectrum_at(i / (q - 1), theme) for i in range(q)]


def trace_profile(x: float, main: float) -> float:
    """Recorded intensity at paper coordinate x (periodic in PAPER): main pulse 1.0, interpulse .22."""
    def gauss(c: float) -> float:
        d = (x - c) % PAPER
        d = min(d, PAPER - d)
        return math.exp(-d * d / (2 * SIGMA * SIGMA))
    return gauss(main) + INTERPULSE * gauss(main + PAPER / 2)


def trace_period_path(main: float, spike: float, seed: int = 5) -> str:
    """One period (0..PAPER) of the trace as a relative path, dense where the spikes are."""
    rng = random.Random(seed)
    xs: List[float] = []
    x = 0.0
    while x < PAPER:
        near = min(abs(((x - c) % PAPER + PAPER / 2) % PAPER - PAPER / 2) for c in (main, main + PAPER / 2))
        xs.append(x)
        x += 1.0 if near < 3.2 * SIGMA else 4.0
    xs.append(PAPER)
    noise = [rng.uniform(-.45, .45) for _ in xs]
    noise[-1] = noise[0]
    # rounded absolute points first, then deltas: no rounding drift, so every tile ends where the next begins
    pts = [(round(xx, 1), round(-spike * trace_profile(xx, main) + nz, 1)) for xx, nz in zip(xs, noise)]
    d = ["M0 %s" % n(pts[0][1], 1)]
    px, py = pts[0]
    for xx, yy in pts[1:]:
        d.append("l%s %s" % (n(xx - px, 1), n(yy - py, 1)))
        px, py = xx, yy
    return "".join(d)


def css_rule(sel: str, body: str) -> str:
    return "%s{%s}" % (sel, body)


def build(theme_name: str = "dark", layout: Layout = README, anim: bool = True) -> str:
    T = M.THEMES[theme_name]
    dark = theme_name == "dark"
    Lo = layout
    W, H = Lo.w, Lo.h
    cx, cy = Lo.cx, Lo.cy
    css: List[str] = []
    defs: List[str] = []
    body: List[str] = []
    kf: List[str] = []
    hot, warm = T["hot"], T["warm"]
    t_pulse = Lo.first_pulse()
    f_m = (t_pulse - M.CLOCK0) / M.P1           # fraction of a rotation at the first main pulse
    tele = Lo.tele

    # ------------------------------------------------------------------ defs
    if dark:
        defs.append(K.radial("neb", [(0, T["nebula"][0], .95), (.38, T["nebula"][1], .7), (1, T["nebula"][1], 0)],
                             cx, cy, 900))
        defs.append(K.radial("glow", [(0, "#FFFFFF", 1), (.1, M.ICE, .95), (.3, M.BEAM, .45), (.62, M.PLASMA, .14),
                                      (1, M.PLASMA, 0)]))
        defs.append(K.radial("bloom", [(0, M.ICE, .9), (.3, M.BEAM, .35), (.65, M.PLASMA, .1), (1, M.PLASMA, 0)]))
    else:
        defs.append(K.radial("glow", [(0, "#6B3FD1", .32), (.35, "#6B3FD1", .12), (1, "#6B3FD1", 0)]))
        defs.append(K.radial("bloom", [(0, "#6B3FD1", .5), (.35, "#7A2FF0", .16), (1, "#7A2FF0", 0)]))
    if anim:
        f, fo = T["flash"], float(T["flash_op"])  # type: ignore[arg-type]
        defs.append(K.radial("flashv", [(0, f, fo), (.12, f, fo * .7), (.3, f, fo * .28), (.55, f, fo * .1),
                                        (1, f, 0)], cx, cy, IGN_R))
    R = STRIKE_R
    b1c, b2c = T["beam1"], T["beam2"]
    if dark:
        defs.append(K.linear_user("g1", 0, R, [(0, b1c[0], 1), (.07, b1c[1], .95), (.42, b1c[2], .5), (1, b1c[2], 0)]))
        defs.append(K.linear_user("g2", 0, R, [(0, b2c[0], 1), (.07, b2c[1], .9), (.42, b2c[2], .45), (1, b2c[2], 0)]))
        defs.append(K.linear_user("gc", 0, R * .7, [(0, M.ICE, 1), (1, M.ICE, 0)]))
    else:
        defs.append(K.linear_user("g1", 0, R, [(0, b1c[0], 1), (.3, b1c[1], .8), (.6, b1c[2], .4), (1, b1c[2], 0)]))
        defs.append(K.linear_user("g2", 0, R, [(0, b2c[0], 1), (.3, b2c[1], .75), (.6, b2c[2], .35), (1, b2c[2], 0)]))
        defs.append(K.linear_user("gc", 0, R * .7, [(0, "#1B1530", .9), (1, "#1B1530", 0)]))
    defs.append(K.linear_user("spec", Lo.ox, Lo.ox + Lo.wm_w, K.spectrum_pairs(T)))
    # anamorphic streak: light, not a bar. Peak (ICE) at the core, tinted toward BEAM as it spreads, fading
    # to 0 well inside the frame on the left (the core is only 250 px from the edge) and by about 70 % of
    # the streak's half-length on the right
    sl, sr = min(cx - 24, STREAK_RX * .7), STREAK_RX * .7
    defs.append(K.linear_user("ana", cx - sl, cx + sr, [
        (0, M.BEAM if dark else "#6B3FD1", 0), (sl / (sl + sr) * .55, M.BEAM if dark else "#6B3FD1", .35),
        (sl / (sl + sr), M.ICE if dark else "#3A2A66", 1),
        (sl / (sl + sr) + sr / (sl + sr) * .35, M.BEAM if dark else "#6B3FD1", .45), (1, M.PLASMA if dark else "#6B3FD1", 0)]))
    # wordmark clip (run-length rects) for the glint and the wash
    runs = M.wordmark_runs()
    clip = "".join(K.rect(Lo.ox + c * Lo.cw, Lo.oy + r * Lo.ch, ln * Lo.cw + OVERLAP, Lo.ch + OVERLAP) for r, c, ln in runs)
    defs.append('<clipPath id="wm">%s</clipPath>' % clip)
    defs.append('<filter id="soft" x="-10%%" y="-40%%" width="120%%" height="180%%"><feGaussianBlur stdDeviation="%s"/></filter>'
                % ("11" if dark else "9"))

    # ------------------------------------------------------------------ base CSS (the poster state)
    q = cell_classes(T, M.WORDMARK_COLS)
    css.append("".join(".k%d{fill:%s}" % (i, c) for i, c in enumerate(q)))
    # animated: cells wait unlit (opacity 0) above a seamless ghost path at .13 ("lit by starlight")
    css.append(".cell{opacity:%s}" % ("0" if anim else "1"))
    css.append(".shd{opacity:%s}" % n(T["shadow_op"]))
    css.append(".b1{rotate:%sdeg}.b2{rotate:%sdeg}" % (n(M.A1_0, 3), n(M.A2_0, 3)))
    css.append(".fx0{opacity:0}")
    if dark:
        css.append(".add{mix-blend-mode:screen}")
    else:
        # paper is opaque and near white, so multiply == normal blending of the same ink: the beam
        # opacity is folded into each wedge instead of blending/isolating the rotating group (cheaper)
        pass
    css.append(".gl{mix-blend-mode:%s;opacity:%s}" % (T["glint_blend"], n(Lo.glint_op or T["glint_op"])))
    css.append("text{font-family:%s}" % M.MONO)

    # ------------------------------------------------------------------ animation CSS
    sp = M.timing("spring")
    ox_, oy_ = Lo.dolly_origin
    if anim:
        css.append(".dolly{transform-origin:%spx %spx;animation:dolly 5s %s backwards}" % (n(ox_), n(oy_), M.E_SETTLE))
        kf.append("@keyframes dolly{from{scale:1.04}to{scale:1}}")
        a = Lo.shake
        shake = "".join("%s%%{translate:%spx %spx}" % (p, n(x * a), n(y * a)) for p, x, y in M.SHAKE)
        css.append(".cam{animation:shake %s linear %s}" % (K.ms(M.SHAKE_MS / 1000), K.ms(M.IGNITION)))
        kf.append("@keyframes shake{%s}" % shake)
        css.append(".chg{transform-origin:%spx %spx;animation:chg .3s %s backwards}" % (n(cx), n(cy), M.E_CHARGE))
        kf.append("@keyframes chg{from{scale:.15;opacity:.35}to{scale:1;opacity:1}}")
        css.append(".brt{transform-origin:%spx %spx;animation:brt %s %s .3s infinite}" % (n(cx), n(cy), K.ms(M.H), M.E_BREATHE))
        kf.append("@keyframes brt{0%,100%{scale:1}50%{scale:.94}}")
        css.append(".grow{transform-origin:0 0;animation:grow .7s %s backwards;%s}" % (K.ms(M.CLOCK0), sp))
        kf.append("@keyframes grow{from{scale:0}to{scale:1}}")
        css.append(".b1{animation:r1 %s linear %s infinite}" % (K.ms(M.P1), K.ms(M.CLOCK0)))
        css.append(".b2{animation:r2 %s linear %s infinite}" % (K.ms(M.P2), K.ms(M.CLOCK0)))
        kf.append("@keyframes r1{from{rotate:%sdeg}to{rotate:%sdeg}}" % (n(M.A1_0, 3), n(M.A1_0 + 360 * M.SPIN1, 3)))
        kf.append("@keyframes r2{from{rotate:%sdeg}to{rotate:%sdeg}}" % (n(M.A2_0, 3), n(M.A2_0 + 360 * M.SPIN2, 3)))
        # S1 impact frame: two frames held, then decay
        hold = M.F / (M.F + .9) * 100
        css.append(".ign{animation:ign %s %s}" % (K.ms(M.F + .9), K.ms(M.IGNITION)))
        kf.append("@keyframes ign{0%%{opacity:1}%s{opacity:1;animation-timing-function:%s}100%%{opacity:0}}"
                  % (K.pct(hold), M.E_IMPACT))
        css.append(".ring{transform-origin:%spx %spx;animation:ring 1.1s %s %s}" % (n(cx), n(cy), M.E_SHOCK, K.ms(M.IGNITION)))
        css.append(".ring2{animation-duration:1.4s;animation-delay:%s}" % K.ms(M.IGNITION + .12))
        kf.append("@keyframes ring{0%{scale:.01;opacity:.9;stroke-width:14px}100%{scale:1;opacity:0;stroke-width:.6px}}")
        css.append(".ring2{animation-name:ring2}")
        kf.append("@keyframes ring2{0%{scale:.01;opacity:.8;stroke-width:6px}100%{scale:1;opacity:0;stroke-width:.6px}}")
        # S2 shock-lit cells (fill-mode forwards only)
        k6, k35 = HIT_KEYS[theme_name]
        css.append(".cell{animation:hit .62s linear forwards}.lt{animation:kick .7s linear}")
        kf.append("@keyframes hit{0%%{opacity:1;fill:%s}%s{fill:%s}%s{fill:%s;animation-timing-function:%s}100%%{opacity:1}}"
                  % (hot, K.pct(k6), hot, K.pct(k35), warm, M.E_COOL))
        kf.append("@keyframes kick{0%%{translate:0 0;animation-timing-function:%s}12%%{translate:var(--kx) var(--ky);%s}"
                  "100%%{translate:0 0}}" % (M.E_SETTLE, M.timing("kick")))
        css.append(".shd{animation:shd .3s %s backwards}" % M.E_SETTLE)
        kf.append("@keyframes shd{from{opacity:0}}")
        # S5 decode of CODE
        css.append(".nz{opacity:0;animation:nz .04s steps(1,end)}")
        kf.append("@keyframes nz{0%,100%{opacity:1}}")
        css.append(".lk{animation:lk .001s backwards}")
        kf.append("@keyframes lk{0%,100%{opacity:0}}")
        css.append(".rule{transform-box:fill-box;animation:rule .6s %s 1.05s backwards}.rl{transform-origin:100%% 50%%}"
                   ".rr{transform-origin:0 50%%}" % M.E_SETTLE)
        kf.append("@keyframes rule{from{scale:0 1}}")
        # S4 pulse: bloom + streak + wash every L, the first at the first main pulse
        rise, decay = .04, .45
        css.append(".pl{animation:pl %s linear %s infinite}" % (K.ms(M.L), K.ms(t_pulse - rise)))
        kf.append("@keyframes pl{0%%{opacity:0}%s{opacity:1;animation-timing-function:%s}%s{opacity:0}100%%{opacity:0}}"
                  % (K.pct(rise / M.L * 100), M.E_IMPACT, K.pct((rise + decay) / M.L * 100)))
    # ------------------------------------------------------------------ layers
    ext = 12
    body.append(K.rect(0, 0, W, H, fill=T["bg"]))
    if not dark:
        # chart-recorder plate: 40 px grid, 200 px major grid (static, part of the frame)
        defs.append('<pattern id="grid" width="200" height="200" patternUnits="userSpaceOnUse">'
                    '<path d="%s" stroke="%s" fill="none" shape-rendering="crispEdges"/>'
                    '<path d="M.5 0V200M0 .5H200" stroke="%s" fill="none" shape-rendering="crispEdges"/></pattern>'
                    % ("".join("M%s 0V200M0 %sH200" % (n(k + .5), n(k + .5)) for k in range(40, 200, 40)), T["grid"],
                       T["grid_major"]))
        body.append(K.rect(0, 0, W, H, fill="url(#grid)"))
    cam: List[str] = []
    # sky
    rng = random.Random(11)
    stars: Dict[str, List[str]] = {}
    for _ in range(120):
        x, y = rng.uniform(-ext, W + ext), rng.uniform(-ext, H + ext)
        r = rng.uniform(.4, 1.3)
        o = rng.choice((.2, .35, .5, .65, .8))
        if not dark:
            o = o * .45
        stars.setdefault(n(o), []).append(K.circle(round(x), round(y), round(r, 1)))
    sky = "".join(K.g("".join(v), opacity=k) for k, v in sorted(stars.items()))
    cam.append(K.g(sky, fill=T["star"]))
    if dark:
        cam.append(K.rect(-ext, -ext, W + 2 * ext, H + 2 * ext, fill="url(#neb)"))

    # beams (S1 grow, fx clock)
    def beam_end(grad: str, sign: int, dim: float, light: bool) -> str:
        out = []
        for hw, op in M.BEAM_WEDGES:
            if light and hw >= 260:
                continue
            out.append('<path d="%s" fill="url(#%s)" opacity="%s"%s/>' % (
                K.wedge_path(R, hw), grad, n(op * dim), ' transform="scale(-1 1)"' if sign < 0 else ""))
        out.append('<path d="M0 -%sL%s 0L0 %sZ" fill="url(#gc)" opacity="%s"%s/>' % (
            n(M.BEAM_CORE_LINE / 2), n(R * .7), n(M.BEAM_CORE_LINE / 2), n(dim), ' transform="scale(-1 1)"' if sign < 0 else ""))
        return "".join(out)

    light = not dark
    bo = 1.0 if dark else float(T["beam_op"])  # type: ignore[arg-type]
    b1 = beam_end("g1", 1, 1.0 * bo, light) + beam_end("g1", -1, .62 * bo, light)
    b2 = beam_end("g2", 1, .8 * bo, light) + beam_end("g2", -1, .55 * bo, light)
    b1_glint = b1 if dark else beam_end("g1", 1, 1.0, light) + beam_end("g1", -1, .62, light)
    cam.append('<g transform="translate(%s %s)" class="add"><g class="grow"><g class="b2">%s</g><g class="b1">%s</g></g></g>'
               % (n(cx), n(cy), b2, b1))

    # wordmark: static glow, shadows, cells
    glow = "".join(K.rect(Lo.ox + c * Lo.cw, Lo.oy + r * Lo.ch, ln * Lo.cw, Lo.ch) for r, c, ln in runs)
    cam.append('<g filter="url(#soft)" opacity="%s" fill="url(#spec)">%s</g>' % (".24" if dark else ".12", glow))
    if anim:
        ghost = "".join("M%s %sh%sv%sh%sz" % (n(Lo.ox + c * Lo.cw), n(Lo.oy + r * Lo.ch), n(ln * Lo.cw), n(Lo.ch), n(-ln * Lo.cw))
                        for r, c, ln in runs)
        cam.append('<path d="%s" fill="url(#spec)" opacity=".13"/>' % ghost)
    # S2: every cell is struck (hot flash) at its own shockwave arrival, but each LETTER kicks as one
    # rigid body: one translate per letter, from the instant the front first touches it, along the
    # vector from the core to the letter's centroid. Neighbouring cells therefore never drift apart and
    # no seam can open inside a letter during the hot phase.
    shd, front = [], []
    times = []
    for li, letter in enumerate(M.wordmark_letters()):
        lx = sum(Lo.ox + (x + .5) * Lo.cw for x, _ in letter) / len(letter)
        ly = sum(Lo.oy + (y + .5) * Lo.ch for _, y in letter) / len(letter)
        ang = math.atan2(ly - cy, lx - cx)
        kx, ky = math.cos(ang) * KICK, math.sin(ang) * KICK
        l_ts = min(strike_time(math.hypot(Lo.ox + (x + .5) * Lo.cw - cx, Lo.oy + (y + .5) * Lo.ch - cy)) for x, y in letter)
        f_cells, s_cells = [], []
        for x, y in letter:
            px, py = Lo.ox + x * Lo.cw, Lo.oy + y * Lo.ch
            mx, my = px + Lo.cw / 2, py + Lo.ch / 2
            ts = strike_time(math.hypot(mx - cx, my - cy))
            times.append(ts)
            k = int(round(x / (M.WORDMARK_COLS - 1) * (len(q) - 1)))
            geo = 'x="%s" y="%s" width="%s" height="%s"' % (n(px), n(py), n(Lo.cw + OVERLAP), n(Lo.ch + OVERLAP))
            sst = ' style="animation-delay:%s"' % K.ms(ts + M.SHADOW_LAG) if anim else ""
            if anim:
                # the hot phase is struck in HIT_STRIPS vertical strips per cell, each at its own arrival
                # time: neighbouring 15 px columns cool ~7 ms apart, which E_COOL's steep start turned into
                # visible per-column bands; 5 px strips make the hot-to-colour front a soft wipe
                sw = Lo.cw / HIT_STRIPS
                for j in range(HIT_STRIPS):
                    sx = px + j * sw
                    tj = strike_time(math.hypot(sx + sw / 2 - cx, my - cy))
                    times.append(tj)
                    f_cells.append('<rect x="%s" y="%s" width="%s" height="%s" class="cell k%d" style="animation-delay:%s"/>' % (
                        n(sx), n(py), n(sw + OVERLAP if j == HIT_STRIPS - 1 else sw + .5), n(Lo.ch + OVERLAP), k, K.ms(tj)))
            else:
                f_cells.append('<rect %s class="cell k%d"/>' % (geo, k))
            s_cells.append('<rect %s class="shd"%s/>' % (geo, sst))
        lst = ' style="animation-delay:%s;--kx:%spx;--ky:%spx"' % (K.ms(l_ts), n(kx, 1), n(ky, 1)) if anim else ""
        sst = ' style="animation-delay:%s;--kx:%spx;--ky:%spx"' % (K.ms(l_ts + M.SHADOW_LAG), n(kx, 1), n(ky, 1)) if anim else ""
        front.append('<g class="lt"%s>%s</g>' % (lst, "".join(f_cells)))
        shd.append('<g class="lt"%s>%s</g>' % (sst, "".join(s_cells)))
    cam.append('<g transform="translate(6 6)" fill="%s">%s</g>' % (T["shadow"], "".join(shd)))
    cam.append('<g shape-rendering="crispEdges">%s</g>' % "".join(front))
    # S3 glint: a copy of beam 1, clipped to the cells
    cam.append('<g clip-path="url(#wm)"><g class="gl"><g transform="translate(%s %s)"><g class="grow"><g class="b1">%s</g></g></g></g></g>'
               % (n(cx), n(cy), b1_glint))
    # S4 wash, clipped to the letters (peak .10)
    cam.append('<g class="fx0 pl"><g clip-path="url(#wm)">%s</g></g>'
               % K.rect(Lo.ox, Lo.oy, Lo.wm_w + 1, Lo.wm_h + 1, fill=M.ICE if dark else "#6B3FD1", opacity=".1"))

    # CODE (pixel font) + rules
    px_ = Lo.code_px
    pitch = Lo.code_pitch
    code_w = K.pixel_text_width("CODE", px_, pitch)
    mid = Lo.ox + Lo.wm_w / 2
    code_x = mid - code_w / 2
    code_y = Lo.code_y
    ink = T["ink"]
    pats = M.decode_patterns()
    code = []
    for i, ch in enumerate("CODE"):
        gx = code_x + i * pitch
        t0 = M.snap(.95 + i * .06)
        if anim:
            cols = ((M.BEAM if dark else "#0A8FB0", 1), (M.BEAM if dark else "#0A8FB0", .3), (ink, .7))
            for j, (col, op) in enumerate(cols):
                code.append('<path d="%s" fill="%s"%s class="nz" style="animation-delay:%s"/>' % (
                    K.pixel_glyph_path(ch, gx, code_y, px_, pats[j], .15), col, ' fill-opacity="%s"' % n(op) if op < 1 else "",
                    K.ms(t0 + j * .04)))
            code.append('<path d="%s" class="lk" style="animation-delay:%s"/>' % (
                K.pixel_glyph_path(ch, gx, code_y, px_, None, .15), K.ms(t0 + .12)))
        else:
            code.append('<path d="%s"/>' % K.pixel_glyph_path(ch, gx, code_y, px_, None, .15))
    cam.append(K.g("".join(code), fill=ink))
    ry = code_y + 3.5 * px_ - .75
    gap = 24
    cam.append(K.rect(Lo.ox, ry, code_x - gap - Lo.ox, 1.5, fill="url(#spec)", class_="rule rl"))
    cam.append(K.rect(code_x + code_w + gap, ry, Lo.ox + Lo.wm_w - (code_x + code_w + gap), 1.5, fill="url(#spec)",
                      class_="rule rr"))

    # the star
    core = []
    core.append(K.circle(cx, cy, 70, fill="url(#glow)"))
    if dark:
        core.append(K.circle(cx, cy, 5.5, fill="#FFFFFF"))
    else:
        core.append(K.circle(cx, cy, 9, fill="none", stroke="#6B3FD1", stroke_width=1.5))
        core.append(K.circle(cx, cy, 5.5, fill="#1B1530"))
    cam.append(K.circle(cx, cy, 3, fill=M.ICE if dark else "#1B1530", opacity=".5"))
    cam.append('<g class="chg"><g class="brt">%s</g></g>' % "".join(core))
    # S4 bloom + one anamorphic streak (every L)
    cam.append('<g class="fx0 pl%s">%s%s%s</g>' % (
        " add" if dark else "", K.circle(cx, cy, 160, fill="url(#bloom)"),
        '<ellipse cx="%s" cy="%s" rx="%s" ry="9" fill="url(#ana)" opacity="%s"/>' % (n(cx), n(cy), n(STREAK_RX), ".12" if dark else ".1"),
        '<ellipse cx="%s" cy="%s" rx="%s" ry="2.2" fill="url(#ana)" opacity="%s"/>' % (n(cx), n(cy), n(STREAK_RX), ".45" if dark else ".35")))
    # S1 ignition flash + core bloom, shockwaves
    if anim:
        # Local, not a flood: a radial lift centred on the core (its peak is the theme's flash_op),
        # falling to a faint vignette by the first letter and to nothing well before the far edge, plus
        # a tight core bloom that stops short of the wordmark. The letters are never greyed (no letter
        # wash above .10, no full flood).
        cam.append('<g class="fx0 ign">%s%s</g>' % (
            K.circle(cx, cy, IGN_R, fill="url(#flashv)"),
            K.circle(cx, cy, IGN_BLOOM, fill="url(#bloom)")))
        ring_col = "#DDF8FF" if dark else "#6B3FD1"
        cam.append(K.circle(cx, cy, R, fill="none", stroke=ring_col, class_="fx0 ring", vector_effect="non-scaling-stroke"))
        cam.append(K.circle(cx, cy, R, fill="none", stroke=M.PLASMA if dark else "#7A2FF0",
                            class_="fx0 ring ring2", vector_effect="non-scaling-stroke"))

    # readout (README layout)
    if Lo.readout:
        cam.append(readout_block(T, Lo, anim, css, kf))
    # telemetry
    if tele:
        cam.append(telemetry_block(T, Lo, anim, css, kf, f_m, t_pulse))
    if Lo.name == "social":
        cam.append(social_text(T, Lo))
    body.append('<g class="dolly"><g class="cam">%s</g></g>' % "".join(cam))

    # reduced motion: the designed poster (base state) + cells lit
    if anim:
        rm = ".cell{opacity:1}"
        css.append("".join(kf))
        css.append(K.reduced_motion(rm))
    title = "Pulsar Code"
    desc = ("A pulsar beside the PULSAR CODE wordmark; its two beams turn at the speeds the terminal uses "
            "(%s s and %s s per turn)" % (n(M.P1, 2), n(M.P2, 2)))
    return K.svg_doc(W, H, "".join(body), "".join(css), "".join(defs), title=title, desc=desc,
                     label=ALT if Lo.name == "readme" else "Pulsar Code: " + M.TAGLINE)


def readout_block(T: Dict[str, object], Lo: Layout, anim: bool, css: List[str], kf: List[str]) -> str:
    """Five generated entries stacked in a clipped window; one swap animation, one decode flicker."""
    x, y = Lo.readout  # type: ignore[misc]
    size = M.SIZES["readout"]
    lh = 24.0
    route = T["route"]  # type: ignore[assignment]
    dark = T["name"] == "dark"
    bullet_col = M.DUST if dark else "#9C94BC"
    arrow_col = M.DUST if dark else "#9C94BC"
    dim = T["dim"]
    ink = T["ink"]
    lines = []
    for i, e in enumerate(M.readout_entries()):
        col = route[e["group"] if e["group"] != "adapter" else "adapter"]  # type: ignore[index]
        pre = e["prefix"]
        bullets = "" if e["keyless"] else M.BULLET * 8
        parts = [(pre, col), (bullets, bullet_col), ("  %s  " % M.ARROW, arrow_col), (e["name"], ink),
                 (" %s %s %s %s" % (M.DOT, e["route"], M.DOT, e["wire"]), dim), ("  %s  " % M.ARROW, arrow_col),
                 ("Pulsar Core", ink), (" " + M.CORE_HOST, dim)]
        text = "".join(p for p, _ in parts)
        spans = "".join('<tspan fill="%s">%s</tspan>' % (c, K.esc(p)) for p, c in parts if p)
        length = len(text) * size * M.MONO_ADVANCE
        lines.append('<text x="%s" y="%s" font-size="%d" textLength="%s" lengthAdjust="spacingAndGlyphs" xml:space="preserve">%s</text>'
                     % (n(x), n(y + i * lh), size, n(length), spans))
    n_e = len(lines)
    clip_id = "roc"
    defs = '<clipPath id="%s">%s</clipPath>' % (clip_id, K.rect(x - 4, y - size - 4, 900, lh))
    if anim:
        L = M.L
        css.append(".rs{animation:rs %s steps(1,end) 1.4s infinite}" % K.ms(L * n_e))
        frames = "".join("%s{translate:0 %spx}" % (K.pct(100.0 * k / n_e), n(-lh * k)) for k in range(n_e))
        kf.append("@keyframes rs{%s100%%{translate:0 %spx}}" % (frames, n(-lh * (n_e - 1))))
        a, b = .12 / L * 100, (L - .12) / L * 100
        css.append(".rf{animation:rf %s linear 1.4s infinite backwards}" % K.ms(L))
        kf.append("@keyframes rf{0%%{opacity:0;animation-timing-function:steps(3,end)}%s{opacity:1}%s{opacity:1;"
                  "animation-timing-function:steps(3,end)}100%%{opacity:0}}" % (K.pct(a), K.pct(b)))
        css.append(".ri{animation:ri .52s 1.4s backwards;%s}" % M.timing("soft"))
        kf.append("@keyframes ri{from{translate:0 6px}to{translate:0 0}}")
        return '<defs>%s</defs><g clip-path="url(#%s)"><g class="ri"><g class="rf"><g class="rs">%s</g></g></g></g>' % (
            defs, clip_id, "".join(lines))
    return '<defs>%s</defs><g clip-path="url(#%s)">%s</g>' % (defs, clip_id, "".join(lines))


def telemetry_block(T: Dict[str, object], Lo: Layout, anim: bool, css: List[str], kf: List[str], f_m: float,
                    t_pulse: float) -> str:
    tl = Lo.tele
    assert tl is not None
    x0, pen, base, spike = tl["x0"], tl["pen"], tl["base"], tl["spike"]
    dark = T["name"] == "dark"
    main = (pen + PAPER * f_m) % PAPER          # paper x of a main spike (phase lock)
    s0 = PAPER * f_m                            # poster offset: a main spike under the pen
    period = trace_period_path(main, spike)
    trace_col = T["trace"]
    out = []
    defs = []
    defs.append('<path id="tp" d="%s" fill="none" stroke="%s" stroke-width="1.5" stroke-linejoin="round"/>' % (period, trace_col))
    defs.append('<linearGradient id="tf" gradientUnits="userSpaceOnUse" x1="%s" x2="%s" y1="0" y2="0">%s</linearGradient>'
                % (n(x0), n(x0 + 260), K.stops([(0, "#FFFFFF", 0), (1, "#FFFFFF", 1)])))
    defs.append('<mask id="tm" maskUnits="userSpaceOnUse" x="0" y="%s" width="%d" height="%s">%s</mask>' % (
        n(tl["y0"] - 4), Lo.w, n(tl["y1"] - tl["y0"] + 8), K.rect(x0, tl["y0"] - 4, pen - x0, tl["y1"] - tl["y0"] + 8, fill="url(#tf)")))
    copies = int(math.ceil((pen + PAPER - x0) / PAPER)) + 2
    start = math.floor(x0 / PAPER) * PAPER
    uses = "".join('<use href="#tp" x="%s" y="%s"/>' % (n(start + k * PAPER), n(base)) for k in range(copies))
    ticks = ""
    if dark:
        tk = "".join("M%s %sv4" % (n(start + k * 40), n(base + 9)) for k in range(copies * 4))
        ticks = '<path d="%s" stroke="%s" stroke-opacity=".55" fill="none"/>' % (tk, M.DUST)
        base_line = ""
    else:
        base_line = ""
    if anim:
        # the paper starts blank: the pen puts ink down from PEN_DOWN on, so the recording grows leftwards
        # from the pen as the paper scrolls (nothing on the paper predates the ignition). One rect in a
        # mask, soft-edged over 16 px, slides left at paper speed until the ink reaches the left edge.
        defs.append(K.linear_user("rvg", pen - 8, pen + 8, [(0, "#000000", 1), (1, "#FFFFFF", 1)]))
        defs.append('<mask id="rvm" maskUnits="userSpaceOnUse" x="0" y="%s" width="%d" height="%s">%s</mask>' % (
            n(tl["y0"] - 4), Lo.w, n(tl["y1"] - tl["y0"] + 8),
            K.rect(pen - 8, tl["y0"] - 4, Lo.w, tl["y1"] - tl["y0"] + 8, fill="url(#rvg)", class_="rv")))
    out.append('<defs>%s</defs>' % "".join(defs))
    paper = '<g class="paper">%s%s</g>' % (uses, ticks)
    if anim:
        paper = '<g mask="url(#rvm)">%s</g>' % paper
    out.append('<g mask="url(#tm)">%s</g>%s' % (paper, base_line))
    # pen arm, ring and dot
    pen_col = T["pen"]
    arm_col = M.DUST if dark else "#B6ACD6"
    out.append(K.rect(pen - .5, tl["y0"] - 2, 1, base - spike - 6 - tl["y0"] + 2, fill=arm_col, opacity=".7"))
    yp = -spike * trace_profile(pen + s0, main)
    out.append('<g class="pen" style="translate:0 %spx">%s%s</g>' % (
        n(yp, 1), K.circle(pen, base, 7, fill="none", stroke=M.BEAM if dark else "#6B3FD1", stroke_width=1.2,
                           class_="fx0 pr"), K.circle(pen, base, 2.5, fill=pen_col)))
    if not dark:
        cap = "CHART 01 %s BEAM PERIOD %s s" % (M.DOT, "%.2f" % M.P1)
        out.append(K.mono_text(x0, Lo.h - 6, cap, 10, "#5D5878", letter_spacing=".5"))
    if anim:
        css.append(".paper{translate:%spx 0;animation:paper %s linear %s infinite backwards}" % (
            n(-s0, 2), K.ms(M.P1), K.ms(M.CLOCK0)))
        kf.append("@keyframes paper{from{translate:0 0}to{translate:-%spx 0}}" % n(PAPER))
        # pen rides the trace: sample the profile under the pen over one rotation (dense near spikes)
        keys = []
        s = 0.0
        while s <= PAPER + 1e-6:
            v = -spike * trace_profile(pen + s, main)
            keys.append((s / PAPER * 100, v))
            near = min(abs(((pen + s - c) % PAPER + PAPER / 2) % PAPER - PAPER / 2) for c in (main, main + PAPER / 2))
            s += 1.0 if near < 3 * SIGMA else 6.0
        if keys[-1][0] < 100:
            keys.append((100, keys[0][1]))
        css.append(".pen{animation:pen %s linear %s infinite backwards}" % (K.ms(M.P1), K.ms(M.CLOCK0)))
        kf.append("@keyframes pen{%s}" % "".join("%s{translate:0 %spx}" % (K.pct(p), n(v, 1)) for p, v in keys))
        # glow ring at every main pulse (cause: beam 1 north end crossing theta_pen)
        css.append(".pr{transform-box:fill-box;transform-origin:50%% 50%%;animation:pr %s linear %s infinite}" % (
            K.ms(M.P1), K.ms(t_pulse)))
        kf.append("@keyframes pr{0%%{opacity:1;scale:.6;animation-timing-function:%s}%s{opacity:0;scale:1.4}100%%{opacity:0;scale:1.4}}"
                  % (M.E_SHOCK, K.pct(.4 / M.P1 * 100)))
        css.append(".tele{animation:tele .4s %s 1.6s backwards}" % M.E_SETTLE)
        travel = pen + 8 - (x0 - 8)
        css.append(".rv{translate:-%spx 0;animation:rv %s linear %s backwards}" % (n(travel), K.ms(travel / (PAPER / M.P1)),
                                                                                  K.ms(PEN_DOWN)))
        kf.append("@keyframes rv{from{translate:0 0}}")
        kf.append("@keyframes tele{from{opacity:0}}")
    return '<g class="tele">%s</g>' % "".join(out)


SOCIAL_CHIPS = ("NVIDIA NIM", "OpenRouter", "OpenAI", "Google Gemini", "DeepSeek", "Moonshot Kimi", "Ollama",
                "Amazon Bedrock")


def social_chips() -> List[Tuple[str, str]]:
    """(name, group) for the 8 named chips; names must be advertised providers."""
    by_name = {p.name: p for p in M.advertised_providers()}
    out = []
    for name in SOCIAL_CHIPS:
        p = by_name[name]
        out.append((name, M.provider_group(p)))
    return out


SOCIAL_UNIT = "providers"   # pinned by tests/test_screens_public.py; the README says "provider profiles"
SOCIAL_X = 484          # the wordmark's left edge: the whole text block hangs from this one axis


def social_text(T: Dict[str, object], Lo: Layout) -> str:
    """Tagline and readout, left-aligned to the wordmark (fonts come only from the capture page's
    local @font-face: no system fallback, so the raster does not depend on the machine)."""
    out = []
    route = T["route"]  # type: ignore[assignment]
    x = Lo.ox
    out.append('<text x="%s" y="383" font-size="34" font-weight="600" fill="#B9B4D6" '
               'style="font-family:Inter">%s</text>' % (n(x), K.esc(M.TAGLINE)))
    e = M.readout_entries()[0]
    size = 15
    parts = [(e["prefix"], route["adapter"]), (M.BULLET * 8, M.DUST), ("  %s  " % M.ARROW, M.DUST), (e["name"], M.ICE),
             (" %s %s %s %s" % (M.DOT, e["route"], M.DOT, e["wire"]), M.ASH), ("  %s  " % M.ARROW, M.DUST),
             ("Pulsar Core", M.ICE), (" " + M.CORE_HOST, M.ASH)]
    text = "".join(p for p, _ in parts)
    length = len(text) * size * M.MONO_ADVANCE
    out.append('<text x="%s" y="426" font-size="%d" textLength="%s" lengthAdjust="spacingAndGlyphs" '
               'xml:space="preserve" style="font-family:\'JetBrains Mono\'">%s</text>' % (
                   n(x), size, n(length), "".join('<tspan fill="%s">%s</tspan>' % (c, K.esc(p)) for p, c in parts)))
    return "".join(out)


def social_overlay_html() -> str:
    """Route-coloured chips (8 names + the computed +N) and the footer line, as HTML over the social SVG."""
    chips = social_chips()
    more = M.provider_count() - len(chips)
    route = M.DARK["route"]
    spans = "".join('<span><i style="background:%s"></i>%s</span>' % (route[g], K.esc(nm)) for nm, g in chips)  # type: ignore[index]
    if more > 0:
        spans += "<span>+%d</span>" % more
    return ("<div class='chips'>%s</div><div class='foot'>%s %s %s</div>"
            % (spans, K.esc(M.count_phrase(SOCIAL_UNIT)), M.DOT, M.URL))


SOCIAL_CSS = (".chips{position:absolute;left:%dpx;width:%dpx;top:452px;display:flex;flex-wrap:wrap;gap:8px;"
              "font:500 14px Inter;color:#ECF6FF}"
              ".chips span{display:flex;align-items:center;gap:8px;height:30px;padding:0 12px;border-radius:15px;"
              "background:#13101E;border:1px solid #FFFFFF1F;box-sizing:border-box}"
              ".chips i{width:6.4px;height:6.4px;border-radius:50%%}"
              ".foot{position:absolute;left:%dpx;top:560px;font:16px 'JetBrains Mono';color:#9692AC}"
              % (SOCIAL_X, 1230 - SOCIAL_X, SOCIAL_X))


def outputs() -> Dict[Path, str]:
    v = VERSION
    out = {
        BRAND / K.themed_name("hero", "dark", False, v): build("dark", README, True),
        BRAND / K.themed_name("hero", "light", False, v): build("light", README, True),
        BRAND / K.themed_name("hero", "dark", True, v): build("dark", README, False),
        BRAND / K.themed_name("hero", "light", True, v): build("light", README, False),
    }
    return out


def site_outputs() -> Dict[Path, str]:
    inline = K.prefix_ids(build("dark", README, True), "h-")
    return {
        SITE / "hero-inline-dark.svg": inline,
        SITE / K.themed_name("hero", "dark", True, VERSION): build("dark", README, False),
    }


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--layout" in argv:
        lay = argv[argv.index("--layout") + 1]
        sys.stdout.write(build("dark", LAYOUTS[lay], "--static" not in argv))
        return 0
    for path, svg in list(outputs().items()) + list(site_outputs().items()):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(svg, encoding="utf-8")
        print("%-48s %7.1f KB  gzip %5.1f KB" % (path.relative_to(ROOT), len(svg.encode()) / 1024, K.gz_size(svg) / 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
