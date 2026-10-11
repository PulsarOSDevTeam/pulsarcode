# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Pulsar Code motion and colour tokens: the one clock every brand surface keeps.

Every number that can be read from the code is read from the code: the beam clock comes from
`pulsar.fx.Beams.angles`, the palette from the `pulsar.fx` constants, the Pilot curves from
`pulsar.pilot.overlay`, provider groups and counts from `pulsar.screens.advertised_providers()`
and key prefixes from `pulsar.providers._BY_PREFIX`. Nothing here may hard-code a count.

    python tools/brand/motion.py            # print tokens.json to stdout
    python tools/brand/motion.py --write    # write tools/brand/tokens.json

Imported read-only by tools/demo; the site copies tokens.json.
Python 3.9 compatible.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pulsar import fx  # noqa: E402
from pulsar import providers as _P  # noqa: E402
from pulsar.pilot import overlay as _OV  # noqa: E402
from pulsar.screens import advertised_providers  # noqa: E402

TOKENS_PATH = Path(__file__).resolve().parent / "tokens.json"

# ---------------------------------------------------------------------------
# Copy
# ---------------------------------------------------------------------------

TAGLINE = "Run the official Claude Code CLI on any model"
URL = "github.com/PulsarOSDevTeam/pulsarcode"
FAKE_KEY = "sk-or-v1-fake-key-not-real"
CORE_HOST = "127.0.0.1"
BULLET = "•"
DOT = "·"
ARROW = "→"

# ---------------------------------------------------------------------------
# The clock (read from fx.Beams.angles: a = w t + a0)
# ---------------------------------------------------------------------------

_a0 = fx.Beams.angles(0.0)
_a1 = fx.Beams.angles(1.0)
W1 = _a1[0] - _a0[0]                 # rad/s, beam 1 (1.25 today, clockwise on screen: y is down)
W2 = _a1[1] - _a0[1]                 # rad/s, beam 2 (-0.95 today, counter-rotating)
A1_0 = math.degrees(_a0[0])          # beam 1 at t = 0, degrees (20.05 today)
A2_0 = math.degrees(_a0[1])          # beam 2 at t = 0, degrees (120.32 today)
P1 = 2 * math.pi / abs(W1)           # 5.0265 s
P2 = 2 * math.pi / abs(W2)           # 6.6139 s
H = P1 / 2                           # core breathe; a bipolar beam passes any angle every H
Q = P1 / 4                           # phase step between dividers and between cards
L = 2 * P1                           # loop of cards, Signal Path, full bloom, readout swap
F = 1.0 / 30                         # impact frame: two frames at 60 fps (33.3 ms)
BEATS_MS = (0, 300, 750, 1250, 1650)  # charge / ignition / readable / CODE locked / at rest
STAGGER_MS = {"cards": 60, "chips": 60, "keycaps": 50, "column": 11}
SHADOW_LAG = 0.050
IGNITION = 0.300                     # s: impact frame
CLOCK0 = 0.340                       # s: beams start turning (hero clock origin)
SPIN1 = 1 if W1 > 0 else -1          # +1 = clockwise in SVG
SPIN2 = 1 if W2 > 0 else -1


def snap(t: float) -> float:
    """Snap a one-shot delay (s) to the impact-frame grid."""
    return round(t / F) * F


def beam1_angle(t: float) -> float:
    """Beam 1 angle in degrees at hero time t (s): the fx clock starting at CLOCK0."""
    return A1_0 + math.degrees(W1) * max(0.0, t - CLOCK0)


def beam2_angle(t: float) -> float:
    return A2_0 + math.degrees(W2) * max(0.0, t - CLOCK0)


def first_crossing(theta_deg: float, a0: float = A1_0, period: float = P1, spin: int = SPIN1) -> float:
    """Seconds after the clock origin until a beam end at a0 first reaches theta (strictly > 0)."""
    d = ((theta_deg - a0) * spin) % 360.0
    if d < 1e-9:
        d = 360.0
    return d / 360.0 * period


# ---------------------------------------------------------------------------
# Easing
# ---------------------------------------------------------------------------

E_CHARGE = "cubic-bezier(.7,0,1,.6)"
E_IMPACT = "cubic-bezier(.05,.9,.2,1)"
E_SHOCK = "cubic-bezier(.33,1,.68,1)"
E_SPRING_FALLBACK = "cubic-bezier(.34,1.56,.64,1)"
E_KICK_FALLBACK = "cubic-bezier(.34,1.8,.5,1)"
E_SPRING_SOFT_FALLBACK = "cubic-bezier(.22,1.2,.36,1)"
E_SETTLE = "cubic-bezier(.16,1,.3,1)"
E_COOL = "cubic-bezier(.2,.8,.2,1)"
E_GLIDE = "cubic-bezier(.65,0,.35,1)"
E_POP = "cubic-bezier(.34,1.9,.64,1)"
E_COMMIT = "cubic-bezier(.5,0,.75,0)"
E_SNAP = "cubic-bezier(.2,0,0,1)"
E_SNAP_MS = 160
E_BREATHE = "cubic-bezier(.45,0,.55,1)"
STEPS_DECODE = "steps(3,end)"
STEPS_FRAME = "steps(1,end)"
CURSOR_BLINK = ("steps(2,start)", 1.06)


def bezier_xy(x1: float, y1: float, x2: float, y2: float, s: float) -> Tuple[float, float]:
    u = 1 - s
    return (3 * u * u * s * x1 + 3 * u * s * s * x2 + s ** 3, 3 * u * u * s * y1 + 3 * u * s * s * y2 + s ** 3)


def parse_bezier(css: str) -> Tuple[float, float, float, float]:
    a = [float(v) for v in css[css.index("(") + 1:css.index(")")].split(",")]
    return a[0], a[1], a[2], a[3]


def bezier_y(css: str, x: float) -> float:
    """Eased progress for time fraction x under a cubic-bezier() token."""
    x1, y1, x2, y2 = parse_bezier(css)
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if bezier_xy(x1, y1, x2, y2, mid)[0] < x:
            lo = mid
        else:
            hi = mid
    return bezier_xy(x1, y1, x2, y2, (lo + hi) / 2)[1]


def bezier_x_for_y(css: str, y: float) -> float:
    """Time fraction at which a cubic-bezier() token reaches progress y (monotone curves)."""
    x1, y1, x2, y2 = parse_bezier(css)
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if bezier_xy(x1, y1, x2, y2, mid)[1] < y:
            lo = mid
        else:
            hi = mid
    return bezier_xy(x1, y1, x2, y2, (lo + hi) / 2)[0]


def damping_for(overshoot: float) -> float:
    """Damping ratio z for a target overshoot OS: z = -ln(OS) / sqrt(pi^2 + ln(OS)^2)."""
    ln = math.log(overshoot)
    return -ln / math.sqrt(math.pi ** 2 + ln ** 2)


def spring_value(t: float, zeta: float, omega: float) -> float:
    wd = omega * math.sqrt(1 - zeta * zeta)
    return 1 - math.exp(-zeta * omega * t) * (math.cos(wd * t) + (zeta * omega / wd) * math.sin(wd * t))


def spring_points(overshoot: float, settle_ms: float, omega: float, n: int = 32) -> List[float]:
    z = damping_for(overshoot)
    pts = [round(spring_value(settle_ms / 1000.0 * i / (n - 1), z, omega), 3) for i in range(n)]
    pts[0] = 0.0
    pts[-1] = 1.0
    return pts


def _num(v: float) -> str:
    s = ("%.3f" % v).rstrip("0").rstrip(".")
    if s.startswith("0."):
        s = s[1:]
    elif s.startswith("-0."):
        s = "-" + s[2:]
    return s or "0"


def spring(overshoot: float = 0.12, settle_ms: float = 620, omega: float = 15.0, n: int = 32) -> str:
    """CSS linear() sampled from a damped oscillator (32 points, 3 decimals, last forced to 1)."""
    return "linear(%s)" % ",".join(_num(v) for v in spring_points(overshoot, settle_ms, omega, n))


E_SPRING = spring(0.12, 620, 15.0)
E_SPRING_SOFT = spring(0.03, 520, 11.0)
SPRING = {"overshoot": 0.12, "zeta": round(damping_for(0.12), 3), "omega": 15.0, "ms": 620}
SPRING_SOFT = {"overshoot": 0.03, "zeta": round(damping_for(0.03), 3), "omega": 11.0, "ms": 520}


def timing(name: str) -> str:
    """CSS declarations for a spring timing: the bezier first, linear() second (cascade fallback)."""
    fb, ln = {"spring": (E_SPRING_FALLBACK, E_SPRING), "soft": (E_SPRING_SOFT_FALLBACK, E_SPRING_SOFT),
              "kick": (E_KICK_FALLBACK, E_SPRING)}[name]
    return "animation-timing-function:%s;animation-timing-function:%s" % (fb, ln)


# Shake on .cam at the hero's ignition (S1 in hero.py), keyframe % -> px
SHAKE = ((0, 0, 0), (7, -6, 3), (16, 5, -4), (27, -4, 2), (40, 3, -1), (55, -1.5, 1), (72, .6, -.4), (100, 0, 0))
SHAKE_MS = 420
SHAKE_AMP = {"hero": 1.0, "social": 1.3, "thud": 0.33}

# Pilot overlay (read from the code)
ease_io = _OV.ease_io
ease_out = _OV.ease_out
ease_back = _OV.ease_back
glide_duration = _OV.glide_duration
RIPPLE = {"r0": 10, "r1": 58, "dur": 0.62, "stroke": 7.5, "inner_delay": 0.07, "inner_r0": 5, "inner_r1": 36,
          "flash_r": 12, "flash_until": 0.22}
CHIP_LABEL_MAX = 48


def ripple_radius(p: float) -> float:
    return RIPPLE["r0"] + RIPPLE["r1"] * ease_out(p)


# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------


def hexc(c: Sequence[float]) -> str:
    return "#%02X%02X%02X" % tuple(max(0, min(255, int(round(v)))) for v in c[:3])


EMBER, FLARE, PLASMA, BEAM = (hexc(c) for c in fx.SPECTRUM)
ICE, ASH, DUST, NIGHT = hexc(fx.ICE), hexc(fx.ASH), hexc(fx.DUST), hexc(fx.NIGHT)
OK, WARN, BAD = hexc(fx.OK), hexc(fx.WARN), hexc(fx.BAD)
SPECTRUM_STOPS = (0.0, 0.35, 0.7, 1.0)

DARK: Dict[str, object] = {
    "name": "dark",
    "bg": "#07060B", "nebula": ("#2A0F4A", "#150A2A"), "ink": ICE, "dim": ASH, "rule": DUST, "faint": "#2C2840",
    "shadow": "#2A1452", "shadow_op": .6,
    "spectrum": (EMBER, FLARE, PLASMA, BEAM),
    "hot": "#FFFFFF", "warm": "#FFE9F6",
    "core": ICE, "core_ring": None,
    "beam1": (ICE, BEAM, PLASMA), "beam2": (ICE, FLARE, PLASMA), "beam_blend": "screen", "beam_op": 1.0,
    "glint_blend": "screen", "glint_op": .55,
    "pen": ICE, "trace": BEAM,
    "route": {"adapter": BEAM, "native": PLASMA, "cloud": EMBER, "local": FLARE},
    "ok": OK, "warn": WARN, "bad": BAD,
    "chip": "#13101E", "chip_stroke": "#FFFFFF14",
    "flash": ICE, "flash_op": .24, "star": "#D8D4FF",
    "grid": DUST, "grid_op": .35, "panel": "#0D0B15",
}
LIGHT: Dict[str, object] = {
    "name": "light",
    "bg": "#FBFAF6", "nebula": None, "ink": "#1B1530", "dim": "#5D5878", "rule": "#CFC6E6", "faint": "#DCD3F2",
    "shadow": "#D8CCF6", "shadow_op": .9,
    "spectrum": ("#D94F00", "#D6156A", "#7A2FF0", "#0A8FB0"),
    "hot": "#1B1530", "warm": "#3A2A66",
    "core": "#1B1530", "core_ring": "#6B3FD1",
    "beam1": ("#0A8FB0", "#0A8FB0", "#7A2FF0"), "beam2": ("#D6156A", "#D6156A", "#7A2FF0"),
    "beam_blend": "multiply", "beam_op": .55,
    "glint_blend": "multiply", "glint_op": .45,
    "pen": "#6B3FD1", "trace": "#1B1530",
    "route": {"adapter": "#0A8FB0", "native": "#7A2FF0", "cloud": "#D94F00", "local": "#D6156A"},
    "ok": "#0E8A5A", "warn": "#9A6A00", "bad": "#C8203A",
    "chip": "#FFFFFF", "chip_stroke": "#DCD3F2",
    "flash": "#6B3FD1", "flash_op": .10, "star": "#8B7FD0",
    "grid": "#E9E3F7", "grid_major": "#DCD3F2", "grid_op": 1.0, "panel": "#FFFFFF",
}
THEMES = {"dark": DARK, "light": LIGHT}

# Beam geometry: 4 stacked wedges per beam end (half-width at the tip px, opacity) + a hot core line
BEAM_WEDGES = ((260, .10), (120, .18), (46, .35), (12, .90))
BEAM_CORE_LINE = 0.6


def _rgb(h: str) -> Tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def spectrum_at(t: float, theme: Optional[Dict[str, object]] = None) -> str:
    """Colour at position t in [0, 1] along the spectrum (stops at 0 / .35 / .7 / 1)."""
    stops = [_rgb(c) for c in (theme or DARK)["spectrum"]]  # type: ignore[index]
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    for i in range(len(SPECTRUM_STOPS) - 1):
        a, b = SPECTRUM_STOPS[i], SPECTRUM_STOPS[i + 1]
        if t <= b or i == len(SPECTRUM_STOPS) - 2:
            k = (t - a) / (b - a)
            return hexc([stops[i][j] + (stops[i + 1][j] - stops[i][j]) * k for j in range(3)])
    return hexc(stops[-1])


def mix(a: str, b: str, k: float) -> str:
    ra, rb = _rgb(a), _rgb(b)
    return hexc([ra[i] + (rb[i] - ra[i]) * k for i in range(3)])


def luminance(h: str) -> float:
    def ch(v: float) -> float:
        v = v / 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(h)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


# ---------------------------------------------------------------------------
# Type
# ---------------------------------------------------------------------------

MONO = 'ui-monospace,"SF Mono","Cascadia Mono",Menlo,Consolas,"DejaVu Sans Mono",monospace'
SANS = 'ui-sans-serif,system-ui,"Segoe UI",Roboto,sans-serif'
SIZES = {"caption": 13, "chip": 12, "readout": 15, "eyebrow": 11, "eyebrow_tracking": 2}
MONO_ADVANCE = 0.6                   # em: advance used to pin mono runs with textLength

# 5x7 pixel font (rows top to bottom, "1" = pixel)
PIXEL_FONT: Dict[str, Tuple[str, ...]] = {
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01110", "10001", "10000", "10000", "10000", "10001", "01110"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01110", "10001", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("01110", "00100", "00100", "00100", "00100", "00100", "01110"),
    "J": ("00111", "00010", "00010", "00010", "00010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "10001", "11001", "10101", "10011", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "10101", "01010"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11111", "00010", "00100", "00010", "00001", "10001", "01110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
    "/": ("00001", "00001", "00010", "00100", "01000", "10000", "10000"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    ".": ("00000", "00000", "00000", "00000", "00000", "01100", "01100"),
    "+": ("00000", "00100", "00100", "11111", "00100", "00100", "00000"),
    ":": ("00000", "01100", "01100", "00000", "01100", "01100", "00000"),
    " ": ("00000",) * 7,
}

# Decode noise patterns for CODE (S5 in hero.py): 5x7 bitmaps derived from fx.DECODE_GLYPHS shapes
_SHADES = {"█": 1.0, "▓": .75, "▒": .5, "░": .25}


def decode_patterns(seed: int = 3) -> List[Tuple[str, ...]]:
    """Three 5x7 noise bitmaps whose densities follow the shade glyphs of fx.DECODE_GLYPHS."""
    import random
    rng = random.Random(seed)
    dens = [_SHADES[g] for g in fx.DECODE_GLYPHS if g in _SHADES][1:4] or [.75, .5, .25]
    out = []
    for d in dens:
        out.append(tuple("".join("1" if rng.random() < d * .8 else "0" for _ in range(5)) for _ in range(7)))
    return out


# ---------------------------------------------------------------------------
# Wordmark
# ---------------------------------------------------------------------------

WORDMARK = fx.WORDMARK


def wordmark_cells() -> List[Tuple[int, int]]:
    """(col, row) of every full block in rows 0-4 of fx.WORDMARK (130 today)."""
    return [(x, y) for y, row in enumerate(WORDMARK[:5]) for x, ch in enumerate(row) if ch == "█"]


def wordmark_runs() -> List[Tuple[int, int, int]]:
    """(row, col0, length) horizontal runs of full blocks: run-length rects for small lockups."""
    out = []
    for y, row in enumerate(WORDMARK[:5]):
        x = 0
        while x < len(row):
            if row[x] != "█":
                x += 1
                continue
            e = x
            while e < len(row) and row[e] == "█":
                e += 1
            out.append((y, x, e - x))
            x = e
    return out


def wordmark_letters() -> List[List[Tuple[int, int]]]:
    """The cells of rows 0-4 grouped into letters (4-connected components, left to right): P U L S A R."""
    cells = set(wordmark_cells())
    seen: Dict[Tuple[int, int], int] = {}
    letters: List[List[Tuple[int, int]]] = []
    for c in sorted(cells):
        if c in seen:
            continue
        stack, comp = [c], []
        seen[c] = len(letters)
        while stack:
            x, y = stack.pop()
            comp.append((x, y))
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nb = (x + dx, y + dy)
                if nb in cells and nb not in seen:
                    seen[nb] = len(letters)
                    stack.append(nb)
        letters.append(sorted(comp, key=lambda q: (q[1], q[0])))
    return letters


WORDMARK_COLS = len(WORDMARK[0])
WORDMARK_ROWS = 5


# ---------------------------------------------------------------------------
# Providers (all computed)
# ---------------------------------------------------------------------------

GROUP_ORDER = ("local", "native", "cloud", "adapter")


def provider_group(p: _P.Provider) -> str:
    if p.local:
        return "local"
    if p.route == "native":
        return "native"
    if p.route == "cloud":
        return "cloud"
    return "adapter"


def provider_groups() -> Dict[str, List[_P.Provider]]:
    groups: Dict[str, List[_P.Provider]] = {g: [] for g in GROUP_ORDER}
    for p in advertised_providers():
        groups[provider_group(p)].append(p)
    assert sum(len(v) for v in groups.values()) == len(advertised_providers())
    return groups


def provider_count() -> int:
    return len(advertised_providers())


def version() -> str:
    from pulsar import __version__
    return __version__


def count_phrase(unit: str = "provider profiles") -> str:
    """Dated count wording for brand surfaces: "36 provider profiles in v3.0.0" (both values computed),
    the README's own wording. social.png still says "providers" because tests/test_screens_public.py
    (not owned by tools/brand) pins that word on the social card."""
    return "%d %s in v%s" % (provider_count(), unit, version())


def provider(pid: str) -> _P.Provider:
    p = _P.get(pid)
    assert p is not None, pid
    return p


def default_model(p: _P.Provider) -> str:
    return p.fallback_model or ""


def wire_label(p: _P.Provider) -> str:
    if p.route == "native":
        return "native wire"
    return "%s wire" % p.wire_for(default_model(p))


# The hero readout cycles these (provider id, key prefix or "" for keyless).
READOUT_IDS = (("openrouter", "sk-or-"), ("nvidia", "nvapi-"), ("groq", "gsk_"), ("anthropic", "sk-ant-api"),
               ("ollama", ""))


def readout_entries() -> List[Dict[str, str]]:
    """Generated readout lines; asserts every provider exists, is advertised and every prefix detects it."""
    prefixes = {pre: p.id for pre, p in _P._BY_PREFIX}
    advertised = {p.id for p in advertised_providers()}
    out = []
    for pid, pre in READOUT_IDS:
        p = _P.get(pid)
        assert p is not None, pid
        assert pid in advertised, pid
        if pre:
            assert prefixes.get(pre) == pid, (pre, pid)
            # longest prefix wins: the masked prefix must resolve to this provider
            assert _P._by_prefix(pre + "x" * 8) is p, pre
        else:
            # keyless: show the word the CLI accepts for this engine (detect() maps it, offline)
            pre_word = p.id
            det = _P.detect(pre_word)
            assert det.kind == "local" and det.candidates and det.candidates[0].id == pid, (pre_word, det.kind)
        route = "local" if p.local else p.route
        out.append({
            "id": p.id, "name": p.name, "prefix": pre or pre_word, "masked": (pre + BULLET * 8) if pre else pre_word,
            "keyless": "" if pre else "1",
            "route": route, "group": provider_group(p),
            "wire": "no key" if (p.local and not p.needs_key) else wire_label(p),
            "core": "Pulsar Core %s" % CORE_HOST,
        })
    return out


def readout_text(e: Dict[str, str]) -> str:
    return "%s  %s  %s %s %s %s %s  %s  %s" % (e["masked"], ARROW, e["name"], DOT, e["route"], DOT, e["wire"],
                                                 ARROW, e["core"])


def providers_json() -> Dict[str, object]:
    """docs/assets/brand/providers.json for the site's in-browser detector. No keys, no console URLs."""
    advertised = {p.id for p in advertised_providers()}
    def wire(p: _P.Provider) -> str:
        # native: Anthropic Messages forwarded as is; cloud: Claude Code's own integration
        return p.route if p.route in ("native", "cloud") else p.wire_for(default_model(p))

    prefixes = [[pre, p.id, p.name, "local" if p.local else p.route, wire(p)]
                for pre, p in _P._BY_PREFIX if p.id in advertised]
    unlisted = [pre for pre, p in _P._BY_PREFIX if p.id not in advertised]

    def first_sentence(note: str) -> str:
        cut = note.split(". ")[0].split(": ")[0]
        cut = cut.split("https://")[0].rstrip(" (")
        return cut.rstrip(".") + "."

    groups = provider_groups()
    return {
        "count": provider_count(),
        "prefixes": prefixes,
        "unlisted": unlisted,
        "reject": [[pre, first_sentence(note)] for pre, note in _P._REJECT],
        "ambiguous": ["sk-"],
        "groups": {g: [p.id for p in groups[g]] for g in GROUP_ORDER},
        "names": {p.id: p.name for p in advertised_providers()},
        "core": CORE_HOST,
    }


# ---------------------------------------------------------------------------
# Token dump
# ---------------------------------------------------------------------------


def tokens() -> Dict[str, object]:
    groups = provider_groups()
    return {
        "generated_by": "tools/brand/motion.py",
        "tagline": TAGLINE,
        "clock": {"w1": W1, "w2": W2, "a1_0_deg": round(A1_0, 4), "a2_0_deg": round(A2_0, 4),
                  "P1": round(P1, 4), "P2": round(P2, 4), "H": round(H, 4), "Q": round(Q, 4), "L": round(L, 4),
                  "frame": round(F, 4), "ignition": IGNITION, "clock0": CLOCK0,
                  "beats_ms": list(BEATS_MS), "stagger_ms": STAGGER_MS, "shadow_lag": SHADOW_LAG},
        "easing": {"E_CHARGE": E_CHARGE, "E_IMPACT": E_IMPACT, "E_SHOCK": E_SHOCK,
                   "E_SPRING": E_SPRING, "E_SPRING_FALLBACK": E_SPRING_FALLBACK,
                   "E_SPRING_SOFT": E_SPRING_SOFT, "E_SPRING_SOFT_FALLBACK": E_SPRING_SOFT_FALLBACK,
                   "E_KICK_FALLBACK": E_KICK_FALLBACK, "E_SETTLE": E_SETTLE, "E_COOL": E_COOL,
                   "E_GLIDE": E_GLIDE, "E_POP": E_POP, "E_COMMIT": E_COMMIT, "E_SNAP": E_SNAP,
                   "E_SNAP_MS": E_SNAP_MS, "E_BREATHE": E_BREATHE, "STEPS_DECODE": STEPS_DECODE,
                   "STEPS_FRAME": STEPS_FRAME, "CURSOR_BLINK": list(CURSOR_BLINK)},
        "spring": SPRING, "spring_soft": SPRING_SOFT,
        "shake": {"keys": [list(k) for k in SHAKE], "ms": SHAKE_MS, "amp": SHAKE_AMP},
        "pilot": {"glide": "0.25 + 0.2*min(1, d/900) s", "ripple": RIPPLE, "chip_label_max": CHIP_LABEL_MAX},
        "colors": {"dark": _jsonable(DARK), "light": _jsonable(LIGHT)},
        "spectrum_stops": list(SPECTRUM_STOPS),
        "beam": {"wedges": [list(w) for w in BEAM_WEDGES], "core_line": BEAM_CORE_LINE},
        "type": {"mono": MONO, "sans": SANS, "sizes": SIZES},
        "pixel_font": {k: list(v) for k, v in PIXEL_FONT.items()},
        "providers": {"count": provider_count(), "groups": {g: len(groups[g]) for g in GROUP_ORDER}},
        "readout": readout_entries(),
    }


def _jsonable(d: Dict[str, object]) -> Dict[str, object]:
    return {k: (list(v) if isinstance(v, tuple) else v) for k, v in d.items()}


def tokens_json() -> str:
    return json.dumps(tokens(), indent=1, ensure_ascii=False, sort_keys=False) + "\n"


def write_tokens(path: Path = TOKENS_PATH) -> Path:
    path.write_text(tokens_json(), encoding="utf-8")
    return path


if __name__ == "__main__":
    if "--write" in sys.argv[1:]:
        print(write_tokens())
    else:
        sys.stdout.write(tokens_json())
