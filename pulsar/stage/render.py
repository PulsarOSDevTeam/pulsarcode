# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Pulsar Stage terminal layout kit.

Pure drawing helpers shared by the animated viewer and the plain renderer.
Every function draws into a rectangular region of an fx.Canvas through a
clipped View, so nothing lands outside its box, and everything degrades to
ASCII (View.u False), tiny regions and sparse data.

  text       display-width aware writing, wrapping, clipping, ASCII folding
  markdown   markdown-lite -> styled lines (headings, bullets, **bold**, `code`)
  big type   a block font (A-Z 0-9 punctuation) in compact/tall/wide sizes
  charts     braille line charts, spectrum bars, sparklines, nice axes
  tables     box-drawn tables with column fitting and truncation
  diagrams   layered graph layout (longest-path ranks, barycenter order)
             with orthogonal routed edges and inline labels
  code       line-numbered panels with light syntax highlighting
  panels     metric tiles, timeline, progress steps, cards, slides

`t` is seconds since the visual appeared (drives intro animations; the
default renders the settled frame) and `motion` enables loops such as
spinners and edge pulses.
"""

from __future__ import annotations

import math
import random
import re
import unicodedata
from functools import lru_cache
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pulsar import fx
from pulsar.fx import ASH, BAD, BEAM, DUST, EMBER, FLARE, ICE, NIGHT, OK, PLASMA, SPECTRUM, WARN, Color

TEXT: Color = (214, 218, 234)
SOFT: Color = (170, 166, 194)
FAINT: Color = (58, 54, 78)
INLINE_CODE: Color = (126, 222, 255)
EMPH: Color = (198, 178, 255)
SERIES: Tuple[Color, ...] = (BEAM, FLARE, EMBER, PLASMA, OK, WARN, (255, 140, 200), (120, 170, 255))
SETTLED = 99.0

Seg = Tuple[str, Optional[Color], bool]
Line = List[Seg]

TONES: Dict[str, Tuple[Color, Color]] = {
    "info": (BEAM, PLASMA), "success": (OK, BEAM), "warn": (WARN, EMBER),
    "error": (BAD, FLARE), "neutral": (ASH, DUST),
}
TONE_TAG = {"info": ("◉", "@", "INFO"), "success": ("✓", "+", "SUCCESS"), "warn": ("▲", "!", "WARNING"),
            "error": ("✗", "x", "ERROR"), "neutral": ("•", "*", "NOTE")}
KIND_ICON = {"deck": ("◧", "#"), "card": ("◈", "*"), "markdown": ("¶", "="), "code": ("❮❯", "<>"),
             "table": ("▦", "#"), "chart": ("▥", "|"), "diagram": ("◇", "o"), "metrics": ("◔", "%"),
             "timeline": ("┆", ":"), "progress": ("◌", "~")}


def ease(p: float) -> float:
    p = 0.0 if p < 0 else 1.0 if p > 1 else p
    return 1 - (1 - p) ** 3


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


# ---------------------------------------------------------------------------
# Text: width, ASCII folding, clipping, wrapping
# ---------------------------------------------------------------------------

_WIDE_SEEN = [False]


@lru_cache(maxsize=8192)
def cwidth(ch: str) -> int:
    """Terminal cell width of one code point: 0 (combining/format), 1 or 2 (wide).

    0 also covers what terminals disagree about, so the writers (which skip width-0 code points) never
    send it to the tty: line and paragraph separators (Zl, Zp),
    private-use (Co) and unassigned (Cn) code points, and the emoji skin-tone modifiers U+1F3FB..U+1F3FF
    (2 cells alone, 0 after an emoji, depending on the terminal).
    """
    o = ord(ch)
    if o < 0x300:
        return 1 if o >= 32 and o != 0x7F and not 0x80 <= o <= 0x9F else 0
    if o in (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF) or 0xFE00 <= o <= 0xFE0F or 0xE0100 <= o <= 0xE01EF:
        return 0
    if 0x1F3FB <= o <= 0x1F3FF:
        return 0
    if 0x1F300 <= o <= 0x1FAFF:
        return 2
    cat = unicodedata.category(ch)
    if unicodedata.combining(ch) or cat in ("Mn", "Me", "Cf", "Cc", "Cs", "Zl", "Zp", "Co", "Cn"):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


# ASCII control characters (C0 and DEL) drawn as a space by View.text's ASCII fast path; the other writers skip
# them through cwidth() == 0.
_C0_SPACE = {i: " " for i in list(range(0x20)) + [0x7F]}


def swidth(s: str) -> int:
    if s.isascii():
        return len(s)
    return sum(cwidth(c) for c in s)


_ASCII = {
    "“": '"', "”": '"', "‘": "'", "’": "'", "\u2014": "-", "\u2013": "-", "…": "...",
    "•": "*", "·": ".", "→": "->", "←": "<-", "↑": "^", "↓": "v", "▶": ">", "◀": "<", "▸": ">", "◂": "<",
    "✓": "v", "✗": "x", "×": "x", "≤": "<=", "≥": ">=", "≠": "!=", "±": "+-", "°": "o", "€": "EUR",
    "£": "GBP", "¥": "JPY", "©": "(c)", "®": "(R)", "™": "TM", " ": " ", "◉": "@", "●": "o", "○": "o",
    "▲": "^", "▼": "v", "★": "*", "⚡": "!", "λ": "L", "Σ": "S", "Δ": "D", "π": "pi", "µ": "u", "μ": "u",
}


def asc(s: str) -> str:
    """Fold text to printable ASCII for terminals without UTF-8."""
    if s.isascii():
        return s
    out = []
    for ch in s:
        if ord(ch) < 128:
            out.append(ch)
            continue
        m = _ASCII.get(ch)
        if m is None:
            if cwidth(ch) == 0:
                continue
            m = unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode("ascii") or "?"
        out.append(m)
    return "".join(out)


def prep(s: Any, u: bool = True) -> str:
    s = "" if s is None else str(s)
    if not s.isascii():
        s = unicodedata.normalize("NFC", s)
        if not u:
            s = asc(s)
    return s


def ellipsis(u: bool) -> str:
    return "…" if u else "~"


def clip(s: str, w: int, u: bool = True) -> str:
    """Truncate to at most w cells, ending in an ellipsis when cut."""
    s = prep(s, u)
    if w <= 0:
        return ""
    if swidth(s) <= w:
        return s
    ell = ellipsis(u)
    out, used = [], 0
    for ch in s:
        cw = cwidth(ch)
        if used + cw > w - 1:
            break
        out.append(ch)
        used += cw
    return "".join(out).rstrip() + ell


def wrap(s: str, w: int, u: bool = True) -> List[str]:
    """Width-aware word wrap; hard-breaks words longer than the line."""
    s = prep(s, u)
    w = max(1, w)
    lines: List[str] = []
    for para in s.split("\n"):
        cur, cw = "", 0
        for word in para.split(" "):
            if not word:
                continue
            ww = swidth(word)
            if cur and cw + 1 + ww > w:
                lines.append(cur)
                cur, cw = "", 0
            if ww > w:
                for ch in word:
                    c = cwidth(ch)
                    if cw + c > w:
                        lines.append(cur)
                        cur, cw = "", 0
                    cur += ch
                    cw += c
                continue
            cur = cur + " " + word if cur else word
            cw = cw + 1 + ww if cw else ww
        lines.append(cur)
    return lines


# ---------------------------------------------------------------------------
# View: a clipped window onto a Canvas
# ---------------------------------------------------------------------------

_ASCII_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class View:
    """Local coordinates over a Canvas region; writes outside the region are dropped.

    Wide (CJK, emoji) glyphs take two cells: the glyph plus an empty-string
    placeholder, which the diff renderer writes as nothing.
    """

    __slots__ = ("cv", "ox", "oy", "w", "h", "clip", "u")

    def __init__(self, cv: Any, x: int = 0, y: int = 0, w: Optional[int] = None, h: Optional[int] = None,
                 u: Optional[bool] = None) -> None:
        if isinstance(cv, View):
            base, ox, oy, pc = cv.cv, cv.ox + x, cv.oy + y, cv.clip
            w = cv.w - x if w is None else w
            h = cv.h - y if h is None else h
            self.u = cv.u if u is None else u
        else:
            base, ox, oy, pc = cv, x, y, (0, 0, cv.w, cv.h)
            w = cv.w - x if w is None else w
            h = cv.h - y if h is None else h
            self.u = True if u is None else u
        self.cv, self.ox, self.oy = base, ox, oy
        self.w, self.h = max(0, int(w)), max(0, int(h))
        self.clip = (max(pc[0], ox), max(pc[1], oy), min(pc[2], ox + self.w), min(pc[3], oy + self.h))

    def sub(self, x: int, y: int, w: int, h: int) -> "View":
        return View(self, x, y, w, h)

    def put(self, x: int, y: int, ch: str, fg: Optional[Color] = None, bold: bool = False) -> None:
        ax, ay = self.ox + x, self.oy + y
        c = self.clip
        if c[0] <= ax < c[2] and c[1] <= ay < c[3]:
            row = self.cv.cells[ay]
            if ch != "":
                if row[ax][0] == "" and ax > 0:
                    p = row[ax - 1]
                    row[ax - 1] = (" ", p[1], p[2])
                if ax + 1 < len(row) and row[ax + 1][0] == "":
                    n = row[ax + 1]
                    row[ax + 1] = (" ", n[1], n[2])
            row[ax] = (ch, fg, bold)

    def get(self, x: int, y: int) -> Tuple[str, Optional[Color], bool]:
        ax, ay = self.ox + x, self.oy + y
        c = self.clip
        if c[0] <= ax < c[2] and c[1] <= ay < c[3]:
            return self.cv.cells[ay][ax]
        return fx.BLANK

    def blank(self, x: int, y: int) -> bool:
        return self.get(x, y)[0] == " "

    def text(self, x: int, y: int, s: str, fg: Optional[Color] = None, bold: bool = False,
             maxw: Optional[int] = None) -> int:
        if not s:
            return x
        if not s.isascii():
            s = prep(s, self.u)
        limit = self.w if maxw is None else min(self.w, x + maxw)
        if s.isascii():
            n = limit - x
            if n <= 0:
                return x
            if len(s) > n:
                s = s[:n]
            if not s.isprintable():
                # A control character in a cell reaches the tty as is ("\n" moves the cursor down a row):
                # it becomes a space, which keeps the width swidth() (len) promised to the caller.
                s = s.translate(_C0_SPACE)
            for i, ch in enumerate(s):
                self.put(x + i, y, ch, fg, bold)
            return x + len(s)
        cx = x
        for ch in s:
            cw = cwidth(ch)
            if cw == 0:
                continue
            if cx + cw > limit:
                break
            self.put(cx, y, ch, fg, bold)
            if cw == 2:
                self.put(cx + 1, y, "", fg, bold)
                _WIDE_SEEN[0] = True
            cx += cw
        return cx

    def gtext(self, x: int, y: int, s: str, stops: Sequence[Color] = SPECTRUM, bold: bool = False,
              span: Optional[int] = None, offset: float = 0.0, bright: float = 1.0, maxw: Optional[int] = None) -> int:
        s = prep(s, self.u)
        n = max(1, (span or swidth(s)) - 1)
        limit = self.w if maxw is None else min(self.w, x + maxw)
        cx = x
        for ch in s:
            cw = cwidth(ch)
            if cw == 0:
                continue
            if cx + cw > limit:
                break
            col = fx.grad(stops, fx.tri((cx - x) / n + offset))
            if bright != 1.0:
                col = fx.scale(col, bright)
            self.put(cx, y, ch, col, bold)
            if cw == 2:
                self.put(cx + 1, y, "", col, bold)
                _WIDE_SEEN[0] = True
            cx += cw
        return cx

    def center(self, y: int, s: str, fg: Optional[Color] = None, bold: bool = False) -> int:
        s = clip(s, self.w, self.u)
        x = max(0, (self.w - swidth(s)) // 2)
        self.text(x, y, s, fg, bold)
        return x

    def hline(self, x: int, y: int, w: int, ch: Optional[str] = None, fg: Optional[Color] = NIGHT) -> None:
        ch = ch or ("─" if self.u else "-")
        for i in range(max(0, w)):
            self.put(x + i, y, ch, fg)

    def ghline(self, x: int, y: int, w: int, stops: Sequence[Color] = SPECTRUM, ch: Optional[str] = None,
               bright: float = 1.0, offset: float = 0.0) -> None:
        ch = ch or ("━" if self.u else "=")
        n = max(1, w - 1)
        for i in range(max(0, w)):
            self.put(x + i, y, ch, fx.scale(fx.grad(stops, fx.tri(i / n + offset)), bright))

    def vline(self, x: int, y: int, h: int, ch: Optional[str] = None, fg: Optional[Color] = NIGHT) -> None:
        ch = ch or ("│" if self.u else "|")
        for j in range(max(0, h)):
            self.put(x, y + j, ch, fg)

    def fill(self, x: int, y: int, w: int, h: int, ch: str = " ", fg: Optional[Color] = None) -> None:
        for j in range(max(0, h)):
            for i in range(max(0, w)):
                self.put(x + i, y + j, ch, fg)

    def box(self, x: int, y: int, w: int, h: int, fg: Optional[Color] = DUST, heavy: bool = False,
            fill: bool = False, stops: Optional[Sequence[Color]] = None, phase: float = 0.0,
            bright: float = 1.0) -> None:
        if w < 2 or h < 2:
            return
        if fill:
            self.fill(x + 1, y + 1, w - 2, h - 2)
        if self.u:
            tl, tr, bl, br, hz, vt = ("┏", "┓", "┗", "┛", "━", "┃") if heavy else ("╭", "╮", "╰", "╯", "─", "│")
        else:
            tl, tr, bl, br, hz, vt = "+", "+", "+", "+", "-", "|"
        if stops is None:
            col = fg if bright == 1.0 or fg is None else fx.scale(fg, bright)
            for i in range(1, w - 1):
                self.put(x + i, y, hz, col)
                self.put(x + i, y + h - 1, hz, col)
            for j in range(1, h - 1):
                self.put(x, y + j, vt, col)
                self.put(x + w - 1, y + j, vt, col)
            self.put(x, y, tl, col)
            self.put(x + w - 1, y, tr, col)
            self.put(x, y + h - 1, bl, col)
            self.put(x + w - 1, y + h - 1, br, col)
            return
        perim: List[Tuple[int, int, str]] = [(x, y, tl)] + [(x + i, y, hz) for i in range(1, w - 1)] + \
            [(x + w - 1, y, tr)] + [(x + w - 1, y + j, vt) for j in range(1, h - 1)] + \
            [(x + w - 1, y + h - 1, br)] + [(x + w - 1 - i, y + h - 1, hz) for i in range(1, w - 1)] + \
            [(x, y + h - 1, bl)] + [(x, y + h - 1 - j, vt) for j in range(1, h - 1)]
        n = len(perim)
        for k, (px, py, ch) in enumerate(perim):
            tt = (k / n + phase) % 1.0
            self.put(px, py, ch, fx.scale(fx.grad(stops, abs(tt * 2 - 1)), bright))


def view(cv: Any, x: int = 0, y: int = 0, w: Optional[int] = None, h: Optional[int] = None,
         u: Optional[bool] = None) -> View:
    return cv if isinstance(cv, View) and (x, y, w, h, u) == (0, 0, None, None, None) else View(cv, x, y, w, h, u)


def fix_wide(cv: Any) -> None:
    """Repair half-overwritten wide glyphs after compositing (blits, slides, overlays)."""
    for row in cv.cells:
        n = len(row)
        for x in range(n):
            ch = row[x][0]
            if ch == "":
                if x == 0 or row[x - 1][0] == "" or len(row[x - 1][0]) != 1 or cwidth(row[x - 1][0]) != 2:
                    row[x] = (" ", row[x][1], row[x][2])
            elif len(ch) == 1 and ord(ch) >= 0x1100 and cwidth(ch) == 2:
                if x + 1 >= n or row[x + 1][0] != "":
                    row[x] = (" ", row[x][1], row[x][2])


def wide_seen() -> bool:
    return _WIDE_SEEN[0]


# ---------------------------------------------------------------------------
# Decrypt-in text
# ---------------------------------------------------------------------------

def _scramble(u: bool) -> str:
    return fx.DECODE_GLYPHS if u else "#%&*+=-:01<>/\\"


def draw_decrypt(v: View, x: int, y: int, s: str, t: float, dur: float = 0.55, stops: Sequence[Color] = SPECTRUM,
                 bold: bool = True, span: Optional[int] = None, offset: float = 0.0, seed: int = 0,
                 maxw: Optional[int] = None) -> int:
    """Gradient text whose characters resolve out of glyph noise, left to right."""
    s = prep(s, v.u)
    if t >= dur + 0.25:
        return v.gtext(x, y, s, stops, bold, span, offset, maxw=maxw)
    n = max(1, len(s))
    span_n = max(1, (span or swidth(s)) - 1)
    rng = random.Random(int(t * 30) * 7919 + seed)
    glyphs = _scramble(v.u)
    limit = v.w if maxw is None else min(v.w, x + maxw)
    cx = x
    for i, ch in enumerate(s):
        cw = cwidth(ch)
        if cw == 0:
            continue
        if cx + cw > limit:
            break
        tr = dur * (i / n) * 0.85 + (rng.random() * 0.08)
        col = fx.grad(stops, fx.tri((cx - x) / span_n + offset))
        if t < tr - 0.22:
            pass
        elif t < tr and ch != " ":
            v.put(cx, y, rng.choice(glyphs), fx.scale(fx.grad(SPECTRUM, rng.random()), 0.45 + 0.4 * rng.random()))
        else:
            flash = clamp(1 - (t - tr) / 0.18, 0, 1)
            v.put(cx, y, ch, fx.lerp(col, ICE, flash), bold)
            if cw == 2:
                v.put(cx + 1, y, "", col, bold)
        cx += cw
    return cx


# ---------------------------------------------------------------------------
# Markdown-lite
# ---------------------------------------------------------------------------

_MD_H = re.compile(r"^(#{1,4})\s+(.*)$")
_MD_UL = re.compile(r"^(\s*)[-*+•]\s+(.*)$")
_MD_OL = re.compile(r"^(\s*)(\d{1,4})[.)]\s+(.*)$")
_MD_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_MD_HR = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_MD_INLINE = re.compile(r"(\*\*[^*\n]+?\*\*|__[^_\n]+?__|`[^`\n]+`|\[[^\]\n]+\]\([^)\s]*\)|"
                        r"(?<![\w*])\*[^*\s](?:[^*\n]*?[^*\s])?\*(?![\w*]))")


def parse_md(text: str) -> List[Tuple[Any, ...]]:
    """Blocks: ('h', level, text) ('p', text) ('ul', depth, text) ('ol', depth, n, text)
    ('quote', text) ('code', lang, text) ('hr',) ('blank',)."""
    blocks: List[Tuple[Any, ...]] = []
    para: List[str] = []
    code: Optional[Tuple[str, List[str]]] = None

    def flush() -> None:
        if para:
            blocks.append(("p", " ".join(para)))
            del para[:]

    for raw in (text or "").split("\n"):
        if code is not None:
            if raw.strip().startswith("```"):
                blocks.append(("code", code[0], "\n".join(code[1])))
                code = None
            else:
                code[1].append(raw.rstrip())
            continue
        s = raw.rstrip()
        st = s.strip()
        if st.startswith("```"):
            flush()
            code = (st[3:].strip().lower(), [])
            continue
        if not st:
            flush()
            if blocks and blocks[-1][0] != "blank":
                blocks.append(("blank",))
            continue
        if _MD_HR.match(st):
            flush()
            blocks.append(("hr",))
            continue
        m = _MD_H.match(st)
        if m:
            flush()
            blocks.append(("h", len(m.group(1)), m.group(2).strip().rstrip("#").strip()))
            continue
        m = _MD_UL.match(s)
        if m:
            flush()
            blocks.append(("ul", min(3, len(m.group(1).replace("\t", "    ")) // 2), m.group(2).strip()))
            continue
        m = _MD_OL.match(s)
        if m:
            flush()
            blocks.append(("ol", min(3, len(m.group(1)) // 2), int(m.group(2)), m.group(3).strip()))
            continue
        m = _MD_QUOTE.match(s)
        if m:
            flush()
            blocks.append(("quote", m.group(1).strip()))
            continue
        if raw[:1] in (" ", "\t") and not para and blocks and blocks[-1][0] in ("ul", "ol"):
            b = blocks[-1]
            blocks[-1] = b[:-1] + (b[-1] + " " + st,)
            continue
        para.append(st)
    if code is not None:
        blocks.append(("code", code[0], "\n".join(code[1])))
    flush()
    while blocks and blocks[0][0] == "blank":
        blocks.pop(0)
    while blocks and blocks[-1][0] == "blank":
        blocks.pop()
    return blocks


def parse_inline(s: str) -> List[Tuple[str, str]]:
    """Spans (text, style): n normal, b bold, c code, i emphasis, l link text."""
    out: List[Tuple[str, str]] = []
    pos = 0
    for m in _MD_INLINE.finditer(s):
        if m.start() > pos:
            out.append((s[pos:m.start()], "n"))
        tok = m.group(0)
        if tok.startswith("**") or tok.startswith("__"):
            out.append((tok[2:-2], "b"))
        elif tok.startswith("`"):
            out.append((tok[1:-1], "c"))
        elif tok.startswith("["):
            out.append((tok[1:tok.index("]")], "l"))
        else:
            out.append((tok[1:-1], "i"))
        pos = m.end()
    if pos < len(s):
        out.append((s[pos:], "n"))
    return out


_Ch = Tuple[str, Optional[Color], bool]


def _styled(s: str, base: Color, u: bool, bold: bool = False, grad_stops: Optional[Sequence[Color]] = None) -> List[_Ch]:
    chars: List[_Ch] = []
    for txt, style in parse_inline(prep(s, u)):
        if style == "b":
            col, b = ICE, True
        elif style == "c":
            col, b = INLINE_CODE, False
        elif style == "i":
            col, b = EMPH, bold
        elif style == "l":
            col, b = BEAM, bold
        else:
            col, b = base, bold
        for ch in txt:
            if cwidth(ch) or ch == " ":
                chars.append((ch, col, b))
    if grad_stops:
        n = max(1, len(chars) - 1)
        chars = [(c, fx.grad(grad_stops, i / n), b) for i, (c, _, b) in enumerate(chars)]
    return chars


def _wrap_chars(chars: List[_Ch], width: int) -> List[List[_Ch]]:
    width = max(1, width)
    words: List[List[_Ch]] = []
    cur: List[_Ch] = []
    for c in chars:
        if c[0] == " ":
            if cur:
                words.append(cur)
                cur = []
        else:
            cur.append(c)
    if cur:
        words.append(cur)
    lines: List[List[_Ch]] = []
    line: List[_Ch] = []
    lw = 0
    for wd in words:
        ww = sum(cwidth(c[0]) for c in wd)
        if line and lw + 1 + ww > width:
            lines.append(line)
            line, lw = [], 0
        if ww > width:
            if line:
                lines.append(line)
                line, lw = [], 0
            for c in wd:
                cw = cwidth(c[0])
                if lw + cw > width:
                    lines.append(line)
                    line, lw = [], 0
                line.append(c)
                lw += cw
            continue
        if line:
            line.append((" ", None, False))
            lw += 1
        line.extend(wd)
        lw += ww
    if line or not lines:
        lines.append(line)
    return lines


def _segs(chars: List[_Ch]) -> Line:
    out: Line = []
    for ch, col, b in chars:
        if out and out[-1][1] == col and out[-1][2] == b:
            out[-1] = (out[-1][0] + ch, col, b)
        else:
            out.append((ch, col, b))
    return out


def line_width(line: Line) -> int:
    return sum(swidth(s) for s, _, _ in line)


@lru_cache(maxsize=256)
def _md_layout_cached(text: str, width: int, u: bool, base: Color) -> Tuple[Tuple[int, Tuple[Seg, ...]], ...]:
    out: List[Tuple[int, Line]] = []
    blocks = parse_md(text)
    top_items = sum(1 for b in blocks if b[0] in ("ul", "ol") and b[1] == 0)
    ol_w = max([len(str(b[2])) for b in blocks if b[0] == "ol"] or [1])
    item = 0
    bullet_i = 0
    for b in blocks:
        kind = b[0]
        if kind == "blank":
            out.append((item, []))
            continue
        if kind == "h":
            level, txt = b[1], b[2]
            if out and out[-1][1]:
                out.append((item, []))
            if level == 1:
                chars = _styled(txt, ICE, u, True, SPECTRUM)
            elif level == 2:
                chars = _styled(txt, ICE, u, True)
            else:
                chars = _styled(txt.upper() if level >= 4 else txt, SOFT, u, True)
            for ln in _wrap_chars(chars, width):
                out.append((item, _segs(ln)))
        elif kind == "p":
            for ln in _wrap_chars(_styled(b[1], base, u), width):
                out.append((item, _segs(ln)))
        elif kind in ("ul", "ol"):
            depth = b[1]
            indent = 2 * depth
            if kind == "ul":
                if depth == 0:
                    mark = ("▸ " if u else "> ")
                    col = fx.grad(SPECTRUM, (bullet_i / max(1, top_items - 1)) * 0.85 if top_items > 1 else 0.2)
                    bullet_i += 1
                else:
                    mark, col = ("◦ " if u else "- "), DUST
                prefix = [(" " * indent, None, False), (mark, col, True)]
                pw = indent + 2
            else:
                num = ("%d." % b[2]).rjust(ol_w + 1) + " "
                prefix = [(" " * indent, None, False), (num, BEAM, True)]
                pw = indent + len(num)
                if depth == 0:
                    bullet_i += 1
            txt = b[-1]
            lines = _wrap_chars(_styled(txt, base, u), max(4, width - pw))
            for i, ln in enumerate(lines):
                pre: Line = prefix if i == 0 else [(" " * pw, None, False)]
                out.append((item, [p for p in pre if p[0]] + _segs(ln)))
        elif kind == "quote":
            for ln in _wrap_chars(_styled(b[1], SOFT, u), max(4, width - 2)):
                out.append((item, [("▌ " if u else "| ", PLASMA, True)] + _segs(ln)))
        elif kind == "code":
            for segs in highlight(b[2], b[1])[:200]:
                segs2: Line = [("  ", None, False)]
                used = 2
                for txt, col, bold in segs:
                    room = width - used
                    if room <= 0:
                        break
                    t2 = prep(txt, u)
                    if swidth(t2) > room:
                        t2 = clip(t2, room, u)
                    segs2.append((t2, col, bold))
                    used += swidth(t2)
                out.append((item, segs2))
        elif kind == "hr":
            out.append((item, [(("─" if u else "-") * width, NIGHT, False)]))
        item += 1
    while out and not out[-1][1]:
        out.pop()
    return tuple((i, tuple(ln)) for i, ln in out)


def md_layout(text: str, width: int, u: bool = True, base: Color = TEXT) -> List[Tuple[int, Line]]:
    """Markdown-lite to (item index, styled line) pairs, wrapped to `width` cells."""
    return [(i, list(ln)) for i, ln in _md_layout_cached(text or "", max(4, width), bool(u), base)]


def md_lines(text: str, width: int, u: bool = True, base: Color = TEXT) -> List[Line]:
    return [ln for _, ln in md_layout(text, width, u, base)]


def draw_line(v: View, x: int, y: int, line: Line, maxw: Optional[int] = None, bright: float = 1.0,
              shift: int = 0) -> int:
    cx = x + shift
    limit = (x + maxw) if maxw is not None else v.w
    for txt, col, bold in line:
        if cx >= limit:
            break
        if col is not None and bright != 1.0:
            col = fx.scale(col, bright)
        cx = v.text(cx, y, txt, col, bold, maxw=limit - cx)
    return cx


def airy(lay: List[Tuple[int, Line]], h: int) -> List[Tuple[int, Line]]:
    """Add a blank row between list items when there is room (slides breathe)."""
    items = len({i for i, _ in lay})
    if items < 2 or len(lay) + items - 1 > h or any(not ln for _, ln in lay):
        return lay
    out: List[Tuple[int, Line]] = []
    for idx, (i, ln) in enumerate(lay):
        if idx and i != lay[idx - 1][0]:
            out.append((i, []))
        out.append((i, ln))
    return out


def draw_md(cv: Any, x: int, y: int, w: int, h: int, text: str, u: Optional[bool] = None, t: float = SETTLED,
            base: Color = TEXT, stagger: float = 0.06, scroll: int = 0, spacious: bool = False) -> int:
    """Draw markdown-lite into a region. Returns the number of hidden rows (overflow)."""
    v = View(cv, x, y, w, h, u)
    lay = md_layout(text, v.w, v.u, base)
    if spacious:
        lay = airy(lay, v.h)
    scroll = int(clamp(scroll, 0, max(0, len(lay) - v.h)))
    shown = lay[scroll:scroll + v.h]
    for row, (item, ln) in enumerate(shown):
        appear = item * stagger
        if t < appear:
            continue
        k = ease((t - appear) / 0.35)
        draw_line(v, 0, row, ln, bright=0.35 + 0.65 * k, shift=int(round((1 - k) * 6)))
    hidden = max(0, len(lay) - v.h)
    if hidden and v.h >= 2:
        more = (" ↓ %d more " if v.u else " v %d more ") % (len(lay) - scroll - len(shown))
        if len(lay) - scroll - len(shown) > 0:
            v.text(max(0, v.w - swidth(more)), v.h - 1, more, DUST)
    return hidden


# ---------------------------------------------------------------------------
# Big type
# ---------------------------------------------------------------------------

_BIG_SRC = {
    "A": ".##./#..#/####/#..#/#..#", "B": "###./#..#/###./#..#/###.", "C": ".###/#.../#.../#.../.###",
    "D": "###./#..#/#..#/#..#/###.", "E": "####/#.../###./#.../####", "F": "####/#.../###./#.../#...",
    "G": ".###/#.../#.##/#..#/.###", "H": "#..#/#..#/####/#..#/#..#", "I": "###/.#./.#./.#./###",
    "J": "..##/...#/...#/#..#/.##.", "K": "#..#/#.#./##../#.#./#..#", "L": "#.../#.../#.../#.../####",
    "M": "#...#/##.##/#.#.#/#...#/#...#", "N": "#..#/##.#/#.##/#..#/#..#", "O": ".##./#..#/#..#/#..#/.##.",
    "P": "###./#..#/###./#.../#...", "Q": ".##./#..#/#..#/#.#./.#.#", "R": "###./#..#/###./#.#./#..#",
    "S": ".###/#.../.##./...#/###.", "T": "#####/..#../..#../..#../..#..", "U": "#..#/#..#/#..#/#..#/.##.",
    "V": "#...#/#...#/.#.#./.#.#./..#..", "W": "#...#/#...#/#.#.#/##.##/#...#", "X": "#...#/.#.#./..#../.#.#./#...#",
    "Y": "#...#/.#.#./..#../..#../..#..", "Z": "####/...#/.##./#.../####",
    "0": ".##./#.##/##.#/#..#/.##.", "1": ".#./##./.#./.#./###", "2": "###./...#/.##./#.../####",
    "3": "###./...#/.##./...#/###.", "4": "#..#/#..#/####/...#/...#", "5": "####/#.../###./...#/###.",
    "6": ".##./#.../###./#..#/.##.", "7": "####/...#/..#./.#../.#..", "8": ".##./#..#/.##./#..#/.##.",
    "9": ".##./#..#/.###/...#/.##.",
    " ": "../../../../..", ".": "././././#", ",": "../../../.#/#.", ":": "./#/./#/.",
    ";": "../.#/../.#/#.", "!": "#/#/#/./#", "?": "###./...#/.##./..../.#..", "-": ".../.../###/.../...",
    "+": ".../.#./###/.#./...", "=": ".../###/.../###/...", "/": "..#/..#/.#./#../#..", "\\": "#../#../.#./..#/..#",
    "'": "#/#/././.", '"': "#.#/#.#/.../.../...", "(": ".#/#./#./#./.#", ")": "#./.#/.#/.#/#.",
    "[": "##/#./#./#./##", "]": "##/.#/.#/.#/##", "<": "..#/.#./#../.#./..#", ">": "#../.#./..#/.#./#..",
    "_": ".../.../.../.../###", "*": ".../#.#/.#./#.#/...", "#": ".#.#./#####/.#.#./#####/.#.#.",
    "%": "#...#/...#./..#../.#.../#...#", "&": ".#../#.#./.#../#.#./.#.#", "|": "#/#/#/#/#",
    "^": ".#./#.#/.../.../...", "~": "..../.#.#/#.#./..../....", "$": ".####/#.#../.###./..#.#/####.",
    "@": ".###./#..##/#.#.#/#..##/.##..", "◉": ".###./#...#/#.#.#/#...#/.###.", "·": "./././#/.",
    "×": ".../#.#/.#./#.#/...", "\u201c": "..#..#/.#..#./##.##./##.##./......",
    "→": "..../..#./####/..#./....", "←": "..../.#../####/.#../....",
}
BIG_GLYPHS: Dict[str, Tuple[str, ...]] = {k: tuple(v.split("/")) for k, v in _BIG_SRC.items()}
BIG_MODES = ("wide", "tall", "compact")


def big_prep(text: str) -> str:
    """Uppercase for big type, keeping characters the glyph table has and folding the rest to ASCII."""
    out = []
    for ch in str(text or "").upper():
        out.append(ch if ch in BIG_GLYPHS else asc(ch))
    return "".join(out)


def _big_char(ch: str) -> Tuple[str, ...]:
    g = BIG_GLYPHS.get(ch)
    if g is None:
        g = BIG_GLYPHS.get(ch.upper())
    if g is None:
        base = unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode("ascii").upper()[:1]
        g = BIG_GLYPHS.get(base) or BIG_GLYPHS["?"]
    return g


def big_height(mode: str) -> int:
    return 3 if mode == "compact" else 5


def _big_geom(mode: str) -> Tuple[int, int]:
    """(cells per pixel column, gap cells between glyphs)."""
    return (2, 2) if mode == "wide" else (1, 1)


def big_width(text: str, mode: str = "compact") -> int:
    sx, gap = _big_geom(mode)
    gl = [_big_char(c) for c in text]
    if not gl:
        return 0
    return sum(len(g[0]) * sx for g in gl) + gap * (len(gl) - 1)


def big_fit(text: str, w: int, h: int, modes: Sequence[str] = BIG_MODES, max_lines: int = 2,
            max_chars: int = 40) -> Optional[Tuple[str, List[str]]]:
    """Largest big-type mode (and word-wrapped lines) that fits w x h, or None."""
    text = " ".join(big_prep(text).split())
    if not text or len(text) > max_chars:
        return None
    for mode in modes:
        rows = big_height(mode)
        if rows > h:
            continue
        if big_width(text, mode) <= w:
            return mode, [text]
        if max_lines >= 2 and rows * 2 + 1 <= h:
            words = text.split(" ")
            lines: List[str] = []
            cur = ""
            for wd in words:
                cand = (cur + " " + wd) if cur else wd
                if big_width(cand, mode) <= w:
                    cur = cand
                else:
                    if cur:
                        lines.append(cur)
                    cur = wd
            lines.append(cur)
            if len(lines) <= max_lines and all(big_width(ln, mode) <= w for ln in lines):
                return mode, lines
    return None


def draw_big(cv: Any, x: int, y: int, text: str, mode: str = "compact", u: Optional[bool] = None,
             stops: Sequence[Color] = SPECTRUM, offset: float = 0.0, t: float = SETTLED, span: Optional[int] = None,
             bright: float = 1.0, shimmer: Optional[float] = None, seed: int = 0) -> int:
    """Draw big type at (x, y). Returns its width. `t` < ~0.8 plays the decrypt-in."""
    v = view(cv, u=u) if not isinstance(cv, View) else cv
    text = big_prep(text)
    sx, gap = _big_geom(mode)
    total = big_width(text, mode)
    span_n = max(1, (span or total) - 1)
    rng = random.Random(int(t * 30) * 104729 + seed)
    glyphs = _scramble(v.u)
    dur = 0.65
    cx = x
    for ch in text:
        g = _big_char(ch)
        gw = len(g[0])
        for px in range(gw):
            for k in range(sx):
                col_x = cx + px * sx + k
                rel = (col_x - x) / max(1, total)
                tr = dur * rel + 0.05
                base = fx.grad(stops, fx.tri((col_x - x) / span_n + offset))
                if shimmer is not None:
                    d = abs((col_x - x) - shimmer)
                    if d < 5:
                        base = fx.lerp(base, ICE, (1 - d / 5) * 0.7)
                if bright != 1.0:
                    base = fx.scale(base, bright)
                state = 1.0 if t >= tr else (0.0 if t < tr - 0.3 else 0.5)
                if state == 0.0:
                    continue
                if mode == "compact":
                    for r in range(3):
                        top = g[2 * r][px] == "#"
                        bot = (2 * r + 1 < 5) and g[2 * r + 1][px] == "#"
                        if not (top or bot):
                            continue
                        if state < 1:
                            v.put(col_x, y + r, rng.choice(glyphs), fx.scale(fx.grad(SPECTRUM, rng.random()), 0.5))
                            continue
                        if v.u:
                            chr_ = "█" if top and bot else ("▀" if top else "▄")
                        else:
                            chr_ = "#" if top and bot else ('"' if top else ",")
                        flash = clamp(1 - (t - tr) / 0.2, 0, 1)
                        v.put(col_x, y + r, chr_, fx.lerp(base, ICE, flash), True)
                else:
                    for r in range(5):
                        if g[r][px] != "#":
                            continue
                        if state < 1:
                            v.put(col_x, y + r, rng.choice(glyphs), fx.scale(fx.grad(SPECTRUM, rng.random()), 0.5))
                            continue
                        flash = clamp(1 - (t - tr) / 0.2, 0, 1)
                        shade = 1.0 - 0.18 * (r / 4.0)
                        v.put(col_x, y + r, "█" if v.u else "#", fx.lerp(fx.scale(base, shade), ICE, flash), True)
        cx += gw * sx + gap
    return total


def split_value(value: str) -> Tuple[str, str]:
    """('$1,284', 'ms') style split: the part worth setting in big type, and a small-text unit suffix."""
    m = _NUM_PART.match(value or "")
    if not m:
        return value or "", ""
    pre, num, post = m.groups()
    if post and any(c.isalpha() for c in post):
        return pre + num, post.strip()
    return value, ""


def big_value_width(value: str, mode: str) -> int:
    head, unit = split_value(value)
    return big_width(big_prep(head), mode) + (1 + swidth(unit) if unit else 0)


def draw_big_value(v: View, x: int, y: int, value: str, mode: str = "compact", span: Optional[int] = None,
                   offset: float = 0.0, t: float = SETTLED, seed: int = 0) -> int:
    """Big digits with a small-text unit on the baseline (412 ms, 98.2 %, 1.2k tok/s)."""
    head, unit = split_value(value)
    w = draw_big(v, x, y, head, mode, t=t, span=span, offset=offset, seed=seed)
    if unit:
        v.gtext(x + w + 1, y + big_height(mode) - 1, unit, SPECTRUM, True, offset=offset + 0.7)
        w += 1 + swidth(unit)
    return w


def draw_title(v: View, x: int, y: int, w: int, h: int, text: str, t: float = SETTLED,
               modes: Sequence[str] = ("compact",), align: str = "left", max_lines: int = 1,
               offset: float = 0.0, seed: int = 0) -> int:
    """Big type when it fits, else a gradient bold line. Returns rows used."""
    if not text:
        return 0
    if not v.u:
        modes = tuple(m for m in modes if m != "compact")   # half-block shapes do not survive ASCII
    fit = big_fit(text, w, h, modes, max_lines) if modes else None
    if fit:
        mode, lines = fit
        rows = big_height(mode)
        span = max(big_width(ln, mode) for ln in lines)
        yy = y
        for i, ln in enumerate(lines):
            bw = big_width(ln, mode)
            bx = x + (w - bw) // 2 if align == "center" else x
            draw_big(v, bx, yy, ln, mode, t=t - 0.12 * i, span=span, offset=offset, seed=seed + i)
            yy += rows + 1
        return yy - y - 1
    lines = wrap(text, w, v.u)[:max(1, max_lines)]
    if len(wrap(text, w, v.u)) > len(lines):
        lines[-1] = clip(lines[-1] + " …", w, v.u)
    for i, ln in enumerate(lines):
        lx = x + (w - swidth(ln)) // 2 if align == "center" else x
        draw_decrypt(v, lx, y + i, ln, t - 0.08 * i, span=w if align == "left" else None, offset=offset, seed=seed + i)
    return len(lines)


# ---------------------------------------------------------------------------
# Numbers, sparklines, charts
# ---------------------------------------------------------------------------

def _trim(s: str) -> str:
    return s.rstrip("0").rstrip(".") if "." in s else s


# Values beyond this magnitude are clamped before any chart arithmetic, so spans like hi - lo stay
# finite (the schema accepts any finite float, and 1e308 - -1e308 overflows to inf). Magnitudes below
# VALUE_EPS snap to zero: subnormal spans underflow tick maths (log10(0), division by zero).
VALUE_CAP = 1e300
VALUE_EPS = 1e-300


def finite(values: Sequence[Any]) -> List[float]:
    """Chart-safe numbers: non-numeric and non-finite entries dropped, magnitudes clamped to VALUE_CAP."""
    out: List[float] = []
    for z in values or ():
        try:
            f = float(z)
        except (TypeError, ValueError):
            continue
        if f != f or f in (float("inf"), float("-inf")):
            continue
        out.append(0.0 if abs(f) < VALUE_EPS else clamp(f, -VALUE_CAP, VALUE_CAP))
    return out


def _clean_series(series: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Series with finite values only; series left with no values are dropped."""
    out = []
    for s in series or []:
        if not isinstance(s, dict):
            continue
        vals = finite(s.get("values") or [])
        if vals:
            out.append(dict(s, values=vals))
    return out


def fmt_num(v: float, unit: str = "") -> str:
    if v != v or v in (float("inf"), float("-inf")):
        return "n/a"
    a = abs(v)
    if a >= 1e15:
        s = "%.1e" % v
    elif a >= 1e12:
        s = _trim("%.1f" % (v / 1e12)) + "T"
    elif a >= 1e9:
        s = _trim("%.1f" % (v / 1e9)) + "B"
    elif a >= 1e6:
        s = _trim("%.1f" % (v / 1e6)) + "M"
    elif a >= 1e4:
        s = _trim("%.1f" % (v / 1e3)) + "k"
    elif a >= 100 or v == int(v):
        s = "%d" % round(v)
    elif a >= 1:
        s = _trim("%.2f" % v)
    elif a == 0:
        s = "0"
    else:
        s = _trim("%.3f" % v) if a >= 0.001 else "%.1e" % v
    if not unit:
        return s
    if unit in ("$", "€", "£", "¥"):
        return ("-" + unit + s[1:]) if s.startswith("-") else unit + s
    return s + unit if len(unit) <= 2 or unit in ("ms", "ops") else s + " " + unit


def nice_ticks(lo: float, hi: float, n: int = 5) -> Tuple[float, float, float]:
    if hi < lo:
        lo, hi = hi, lo
    if hi == lo:
        if hi == 0:
            return 0.0, 1.0, 0.25
        pad = abs(hi) * 0.1
        lo, hi = lo - pad, hi + pad
    raw = (hi - lo) / max(1, n - 1)
    if not (VALUE_EPS <= raw < float("inf")):
        return lo, hi, (hi - lo) if hi - lo > 0 and hi - lo != float("inf") else 1.0
    mag = 10 ** math.floor(math.log10(raw))
    step = mag
    for m in (1, 2, 2.5, 5, 10):
        step = m * mag
        if step >= raw:
            break
    nlo = math.floor(lo / step + 1e-9) * step
    nhi = math.ceil(hi / step - 1e-9) * step
    if nhi <= nlo:
        nhi = nlo + step
    return nlo, nhi, step


def resample(values: Sequence[float], n: int) -> List[float]:
    """Bucket-average down to n points, or linearly stretch up to n points."""
    vals = list(values)
    if n <= 0 or not vals:
        return []
    m = len(vals)
    if m == n:
        return vals
    if m > n:
        out = []
        for i in range(n):
            a = int(i * m / n)
            b = max(a + 1, int((i + 1) * m / n))
            chunk = vals[a:b]
            out.append(sum(chunk) / len(chunk))
        return out
    if m == 1:
        return vals * n
    out = []
    for i in range(n):
        pos = i * (m - 1) / max(1, n - 1)
        j = min(int(pos), m - 2)
        f = pos - j
        out.append(vals[j] * (1 - f) + vals[j + 1] * f)
    return out


SPARK_U = "▁▂▃▄▅▆▇█"
SPARK_A = "_.,-=+*#"


def sparkline(values: Sequence[float], width: int, u: bool = True) -> str:
    values = finite(values)
    vals = resample(values, min(width, len(values))) if len(values) > width else list(values)
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    ramp = SPARK_U if u else SPARK_A
    if hi == lo:
        return ramp[3] * len(vals)
    return "".join(ramp[int(round((v - lo) / (hi - lo) * (len(ramp) - 1)))] for v in vals)


def draw_sparkline(cv: Any, x: int, y: int, w: int, values: Sequence[float], u: Optional[bool] = None,
                   stops: Sequence[Color] = SPECTRUM, bright: float = 1.0, t: float = SETTLED) -> int:
    v = view(cv, u=u) if not isinstance(cv, View) else cv
    s = sparkline(values, w, v.u)
    n = max(1, len(s) - 1)
    k = ease(t / 0.6)
    shown = int(round(len(s) * k))
    for i, ch in enumerate(s[:shown]):
        v.put(x + i, y, ch, fx.scale(fx.grad(stops, i / n), bright))
    return len(s)


_BRAILLE = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))


def _line_dots(x0: int, y0: int, x1: int, y1: int):
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    err = dx + dy
    while True:
        yield x0, y0
        if x0 == x1 and y0 == y1:
            return
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def _legend(v: View, y: int, series: List[Dict[str, Any]], unit: str, colors: Sequence[Color]) -> None:
    x = 0
    for i, s in enumerate(series):
        name = s.get("name") or ("series %d" % (i + 1))
        vals = s.get("values") or []
        last = fmt_num(vals[-1], unit) if vals else ""
        item_w = 2 + swidth(prep(name, v.u)) + (1 + len(last) if last else 0) + 3
        if x + item_w > v.w and x > 0:
            break
        v.put(x, y, "●" if v.u else "o", colors[i % len(colors)], True)
        x = v.text(x + 2, y, name, TEXT, False)
        if last:
            x = v.text(x + 1, y, last, colors[i % len(colors)], True)
        x += 3


def _x_labels(v: View, y: int, px: int, pw: int, labels: List[str], n: int, positions: List[int],
              axis_y: Optional[int] = None) -> None:
    """Place x labels under their columns, skipping ones that would collide."""
    if not labels:
        return
    taken_until = -1
    order = list(range(len(positions)))
    last = len(positions) - 1
    for i in order:
        if i >= len(labels):
            break
        lab = clip(labels[i], max(3, pw // 2), v.u)
        lw = swidth(lab)
        cx = px + positions[i]
        lx = int(clamp(cx - lw // 2, 0, v.w - lw))
        if lx <= taken_until + 1:
            continue
        # keep room for the last label
        if i != last and last < len(labels):
            lab_last = clip(labels[last], max(3, pw // 2), v.u)
            last_x = int(clamp(px + positions[last] - swidth(lab_last) // 2, 0, v.w - swidth(lab_last)))
            if lx + lw + 1 >= last_x and i != last:
                continue
        v.text(lx, y, lab, SOFT)
        if axis_y is not None and v.u:
            v.put(cx, axis_y, "┴", DUST)
        taken_until = lx + lw


def _y_axis(v: View, top: int, ph: int, lo: float, hi: float, step: float, unit: str, axis_x: int,
            grid_w: int) -> None:
    span = hi - lo
    nt = int(round(span / step)) + 1
    marks = {}
    for k in range(nt):
        tv = lo + k * step
        row = top + int(round((1 - (tv - lo) / span) * (ph - 1)))
        marks[row] = tv
    for r in range(top, top + ph):
        if r in marks:
            lab = fmt_num(marks[r], unit)
            v.text(max(0, axis_x - 1 - swidth(lab)), r, lab, DUST)
            v.put(axis_x, r, "┤" if v.u else "+", DUST)
            for gx in range(grid_w):
                if (gx % 2) == 0:
                    v.put(axis_x + 1 + gx, r, "┈" if v.u else ".", FAINT)
        else:
            v.put(axis_x, r, "│" if v.u else "|", NIGHT)


def _value_range(series: List[Dict[str, Any]], zero: bool) -> Tuple[float, float]:
    vals = [float(x) for s in series for x in (s.get("values") or [])]
    if not vals:
        return 0.0, 1.0
    lo, hi = min(vals), max(vals)
    if zero:
        lo, hi = min(0.0, lo), max(0.0, hi)
    return lo, hi


def draw_line_chart(cv: Any, x: int, y: int, w: int, h: int, labels: List[str], series: List[Dict[str, Any]],
                    unit: str = "", u: Optional[bool] = None, t: float = SETTLED, motion: bool = False) -> None:
    v = View(cv, x, y, w, h, u)
    series = _clean_series(series)
    if not series:
        return
    if v.w < 12 or v.h < 4:
        draw_sparkline(v, 0, max(0, v.h // 2), v.w, series[0]["values"], t=t)
        return
    multi = len(series) > 1
    colors = SERIES if multi else (BEAM,)
    top = 0
    if multi or series[0].get("name"):
        _legend(v, 0, series, unit, colors)
        top = 2 if v.h >= 12 else 1
    ph = v.h - top - 2
    lo, hi = _value_range(series, False)
    lo, hi, step = nice_ticks(lo, hi, max(2, min(6, ph // 2 + 1)))
    yw = max(swidth(fmt_num(lo + k * step, unit)) for k in range(int(round((hi - lo) / step)) + 1))
    axis_x = yw + 1
    pw = v.w - axis_x - 2
    if pw < 4 or ph < 2:
        draw_sparkline(v, 0, max(0, v.h // 2), v.w, series[0]["values"], t=t)
        return
    px = axis_x + 1
    _y_axis(v, top, ph, lo, hi, step, unit, axis_x, pw)
    v.put(axis_x, top + ph, "╰" if v.u else "+", DUST)
    v.hline(px, top + ph, pw, "─" if v.u else "-", DUST)
    n = max(len(s["values"]) for s in series)
    sxr, syr = (2, 4) if v.u else (1, 1)
    dots_w, dots_h = pw * sxr, ph * syr
    reveal = ease(t / 0.9) * dots_w
    cells: Dict[Tuple[int, int], List[int]] = {}
    tops: Dict[int, int] = {}
    span = hi - lo
    for si, s in enumerate(series):
        vals = s["values"]
        pts = []
        for i, val in enumerate(vals):
            dx = int(round(i * (dots_w - 1) / max(1, n - 1))) if n > 1 else dots_w // 2
            dy = int(round((1 - (float(val) - lo) / span) * (dots_h - 1)))
            pts.append((dx, dy))
        segs = list(zip(pts, pts[1:])) or [(pts[0], pts[0])]
        for (x0, y0), (x1, y1) in segs:
            for ddx, ddy in _line_dots(x0, y0, x1, y1):
                if ddx > reveal:
                    continue
                key = (ddx // sxr, ddy // syr)
                c = cells.get(key)
                if c is None:
                    c = cells[key] = [0, si]
                c[0] |= _BRAILLE[ddy % syr][ddx % sxr] if v.u else 1
                c[1] = si
                tops[key[0]] = min(tops.get(key[0], ph), key[1])
    if not multi and v.u:
        for cx, cy0 in tops.items():
            for cy in range(cy0 + 1, ph):
                fade = 0.2 * (1 - (cy - cy0) / max(1, ph - cy0)) + 0.04
                v.put(px + cx, top + cy, "⣿", fx.scale(fx.grad(SPECTRUM, cx / max(1, pw - 1)), fade))
    for (cx, cy), (bits, si) in cells.items():
        col = fx.grad(SPECTRUM, cx / max(1, pw - 1)) if not multi else colors[si % len(colors)]
        ch = chr(0x2800 + bits) if v.u else "*"
        v.put(px + cx, top + cy, ch, col, True)
    if motion and not multi and n > 1 and t > 1.0:
        head = int((t * 18) % (pw + 14))
        for k in range(4):
            cx = head - k
            if 0 <= cx < pw and cx in tops:
                key = (cx, tops[cx])
                if key in cells:
                    v.put(px + cx, top + key[1], chr(0x2800 + cells[key][0]) if v.u else "*",
                          fx.lerp(ICE, fx.grad(SPECTRUM, cx / max(1, pw - 1)), k / 4), True)
    positions = [int(round(i * (dots_w - 1) / max(1, n - 1))) // sxr if n > 1 else pw // 2 for i in range(n)]
    labs = [prep(lb, v.u) for lb in (labels or [])][:n]
    _x_labels(v, top + ph + 1, px, pw, labs, n, positions, top + ph)


def draw_bar_chart(cv: Any, x: int, y: int, w: int, h: int, labels: List[str], series: List[Dict[str, Any]],
                   unit: str = "", u: Optional[bool] = None, t: float = SETTLED, motion: bool = False,
                   orient: str = "auto") -> None:
    v = View(cv, x, y, w, h, u)
    series = _clean_series(series)
    if not series or v.w < 6 or v.h < 2:
        return
    n = max(len(s["values"]) for s in series)
    ns = len(series)
    labs = [prep(lb, v.u) for lb in (labels or [])] + [""] * n
    labs = [lb or str(i + 1) for i, lb in enumerate(labs[:n])]
    multi = ns > 1
    top = 0
    if multi:
        _legend(v, 0, series, unit, SERIES)
        top = 2 if v.h >= 12 else 1
    lo, hi = _value_range(series, True)
    longest = max(swidth(lb) for lb in labs)
    ph = v.h - top - 2
    nlo, nhi, step = nice_ticks(lo, hi, max(2, min(6, max(2, ph) // 2 + 1)))
    if not multi and ph >= 8:
        # The value label sits one row past the bar's end; when the bar ends on the axis limit that row
        # is off the plot and the largest (or most negative) value would go unlabelled.
        row_v = (nhi - nlo) / max(1, ph)
        if hi > 0 and nhi - hi < row_v:
            nhi += step
        if lo < 0 and lo - nlo < row_v:
            nlo -= step
    yw = max(swidth(fmt_num(nlo + k * step, unit)) for k in range(int(round((nhi - nlo) / step)) + 1))
    pw = v.w - yw - 3
    slot = pw / max(1, n)
    vertical = orient == "vertical" or (orient == "auto" and slot >= ns + 1 and slot >= 3 and ph >= 5 and
                                       (longest <= slot + 1 or n <= 4 or n * (ns + (1 if multi else 0)) > v.h - top))
    grow = lambda i: ease((t - 0.035 * i) / 0.7)  # noqa: E731
    if vertical:
        if ph < 3 or pw < n:
            return
        axis_x = yw + 1
        px = axis_x + 1
        # Rows top .. top+ph: the ticks spread over all of them, so the lowest tick (0 for an all-positive
        # chart) sits on the axis row top+ph. Bars rise from the zero tick's own row (the baseline) and
        # fill only the rows above it (positive) or below it (negative), never the baseline row itself.
        _y_axis(v, top, ph + 1, nlo, nhi, step, unit, axis_x, pw)
        span = nhi - nlo
        unit_rows = ph / span
        zr = int(clamp(round((nhi - 0) * unit_rows), 0, ph))
        zero_f = float(zr)
        v.put(axis_x, top + ph, "╰" if v.u else "+", DUST)
        v.hline(px, top + ph, pw, "─" if v.u else "-", DUST)
        if zr < ph:
            v.put(axis_x, top + zr, "┼" if v.u else "+", DUST)
            v.hline(px, top + zr, pw, "─" if v.u else "-", DUST)
        slot_i = max(1, int(slot))
        gap = 1 if slot_i > ns else 0
        bw = max(1, min(9, (slot_i - gap) // ns))
        group = bw * ns
        positions = []
        for i in range(n):
            gx = px + int(i * slot) + (slot_i - group) // 2
            positions.append(gx - px + group // 2)
            for si, s in enumerate(series):
                vals = s["values"]
                if i >= len(vals):
                    continue
                val = float(vals[i]) * grow(i)
                if val >= 0:
                    y_f = zero_f - val * unit_rows
                    a, b = y_f, zero_f
                else:
                    y_f = min(float(ph), zero_f + 1 - val * unit_rows)
                    a, b = zero_f + 1, y_f
                base_col = SERIES[si % len(SERIES)] if multi else fx.grad(SPECTRUM, i / max(1, n - 1))
                bx = gx + si * bw
                for r in range(ph):
                    cov_top, cov_bot = max(a, r), min(b, r + 1)
                    cov = cov_bot - cov_top
                    if cov <= 0.06:
                        continue
                    if cov >= 0.94:
                        ch = "█" if v.u else "#"
                    elif val >= 0:
                        ch = SPARK_U[int(clamp(round(cov * 8) - 1, 0, 7))] if v.u else "_"
                    else:
                        ch = ("▀" if cov >= 0.5 else "▔") if v.u else "-"
                    shade = 0.72 + 0.28 * (1 - (r - a) / max(1.0, b - a)) if val >= 0 else 0.9
                    col = fx.scale(base_col, shade)
                    for k in range(bw):
                        v.put(bx + k, top + r, ch, col, False)
                if not multi and t > 0.5:
                    lab = fmt_num(float(vals[i]), unit)
                    ly = top + int(math.floor(y_f)) - 1 if val >= 0 else top + int(math.ceil(y_f))
                    if swidth(lab) <= slot_i + 1 and top <= ly < top + ph and (ly < top + zr or val < 0):
                        v.text(gx + (group - swidth(lab)) // 2, ly, lab, fx.lerp(base_col, ICE, 0.5), True)
        _x_labels(v, top + ph + 1, px, pw, labs, n, positions)
        return
    # horizontal
    label_w = int(clamp(longest, 3, max(6, v.w // 3)))
    vals_txt = [fmt_num(float(s["values"][i]), unit) if i < len(s["values"]) else ""
                for s in series for i in range(n)]
    val_w = max(swidth(s) for s in vals_txt) if vals_txt else 1
    bx = label_w + 2
    bw_cells = v.w - bx - val_w - 2
    if bw_cells < 3:
        label_w = max(3, v.w - val_w - 8)
        bx = label_w + 2
        bw_cells = v.w - bx - val_w - 2
        if bw_cells < 2:
            return
    span = (hi - lo) or 1.0
    zero_c = (0 - lo) / span * bw_cells
    row = top
    rows_needed = n * ns + (n - 1 if multi else 0)
    room = v.h - top
    shown_n = n if rows_needed <= room else max(1, (room - 1) // (ns + (1 if multi else 0)))
    for i in range(shown_n):
        if row >= v.h:
            break
        for si, s in enumerate(series):
            if row >= v.h:
                break
            if si == 0:
                v.text(0, row, clip(labs[i], label_w, v.u), TEXT if not multi else ICE, multi)
            v.put(bx - 1, row, "│" if v.u else "|", NIGHT)
            vals = s["values"]
            if i < len(vals):
                val = float(vals[i]) * grow(i)
                length = val / span * bw_cells
                start = zero_c if val >= 0 else zero_c + length
                end = zero_c + length if val >= 0 else zero_c
                c0 = int(math.floor(start + 1e-9))
                full_end = int(math.floor(end + 1e-9))
                base_col = SERIES[si % len(SERIES)] if multi else None
                for cx in range(c0, full_end):
                    col = base_col or fx.grad(SPECTRUM, cx / max(1, bw_cells - 1))
                    if base_col is not None:
                        col = fx.scale(col, 0.6 + 0.4 * (cx - c0) / max(1, full_end - c0))
                    v.put(bx + cx, row, "█" if v.u else "#", col)
                frac = end - full_end
                if val >= 0 and frac > 0.06 and full_end < bw_cells:
                    col = base_col or fx.grad(SPECTRUM, full_end / max(1, bw_cells - 1))
                    v.put(bx + full_end, row, "▏▎▍▌▋▊▉█"[int(clamp(round(frac * 8) - 1, 0, 7))] if v.u else "#", col)
                if t > 0.4:
                    lab = fmt_num(float(vals[i]), unit)
                    lx = bx + int(math.ceil(end)) + 1 if val >= 0 else bx + int(math.ceil(zero_c)) + 1
                    v.text(min(lx, v.w - swidth(lab)), row, lab, ICE if val == max(
                        float(z) for z in vals) else SOFT, val == max(float(z) for z in vals))
            row += 1
        if multi:
            row += 1
    if shown_n < n and row <= v.h - 1:
        v.text(0, min(v.h - 1, row), ("… %d more" if v.u else "~ %d more") % (n - shown_n), DUST)


def draw_spark_chart(cv: Any, x: int, y: int, w: int, h: int, labels: List[str], series: List[Dict[str, Any]],
                     unit: str = "", u: Optional[bool] = None, t: float = SETTLED, motion: bool = False) -> None:
    v = View(cv, x, y, w, h, u)
    series = _clean_series(series)
    if not series or v.w < 6:
        return
    ns = len(series)
    per = max(2, v.h // ns)
    bh = int(clamp(per - 2, 1, 6))
    yy = 0
    for si, s in enumerate(series):
        if yy >= v.h:
            break
        vals = [float(z) for z in s["values"]]
        lo, hi = min(vals), max(vals)
        name = s.get("name") or ("series %d" % (si + 1) if ns > 1 else "")
        last = fmt_num(vals[-1], unit)
        stats = "min %s  max %s" % (fmt_num(lo, unit), fmt_num(hi, unit))
        x2 = v.w - swidth(last)
        v.gtext(x2, yy, last, SPECTRUM, True, offset=0.5)
        if name:
            v.text(0, yy, clip(name, max(1, x2 - 2 - len(stats) - 2), v.u), ICE, True)
        if x2 - len(stats) - 2 > swidth(prep(name, v.u)) + 2:
            v.text(x2 - len(stats) - 2, yy, stats, DUST)
        cols = v.w
        rs = resample(vals, cols)
        k = ease(t / 0.8)
        if bh <= 1 or not v.u:
            draw_sparkline(v, 0, yy + 1, v.w, vals, t=t)
        else:
            for cx, val in enumerate(rs):
                if cx > k * cols:
                    break
                lvl = ((val - lo) / (hi - lo) if hi > lo else 0.5) * bh * 8
                lvl = max(1, int(round(lvl)))
                colc = fx.grad(SPECTRUM, cx / max(1, cols - 1))
                for r in range(bh):
                    cell = lvl - r * 8
                    if cell <= 0:
                        break
                    ch = "█" if cell >= 8 else SPARK_U[cell - 1]
                    shade = 0.35 + 0.65 * ((r + 1) / bh)
                    v.put(cx, yy + bh - r, ch, fx.scale(colc, shade))
        yy += bh + 2


def draw_chart(cv: Any, x: int, y: int, w: int, h: int, scene: Dict[str, Any], u: Optional[bool] = None,
               t: float = SETTLED, motion: bool = False) -> None:
    kind = scene.get("chart") or "bar"
    fn = {"line": draw_line_chart, "spark": draw_spark_chart}.get(kind, draw_bar_chart)
    fn(cv, x, y, w, h, list(scene.get("labels") or []), list(scene.get("series") or []), scene.get("unit") or "",
       u=u, t=t, motion=motion)


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

_NUMERIC = re.compile(r"^[\s$€£¥+\-−~≈<>]*[\d,]*\.?\d+(?:[eE][+-]?\d+)?\s*(%|[kKmMbBtT]|ms|s|x|×|GB|MB|KB|TB|tok/s)?\s*$")
_GOOD = {"ok", "pass", "passed", "done", "yes", "true", "up", "healthy", "success", "green", "✓", "online", "ready"}
_BADW = {"fail", "failed", "error", "no", "false", "down", "red", "✗", "offline", "broken", "critical"}
_WARNW = {"warn", "warning", "pending", "partial", "degraded", "slow", "yellow", "flaky", "beta"}


def fit_columns(natural: List[int], minimum: List[int], avail: int) -> List[int]:
    """Shrink column widths (largest first) until they fit `avail` cells."""
    widths = list(natural)
    if sum(widths) <= avail:
        return widths
    lo, hi = 1, max(natural) if natural else 1
    best = [min(nw, max(mw, 1)) for nw, mw in zip(natural, minimum)]
    while lo <= hi:
        cap = (lo + hi) // 2
        cand = [min(nw, max(cap, mw)) for nw, mw in zip(natural, minimum)]
        if sum(cand) <= avail:
            best = cand
            lo = cap + 1
        else:
            hi = cap - 1
    spare = avail - sum(best)
    i = 0
    while spare > 0 and any(b < nw for b, nw in zip(best, natural)):
        j = i % len(best)
        if best[j] < natural[j]:
            best[j] += 1
            spare -= 1
        i += 1
    return best


def _cell_color(cell: str, numeric: bool, base: Color) -> Tuple[Color, bool]:
    low = cell.strip().lower()
    if low in _GOOD:
        return OK, True
    if low in _BADW:
        return BAD, True
    if low in _WARNW:
        return WARN, False
    if numeric:
        if low.startswith("+"):
            return OK, False
        if low.startswith("-") or low.startswith("−"):
            return (255, 120, 140), False
        return (190, 214, 255), False
    return base, False


def table_geometry(columns: List[str], rows: List[List[str]], w: int, u: bool = True) -> Tuple[List[int], int, List[bool]]:
    """(column widths, number of columns shown, numeric flags) for a w-cell table."""
    ncol = max([len(columns)] + [len(r) for r in rows]) if (columns or rows) else 0
    cols = list(columns) + [""] * (ncol - len(columns))
    cells = [[prep(r[i], u) if i < len(r) else "" for i in range(ncol)] for r in rows]
    natural = [min(48, max([swidth(prep(cols[i], u))] + [swidth(r[i]) for r in cells] + [1])) for i in range(ncol)]
    numeric = []
    for i in range(ncol):
        vals = [r[i] for r in cells if r[i].strip()]
        numeric.append(bool(vals) and all(_NUMERIC.match(x) for x in vals))
    minimum = [min(nw, max(3, min(swidth(prep(cols[i], u)), 6))) for i, nw in enumerate(natural)]
    shown = ncol
    while shown > 1 and sum(minimum[:shown]) + 3 * shown + 1 > w:
        shown -= 1
    avail = w - (3 * shown + 1)
    minimum = minimum[:shown] if sum(minimum[:shown]) <= avail else [1] * shown
    widths = fit_columns(natural[:shown], minimum, max(shown, avail))
    return widths, shown, numeric[:shown]


def draw_table(cv: Any, x: int, y: int, w: int, h: int, columns: List[str], rows: List[List[str]],
               u: Optional[bool] = None, t: float = SETTLED, scroll: int = 0) -> int:
    """Box-drawn table. Returns hidden row count (for scrolling)."""
    v = View(cv, x, y, w, h, u)
    rows = [[str(c) for c in r] for r in (rows or [])]
    columns = [str(c) for c in (columns or [])]
    if v.w < 5 or v.h < 3 or not (columns or rows):
        return 0
    widths, shown, numeric = table_geometry(columns, rows, v.w, v.u)
    total = sum(widths) + 3 * shown + 1
    has_head = any(c.strip() for c in columns)
    U = v.u
    hz = "─" if U else "-"
    vt = "│" if U else "|"
    body_rows = v.h - (4 if has_head else 2)
    if body_rows < 1:
        body_rows = max(0, v.h - 2)
        has_head = False
    scroll = int(clamp(scroll, 0, max(0, len(rows) - body_rows)))
    vis = rows[scroll:scroll + body_rows]
    hidden = max(0, len(rows) - body_rows)
    cut_cols = shown < max([len(columns)] + [len(r) for r in rows])

    def sep(yy: int, left: str, mid: str, right: str, ch: str, grad_line: bool = False) -> None:
        xx = 0
        gcol = (lambda q: fx.grad(SPECTRUM, q / max(1, total - 1))) if grad_line else (lambda q: DUST)
        v.put(xx, yy, left, gcol(0))
        for i, cw in enumerate(widths):
            for k in range(cw + 2):
                v.put(xx + 1 + k, yy, ch, gcol(xx + 1 + k))
            xx += cw + 3
            v.put(xx, yy, (mid if i < shown - 1 else right), gcol(xx))

    def row_cells(yy: int, cells: List[str], head: bool, zebra: int, k: float) -> None:
        xx = 0
        v.put(xx, yy, vt, DUST)
        for i, cw in enumerate(widths):
            raw = prep(cells[i], v.u) if i < len(cells) else ""
            txt = clip(raw, cw, v.u)
            if head:
                col, b = ICE, True
            else:
                base = TEXT if zebra % 2 == 0 else (186, 190, 212)
                col, b = _cell_color(raw, numeric[i], base)
                if i == 0 and not numeric[i]:
                    b = True
                    col = ICE if zebra % 2 == 0 else (204, 208, 228)
            if k < 1:
                col = fx.scale(col, 0.3 + 0.7 * k)
            pad = cw - swidth(txt)
            tx = xx + 2 + (pad if (numeric[i] and not head) else 0)
            v.text(tx, yy, txt, col, b)
            xx += cw + 3
            v.put(xx, yy, vt, DUST)

    yy = 0
    if U:
        sep(yy, "╭", "┬", "╮", hz)
    else:
        sep(yy, "+", "+", "+", hz)
    yy += 1
    if has_head:
        row_cells(yy, columns, True, 0, 1.0)
        yy += 1
        if U:
            sep(yy, "┝", "┿", "┥", "━", grad_line=True)
        else:
            sep(yy, "+", "+", "+", "=")
        yy += 1
    for ri, r in enumerate(vis):
        k = ease((t - 0.03 * ri) / 0.3)
        if t < 0.03 * ri:
            r = [""] * len(r)
        row_cells(yy, r, False, ri + scroll, k)
        yy += 1
    if U:
        sep(yy, "╰", "┴", "╯", hz)
    else:
        sep(yy, "+", "+", "+", hz)
    notes = []
    if hidden:
        below = len(rows) - scroll - len(vis)
        if scroll:
            notes.append(("↑ %d" if U else "^ %d") % scroll)
        if below:
            notes.append(("↓ %d more" if U else "v %d more") % below)
    if cut_cols:
        notes.append("+%d cols" % (max([len(columns)] + [len(r) for r in rows]) - shown))
    if notes:
        note = " " + "  ".join(notes) + " "
        v.text(max(1, total - swidth(note) - 2), yy, note, ASH)
    return hidden


# ---------------------------------------------------------------------------
# Diagrams: layered layout + orthogonal routing
# ---------------------------------------------------------------------------

N_, E_, S_, W_ = 1, 2, 4, 8
_JOIN_U = {N_ | S_: "│", E_ | W_: "─", E_ | S_: "╭", W_ | S_: "╮", N_ | E_: "╰", N_ | W_: "╯",
           N_ | E_ | S_: "├", N_ | W_ | S_: "┤", E_ | W_ | S_: "┬", E_ | W_ | N_: "┴", 15: "┼",
           N_: "│", S_: "│", E_: "─", W_: "─"}
_JOIN_A = {N_ | S_: "|", E_ | W_: "-", N_: "|", S_: "|", E_: "-", W_: "-"}


def diagram_ranks(nodes: List[Dict[str, Any]],
                  edges: List[Dict[str, Any]]) -> Tuple[Dict[str, int], List[Tuple[str, str, int]]]:
    """Longest-path ranks after breaking cycles. Returns (rank by id, DAG edges (u, v, original index))."""
    ids = [n["id"] for n in nodes]
    known = set(ids)
    succ: Dict[str, List[Tuple[str, int]]] = {i: [] for i in ids}
    for k, e in enumerate(edges):
        a, b = e.get("from"), e.get("to")
        if a in known and b in known and a != b:
            succ[a].append((b, k))
    state: Dict[str, int] = {}
    back = set()
    for root in ids:
        if state.get(root):
            continue
        state[root] = 1
        stack = [(root, iter(succ[root]))]
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                state[node] = 2
                stack.pop()
                continue
            b, k = nxt
            st = state.get(b, 0)
            if st == 1:
                back.add(k)
            elif st == 0:
                state[b] = 1
                stack.append((b, iter(succ[b])))
    dag: List[Tuple[str, str, int]] = []
    seen = set()
    for k, e in enumerate(edges):
        a, b = e.get("from"), e.get("to")
        if a not in known or b not in known or a == b:
            continue
        uu, vv = (b, a) if k in back else (a, b)
        if (uu, vv) in seen:
            continue
        seen.add((uu, vv))
        dag.append((uu, vv, k))
    indeg = {i: 0 for i in ids}
    out: Dict[str, List[str]] = {i: [] for i in ids}
    for a, b, _ in dag:
        out[a].append(b)
        indeg[b] += 1
    rank = {i: 0 for i in ids}
    queue = [i for i in ids if indeg[i] == 0]
    qi = 0
    while qi < len(queue):
        nd = queue[qi]
        qi += 1
        for m in out[nd]:
            rank[m] = max(rank[m], rank[nd] + 1)
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
    return rank, dag


class DiagramLayout:
    def __init__(self) -> None:
        self.w = 0
        self.h = 0
        self.direction = "LR"
        self.ranks: Dict[str, int] = {}
        self.boxes: Dict[str, Tuple[int, int, int, int, List[str]]] = {}
        self.bits: Dict[Tuple[int, int], int] = {}
        self.colors: Dict[Tuple[int, int], Color] = {}
        self.arrows: List[Tuple[int, int, str, Color]] = []
        self.labels: List[Tuple[int, int, str, Color]] = []
        self.paths: List[List[Tuple[int, int]]] = []
        self.order: List[List[Any]] = []


def _order_layers(nodes: List[Dict[str, Any]], rank: Dict[str, int], dag: List[Tuple[str, str, int]]):
    """Layers with dummy nodes for long edges, ordered by barycenter sweeps to cut crossings."""
    nlayers = (max(rank.values()) + 1) if rank else 1
    layers: List[List[Any]] = [[] for _ in range(nlayers)]
    segs: List[Tuple[Any, Any, int]] = []
    chains: Dict[int, List[Any]] = {}
    first_seen: Dict[Any, int] = {}
    for idx, n in enumerate(nodes):
        first_seen[n["id"]] = idx
    for a, b, k in dag:
        ra, rb = rank[a], rank[b]
        chain = [a]
        for r in range(ra + 1, rb):
            d = ("~", k, r)
            first_seen[d] = first_seen.get(a, 0)
            chain.append(d)
        chain.append(b)
        chains[k] = chain
        for p, q in zip(chain, chain[1:]):
            segs.append((p, q, k))
    rank_of: Dict[Any, int] = dict(rank)
    for k, chain in chains.items():
        for d in chain[1:-1]:
            rank_of[d] = d[2]
    for key, r in rank_of.items():
        layers[r].append(key)
    for layer in layers:
        layer.sort(key=lambda z: first_seen.get(z, 0))
    preds: Dict[Any, List[Any]] = {}
    succs: Dict[Any, List[Any]] = {}
    for p, q, _ in segs:
        succs.setdefault(p, []).append(q)
        preds.setdefault(q, []).append(p)

    def crossings(lys: List[List[Any]]) -> int:
        total = 0
        for r in range(len(lys) - 1):
            pos_a = {z: i for i, z in enumerate(lys[r])}
            pos_b = {z: i for i, z in enumerate(lys[r + 1])}
            pairs = [(pos_a[p], pos_b[q]) for p, q, _ in segs if p in pos_a and q in pos_b]
            for i in range(len(pairs)):
                a1, b1 = pairs[i]
                for j in range(i + 1, len(pairs)):
                    a2, b2 = pairs[j]
                    if (a1 - a2) * (b1 - b2) < 0:
                        total += 1
        return total

    best = [list(ly) for ly in layers]
    best_c = crossings(best)
    cur = [list(ly) for ly in layers]
    for it in range(8):
        if best_c == 0:
            break
        rng_down = range(1, len(cur)) if it % 2 == 0 else range(len(cur) - 2, -1, -1)
        for r in rng_down:
            ref = cur[r - 1] if it % 2 == 0 else cur[r + 1]
            pos = {z: i for i, z in enumerate(ref)}
            nb = preds if it % 2 == 0 else succs

            def bc(z: Any, _pos: Dict[Any, int] = pos, _nb: Dict[Any, List[Any]] = nb, _r: int = r) -> float:
                ps = [_pos[p] for p in _nb.get(z, []) if p in _pos]
                return sum(ps) / len(ps) if ps else cur[_r].index(z)
            cur[r] = sorted(cur[r], key=bc)
        c = crossings(cur)
        if c < best_c:
            best_c, best = c, [list(ly) for ly in cur]
    return best, segs, rank_of


def _geometry(nodes: List[Dict[str, Any]], layers: List[List[Any]], segs: List[Tuple[Any, Any, int]],
              rank_of: Dict[Any, int], edges: List[Dict[str, Any]], direction: str, label_w: int,
              gap_min: int, vgap: int, u: bool, dag_rev: Dict[int, bool], label_cap: int = 18) -> DiagramLayout:
    L = DiagramLayout()
    L.direction = direction
    L.order = layers
    info = {n["id"]: n for n in nodes}
    size: Dict[Any, Tuple[int, int, List[str]]] = {}
    for key in rank_of:
        if isinstance(key, tuple):
            size[key] = (1, 1, [])
        else:
            lab = prep(info[key].get("label") or key, u)
            lines = wrap(lab, label_w, u)
            if len(lines) > 3:
                lines = lines[:3]
                lines[2] = clip(lines[2] + " …", label_w, u)
            bw = max(swidth(s) for s in lines) + 4
            size[key] = (bw, len(lines) + 2, lines)
    # gaps sized for edge labels between consecutive ranks
    nl = len(layers)
    gap_lab = [0] * max(0, nl - 1)
    for p, q, k in segs:
        lab = edges[k].get("label") or ""
        if lab:
            r = rank_of[p]
            if 0 <= r < len(gap_lab):
                gap_lab[r] = max(gap_lab[r], min(swidth(prep(lab, u)), label_cap))
    lanes_needed = [0] * max(0, nl - 1)
    for r in range(nl - 1):
        lanes_needed[r] = len({p for p, q, k in segs if rank_of[p] == r})
    LR = direction == "LR"
    pos: Dict[Any, Tuple[int, int]] = {}
    if LR:
        colw = [max([size[z][0] for z in ly if not isinstance(z, tuple)] or [1]) for ly in layers]
        gaps = [max(gap_min, gap_lab[r] + 6 + min(lanes_needed[r], 6) if gap_lab[r] else gap_min,
                    min(lanes_needed[r] + 4, 12)) for r in range(nl - 1)]
        colx = [0]
        for r in range(1, nl):
            colx.append(colx[-1] + colw[r - 1] + gaps[r - 1])
        L.w = colx[-1] + colw[-1] if nl else 0

        def extent(z: Any) -> int:
            return size[z][1]
        stacks = [sum(extent(z) for z in ly) + vgap * max(0, len(ly) - 1) for ly in layers]
        H = max(stacks) if stacks else 0
        ys: Dict[Any, int] = {}
        for r, ly in enumerate(layers):
            yy = (H - stacks[r]) // 2
            for z in ly:
                ys[z] = yy
                yy += extent(z) + vgap
        nbr: Dict[Any, List[Any]] = {}
        for p, q, _ in segs:
            nbr.setdefault(p, []).append(q)
            nbr.setdefault(q, []).append(p)
        for sweep in range(4):
            seq = range(nl) if sweep % 2 == 0 else range(nl - 1, -1, -1)
            for r in seq:
                ly = layers[r]
                want = []
                for z in ly:
                    cs = [ys[o] + extent(o) // 2 for o in nbr.get(z, []) if o in ys and rank_of[o] != r]
                    c = sum(cs) / len(cs) if cs else ys[z] + extent(z) // 2
                    want.append(int(round(c - extent(z) // 2)))
                prev_end = -vgap
                placed = []
                for z, wy in zip(ly, want):
                    yy = max(wy, prev_end + vgap, 0)
                    placed.append(yy)
                    prev_end = yy + extent(z)
                limit = H
                for i in range(len(ly) - 1, -1, -1):
                    z = ly[i]
                    if placed[i] + extent(z) > limit:
                        placed[i] = limit - extent(z)
                    limit = placed[i] - vgap
                for z, yy in zip(ly, placed):
                    ys[z] = max(0, yy)
        L.h = H
        for r, ly in enumerate(layers):
            for z in ly:
                bw, bh, lines = size[z]
                if isinstance(z, tuple):
                    pos[z] = (colx[r], ys[z])
                else:
                    bx = colx[r] + (colw[r] - bw) // 2
                    pos[z] = (bx, ys[z])
                    L.boxes[z] = (bx, ys[z], bw, bh, lines)
        L.ranks = {k: v for k, v in rank_of.items() if not isinstance(k, tuple)}
        # routing
        incoming: Dict[Any, int] = {}
        for p, q, k in segs:
            incoming[q] = incoming.get(q, 0) + 1
        lane_of: Dict[Tuple[int, Any], int] = {}
        for r in range(nl - 1):
            g0 = colx[r] + colw[r]
            g1 = colx[r + 1] - 1
            groups = sorted({p for p, q, k in segs if rank_of[p] == r and
                             _port_lr(p, pos, size)[1] != _port_lr_in(q, pos, size)[1]},
                            key=lambda z: _port_lr(z, pos, size)[1])
            lo_x, hi_x = g0 + 1, g1 - 2
            src_side = {p for p, q, k in segs if rank_of[p] == r and edges[k].get("label") and incoming.get(q, 0) > 1}
            left = [p for p in groups if p not in src_side]
            right = [p for p in groups if p in src_side]
            for i, p in enumerate(groups):
                if hi_x <= lo_x:
                    lx = (g0 + g1) // 2
                elif gap_lab[r]:
                    # bundle lanes by the side that has no label, so each label gets a long straight run
                    lx = min(lo_x + left.index(p), hi_x) if p in left else max(hi_x - right.index(p), lo_x)
                else:
                    lx = lo_x + int(round((i + 1) * (hi_x - lo_x) / (len(groups) + 1)))
                lane_of[(r, p)] = lx
        paths: Dict[int, List[Tuple[int, int]]] = {}
        label_spots: Dict[int, List[Tuple[int, int, int]]] = {}
        for p, q, k in segs:
            r = rank_of[p]
            sx, sy = _port_lr(p, pos, size)
            tx, ty = _port_lr_in(q, pos, size)
            col = fx.scale(fx.grad(SPECTRUM, r / max(1, nl - 1) * 0.85 + 0.05), 0.9)
            pts: List[Tuple[int, int]] = []
            if isinstance(p, tuple):
                _seg(L, colx[r] - 1, sy, sx, sy, col, pts)
            if sy == ty:
                _seg(L, sx, sy, tx, ty, col, pts)
                label_spots.setdefault(k, []).append((sx, tx, sy))
            else:
                lx = lane_of.get((r, p), (sx + tx) // 2)
                _seg(L, sx, sy, lx, sy, col, pts)
                _seg(L, lx, sy, lx, ty, col, pts)
                _seg(L, lx, ty, tx, ty, col, pts)
                label_spots.setdefault(k, []).append((lx + 1, tx, ty) if incoming.get(q, 0) <= 1 else (sx, lx - 1, sy))
            paths.setdefault(k, []).extend(pts)
            if not isinstance(q, tuple):
                rev = dag_rev.get(k, False)
                if not rev:
                    L.arrows.append((tx, ty, "▶" if u else ">", _node_color(q, info, L.ranks, nl)))
            if not isinstance(p, tuple) and dag_rev.get(k, False):
                L.arrows.append((sx, sy, "◀" if u else "<", _node_color(p, info, L.ranks, nl)))
        _place_labels(L, edges, label_spots, u, horizontal=True)
        L.paths = [paths[k] for k in sorted(paths)]
        return L
    # TB
    roww = [0] * nl
    rowh = [max([size[z][1] for z in ly if not isinstance(z, tuple)] or [1]) for ly in layers]
    hg = 3
    gaps = [max(3, min(lanes_needed[r] + 2, 6)) for r in range(nl - 1)]
    rowy = [0]
    for r in range(1, nl):
        rowy.append(rowy[-1] + rowh[r - 1] + gaps[r - 1])
    L.h = rowy[-1] + rowh[-1] if nl else 0
    for r, ly in enumerate(layers):
        roww[r] = sum(size[z][0] for z in ly) + hg * max(0, len(ly) - 1)
    W = max(roww) if roww else 0
    xs: Dict[Any, int] = {}
    for r, ly in enumerate(layers):
        xx = (W - roww[r]) // 2
        for z in ly:
            xs[z] = xx
            xx += size[z][0] + hg
    nbr2: Dict[Any, List[Any]] = {}
    for p, q, _ in segs:
        nbr2.setdefault(p, []).append(q)
        nbr2.setdefault(q, []).append(p)
    for sweep in range(4):
        seq = range(nl) if sweep % 2 == 0 else range(nl - 1, -1, -1)
        for r in seq:
            ly = layers[r]
            want = []
            for z in ly:
                cs = [xs[o] + size[o][0] // 2 for o in nbr2.get(z, []) if rank_of[o] != r]
                c = sum(cs) / len(cs) if cs else xs[z] + size[z][0] // 2
                want.append(int(round(c - size[z][0] // 2)))
            prev_end = -hg
            placed = []
            for z, wx in zip(ly, want):
                xx = max(wx, prev_end + hg, 0)
                placed.append(xx)
                prev_end = xx + size[z][0]
            limit = W
            for i in range(len(ly) - 1, -1, -1):
                z = ly[i]
                if placed[i] + size[z][0] > limit:
                    placed[i] = limit - size[z][0]
                limit = placed[i] - hg
            for z, xx in zip(ly, placed):
                xs[z] = max(0, xx)
    L.w = W
    for r, ly in enumerate(layers):
        for z in ly:
            bw, bh, lines = size[z]
            if isinstance(z, tuple):
                pos[z] = (xs[z], rowy[r])
            else:
                by = rowy[r] + (rowh[r] - bh) // 2
                pos[z] = (xs[z], by)
                L.boxes[z] = (xs[z], by, bw, bh, lines)
    L.ranks = {k: v for k, v in rank_of.items() if not isinstance(k, tuple)}
    lane_of2: Dict[Tuple[int, Any], int] = {}
    for r in range(nl - 1):
        g0 = rowy[r] + rowh[r]
        g1 = rowy[r + 1] - 1
        groups = sorted({p for p, q, k in segs if rank_of[p] == r and
                         _port_tb(p, pos, size)[0] != _port_tb_in(q, pos, size)[0]},
                        key=lambda z: _port_tb(z, pos, size)[0])
        lo_y, hi_y = g0 + 1, g1 - 1
        for i, p in enumerate(groups):
            if hi_y <= lo_y:
                ly_ = (g0 + g1) // 2
            else:
                ly_ = (lo_y + int(round(i * (hi_y - lo_y) / max(1, len(groups) - 1))) if len(groups) > 1
                       else (lo_y + hi_y) // 2)
            lane_of2[(r, p)] = ly_
    paths2: Dict[int, List[Tuple[int, int]]] = {}
    spots: Dict[int, List[Tuple[int, int, int]]] = {}
    for p, q, k in segs:
        r = rank_of[p]
        sx, sy = _port_tb(p, pos, size)
        tx, ty = _port_tb_in(q, pos, size)
        col = fx.scale(fx.grad(SPECTRUM, r / max(1, nl - 1) * 0.85 + 0.05), 0.9)
        pts = []
        if isinstance(p, tuple):
            _seg(L, sx, rowy[r] - 1, sx, sy, col, pts)
        if sx == tx:
            _seg(L, sx, sy, tx, ty, col, pts)
            spots.setdefault(k, []).append((sx + 2, sx + 2 + 18, (sy + ty) // 2))
        else:
            ly_ = lane_of2.get((r, p), (sy + ty) // 2)
            _seg(L, sx, sy, sx, ly_, col, pts)
            _seg(L, sx, ly_, tx, ly_, col, pts)
            _seg(L, tx, ly_, tx, ty, col, pts)
            a, b = (min(sx, tx) + 1, max(sx, tx) - 1)
            spots.setdefault(k, []).append((a, b, ly_) if b - a > 4 else (tx + 2, tx + 20, ly_ + 1))
        paths2.setdefault(k, []).extend(pts)
        if not isinstance(q, tuple) and not dag_rev.get(k, False):
            L.arrows.append((tx, ty, "▼" if u else "v", _node_color(q, info, L.ranks, nl)))
        if not isinstance(p, tuple) and dag_rev.get(k, False):
            L.arrows.append((sx, sy, "▲" if u else "^", _node_color(p, info, L.ranks, nl)))
    _place_labels(L, edges, spots, u, horizontal=True)
    L.paths = [paths2[k] for k in sorted(paths2)]
    return L


def _port_lr(z: Any, pos: Dict[Any, Tuple[int, int]], size: Dict[Any, Tuple[int, int, List[str]]]) -> Tuple[int, int]:
    x, y = pos[z]
    bw, bh, _ = size[z]
    if isinstance(z, tuple):
        return x + bw, y
    return x + bw, y + bh // 2


def _port_lr_in(z: Any, pos: Dict[Any, Tuple[int, int]], size: Dict[Any, Tuple[int, int, List[str]]]) -> Tuple[int, int]:
    x, y = pos[z]
    bw, bh, _ = size[z]
    if isinstance(z, tuple):
        return x - 1, y
    return x - 1, y + bh // 2


def _port_tb(z: Any, pos: Dict[Any, Tuple[int, int]], size: Dict[Any, Tuple[int, int, List[str]]]) -> Tuple[int, int]:
    x, y = pos[z]
    bw, bh, _ = size[z]
    if isinstance(z, tuple):
        return x, y + 1
    return x + bw // 2, y + bh


def _port_tb_in(z: Any, pos: Dict[Any, Tuple[int, int]], size: Dict[Any, Tuple[int, int, List[str]]]) -> Tuple[int, int]:
    x, y = pos[z]
    bw, bh, _ = size[z]
    if isinstance(z, tuple):
        return x, y - 1
    return x + bw // 2, y - 1


def _seg(L: DiagramLayout, x0: int, y0: int, x1: int, y1: int, col: Color, pts: List[Tuple[int, int]]) -> None:
    """Add an axis-aligned segment to the line layer (bits record which way each cell connects)."""
    if x0 == x1 and y0 == y1:
        L.bits[(x0, y0)] = L.bits.get((x0, y0), 0)
        L.colors[(x0, y0)] = col
        pts.append((x0, y0))
        return
    if y0 == y1:
        step = 1 if x1 > x0 else -1
        xs = list(range(x0, x1 + step, step))
        for i, xx in enumerate(xs):
            b = 0
            if i > 0:
                b |= W_ if step > 0 else E_
            if i < len(xs) - 1:
                b |= E_ if step > 0 else W_
            L.bits[(xx, y0)] = L.bits.get((xx, y0), 0) | b
            L.colors[(xx, y0)] = col
            pts.append((xx, y0))
    else:
        step = 1 if y1 > y0 else -1
        ys = list(range(y0, y1 + step, step))
        for i, yy in enumerate(ys):
            b = 0
            if i > 0:
                b |= N_ if step > 0 else S_
            if i < len(ys) - 1:
                b |= S_ if step > 0 else N_
            L.bits[(x0, yy)] = L.bits.get((x0, yy), 0) | b
            L.colors[(x0, yy)] = col
            pts.append((x0, yy))


def _node_color(z: Any, info: Dict[str, Dict[str, Any]], ranks: Dict[str, int], nl: int) -> Color:
    tone = info.get(z, {}).get("tone") or ""
    if tone in TONES:
        return TONES[tone][0]
    return fx.grad(SPECTRUM, ranks.get(z, 0) / max(1, nl - 1) * 0.85 + 0.05)


def _place_labels(L: DiagramLayout, edges: List[Dict[str, Any]], spots: Dict[int, List[Tuple[int, int, int]]],
                  u: bool, horizontal: bool = True) -> None:
    taken: List[Tuple[int, int, int]] = []
    for k, cands in spots.items():
        lab = prep(edges[k].get("label") or "", u)
        if not lab:
            continue
        best = max(cands, key=lambda c: c[1] - c[0])
        a, b, yy = best
        room = b - a - 1
        if room < 3:
            continue
        txt = clip(lab, room - 2, u)
        lw = swidth(txt) + 2
        lx = a + (room - lw) // 2 + 1
        if any(ty == yy and not (lx + lw <= tx0 or lx >= tx1) for tx0, tx1, ty in taken):
            continue
        taken.append((lx, lx + lw, yy))
        L.labels.append((lx, yy, " " + txt + " ", ICE))


@lru_cache(maxsize=48)
def _layout_cached(nodes_key: Tuple[Tuple[str, str, str], ...], edges_key: Tuple[Tuple[str, str, str], ...],
                   direction: str, w: int, h: int, u: bool) -> Optional[DiagramLayout]:
    nodes = [{"id": a, "label": b, "tone": c} for a, b, c in nodes_key]
    edges = [{"from": a, "to": b, "label": c} for a, b, c in edges_key]
    rank, dag = diagram_ranks(nodes, edges)
    layers, segs, rank_of = _order_layers(nodes, rank, dag)
    rev = {}
    for a, b, k in dag:
        rev[k] = (edges[k]["from"] != a)
    other = "TB" if direction == "LR" else "LR"
    attempts = [(direction, 22, 8, 1, 18), (direction, 22, 6, 1, 10), (direction, 16, 5, 1, 8),
                (direction, 22, 4, 0, 6), (direction, 12, 4, 0, 5), (other, 18, 6, 1, 12), (other, 12, 4, 1, 8),
                (direction, 9, 4, 0, 4), (other, 8, 4, 0, 4), (direction, 8, 3, 0, 0), (other, 8, 3, 0, 0)]
    for dirn, lw, gm, vg, cap in attempts:
        if vg == 1 and dirn == "LR":
            L = _geometry(nodes, layers, segs, rank_of, edges, dirn, lw, gm, 2, u, rev, cap)
            if L.w <= w and L.h <= h:
                return L
        L = _geometry(nodes, layers, segs, rank_of, edges, dirn, lw, gm, vg if dirn == "LR" else 1, u, rev, cap)
        if L.w <= w and L.h <= h:
            if cap == 0:
                L.labels = []
            return L
    return None


def layout_diagram(nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]], direction: str = "LR",
                   w: int = 200, h: int = 100, u: bool = True) -> Optional[DiagramLayout]:
    """Layered layout that fits w x h (trying tighter spacing, then the other direction), or None."""
    nk = tuple((str(n.get("id")), str(n.get("label") or n.get("id")), str(n.get("tone") or "")) for n in nodes)
    ek = tuple((str(e.get("from")), str(e.get("to")), str(e.get("label") or "")) for e in edges)
    return _layout_cached(nk, ek, "TB" if str(direction).upper() == "TB" else "LR", int(w), int(h), bool(u))


def draw_diagram(cv: Any, x: int, y: int, w: int, h: int, scene: Dict[str, Any], u: Optional[bool] = None,
                 t: float = SETTLED, motion: bool = False) -> None:
    v = View(cv, x, y, w, h, u)
    nodes = list(scene.get("nodes") or [])
    edges = list(scene.get("edges") or [])
    if not nodes or v.w < 4 or v.h < 1:
        return
    L = layout_diagram(nodes, edges, scene.get("direction") or "LR", v.w, v.h, v.u)
    if L is None:
        _diagram_list(v, nodes, edges, t)
        return
    ox = (v.w - L.w) // 2
    oy = max(0, (v.h - L.h) // 2)
    nl = (max(L.ranks.values()) + 1) if L.ranks else 1
    shown_rank = (t / 0.9) * nl + 0.6
    info = {n["id"]: n for n in nodes}
    join = _JOIN_U if v.u else _JOIN_A
    # edges first (boxes overdraw their ends)
    for (cx, cy), bits in L.bits.items():
        ch = join.get(bits, "+" if not v.u else "┼")
        col = L.colors.get((cx, cy), DUST)
        v.put(ox + cx, oy + cy, ch, col)
    if motion and t > 1.0:
        for pi, path in enumerate(L.paths):
            if len(path) < 3:
                continue
            head = int((t * 16 + pi * 9) % (len(path) + 10))
            for k in range(4):
                idx = head - k
                if 0 <= idx < len(path):
                    px, py = path[idx]
                    cell = v.get(ox + px, oy + py)
                    if cell[0] not in (" ", ""):
                        v.put(ox + px, oy + py, cell[0], fx.lerp(ICE, BEAM, k / 4), k == 0)
    for ax, ay, ch, col in L.arrows:
        v.put(ox + ax, oy + ay, ch, col, True)
    for lx, ly, txt, col in L.labels:
        v.text(ox + lx, oy + ly, txt, SOFT)
    for nid, (bx, by, bw, bh, lines) in L.boxes.items():
        r = L.ranks.get(nid, 0)
        if r > shown_rank:
            v.fill(ox + bx, oy + by, bw, bh)
            continue
        k = ease((shown_rank - r) / 1.2)
        col = _node_color(nid, info, L.ranks, nl)
        v.fill(ox + bx + 1, oy + by + 1, bw - 2, bh - 2)
        tone = info.get(nid, {}).get("tone") or ""
        stops = TONES[tone] if tone in TONES else (col, fx.lerp(col, ICE, 0.35))
        v.box(ox + bx, oy + by, bw, bh, stops=stops, phase=(t * 0.15 + r * 0.2) if motion else 0.0,
              bright=0.5 + 0.5 * k)
        for i, ln in enumerate(lines):
            lx = ox + bx + (bw - swidth(ln)) // 2
            v.text(lx, oy + by + 1 + i, ln, fx.lerp(NIGHT, ICE, k), True)
    # hide edges into not-yet-revealed nodes
    if shown_rank < nl:
        for nid, (bx, by, bw, bh, lines) in L.boxes.items():
            if L.ranks.get(nid, 0) > shown_rank:
                v.fill(ox + bx - 1, oy + by, bw + 2, bh)


def _diagram_list(v: View, nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]], t: float) -> None:
    """Compact fallback when the graph cannot fit: one edge per line."""
    rank, _ = diagram_ranks(nodes, edges)
    nl = (max(rank.values()) + 1) if rank else 1
    info = {n["id"]: n for n in nodes}
    lab = {n["id"]: n.get("label") or n["id"] for n in nodes}
    rows: List[Tuple[str, str, str]] = []
    linked = set()
    for e in edges:
        a, b = e.get("from"), e.get("to")
        if a in lab and b in lab:
            rows.append((a, b, e.get("label") or ""))
            linked.update((a, b))
    for n in nodes:
        if n["id"] not in linked:
            rows.append((n["id"], "", ""))
    arrow = " ──▶ " if v.u else " --> "
    # When rows overflow, the last line is the "N more" note: draw one row fewer so it never lands on
    # top of an edge (which left the tail of that edge's label showing after the note).
    fit = v.h - 1 if len(rows) > v.h and v.h > 1 else v.h
    for i, (a, b, lb) in enumerate(rows[:fit]):
        if t < i * 0.04:
            break
        x = 0
        v.put(x, i, "◉" if v.u else "@", _node_color(a, info, rank, nl), True)
        x = v.text(x + 2, i, clip(lab[a], max(4, v.w // 3), v.u), ICE, True)
        if b:
            x = v.text(x, i, arrow, DUST)
            x = v.text(x, i, clip(lab[b], max(4, v.w // 3), v.u), _node_color(b, info, rank, nl), True)
            if lb:
                v.text(x + 2, i, clip(lb, max(0, v.w - x - 2), v.u), SOFT)
    if len(rows) > v.h and v.h > 1:
        v.text(0, v.h - 1, ("… %d more" if v.u else "~ %d more") % (len(rows) - fit), DUST)


# ---------------------------------------------------------------------------
# Code: syntax highlighting + panel
# ---------------------------------------------------------------------------

C_KW: Color = (255, 92, 168)
C_STR: Color = (255, 184, 108)
C_NUM: Color = (196, 150, 255)
C_COM: Color = (112, 108, 136)
C_FN: Color = (110, 222, 255)
C_TYPE: Color = (124, 240, 214)
C_DEC: Color = (186, 132, 255)
C_PUN: Color = (150, 146, 178)
C_TXT: Color = (216, 220, 236)
C_KEY: Color = (110, 222, 255)
C_LIT: Color = (255, 128, 128)

_LANG_ALIAS = {"python": "py", "py3": "py", "python3": "py", "javascript": "js", "jsx": "js", "mjs": "js",
               "cjs": "js", "node": "js", "typescript": "ts", "tsx": "ts", "golang": "go", "rs": "rust",
               "bash": "sh", "shell": "sh", "zsh": "sh", "console": "sh", "fish": "sh", "ksh": "sh", "ps1": "sh",
               "powershell": "sh", "yml": "yaml", "postgres": "sql", "postgresql": "sql", "mysql": "sql",
               "sqlite": "sql", "jsonc": "json", "json5": "json", "toml": "yaml", "ini": "yaml", "c": "generic",
               "cpp": "generic", "java": "generic", "kotlin": "generic", "swift": "generic", "rb": "generic",
               "ruby": "generic", "cs": "generic", "php": "generic", "": "generic", "text": "generic"}

_KW = {
    "py": "False None True and as assert async await break class continue def del elif else except finally for from "
          "global if import in is lambda nonlocal not or pass raise return try while with yield match case self cls",
    "js": "break case catch class const continue debugger default delete do else export extends finally for function "
          "if import in instanceof let new return super switch this throw try typeof var void while with yield async "
          "await of static get set from as",
    "ts": "break case catch class const continue debugger default delete do else export extends finally for function "
          "if import in instanceof let new return super switch this throw try typeof var void while with yield async "
          "await of static get set from as interface type enum implements private public protected readonly declare "
          "namespace abstract keyof satisfies is infer",
    "go": "break case chan const continue default defer else fallthrough for func go goto if import interface map "
          "package range return select struct switch type var",
    "rust": "as async await break const continue crate dyn else enum extern fn for if impl in let loop match mod move "
            "mut pub ref return self Self static struct super trait type unsafe use where while",
    "sh": "if then else elif fi for while until do done case esac function in return export local readonly set unset "
          "echo exit source alias cd test shift trap eval exec sudo",
    "sql": "select from where join left right inner outer full cross on group by order having limit offset insert into "
           "values update set delete create alter drop table index view primary key foreign references not null and "
           "or as distinct union all case when then else end with returning default unique check exists between like "
           "in is asc desc begin commit rollback",
    "generic": "if else for while do return break continue function def class import from export const let var new "
               "public private protected static void int float double bool string struct enum switch case default "
               "try catch finally throw throws interface package fn func match use mod impl trait",
}
_LIT = {"py": "", "js": "true false null undefined NaN Infinity", "ts": "true false null undefined NaN Infinity never "
        "unknown any string number boolean void object", "go": "nil true false iota", "rust": "true false Some None Ok Err",
        "sh": "true false", "sql": "true false null", "generic": "true false null nil None True False"}
_BUILTIN = {"py": "print len range str int float dict list set tuple bool type isinstance super open enumerate zip "
            "map filter sorted min max sum any all repr hasattr getattr setattr object Exception ValueError TypeError "
            "KeyError RuntimeError", "go": "string int int64 int32 uint8 byte rune error bool float64 float32 any make "
            "len cap append new panic", "rust": "i8 i16 i32 i64 u8 u16 u32 u64 usize isize f32 f64 bool char str "
            "String Vec Option Result Box"}


@lru_cache(maxsize=32)
def _lang_rx(lang: str) -> "re.Pattern[str]":
    parts = []
    if lang in ("py", "sh", "yaml", "generic"):
        parts.append(r"(?P<com>#.*$)" if lang != "generic" else r"(?P<com>(?://|#).*$)")
    if lang in ("js", "ts", "go", "rust", "generic", "json"):
        parts.append(r"(?P<bcom>/\*.*?(?:\*/|$))")
        if lang != "generic":
            parts.append(r"(?P<com>//.*$)")
    if lang == "sql":
        parts.append(r"(?P<com>--.*$)")
        parts.append(r"(?P<bcom>/\*.*?(?:\*/|$))")
    if lang == "py":
        parts.append(r"(?P<tri>[rRbBfFuU]{0,2}(?P<q>\"\"\"|''')(?:.*?(?P=q)|.*$))")
        parts.append(r"(?P<dec>^\s*@[\w.]+)")
    if lang == "json":
        parts.append(r"(?P<key>\"(?:[^\"\\]|\\.)*\"(?=\s*:))")
    if lang == "yaml":
        parts.append(r"(?P<key>^\s*(?:-\s+)?[^\s#:\"'][^#:]*?(?=\s*:(?:\s|$)))")
    q = r"\"(?:[^\"\\]|\\.)*\"?|'(?:[^'\\]|\\.)*'?"
    if lang in ("js", "ts", "go"):
        q += r"|`[^`]*`?"
    if lang == "rust":
        q = r"\"(?:[^\"\\]|\\.)*\"?|'(?:[^'\\]|\\.)'"
    parts.append(r"(?P<str>(?:[rRbBfFuU]{1,2})?(?:%s))" % q if lang == "py" else r"(?P<str>%s)" % q)
    if lang == "sh":
        parts.append(r"(?P<var>\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*|\$[0-9@#?$!*-])")
    parts.append(r"(?P<num>\b(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?)\b)")
    kw = _KW.get(lang, "")
    if kw:
        parts.append(r"(?P<kw>\b(?:%s)\b)" % "|".join(sorted(set(kw.split()), key=len, reverse=True)))
    lit = _LIT.get(lang, "")
    if lit:
        parts.append(r"(?P<lit>\b(?:%s)\b)" % "|".join(lit.split()))
    bi = _BUILTIN.get(lang, "")
    if bi:
        parts.append(r"(?P<bi>\b(?:%s)\b)" % "|".join(bi.split()))
    if lang == "rust":
        parts.append(r"(?P<mac>\b[a-z_][a-z0-9_]*!)")
    if lang not in ("json", "yaml", "sql"):
        parts.append(r"(?P<fn>\b[A-Za-z_]\w*(?=\s*\())")
    if lang in ("ts", "go", "rust", "py", "js", "generic"):
        parts.append(r"(?P<typ>\b[A-Z][A-Za-z0-9_]*\b)")
    parts.append(r"(?P<pun>[{}\[\]();,.:<>=+\-*/%!&|^~?@]+)")
    flags = re.M | (re.I if lang == "sql" else 0)
    return re.compile("|".join(parts), flags)


_TOKCOL = {"com": C_COM, "bcom": C_COM, "tri": C_STR, "str": C_STR, "num": C_NUM, "kw": C_KW, "lit": C_LIT,
           "bi": C_TYPE, "fn": C_FN, "typ": C_TYPE, "pun": C_PUN, "dec": C_DEC, "key": C_KEY, "var": C_DEC,
           "mac": C_FN}


def norm_lang(lang: str) -> str:
    lang = (lang or "").strip().lower()
    if lang in ("py", "js", "ts", "go", "rust", "sh", "json", "yaml", "sql", "generic"):
        return lang
    return _LANG_ALIAS.get(lang, "generic")


@lru_cache(maxsize=64)
def _highlight_cached(text: str, lang: str) -> Tuple[Tuple[Seg, ...], ...]:
    lang = norm_lang(lang)
    rx = _lang_rx(lang)
    out: List[Tuple[Seg, ...]] = []
    state = ""          # "" | "bcom" | '"""' | "'''"
    for line in text.split("\n"):
        segs: List[Seg] = []
        pos = 0
        if state:
            end_tok = "*/" if state == "bcom" else state
            j = line.find(end_tok)
            col = C_COM if state == "bcom" else C_STR
            if j < 0:
                out.append(((line, col, False),) if line else ())
                continue
            segs.append((line[:j + len(end_tok)], col, False))
            pos = j + len(end_tok)
            state = ""
        for m in rx.finditer(line, pos):
            if m.start() > pos:
                segs.append((line[pos:m.start()], C_TXT, False))
            kind = m.lastgroup or "pun"
            if kind == "q":
                kind = "tri"
            tok = m.group(0)
            if kind == "bcom" and not tok.endswith("*/"):
                state = "bcom"
            elif kind == "tri":
                qq = m.group("q") or '"""'
                body = tok[tok.index(qq) + 3:]
                if not body.endswith(qq):
                    state = qq
            segs.append((tok, _TOKCOL.get(kind, C_TXT), kind == "kw"))
            pos = m.end()
        if pos < len(line):
            segs.append((line[pos:], C_TXT, False))
        out.append(tuple(segs))
    return tuple(out)


def highlight(text: str, lang: str = "") -> List[Line]:
    """Per-line styled segments for code (regex-light, stateful for block comments / triple quotes)."""
    return [list(s) for s in _highlight_cached(text or "", lang or "")]


def draw_code(cv: Any, x: int, y: int, w: int, h: int, code: Dict[str, Any], u: Optional[bool] = None,
              t: float = SETTLED, scroll: Optional[int] = None, frame: bool = True) -> int:
    """Line-numbered code panel. Returns hidden line count."""
    v = View(cv, x, y, w, h, u)
    if v.w < 8 or v.h < 3:
        return 0
    text = (code or {}).get("text") or ""
    lang = (code or {}).get("lang") or ""
    fname = (code or {}).get("filename") or ""
    hl = set(int(n) for n in ((code or {}).get("highlight") or []))
    lines = highlight(text, lang)
    while lines and not any(s.strip() for s, _, _ in lines[-1]):
        lines.pop()
    nlines = len(lines)
    if frame and nlines + 2 < v.h:
        v = View(v, 0, 0, v.w, max(3, nlines + 2))
    inner_x, inner_y, inner_w, inner_h = (1, 1, v.w - 2, v.h - 2) if frame else (0, 0, v.w, v.h)
    if frame:
        v.box(0, 0, v.w, v.h, stops=(fx.scale(PLASMA, 0.7), fx.scale(BEAM, 0.55)), bright=0.8)
        tx = 2
        if fname:
            tx = v.text(tx, 0, " " + clip(fname, max(4, v.w // 2), v.u) + " ", ICE, True)
        lang_n = norm_lang(lang) if lang else ""
        tag = (lang or lang_n)
        if tag and tag != "generic":
            label = " " + tag + " "
            if tx + swidth(label) + 2 < v.w:
                v.text(v.w - swidth(label) - 2, 0, label, DUST)
    if inner_h <= 0:
        return 0
    gw = len(str(max(1, nlines)))
    gutter = gw + 3
    if scroll is None:
        start = 0
        if hl and nlines > inner_h:
            first = min(hl)
            start = max(0, min(first - 1 - max(1, inner_h // 4), nlines - inner_h))
    else:
        start = int(clamp(scroll, 0, max(0, nlines - inner_h)))
    vis = lines[start:start + inner_h]
    total_chars = sum(sum(len(s) for s, _, _ in ln) for ln in vis) or 1
    budget = int(ease(t / 0.9) * total_chars) if t < 0.9 else 10 ** 9
    cursor_drawn = False
    cw_avail = inner_w - gutter
    for i, segs in enumerate(vis):
        n = start + i + 1
        yy = inner_y + i
        is_hl = n in hl
        dim = 0.55 if (hl and not is_hl) else 1.0
        if is_hl:
            v.put(inner_x, yy, "▌" if v.u else ">", FLARE, True)
        num = str(n).rjust(gw)
        v.text(inner_x + 1, yy, num, BEAM if is_hl else fx.scale(DUST, 0.9), is_hl)
        v.put(inner_x + gw + 2, yy, "│" if v.u else "|", FAINT if not is_hl else fx.scale(FLARE, 0.6))
        cx = inner_x + gutter
        limit = inner_x + inner_w
        clipped = False
        for txt, col, bold in segs:
            if budget <= 0:
                break
            if len(txt) > budget:
                txt = txt[:budget]
            budget -= len(txt)
            txt = prep(txt, v.u)
            room = limit - cx
            if room <= 0:
                clipped = True
                break
            if swidth(txt) > room:
                txt = clip(txt, room, v.u)
                clipped = True
            cx = v.text(cx, yy, txt, fx.scale(col, dim) if dim != 1 else col, bold)
        if clipped and cw_avail > 2:
            v.put(limit - 1, yy, "›" if v.u else ">", DUST)
        if budget <= 0 and not cursor_drawn and t < 0.9:
            v.put(min(cx, limit - 1), yy, "▌" if v.u else "_", BEAM, True)
            cursor_drawn = True
    hidden = max(0, nlines - inner_h)
    if hidden and frame:
        notes = []
        if start:
            notes.append(("↑ %d" if v.u else "^ %d") % start)
        below = nlines - start - len(vis)
        if below:
            notes.append(("↓ %d more" if v.u else "v %d more") % below)
        if notes:
            note = " " + "  ".join(notes) + " "
            v.text(v.w - swidth(note) - 2, v.h - 1, note, ASH)
    return hidden


# ---------------------------------------------------------------------------
# Metrics, timeline, progress, card
# ---------------------------------------------------------------------------

_NUM_PART = re.compile(r"^(\D*?)(-?\d[\d,]*\.?\d*|-?\.\d+)(.*)$")


def count_up(value: str, k: float) -> str:
    """Render a numeric value partway through a count-up (k in 0..1), keeping its format."""
    if k >= 1:
        return value
    m = _NUM_PART.match(value or "")
    if not m:
        return value
    pre, num, post = m.groups()
    try:
        target = float(num.replace(",", ""))
    except ValueError:
        return value
    dec = len(num.split(".")[1]) if "." in num else 0
    cur = target * k
    s = ("{:,.%df}" % dec).format(cur) if "," in num else ("%." + str(dec) + "f") % cur
    return pre + s + post


def _trend_area(v: View, x: int, y: int, w: int, h: int, values: Sequence[float], t: float = SETTLED,
                stops: Sequence[Color] = SPECTRUM) -> None:
    """A small filled area chart (eighth blocks), h rows tall, bottom at y + h - 1."""
    vals = finite(values)
    if not vals or w <= 0 or h <= 0:
        return
    if not v.u or h == 1:
        draw_sparkline(v, x, y + h - 1, w, vals, t=t)
        return
    rs = resample(vals, w)
    lo, hi = min(vals), max(vals)
    k = ease(t / 0.8)
    for cx, val in enumerate(rs):
        if cx > k * w:
            break
        lvl = max(1, int(round(((val - lo) / (hi - lo) if hi > lo else 0.5) * h * 8)))
        col = fx.grad(stops, cx / max(1, w - 1))
        for r in range(h):
            cell = lvl - r * 8
            if cell <= 0:
                break
            v.put(x + cx, y + h - 1 - r, "█" if cell >= 8 else SPARK_U[cell - 1], fx.scale(col, 0.4 + 0.6 * (r + 1) / h))


def draw_metrics(cv: Any, x: int, y: int, w: int, h: int, metrics: List[Dict[str, Any]], u: Optional[bool] = None,
                 t: float = SETTLED, motion: bool = False) -> None:
    v = View(cv, x, y, w, h, u)
    ms = list(metrics or [])
    if not ms or v.w < 8 or v.h < 3:
        return
    n = len(ms)
    cols = max(1, min(n, v.w // 22))
    rows = math.ceil(n / cols)
    while rows > 1 and rows * 5 > v.h and v.w // (cols + 1) >= 16 and cols < n:
        cols += 1
        rows = math.ceil(n / cols)
    gap = 2 if v.w >= 60 else 1
    tw = (v.w - gap * (cols - 1)) // cols
    big = v.u and all(big_value_width(m.get("value") or "", "compact") <= tw - 4 and len(m.get("value") or "") <= 14
                      for m in ms)
    want = (9 if big else 7) if any(m.get("trend") for m in ms) else (7 if big else 5)
    th = int(clamp((v.h - (rows - 1)) // rows, 3, want))
    if big and th < 7:
        big = False
        th = int(clamp((v.h - (rows - 1)) // rows, 3, 7))
    for i, m in enumerate(ms):
        r, c = divmod(i, cols)
        tx = c * (tw + gap)
        ty = r * (th + 1)
        if ty + th > v.h:
            break
        appear = 0.06 * i
        if t < appear:
            continue
        k = ease((t - appear) / 0.6)
        hue = (i / max(1, n - 1)) * 0.9
        stops = (fx.grad(SPECTRUM, hue), fx.scale(PLASMA, 0.6), fx.scale(BEAM, 0.5))
        v.box(tx, ty, tw, th, stops=stops, phase=(t * 0.12 + i * 0.17) if motion else i * 0.17, bright=0.45 + 0.45 * k)
        inner = View(v, tx + 2, ty + 1, tw - 4, th - 2)
        label = prep(m.get("label") or "", v.u).upper()
        delta = prep(m.get("delta") or "", v.u).strip()
        dtxt, dcol = "", ASH
        if delta:
            up = delta[:1] in ("+", "▲", "↑")
            down = delta[:1] in ("-", "▼", "↓", "−")
            dcol = OK if up else BAD if down else ASH
            body = delta.lstrip("+-▲▼↑↓−").strip() if (up or down) else delta
            dtxt = ((("▲ " if up else "▼ ") if (up or down) else "") + body) if v.u else delta
        dw = swidth(dtxt)
        val = count_up(m.get("value") or "", ease((t - appear) / 0.9))
        if inner.h < 2:
            # One-row tile (many metrics, little height): the value must survive, so it leads, the delta
            # keeps the right edge and the label takes whatever is left between them.
            room = inner.w - (dw + 1 if dtxt and dw + 4 <= inner.w else 0)
            vs = clip(val, max(1, room), v.u)
            vx = inner.gtext(0, 0, vs, SPECTRUM, True, offset=hue * 0.6) if vs else 0
            if dtxt and dw + 4 <= inner.w:
                inner.text(inner.w - dw, 0, dtxt, dcol, True)
            lw_room = room - vx - (2 if vs else 0) - 1
            if label and lw_room >= 3:
                inner.text(vx + (2 if vs else 0), 0, clip(label, lw_room, v.u), SOFT)
            continue
        inner.text(0, 0, clip(label, max(1, inner.w - (dw + 2 if dtxt else 0)), v.u), SOFT)
        if dtxt and dw < inner.w:
            inner.text(inner.w - dw, 0, dtxt, dcol, True)
        if big:
            draw_big_value(inner, 0, 1, val, "compact", offset=hue * 0.6)
            vy = 4
        else:
            inner.gtext(0, 1, clip(val, inner.w, v.u), SPECTRUM, True, offset=hue * 0.6)
            vy = 2
        trend = m.get("trend") or []
        if trend and inner.h - vy - 1 >= 1:
            _trend_area(inner, 0, vy + 1, inner.w, inner.h - vy - 1, trend, t - appear)


STATUS_STYLE = {"done": ("✓", "v", OK), "active": ("◉", "*", BEAM), "failed": ("✗", "x", BAD),
                "pending": ("○", "o", DUST), "": ("●", "o", PLASMA)}


def _status_glyph(st: str, u: bool, t: float, motion: bool, i: int = 0) -> Tuple[str, Color]:
    g_u, g_a, col = STATUS_STYLE.get(st, STATUS_STYLE[""])
    if st == "active":
        if motion:
            ch = fx.SPINNER[int(t * 12 + i) % len(fx.SPINNER)] if u else "-\\|/"[int(t * 8) % 4]
            return ch, fx.grad((BEAM, PLASMA, FLARE, PLASMA, BEAM), (t * 0.6) % 1.0)
        return (g_u if u else g_a), BEAM
    return (g_u if u else g_a), col


def draw_timeline(cv: Any, x: int, y: int, w: int, h: int, events: List[Dict[str, Any]], u: Optional[bool] = None,
                  t: float = SETTLED, motion: bool = False, scroll: int = 0) -> int:
    v = View(cv, x, y, w, h, u)
    evs = list(events or [])
    if not evs or v.w < 10 or v.h < 1:
        return 0
    when_w = min(max([swidth(prep(e.get("when") or "", v.u)) for e in evs] + [0]), max(0, v.w // 4))
    sx = when_w + 2 if when_w else 1
    tx = sx + 3
    tw = max(4, v.w - tx)
    blocks = []
    for e in evs:
        det = wrap(e.get("detail") or "", tw, v.u) if e.get("detail") else []
        blocks.append((e, det[:3]))
    total = sum(1 + len(d) for _, d in blocks) + max(0, len(blocks) - 1)
    compact = total > v.h
    if compact:
        blocks = [(e, d[:1]) for e, d in blocks]
        total = sum(1 + len(d) for _, d in blocks)
        if total > v.h:
            blocks = [(e, []) for e, _ in blocks]
            total = len(blocks)
    active = next((i for i, (e, _) in enumerate(blocks) if e.get("status") == "active"), None)
    start = 0
    if total > v.h:
        start = int(clamp(scroll if scroll else (active - v.h // 2 if active is not None else 0), 0, len(blocks) - v.h))
    yy = 0
    n = len(blocks)
    for i, (e, det) in enumerate(blocks[start:], start):
        if yy >= v.h:
            break
        appear = 0.07 * (i - start)
        if t < appear:
            break
        k = ease((t - appear) / 0.4)
        st = e.get("status") or ""
        g, col = _status_glyph(st, v.u, t, motion, i)
        if st == "":
            col = fx.grad(SPECTRUM, i / max(1, n - 1) * 0.9)
        when = clip(e.get("when") or "", when_w, v.u)
        if when:
            v.text(when_w - swidth(when), yy, when, fx.scale(ASH, 0.6 + 0.4 * k))
        v.put(sx, yy, g, fx.lerp(NIGHT, col, k), True)
        v.text(tx, yy, clip(e.get("title") or "", tw, v.u), fx.lerp(NIGHT, ICE if st != "pending" else SOFT, k),
               st != "pending")
        spine_col = fx.scale(OK, 0.55) if st == "done" else fx.scale(fx.grad(SPECTRUM, i / max(1, n - 1)), 0.45)
        seg_h = len(det) + (0 if compact else 1)
        for j, d in enumerate(det):
            v.text(tx, yy + 1 + j, d, fx.scale(SOFT, 0.85 * k + 0.15))
        if i < n - 1:
            for j in range(seg_h):
                v.put(sx, yy + 1 + j, "│" if v.u else "|", spine_col)
        yy += 1 + seg_h
    return max(0, total - v.h)


def draw_progress(cv: Any, x: int, y: int, w: int, h: int, steps: List[Dict[str, Any]], u: Optional[bool] = None,
                  t: float = SETTLED, motion: bool = False) -> None:
    v = View(cv, x, y, w, h, u)
    sts = list(steps or [])
    if not sts or v.w < 10 or v.h < 1:
        return
    n = len(sts)
    done = sum(1 for s in sts if s.get("status") == "done")
    failed = sum(1 for s in sts if s.get("status") == "failed")
    active = sum(1 for s in sts if s.get("status") == "active")
    yy = 0
    if v.h >= 4:
        label = "%d/%d" % (done, n)
        pct = "%d%%" % int(round(100 * done / n))
        right = label + "  " + pct + (("  %d failed" % failed) if failed else "")
        bw = max(4, v.w - swidth(right) - 2)
        frac = (done + 0.5 * active) / n * ease(t / 0.7)
        fill = frac * bw
        for i in range(bw):
            if i < int(fill):
                col = fx.grad(SPECTRUM, i / max(1, bw - 1))
                if motion and active:
                    d = abs(i - ((t * 30) % (bw + 20) - 10))
                    if d < 4:
                        col = fx.lerp(col, ICE, (1 - d / 4) * 0.8)
                v.put(i, yy, "━" if v.u else "=", col)
            elif i == int(fill) and fill - int(fill) > 0.3:
                v.put(i, yy, "╸" if v.u else "-", fx.grad(SPECTRUM, i / max(1, bw - 1)))
            else:
                v.put(i, yy, "─" if v.u else "-", FAINT)
        v.text(bw + 2, yy, label, ICE, True)
        v.text(bw + 2 + len(label) + 2, yy, pct, fx.grad(SPECTRUM, frac), True)
        if failed:
            v.text(bw + 2 + len(label) + 2 + len(pct) + 2, yy, "%d failed" % failed, BAD, True)
        yy = 2
    rows_left = v.h - yy
    start = 0
    if n > rows_left:
        cur = next((i for i, s in enumerate(sts) if s.get("status") in ("active", "failed")), done)
        start = int(clamp(cur - rows_left // 2, 0, n - rows_left))
    title_w = max(4, v.w - 4)
    for i in range(start, n):
        if yy >= v.h:
            break
        s = sts[i]
        st = s.get("status") or "pending"
        appear = 0.05 * (i - start)
        if t < appear:
            break
        k = ease((t - appear) / 0.35)
        g, col = _status_glyph(st, v.u, t, motion, i)
        v.put(1, yy, g, fx.lerp(NIGHT, col, k), True)
        title = s.get("title") or ""
        detail = s.get("detail") or ""
        tcol = {"done": SOFT, "active": ICE, "failed": BAD, "pending": fx.scale(SOFT, 0.7)}.get(st, SOFT)
        if st == "active" and motion:
            pulse = 0.5 + 0.5 * math.sin(t * 4)
            tcol = fx.lerp(ICE, BEAM, pulse * 0.35)
        tt = clip(title, title_w, v.u)
        end = v.text(4, yy, tt, fx.lerp(NIGHT, tcol, k), st in ("active", "failed"))
        if detail:
            room = v.w - end - 3
            dcol = DUST if st != "failed" else fx.scale(BAD, 0.8)
            if room >= 8:
                d = clip(detail, room, v.u)
                v.text(v.w - swidth(d), yy, d, dcol)
            elif yy + 1 < v.h and (i == n - 1 or rows_left > n * 2):
                yy += 1
                v.text(4, yy, clip(detail, title_w, v.u), dcol)
        yy += 1


def draw_card(cv: Any, x: int, y: int, w: int, h: int, body: str, tone: str = "info", title: str = "",
              u: Optional[bool] = None, t: float = SETTLED, motion: bool = False) -> None:
    v = View(cv, x, y, w, h, u)
    if v.w < 8 or v.h < 3:
        return
    stops = TONES.get(tone, TONES["info"])
    cw = min(v.w, 100)
    lay = md_layout(body or "", cw - 6, v.u)
    content_h = len(lay) + (2 if title else 0)
    ch = int(clamp(content_h + 4, 4, v.h))
    bx = (v.w - cw) // 2
    by = max(0, (v.h - ch) // 3)
    k = ease(t / 0.5)
    v.box(bx, by, cw, ch, stops=stops, phase=t * 0.18 if motion else 0.0, bright=0.4 + 0.6 * k, fill=True)
    g_u, g_a, label = TONE_TAG.get(tone, TONE_TAG["info"])
    tag = " %s %s " % (g_u if v.u else g_a, label)
    v.text(bx + 3, by, tag, stops[0], True)
    inner = View(v, bx + 3, by + 2, cw - 6, ch - 3)
    yy = 0
    if title:
        draw_decrypt(inner, 0, 0, clip(title, inner.w, v.u), t, stops=(ICE, stops[0]))
        yy = 2
    draw_md(inner, 0, yy, inner.w, inner.h - yy, body or "", t=t - 0.1)


# ---------------------------------------------------------------------------
# Scenes and slides
# ---------------------------------------------------------------------------

def draw_kind(cv: Any, x: int, y: int, w: int, h: int, scene: Dict[str, Any], u: Optional[bool] = None,
              t: float = SETTLED, motion: bool = False, scroll: int = 0) -> int:
    """Draw a non-deck scene's payload (no title). Returns hidden rows (scrollable overflow)."""
    kind = scene.get("kind")
    if kind == "card":
        draw_card(cv, x, y, w, h, scene.get("body") or "", scene.get("tone") or "info", "", u, t, motion)
    elif kind == "markdown":
        return draw_md(cv, x, y, w, h, scene.get("body") or "", u, t, scroll=scroll)
    elif kind == "code":
        return draw_code(cv, x, y, w, h, scene.get("code") or {}, u, t, scroll if scroll else None)
    elif kind == "table":
        return draw_table(cv, x, y, w, h, scene.get("columns") or [], scene.get("rows") or [], u, t, scroll)
    elif kind == "chart":
        draw_chart(cv, x, y, w, h, scene, u, t, motion)
    elif kind == "diagram":
        draw_diagram(cv, x, y, w, h, scene, u, t, motion)
    elif kind == "metrics":
        draw_metrics(cv, x, y, w, h, scene.get("metrics") or [], u, t, motion)
    elif kind == "timeline":
        return draw_timeline(cv, x, y, w, h, scene.get("events") or [], u, t, motion, scroll)
    elif kind == "progress":
        draw_progress(cv, x, y, w, h, scene.get("steps") or [], u, t, motion)
    return 0


def _header(v: View, title: str, subtitle: str, t: float, big_ok: bool, seed: int = 0) -> int:
    """Title block for a slide or scene. Returns rows used (including the rule and a spacer)."""
    y = 0
    if title:
        modes: Tuple[str, ...] = (("tall", "compact") if v.h >= 34 else ("compact",)) if big_ok else ()
        used = draw_title(v, 0, 0, v.w, 5 if v.h >= 34 else 3, title, t, modes, max_lines=1, seed=seed)
        y += used
    if subtitle:
        if y:
            y += 1 if big_ok else 0
        draw_decrypt(v, 0, y, clip(subtitle, v.w, v.u), t - 0.15, stops=(SOFT, ASH), bold=False, seed=seed + 7)
        y += 1
    if title or subtitle:
        rule_w = min(v.w, max(12, int(v.w * 0.35)))
        k = ease((t - 0.1) / 0.5)
        v.ghline(0, y, int(rule_w * k), SPECTRUM, ch="━" if v.u else "=", bright=0.8)
        y += 2
    return y


def _halo(v: View, cx: int, cy: int, t: float) -> None:
    """Dim beacon rings behind a title slide."""
    for i in range(3):
        p = ((t / 2.4) + i / 3) % 1.0
        r = p * max(v.w, v.h * 2) * 0.55
        if r < 2:
            continue
        fade = (1 - p) ** 1.6 * 0.5
        steps = int(r * 6) + 12
        for s in range(steps):
            a = 2 * math.pi * s / steps
            xx = int(round(cx + math.cos(a) * r))
            yy = int(round(cy + math.sin(a) * r * 0.5))
            if v.blank(xx, yy):
                v.put(xx, yy, "·" if v.u else ".", fx.scale(fx.grad((BEAM, PLASMA, FLARE), p), fade))


def draw_slide(cv: Any, x: int, y: int, w: int, h: int, slide: Dict[str, Any], u: Optional[bool] = None,
               t: float = SETTLED, motion: bool = False, index: int = 0, total: int = 1, scroll: int = 0) -> int:
    """Draw one deck slide by its layout. Returns hidden rows."""
    v = View(cv, x, y, w, h, u)
    if v.w < 8 or v.h < 3:
        return 0
    layout = slide.get("layout") or "auto"
    title = slide.get("title") or ""
    sub = slide.get("subtitle") or ""
    body = slide.get("body") or ""
    code = slide.get("code")
    stat = slide.get("stat")
    embed = slide.get("embed")
    padx = 1 if v.w < 50 else 3 if v.w < 110 else 6
    pady = 0 if v.h < 16 else 1
    inner = View(v, padx, pady, v.w - 2 * padx, v.h - 2 * pady)
    seed = index * 31
    big_ok = inner.h >= 16 and inner.w >= 30
    if layout == "title":
        return _slide_title(v, inner, title, sub, body, t, motion, seed)
    if layout == "stat" and stat:
        return _slide_stat(inner, title, sub, body, stat, t, seed)
    if layout == "quote":
        return _slide_quote(inner, title, sub, body, t, seed)
    y0 = _header(inner, title, sub, t, big_ok, seed)
    rest = View(inner, 0, y0, inner.w, inner.h - y0)
    side = code or embed or stat
    if layout == "split" and rest.w >= 50:
        lw = int(rest.w * 0.46)
        left = View(rest, 0, 0, lw, rest.h)
        right = View(rest, lw + 3, 0, rest.w - lw - 3, rest.h)
        hidden = 0
        if side:
            hidden = draw_md(left, 0, 0, left.w, left.h, body, t=t - 0.2, scroll=scroll)
            _slide_side(right, code, embed, stat, t - 0.3, motion)
        else:
            lines = md_layout(body, lw, rest.u)
            half = (len(lines) + 1) // 2
            first = "\n".join(_plain_line(ln) for _, ln in lines[:half])
            second = "\n".join(_plain_line(ln) for _, ln in lines[half:])
            draw_md(left, 0, 0, left.w, left.h, first, t=t - 0.2)
            draw_md(right, 0, 0, right.w, right.h, second, t=t - 0.3)
        return hidden
    if side:
        blines = md_layout(body, rest.w, rest.u) if body else []
        take = min(len(blines), 3 if (code or embed) else len(blines), max(0, rest.h - 6))
        if take:
            draw_md(rest, 0, 0, rest.w, take, body, t=t - 0.2)
        panel = View(rest, 0, take + (1 if take else 0), rest.w, rest.h - take - (1 if take else 0))
        _slide_side(panel, code, embed, stat, t - 0.3, motion)
        return 0
    # bullets / auto
    lay = md_layout(body, rest.w, rest.u)
    if len(lay) > rest.h and rest.w >= 90:
        colw = (rest.w - 4) // 2
        lay2 = md_layout(body, colw, rest.u)
        if len(lay2) <= rest.h * 2:
            cut = _split_point(lay2, rest.h)
            for col, part in ((0, lay2[:cut]), (colw + 4, lay2[cut:])):
                for row, (item, ln) in enumerate(part[:rest.h]):
                    appear = 0.2 + item * 0.07
                    if t < appear:
                        continue
                    k = ease((t - appear) / 0.35)
                    draw_line(rest, col, row, ln, maxw=colw, bright=0.35 + 0.65 * k, shift=int(round((1 - k) * 6)))
            return 0
    return draw_md(rest, 0, 0, rest.w, rest.h, body, t=t - 0.2, stagger=0.07, scroll=scroll, spacious=True)


def _plain_line(ln: Line) -> str:
    return "".join(s for s, _, _ in ln)


def _split_point(lay: List[Tuple[int, Line]], h: int) -> int:
    cut = min(len(lay), h)
    # move the cut back to an item boundary so a bullet never splits across columns
    while 0 < cut < len(lay) and lay[cut][0] == lay[cut - 1][0]:
        cut -= 1
    return cut if cut > 0 else min(len(lay), h)


def _slide_side(v: View, code: Optional[Dict[str, Any]], embed: Optional[Dict[str, Any]],
                stat: Optional[Dict[str, Any]], t: float, motion: bool) -> None:
    if code:
        draw_code(v, 0, 0, v.w, v.h, code, t=t)
    elif embed:
        hdr = 0
        if embed.get("title"):
            v.text(0, 0, clip(embed["title"], v.w, v.u), SOFT, True)
            hdr = 2
        draw_kind(v, 0, hdr, v.w, v.h - hdr, embed, t=t, motion=motion)
    elif stat:
        _slide_stat(v, "", "", "", stat, t, 0)


def _slide_title(outer: View, v: View, title: str, sub: str, body: str, t: float, motion: bool, seed: int) -> int:
    fit = big_fit(title, v.w, max(3, v.h - 6), BIG_MODES if v.u else ("wide", "tall"), 2) if title else None
    if fit:
        mode, lines = fit
        th = len(lines) * (big_height(mode) + 1) - 1
    else:
        lines = wrap(title, v.w, v.u)[:3]
        th = len(lines)
    blines = wrap(body, min(v.w, 80), v.u) if body else []
    block = th + (2 if sub else 0) + 2 + (len(blines[:4]) + 1 if blines else 0)
    y0 = max(0, (v.h - block) // 2)
    if motion:
        _halo(outer, outer.w // 2, v.oy - outer.oy + y0 + th // 2, t)
    if fit:
        mode, lines = fit
        rows = big_height(mode)
        span = max(big_width(ln, mode) for ln in lines)
        shimmer = ((t - 1.0) * 60) % (span + 60) - 20 if motion and t > 1.0 else None
        for i, ln in enumerate(lines):
            bw = big_width(ln, mode)
            draw_big(v, (v.w - bw) // 2, y0 + i * (rows + 1), ln, mode, t=t - 0.1 * i, span=span, seed=seed + i,
                     shimmer=shimmer)
    else:
        for i, ln in enumerate(lines):
            draw_decrypt(v, (v.w - swidth(ln)) // 2, y0 + i, ln, t - 0.08 * i, seed=seed + i)
    y = y0 + th + 1
    if sub:
        s = clip(sub, v.w, v.u)
        draw_decrypt(v, (v.w - swidth(s)) // 2, y, s, t - 0.35, stops=(TEXT, SOFT), bold=False, seed=seed + 9)
        y += 2
    k = ease((t - 0.45) / 0.6)
    rw = int(min(v.w, max(16, v.w * 0.4)) * k)
    v.ghline((v.w - rw) // 2, y, rw, SPECTRUM, bright=0.9, offset=(t * 0.1) if motion else 0.0)
    y += 2
    for i, ln in enumerate(blines[:max(0, min(4, v.h - y))]):
        if t < 0.6 + i * 0.08:
            break
        kk = ease((t - 0.6 - i * 0.08) / 0.4)
        v.text((v.w - swidth(ln)) // 2, y + i, ln, fx.lerp(NIGHT, SOFT, kk))
    return 0


def _slide_stat(v: View, title: str, sub: str, body: str, stat: Dict[str, Any], t: float, seed: int) -> int:
    value = count_up(stat.get("value") or "", ease(t / 1.0))
    label = stat.get("label") or ""
    y = 0
    if title:
        tt = clip(prep(title, v.u).upper(), v.w, v.u)
        draw_decrypt(v, (v.w - swidth(tt)) // 2, 0, tt, t, seed=seed)
        y = 2
    head, unit = split_value(stat.get("value") or "")
    unit_w = (1 + swidth(unit)) if unit else 0
    fit = big_fit(head, v.w - unit_w, max(3, v.h - y - 4), BIG_MODES if v.u else ("wide", "tall"), 1)
    blines = wrap(body, min(v.w, 80), v.u) if body else []
    if fit:
        mode, _ = fit
        rows = big_height(mode)
        block = rows + 2 + (2 if sub else 0) + (len(blines[:3]) + 1 if blines else 0)
        yy = y + max(0, (v.h - y - block) // 2)
        final = big_value_width(stat.get("value") or "", mode)
        bw = big_value_width(value, mode)
        draw_big_value(v, (v.w - final) // 2 + (final - bw), yy, value, mode, span=final, seed=seed)
        yy += rows + 1
    else:
        block = 1 + 2 + (len(blines[:3]) + 1 if blines else 0)
        yy = y + max(0, (v.h - y - block) // 2)
        s = clip(value, v.w, v.u)
        v.gtext((v.w - swidth(s)) // 2, yy, s, SPECTRUM, True)
        yy += 2
    if label:
        lab = clip(prep(label, v.u).upper(), v.w, v.u)
        draw_decrypt(v, (v.w - swidth(lab)) // 2, yy, lab, t - 0.3, stops=(ICE, SOFT), seed=seed + 3)
        yy += 2
    if sub:
        s = clip(sub, v.w, v.u)
        v.text((v.w - swidth(s)) // 2, yy, s, SOFT)
        yy += 2
    for i, ln in enumerate(blines[:3]):
        v.text((v.w - swidth(ln)) // 2, yy + i, ln, fx.lerp(NIGHT, SOFT, ease((t - 0.5 - 0.08 * i) / 0.4)))
    return 0


def _slide_quote(v: View, title: str, sub: str, body: str, t: float, seed: int) -> int:
    qw = min(v.w - 4, 72)
    text = body or title
    lines = wrap(text, qw, v.u)[: max(1, v.h - 7)]
    who = sub or (title if body else "")
    block = (6 if v.h >= 16 else 4) + len(lines) + (2 if who else 0)
    y = max(0, (v.h - block) // 2)
    mark = "\u201c"
    mode = "wide" if v.h >= 16 else ("compact" if v.u else "tall")
    mw = big_width(mark, mode)
    draw_big(v, (v.w - mw) // 2, y, mark, mode, t=t, seed=seed)
    yy = y + big_height(mode) + 1
    for i, ln in enumerate(lines):
        if t < 0.2 + i * 0.06:
            break
        k = ease((t - 0.2 - i * 0.06) / 0.4)
        v.text((v.w - swidth(ln)) // 2, yy + i, ln, fx.lerp(NIGHT, ICE, k), True)
    yy += len(lines) + 1
    if who:
        k = ease((t - 0.5) / 0.5)
        w2 = clip(who, qw - 6, v.u)
        rw = 4
        x0 = (v.w - (rw + 2 + swidth(w2))) // 2
        v.ghline(x0, yy, int(rw * k) or 1, SPECTRUM)
        v.text(x0 + rw + 2, yy, w2, fx.lerp(NIGHT, SOFT, k))
    return 0


def draw_scene(cv: Any, x: int, y: int, w: int, h: int, scene: Dict[str, Any], slide: int = 0,
               u: Optional[bool] = None, t: float = SETTLED, motion: bool = False, scroll: int = 0) -> int:
    """Draw any normalized scene into a region. Returns hidden rows (scrollable overflow)."""
    v = View(cv, x, y, w, h, u)
    if v.w < 4 or v.h < 2:
        return 0
    kind = scene.get("kind")
    if kind == "deck":
        slides = scene.get("slides") or []
        if not slides:
            return 0
        i = int(clamp(slide, 0, len(slides) - 1))
        return draw_slide(v, 0, 0, v.w, v.h, slides[i], t=t, motion=motion, index=i, total=len(slides),
                          scroll=scroll)
    padx = 1 if v.w < 50 else 3 if v.w < 110 else 6
    pady = 0 if v.h < 16 else 1
    inner = View(v, padx, pady, v.w - 2 * padx, v.h - 2 * pady)
    title = scene.get("title") or ""
    sub = scene.get("subtitle") or ""
    y0 = 0
    if title or sub:
        y0 = _header(inner, title, sub, t, inner.h >= 22 and kind in ("chart", "metrics", "progress", "diagram"),
                     seed=len(title))
        if inner.h - y0 < 4:
            y0 = 0
            inner.text(0, 0, clip(title or sub, inner.w, inner.u), ICE, True)
            y0 = 1
    return draw_kind(inner, 0, y0, inner.w, inner.h - y0, scene, t=t - 0.15, motion=motion, scroll=scroll)


def natural_height(scene: Dict[str, Any], w: int, slide: Optional[Dict[str, Any]] = None, u: bool = True) -> int:
    """Rows a scene (or one slide) wants at width w; used by the plain renderer."""
    def kind_h(s: Dict[str, Any]) -> int:
        k = s.get("kind")
        if k in ("markdown",):
            return len(md_layout(s.get("body") or "", w, u)) + 1
        if k == "card":
            return len(md_layout(s.get("body") or "", min(w, 100) - 6, u)) + 6
        if k == "code":
            return min(80, len(((s.get("code") or {}).get("text") or "").split("\n")) + 2)
        if k == "table":
            return min(60, len(s.get("rows") or []) + 4)
        if k == "chart":
            if s.get("chart") == "spark":
                return 6 * len(s.get("series") or [1])
            if s.get("chart") == "bar":
                n = max(len(x.get("values") or []) for x in s.get("series") or [{}])
                return int(clamp(n * len(s.get("series") or [1]) + 3, 10, 40))
            return 16
        if k == "diagram":
            L = layout_diagram(s.get("nodes") or [], s.get("edges") or [], s.get("direction") or "LR", w, 200, u)
            return (L.h + 1) if L else len(s.get("edges") or []) + 2
        if k == "metrics":
            n = len(s.get("metrics") or [])
            cols = max(1, min(n, w // 22))
            return math.ceil(n / cols) * 8
        if k == "timeline":
            return sum(1 + min(3, len(wrap(e.get("detail") or "", max(4, w - 20), u)) if e.get("detail") else 1)
                       for e in s.get("events") or []) + 1
        if k == "progress":
            return len(s.get("steps") or []) + 3
        return 10
    pad = 2
    if slide is not None:
        hdr = (6 if slide.get("title") else 0) + (2 if slide.get("subtitle") else 0)
        body = len(md_layout(slide.get("body") or "", w - 6, u)) if slide.get("body") else 0
        extra = 0
        if slide.get("code"):
            extra = kind_h({"kind": "code", "code": slide["code"]})
        elif slide.get("embed"):
            extra = kind_h(slide["embed"])
        elif slide.get("stat"):
            extra = 9
        if slide.get("layout") == "title":
            return int(clamp(hdr + body + 10, 12, 30))
        return int(clamp(hdr + body + extra + pad + 2, 8, 90))
    hdr = 4 if scene.get("title") or scene.get("subtitle") else 0
    return int(clamp(hdr + kind_h(scene) + pad, 6, 120))
