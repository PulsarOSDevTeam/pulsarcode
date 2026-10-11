# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""The pulsar cursor: an always-on-top, click-through overlay that shows the
user what the model is doing with their computer.

Run as its own process (python -m pulsar.pilot.overlay) and drive it with
JSON lines on stdin:

  {"op":"glide","x":..,"y":..,"label":"Save","ack":1}      comet glide (250-450 ms)
  {"op":"aim","x":..,"y":..,"label":"Save","ack":2}        glide + lock-on reticle
  {"op":"click","x":..,"y":..,"button":"left","count":1}   gradient ripple + chip
  {"op":"type","x":..,"y":..,"chars":42,"dur":0.6}         pulsing caret
  {"op":"key","x":..,"y":..,"label":"ctrl+s"}              keycaps
  {"op":"scroll","x":..,"y":..,"dx":0,"dy":3}              chevrons
  {"op":"drag","x1":..,"y1":..,"x2":..,"y2":..,"dur":0.5}  spectrum tether
  {"op":"scan","label":"1280x800"}                         screenshot sweep
  {"op":"hide","ack":3}  {"op":"show"}  {"op":"ping","ack":4}  {"op":"quit"}

stdout carries {"hello":1,...} once, then {"ack":n,...} replies. EOF on stdin
quits. Nothing here intercepts a click, and it is built never to keep the
keyboard focus:

  X11      override-redirect window shaped with XShape to exactly the pixels
           drawn (no compositor needed) and an empty input region.
  macOS    transparent borderless window, ignoresMouseEvents, accessory app
           (no Dock icon). Tk can activate the app while it starts: the focus
           is handed back to the app that had it, the window is never raised
           again while shown, and Pilot refuses to type while the overlay
           holds the focus (pulsar.pilot.tools).
  Windows  layered colour-key window with WS_EX_TRANSPARENT | NOACTIVATE.

When click-through cannot be set up the hello says so and the caller hides
the overlay around real input. Without tkinter the overlay is simply absent.
"""

from __future__ import annotations

import json
import math
import os
import queue
import random
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from pulsar.fx import BEAM, EMBER, FLARE, ICE, PLASMA, SPECTRUM, Color, grad, lerp, tri

INK: Color = (10, 9, 17)
CHIP: Color = (19, 16, 30)
CHIP_EDGE: Color = (70, 52, 120)
MUTED: Color = (150, 146, 172)
CHROMA_KEY = "#ff00fe"
IDLE_HIDE = 9.0
IDLE_EXIT = 600.0
TRAIL = 0.1

Prim = Tuple[Any, ...]


# ---------------------------------------------------------------------------
# Easing and geometry
# ---------------------------------------------------------------------------

def ease_io(p: float) -> float:
    p = 0.0 if p < 0 else 1.0 if p > 1 else p
    return 4 * p * p * p if p < 0.5 else 1 - (-2 * p + 2) ** 3 / 2


def ease_out(p: float) -> float:
    p = 0.0 if p < 0 else 1.0 if p > 1 else p
    return 1 - (1 - p) ** 3


def ease_back(p: float) -> float:
    p = 0.0 if p < 0 else 1.0 if p > 1 else p
    c = 1.9
    return 1 + (c + 1) * (p - 1) ** 3 + c * (p - 1) ** 2


def glide_duration(x1: float, y1: float, x2: float, y2: float) -> float:
    """250-450 ms, longer for longer trips."""
    d = math.hypot(x2 - x1, y2 - y1)
    return 0.25 + 0.2 * min(1.0, d / 900.0)


def rrect(x0: float, y0: float, x1: float, y1: float, r: float, seg: int = 4) -> List[float]:
    """Rounded rectangle as a flat polygon (explicit points so shape masks match exactly)."""
    r = max(0.0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    pts: List[float] = []
    for cx, cy, a0 in ((x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0), (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180)):
        for i in range(seg + 1):
            a = math.radians(a0 + 90.0 * i / seg)
            pts += [cx + r * math.cos(a), cy + r * math.sin(a)]
    return pts


def _polar(cx: float, cy: float, a: float, r: float, off: float = 0.0) -> Tuple[float, float]:
    ca, sa = math.cos(a), math.sin(a)
    return cx + ca * r - sa * off, cy + sa * r + ca * off


def hexc(c: Color) -> str:
    return "#%02x%02x%02x" % (max(0, min(255, int(c[0]))), max(0, min(255, int(c[1]))), max(0, min(255, int(c[2]))))


# ---------------------------------------------------------------------------
# Scene: pure state -> primitives (no tk; unit-tested)
# ---------------------------------------------------------------------------
# Primitives
#   ("oval", cx, cy, r, fill|None, outline|None, width)
#   ("arc", cx, cy, r, start_deg, extent_deg, color, width)
#   ("poly", [x0, y0, ...], fill, outline|None, width)
#   ("line", [x0, y0, ...], color, width)
#   ("rect", x0, y0, x1, y1, fill)
#   ("text", x, y, text, color, size_px, bold, anchor)    (always drawn on a chip)


class Scene:
    def __init__(self, w: int, h: int, k: float = 1.0, reduced: bool = False,
                 measure: Optional[Callable[[str, int, bool], int]] = None, seed: int = 7) -> None:
        self.w, self.h, self.k = w, h, k
        self.reduced = reduced
        self.measure = measure or (lambda s, size, bold: int(len(s) * size * (0.64 if bold else 0.58)))
        self.rng = random.Random(seed)
        self.x, self.y = w / 2.0, h / 2.0
        self.visible = False
        self.shown_at = 0.0
        self.hide_at = 0.0
        self.last = 0.0
        self.glides: List[Tuple[float, float, float, float, float, float]] = []   # x1,y1,x2,y2,t0,dur
        self.fx: List[Dict[str, Any]] = []
        self.parts: List[Dict[str, Any]] = []
        self.chip: Optional[Dict[str, Any]] = None
        self.lock: Optional[Dict[str, Any]] = None
        self.beam = -0.6
        self.prev_pos = (self.x, self.y)
        self.idle_hide = IDLE_HIDE

    # -- motion --------------------------------------------------------------------------
    def pos_at(self, t: float) -> Tuple[float, float]:
        for x1, y1, x2, y2, t0, dur in reversed(self.glides):
            if t >= t0:
                if dur <= 0 or t >= t0 + dur:
                    return x2, y2
                e = ease_io((t - t0) / dur)
                return x1 + (x2 - x1) * e, y1 + (y2 - y1) * e
        if self.glides:
            g = self.glides[0]
            return g[0], g[1]
        return self.x, self.y

    def gliding(self, t: float) -> bool:
        return bool(self.glides) and t < self.glides[-1][4] + self.glides[-1][5]

    def _glide(self, x: float, y: float, t: float) -> float:
        cx, cy = self.pos_at(t)
        dur = 0.0 if self.reduced else glide_duration(cx, cy, x, y)
        if math.hypot(x - cx, y - cy) < 2:
            dur = 0.0
        self.glides = self.glides[-2:] + [(cx, cy, float(x), float(y), t, dur)]
        self.x, self.y = float(x), float(y)
        return dur

    def _show(self, t: float, frm: Optional[Sequence[float]] = None) -> None:
        if not self.visible:
            self.visible = True
            self.shown_at = t
            if frm is not None and len(frm) == 2:
                fx_, fy_ = float(frm[0]), float(frm[1])
            else:
                fx_, fy_ = self.x, self.y
            self.glides = [(fx_, fy_, fx_, fy_, t, 0.0)]
            self.x, self.y = fx_, fy_
        self.hide_at = 0.0

    def _chip(self, verb: str, label: str, t: float, ttl: float = 1.8, tone: Color = BEAM) -> None:
        label = " ".join(str(label or "").split())[:48]
        self.chip = {"verb": verb, "label": label, "t0": t, "until": t + ttl, "tone": tone}

    # -- ops -----------------------------------------------------------------------------
    def apply(self, op: Dict[str, Any], t: float) -> float:
        """Apply one op. Returns seconds until the op's ack is due (0 = now)."""
        kind = str(op.get("op") or "")
        self.last = t
        if kind in ("glide", "aim", "click", "drag", "scan", "key", "type", "scroll"):
            for f in self.fx:   # a new action fast-fades the previous one's keycaps / chevrons / caret
                if f["kind"] in ("keys", "scroll", "caret") and t > f["t0"]:
                    f["dur"] = min(f["dur"], t - f["t0"] + 0.12)
        x = _f(op.get("x"), self.x)
        y = _f(op.get("y"), self.y)
        frm = op.get("from")
        if kind in ("glide", "aim"):
            self._show(t, frm)
            dur = self._glide(x, y, t)
            verb = str(op.get("verb") or ("move" if kind == "glide" else "click"))
            if op.get("label") or kind == "aim":
                self._chip(verb, str(op.get("label") or ""), t, ttl=dur + 1.6)
            if kind == "aim":
                lock = 0.0 if self.reduced else 0.16
                self.lock = {"x": x, "y": y, "t0": t + dur, "dur": lock, "until": t + dur + lock + 0.6}
                return dur + lock
            return dur
        if kind == "click":
            self._show(t, frm)
            if math.hypot(x - self.x, y - self.y) >= 2:
                self.glides.append((x, y, x, y, t, 0.0))
                self.x, self.y = x, y
            button = str(op.get("button") or "left")
            count = max(1, min(3, int(_f(op.get("count"), 1))))
            for i in range(count):
                self.fx.append({"kind": "ripple", "x": x, "y": y, "t0": t + i * 0.11, "dur": 0.32 if self.reduced else 0.62,
                                "button": button})
            if not self.reduced:
                self._burst(x, y, t, 12 if button == "left" else 8)
            verb = {1: "", 2: "double ", 3: "triple "}[count] + ("click" if button == "left" else button + " click")
            self._chip(verb, str(op.get("label") or ""), t, tone=PLASMA if button == "right" else BEAM)
            self.lock = None
            return 0.0
        if kind == "type":
            self._show(t, frm)
            if "x" in op:
                self._glide(x, y, t)
            chars = int(_f(op.get("chars"), 0))
            dur = max(0.6, min(6.0, _f(op.get("dur"), chars * 0.012 + 0.4)))
            self.fx.append({"kind": "caret", "t0": t, "dur": dur})
            self._chip("type", "%d char%s" % (chars, "" if chars == 1 else "s") if not op.get("label")
                       else str(op.get("label")), t, ttl=dur + 0.8, tone=FLARE)
            return 0.0
        if kind == "key":
            self._show(t, frm)
            label = str(op.get("label") or "")
            self.fx.append({"kind": "keys", "t0": t, "dur": 1.5, "groups": _key_groups(label), "x": self.x, "y": self.y})
            self.fx.append({"kind": "pulse", "x": self.x, "y": self.y, "t0": t, "dur": 0.4})
            self.chip = None
            return 0.0
        if kind == "scroll":
            self._show(t, frm)
            if "x" in op:
                self._glide(x, y, t)
            self.fx.append({"kind": "scroll", "t0": t, "dur": 0.9, "dx": _f(op.get("dx"), 0), "dy": _f(op.get("dy"), 0),
                            "x": self.x, "y": self.y})
            dy, dx = _f(op.get("dy"), 0), _f(op.get("dx"), 0)
            what = ("down" if dy > 0 else "up") if abs(dy) >= abs(dx) else ("right" if dx > 0 else "left")
            self._chip("scroll", "%s %d" % (what, int(max(abs(dy), abs(dx)))), t, ttl=1.4)
            return 0.0
        if kind == "drag":
            self._show(t, frm)
            x1, y1 = _f(op.get("x1"), self.x), _f(op.get("y1"), self.y)
            x2, y2 = _f(op.get("x2"), self.x), _f(op.get("y2"), self.y)
            dur = max(0.2, min(3.0, _f(op.get("dur"), 0.5)))
            self.glides.append((x1, y1, x2, y2, t, 0.0 if self.reduced else dur))
            self.x, self.y = x2, y2
            self.fx.append({"kind": "tether", "x": x1, "y": y1, "t0": t, "dur": dur + 0.5, "end": t + dur})
            self.fx.append({"kind": "ripple", "x": x1, "y": y1, "t0": t, "dur": 0.45, "button": "left"})
            self.fx.append({"kind": "ripple", "x": x2, "y": y2, "t0": t + dur, "dur": 0.55, "button": "left"})
            self._chip("drag", str(op.get("label") or ""), t, ttl=dur + 1.2)
            self.lock = None
            return dur
        if kind == "scan":
            self._show(t, frm)
            self.fx.append({"kind": "scan", "t0": t, "dur": 0.2 if self.reduced else 0.55})
            self._chip("screenshot", str(op.get("label") or ""), t, ttl=1.6, tone=BEAM)
            return 0.0
        if kind == "show":
            self._show(t, frm)
            return 0.0
        if kind == "hide":
            self.visible = False
            self.fx = []
            self.parts = []
            self.chip = None
            self.lock = None
            return 0.0
        return 0.0

    def _burst(self, x: float, y: float, t: float, n: int) -> None:
        for i in range(n):
            a = 2 * math.pi * (i + self.rng.random() * 0.6) / n
            sp = (90 + self.rng.random() * 140) * self.k
            self.parts.append({"x": x, "y": y, "vx": math.cos(a) * sp, "vy": math.sin(a) * sp, "t0": t,
                               "life": 0.35 + self.rng.random() * 0.25, "c": grad(SPECTRUM, i / max(1, n - 1)),
                               "s": (1.6 + self.rng.random() * 1.6) * self.k})

    # -- frame ---------------------------------------------------------------------------
    def busy(self, t: float) -> bool:
        return self.visible and (self.gliding(t) or bool(self.fx) or bool(self.parts)
                                 or t - self.shown_at < 0.4 or (self.hide_at and t < self.hide_at + 0.4))

    def tick(self, t: float) -> None:
        """Advance time-based state (idle auto-hide, expiry)."""
        self.fx = [f for f in self.fx if t < f["t0"] + f["dur"]]
        self.parts = [p for p in self.parts if t < p["t0"] + p["life"]]
        if self.chip and t > self.chip["until"]:
            self.chip = None
        if self.lock and t > self.lock["until"]:
            self.lock = None
        if self.visible and not self.hide_at and not self.busy(t) and t - self.last > self.idle_hide:
            self.hide_at = t
        if self.hide_at and t > self.hide_at + 0.35:
            self.visible = False
            self.hide_at = 0.0

    def frame(self, t: float) -> List[Prim]:
        self.tick(t)
        if not self.visible:
            return []
        out: List[Prim] = []
        k = self.k
        x, y = self.pos_at(t)
        # spawn trail sparks while gliding
        if self.gliding(t) and not self.reduced:
            px, py = self.prev_pos
            vx, vy = x - px, y - py
            if math.hypot(vx, vy) > 3:
                for _ in range(2):
                    a = math.atan2(-vy, -vx) + (self.rng.random() - 0.5) * 1.4
                    sp = (60 + self.rng.random() * 120) * k
                    self.parts.append({"x": x, "y": y, "vx": math.cos(a) * sp, "vy": math.sin(a) * sp, "t0": t,
                                       "life": 0.25 + self.rng.random() * 0.2,
                                       "c": grad(SPECTRUM, self.rng.random()), "s": (1.2 + self.rng.random() * 1.4) * k})
        self.prev_pos = (x, y)
        scale = 1.0
        if t - self.shown_at < 0.28 and not self.reduced:
            scale = ease_back((t - self.shown_at) / 0.28)
        if self.hide_at:
            scale = max(0.0, 1 - (t - self.hide_at) / 0.3)
        for f in self.fx:
            if f["kind"] == "scan":
                self._scan(out, f, t)
        if not self.reduced and not any(f["kind"] == "tether" for f in self.fx):
            self._trail(out, t)
        for f in self.fx:
            if f["kind"] == "tether":
                self._tether(out, f, t, x, y)
        for f in self.fx:
            kind = f["kind"]
            if kind == "ripple" and t >= f["t0"]:
                self._ripple(out, f, t)
            elif kind == "pulse":
                self._pulse(out, f, t)
            elif kind == "scroll":
                self._scroll(out, f, t, x, y)
        self._particles(out, t)
        if self.lock and t >= self.lock["t0"]:
            self._reticle(out, self.lock, t)
        self._core(out, x, y, t, scale)
        for f in self.fx:
            if f["kind"] == "caret":
                self._caret(out, f, t, x, y)
        for f in self.fx:
            if f["kind"] == "keys":
                self._keys(out, f, t, x, y)
        if self.chip and scale > 0.5:
            self._draw_chip(out, x, y, t)
        return out

    # -- pieces ----------------------------------------------------------------------------
    def _core(self, out: List[Prim], x: float, y: float, t: float, s: float) -> None:
        k = self.k * s
        if k < 0.08:
            return
        moving = self.gliding(t)
        g = self.glides[-1] if self.glides else None
        if moving and g is not None and math.hypot(g[2] - g[0], g[3] - g[1]) > 4:
            target = math.atan2(g[1] - g[3], g[0] - g[2])   # beams stream behind the motion
            d = (target - self.beam + math.pi) % (2 * math.pi) - math.pi
            self.beam += d * 0.35
        elif not self.reduced:
            self.beam += 0.03
        breath = 0.0 if self.reduced else math.sin(t * 4.2) * 0.9
        stretch = 1.3 if moving else 1.0
        # pulsar beams (two jets)
        for j, (c1, c2) in enumerate(((FLARE, EMBER), (PLASMA, BEAM))):
            a = self.beam + j * math.pi
            base, tip = 7.0 * k, (38.0 + 4 * math.sin(t * 3.1 + j * 1.7)) * k * stretch
            half = 4.4 * k
            mid = base + (tip - base) * 0.3
            o = 1.7 + 0.5 * k
            ink = [_polar(x, y, a, base - o, half + o), _polar(x, y, a, mid, half * 0.78 + o),
                   _polar(x, y, a, tip + o * 1.6), _polar(x, y, a, mid, -half * 0.78 - o),
                   _polar(x, y, a, base - o, -half - o)]
            out.append(("poly", _flat(ink), INK, None, 0))
            body = [_polar(x, y, a, base, half), _polar(x, y, a, mid, half * 0.78), _polar(x, y, a, tip),
                    _polar(x, y, a, mid, -half * 0.78), _polar(x, y, a, base, -half)]
            out.append(("poly", _flat(body), c1, None, 0))
            core = [_polar(x, y, a, base, half * 0.5), _polar(x, y, a, mid, half * 0.42),
                    _polar(x, y, a, tip * 0.8), _polar(x, y, a, mid, -half * 0.42), _polar(x, y, a, base, -half * 0.5)]
            out.append(("poly", _flat(core), c2, None, 0))
            hot = _polar(x, y, a, tip * 0.55)
            out.append(("line", [_polar(x, y, a, base)[0], _polar(x, y, a, base)[1], hot[0], hot[1]],
                        lerp(c2, ICE, 0.7), max(1.0, 1.3 * k)))
        # segmented spectrum ring + hot inner ring
        r = 15.5 * k + breath * k
        out.append(("oval", x, y, r, None, INK, 3.6 * k + 3.0))
        out.append(("oval", x, y, r - 1.0 * k, INK, None, 0))          # dark lens behind the core
        n = 24
        spin = 0.0 if self.reduced else -t * 60.0
        for i in range(n):
            start = spin + i * 360.0 / n
            out.append(("arc", x, y, r, start, 360.0 / n - 4.5, grad(SPECTRUM, tri(i * 2.0 / n + t * 0.25)), 3.6 * k))
        glow = 0.5 + 0.5 * math.sin(t * 6.0)
        out.append(("oval", x, y, r - 4.6 * k, None, lerp(BEAM, ICE, 0.35 + 0.4 * glow), max(1.0, 1.4 * k)))
        # core
        out.append(("oval", x, y, 6.6 * k, INK, None, 0))
        out.append(("oval", x, y, 5.0 * k, lerp(ICE, BEAM, 0.1 + 0.3 * glow), None, 0))
        out.append(("oval", x, y, max(1.0, 1.5 * k), INK, None, 0))

    def _trail(self, out: List[Prim], t: float) -> None:
        if not self.glides:
            return
        g = self.glides[-1]
        if t > g[4] + g[5] + TRAIL or g[5] <= 0:
            return
        n = 20
        pts = [self.pos_at(t - TRAIL * i / (n - 1)) for i in range(n)]
        length = sum(math.hypot(pts[i][0] - pts[i + 1][0], pts[i][1] - pts[i + 1][1]) for i in range(n - 1))
        if length < 6:
            return
        k = self.k
        rows = []
        for i, (px, py) in enumerate(pts):
            a = pts[max(0, i - 1)]
            b = pts[min(n - 1, i + 1)]
            dx, dy = a[0] - b[0], a[1] - b[1]
            d = math.hypot(dx, dy) or 1.0
            f = 1 - i / (n - 1.0)
            rows.append((px, py, -dy / d, dx / d, (0.3 + 8.5 * f ** 1.8) * k))
        ink = [(px + nx * (w * 1.3 + 0.8), py + ny * (w * 1.3 + 0.8)) for px, py, nx, ny, w in rows] + \
            [(px - nx * (w * 1.3 + 0.8), py - ny * (w * 1.3 + 0.8)) for px, py, nx, ny, w in reversed(rows)]
        out.append(("poly", _flat(ink), INK, None, 0))
        for i in range(n - 1):
            f = 1 - (i + 0.5) / (n - 1.0)
            c = grad((EMBER, FLARE, PLASMA, BEAM, ICE), f ** 0.8)
            p0, p1 = rows[i], rows[i + 1]
            quad = [p0[0] + p0[2] * p0[4], p0[1] + p0[3] * p0[4], p1[0] + p1[2] * p1[4], p1[1] + p1[3] * p1[4],
                    p1[0] - p1[2] * p1[4], p1[1] - p1[3] * p1[4], p0[0] - p0[2] * p0[4], p0[1] - p0[3] * p0[4]]
            out.append(("poly", quad, c, None, 0))
        hot = [(px, py) for px, py, _, _, _ in rows[: max(2, n // 3)]]
        out.append(("line", _flat(hot), ICE, max(1.0, 2.2 * k)))

    def _particles(self, out: List[Prim], t: float) -> None:
        for p in self.parts:
            age = t - p["t0"]
            if age < 0:
                continue
            f = 1 - age / p["life"]
            px = p["x"] + p["vx"] * age * (1 - age * 0.9)
            py = p["y"] + p["vy"] * age * (1 - age * 0.9)
            s = p["s"] * (0.4 + 0.6 * f)
            out.append(("poly", [px, py - s - 0.8, px + s + 0.8, py, px, py + s + 0.8, px - s - 0.8, py], INK, None, 0))
            out.append(("poly", [px, py - s, px + s, py, px, py + s, px - s, py], lerp(p["c"], ICE, 0.25 * f), None, 0))

    def _ripple(self, out: List[Prim], f: Dict[str, Any], t: float) -> None:
        k = self.k
        p = (t - f["t0"]) / f["dur"]
        x, y = f["x"], f["y"]
        right = f.get("button") == "right"
        e = ease_out(p)
        r = (10 + 58 * e) * k
        w = max(1.0, 7.5 * (1 - p) * k)
        col = grad((PLASMA, FLARE, PLASMA) if right else SPECTRUM, p)
        if w > 1.2:
            out.append(("oval", x, y, r, None, INK, w + 3))
        if right:
            for i in range(10):
                out.append(("arc", x, y, r, i * 36.0 + p * 90, 22.0, col, w))
        else:
            out.append(("oval", x, y, r, None, col, w))
        p2 = (t - f["t0"] - 0.07) / f["dur"]
        if 0 <= p2 < 1 and not self.reduced:
            r2 = (5 + 36 * ease_out(p2)) * k
            w2 = max(1.0, 3.2 * (1 - p2) * k)
            out.append(("oval", x, y, r2, None, INK, w2 + 2.5))
            out.append(("oval", x, y, r2, None, lerp(ICE, BEAM if not right else PLASMA, p2), w2))
        if p < 0.22:
            fr = 12 * (1 - p / 0.22) * k
            out.append(("oval", x, y, fr + 1.5, INK, None, 0))
            out.append(("oval", x, y, fr, ICE, None, 0))
        if not self.reduced and 0.06 < p < 0.8:
            q = (p - 0.06) / 0.74
            for i in range(8):
                a = i * math.pi / 4 + math.pi / 8
                r1 = (24 + 50 * ease_out(q)) * k
                r2 = r1 + 8 * (1 - q) * k
                p1, p2_ = _polar(x, y, a, r1), _polar(x, y, a, r2)
                out.append(("line", [p1[0], p1[1], p2_[0], p2_[1]], INK, 4.5 * k * (1 - q) + 2))
                out.append(("line", [p1[0], p1[1], p2_[0], p2_[1]], grad(SPECTRUM, i / 7.0), max(1.0, 2.4 * k * (1 - q))))

    def _pulse(self, out: List[Prim], f: Dict[str, Any], t: float) -> None:
        p = (t - f["t0"]) / f["dur"]
        r = (12 + 22 * ease_out(p)) * self.k
        w = max(1.0, 3.5 * (1 - p) * self.k)
        out.append(("oval", f["x"], f["y"], r, None, INK, w + 2.5))
        out.append(("oval", f["x"], f["y"], r, None, grad((BEAM, PLASMA), p), w))

    def _reticle(self, out: List[Prim], lk: Dict[str, Any], t: float) -> None:
        """Four viewfinder corners that snap in around the target."""
        k = self.k
        p = 1.0 if lk["dur"] <= 0 else min(1.0, (t - lk["t0"]) / lk["dur"])
        e = ease_out(p)
        R = (40 - 18 * e) * k
        rot = math.radians(45 * (1 - e))
        leg = 9 * k
        col = lerp(ICE, BEAM, 0.5 + 0.5 * math.sin(t * 14))
        cx, cy = lk["x"], lk["y"]
        cr, sr = math.cos(rot), math.sin(rot)

        def P(dx: float, dy: float) -> List[float]:
            return [cx + dx * cr - dy * sr, cy + dx * sr + dy * cr]
        for sx, sy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
            pts = P(sx * R, sy * (R - leg)) + P(sx * R, sy * R) + P(sx * (R - leg), sy * R)
            out.append(("line", pts, INK, 2.6 * k + 3))
            out.append(("line", pts, col, 2.6 * k))

    def _scroll(self, out: List[Prim], f: Dict[str, Any], t: float, x: float, y: float) -> None:
        k = self.k
        x, y = f.get("x", x), f.get("y", y)
        dx, dy = f["dx"], f["dy"]
        a = math.atan2(1 if dy > 0 else -1, 0) if abs(dy) >= abs(dx) else (0.0 if dx > 0 else math.pi)
        phase = ((t - f["t0"]) * 2.6) % 1.0
        for i in range(3):
            d = (32 + (i + phase) * 11) * k
            fade = 1 - (i + phase) / 3.0
            cx, cy = _polar(x, y, a, d)
            pa = _polar(cx, cy, a, -5 * k, 7 * k)
            pb = _polar(cx, cy, a, 2 * k)
            pc = _polar(cx, cy, a, -5 * k, -7 * k)
            pts = [pa[0], pa[1], pb[0], pb[1], pc[0], pc[1]]
            out.append(("line", pts, INK, 3.2 * k * fade + 3))
            out.append(("line", pts, grad(SPECTRUM, 0.15 + 0.8 * fade), max(1.0, 3.2 * k * fade)))

    def _tether(self, out: List[Prim], f: Dict[str, Any], t: float, x: float, y: float) -> None:
        k = self.k
        x1, y1 = f["x"], f["y"]
        if math.hypot(x - x1, y - y1) < 3:
            return
        n = 14
        pts = [(x1 + (x - x1) * i / n, y1 + (y - y1) * i / n) for i in range(n + 1)]
        out.append(("line", _flat(pts), INK, 4.6 * k + 4))
        for i in range(n):
            out.append(("line", [pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1]], grad(SPECTRUM, i / (n - 1.0)), 4.6 * k))
        out.append(("line", _flat(pts), lerp(ICE, BEAM, 0.3), max(1.0, 1.2 * k)))
        for j in range(4):
            q = ((t - f["t0"]) * 1.4 + j / 4.0) % 1.0
            bx, by = x1 + (x - x1) * q, y1 + (y - y1) * q
            out.append(("oval", bx, by, 3.2 * k, ICE, INK, 1.5))
        out.append(("oval", x1, y1, 5 * k, INK, None, 0))
        out.append(("oval", x1, y1, 3.4 * k, EMBER, None, 0))

    def _caret(self, out: List[Prim], f: Dict[str, Any], t: float, x: float, y: float) -> None:
        k = self.k
        age = t - f["t0"]
        pulse = 0.5 + 0.5 * math.sin(age * 14)
        cx = x + 27 * k
        h = 13 * k
        out.append(("rect", cx - 2.6 * k, y - h - 1.6, cx + 2.6 * k, y + h + 1.6, INK))
        out.append(("rect", cx - 1.3 * k, y - h, cx + 1.3 * k, y + h, lerp(BEAM, ICE, pulse)))
        out.append(("rect", cx - 4.2 * k, y - h - 1.6, cx + 4.2 * k, y - h + 1.6, lerp(BEAM, ICE, pulse)))
        out.append(("rect", cx - 4.2 * k, y + h - 1.6, cx + 4.2 * k, y + h + 1.6, lerp(BEAM, ICE, pulse)))
        if not self.reduced and age < f["dur"] - 0.2:
            beat = int(age / 0.07)
            for b in range(max(0, beat - 6), beat + 1):
                bt = b * 0.07
                a = age - bt
                if a < 0 or a > 0.45:
                    continue
                rr = random.Random(b * 7919)
                px = cx + 6 * k + a * (40 + rr.random() * 40) * k
                py = y - h * 0.4 - a * (60 + rr.random() * 50) * k
                s = (3.2 - a * 5) * k
                if s <= 0.4:
                    continue
                c = grad(SPECTRUM, (b * 0.17) % 1.0)
                out.append(("rect", px - s - 1.2, py - s - 1.2, px + s + 1.2, py + s + 1.2, INK))
                out.append(("rect", px - s, py - s, px + s, py + s, c))

    def _keys(self, out: List[Prim], f: Dict[str, Any], t: float, x: float, y: float) -> None:
        k = self.k
        x, y = f.get("x", x), f.get("y", y)
        age = t - f["t0"]
        size = int(round(11 * k))
        caps: List[Tuple[str, int]] = []
        for gi, group in enumerate(f["groups"]):
            for name in group:
                caps.append((name, gi))
        if not caps:
            return
        hgt = 24 * k
        pad = 9 * k
        gap = 5 * k
        widths = [max(hgt, self.measure(name, size, True) + 2 * pad) for name, _ in caps]
        total = sum(widths) + gap * (len(caps) - 1) + 10 * k * (len(f["groups"]) - 1)
        x0 = x + 30 * k
        y0 = y - 58 * k
        if x0 + total > self.w - 8:
            x0 = x - 30 * k - total
        if y0 < 8:
            y0 = y + 34 * k
        cx = x0
        fade = 1.0 if age < f["dur"] - 0.2 else max(0.0, (f["dur"] - age) / 0.2)
        prev_group = 0
        for i, ((name, gi), w) in enumerate(zip(caps, widths)):
            if gi != prev_group:
                cx += 10 * k
                prev_group = gi
            p = ease_back((age - i * 0.05) / 0.16)
            if p <= 0.02:
                cx += w + gap
                continue
            s = p * (0.6 + 0.4 * fade)
            mx, my = cx + w / 2, y0 + hgt / 2
            hw, hh = w / 2 * s, hgt / 2 * s
            tone = grad(SPECTRUM, i / max(1.0, len(caps) - 1.0))
            out.append(("poly", rrect(mx - hw - 2, my - hh - 2, mx + hw + 2, my + hh + 2 + 2 * k, 7 * k), INK, None, 0))
            out.append(("poly", rrect(mx - hw, my - hh + 2 * k, mx + hw, my + hh + 2 * k, 6 * k), lerp(tone, INK, 0.45), None, 0))
            out.append(("poly", rrect(mx - hw, my - hh, mx + hw, my + hh, 6 * k), CHIP, tone, max(1.0, 1.4 * k)))
            if s > 0.75:
                out.append(("text", mx, my, name, ICE, size, True, "center"))
            cx += w + gap

    def _scan(self, out: List[Prim], f: Dict[str, Any], t: float) -> None:
        k = self.k
        p = (t - f["t0"]) / f["dur"]
        w, h = self.w, self.h
        y = ease_io(p) * (h + 30) - 10
        bands = ((0, 2.4, ICE), (-4, 2.0, BEAM), (-9, 1.4, PLASMA), (-15, 1.0, FLARE), (-22, 1.0, EMBER))
        if not self.reduced:
            for off, th, col in bands:
                yy = y + off * k
                if -4 < yy < h + 4:
                    out.append(("rect", 0, yy - th * k / 2, w, yy + th * k / 2, col))
        vis = min(1.0, p / 0.15, (1 - p) / 0.15 + 0.0001)
        leg = 46 * k * max(0.0, vis)
        ins = 14 * k
        if leg > 2:
            for sx, sy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
                cx = ins if sx > 0 else w - 1 - ins
                cy = ins if sy > 0 else h - 1 - ins
                pts = [cx, cy + sy * leg, cx, cy, cx + sx * leg, cy]
                out.append(("line", pts, INK, 3 * k + 3))
                out.append(("line", pts, BEAM, 3 * k))

    def _draw_chip(self, out: List[Prim], x: float, y: float, t: float) -> None:
        c = self.chip
        assert c is not None
        k = self.k
        size = int(round(11 * k))
        verb = c["verb"].upper()
        label = c["label"]
        wv = self.measure(verb, size, True)
        wl = self.measure(label, size, False) if label else 0
        pad = 9 * k
        dot = 14 * k
        hgt = 24 * k
        w = pad + dot + wv + (8 * k + wl if label else 0) + pad
        cx, cy = x + 26 * k, y + 24 * k
        if cx + w > self.w - 6:
            cx = x - 26 * k - w
        if cy + hgt > self.h - 6:
            cy = y - 24 * k - hgt
        age = t - c["t0"]
        rise = 0.0 if self.reduced else 6 * k * (1 - ease_out(age / 0.18))
        cy += rise
        edge = lerp(ICE, CHIP_EDGE, ease_out(age / 0.3))
        out.append(("poly", rrect(cx - 1.8, cy - 1.8, cx + w + 1.8, cy + hgt + 1.8, 9 * k), INK, None, 0))
        out.append(("poly", rrect(cx, cy, cx + w, cy + hgt, 8 * k), CHIP, edge, 1))
        out.append(("rect", cx + 1, cy + hgt - 2.2 * k, cx + w - 1, cy + hgt - 1, c["tone"]))
        mx, my = cx + pad + 5 * k, cy + hgt / 2 - 0.5
        out.append(("oval", mx, my, 5.2 * k, None, lerp(c["tone"], ICE, 0.2), 1.8 * k))
        out.append(("oval", mx, my, 2.0 * k, ICE, None, 0))
        tx = cx + pad + dot
        out.append(("text", tx, my, verb, c["tone"], size, True, "w"))
        if label:
            out.append(("text", tx + wv + 8 * k, my, label, ICE, size, False, "w"))


def _f(v: Any, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _flat(pts: Sequence[Tuple[float, float]]) -> List[float]:
    out: List[float] = []
    for px, py in pts:
        out += [px, py]
    return out


_CAP_NAMES = {"cmd": "cmd", "ctrl": "ctrl", "alt": "alt", "shift": "shift", "enter": "enter", "escape": "esc",
              "backspace": "bksp", "delete": "del", "space": "space", "tab": "tab", "pageup": "pgup",
              "pagedown": "pgdn", "up": "up", "down": "down", "left": "left", "right": "right"}


def _key_groups(label: str) -> List[List[str]]:
    groups = []
    for token in label.split()[:6]:
        parts = [p for p in token.replace("++", "+plus").split("+") if p][:5]
        names = []
        for p in parts:
            low = p.lower()
            if low == "plus":
                names.append("+")
            elif len(p) == 1:
                names.append(p.upper())
            else:
                names.append(_CAP_NAMES.get(low, low[:6]))
        if names:
            groups.append(names)
    return groups


# ---------------------------------------------------------------------------
# Platform click-through
# ---------------------------------------------------------------------------

class XShapeMask:
    """Mirror the frame's primitives into a 1-bit pixmap and shape the window with it."""

    def __init__(self, win: int, w: int, h: int, extra_windows: Sequence[int] = ()) -> None:
        import ctypes
        import ctypes.util
        self.ct = ctypes
        x = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        e = ctypes.CDLL(ctypes.util.find_library("Xext") or "libXext.so.6")
        vp, ul, ci, cu = ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_uint
        self.x, self.e = x, e
        for fname, res, args in (
                ("XOpenDisplay", vp, [ctypes.c_char_p]), ("XCreatePixmap", ul, [vp, ul, cu, cu, cu]),
                ("XCreateGC", vp, [vp, ul, ul, vp]), ("XSetForeground", ci, [vp, vp, ul]),
                ("XFillRectangle", ci, [vp, ul, vp, ci, ci, cu, cu]),
                ("XFillArc", ci, [vp, ul, vp, ci, ci, cu, cu, ci, ci]),
                ("XDrawArc", ci, [vp, ul, vp, ci, ci, cu, cu, ci, ci]),
                ("XFillPolygon", ci, [vp, ul, vp, vp, ci, ci, ci]),
                ("XDrawLines", ci, [vp, ul, vp, vp, ci, ci]),
                ("XSetLineAttributes", ci, [vp, vp, cu, ci, ci, ci]), ("XFlush", ci, [vp]),
                ("XSync", ci, [vp, ci]), ("XFreePixmap", ci, [vp, ul]), ("XFreeGC", ci, [vp, vp]),
                ("XCloseDisplay", ci, [vp]), ("XFree", ci, [vp]),
                ("XQueryTree", ci, [vp, ul, ctypes.POINTER(ul), ctypes.POINTER(ul),
                                    ctypes.POINTER(ctypes.POINTER(ul)), ctypes.POINTER(cu)])):
            fn = getattr(x, fname)
            fn.restype, fn.argtypes = res, args
        for fname, res, args in (("XShapeQueryExtension", ci, [vp, ctypes.POINTER(ci), ctypes.POINTER(ci)]),
                                 ("XShapeQueryVersion", ci, [vp, ctypes.POINTER(ci), ctypes.POINTER(ci)]),
                                 ("XShapeCombineMask", None, [vp, ul, ci, ci, ci, ul, ci]),
                                 ("XShapeCombineRectangles", None, [vp, ul, ci, ci, ci, vp, ci, ci, ci])):
            fn = getattr(e, fname)
            fn.restype, fn.argtypes = res, args
        self.dpy = x.XOpenDisplay(None)
        if not self.dpy:
            raise RuntimeError("no X display")
        self._install_error_handler()
        a, b = ci(), ci()
        if not e.XShapeQueryExtension(self.dpy, ctypes.byref(a), ctypes.byref(b)):
            raise RuntimeError("X server has no SHAPE extension")
        e.XShapeQueryVersion(self.dpy, ctypes.byref(a), ctypes.byref(b))
        self.input_ok = (a.value, b.value) >= (1, 1)
        self.win = win
        self.windows: List[int] = [win]
        self.retarget(win, extra_windows)
        self.w, self.h = w, h
        self.pm = x.XCreatePixmap(self.dpy, win, w, h, 1)
        self.gc = x.XCreateGC(self.dpy, self.pm, 0, None)

        class XPoint(ctypes.Structure):
            _fields_ = [("x", ctypes.c_short), ("y", ctypes.c_short)]
        self.XPoint = XPoint
        self.clickthrough()

    def outermost(self, win: int) -> int:
        """Walk up to the child of the root. Tk wraps every toplevel in a wrapper window, and without a
        window manager `wm frame` reports the toplevel itself, not that wrapper."""
        ct = self.ct
        cur = win
        for _ in range(16):
            root, parent = ct.c_ulong(), ct.c_ulong()
            kids = ct.POINTER(ct.c_ulong)()
            n = ct.c_uint()
            if not self.x.XQueryTree(self.dpy, cur, ct.byref(root), ct.byref(parent), ct.byref(kids), ct.byref(n)):
                break
            if kids:
                self.x.XFree(kids)
            if not parent.value or parent.value == root.value:
                return cur
            cur = parent.value
        return cur

    def retarget(self, win: int, extra: Sequence[int] = ()) -> None:
        top = self.outermost(win)
        self.win = top
        self.windows = [top] + [w_ for w_ in [win] + list(extra) if w_ and w_ != top]
        self.windows = list(dict.fromkeys(self.windows))

    def _install_error_handler(self) -> None:
        """Errors on our own connection (e.g. a Tk child window not created yet) are harmless; anything
        else goes to the previous handler (Tk's) so Tk keeps its own error semantics."""
        ct = self.ct
        proto = ct.CFUNCTYPE(ct.c_int, ct.c_void_p, ct.c_void_p)
        self.x.XSetErrorHandler.restype = ct.c_void_p
        self.x.XSetErrorHandler.argtypes = [proto]
        holder: Dict[str, Any] = {}
        mine = self.dpy

        def handler(dpy, event):
            if dpy == mine or not holder.get("prev"):
                return 0
            return holder["prev"](dpy, event)
        self._handler = proto(handler)
        prev = self.x.XSetErrorHandler(self._handler)
        holder["prev"] = proto(prev) if prev else None

    def clickthrough(self) -> bool:
        if not self.input_ok:
            return False
        for w_ in self.windows:
            self.e.XShapeCombineRectangles(self.dpy, w_, 2, 0, 0, None, 0, 0, 0)   # ShapeInput, empty, ShapeSet
        self.x.XFlush(self.dpy)
        return True

    def _pts(self, flat: Sequence[float]):
        n = len(flat) // 2
        arr = (self.XPoint * n)()
        for i in range(n):
            arr[i].x = int(round(max(-30000, min(30000, flat[2 * i]))))
            arr[i].y = int(round(max(-30000, min(30000, flat[2 * i + 1]))))
        return arr, n

    def apply(self, prims: Sequence[Prim]) -> None:
        x, d, pm, gc = self.x, self.dpy, self.pm, self.gc
        x.XSetForeground(d, gc, 0)
        x.XFillRectangle(d, pm, gc, 0, 0, self.w, self.h)
        x.XSetForeground(d, gc, 1)
        for p in prims:
            kind = p[0]
            if kind == "oval":
                _, cx, cy, r, fill, outline, width = p
                bx, by, bw = int(round(cx - r)), int(round(cy - r)), max(1, int(round(2 * r)))
                if fill is not None:
                    x.XFillArc(d, pm, gc, bx, by, bw, bw, 0, 360 * 64)
                if outline is not None and width > 0:
                    x.XSetLineAttributes(d, gc, max(1, int(round(width))), 0, 2, 1)
                    x.XDrawArc(d, pm, gc, bx, by, bw, bw, 0, 360 * 64)
            elif kind == "arc":
                _, cx, cy, r, start, extent, _c, width = p
                bx, by, bw = int(round(cx - r)), int(round(cy - r)), max(1, int(round(2 * r)))
                x.XSetLineAttributes(d, gc, max(1, int(round(width))), 0, 1, 1)
                x.XDrawArc(d, pm, gc, bx, by, bw, bw, int(start * 64), int(extent * 64))
            elif kind == "poly":
                arr, n = self._pts(p[1])
                if n >= 3:
                    x.XFillPolygon(d, pm, gc, arr, n, 0, 0)
                if p[3] is not None and p[4] > 0:
                    arr2, n2 = self._pts(list(p[1]) + list(p[1][:2]))
                    x.XSetLineAttributes(d, gc, max(1, int(round(p[4]))), 0, 1, 1)
                    x.XDrawLines(d, pm, gc, arr2, n2, 0)
            elif kind == "line":
                arr, n = self._pts(p[1])
                x.XSetLineAttributes(d, gc, max(1, int(round(p[3]))), 0, 2, 1)
                x.XDrawLines(d, pm, gc, arr, n, 0)
            elif kind == "rect":
                _, x0, y0, x1, y1, _c = p
                x.XFillRectangle(d, pm, gc, int(round(x0)), int(round(y0)), max(1, int(round(x1 - x0))),
                                 max(1, int(round(y1 - y0))))
        self.e.XShapeCombineMask(d, self.win, 0, 0, 0, pm, 0)   # ShapeBounding, ShapeSet
        x.XFlush(d)

    def close(self) -> None:
        try:
            self.x.XFreeGC(self.dpy, self.gc)
            self.x.XFreePixmap(self.dpy, self.pm)
            self.x.XCloseDisplay(self.dpy)
        except Exception:
            pass


def _objc():
    import ctypes
    import ctypes.util
    ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AppKit.framework/AppKit")
    objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc") or "/usr/lib/libobjc.A.dylib")
    objc.objc_getClass.restype, objc.objc_getClass.argtypes = ctypes.c_void_p, [ctypes.c_char_p]
    objc.sel_registerName.restype, objc.sel_registerName.argtypes = ctypes.c_void_p, [ctypes.c_char_p]
    return objc


def _mac_frontmost() -> Optional[int]:
    """The app the user is in right now (NSRunningApplication*), captured before Tk starts."""
    import ctypes
    objc = _objc()
    addr = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
    send = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(addr)
    ws = send(objc.objc_getClass(b"NSWorkspace"), objc.sel_registerName(b"sharedWorkspace"))
    app = send(ws, objc.sel_registerName(b"frontmostApplication")) if ws else None
    if app:
        send(app, objc.sel_registerName(b"retain"))
    return app


def _mac_setup(root, give_back: Optional[int] = None) -> bool:
    """Accessory app (never activates), every window ignores the mouse, and the user's app keeps focus."""
    import ctypes
    objc = _objc()
    addr = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
    vp = ctypes.c_void_p
    send = ctypes.CFUNCTYPE(vp, vp, vp)(addr)
    send_l = ctypes.CFUNCTYPE(vp, vp, vp, ctypes.c_long)(addr)
    send_ul = ctypes.CFUNCTYPE(vp, vp, vp, ctypes.c_ulong)(addr)
    send_b = ctypes.CFUNCTYPE(vp, vp, vp, ctypes.c_bool)(addr)
    count = ctypes.CFUNCTYPE(ctypes.c_ulong, vp, vp)(addr)

    def sel(name: str):
        return objc.sel_registerName(name.encode())
    app = send(objc.objc_getClass(b"NSApplication"), sel("sharedApplication"))
    if not app:
        return False
    send_l(app, sel("setActivationPolicy:"), 1)          # NSApplicationActivationPolicyAccessory
    wins = send(app, sel("windows"))
    n = count(wins, sel("count")) if wins else 0
    done = False
    for i in range(int(n)):
        w = send_ul(wins, sel("objectAtIndex:"), i)
        if not w:
            continue
        send_b(w, sel("setIgnoresMouseEvents:"), True)
        send_b(w, sel("setHasShadow:"), False)
        send_ul(w, sel("setCollectionBehavior:"), 1 | 16 | 256)   # all spaces, stationary, fullscreen aux
        send_l(w, sel("setLevel:"), 1000)                          # screen saver level
        done = True
    if give_back:
        send(app, sel("deactivate"))
        send_ul(give_back, sel("activateWithOptions:"), 2)       # NSApplicationActivateIgnoringOtherApps
    return done


def _win_setup(root) -> bool:
    import ctypes
    u = ctypes.WinDLL("user32")  # type: ignore[attr-defined]
    u.GetParent.restype, u.GetParent.argtypes = ctypes.c_void_p, [ctypes.c_void_p]
    get = getattr(u, "GetWindowLongPtrW", None) or u.GetWindowLongW
    put = getattr(u, "SetWindowLongPtrW", None) or u.SetWindowLongW
    get.restype, get.argtypes = ctypes.c_ssize_t, [ctypes.c_void_p, ctypes.c_int]
    put.restype, put.argtypes = ctypes.c_ssize_t, [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
    u.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_uint]
    hwnd = u.GetParent(root.winfo_id()) or root.winfo_id()
    ex = get(hwnd, -20)
    put(hwnd, -20, ex | 0x80000 | 0x20 | 0x80 | 0x08000000)   # LAYERED TRANSPARENT TOOLWINDOW NOACTIVATE
    u.SetWindowPos(hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0020)  # TOPMOST, no move/size/activate
    return True


# ---------------------------------------------------------------------------
# Process
# ---------------------------------------------------------------------------

class Overlay:
    def __init__(self) -> None:
        import tkinter as tk
        import tkinter.font as tkfont
        self.tk = tk
        self.ws = ""
        self.front: Optional[int] = None
        if sys.platform.startswith("win"):
            try:
                import ctypes
                from pulsar.pilot.windows import set_dpi_aware
                set_dpi_aware(ctypes.WinDLL("user32"))  # type: ignore[attr-defined]  # same pixels as the backend
            except Exception:
                pass
        elif sys.platform == "darwin":
            try:
                self.front = _mac_frontmost()
            except Exception:
                self.front = None
        root = self.root = tk.Tk()
        root.withdraw()
        self.ws = str(root.tk.call("tk", "windowingsystem"))
        self.w, self.h = root.winfo_screenwidth(), root.winfo_screenheight()
        if os.environ.get("PULSAR_PILOT_OVERLAY_GEOM"):
            gw, gh = os.environ["PULSAR_PILOT_OVERLAY_GEOM"].lower().split("x")
            self.w, self.h = int(gw), int(gh)
        k = 1.0
        if self.ws != "aqua":
            try:
                k = max(1.0, min(3.0, float(root.winfo_fpixels("1i")) / 96.0))
            except Exception:
                k = 1.0
        if os.environ.get("PULSAR_PILOT_OVERLAY_SCALE"):
            k = float(os.environ["PULSAR_PILOT_OVERLAY_SCALE"])
        root.overrideredirect(True)
        root.geometry("%dx%d+0+0" % (self.w, self.h))
        bg = "#0a0911"
        if self.ws == "aqua":
            try:
                root.attributes("-transparent", True)
                bg = "systemTransparent"
            except tk.TclError:
                pass
        elif self.ws == "win32":
            bg = CHROMA_KEY
            try:
                root.attributes("-transparentcolor", CHROMA_KEY)
            except tk.TclError:
                pass
        root.configure(bg=bg)
        try:
            root.attributes("-topmost", True)
        except tk.TclError:
            pass
        self.canvas = tk.Canvas(root, width=self.w, height=self.h, bg=bg, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        families = set(f.lower() for f in tkfont.families(root))
        fam = next((f for f in ("JetBrains Mono", "SF Mono", "Menlo", "Cascadia Mono", "Consolas", "Segoe UI",
                                "DejaVu Sans Mono", "Liberation Mono", "Helvetica", "helvetica", "courier")
                    if f.lower() in families), "TkDefaultFont")
        self.family = fam
        self.fonts: Dict[Tuple[int, bool], Any] = {}
        self.tkfont = tkfont
        reduced = os.environ.get("PULSAR_REDUCED_MOTION") == "1"
        self.scene = Scene(self.w, self.h, k, reduced=reduced, measure=self.measure)
        self.mask: Optional[XShapeMask] = None
        self.clickthrough = False
        root.update_idletasks()
        if self.ws == "x11":
            try:
                frame = int(root.wm_frame(), 16)
                self.mask = XShapeMask(frame, self.w, self.h, extra_windows=(root.winfo_id(), self.canvas.winfo_id()))
                self.clickthrough = self.mask.input_ok
                self.mask.apply([])
            except Exception as exc:
                raise RuntimeError("X11 shape setup failed: %s" % exc)
        elif self.ws == "aqua":
            # Map once while still empty (fully transparent) to configure the NSWindow; if the
            # window cannot be made click-through it must never sit on screen while idle.
            root.deiconify()
            root.update()
            try:
                self.clickthrough = _mac_setup(root, give_back=self.front)
            except Exception:
                self.clickthrough = False
            root.withdraw()
            root.update()
        elif self.ws == "win32":
            root.deiconify()
            root.update()
            try:
                self.clickthrough = _win_setup(root)
            except Exception:
                self.clickthrough = False
            root.withdraw()
            root.update()
        if not self.clickthrough:
            self.scene.idle_hide = 1.5
        self.queue: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
        self.acks: List[Tuple[float, Any, bool]] = []
        self.shown = False
        self.last_lift = 0.0
        self.t0 = time.monotonic()
        self.out_lock = threading.Lock()
        self.alive = True

    def font(self, size: int, bold: bool):
        key = (size, bold)
        f = self.fonts.get(key)
        if f is None:
            f = self.tkfont.Font(root=self.root, family=self.family, size=-max(8, size),
                                 weight="bold" if bold else "normal")
            self.fonts[key] = f
        return f

    def measure(self, s: str, size: int, bold: bool) -> int:
        return int(self.font(size, bold).measure(s))

    # -- io ----------------------------------------------------------------------------
    def emit(self, msg: Dict[str, Any]) -> None:
        with self.out_lock:
            try:
                sys.stdout.write(json.dumps(msg) + "\n")
                sys.stdout.flush()
            except (OSError, ValueError):
                self.alive = False

    def reader(self) -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict):
                self.queue.put(msg)
        self.queue.put(None)

    # -- loop ------------------------------------------------------------------------------
    def _set_shown(self, on: bool) -> None:
        if on == self.shown:
            return
        self.shown = on
        if on:
            self.root.deiconify()
            self.root.lift()
            self.root.update_idletasks()
            if self.ws == "aqua":
                try:
                    self.clickthrough = _mac_setup(self.root)
                except Exception:
                    self.clickthrough = False
            elif self.mask is not None:
                self._retarget_mask()
                self.mask.clickthrough()
        else:
            self.canvas.delete("all")
            if self.mask is not None:
                self.mask.apply([])
            self.root.withdraw()
        self.root.update()

    def _retarget_mask(self) -> None:
        """Tk may create the wrapper window on first map; shape whatever is outermost now."""
        assert self.mask is not None
        try:
            frame = int(self.root.wm_frame(), 16)
        except (ValueError, self.tk.TclError):
            return
        self.mask.retarget(frame, (self.root.winfo_id(), self.canvas.winfo_id()))

    def tick(self) -> None:
        delay = 60
        try:
            delay = self._tick()
        except Exception as exc:   # a bad frame must never freeze the cursor on screen
            sys.stderr.write("pulsar overlay: %s: %s\n" % (type(exc).__name__, exc))
            try:
                self.scene.apply({"op": "hide"}, time.monotonic() - self.t0)
                self._set_shown(False)
            except Exception:
                pass
        if not self.alive:
            try:
                self.root.destroy()
            except Exception:
                pass
            return
        self.root.after(delay, self.tick)

    def _tick(self) -> int:
        if not self.alive:
            return 0
        t = time.monotonic() - self.t0
        hide_now = False
        while True:
            try:
                msg = self.queue.get_nowait()
            except queue.Empty:
                break
            if msg is None or msg.get("op") == "quit":
                self.alive = False
                break
            try:
                due = self.scene.apply(msg, t)
            except Exception:
                due = 0.0
            if msg.get("op") == "hide":
                hide_now = True
            if "ack" in msg:
                self.acks.append((t + due, msg.get("ack"), bool(msg.get("hide"))))
        if not self.alive:
            return 0
        if hide_now:
            was = self.shown
            self._set_shown(False)
            for _due, aid, _hide in self.acks:   # everything pending is settled once we are off screen
                self.emit({"ack": aid, "visible": False, "was_visible": was, "clickthrough": self.clickthrough})
            self.acks = []
        prims = self.scene.frame(t) if self.scene.visible else []
        if self.scene.visible and not self.shown:
            self._set_shown(True)
        self.draw(prims)
        if not self.scene.visible and self.shown:
            self._set_shown(False)
        # Other windows can stack above an override-redirect window on X11 (and a topmost one on Windows), so
        # it is raised again every second. Not on macOS: the screen-saver window level keeps it on top there,
        # and raising a window can make its app the active one, taking the keyboard focus from the user.
        if self.shown and self.ws != "aqua" and t - self.last_lift > 1.0:
            self.root.lift()
            self.last_lift = t
        ready = [a for a in self.acks if a[0] <= t]
        if ready:
            self.acks = [a for a in self.acks if a[0] > t]
            for _due, aid, hide in ready:
                if hide:   # caller is about to send real input and we are not click-through
                    self.scene.apply({"op": "hide"}, t)
                    self._set_shown(False)
                self.emit({"ack": aid, "visible": self.shown, "clickthrough": self.clickthrough})
        if self.scene.busy(t):
            return 16
        if self.shown:
            return 40
        if t - self.scene.last > IDLE_EXIT and not self.acks:
            self.alive = False      # the caller respawns us on the next action
        return 120

    def draw(self, prims: Sequence[Prim]) -> None:
        c = self.canvas
        c.delete("all")
        for p in prims:
            kind = p[0]
            if kind == "oval":
                _, cx, cy, r, fill, outline, width = p
                c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=hexc(fill) if fill is not None else "",
                              outline=hexc(outline) if outline is not None else "", width=width if outline else 0)
            elif kind == "arc":
                _, cx, cy, r, start, extent, col, width = p
                c.create_arc(cx - r, cy - r, cx + r, cy + r, start=start, extent=extent, style="arc",
                             outline=hexc(col), width=width)
            elif kind == "poly":
                _, pts, fill, outline, width = p
                c.create_polygon(*pts, fill=hexc(fill), outline=hexc(outline) if outline is not None else "",
                                 width=width if outline is not None else 0)
            elif kind == "line":
                _, pts, col, width = p
                c.create_line(*pts, fill=hexc(col), width=width, capstyle="round", joinstyle="round")
            elif kind == "rect":
                _, x0, y0, x1, y1, col = p
                c.create_rectangle(x0, y0, x1, y1, fill=hexc(col), outline="", width=0)
            elif kind == "text":
                _, tx, ty, s, col, size, bold, anchor = p
                c.create_text(tx, ty, text=s, fill=hexc(col), font=self.font(size, bold), anchor=anchor)
        if self.mask is not None and self.shown:
            self.mask.apply(prims)
        self.root.update_idletasks()

    def run(self) -> None:
        threading.Thread(target=self.reader, daemon=True).start()
        self.emit({"hello": 1, "ok": True, "clickthrough": self.clickthrough,
                   "platform": self.ws, "w": self.w, "h": self.h, "k": self.scene.k})
        self.root.after(10, self.tick)
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            pass
        if self.mask is not None:
            self.mask.close()


def probe() -> Dict[str, Any]:
    """Can an overlay run here? (imports tkinter and opens the display, shows nothing)"""
    try:
        import tkinter as tk
    except Exception as exc:
        return {"ok": False, "error": "no tkinter (%s)" % type(exc).__name__}
    try:
        r = tk.Tk()
        r.withdraw()
        ws = str(r.tk.call("tk", "windowingsystem"))
        ver = str(r.tk.call("info", "patchlevel"))
        r.destroy()
    except Exception as exc:
        return {"ok": False, "error": "cannot open a window: %s" % str(exc).split("\n")[0][:120]}
    if ws == "x11":
        try:
            import ctypes
            import ctypes.util
            ctypes.CDLL(ctypes.util.find_library("Xext") or "libXext.so.6").XShapeCombineMask
        except Exception:
            return {"ok": False, "error": "libXext (SHAPE) missing"}
    return {"ok": True, "platform": ws, "tk": ver}


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--probe" in argv:
        sys.stdout.write(json.dumps(probe()) + "\n")
        return 0
    try:
        ov = Overlay()
    except Exception as exc:
        sys.stdout.write(json.dumps({"hello": 1, "ok": False, "error": str(exc).split("\n")[0][:200]}) + "\n")
        sys.stdout.flush()
        return 1
    ov.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
