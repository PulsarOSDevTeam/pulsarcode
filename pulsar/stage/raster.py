# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Pulsar Stage as a cell raster, for the Stage pane inside Claude Code.

Claude Code's function-hooks `Raster` element draws a fixed box of terminal
cells, each packed as three little-endian u32 words `[codePoint, fg, bg]`:
the code point one printable, width-1 character of the Basic Multilingual
Plane, a colour `0x00RRGGBB` or `0x01000000` for the terminal's default.
This module draws a normalized scene exactly as the terminal viewer draws
its content area (`pulsar.stage.render.draw_scene`, no header, rail or
footer: the pane has its own title row) and packs the canvas that way.

  render(scene, cols, rows, slide, t, ascii) -> bytes   the raw cells
  encode(cells) -> str                                  base64, as Raster takes it
  frame(scene, cols, rows, slide, t, ascii) -> dict     what the pane fetches

`t` is seconds since the scene (or slide) appeared, so a client asks for
increasing `t` to play the intro; a large `t` is the settled frame. Frames
are drawn with ambient motion off, so a frame is a pure function of `t`
and `frame()` can say exactly when the intro is over (`animating`).

Every cell is made safe for the Raster contract here, whatever the canvas
holds. A cell only ever holds printable ASCII or a code point of
`CELL_RANGES`: a fixed table of scripts and symbol blocks (Latin, Greek,
Cyrillic, punctuation, arrows, math, box drawing, blocks, geometric shapes,
braille, ...) that Claude Code itself accepted as Raster cells. It is not
derived from Python's Unicode tables, which lag the engine's (Unicode 16
made the trigram and hexagram symbols wide, and one refused cell blanks the
whole pane), so the output is the same on every Python. Anything else is a
blank (controls, marks, format and bidi characters, spaces) or the
placeholder (wide CJK and emoji, then a blank so the grid stays aligned;
other scripts, astral, private-use and unassigned code points).
`pulsar/fnplug/tests/cells.test.tsx` mounts every code point of the table
through the real plugin under `claude plugin test`, so the engine is the
oracle; tests/test_stage_pane.py keeps the two tables equal. Pure and
fast: no I/O, no globals beyond small caches.
"""

from __future__ import annotations

import base64
import bisect
import hashlib
import json
import math
import sys
import unicodedata
from array import array
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

from pulsar import fx
from pulsar.stage import render as R

MAX_COLS = 512
MAX_ROWS = 256

DEFAULT = 0x01000000          # the terminal's own colour (Raster contract: bit 24 alone)
SPACE = 0x20
PLACEHOLDER = 0x25AF          # ▯ stands in for a glyph that cannot sit in one cell (CJK, emoji, astral)
ASCII_PLACEHOLDER = 0x3F      # ?

# The pane paints its own night sky (the web stage's #07060c), so slides read the same on light and
# dark terminal themes; Claude Code's Raster palette rounds it to #000011, which sits flush with a
# dark terminal. Text the canvas leaves uncoloured is drawn in the stage's body colour.
BACKGROUND: fx.Color = (7, 6, 12)
FOREGROUND: fx.Color = R.TEXT

SETTLE_CAP = 15.0             # seconds: past this no frame reports `animating` (bounds a client's fast polling)
_SETTLED_KEEP = 48


def _rgb(c: Optional[Tuple[int, int, int]], default: int) -> int:
    if c is None:
        return default
    try:
        r, g, b = c
        return (max(0, min(255, int(r))) << 16) | (max(0, min(255, int(g))) << 8) | max(0, min(255, int(b)))
    except (TypeError, ValueError):
        return default


BG = _rgb(BACKGROUND, DEFAULT)
FG = _rgb(FOREGROUND, DEFAULT)

# Every non-ASCII code point a raster cell may hold, as inclusive ranges. Built from these blocks:
# Latin-1, Latin Extended-A/B and Additional, IPA, spacing modifiers, Greek (and Extended), Cyrillic,
# Armenian, Hebrew and Arabic letters, Georgian, general punctuation, super/subscripts, currency,
# letterlike, number forms, arrows, math operators, technical, control pictures, OCR, enclosed
# alphanumerics, box drawing, block elements, geometric shapes, miscellaneous symbols, dingbats,
# supplemental arrows and math, braille, miscellaneous symbols and arrows. Kept: what Python 3.9 to 3.13
# all call a printable, narrow, non-combining character (plus U+20C0, Unicode 14), minus the trigrams
# (U+2630-2637) and digrams (U+268A-268F) that Unicode 16 made wide; then every one of them mounted as a
# Raster cell in Claude Code 2.1.287 (pulsar/fnplug/tests/cells.test.tsx). Change both together.
CELL_RANGES: Tuple[Tuple[int, int], ...] = (
    (0x00A1, 0x00AC), (0x00AE, 0x02FF), (0x0370, 0x0377), (0x037A, 0x037F), (0x0384, 0x038A),
    (0x038C, 0x038C), (0x038E, 0x03A1), (0x03A3, 0x0482), (0x048A, 0x052F), (0x0531, 0x0556),
    (0x0559, 0x058A), (0x058D, 0x058F), (0x05D0, 0x05EA), (0x05EF, 0x05F4), (0x0620, 0x064A),
    (0x0660, 0x066D), (0x10A0, 0x10C5), (0x10C7, 0x10C7), (0x10CD, 0x10CD), (0x10D0, 0x10FF),
    (0x1E00, 0x1F15), (0x1F18, 0x1F1D), (0x1F20, 0x1F45), (0x1F48, 0x1F4D), (0x1F50, 0x1F57),
    (0x1F59, 0x1F59), (0x1F5B, 0x1F5B), (0x1F5D, 0x1F5D), (0x1F5F, 0x1F7D), (0x1F80, 0x1FB4),
    (0x1FB6, 0x1FC4), (0x1FC6, 0x1FD3), (0x1FD6, 0x1FDB), (0x1FDD, 0x1FEF), (0x1FF2, 0x1FF4),
    (0x1FF6, 0x1FFE), (0x2010, 0x2027), (0x2030, 0x205E), (0x2070, 0x2071), (0x2074, 0x208E),
    (0x2090, 0x209C), (0x20A0, 0x20C0), (0x2100, 0x218B), (0x2190, 0x2319), (0x231C, 0x2328),
    (0x232B, 0x23E8), (0x23ED, 0x23EF), (0x23F1, 0x23F2), (0x23F4, 0x2426), (0x2440, 0x244A),
    (0x2460, 0x25FC), (0x25FF, 0x2613), (0x2616, 0x262F), (0x2638, 0x2647), (0x2654, 0x267E),
    (0x2680, 0x2689), (0x2690, 0x2692), (0x2694, 0x26A0), (0x26A2, 0x26A9), (0x26AC, 0x26BC),
    (0x26BF, 0x26C3), (0x26C6, 0x26CD), (0x26CF, 0x26D3), (0x26D5, 0x26E9), (0x26EB, 0x26F1),
    (0x26F4, 0x26F4), (0x26F6, 0x26F9), (0x26FB, 0x26FC), (0x26FE, 0x2704), (0x2706, 0x2709),
    (0x270C, 0x2727), (0x2729, 0x274B), (0x274D, 0x274D), (0x274F, 0x2752), (0x2756, 0x2756),
    (0x2758, 0x2794), (0x2798, 0x27AF), (0x27B1, 0x27BE), (0x27C0, 0x2B1A), (0x2B1D, 0x2B4F),
    (0x2B51, 0x2B54), (0x2B56, 0x2B73), (0x2B76, 0x2B95), (0x2B97, 0x2BFF),
)
_CELL_STARTS = tuple(lo for lo, _hi in CELL_RANGES)

_ZERO_CATS = frozenset(("Mn", "Me", "Cf", "Zl", "Zp", "Zs"))   # invisible marks, format, separators: a blank


def in_cell_table(o: int) -> bool:
    """True when code point `o` may sit in a raster cell as itself (printable ASCII or CELL_RANGES)."""
    if 0x20 <= o <= 0x7E:
        return True
    i = bisect.bisect_right(_CELL_STARTS, o) - 1
    return i >= 0 and o <= CELL_RANGES[i][1]


def safe_codepoint(ch: str, ascii: bool = False) -> int:
    """The code point a canvas cell becomes in the raster: always printable, width 1, BMP."""
    return _safe_u(ch) if not ascii else _safe_a(ch)


_CACHE_U: Dict[str, int] = {}
_CACHE_A: Dict[str, int] = {}


def _safe_u(ch: str) -> int:
    hit = _CACHE_U.get(ch)
    if hit is not None:
        return hit
    cp = _classify(ch)
    if len(_CACHE_U) < 65536:
        _CACHE_U[ch] = cp
    return cp


def _safe_a(ch: str) -> int:
    hit = _CACHE_A.get(ch)
    if hit is not None:
        return hit
    cp = _classify(ch)
    if cp > 0x7E:
        cp = ASCII_PLACEHOLDER
    if len(_CACHE_A) < 65536:
        _CACHE_A[ch] = cp
    return cp


def _classify(ch: str) -> int:
    if not isinstance(ch, str) or ch == "":
        return SPACE                       # the second cell of a wide glyph, or nothing
    if len(ch) != 1:
        return PLACEHOLDER                 # a cluster in one cell: never forward it whole
    o = ord(ch)
    if 0x20 <= o <= 0x7E:
        return o
    if o < 0x20 or 0x7F <= o <= 0x9F:
        return SPACE                       # C0/C1 controls (ESC included) draw as nothing
    if in_cell_table(o):
        return o
    if o > 0xFFFF or 0xD800 <= o <= 0xDFFF:
        return PLACEHOLDER
    if 0xFE00 <= o <= 0xFE0F or 0x200B <= o <= 0x200F or 0x2028 <= o <= 0x202E or 0x2060 <= o <= 0x206F:
        return SPACE                       # variation selectors, zero-width and bidi controls (RTL marks)
    if unicodedata.category(ch) in _ZERO_CATS:
        return SPACE                       # combining marks, format characters, NBSP and other spaces
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        folded = unicodedata.normalize("NFKC", ch)     # fullwidth Latin and digits fold to one plain cell
        if len(folded) == 1 and 0x21 <= ord(folded) <= 0x7E:
            return ord(folded)
    return PLACEHOLDER                     # wide (CJK, emoji), other scripts, private use, unassigned


def _clamp_int(v: Any, lo: int, hi: int, default: int) -> int:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return max(lo, min(hi, int(f)))


def _clamp_t(t: Any) -> float:
    try:
        f = float(t)
    except (TypeError, ValueError):
        return R.SETTLED
    if f != f:
        return R.SETTLED
    return max(0.0, min(1e6, f))


def slide_count(scene: Dict[str, Any]) -> int:
    if isinstance(scene, dict) and scene.get("kind") == "deck":
        return max(1, len(scene.get("slides") or []))
    return 1


def _draw(cv: fx.Canvas, scene: Dict[str, Any], slide: int, t: float, ascii: bool) -> None:
    try:
        R.draw_scene(cv, 0, 0, cv.w, cv.h, scene, slide=slide, u=not ascii, t=t, motion=False)
    except Exception as exc:  # a bad scene must never take the pane down
        for row in cv.cells:
            row[:] = [fx.BLANK] * cv.w
        try:
            R.draw_card(cv, 0, 0, cv.w, cv.h, "This scene could not be drawn in the pane (%s). "
                        "Try `pulsarcode stage --web`." % type(exc).__name__, "error", "", u=not ascii)
        except Exception:
            pass


def pack(cv: fx.Canvas, ascii: bool = False) -> bytes:
    """A canvas as raw Raster cells: cols*rows little-endian u32 [codePoint, fg, bg] triplets."""
    words = array("I")
    safe = _safe_a if ascii else _safe_u
    colours: Dict[Any, int] = {None: FG}
    bg = BG
    for row in cv.cells:
        for cell in row:
            ch, fg, _bold = cell
            cp = safe(ch)
            c = colours.get(fg)
            if c is None:
                c = colours[fg] = _rgb(fg, FG)
            words.append(cp)
            words.append(c)
            words.append(bg)
    if sys.byteorder != "little":
        words.byteswap()
    return words.tobytes()


def render(scene: Dict[str, Any], cols: int, rows: int, slide: int = 0, t: float = 9.0,
           ascii: bool = False) -> bytes:
    """The scene drawn as the terminal viewer draws its content area, as raw Raster cells.

    len(result) == cols * rows * 12. `t` is seconds since the scene/slide appeared.
    """
    cols = _clamp_int(cols, 1, MAX_COLS, 80)
    rows = _clamp_int(rows, 1, MAX_ROWS, 24)
    cv = fx.Canvas(cols, rows)
    if isinstance(scene, dict):
        _draw(cv, scene, _clamp_int(slide, 0, max(0, slide_count(scene) - 1), 0), _clamp_t(t), bool(ascii))
    return pack(cv, bool(ascii))


def encode(cells: bytes) -> str:
    """Raw cells as standard padded base64, the encoding RasterProps.cells takes."""
    return base64.b64encode(bytes(cells)).decode("ascii")


# Settled frames, so `animating` is exact: a frame still differs from where it will come to rest.
_SETTLED: "OrderedDict[Tuple[Any, ...], bytes]" = OrderedDict()


def _fingerprint(scene: Dict[str, Any], slide: int) -> str:
    part: Any = scene
    if scene.get("kind") == "deck":
        slides = scene.get("slides") or []
        part = slides[slide] if 0 <= slide < len(slides) else {}
    elif "at" in scene:
        part = {k: v for k, v in scene.items() if k != "at"}
    try:
        blob = json.dumps(part, sort_keys=True, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        blob = repr(part)
    return hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest()


def _settled(scene: Dict[str, Any], cols: int, rows: int, slide: int, ascii: bool) -> bytes:
    key = (_fingerprint(scene, slide), cols, rows, slide, ascii)
    hit = _SETTLED.get(key)
    if hit is not None:
        _SETTLED.move_to_end(key)
        return hit
    cells = render(scene, cols, rows, slide, R.SETTLED, ascii)
    _SETTLED[key] = cells
    while len(_SETTLED) > _SETTLED_KEEP:
        _SETTLED.popitem(last=False)
    return cells


def frame(scene: Dict[str, Any], cols: int, rows: int, slide: int = 0, t: float = 9.0,
          ascii: bool = False) -> Dict[str, Any]:
    """One pane frame: the cells (base64), its geometry, the deck position and whether the intro still plays."""
    scene = scene if isinstance(scene, dict) else {}
    cols = _clamp_int(cols, 1, MAX_COLS, 80)
    rows = _clamp_int(rows, 1, MAX_ROWS, 24)
    n = slide_count(scene)
    i = _clamp_int(slide, 0, n - 1, 0)
    t = _clamp_t(t)
    ascii = bool(ascii)
    if t >= SETTLE_CAP:
        cells = _settled(scene, cols, rows, i, ascii)
        animating = False
    else:
        cells = render(scene, cols, rows, i, t, ascii)
        animating = cells != _settled(scene, cols, rows, i, ascii)
    slide_title = ""
    if scene.get("kind") == "deck":
        slides = scene.get("slides") or []
        if 0 <= i < len(slides) and isinstance(slides[i], dict):
            slide_title = str(slides[i].get("title") or "")
    return {
        "cells": encode(cells),
        "cols": cols,
        "rows": rows,
        "slides": n,
        "slide": i,
        "animating": animating,
        "title": _label(scene.get("title") or scene.get("kind") or ""),
        "slide_title": _label(slide_title),
        "kind": _wellformed(scene.get("kind") or ""),
        "id": _wellformed(scene.get("id") or ""),
        "t": round(t, 3) if not math.isinf(t) else R.SETTLED,
    }


def _wellformed(s: Any) -> str:
    """A str that encodes as UTF-8 (JSON answers): a lone surrogate becomes U+FFFD."""
    text = str(s)
    if not text.isascii():
        text = "".join("\ufffd" if 0xD800 <= ord(ch) <= 0xDFFF else ch for ch in text)
    return text


def _label(s: Any, limit: int = 120) -> str:
    """Text the plugin draws with Ink: no control, format or unpaired surrogate characters (wide is fine)."""
    out = []
    for ch in R.prep(str(s or ""), True):
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"):
            ch = " "
        elif cat.startswith("Z"):
            ch = " "
        out.append(ch)
        if len(out) >= limit:
            break
    return " ".join("".join(out).split())
