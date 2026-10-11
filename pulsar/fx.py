# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Pulsar Code visual engine.

A tiny retained-mode renderer for the terminal: draw into a Canvas, hand
it to a Screen, and only the cells that changed since the last frame are
written. That keeps 30 fps animations flicker-free even over SSH.

Brand system
  spectrum   ember #FF6A00 -> flare #FF2E88 -> plasma #9D4DFF -> beam #36E2FF
  mark       ◉  (a pulsar: a dense core with a sweeping beam)
  wordmark   PULSAR in ANSI-shadow block type over a  C O D E  rule

Effects
  Rain        spectrum glyph rain            rings      beacon pulses
  Beams       counter-rotating pulsar beams  sweep      sonar sweep
  Particles   sparks with gravity and fade   Stars      quiet starfield
  Glitch      slice displacement + chromatic split bursts
  scanline    a bright line that rakes across a region
  flare       anamorphic lens flare (beacon ignition)
  bar         gradient progress bar with shimmer
  warp        hyperspace streaks
  FrameBudget measures frame cost and scales effect density to keep up

Color depth: truecolor, 256-color, the 16 ANSI colors, or none.
"""

from __future__ import annotations

import math
import random
import sys
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

Color = Tuple[int, int, int]

EMBER: Color = (255, 106, 0)
FLARE: Color = (255, 46, 136)
PLASMA: Color = (157, 77, 255)
BEAM: Color = (54, 226, 255)
ICE: Color = (236, 246, 255)
ASH: Color = (150, 146, 172)
DUST: Color = (88, 84, 108)
NIGHT: Color = (44, 40, 60)
OK: Color = (80, 250, 170)
WARN: Color = (255, 196, 0)
BAD: Color = (255, 70, 90)
SPECTRUM: Tuple[Color, ...] = (EMBER, FLARE, PLASMA, BEAM)

MARK = "◉"
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
PULSE = " ·∙•●◉●•∙· "
RAIN_GLYPHS = "ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝ0123456789{}<>/=+*;:$#@&ΣΔλψΩ"
RAIN_ASCII = "0123456789abcdef{}<>/=+*;:$#@&|"
DECODE_GLYPHS = "█▓▒░╳╱╲◢◣◤◥▚▞▙▟01"
DECODE_ASCII = "#%&*+=-:01"


def hex_rgb(h: str) -> Color:
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def lerp(a: Color, b: Color, t: float) -> Color:
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    return (int(a[0] + (b[0] - a[0]) * t), int(a[1] + (b[1] - a[1]) * t), int(a[2] + (b[2] - a[2]) * t))


def grad(stops: Sequence[Color], t: float) -> Color:
    if len(stops) == 1:
        return stops[0]
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    seg = t * (len(stops) - 1)
    i = min(int(seg), len(stops) - 2)
    return lerp(stops[i], stops[i + 1], seg - i)


def tri(t: float) -> float:
    """Triangle wave on [0,1]: lets gradients drift without a hard seam."""
    t = t % 2.0
    return t if t <= 1.0 else 2.0 - t


def scale(c: Color, f: float) -> Color:
    return (max(0, min(255, int(c[0] * f))), max(0, min(255, int(c[1] * f))), max(0, min(255, int(c[2] * f))))


def clamp01(t: float) -> float:
    return 0.0 if t < 0 else 1.0 if t > 1 else t


def ease_out(t: float) -> float:
    t = clamp01(t)
    return 1 - (1 - t) ** 3


def ease_in_out(t: float) -> float:
    t = clamp01(t)
    return 4 * t * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


# ---------------------------------------------------------------------------
# Color output
# ---------------------------------------------------------------------------

# The canonical 16 ANSI colors (index order). Real terminals theme these, so the mapping
# below picks by hue family and brightness rather than by distance to these exact values.
ANSI16: Tuple[Color, ...] = (
    (0, 0, 0), (205, 49, 49), (13, 188, 121), (229, 229, 16), (36, 114, 200), (188, 63, 188), (17, 168, 205),
    (229, 229, 229), (102, 102, 102), (241, 76, 76), (35, 209, 139), (245, 245, 67), (59, 142, 234),
    (214, 112, 214), (41, 184, 219), (255, 255, 255))


def rgb16(c: Color) -> int:
    """Nearest of the 16 ANSI colors by hue family and brightness (0-15).

    Never returns black for a visible color: dim tones become bright-black (dark gray), so
    faded glyphs stay faintly visible on a black background instead of vanishing.
    """
    r, g, b = c
    mx, mn = max(r, g, b), min(r, g, b)
    v = mx / 255.0
    s = 0.0 if mx == 0 else (mx - mn) / float(mx)
    if s < 0.28 or v < 0.18 or (v < 0.3 and s < 0.6):
        if v < 0.06:
            return 0
        if v < 0.55:
            return 8
        if v < 0.86:
            return 7
        return 15
    d = float(mx - mn)
    if mx == r:
        hue = (60 * (g - b) / d) % 360
    elif mx == g:
        hue = 60 * (b - r) / d + 120
    else:
        hue = 60 * (r - g) / d + 240
    if hue >= 340 or hue < 30:
        base = 1      # red
    elif hue < 75:
        base = 3      # yellow
    elif hue < 160:
        base = 2      # green
    elif hue < 205:
        base = 6      # cyan
    elif hue < 275:
        base = 4      # blue
    else:
        base = 5      # magenta
    return base + (8 if v >= 0.62 else 0)


class Ink:
    """Turns colors into escape codes for the terminal's color depth (0, 4, 8 or 24)."""

    def __init__(self, depth: int) -> None:
        self.depth = depth
        self._cache: Dict[Tuple[Color, bool, bool], str] = {}

    def code(self, fg: Optional[Color], bold: bool = False, bg: Optional[Color] = None) -> str:
        if self.depth == 0:
            return ""
        key = (fg or (-1, -1, -1), bold, bg is not None and bg != (0, 0, 0))
        if bg is None and key in self._cache:
            return self._cache[key]
        parts = ["0"]
        if bold:
            parts.append("1")
        if fg is not None:
            parts.append(self._c(fg, 38))
        if bg is not None:
            parts.append(self._c(bg, 48))
        out = "\x1b[" + ";".join(parts) + "m"
        if bg is None:
            if len(self._cache) > 8192:
                self._cache.clear()
            self._cache[key] = out
        return out

    def _c(self, c: Color, base: int) -> str:
        if self.depth >= 24:
            return "%d;2;%d;%d;%d" % (base, c[0], c[1], c[2])
        if self.depth >= 8:
            return "%d;5;%d" % (base, rgb256(c))
        idx = rgb16(c)
        off = base - 8          # 30 for foreground, 40 for background
        return str(off + idx) if idx < 8 else str(off + 60 + idx - 8)

    def paint(self, text: str, fg: Optional[Color], bold: bool = False) -> str:
        if self.depth == 0:
            return text
        return self.code(fg, bold) + text + "\x1b[0m"

    def gradient(self, text: str, stops: Sequence[Color] = SPECTRUM, bold: bool = False, offset: float = 0.0) -> str:
        if self.depth == 0:
            return text
        n = max(1, len(text) - 1)
        out = []
        for i, ch in enumerate(text):
            out.append(self.code(grad(stops, tri((i / n) + offset)), bold) + ch)
        return "".join(out) + "\x1b[0m"


_CUBE = (0, 95, 135, 175, 215, 255)


def rgb256(c: Color) -> int:
    """Nearest xterm-256 index: the 6x6x6 cube or the 24-step gray ramp, whichever is closer.

    Desaturated brand tones (NIGHT, DUST, ASH) land on grays instead of tinted cube corners.
    """
    r, g, b = c
    q = lambda v: 0 if v < 48 else 1 if v < 115 else (v - 35) // 40  # noqa: E731
    qr, qg, qb = q(r), q(g), q(b)
    cr, cg, cb = _CUBE[qr], _CUBE[qg], _CUBE[qb]
    cube = 16 + 36 * qr + 6 * qg + qb
    avg = (r + g + b) // 3
    gi = 23 if avg > 238 else max(0, (avg - 3) // 10)
    gv = 8 + 10 * gi
    d_cube = (cr - r) ** 2 + (cg - g) ** 2 + (cb - b) ** 2
    d_gray = (gv - r) ** 2 + (gv - g) ** 2 + (gv - b) ** 2
    return 232 + gi if d_gray < d_cube else cube


# ---------------------------------------------------------------------------
# Canvas + diff renderer
# ---------------------------------------------------------------------------

Cell = Tuple[str, Optional[Color], bool]
BLANK: Cell = (" ", None, False)


class Canvas:
    def __init__(self, w: int, h: int) -> None:
        self.w, self.h = w, h
        self.cells: List[List[Cell]] = [[BLANK] * w for _ in range(h)]

    def put(self, x: int, y: int, ch: str, fg: Optional[Color] = None, bold: bool = False) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            self.cells[y][x] = (ch, fg, bold)

    def get(self, x: int, y: int) -> Cell:
        if 0 <= x < self.w and 0 <= y < self.h:
            return self.cells[y][x]
        return BLANK

    def clear(self, x: int, y: int, w: int, h: int) -> None:
        """Blank a rectangle (clipped): the backdrop for text that must stay readable."""
        x0, x1 = max(0, x), min(self.w, x + w)
        if x1 <= x0:
            return
        for yy in range(max(0, y), min(self.h, y + h)):
            self.cells[yy][x0:x1] = [BLANK] * (x1 - x0)

    def text(self, x: int, y: int, s: str, fg: Optional[Color] = None, bold: bool = False) -> int:
        for i, ch in enumerate(s):
            self.put(x + i, y, ch, fg, bold)
        return x + len(s)

    def gtext(self, x: int, y: int, s: str, stops: Sequence[Color] = SPECTRUM, bold: bool = False,
              span: Optional[int] = None, offset: float = 0.0) -> int:
        n = max(1, (span or len(s)) - 1)
        for i, ch in enumerate(s):
            self.put(x + i, y, ch, grad(stops, tri((i / n) + offset)), bold)
        return x + len(s)

    def center(self, y: int, s: str, fg: Optional[Color] = None, bold: bool = False) -> int:
        x = max(0, (self.w - len(s)) // 2)
        self.text(x, y, s, fg, bold)
        return x

    def hline(self, x: int, y: int, w: int, ch: str = "─", fg: Optional[Color] = DUST) -> None:
        for i in range(w):
            self.put(x + i, y, ch, fg)

    def box(self, x: int, y: int, w: int, h: int, fg: Optional[Color] = DUST, title: str = "",
            title_fg: Optional[Color] = None, heavy: bool = False, unicode: bool = True, fill: bool = False) -> None:
        if w < 2 or h < 2:
            return
        if fill:
            self.clear(x + 1, y + 1, w - 2, h - 2)
        if unicode:
            tl, tr, bl, br, hz, vt = ("┏", "┓", "┗", "┛", "━", "┃") if heavy else ("╭", "╮", "╰", "╯", "─", "│")
        else:
            tl, tr, bl, br, hz, vt = "+", "+", "+", "+", "-", "|"
        self.put(x, y, tl, fg)
        self.put(x + w - 1, y, tr, fg)
        self.put(x, y + h - 1, bl, fg)
        self.put(x + w - 1, y + h - 1, br, fg)
        for i in range(1, w - 1):
            self.put(x + i, y, hz, fg)
            self.put(x + i, y + h - 1, hz, fg)
        for j in range(1, h - 1):
            self.put(x, y + j, vt, fg)
            self.put(x + w - 1, y + j, vt, fg)
        if title:
            self.text(x + 2, y, " " + title + " ", title_fg or fg, bold=True)

    def gbox(self, x: int, y: int, w: int, h: int, stops: Sequence[Color], phase: float = 0.0,
             unicode: bool = True, bright: float = 1.0, draw: float = 1.0) -> None:
        """Box whose border carries a gradient that travels around the perimeter.

        draw < 1 traces only that fraction of the border (from the top-left corner both ways),
        which animates a panel "powering on".
        """
        if w < 2 or h < 2:
            return
        self.box(x, y, w, h, None, unicode=unicode)
        perim = [(x + i, y) for i in range(w)] + [(x + w - 1, y + j) for j in range(1, h)] + \
                [(x + w - 1 - i, y + h - 1) for i in range(1, w)] + [(x, y + h - 1 - j) for j in range(1, h - 1)]
        n = len(perim)
        lit = n if draw >= 1 else int(n * clamp01(draw) / 2)
        for k, (px, py) in enumerate(perim):
            ch, _, b = self.get(px, py)
            if draw < 1 and not (k <= lit or n - k <= lit):
                self.put(px, py, " ")
                continue
            t = (k / n + phase) % 1.0
            c = grad(stops, abs(t * 2 - 1))
            self.put(px, py, ch, scale(c, bright), b)


_LIVE_SCREENS: List["Screen"] = []


def _restore_all() -> None:  # atexit: a crash must never leave the alternate screen up
    for scr in list(_LIVE_SCREENS):
        try:
            scr.close()
        except Exception:
            pass


class Screen:
    """Owns the alternate screen and paints canvases with minimal diffs."""

    ENTER = "\x1b[?1049h\x1b[?25l\x1b[2J\x1b[H"
    LEAVE = "\x1b[0m\x1b[2J\x1b[?25h\x1b[?1049l"

    def __init__(self, out=None, depth: int = 24) -> None:
        self.out = out or sys.stderr
        self.ink = Ink(depth)
        self.prev: Optional[Canvas] = None
        self.active = False
        self._unregister: Optional[Callable[[], None]] = None
        self._epoch = 0

    def __enter__(self) -> "Screen":
        self.out.write(self.ENTER)
        self.out.flush()
        self.active = True
        if not _LIVE_SCREENS:
            import atexit
            if not getattr(_restore_all, "_hooked", False):
                atexit.register(_restore_all)
                _restore_all._hooked = True  # type: ignore[attr-defined]
        _LIVE_SCREENS.append(self)
        try:
            from pulsar import term
            self._epoch = term.RESUME_EPOCH
            self._unregister = term.register_screen("\x1b[0m\x1b[?25h\x1b[?1049l", "\x1b[?1049h\x1b[?25l\x1b[2J")
        except Exception:
            self._unregister = None
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self in _LIVE_SCREENS:
            _LIVE_SCREENS.remove(self)
        if self._unregister is not None:
            self._unregister()
            self._unregister = None
        if self.active:
            self.active = False
            try:
                self.out.write(self.LEAVE)
                self.out.flush()
            except (OSError, ValueError):
                pass

    def invalidate(self) -> None:
        self.prev = None

    def present(self, cv: Canvas) -> None:
        try:
            from pulsar import term
            if term.RESUME_EPOCH != self._epoch:      # back from Ctrl-Z: the old frame is gone
                self._epoch = term.RESUME_EPOCH
                self.prev = None
        except Exception:
            pass
        prev = self.prev
        full = prev is None or prev.w != cv.w or prev.h != cv.h
        out: List[str] = []
        if full:
            out.append("\x1b[0m\x1b[2J")
        cur_style = None
        for y in range(cv.h):
            row = cv.cells[y]
            prow = None if full else prev.cells[y]  # type: ignore[union-attr]
            if prow is not None and row == prow:
                continue
            x = 0
            while x < cv.w:
                if prow is not None and row[x] == prow[x]:
                    x += 1
                    continue
                out.append("\x1b[%d;%dH" % (y + 1, x + 1))
                while x < cv.w and (prow is None or row[x] != prow[x] or
                                    (x + 1 < cv.w and row[x + 1] != prow[x + 1])):
                    ch, fg, bold = row[x]
                    style = (fg, bold)
                    if style != cur_style:
                        out.append(self.ink.code(fg, bold) if fg is not None or bold else
                                   ("\x1b[0m" if self.ink.depth else ""))
                        cur_style = style
                    # the last guard: a control character (a newline, ESC, BEL) is never written to the
                    # terminal from a cell, whatever put it there
                    out.append(ch if ch.isprintable() else " ")
                    x += 1
        out.append("\x1b[0m")
        self.out.write("".join(out))
        self.out.flush()
        self.prev = cv


# ---------------------------------------------------------------------------
# Wordmark
# ---------------------------------------------------------------------------

_GLYPHS: Dict[str, List[str]] = {
    "P": ["██████╗ ", "██╔══██╗", "██████╔╝", "██╔═══╝ ", "██║     ", "╚═╝     "],
    "U": ["██╗   ██╗", "██║   ██║", "██║   ██║", "██║   ██║", "╚██████╔╝", " ╚═════╝ "],
    "L": ["██╗     ", "██║     ", "██║     ", "██║     ", "███████╗", "╚══════╝"],
    "S": ["███████╗", "██╔════╝", "███████╗", "╚════██║", "███████║", "╚══════╝"],
    "A": [" █████╗ ", "██╔══██╗", "███████║", "██╔══██║", "██║  ██║", "╚═╝  ╚═╝"],
    "R": ["██████╗ ", "██╔══██╗", "██████╔╝", "██╔══██╗", "██║  ██║", "╚═╝  ╚═╝"],
}


def wordmark() -> List[str]:
    rows = ["".join(_GLYPHS[ch][r] for ch in "PULSAR") for r in range(6)]
    w = len(rows[0])
    label = "  C  O  D  E  "
    side = (w - len(label)) // 2
    rows.append("━" * side + label + "━" * (w - side - len(label)))
    return rows


WORDMARK = wordmark()
WORDMARK_W = len(WORDMARK[0])
WORDMARK_H = len(WORDMARK)

WORDMARK_ASCII = [
    " ___  _   _ _    ___   _   ___ ",
    "| _ \\| | | | |  / __| /_\\ | _ \\",
    "|  _/| |_| | |__\\__ \\/ _ \\|   /",
    "|_|   \\___/|____|___/_/ \\_\\_|_\\",
    "-------- C  O  D  E -----------",
]


def logo_cells(unicode: bool = True) -> List[Tuple[int, int, str, str]]:
    """(x, y, char, role) for every non-space wordmark cell. role: core|shade|rule|label."""
    rows = WORDMARK if unicode else WORDMARK_ASCII
    cells = []
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch == " ":
                continue
            if y == len(rows) - 1:
                role = "label" if ch.isalpha() else "rule"
            elif ch in "█_|/\\":
                role = "core"
            else:
                role = "shade"
            cells.append((x, y, ch, role))
    return cells


def logo_size(unicode: bool = True) -> Tuple[int, int]:
    rows = WORDMARK if unicode else WORDMARK_ASCII
    return len(rows[0]), len(rows)


def logo_color(x: int, y: int, role: str, w: int, t: float = 0.0) -> Tuple[Color, bool]:
    base = grad(SPECTRUM, (x / max(1, w - 1)) * 0.92 + 0.04 * math.sin(t * 1.7 + y * 0.6))
    if role == "core":
        return base, True
    if role == "shade":
        return scale(base, 0.42), False
    if role == "label":
        return ICE, True
    return scale(base, 0.55), False


def draw_logo(cv: Canvas, ox: int, oy: int, unicode: bool = True, t: float = 0.0, shimmer: Optional[float] = None,
              reveal: Optional[Dict[Tuple[int, int], float]] = None, rng: Optional[random.Random] = None,
              glow: float = 0.0, lead: float = 0.45) -> None:
    """Draw the wordmark. `reveal` maps cell->resolve-time for the decrypt effect; `glow` (0..1) whitens it."""
    w, _ = logo_size(unicode)
    rng = rng or random
    noise = DECODE_GLYPHS if unicode else DECODE_ASCII
    for x, y, ch, role in logo_cells(unicode):
        color, bold = logo_color(x, y, role, w, t)
        if reveal is not None:
            tr = reveal.get((x, y), 0.0)
            if t < tr - lead:
                continue
            if t < tr:
                g = rng.choice(noise)
                cv.put(ox + x, oy + y, g, scale(grad(SPECTRUM, rng.random()), 0.5 + 0.5 * rng.random()), False)
                continue
            flash = max(0.0, 1.0 - (t - tr) / 0.18)
            if flash > 0:
                color = lerp(color, ICE, flash)
        if shimmer is not None:
            d = abs((x + y * 0.6) - shimmer)
            if d < 4:
                color = lerp(color, ICE, (1 - d / 4) * 0.85)
        if glow > 0:
            color = lerp(color, ICE, glow)
        cv.put(ox + x, oy + y, ch, color, bold)


def reveal_map(unicode: bool, start: float, span: float, rng: random.Random,
               jitter: float = 0.22) -> Dict[Tuple[int, int], float]:
    w, _ = logo_size(unicode)
    return {(x, y): start + span * (x / max(1, w)) + rng.random() * jitter for x, y, _, _ in logo_cells(unicode)}


def decode_text(cv: Canvas, x: int, y: int, text: str, t: float, start: float, span: float,
                rng: random.Random, stops: Sequence[Color] = SPECTRUM, bold: bool = True, unicode: bool = True,
                offset: float = 0.0, lead: float = 0.25) -> bool:
    """Type `text` in left to right as decrypting glyphs. Returns True once fully resolved."""
    n = len(text)
    noise = DECODE_GLYPHS if unicode else DECODE_ASCII
    done = True
    for i, ch in enumerate(text):
        tr = start + span * (i / max(1, n - 1))
        if t < tr:
            done = False
        if ch == " ":
            continue
        if t < tr - lead:
            continue
        if t < tr:
            cv.put(x + i, y, rng.choice(noise), scale(grad(SPECTRUM, rng.random()), 0.45 + 0.4 * rng.random()))
            continue
        col = grad(stops, tri(i / max(1, n - 1) + offset))
        fl = 1.0 - (t - tr) / 0.16
        if fl > 0:
            col = lerp(col, ICE, fl)
        cv.put(x + i, y, ch, col, bold)
    return done


# ---------------------------------------------------------------------------
# Effects
# ---------------------------------------------------------------------------

class Rain:
    """Spectrum glyph rain. Column hue follows the brand gradient."""

    def __init__(self, w: int, h: int, rng: random.Random, unicode: bool = True, density: float = 0.55) -> None:
        self.w, self.h, self.rng = w, h, rng
        self.glyphs = RAIN_GLYPHS if unicode else RAIN_ASCII
        self.density = density
        self.drops: List[List[float]] = []   # [x, head_y, speed, length]
        self.spawning = True
        self.trail: Dict[Tuple[int, int], str] = {}
        for _ in range(int(w * density * 0.5)):
            self._spawn(initial=True)

    def _spawn(self, initial: bool = False) -> None:
        x = self.rng.randrange(0, self.w)
        y = self.rng.uniform(-self.h, 0) if initial else self.rng.uniform(-8, 0)
        self.drops.append([x, y, self.rng.uniform(14, 38), self.rng.randint(4, max(5, self.h // 2))])

    def update(self, dt: float) -> None:
        for d in self.drops:
            d[1] += d[2] * dt
        self.drops = [d for d in self.drops if d[1] - d[3] < self.h]
        if self.spawning:
            target = int(self.w * self.density * 0.5)
            while len(self.drops) < target:
                self._spawn()
        if self.rng.random() < 0.6 and self.trail:
            for key in self.rng.sample(list(self.trail), min(len(self.trail), 6)):
                self.trail[key] = self.rng.choice(self.glyphs)

    def draw(self, cv: Canvas, mask=None, fade: float = 1.0) -> None:
        if fade <= 0.02:
            return
        for x, head, _, length in self.drops:
            xi = int(x)
            hue = grad(SPECTRUM, xi / max(1, self.w - 1))
            for k in range(int(length)):
                y = int(head) - k
                if y < 0 or y >= self.h or (mask and mask(xi, y)):
                    continue
                key = (xi, y)
                if key not in self.trail or self.rng.random() < 0.04:
                    self.trail[key] = self.rng.choice(self.glyphs)
                if k == 0:
                    c, b = scale(ICE, fade), True
                else:
                    f = (1 - k / length) ** 1.6
                    c, b = scale(hue, (0.25 + 0.75 * f) * fade), False
                cv.put(xi, y, self.trail[key], c, b)
        if len(self.trail) > self.w * self.h:
            self.trail.clear()


def rings(cv: Canvas, cx: int, cy: int, t: float, period: float = 0.9, count: int = 3, unicode: bool = True,
          max_r: Optional[float] = None) -> None:
    """Concentric pulses emitted from a core (the pulsar beacon)."""
    max_r = max_r or max(cv.w, cv.h * 2) * 0.6
    chars = "·∙•" if unicode else ".:o"
    for i in range(count):
        p = ((t / period) + i / count) % 1.0
        r = p * max_r
        if r < 1:
            continue
        fade = (1 - p) ** 1.4
        steps = int(r * 7) + 12
        for s in range(steps):
            a = 2 * math.pi * s / steps
            x = int(round(cx + math.cos(a) * r))
            y = int(round(cy + math.sin(a) * r * 0.5))
            cv.put(x, y, chars[min(2, int(fade * 3))], scale(grad((BEAM, PLASMA, FLARE), p), fade), False)
    core = "◉" if unicode else "@"
    cv.put(cx, cy, core, lerp(ICE, BEAM, 0.5 + 0.5 * math.sin(t * 9)), True)


def sweep(cv: Canvas, cx: int, cy: int, r: int, t: float, unicode: bool = True, color: Color = BEAM) -> None:
    """Sonar sweep: a rotating beam with a fading wake, plus range rings."""
    for rr in range(2, r + 1, max(2, r // 3)):
        steps = rr * 8
        for s in range(steps):
            a = 2 * math.pi * s / steps
            cv.put(int(round(cx + math.cos(a) * rr * 2)), int(round(cy + math.sin(a) * rr)),
                   "·" if unicode else ".", NIGHT)
    angle = t * 4.2
    for k in range(18):
        a = angle - k * 0.07
        fade = (1 - k / 18) ** 1.5
        for d in range(1, r + 1):
            x = int(round(cx + math.cos(a) * d * 2))
            y = int(round(cy + math.sin(a) * d))
            ch = "█" if k == 0 and unicode else ("▓" if k < 3 and unicode else "░" if unicode else "*")
            cv.put(x, y, ch, scale(color, fade * (0.4 + 0.6 * d / r)), k == 0)
    cv.put(cx, cy, "◉" if unicode else "@", ICE, True)


class Stars:
    """Sparse twinkling starfield for quiet backgrounds."""

    def __init__(self, w: int, h: int, rng: random.Random, n: Optional[int] = None) -> None:
        self.rng = rng
        self.w, self.h = w, h
        self.stars = [[rng.uniform(0, w), rng.uniform(0, h), rng.uniform(0, 6.28), rng.uniform(0.3, 1.6)]
                      for _ in range(n if n is not None else max(8, w * h // 70))]

    def draw(self, cv: Canvas, t: float, unicode: bool = True) -> None:
        for x, y, ph, sp in self.stars:
            x2 = (x - t * sp * 0.6) % self.w
            tw = 0.5 + 0.5 * math.sin(t * 2.2 * sp + ph)
            if tw < 0.15:
                continue
            ch = ("·" if tw < 0.7 else "∙") if unicode else "."
            cv.put(int(x2), int(y), ch, scale(lerp(NIGHT, ASH, tw), 0.9))


def warp(cv: Canvas, t: float, rng: random.Random, stars: List[List[float]], unicode: bool = True) -> None:
    """Hyperspace jump: stars stream outward from the center with streaks."""
    cx, cy = cv.w / 2, cv.h / 2
    speed = 0.6 + t * 9
    for s in stars:
        ang, dist, hue = s
        d0 = dist * (1 + speed * 0.35) ** 2
        d1 = d0 * (1 + 0.08 * speed)
        for k in range(6):
            dd = d0 + (d1 - d0) * k / 5
            x = int(cx + math.cos(ang) * dd * 2)
            y = int(cy + math.sin(ang) * dd)
            if 0 <= x < cv.w and 0 <= y < cv.h:
                f = 0.35 + 0.65 * (k / 5)
                cv.put(x, y, ("━" if abs(math.cos(ang)) > 0.7 else "┃" if abs(math.cos(ang)) < 0.3 else "∙")
                       if unicode else "-", scale(grad(SPECTRUM, hue), f), k == 5)


def warp_stars(rng: random.Random, n: int = 90) -> List[List[float]]:
    return [[rng.uniform(0, 2 * math.pi), rng.uniform(0.5, 4.0), rng.random()] for _ in range(n)]


def flare(cv: Canvas, cx: int, cy: int, k: float, unicode: bool = True, color: Color = BEAM,
          length: Optional[int] = None, vertical: bool = True) -> None:
    """Anamorphic lens flare: a horizontal streak (and, unless vertical=False, a short vertical one)
    through a hot core."""
    if k <= 0.01:
        return
    span = max(2, length if length is not None else cv.w // 2)
    for dx in range(-span, span + 1):
        f = (1 - abs(dx) / (span + 1.0)) ** 2.2 * k
        if f < 0.05:
            continue
        ch = ("━" if f > 0.55 else "─" if f > 0.2 else "·") if unicode else ("=" if f > 0.55 else "-" if f > 0.2 else ".")
        cv.put(cx + dx, cy, ch, lerp(scale(color, 0.35 + 0.65 * f), ICE, f * f), f > 0.6)
    for dy in (-2, -1, 1, 2) if vertical else ():
        f = (1 - abs(dy) / 3.0) * k * 0.8
        if f > 0.08:
            cv.put(cx, cy + dy, "│" if unicode else "|", lerp(scale(color, f), ICE, f * 0.5), False)
    cv.put(cx, cy, "◉" if unicode else "@", lerp(BEAM, ICE, min(1.0, k * 1.4)), True)


def scanline(cv: Canvas, pos: float, box: Optional[Tuple[int, int, int, int]] = None, color: Color = BEAM,
             strength: float = 1.0, unicode: bool = True, line: bool = True, trail: float = 2.5) -> None:
    """A bright line raking down a region: glyphs it crosses flare toward ICE, a faint trail follows.

    box is (x0, y0, x1, y1), end-exclusive; pos is the line's row (float).
    """
    x0, y0, x1, y1 = box or (0, 0, cv.w, cv.h)
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(cv.w, x1), min(cv.h, y1)
    if x1 <= x0 or strength <= 0:
        return
    lead = int(round(pos))
    for y in range(max(y0, int(math.floor(pos - trail))), min(y1, lead + 1)):
        d = pos - y                      # rows behind the line (0 = on it)
        k = (1 - d / (trail + 0.001)) if d >= 0 else (1 + d)
        k = clamp01(k) * strength
        if k <= 0.02:
            continue
        row = cv.cells[y]
        on_line = line and y == lead
        glyph = "─" if unicode else "-"
        lc = scale(color, 0.5 * strength)
        for x in range(x0, x1):
            ch, fg, b = row[x]
            if ch != " ":
                row[x] = (ch, lerp(fg or ASH, ICE, k * 0.85), b)
            elif on_line:
                row[x] = (glyph, lc, False)


class Glitch:
    """Short bursts of horizontal slice displacement with a chromatic split.

    Bursts are (start, duration, strength) in seconds; level(t) is 0 outside a burst.
    """

    def __init__(self, rng: random.Random, bursts: Optional[Sequence[Tuple[float, float, float]]] = None) -> None:
        self.rng = rng
        self.bursts: List[Tuple[float, float, float]] = list(bursts or [])

    def add(self, start: float, dur: float = 0.08, strength: float = 1.0) -> None:
        self.bursts.append((start, dur, strength))

    def level(self, t: float) -> float:
        lv = 0.0
        for start, dur, k in self.bursts:
            if start <= t < start + dur:
                lv = max(lv, k * (1.0 - 0.5 * (t - start) / dur))
        return lv

    def apply(self, cv: Canvas, box: Tuple[int, int, int, int], t: float, unicode: bool = True,
              split: Tuple[Color, Color] = (BEAM, FLARE), ghost: bool = True, scale_by: float = 1.0) -> float:
        lv = self.level(t) * scale_by
        if lv > 0:
            glitch(cv, box, lv, self.rng, unicode, split, ghost)
        return lv


def glitch(cv: Canvas, box: Tuple[int, int, int, int], amount: float, rng: random.Random, unicode: bool = True,
           split: Tuple[Color, Color] = (BEAM, FLARE), ghost: bool = True) -> None:
    """One glitch frame over box (x0, y0, x1, y1, end-exclusive): some rows slide sideways, glyph edges
    fringe in two split colors (ghost=False keeps text legible: no fringes), and a few cells turn to noise."""
    x0, y0, x1, y1 = max(0, box[0]), max(0, box[1]), min(cv.w, box[2]), min(cv.h, box[3])
    n = x1 - x0
    if n <= 1 or y1 <= y0 or amount <= 0:
        return
    noise = DECODE_GLYPHS if unicode else DECODE_ASCII
    bands = max(1, int(round((y1 - y0) * (0.2 + 0.45 * min(1.0, amount)))))
    for _ in range(bands):
        y = rng.randrange(y0, y1)
        thick = 1 if rng.random() < 0.7 else 2
        shift = rng.choice((-1, 1)) * max(1, int(round(rng.uniform(1, 2 + 4 * min(1.0, amount)))))
        tint = split[0] if shift > 0 else split[1]
        for yy in range(y, min(y1, y + thick)):
            row = cv.cells[yy]
            seg = row[x0:x1]
            new: List[Cell] = [BLANK] * n
            for i, cell in enumerate(seg):
                j = i + shift
                if 0 <= j < n and cell[0] != " ":
                    fg = cell[1]
                    new[j] = (cell[0], lerp(fg, tint, 0.35 * amount) if fg else fg, cell[2])
            # Chromatic split: the opposite fringe color trails where the slice used to be.
            sign = 1 if shift > 0 else -1
            for i, cell in enumerate(seg if ghost else ()):
                if cell[0] == " ":
                    continue
                for off, col in ((-sign, split[1] if sign > 0 else split[0]), (shift + sign, tint)):
                    j = i + off
                    if 0 <= j < n and new[j][0] == " ":
                        new[j] = (cell[0], scale(col, 0.75), False)
            if ghost and rng.random() < 0.55 * amount:
                start = rng.randrange(0, n)
                for j in range(start, min(n, start + rng.randint(2, 6))):
                    if new[j][0] != " " or rng.random() < 0.4:
                        new[j] = (rng.choice(noise), scale(grad(SPECTRUM, rng.random()), 0.8), False)
            row[x0:x1] = new


BEAM_GLOW = "·∙•"
BEAM_GLOW_ASCII = ".:o"


def ray_glyph(angle: float, unicode: bool = True, heavy: bool = False) -> str:
    """The box-drawing stroke that best follows a ray at `angle` (radians, y down, aspect-corrected)."""
    c = abs(math.cos(angle))
    if c > 0.9:
        return ("━" if heavy else "─") if unicode else "-"
    if c < 0.4:
        return ("┃" if heavy else "│") if unicode else "|"
    if math.cos(angle) * math.sin(angle) > 0:
        return "╲" if unicode else "\\"
    return "╱" if unicode else "/"


class Beams:
    """Two counter-rotating pulsar beams, lighthouse style, drawn with gradient glyph density.

    Each beam is bipolar (it leaves the core in both directions, like a real pulsar): a hot ray
    drawn with direction-aware strokes, a soft glow that widens with distance (a cone of light)
    and a fading wake of sparks behind its rotation. Where the beams cross, both flare. Cell
    polar coordinates are precomputed once per size, so a frame is one pass over the lit area.
    """

    def __init__(self, w: int, h: int, cx: float, cy: float, radius: Optional[float] = None,
                 region: Optional[Tuple[int, int, int, int]] = None, aspect: float = 2.0, step: int = 1) -> None:
        self.w, self.h, self.cx, self.cy = w, h, cx, cy
        self.radius = radius or max(w * 0.62, h * aspect * 0.62)
        x0, y0, x1, y1 = region or (0, 0, w, h)
        self.cells: List[Tuple[int, int, float, float, float]] = []   # x, y, angle, r (cells), r/R
        rad = self.radius
        for y in range(max(0, y0), min(h, y1)):
            dy = (y - cy) * aspect
            for x in range(max(0, x0), min(w, x1), max(1, step)):
                dx = x - cx
                r = math.hypot(dx, dy)
                if 1.2 <= r <= rad:
                    self.cells.append((x, y, math.atan2(dy, dx), r, r / rad))

    @staticmethod
    def angles(t: float, speed: float = 1.0) -> Tuple[float, float]:
        return t * 1.25 * speed + 0.35, -t * 0.95 * speed + 2.1

    def draw(self, cv: Canvas, t: float, amount: float = 1.0, unicode: bool = True, speed: float = 1.0,
             wake: bool = True, mask: Optional[Callable[[int, int], bool]] = None,
             colors: Tuple[Sequence[Color], Sequence[Color]] = ((ICE, BEAM, PLASMA), (ICE, FLARE, PLASMA)),
             over: bool = False) -> None:
        """Paint the beams. over=False only lights blank cells (the beams pass *behind* everything)."""
        if amount <= 0.01:
            return
        pi = math.pi
        half = pi / 2
        sin = math.sin
        exp = math.exp
        a1, a2 = self.angles(t, speed)
        cross = (a1 - a2) % pi
        cross = min(cross, pi - cross)
        boost = 1.0 + 0.7 * exp(-(cross * cross) / 0.015)
        glow = BEAM_GLOW if unicode else BEAM_GLOW_ASCII
        strokes = ((ray_glyph(a1, unicode), ray_glyph(a1, unicode, True)),
                   (ray_glyph(a2, unicode), ray_glyph(a2, unicode, True)))
        # A horizontal ray must catch a whole row (rows are 2 x-units tall); a vertical one a column.
        core_w = (0.55 + 0.5 * abs(math.cos(a1)), 0.55 + 0.5 * abs(math.cos(a2)))
        cells = cv.cells
        stops = colors
        for x, y, ang, r, rn in self.cells:
            if not over and cells[y][x][0] != " ":
                continue
            if mask is not None and mask(x, y):
                continue
            radial = (1.0 - rn) ** 1.1
            cone = 0.9 + rn * 6.0                   # glow half-width in cells: widens outward
            best = 0.0
            which = 0
            core = False
            for k in (0, 1):
                a = a1 if k == 0 else a2
                s = ((ang - a + half) % pi) - half  # signed angular offset to this beam (either end)
                perp = sin(s) * r
                if perp < 0:
                    perp = -perp
                if perp < cone:
                    i = 1.0 - perp / cone
                    i = i * i * (1.0 if perp > core_w[k] else 1.35)
                else:
                    i = 0.0
                if wake:
                    trail = s if k == 0 else -s     # beam 1 turns toward +angle, beam 2 the other way
                    if trail < 0:
                        wv = 0.30 * exp(trail / 0.42)
                        if wv > i:
                            i = wv
                if i > best:
                    best, which, core = i, k, perp <= core_w[k]
            inten = best * radial * amount * boost
            if inten < 0.07:
                continue
            col = grad(stops[which], rn * 1.25)
            if core and inten > 0.18:
                hot = inten > 0.55
                cells[y][x] = (strokes[which][1 if hot else 0],
                               lerp(scale(col, 0.45 + 0.55 * min(1.0, inten)), ICE, min(0.6, inten * 0.5)), hot)
                continue
            ch = glow[0] if inten < 0.2 else glow[1] if inten < 0.42 else glow[2]
            cells[y][x] = (ch, scale(col, 0.3 + 0.6 * min(1.0, inten)), False)


def shockwave(cv: Canvas, cx: float, cy: float, r: float, k: float, unicode: bool = True,
              stops: Sequence[Color] = (ICE, BEAM, PLASMA)) -> None:
    """One expanding ring (aspect-corrected); k in 0..1 is its remaining energy."""
    if r < 0.5 or k <= 0.02:
        return
    steps = int(r * 9) + 16
    chars = ("•", "∙", "·") if unicode else ("o", ":", ".")
    ch = chars[0] if k > 0.6 else chars[1] if k > 0.3 else chars[2]
    col = scale(grad(stops, 1.0 - k), 0.3 + 0.7 * k)
    seen = set()
    for s in range(steps):
        a = 2 * math.pi * s / steps
        x = int(round(cx + math.cos(a) * r))
        y = int(round(cy + math.sin(a) * r * 0.5))
        if (x, y) in seen:
            continue
        seen.add((x, y))
        cv.put(x, y, ch, col, k > 0.75)


class Particles:
    """Sparks: velocity, drag, gravity, and a glyph and color that cool as they age."""

    def __init__(self, rng: random.Random, gravity: float = 24.0, drag: float = 1.4, cap: int = 900) -> None:
        self.rng = rng
        self.gravity = gravity
        self.drag = drag
        self.cap = cap
        self.p: List[List] = []     # [x, y, vx, vy, life, max_life, color]

    def __len__(self) -> int:
        return len(self.p)

    def emit(self, x: float, y: float, vx: float, vy: float, life: float, color: Color) -> None:
        if len(self.p) < self.cap:
            self.p.append([x, y, vx, vy, life, life, color])

    def burst(self, x: float, y: float, n: int, speed: Tuple[float, float] = (10.0, 34.0),
              colors: Sequence[Color] = SPECTRUM, life: Tuple[float, float] = (0.45, 1.1), up: float = 0.35,
              angle: Optional[float] = None, spread: float = 2 * math.pi) -> None:
        rng = self.rng
        for _ in range(max(0, n)):
            a = rng.uniform(0, 2 * math.pi) if angle is None else angle + rng.uniform(-spread / 2, spread / 2)
            sp = rng.uniform(*speed)
            vx = math.cos(a) * sp
            vy = math.sin(a) * sp * 0.5 - up * sp * 0.4
            self.emit(x, y, vx, vy, rng.uniform(*life), colors[rng.randrange(len(colors))])

    def update(self, dt: float) -> None:
        if not self.p:
            return
        keep = []
        damp = max(0.0, 1.0 - self.drag * dt)
        g = self.gravity * dt
        for q in self.p:
            q[2] *= damp
            q[3] = q[3] * damp + g
            q[0] += q[2] * dt
            q[1] += q[3] * dt
            q[4] -= dt
            if q[4] > 0 and -20 < q[0] < 2000 and q[1] < 1000:
                keep.append(q)
        self.p = keep

    def draw(self, cv: Canvas, unicode: bool = True, mask: Optional[Callable[[int, int], bool]] = None) -> None:
        heads = ("*", "•", "∙", "·") if unicode else ("*", "o", "+", ".")
        for x, y, vx, vy, life, mx, color in self.p:
            k = life / mx
            xi, yi = int(round(x)), int(round(y))
            if mask is not None and mask(xi, yi):
                continue
            ch = heads[0] if k > 0.8 else heads[1] if k > 0.55 else heads[2] if k > 0.28 else heads[3]
            col = scale(lerp(color, ICE, k * k * 0.9), 0.35 + 0.65 * k)
            tx, ty = int(round(x - vx * 0.035)), int(round(y - vy * 0.035))
            if (tx, ty) != (xi, yi) and cv.get(tx, ty)[0] == " " and not (mask is not None and mask(tx, ty)):
                cv.put(tx, ty, heads[3], scale(color, 0.25 + 0.4 * k))
            cv.put(xi, yi, ch, col, k > 0.62)


def bar(cv: Canvas, x: int, y: int, w: int, frac: float, t: float = 0.0, unicode: bool = True,
        stops: Sequence[Color] = SPECTRUM, shimmer: bool = True, empty: Color = NIGHT, bright: float = 1.0,
        flash: float = 0.0, head: bool = True) -> None:
    """Gradient progress bar: a heavy rule filled to frac with sub-cell precision, a shimmer that
    travels along the filled part, a hot head at the fill front, and a faint empty track."""
    if w <= 0:
        return
    frac = clamp01(frac)
    fill = frac * w
    full = int(fill)
    part = fill - full
    on_c, half_c, off_c = ("━", "╸", "─") if unicode else ("=", "-", ".")
    sh = ((t * 1.4) % 1.6 - 0.3) * max(1, full) if shimmer else -99.0
    for i in range(w):
        if i < full or (i == full and part >= 0.5):
            col = grad(stops, i / max(1, w - 1))
            if shimmer:
                d = abs(i - sh)
                if d < 3:
                    col = lerp(col, ICE, (1 - d / 3) * 0.7)
            if head and frac < 1 and i >= full - 1:
                col = lerp(col, ICE, 0.55)
            if flash > 0:
                col = lerp(col, ICE, flash)
            ch = on_c if i < full else half_c
            cv.put(x + i, y, ch, scale(col, bright), i >= full - 1 and frac < 1)
        else:
            cv.put(x + i, y, off_c, empty)


class FrameBudget:
    """Keeps animations smooth on any machine and link.

    Starts from a level ('full', 'lite' or 'minimal', usually hardware.fx_budget()), measures
    each frame's cost, and lowers `quality` when frames run long (raising it back slowly when
    there is headroom). Effects ask on() whether they may run and count() how dense to be.
    """

    BASE = {"full": 1.0, "lite": 0.55, "minimal": 0.1}
    FPS = {"full": 30, "lite": 24, "minimal": 15}
    NEED = {"rain": 0.15, "particles": 0.2, "glitch": 0.25, "scanline": 0.2, "beams": 0.3, "wake": 0.6,
            "flare": 0.15}

    def __init__(self, level: str = "full", fps: Optional[int] = None) -> None:
        self.level = level if level in self.BASE else "full"
        self.base = self.BASE[self.level]
        self.quality = self.base
        self.fps = fps or self.FPS[self.level]
        self.period = 1.0 / self.fps
        self.cost = 0.0
        self.frames = 0
        self.slow = 0
        self._t = time.perf_counter()

    def retarget(self, level: str) -> None:
        """Switch level mid-run (e.g. once the hardware probe lands); quality never jumps up."""
        if level not in self.BASE or level == self.level:
            return
        self.level = level
        self.base = self.BASE[level]
        self.quality = min(self.quality, self.base)
        self.fps = self.FPS[level]
        self.period = 1.0 / self.fps

    def begin(self) -> float:
        self._t = time.perf_counter()
        return self._t

    def end(self) -> float:
        dt = time.perf_counter() - self._t
        self.record(dt)
        return dt

    def record(self, dt: float) -> None:
        self.frames += 1
        self.cost = dt if self.frames == 1 else self.cost * 0.8 + dt * 0.2
        if self.frames <= 3:
            return
        if self.cost > self.period * 0.75:
            self.quality = max(0.12, self.quality * 0.85)
            self.slow += 1
        elif self.cost < self.period * 0.35 and self.quality < self.base:
            self.quality = min(self.base, self.quality * 1.03 + 0.004)

    def remaining(self) -> float:
        return max(0.0, self.period - (time.perf_counter() - self._t))

    def count(self, n: float) -> int:
        return max(0, int(round(n * self.quality)))

    def on(self, effect: str) -> bool:
        return self.quality >= self.NEED.get(effect, 0.0)


# ---------------------------------------------------------------------------
# Haptics: tactile feedback a terminal can actually deliver
# ---------------------------------------------------------------------------

class Haptics:
    """Visual pulses on every action, a shake on errors, and an optional bell.

    PULSAR_HAPTICS=on (default) visual only, =bell adds the terminal bell
    (which many terminals turn into a dock bounce or a system haptic tick),
    =off disables.
    """

    SHAKE = (2, -2, 2, -1, 1, 0)

    def __init__(self, out=None, mode: str = "on") -> None:
        self.out = out or sys.stderr
        self.mode = mode
        self.flash = 0.0
        self.flash_color: Color = BEAM
        self._shake: List[int] = []

    def _bell(self) -> None:
        if self.mode == "bell":
            self.out.write("\a")
            self.out.flush()

    def tap(self) -> None:
        if self.mode != "off":
            self.flash = max(self.flash, 0.35)
            self.flash_color = BEAM

    def confirm(self) -> None:
        if self.mode != "off":
            self.flash = 1.0
            self.flash_color = OK
            self._bell()

    def error(self) -> None:
        if self.mode != "off":
            self.flash = 1.0
            self.flash_color = BAD
            self._shake = list(self.SHAKE)
            self._bell()

    def pulse(self, color: Color = BEAM, strength: float = 0.7) -> None:
        """A silent visual pulse (no bell): for moments the user did not cause."""
        if self.mode != "off":
            self.flash = max(self.flash, strength)
            self.flash_color = color

    def update(self, dt: float) -> None:
        self.flash = max(0.0, self.flash - dt * 3.2)

    def offset(self) -> int:
        return self._shake.pop(0) if self._shake else 0
