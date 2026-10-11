# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Font-independent animated terminal SVG for README embeds ("terminal replays").

GitHub renders a repository SVG inside <img> under a strict CSP: CSS
animations work, scripts and external fonts do not, and the viewer's own
monospace font decides glyph widths. So:

  * block, quadrant, shade, triangle and box-drawing characters are drawn
    as geometry (merged into one <path> per colour per row), never as font
    glyphs, so the wordmark and panels have no seams on any machine;
  * every multi-cell text run gets textLength = cells * cell width, so a
    narrower or wider fallback font still lands on the grid;
  * identical rows are stored once (<g> in <defs>) and frames toggle with
    steps(1,end) visibility keyframes (proven to animate inside <img>).

Everything else wraps or overlays that frame engine and never edits recorded
cell content: brand chrome with a REPLAY chip, a DEMO
chip and a timecode; a camera track (translate + scale on one group, inside
the screen clip); one cursor rect placed from pyte's cursor; an Enter thud
(shake on the world, never on the frame, plus a ring at the cursor); geometry
glow under bright block glyphs; captions in a letterbox below the screen;
a spectrum progress hairline; a 300 ms dip to the background before the loop.
Reduced motion shows the last frame at camera 1.0 with no animation at all.

Timing tokens come from tools/brand/motion.py (the shared brand clock) when it
imports, else from the identical values vendored below.
"""

from __future__ import annotations

import gzip
import hashlib
import html
import json
import math
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

CW, CH, FS = 8.5, 17.0, 14.0                  # cell width (0.5 px quantum), cell height, font size
BASE = 13.0                                   # text baseline inside a cell
BAR = 34.0                                    # chrome bar
PADX, PADY = 16.0, 12.0                       # screen padding around the cell grid
MARGIN = 16.0                                 # canvas margin around the window (room for the shadow)
LETTERBOX = 40.0                              # caption band under the screen (only when a scene has captions)
DIP = 0.3                                     # s: dip to the background before the loop restarts
MAX_ZOOM = 1.8

Run = Tuple[int, str, str, bool]              # x, text, colour, bold
Frame = List[List[Run]]

# ---------------------------------------------------------------------------
# brand tokens (tools/brand/motion.py when present)
# ---------------------------------------------------------------------------


def _load_motion():
    try:
        here = Path(__file__).resolve().parents[1] / "brand"
        if str(here) not in sys.path:
            sys.path.insert(0, str(here))
        import motion  # type: ignore
        return motion
    except Exception:                         # noqa: BLE001 - brand module missing or mid-edit: vendored values
        return None


_M = _load_motion()


def _damping(os_: float) -> float:
    ln = math.log(os_)
    return -ln / math.sqrt(math.pi ** 2 + ln ** 2)


def spring(overshoot: float, settle_ms: float, omega: float, n: int = 32) -> str:
    """CSS linear() sampled from a damped oscillator, like motion.spring: 32 points, 3 decimals, last forced to 1."""
    z = _damping(overshoot)
    wd = omega * math.sqrt(1 - z * z)
    pts = []
    for i in range(n):
        t = settle_ms / 1000.0 * i / (n - 1)
        x = 1 - math.exp(-z * omega * t) * (math.cos(wd * t) + (z * omega / wd) * math.sin(wd * t))
        pts.append(x)
    pts[-1] = 1.0

    def num(v: float) -> str:
        s = "%.3f" % v
        s = s.rstrip("0").rstrip(".") if "." in s else s
        return s.replace("0.", ".", 1) if s.startswith("0.") else ("-" + s[2:] if s.startswith("-0.") else s)
    return "linear(%s)" % ",".join(num(p) for p in pts)


def _tok(name: str, default):
    return getattr(_M, name, default) if _M is not None else default


E_SHOCK = _tok("E_SHOCK", "cubic-bezier(.33,1,.68,1)")
E_IMPACT = _tok("E_IMPACT", "cubic-bezier(.05,.9,.2,1)")
E_SETTLE = _tok("E_SETTLE", "cubic-bezier(.16,1,.3,1)")
E_GLIDE = _tok("E_GLIDE", "cubic-bezier(.65,0,.35,1)")
E_SPRING_SOFT_FALLBACK = _tok("E_SPRING_SOFT_FALLBACK", "cubic-bezier(.22,1.2,.36,1)")
E_SPRING_SOFT = _tok("E_SPRING_SOFT", spring(0.03, 520, 11.0))
SHAKE = _tok("SHAKE", ((0, 0, 0), (7, -6, 3), (16, 5, -4), (27, -4, 2), (40, 3, -1), (55, -1.5, 1),
                       (72, .6, -.4), (100, 0, 0)))
SHAKE_MS = float(_tok("SHAKE_MS", 420))
THUD_AMP = float((_tok("SHAKE_AMP", {}) or {}).get("thud", 0.33))
RING_MS = 240.0                               # the Enter ring: gone before the CLI repaints the prompt
MIN_FILL = 0.4                                # a zoomed hold frames at least this share of inked rows

ICE, ASH, DUST, NIGHT = "#ECF6FF", "#9692AC", "#58546C", "#2C283C"
EMBER, FLARE, PLASMA, BEAM = "#FF6A00", "#FF2E88", "#9D4DFF", "#36E2FF"
WARN, BAD = "#FFC400", "#FF465A"
SCREEN_BG = "#0a0910"                         # record.DEFAULT_BG: the recorded terminal background
BAR_BG = "#0F0D18"
CHIP_BG, CHIP_STROKE = "#13101E", "#FFFFFF14"
MONO = "ui-monospace,'SF Mono','Cascadia Mono','JetBrains Mono',Menlo,Consolas,'DejaVu Sans Mono','Liberation Mono',monospace"
SANS = "ui-sans-serif,system-ui,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif"

EASES = {"GLIDE": E_GLIDE, "SETTLE": E_SETTLE, "LINEAR": "linear"}
ERROR_WORDS = re.compile(r"error|Error|ERROR|refused|failed")

# box drawing: (up, right, down, left) weights: 1 light, 2 heavy, 3 double
BOX = {"─": (0, 1, 0, 1), "━": (0, 2, 0, 2), "│": (1, 0, 1, 0), "┃": (2, 0, 2, 0), "┌": (0, 1, 1, 0),
       "┐": (0, 0, 1, 1), "└": (1, 1, 0, 0), "┘": (1, 0, 0, 1), "├": (1, 1, 1, 0), "┤": (1, 0, 1, 1),
       "┬": (0, 1, 1, 1), "┴": (1, 1, 0, 1), "┼": (1, 1, 1, 1), "═": (0, 3, 0, 3), "║": (3, 0, 3, 0),
       "╔": (0, 3, 3, 0), "╗": (0, 0, 3, 3), "╚": (3, 3, 0, 0), "╝": (3, 0, 0, 3), "╠": (3, 3, 3, 0),
       "╣": (3, 0, 3, 3), "╦": (0, 3, 3, 3), "╩": (3, 3, 0, 3), "╬": (3, 3, 3, 3), "╭": (0, 1, 1, 0),
       "╮": (0, 0, 1, 1), "╯": (1, 0, 0, 1), "╰": (1, 1, 0, 0), "┏": (0, 2, 2, 0), "┓": (0, 0, 2, 2),
       "┗": (2, 2, 0, 0), "┛": (2, 0, 0, 2)}
QUAD = {"▘": "1000", "▝": "0100", "▖": "0010", "▗": "0001", "▚": "1001", "▞": "0110", "▙": "1011",
        "▟": "0111", "▛": "1110", "▜": "1101", "▀": "1100", "▄": "0011", "▌": "1010", "▐": "0101", "█": "1111"}
SHADE = {"░": 1, "▒": 2, "▓": 3}               # opacity classes o1..o3 (.25 .5 .75)
TRI = {"◢": ((1, 0), (1, 1), (0, 1)), "◣": ((0, 0), (1, 1), (0, 1)), "◤": ((0, 0), (1, 0), (0, 1)),
       "◥": ((0, 0), (1, 0), (1, 1))}
DIAG = {"╱": [((0, 1), (1, 0))], "╲": [((0, 0), (1, 1))], "╳": [((0, 1), (1, 0)), ((0, 0), (1, 1))]}
WIDTHS = {"1": "w1", "1.2": "w2", "2.2": "w3"}


def _n(v: float) -> str:
    s = "%.1f" % v
    if s.endswith(".0"):
        s = s[:-2]
    if s.startswith("0."):
        s = s[1:]
    elif s.startswith("-0."):
        s = "-" + s[2:]
    return "0" if s in ("-0", "") else s


def _pc(t: float, total: float) -> str:
    v = max(0.0, min(100.0, 100.0 * t / total))
    s = "%.3f" % v
    s = s.rstrip("0").rstrip(".")
    return (s or "0") + "%"


def _bright(col: str) -> bool:
    try:
        r, g, b = (int(col[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
    except ValueError:
        return False
    v, m = max(r, g, b), min(r, g, b)
    s = (v - m) / v if v else 0.0
    return v >= 0.7 and (s >= 0.35 or v >= 0.92)


COLOUR_STEP = 8                               # colour precision: 32 levels per channel (max error 4/255)


def quantise_colour(col: str, step: int = 0) -> str:
    """Snap a #rrggbb colour to the renderer's precision. Gradient rows (the boot beams) carry a
    distinct colour per cell; at 4/255 the difference is invisible and neighbours merge into one path."""
    step = step or COLOUR_STEP
    col = col.lower()
    try:
        ch = [int(col[i:i + 2], 16) for i in (1, 3, 5)]
    except ValueError:
        return col
    if step <= 1:
        return col
    return "#%02x%02x%02x" % tuple(min(255, int(round(c / float(step))) * step) for c in ch)


class Palette:
    """Paint for row geometry and text. Rows are built with colour tokens; once every row is known,
    colours used often become classes (.c0 fill, .s0 stroke) and rare ones are written inline, which
    is smaller for gradient rows where every cell carries its own colour."""

    TOKEN = re.compile("\x01([FSG])([0-9a-f]{6})\\|([^\x02]*)\x02")
    MIN_USES = 5

    def __init__(self) -> None:
        self.ids: Dict[str, int] = {}
        self.uses: Dict[str, int] = {}
        self.used: set = set()

    @staticmethod
    def token(kind: str, col: str, extra: str = "") -> str:
        return "\x01%s%s|%s\x02" % (kind, quantise_colour(col)[1:], extra)

    def count(self, text: str) -> None:
        for m in self.TOKEN.finditer(text):
            self.uses[m.group(2)] = self.uses.get(m.group(2), 0) + 1

    def resolve(self, text: str) -> str:
        def sub(m) -> str:
            kind, col, extra = m.group(1), m.group(2), m.group(3)
            if self.uses.get(col, 0) >= self.MIN_USES:
                if col not in self.ids:
                    self.ids[col] = len(self.ids)
                i = self.ids[col]
                cls = {"F": ["c%d" % i], "S": ["s%d" % i], "G": ["c%d" % i, "s%d" % i]}[kind]
                self.used.update((c[0], i) for c in cls)
                return ' class="%s"' % " ".join(cls + ([extra] if extra else []))
            hexc = _short_hex(col)
            if kind == "G":           # one colour for fill and glow stroke: .h paints both with it
                return ' color="%s" class="h%s"' % (hexc, " " + extra if extra else "")
            paint = {"F": ' fill="%s"' % hexc, "S": ' stroke="%s"' % hexc}[kind]
            return paint + (' class="%s"' % extra if extra else "")
        return self.TOKEN.sub(sub, text)

    def css(self) -> str:
        out = []
        for col, i in self.ids.items():
            if ("c", i) in self.used:
                out.append(".c%d{fill:%s}" % (i, _short_hex(col)))
            if ("s", i) in self.used:
                out.append(".s%d{stroke:%s}" % (i, _short_hex(col)))
        return "".join(out)


def _short_hex(col: str) -> str:
    col = col.lower().lstrip("#")
    if col[0] == col[1] and col[2] == col[3] and col[4] == col[5]:
        return "#" + col[0] + col[2] + col[4]
    return "#" + col


class _Row:
    """Collects one row's geometry, merged per colour, plus its text runs (colours as Palette tokens)."""

    def __init__(self, glow: bool = False) -> None:
        self.allow_glow = glow
        self.fills: Dict[str, List[str]] = {}
        self.glow: set = set()
        self.strokes: Dict[Tuple[str, str], List[str]] = {}
        self.extra: List[str] = []
        self.texts: List[str] = []
        self.quads: Dict[str, set] = {}       # colour -> {(half-column, half-row)} of block geometry

    def quad(self, hx: int, hy: int, col: str, glow: bool = True) -> None:
        """One quarter cell of block-glyph geometry (half-column hx, half-row hy). Quarters are merged
        into as few rectangles as possible when the row is written: a wordmark or a beam is a handful
        of runs instead of four rects per cell, so more frames fit the size budget."""
        c = quantise_colour(col)
        self.quads.setdefault(c, set()).add((hx, hy))
        if glow and self.allow_glow and _bright(c):
            self.glow.add(c)

    def _merge_quads(self) -> None:
        hw, hh = CW / 2, CH / 2
        for c in sorted(self.quads):
            cells = self.quads[c]
            spans = {0: [], 1: []}
            for hy in (0, 1):
                xs = sorted(hx for hx, y in cells if y == hy)
                for hx in xs:
                    if spans[hy] and spans[hy][-1][1] == hx - 1:
                        spans[hy][-1][1] = hx
                    else:
                        spans[hy].append([hx, hx])
            both = set(map(tuple, spans[0])) & set(map(tuple, spans[1]))
            out = [(a, b, 0, 2) for a, b in sorted(both)]
            out += [(a, b, hy, 1) for hy in (0, 1) for a, b in spans[hy] if (a, b) not in both]
            for a, b, hy, n in sorted(out):
                w, h = (b - a + 1) * hw + .3, n * hh + .3
                d = "M%s %sh%sv%sh-%sz" % (_n(a * hw), _n(hy * hh), _n(w), _n(h), _n(w))
                self.fills.setdefault(c, []).append(d)
        self.quads = {}

    def rect(self, x: float, y: float, w: float, h: float, col: str, glow: bool = True, step: int = 0) -> None:
        c = quantise_colour(col, step)
        d = "M%s %sh%sv%sh-%sz" % (_n(x), _n(y), _n(w), _n(h), _n(w))
        self.fills.setdefault(c, []).append(d)
        if glow and self.allow_glow and _bright(c):
            self.glow.add(c)

    def line(self, x1: float, y1: float, x2: float, y2: float, col: str, width: str) -> None:
        c = quantise_colour(col)
        if y1 == y2:
            d = "M%s %sH%s" % (_n(x1), _n(y1), _n(x2))
        elif x1 == x2:
            d = "M%s %sV%s" % (_n(x1), _n(y1), _n(y2))
        else:
            d = "M%s %sL%s %s" % (_n(x1), _n(y1), _n(x2), _n(y2))
        self.strokes.setdefault((c, width), []).append(d)

    def text(self, x: float, s: str, col: str, bold: bool) -> None:
        tl = ' textLength="%s"' % _n(len(s) * CW) if len(s) > 1 else ""
        self.texts.append('<text x="%s"%s%s>%s</text>'
                          % (_n(x), Palette.token("F", col, "b" if bold else ""), tl, html.escape(s, quote=False)))

    def svg(self) -> str:
        self._merge_quads()
        # geometry glow: the same path stroked underneath its fill (paint-order), 1.6 px at .3
        out = ['<path%s d="%s"/>' % (Palette.token("G", c, "g") if c in self.glow else Palette.token("F", c),
                                     "".join(v)) for c, v in self.fills.items()]
        out += ['<path%s d="%s"/>' % (Palette.token("S", c, WIDTHS.get(w, "w1")), "".join(v))
                for (c, w), v in self.strokes.items()]
        out += self.extra
        if self.texts:
            out.append('<g transform="translate(0 %s)">%s</g>' % (_n(BASE), "".join(self.texts)))
        return "".join(out)


DOT_COLOUR_STEP = 32                          # dot paint precision (max error 16/255 on a 2 px square)
DOTS = {"·": 1.6, "∙": 2.2}                   # middle dots as tiny squares (glyph rain, beam trails)


def _shape(row: _Row, ch: str, x: int, col: str) -> bool:
    X, w, h = x * CW, CW, CH
    if ch in DOTS:
        d = DOTS[ch]
        # a 2 px dot shows no colour detail: coarse paint lets a row's dots share a few paths
        row.rect(X + (w - d) / 2, h / 2 - d / 2 - .5, d, d, col, glow=False, step=DOT_COLOUR_STEP)
        return True
    if ch in QUAD:
        q = QUAD[ch]
        for i, (dx, dy) in enumerate(((0, 0), (1, 0), (0, 1), (1, 1))):
            if q[i] == "1":
                row.quad(2 * x + dx, dy, col)
        return True
    if ch in SHADE:
        row.extra.append('<path%s d="M%s 0h%sv%sh-%sz"/>'
                         % (Palette.token("F", col, "o%d" % SHADE[ch]), _n(X), _n(w + .3), _n(h + .3), _n(w + .3)))
        return True
    if ch in TRI:
        pts = " ".join("%s,%s" % (_n(X + a * w), _n(b * h)) for a, b in TRI[ch])
        row.extra.append('<polygon%s points="%s"/>' % (Palette.token("F", col), pts))
        return True
    if ch in DIAG:
        for a, b in DIAG[ch]:
            row.line(X + a[0] * w, a[1] * h, X + b[0] * w, b[1] * h, col, "1.2")
        return True
    if ch in BOX:
        up, right, down, left = BOX[ch]
        cx, cy = X + w / 2, h / 2

        def seg(weight: int, x1: float, y1: float, x2: float, y2: float, vertical: bool) -> None:
            if not weight:
                return
            if weight == 3:
                for o in (-1.6, 1.6):
                    if vertical:
                        row.line(x1 + o, y1, x2 + o, y2, col, "1")
                    else:
                        row.line(x1, y1 + o, x2, y2 + o, col, "1")
            else:
                row.line(x1, y1, x2, y2, col, "1" if weight == 1 else "2.2")
        if up and up == down:
            seg(up, cx, 0, cx, h, True)
        else:
            seg(up, cx, 0, cx, cy + .5, True)
            seg(down, cx, cy - .5, cx, h, True)
        if left and left == right:
            seg(left, X, cy, X + w, cy, False)
        else:
            seg(left, X, cy, cx + .5, cy, False)
            seg(right, cx - .5, cy, X + w, cy, False)
        return True
    return False


def row_svg(runs: Sequence[Run], glow: bool = False) -> str:
    """One row's SVG with colour tokens (Palette.resolve turns them into classes or inline paint).
    glow: a 1.6 px stroke at .3 under bright block-glyph geometry (the boot wordmark and beams)."""
    row = _Row(glow)
    for x, text, col, bold in runs:
        buf, bx = "", x

        def flush() -> None:
            nonlocal buf
            if buf.strip():
                lead = len(buf) - len(buf.lstrip(" "))
                body = buf.strip(" ")
                row.text((bx + lead) * CW, body, col, bold)
            buf = ""
        i = 0
        while i < len(text):
            ch = text[i]
            if ch in "━─═█" and i + 1 < len(text) and text[i + 1] == ch:
                j = i
                while j < len(text) and text[j] == ch:
                    j += 1
                flush()
                x1, x2, cy = (x + i) * CW, (x + j) * CW, CH / 2
                if ch == "█":
                    for hx in range(2 * (x + i), 2 * (x + j)):
                        row.quad(hx, 0, col)
                        row.quad(hx, 1, col)
                elif ch == "═":
                    row.line(x1, cy - 1.6, x2, cy - 1.6, col, "1")
                    row.line(x1, cy + 1.6, x2, cy + 1.6, col, "1")
                else:
                    row.line(x1, cy, x2, cy, col, "2.2" if ch == "━" else "1")
                i = j
                bx = x + j
                continue
            if _shape(row, ch, x + i, col):
                flush()
                bx = x + i + 1
            else:
                if not buf:
                    bx = x + i
                buf += ch
            i += 1
        flush()
    return row.svg()


# ---------------------------------------------------------------------------
# frames
# ---------------------------------------------------------------------------

def limit_frames(frames: List, max_frames: int, keep: Sequence[int] = ()) -> List:
    """Keep at most max_frames, always the first and the last, evenly spread in time. Indexes in keep
    (frames a caption or camera key is anchored on) survive too: each replaces the nearest spread pick."""
    if len(frames) <= max_frames or max_frames < 2:
        return frames
    step = (len(frames) - 1) / (max_frames - 1)
    picks = {int(round(i * step)) for i in range(max_frames)}
    fixed = {0, len(frames) - 1}
    for k in sorted(set(i for i in keep if 0 <= i < len(frames))):
        if k in picks:
            fixed.add(k)
            continue
        free = [p for p in picks if p not in fixed]
        if free:
            picks.discard(min(free, key=lambda p: (abs(p - k), p)))
        picks.add(k)
        fixed.add(k)
    return [frames[i] for i in sorted(picks)]


# Claude Code's welcome banner draws Anthropic's block-art mascot ("Clawd") from quadrant glyphs:
#      ▐▛███▜▌        no replay, poster or still may show it (scenes crop the banner away, and
#     ▝▜█████▛▘       record_scene.py fails a recording that still shows it). These glyph pairs and
#       ▘▘ ▝▝         eye rows occur in every pose of the mascot and in none of Pulsar's own art.
CLI_MASCOT = re.compile("▐▛|▐█|▛█|█▛|█▜|▜▌|▝▜|▝█|▛▘|▘▘|▝▝|▟███▟")


def mascot_lines(text: str) -> List[str]:
    """The lines of a frame's text that show a piece of Claude Code's mascot."""
    return [line for line in text.split("\n") if CLI_MASCOT.search(line)]


def frame_text(rows: Frame) -> str:
    """A recorded frame as text, runs placed at their columns (spaces between them survive)."""
    lines = []
    for row in rows:
        line = ""
        for x, text, _c, _b in row:
            line = line.ljust(x) + text
        lines.append(line)
    return "\n".join(lines)


def _cur(f) -> Optional[Tuple[int, int]]:
    return tuple(f[2]) if len(f) > 2 and f[2] is not None else None


def _b36(i: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    s = ""
    while True:
        i, r = divmod(i, 36)
        s = digits[r] + s
        if not i:
            return s


# ---------------------------------------------------------------------------
# easing evaluation (for validators: where is the camera at time t)
# ---------------------------------------------------------------------------

def _bezier(spec: str):
    m = re.match(r"cubic-bezier\(([^)]*)\)", spec or "")
    if not m:
        return lambda p: p
    x1, y1, x2, y2 = (float(v) for v in m.group(1).split(","))

    def bx(t: float, a: float, b: float) -> float:
        return 3 * a * t * (1 - t) ** 2 + 3 * b * t * t * (1 - t) + t ** 3

    def f(p: float) -> float:
        if p <= 0 or p >= 1:
            return min(1.0, max(0.0, p))
        lo, hi = 0.0, 1.0
        for _ in range(40):
            mid = (lo + hi) / 2
            if bx(mid, x1, x2) < p:
                lo = mid
            else:
                hi = mid
        return bx((lo + hi) / 2, y1, y2)
    return f


# ---------------------------------------------------------------------------
# geometry of the replay window
# ---------------------------------------------------------------------------

class Layout:
    def __init__(self, cols: int, rows: int, captions: bool) -> None:
        self.cols, self.rows = cols, rows
        self.win_x = self.win_y = MARGIN
        self.win_w = cols * CW + 2 * PADX
        self.scr_x, self.scr_y = self.win_x, self.win_y + BAR
        self.scr_w, self.scr_h = self.win_w, rows * CH + 2 * PADY
        self.box = LETTERBOX if captions else 0.0
        self.win_h = BAR + self.scr_h + self.box
        self.W = int(math.ceil(self.win_w + 2 * MARGIN))
        self.H = int(math.ceil(self.win_h + 2 * MARGIN + 6))
        self.ox, self.oy = self.scr_x + PADX, self.scr_y + PADY     # cell (0, 0)

    def cell(self, col: float, row: float) -> Tuple[float, float]:
        return self.ox + col * CW, self.oy + row * CH

    def cam(self, zoom: float, col: float, row: float) -> Tuple[float, float, float]:
        """(tx, ty, zoom) for transform: translate(tx, ty) scale(zoom), origin at the canvas origin,
        centring cell (col, row) and clamped so the zoomed screen always covers the screen rect."""
        z = max(1.0, min(MAX_ZOOM, float(zoom)))
        cx, cy = self.cell(col + .5, row + .5)
        sx, sy, sw, sh = self.scr_x, self.scr_y, self.scr_w, self.scr_h
        tx = sx + sw / 2 - z * cx
        ty = sy + sh / 2 - z * cy
        tx = min(sx - z * sx, max(sx + sw - z * (sx + sw), tx))
        ty = min(sy - z * sy, max(sy + sh - z * (sy + sh), ty))
        return tx, ty, z

    def viewport(self, tx: float, ty: float, z: float) -> Tuple[float, float, float, float]:
        """The canvas rect (x0, y0, x1, y1) visible in the screen under that camera."""
        sx, sy, sw, sh = self.scr_x, self.scr_y, self.scr_w, self.scr_h
        return (sx - tx) / z, (sy - ty) / z, (sx + sw - tx) / z, (sy + sh - ty) / z


def _rrect(x: float, y: float, w: float, h: float, tl: float, tr: float, br: float, bl: float) -> str:
    return ("M%s %sh%sa%s %s 0 0 1 %s %sv%sa%s %s 0 0 1 -%s %sh-%sa%s %s 0 0 1 -%s -%sv-%sa%s %s 0 0 1 %s -%sz"
            % (_n(x + tl), _n(y), _n(w - tl - tr), _n(tr), _n(tr), _n(tr), _n(tr), _n(h - tr - br), _n(br), _n(br),
               _n(br), _n(br), _n(w - br - bl), _n(bl), _n(bl), _n(bl), _n(bl), _n(h - bl - tl), _n(tl), _n(tl),
               _n(tl), _n(tl)))


# ---------------------------------------------------------------------------
# tracks: camera, captions, cursor, thud
# ---------------------------------------------------------------------------

LINE_GLYPHS = set(BOX) | set(DOTS) | set("━─═╸╺╴╶│┃")


def _wordy(ch: str) -> bool:
    """A cell the camera must not slice: a letter, digit, symbol or block glyph (rules and dots may be cut)."""
    return bool(ch.strip()) and ch not in LINE_GLYPHS


def _ink(frames: List, cols: int, rows: int) -> List[List[int]]:
    """Union over frames of what each cell holds: 2 a wordy glyph, 1 a rule or box line, 0 nothing."""
    grid = [[0] * cols for _ in range(rows)]
    for f in frames:
        for y, line in enumerate(frame_text(f[1]).split("\n")[:rows]):
            for x, ch in enumerate(line[:cols]):
                if grid[y][x] < 2 and ch.strip() and ch not in DOTS:
                    grid[y][x] = 2 if _wordy(ch) else 1
            for x, ch in enumerate(line[:cols]):     # a separator dot inside a phrase ("Pulsar · kimi") binds it
                if ch in "·•" and any(_wordy(c) for c in line[max(0, x - 2):x]) \
                        and any(_wordy(c) for c in line[x + 1:x + 3]):
                    grid[y][x] = 2
    return grid


def edge_cuts(lay: "Layout", tx: float, ty: float, z: float, ink: List[List[int]], level: int = 2) -> int:
    """How many cells of at least ink level (2 words, 1 also rules and box lines) the zoomed viewport's
    edges slice. A horizontal edge slices a cell it passes through, or falls inside a phrase (between
    two cells, or in a single space between words: two spaces separate columns, one separates words
    of a phrase); a vertical edge slices a row whose glyphs it passes through (an edge
    on a row boundary cuts nothing)."""
    vx0, vy0, vx1, vy1 = lay.viewport(tx, ty, z)
    cx0, cx1 = (vx0 - lay.ox) / CW, (vx1 - lay.ox) / CW
    cy0, cy1 = (vy0 - lay.oy) / CH, (vy1 - lay.oy) / CH
    rows, cols = len(ink), len(ink[0]) if ink else 0
    eps = .06

    def at(x: int, y: int) -> bool:
        return 0 <= y < rows and 0 <= x < cols and ink[y][x] >= level

    vis_rows = range(max(0, int(math.floor(cy0 + eps))), min(rows, int(math.ceil(cy1 - eps))))
    vis_cols = range(max(0, int(math.floor(cx0 + eps))), min(cols, int(math.ceil(cx1 - eps))))
    cuts = 0
    for cx in (cx0, cx1):
        k = round(cx)
        for y in vis_rows:
            if abs(cx - k) < eps:             # on a cell boundary: a phrase must not continue across it
                a, b = k - 1, k
                cut = (at(a, y) and at(b, y)) or (at(a, y) and at(b + 1, y)) or (at(a - 1, y) and at(b, y))
            else:                             # inside a cell: it must be blank, and not a one-space word gap
                c = int(math.floor(cx))
                cut = at(c, y) or (at(c - 1, y) and at(c + 1, y))
            cuts += cut
    for cy in (cy0, cy1):
        if abs(cy - round(cy)) >= eps:
            r = int(math.floor(cy))
            cuts += sum(1 for x in vis_cols if at(x, r))
    return cuts


def row_fill(lay: "Layout", tx: float, ty: float, z: float, ink: List[List[int]]) -> float:
    """The share of the rows a camera frames (rows at least half inside the viewport) that hold anything."""
    vx0, vy0, vx1, vy1 = lay.viewport(tx, ty, z)
    r0, r1 = (vy0 - lay.oy) / CH, (vy1 - lay.oy) / CH
    c0, c1 = (vx0 - lay.ox) / CW, (vx1 - lay.ox) / CW
    rows = [y for y in range(len(ink)) if r0 <= y + .5 <= r1]
    cols = [x for x in range(len(ink[0]) if ink else 0) if c0 <= x + .5 <= c1]
    if not rows:
        return 0.0
    return sum(1 for y in rows if any(ink[y][x] for x in cols)) / float(len(rows))


def edge_touches(lay: "Layout", tx: float, ty: float, z: float, ink: List[List[int]]) -> int:
    """Rows whose text runs right up to a vertical viewport edge (the cell just inside it holds a word
    glyph): legal, but it reads as cropped, so the snap prefers a gutter of at least one cell."""
    vx0, vy0, vx1, vy1 = lay.viewport(tx, ty, z)
    cx0, cx1 = (vx0 - lay.ox) / CW, (vx1 - lay.ox) / CW
    cy0, cy1 = (vy0 - lay.oy) / CH, (vy1 - lay.oy) / CH
    rows, cols = len(ink), len(ink[0]) if ink else 0
    out = 0
    for y in range(max(0, int(math.floor(cy0 + .06))), min(rows, int(math.ceil(cy1 - .06)))):
        for x in (int(math.ceil(cx0 - .06)), int(math.floor(cx1 + .06)) - 1):
            if 0 < x < cols - 1 and ink[y][x] >= 2:
                out += 1
    return out


def snap_camera(lay: "Layout", zoom: float, col: float, row: float, ink: List[List[bool]]):
    """(tx, ty, z, cuts): the framing nearest to (zoom, col, row) whose edges slice no word. Searches the
    zoom down to 92 % of the asked one (never below 1) and the centre on a grid of half columns
    (+-6) and quarter rows (+-3), plus every centre that puts a viewport edge exactly on a cell
    boundary (a phrase spanning the whole width, or a row right under a rule, fits only that way). The
    focus cell stays at least one cell inside the viewport. Among framings that slice no word it
    prefers the one whose (clamped) centre and zoom are closest to the ask, with a small cost for each
    rule or box line it cuts and for text that runs right up to a vertical edge."""
    z0 = max(1.0, min(MAX_ZOOM, float(zoom)))
    fx, fy = lay.cell(col + .5, row + .5)

    def centre(tx: float, ty: float, z: float) -> Tuple[float, float]:
        vx0, vy0, vx1, vy1 = lay.viewport(tx, ty, z)
        return ((vx0 + vx1) / 2 - lay.ox) / CW, ((vy0 + vy1) / 2 - lay.oy) / CH
    ask = centre(*lay.cam(z0, col, row))
    cands = {}
    for k in range(9):
        z = round(max(1.0, z0 * (1 - .01 * k)), 4)
        hw, hh = lay.scr_w / (2 * z * CW), lay.scr_h / (2 * z * CH)
        cs = {col + dc2 / 2.0 for dc2 in range(-12, 13)}
        rs = {row + dr4 / 4.0 for dr4 in range(-12, 13)}
        cs.update(c for e in range(-1, lay.cols + 2) for c in (e - .5 + hw, e - .5 - hw))
        rs.update(r for e in range(-1, lay.rows + 2) for r in (e - .5 + hh, e - .5 - hh))
        for r in rs:
            for c in cs:
                tx, ty, zz = lay.cam(z, c, r)
                key = (round(tx, 2), round(ty, 2), zz)
                if key in cands:
                    continue
                vx0, vy0, vx1, vy1 = lay.viewport(tx, ty, zz)
                if not (vx0 + CW <= fx <= vx1 - CW and vy0 + CH <= fy <= vy1 - CH):
                    continue
                cx, cy = centre(tx, ty, zz)
                cands[key] = (abs(cx - ask[0]) + 2 * abs(cy - ask[1]) + 60 * (z0 - zz), tx, ty, zz)
    best = None
    for base, tx, ty, zz in sorted(cands.values()):
        if best is not None and best[0] == 0 and base >= best[1]:
            break
        cuts = edge_cuts(lay, tx, ty, zz, ink)
        if best is not None and cuts > best[0]:
            continue
        cost = base + .25 * edge_cuts(lay, tx, ty, zz, ink, 1)    # keep box and rule lines whole when it is cheap
        cost += 1.5 * edge_touches(lay, tx, ty, zz, ink)          # and a gutter between text and the edge
        if best is None or (cuts, cost) < (best[0], best[1]):
            best = (cuts, cost, tx, ty, zz)
    if best is None:
        tx, ty, z = lay.cam(z0, col, row)
        return tx, ty, z, edge_cuts(lay, tx, ty, z, ink)
    return best[2], best[3], best[4], best[0]


def camera_track(camera: Sequence[Sequence], lay: Layout, frames: Optional[List] = None,
                 total: Optional[float] = None, problems: Optional[List[str]] = None
                 ) -> List[Tuple[float, float, float, float, str]]:
    """[[t, zoom, col, row, ease], ...] -> [(t, tx, ty, z, ease)], with an implicit 1.0 start.

    With frames, every zoomed hold (consecutive keys asking for the same framing) is snapped so its
    viewport edges fall on whitespace for every frame shown during the hold (snap_camera); a hold that
    cannot be framed without slicing a word is reported in problems."""
    keys = []
    for k in sorted(camera, key=lambda k: float(k[0])):
        t, zoom, col, row = float(k[0]), float(k[1]), float(k[2]), float(k[3])
        ease = str(k[4]).upper() if len(k) > 4 else "SETTLE"
        if zoom > MAX_ZOOM + 1e-9:
            raise ValueError("camera zoom %.2f exceeds the %.1f cap" % (zoom, MAX_ZOOM))
        if ease not in EASES:
            raise ValueError("camera ease %r: use GLIDE or SETTLE" % ease)
        keys.append((t, zoom, col, row, ease))
    out: List[Tuple[float, float, float, float, str]] = []
    i = 0
    while i < len(keys):
        j = i
        while j + 1 < len(keys) and keys[j + 1][1:4] == keys[i][1:4]:
            j += 1
        t, zoom, col, row, _e = keys[i]
        if frames and zoom > 1.001:
            # static from the group's first key until its last one (or the end, for the final group)
            t_hold = keys[j][0] if j + 1 < len(keys) else (total if total is not None else frames[-1][0])
            shown = [f for n, f in enumerate(frames)
                     if f[0] <= max(t_hold, t) and (n + 1 >= len(frames) or frames[n + 1][0] > t)]
            ink = _ink(shown, lay.cols, lay.rows)
            tx, ty, z, cuts = snap_camera(lay, zoom, col, row, ink)
            if cuts and problems is not None:
                problems.append("camera at %.2fs slices %d cell(s) of text at the viewport edge" % (t, cuts))
            full, framed = row_fill(lay, 0.0, 0.0, 1.0, ink), row_fill(lay, tx, ty, z, ink)
            if framed < min(MIN_FILL, full) - 1e-9 and problems is not None:
                problems.append("camera at %.2fs frames empty space: %.0f%% of the zoomed rows hold text, "
                                "%.0f%% of the whole screen's" % (t, 100 * framed, 100 * full))
        else:
            tx, ty, z = lay.cam(zoom, col, row)
        for k in keys[i:j + 1]:
            out.append((k[0], tx, ty, z, k[4]))
        i = j + 1
    if not out or out[0][0] > 0:
        out.insert(0, (0.0, 0.0, 0.0, 1.0, "SETTLE"))
    return out


def camera_at(track, t: float) -> Tuple[float, float, float]:
    if t <= track[0][0]:
        return track[0][1:4]
    for a, b in zip(track, track[1:]):
        if a[0] <= t <= b[0]:
            span = b[0] - a[0]
            p = (t - a[0]) / span if span > 0 else 1.0
            e = _bezier(EASES[b[4]])(p)
            return tuple(a[i] + (b[i] - a[i]) * e for i in (1, 2, 3))
    return track[-1][1:4]


def check_camera(frames: List, track, lay: Layout, total: float) -> List[str]:
    """The camera never hides an error: rows with error words or BAD-coloured runs that are on screen
    while zoomed must stay inside the viewport. Also: the zoomed viewport stays inside the screen."""
    problems = []
    n = len(frames)
    for i, f in enumerate(frames):
        t0 = f[0]
        t1 = frames[i + 1][0] if i + 1 < n else total
        spans = []
        for y, runs in enumerate(f[1]):
            for x, text, col, _b in runs:
                if col.lower() == BAD.lower():
                    spans.append((x, x + len(text), y, text))
                for m in ERROR_WORDS.finditer(text):
                    spans.append((x + m.start(), x + m.end(), y, m.group(0)))
        steps = max(2, int((t1 - t0) * 30))
        for k in range(steps + 1):
            t = t0 + (t1 - t0) * k / steps
            tx, ty, z = camera_at(track, t)
            vx0, vy0, vx1, vy1 = lay.viewport(tx, ty, z)
            if (vx0 < lay.scr_x - .5 or vy0 < lay.scr_y - .5 or vx1 > lay.scr_x + lay.scr_w + .5
                    or vy1 > lay.scr_y + lay.scr_h + .5):
                problems.append("camera at %.2fs shows outside the screen" % t)
                break
            if z <= 1.001:
                continue
            for x0, x1, y, word in spans:
                cx0, cy0 = lay.cell(x0, y)
                cx1, cy1 = lay.cell(x1, y + 1)
                if cx0 < vx0 - .5 or cx1 > vx1 + .5 or cy0 < vy0 - .5 or cy1 > vy1 + .5:
                    problems.append("camera at %.2fs hides %r on row %d" % (t, word, y))
                    break
    return sorted(set(problems))[:10]


def check_captions(frames: List, captions: Sequence, total: float, fade_out: float = 0.18) -> List[str]:
    """Each caption's must_show text is on screen for every frame shown while the caption is."""
    problems = []
    n = len(frames)
    for cap in captions:
        t0, t1, text, must = float(cap[0]), float(cap[1]), cap[2], cap[3]
        if not must:
            problems.append("caption %r has no must_show text" % text)
            continue
        for i, f in enumerate(frames):
            a = f[0]
            b = frames[i + 1][0] if i + 1 < n else total
            if b <= t0 or a >= t1 + fade_out:
                continue
            if must not in frame_text(f[1]):
                problems.append("caption %r shows at %.2fs but %r is not on screen" % (text, max(a, t0), must))
                break
    return problems


def _caption_width(text: str) -> float:
    """Pill width: the text measured in a wide sans (DejaVu Sans) when Pillow has it, so system fonts fit."""
    try:
        from PIL import ImageFont
        for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/Library/Fonts/Arial.ttf"):
            if Path(p).exists():
                return float(ImageFont.truetype(p, 13).getlength(text))
    except Exception:                         # noqa: BLE001 - Pillow missing: estimate
        pass
    return len(text) * 7.4


# ---------------------------------------------------------------------------
# the SVG
# ---------------------------------------------------------------------------

HOLD = "steps(1,end)"


def keyframes(points: Sequence[Tuple[float, str]], total: float, ease="linear") -> str:
    """Keyframes from (t, declarations) points. A segment whose two ends are equal is a hold and gets
    steps(1,end), so the browser has nothing to interpolate (and nothing to re-rasterise) until the
    next change: an SVG inside <img> is repainted whole on every interpolated frame. ease is the
    timing of the moving segments: a string, or a function of the index of the segment's end point."""
    pts = sorted(points, key=lambda p: p[0])
    if not pts:
        return ""
    if pts[0][0] > 0:
        pts.insert(0, (0.0, pts[0][1]))
    if pts[-1][0] < total:
        pts.append((total, pts[-1][1]))
    out = []
    for i, (t, v) in enumerate(pts):
        if i + 1 < len(pts):
            nv = pts[i + 1][1]
            tf = HOLD if nv == v else (ease(i + 1) if callable(ease) else ease)
            out.append("%s{%s;animation-timing-function:%s}" % (_pc(t, total), v, tf))
        else:
            out.append("%s{%s}" % (_pc(t, total), v))
    return "".join(out)


def _mark(x: float, y: float) -> str:
    """The pixel mark: core plus one bipolar beam at fx's t = 0 angle (20.05 deg), 14 px."""
    a = math.degrees(0.35)
    return ('<g transform="translate(%s %s) rotate(%.2f)">'
            '<path d="M1.5 -1.3L7 -.25V.25L1.5 1.3z" fill="url(#bn)"/>'
            '<path d="M-1.5 -1.3L-7 -.25V.25L-1.5 1.3z" fill="url(#bs)"/>'
            '<circle r="3.2" fill="%s" opacity=".18"/><circle r="1.7" fill="%s"/></g>' % (_n(x), _n(y), a, ICE, ICE))


def _chip(x_right: float, y: float, label: str, color: str) -> Tuple[str, float]:
    w = len(label) * 7.0 + 14
    x = x_right - w
    return ('<rect x="%s" y="%s" width="%s" height="18" rx="5" fill="%s" stroke="%s"/>'
            '<text x="%s" y="%s" class="ch" fill="%s" textLength="%s">%s</text>'
            % (_n(x), _n(y), _n(w), CHIP_BG, CHIP_STROKE, _n(x + 7), _n(y + 12.5), color, _n(len(label) * 7.0 - 1.5),
               html.escape(label))), x


def ring_anchor(frames: List, curs: List, te: float) -> Optional[Tuple[int, int]]:
    """Where the Enter ring lands: the prompt glyph (the first glyph that is not a rule or box line) of
    the row holding the cursor when Enter is pressed. The CLI clears the typed line on Enter, so the
    ring belongs to the prompt that stays, not to the last typed cell."""
    at, shown = None, None
    for f, c in zip(frames, curs):
        if f[0] > te + 1e-6:
            break
        shown = f
        if c is not None:
            at = c
    if at is None or shown is None:
        return None
    lines = frame_text(shown[1]).split("\n")
    line = lines[at[1]] if 0 <= at[1] < len(lines) else ""
    for x, ch in enumerate(line[:at[0] + 1]):
        if _wordy(ch):
            return (x, at[1])
    return at


def to_svg(frames: List, scene: Dict, *args, **kw) -> str:
    """See _to_svg. A scene's "colour_step" (default COLOUR_STEP) sets the colour precision for this
    render: gradient rows (the boot wordmark and beams) carry one colour per cell, and a coarser step
    merges neighbours into fewer paths, so more frames fit the size budget."""
    global COLOUR_STEP
    prev = COLOUR_STEP
    COLOUR_STEP = int(scene.get("colour_step") or prev)
    try:
        return _to_svg(frames, scene, *args, **kw)
    finally:
        COLOUR_STEP = prev


def _to_svg(frames: List, scene: Dict, chrome: bool = True, camera: Optional[Sequence] = None,
            captions: Optional[Sequence] = None, cursor: bool = True, thud: bool = True) -> str:
    """Render recorded frames [(t, rows[, cursor]), ...] as an animated SVG.

    scene: {"cols", "rows", "title", "alt", "hold_last", "lead", "demo", "glow", "enters", "camera",
    "captions"}. camera/captions default to the scene's own keys; both must hold resolved numeric times
    of the recording.

    lead > 0 opens the replay on its last frame (the composed poster, also what reduced motion and a
    paused <img> show) for lead seconds, then dips to the background and plays from the first frame:
    the loop is rotated so it starts and ends on the same frame, and no viewer ever lands on a half
    drawn opening. Without lead the dip plays at the end, before the restart.

    Raises ValueError when a caption's must_show text is not on screen, the camera hides an error or
    a zoomed hold slices a word at the viewport edge.
    """
    cols, rows = int(scene.get("cols", 100)), int(scene.get("rows", 30))
    title = str(scene.get("title") or "pulsarcode")
    alt = str(scene.get("alt") or title)
    hold = float(scene.get("hold_last", 2.0))
    lead = max(0.0, float(scene.get("lead") or 0.0))
    camera = scene.get("camera") if camera is None else camera
    captions = scene.get("captions") if captions is None else captions
    captions = [c for c in (captions or [])]
    enters = [float(t) for t in (scene.get("enters") or [])] if thud else []
    if not frames:
        raise ValueError("no frames")
    lay = Layout(cols, rows, bool(captions) and chrome)

    # the rotated loop: [0, lead) last frame, [lead, off) dip, then the recording from off on
    off = lead + DIP if lead > 0 and len(frames) > 1 else 0.0
    if off:
        last = frames[-1]
        frames = [(0.0,) + tuple(last[1:])] + [(float(f[0]) + off,) + tuple(f[1:]) for f in frames]
        captions = [[float(c[0]) + off, float(c[1]) + off] + list(c[2:]) for c in captions]
        camera = [[float(k[0]) + off] + list(k[1:]) for k in (camera or [])]
        enters = [t + off for t in enters]
    t_end = float(frames[-1][0])
    total = t_end + hold + (0.0 if off else DIP)
    n = len(frames)

    problems = check_captions(frames, captions, total)
    track = camera_track(camera or [], lay, frames, total, problems)
    if off and len(track) > 1 and track[-1][1:4] != (0.0, 0.0, 1.0):
        # the lead shows the last frame as the loop ended it: framed like the end, released under the dip
        fin = track[-1]
        track = [(0.0,) + fin[1:4] + ("SETTLE",), (off - .001,) + fin[1:4] + ("SETTLE",)] + \
            [k for k in track if k[0] >= off]
        track.insert(2, (off, 0.0, 0.0, 1.0, "SETTLE"))
    if camera:
        problems += check_camera(frames, track, lay, total)
    if problems:
        raise ValueError("; ".join(problems))

    pal = Palette()
    symbols: Dict[str, str] = {}
    sym_svg: List[str] = []
    frame_keys: List[List[str]] = []
    for f in frames:
        keys = []
        for runs in f[1]:
            if not runs:
                keys.append("")
                continue
            h = hashlib.sha1(json.dumps(runs).encode()).hexdigest()
            if h not in symbols:
                symbols[h] = _b36(len(symbols))
                sym_svg.append('<g id="r%s">%s</g>' % (symbols[h], row_svg(runs, bool(scene.get("glow")))))
            keys.append(symbols[h])
        frame_keys.append(keys)

    W, H = lay.W, lay.H
    css: List[str] = []
    body: List[str] = []
    pc = lambda t: _pc(t, total)  # noqa: E731

    css.append("text{font-family:%s;font-size:%spx;white-space:pre}" % (MONO, _n(FS)))
    css.append(".b{font-weight:700}.g{stroke-width:1.6px;stroke-opacity:.3;paint-order:stroke}"
               ".h{fill:currentColor;stroke:currentColor}"
               ".w1{fill:none;stroke-width:1px}.w2{fill:none;stroke-width:1.2px}.w3{fill:none;stroke-width:2.2px}"
               ".o1{opacity:.25}.o2{opacity:.5}.o3{opacity:.75}")
    css.append(".f{visibility:hidden;animation:%.3fs steps(1,end) infinite}#f%d{visibility:visible}" % (total, n - 1))
    for i, f in enumerate(frames):
        t = f[0]
        t_next = frames[i + 1][0] if i + 1 < n else total
        a, b = pc(t), pc(t_next)
        if n == 1:
            continue
        if i == 0:
            css.append("#f0{animation-name:k0}@keyframes k0{0%%{visibility:visible}%s{visibility:hidden}}" % b)
        elif i == n - 1:
            css.append("#f%d{animation-name:k%d}@keyframes k%d{0%%{visibility:hidden}%s{visibility:visible}}"
                       % (i, i, i, a))
        else:
            css.append("#f%d{animation-name:k%d}@keyframes k%d{0%%{visibility:hidden}%s{visibility:visible}"
                       "%s{visibility:hidden}}" % (i, i, i, a, b))

    # ---- window: shadow, body, bar -------------------------------------------------------------
    wx, wy, ww, wh = lay.win_x, lay.win_y, lay.win_w, lay.win_h
    for dy, grow, op in ((2, 0, .22), (6, 3, .10), (12, 7, .05)):
        body.append('<rect x="%s" y="%s" width="%s" height="%s" rx="%s" fill="#000" opacity="%s"/>'
                    % (_n(wx - grow), _n(wy + dy - grow), _n(ww + 2 * grow), _n(wh + 2 * grow), _n(12 + grow),
                       _n(op) if op >= .1 else "%.2f" % op))
    body.append('<rect x="%s" y="%s" width="%s" height="%s" rx="12" fill="%s"/>'
                % (_n(wx), _n(wy), _n(ww), _n(wh), SCREEN_BG))
    if chrome:
        body.append('<path d="%s" fill="%s"/>' % (_rrect(wx, wy, ww, BAR, 12, 12, 0, 0), BAR_BG))
        body.append('<path d="M%s %sh%s" stroke="#FFFFFF" stroke-opacity=".06"/>'
                    % (_n(wx), _n(wy + BAR - .5), _n(ww)))
        body.append(_mark(wx + 20, wy + BAR / 2))
        body.append('<text x="%s" y="%s" class="tt" text-anchor="middle">%s</text>'
                    % (_n(wx + ww / 2), _n(wy + BAR / 2 + 4.2), html.escape(title)))
        # timecode, then chips leftwards
        right = wx + ww - 16
        tc_svg, tc_css, tc_w = _timecode(right, wy + BAR / 2 + 4.2, total, off, t_end - off)
        body.append(tc_svg)
        css.extend(tc_css)
        right -= tc_w + 10
        if scene.get("demo"):
            chip, right = _chip(right, wy + BAR / 2 - 9, "DEMO", WARN)
            body.append(chip)
            right -= 6
        chip, right = _chip(right, wy + BAR / 2 - 9, "REPLAY", ASH)
        body.append(chip)
        css.append(".tt{font-size:12px;fill:%s}.ch{font-size:10px;font-weight:700}" % ASH)

    # ---- screen: clip > shake > camera > content -------------------------------------------------
    sx, sy, sw, sh = lay.scr_x, lay.scr_y, lay.scr_w, lay.scr_h
    bottom_r = 0 if lay.box else 12
    top_r = 0 if chrome else 12
    clip = _rrect(sx, sy, sw, sh, top_r, top_r, bottom_r, bottom_r)
    # Frames shown while the camera holds still sit in a group with that hold's fixed transform; only
    # frames shown while it moves sit under the animated .cam group. Chromium re-lays out every SVG text
    # under a transform whose scale changes, hidden frames included, so a single camera group over all
    # frames made each step of a glide cost a full re-layout of the whole replay.
    moves = [(track[k][0], track[k + 1][0]) for k in range(len(track) - 1) if track[k][1:4] != track[k + 1][1:4]]
    groups: Dict[Tuple[float, float, float], List[int]] = {}
    moving: List[int] = []
    for i, f in enumerate(frames):
        a, b = float(f[0]), float(frames[i + 1][0]) if i + 1 < n else total
        if any(m0 < b and a < m1 for m0, m1 in moves):
            moving.append(i)
        else:
            st = tuple(round(v, 3) for v in camera_at(track, a))
            groups.setdefault(st, []).append(i)

    def frame_group(i: int) -> str:
        return '<g class="f" id="f%d">%s</g>' % (
            i, "".join('<use href="#r%s" y="%s"/>' % (k, _n(y * CH)) for y, k in enumerate(frame_keys[i]) if k))

    def cam_value(st) -> str:
        return "transform:translate(%spx,%spx) scale(%.3f)" % (_n(st[0]), _n(st[1]), st[2])
    grid = '<g transform="translate(%s %s)">' % (_n(lay.ox), _n(lay.oy))
    body.append('<g clip-path="url(#scr)"><g class="shk">')
    for g, (st, idx) in enumerate(sorted(groups.items(), key=lambda kv: kv[1][0])):
        if st == (0.0, 0.0, 1.0):
            body.append('<g>')
        else:
            body.append('<g class="cz z%d">' % g)
            css.append(".z%d{%s}" % (g, cam_value(st)))
        body.append(grid + "".join(frame_group(i) for i in idx) + "</g></g>")
    for i in moving:
        # a frame shown during a move carries the camera path itself, sampled at 30 Hz over the time it
        # is on screen and still before and after: at any moment only the visible frame re-lays out
        a, b = float(frames[i][0]), float(frames[i + 1][0]) if i + 1 < n else total
        ts = [a + (b - a) * k / max(1, int(math.ceil((b - a) * 30))) for k in range(int(math.ceil((b - a) * 30)) + 1)]
        pts = [(t, cam_value(camera_at(track, t))) for t in ts]
        body.append('<g class="cz q%d">' % i + grid + frame_group(i) + "</g></g>")
        css.append(".q%d{animation:q%d %.3fs infinite}@keyframes q%d{%s}" % (i, i, total, i, keyframes(pts, total)))
    if groups.keys() - {(0.0, 0.0, 1.0)} or moving:
        css.append(".cz{transform-box:view-box;transform-origin:0 0}")
    body.append('<g class="cam">')

    # cursor
    curs = [_cur(f) for f in frames]
    if cursor and any(c is not None for c in curs):
        body.append(_cursor(frames, curs, lay, total, css, hold))
    # Enter thud: ring at the cursor, shake on the world
    rings, shake_keys = [], []
    last_end = -1.0
    for k, te in enumerate(enters):
        if te < off or te + SHAKE_MS / 1000 > total - (0.0 if off else DIP) or te < last_end:
            continue
        last_end = te + SHAKE_MS / 1000
        shake_keys.append(te)
        at = ring_anchor(frames, curs, te)
        if at is not None:
            cx, cy = lay.cell(at[0] + .5, at[1] + .5)
            rings.append('<g class="rg rg%d"><circle class="ro ro%d" cx="%s" cy="%s" r="12"/></g>'
                         % (k, k, _n(cx), _n(cy)))
            ring = RING_MS / 1000
            grow = [(te - .001, "transform:scale(.2)"), (te, "transform:scale(.2)"), (te + ring, "transform:scale(1)")]
            fade = [(te - .001, "opacity:0"), (te, "opacity:.8"), (te + ring, "opacity:0")]
            css.append(".rg%d{animation:rg%d %.3fs infinite}@keyframes rg%d{%s}"
                       % (k, k, total, k, keyframes([(0.0, grow[0][1])] + grow, total, E_SHOCK)))
            css.append(".ro%d{animation:ro%d %.3fs infinite}@keyframes ro%d{%s}"
                       % (k, k, total, k, keyframes([(0.0, fade[0][1])] + fade, total, E_IMPACT)))
    if rings:
        body.extend(rings)
        # the stroke does not scale with the ring (non-scaling-stroke): ICE 1.5 px from the first frame
        css.append(".rg{transform-box:fill-box;transform-origin:center}.ro{fill:none;stroke:%s;stroke-width:1.5px;"
                   "opacity:0;vector-effect:non-scaling-stroke;mix-blend-mode:screen}" % ICE)
    body.append("</g></g>")                   # cam, shk
    if shake_keys:
        pts = [(0.0, "transform:translate(0,0)")]
        for te in shake_keys:
            for p, dx, dy in SHAKE:
                pts.append((te + p / 100.0 * SHAKE_MS / 1000,
                            "transform:translate(%spx,%spx)" % (_n(dx * THUD_AMP), _n(dy * THUD_AMP))))
        css.append(".shk{animation:shk %.3fs infinite}@keyframes shk{%s}" % (total, keyframes(pts, total)))
    # camera: one track, moving only between keys that differ
    if len(track) > 1:
        pts = [(t, "transform:translate(%spx,%spx) scale(%.3f)" % (_n(tx), _n(ty), z))
               for t, tx, ty, z, _e in track]
        eases = [EASES[k[4]] for k in track]
        css.append(".cam{transform-box:view-box;transform-origin:0 0;animation:cam %.3fs infinite}"
                   "@keyframes cam{%s}" % (total, keyframes(pts, total, lambda i: eases[min(i, len(eases) - 1)])))
    if n > 1:                                 # a still (one frame) has no loop: no dip, a full hairline
        body.append('<rect class="dip" x="%s" y="%s" width="%s" height="%s" fill="%s" opacity="0"/>'
                    % (_n(sx), _n(sy), _n(sw), _n(sh), SCREEN_BG))
        if off:                               # the dip between the lead (last frame) and the first frame
            dip = [(0.0, "opacity:0"), (lead, "opacity:0"), (off - .001, "opacity:1"), (off, "opacity:0")]
        else:                                 # the dip before the loop restarts
            dip = [(0.0, "opacity:0"), (total - DIP, "opacity:0"), (total, "opacity:1")]
        css.append(".dip{animation:dip %.3fs infinite}@keyframes dip{%s}" % (total, keyframes(dip, total, E_SETTLE)))
        # the hairline steps when the screen changes or the timecode ticks, never in between
        span = total - off
        ticks = sorted({round(float(f[0]), 3) for f in frames if f[0] >= off}
                       | {round(off + s, 3) for s in range(int(math.floor(span)) + 1)})
        pts = ([(0.0, "transform:scaleX(1)")] if off else []) + \
            [(t, "transform:scaleX(%.4f)" % max(0.0, min(1.0, (t - off) / span))) for t in ticks]
        css.append(".pr{transform-box:fill-box;transform-origin:0 0;animation:pr %.3fs infinite}"
                   "@keyframes pr{%s}" % (total, keyframes(pts, total, HOLD)))
    # progress hairline
    body.append('<rect class="pr" x="%s" y="%s" width="%s" height="2" fill="url(#pg)" opacity=".85"/>'
                % (_n(sx), _n(sy + sh - 2), _n(sw)))
    body.append("</g>")                       # clip

    # ---- captions in the letterbox ---------------------------------------------------------------
    poster_caps = []
    if lay.box:
        cy = sy + sh + lay.box / 2
        body.append('<path d="M%s %sh%s" stroke="#FFFFFF" stroke-opacity=".05"/>' % (_n(sx), _n(sy + sh + .5), _n(sw)))
        soft = "%s;animation-timing-function:%s" % (E_SPRING_SOFT_FALLBACK, E_SPRING_SOFT)
        for k, cap in enumerate(captions):
            t0, t1, text = float(cap[0]), float(cap[1]), str(cap[2])
            opts = cap[4] if len(cap) > 4 and isinstance(cap[4], dict) else {}
            tw = _caption_width(text)
            pw = tw + 40
            px = wx + ww / 2 - pw / 2
            poster = bool(opts.get("poster"))
            if poster:
                poster_caps.append(k)
            body.append('<g class="cap cp%d%s">' % (k, " capP" if poster else ""))
            body.append('<rect x="%s" y="%s" width="%s" height="28" rx="14" fill="%s" fill-opacity=".92" stroke="%s"/>'
                        % (_n(px), _n(cy - 14), _n(pw), CHIP_BG, CHIP_STROKE))
            body.append('<rect x="%s" y="%s" width="3" height="14" rx="1.5" fill="url(#pv)"/>'
                        % (_n(px + 11), _n(cy - 7)))
            body.append('<text x="%s" y="%s" class="cpt" text-anchor="middle">%s</text></g>'
                        % (_n(px + 22 + (pw - 22 - 14) / 2), _n(cy + 4.5), html.escape(text)))
            hidden, shown = "opacity:0;transform:translateY(8px)", "opacity:1;transform:translateY(0)"
            pts = [(0.0, hidden), (t0, hidden), (t0 + .3, shown), (t1, shown),
                   (min(total, t1 + .18), "opacity:0;transform:translateY(0)")]
            eases = {2: soft, 4: "linear"}
            css.append(".cp%d{animation:cp%d %.3fs infinite}@keyframes cp%d{%s}"
                       % (k, k, total, k, keyframes(pts, total, lambda i, e=eases: e.get(i, "linear"))))
        css.append(".cap{opacity:0}.capP{opacity:1}.cpt{font-family:%s;font-size:13px;fill:%s}" % (SANS, ICE))

    # ---- window strokes on top -------------------------------------------------------------------
    body.append('<rect x="%s" y="%s" width="%s" height="%s" rx="11.3" fill="none" stroke="url(#pg)" '
                'stroke-width="1.5" stroke-opacity=".7"/>' % (_n(wx + .75), _n(wy + .75), _n(ww - 1.5), _n(wh - 1.5)))
    body.append('<rect x="%s" y="%s" width="%s" height="%s" rx="10" fill="none" stroke="#FFFFFF" '
                'stroke-opacity=".06"/>' % (_n(wx + 2), _n(wy + 2), _n(ww - 4), _n(wh - 4)))

    # ---- reduced motion: the last frame, camera 1.0, nothing moves (last block of the stylesheet) --
    sym_text = "".join(sym_svg)
    pal.count(sym_text)
    sym_text = pal.resolve(sym_text)
    css.append(pal.css())
    css.append("@media (prefers-reduced-motion: reduce){*{animation:none!important}.f{visibility:hidden}"
               "#f%d{visibility:visible}.cam,.shk,.cz{transform:none}.dip{opacity:0}.cap{opacity:0}"
               "%s.cs{opacity:1}}" % (n - 1, "".join(".cp%d{opacity:1}" % k for k in poster_caps)))

    defs = ("<defs><linearGradient id='pg' x1='0' x2='1'><stop offset='0' stop-color='%s'/>"
            "<stop offset='.35' stop-color='%s'/><stop offset='.7' stop-color='%s'/>"
            "<stop offset='1' stop-color='%s'/></linearGradient>"
            "<linearGradient id='pv' x1='0' x2='0' y1='0' y2='1'><stop offset='0' stop-color='%s'/>"
            "<stop offset='.5' stop-color='%s'/><stop offset='1' stop-color='%s'/></linearGradient>"
            "<linearGradient id='bn' x1='0' x2='1'><stop offset='0' stop-color='%s'/><stop offset='1' stop-color='%s' stop-opacity='.2'/></linearGradient>"
            "<linearGradient id='bs' x1='1' x2='0'><stop offset='0' stop-color='%s'/><stop offset='1' stop-color='%s' stop-opacity='.2'/></linearGradient>"
            "<clipPath id='scr'><path d='%s'/></clipPath>%s%s</defs>"
            % (EMBER, FLARE, PLASMA, BEAM, EMBER, FLARE, BEAM, BEAM, BEAM, PLASMA, PLASMA, clip,
               _digit_defs() if chrome else "", sym_text))
    head = ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" role="img" '
            'aria-label="%s"><title>%s</title>' % (W, H, W, H, html.escape(alt), html.escape(alt)))
    return head + defs + "<style>%s</style>" % "".join(css) + "".join(body) + "</svg>"


BLINK = 1.06                                  # s: cursor blink period, while a frame holds > 500 ms


def _cursor(frames: List, curs: List, lay: Layout, total: float, css: List[str], hold: float) -> str:
    """One cell-sized rect moved with steps(1,end); solid while frames change fast, blinking
    (1.06 s) while a frame holds longer than 500 ms; hidden where pyte shows no cursor. The blink is
    written out as steps inside the replay's own timeline (no free-running animation), so the
    cursor costs a repaint only when it actually changes."""
    n = len(frames)
    last = curs[-1]
    ks, prev = [], "?"
    blink = []
    for i, (f, c) in enumerate(zip(frames, curs)):
        t = f[0]
        t_next = frames[i + 1][0] if i + 1 < n else total
        if c is None:
            v = "opacity:0"
        else:
            x, y = lay.cell(c[0], c[1])
            v = "opacity:1;transform:translate(%spx,%spx)" % (_n(x), _n(y))
        if v != prev:
            ks.append("%s{%s}" % (_pc(t, total), v))
            prev = v
        on = True
        blink.append((t, on))
        if c is not None and t_next - t > 0.5:
            k = t + BLINK / 2
            while k < t_next - 1e-6:
                on = not on
                blink.append((k, on))
                k += BLINK / 2
    bs, prev_on = [], None
    for t, on in blink:
        if on != prev_on:
            bs.append("%s{opacity:%d}" % (_pc(t, total), 1 if on else 0))
            prev_on = on
    any_x, any_y = next(lay.cell(c[0], c[1]) for c in curs if c is not None)
    lx, ly = lay.cell(last[0], last[1]) if last is not None else (any_x, any_y)
    css.append(".cur{animation:cur %.3fs steps(1,end) infinite}@keyframes cur{%s}" % (total, "".join(ks)))
    if len(bs) > 1:
        css.append(".cs{animation:cs %.3fs steps(1,end) infinite}@keyframes cs{%s}" % (total, "".join(bs)))
    return ('<g class="cur" transform="translate(%s %s)"%s><rect class="cs" width="%s" height="%s" fill="%s" '
            'fill-opacity=".85"/></g>' % (_n(lx), _n(ly), ' opacity="0"' if last is None else "", _n(CW), _n(CH), ICE))


DIGIT_W, DIGIT_H = 7.2, 14.0


def _digit_defs() -> str:
    return "<g id='dg'>%s</g>" % "".join(
        '<text y="%s" class="tc">%d</text>' % (_n(d * DIGIT_H), d) for d in range(10))


def _timecode(right: float, base: float, total: float, off: float, t_last: float) -> Tuple[str, List[str], float]:
    """mm:ss of the recording, from 10 digit glyphs per position toggled once per second with steps(1).
    The clock starts at off (the rotated loop's first frame); during the lead before it, it shows the
    time the loop ends on, since the lead shows that same frame."""
    span = total - off
    secs = int(math.floor(span - 1e-6))
    pts = ([(0.0, secs)] if off else []) + [(off + s, s) for s in range(secs + 1)]

    def digits(s: int) -> Tuple[int, int, int, int]:
        return ((s // 600) % 6, (s // 60) % 10, (s % 60) // 10, s % 10)
    w = 5 * DIGIT_W
    x0 = right - w
    out, css = [], [".tc{font-size:12px;fill:%s}" % ASH]
    end_v = digits(int(math.floor(t_last)))
    xs = [x0, x0 + DIGIT_W, None, x0 + 3 * DIGIT_W, x0 + 4 * DIGIT_W]
    out.append('<text x="%s" y="%s" class="tc">:</text>' % (_n(x0 + 2 * DIGIT_W), _n(base)))
    for pos, slot in ((0, 0), (1, 1), (2, 3), (3, 4)):
        seq = [(t, digits(s)[pos]) for t, s in pts]
        x = xs[slot]
        cid = "tc%d" % (pos + 1)
        if len(set(d for _t, d in seq)) == 1 and seq[0][1] == end_v[pos]:
            out.append('<text x="%s" y="%s" class="tc">%d</text>' % (_n(x), _n(base), seq[0][1]))
            continue
        ks, prev = [], None
        for t, d in seq:
            if d != prev:
                ks.append("%s{transform:translateY(-%spx)}" % (_pc(t, total), _n(d * DIGIT_H)))
                prev = d
        css.append(".%s{animation:%s %.3fs steps(1,end) infinite}@keyframes %s{%s}"
                   % (cid, cid, total, cid, "".join(ks)))
        out.append('<clipPath id="k%s"><rect x="%s" y="%s" width="%s" height="%s"/></clipPath>'
                   '<g clip-path="url(#k%s)"><use href="#dg" x="%s" y="%s" class="%s" transform="translate(0 -%s)"/></g>'
                   % (cid, _n(x - 1), _n(base - 11), _n(DIGIT_W + 2), _n(DIGIT_H), cid, _n(x), _n(base), cid,
                      _n(end_v[pos] * DIGIT_H)))
    return "".join(out), css, w


def stats(svg: str) -> Dict[str, int]:
    raw = svg.encode()
    return {"bytes": len(raw), "gzip": len(gzip.compress(raw, 9)), "elements": svg.count("<") - svg.count("</")}
