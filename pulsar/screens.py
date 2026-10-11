# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""The four Pulsar Code screens: BOOT, UPLINK, PICKER, WARP.

Each screen has an animated full-screen version (alternate buffer, 30 fps,
diff-rendered) and a plain fallback for pipes, NO_COLOR and
PULSAR_FX=plain (PULSAR_FX=off and CI keep the screens and stop the motion). Nothing here blocks on the network: slow work runs in
threads while the frame loop keeps the terminal alive.
"""

from __future__ import annotations

import math
import os
import re
import random
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from pulsar import TAGLINE, __version__
from pulsar import catalog as C
from pulsar import fx
from pulsar import providers as P
from pulsar.fx import ASH, BAD, BEAM, DUST, EMBER, FLARE, ICE, NIGHT, OK, PLASMA, SPECTRUM, WARN, Canvas
from pulsar.term import FX_OFF, Caps, Keyboard

FPS = 30

# Provider profiles that work when a matching key is pasted but are not shown in the uplink's
# WORKS WITH row or in the generated provider lists.
UNADVERTISED = frozenset({"claude"})


def advertised_providers() -> List[P.Provider]:
    """The provider profiles Pulsar Code names in public lists, in catalogue order."""
    return [p for p in P.PROVIDERS if p.id not in UNADVERTISED]


def key_choices(det: P.Detection, everything: bool = False) -> List[P.Provider]:
    """The 'which provider issued this key?' list: advertised, key-taking providers only.

    Likely candidates come first; with everything=True the other advertised keyed providers
    follow. Unadvertised profiles still work when detection is certain, since that path skips this list."""
    keyed = [p for p in advertised_providers() if p.needs_key]
    first = [p for p in det.candidates if p.id not in UNADVERTISED] or keyed
    if not everything:
        return first
    return first + [p for p in keyed if p not in first]


class UI:
    def __init__(self, caps: Optional[Caps] = None) -> None:
        self.caps = caps or Caps()
        self.out = self.caps.out
        self.ink = fx.Ink(self.caps.color)
        self.rng = random.Random(int(os.environ.get("PULSAR_SEED", "0")) or None)
        self.haptics = fx.Haptics(self.out, os.environ.get("PULSAR_HAPTICS", "on").lower())
        self.u = self.caps.unicode

    @property
    def interactive(self) -> bool:
        return self.caps.tty and self.caps.color > 0 and os.environ.get("PULSAR_FX", "auto").lower() != "plain"

    def canvas(self) -> Canvas:
        w, h = self.caps.size
        return Canvas(w, h)

    def say(self, text: str = "", color: Optional[fx.Color] = None, bold: bool = False) -> None:
        self.out.write((self.ink.paint(text, color, bold) if color or bold else text) + "\n")
        self.out.flush()

    def mark(self) -> str:
        return fx.MARK if self.u else "@"


def header(cv: Canvas, ui: UI, section: str, t: float, right: str = "") -> None:
    x = 2
    mk = ui.mark()
    for xx in range(cv.w):               # nothing (stars, sparks) shows through the header row
        cv.put(xx, 0, " ")
    cv.put(x, 0, mk, fx.lerp(BEAM, ICE, 0.5 + 0.5 * (1 if int(t * 2) % 2 else 0) * 0.6), True)
    x = cv.gtext(x + 2, 0, "PULSAR CODE", SPECTRUM, bold=True, offset=t * 0.15)
    if section:
        sec = _fold("·  " + section, ui.u)
        if x + 2 + len(sec) <= cv.w - 1:
            x = cv.text(x + 2, 0, sec, ASH)
    r = _fold(right or ("v" + __version__), ui.u)
    rx = cv.w - len(r) - 2
    if rx > x + 1:                       # never over the title: dropped when it does not fit
        cv.text(rx, 0, r, DUST)
    cv.hline(0, 1, cv.w, "─" if ui.u else "-", NIGHT)


# Footer items kept first when the row is too narrow for all of them (then the rest, in order).
FOOTER_PRIORITY = ("enter", "esc", "ctrl-c")


def fit_hints(hints: Sequence[Tuple[str, str]], width: int, u: bool = True) -> List[Tuple[str, str]]:
    """The footer items that fit in `width` columns, whole (never cut mid-word): enter, esc and ctrl-c
    first, then the others in order. Returned in their original order."""
    sep = 5
    items = [(_fold(k, u), _fold(label, u)) for k, label in hints]
    order = sorted(range(len(items)), key=lambda i: (items[i][0] not in FOOTER_PRIORITY,
                                                     FOOTER_PRIORITY.index(items[i][0])
                                                     if items[i][0] in FOOTER_PRIORITY else i))
    keep: List[int] = []
    used = 0
    for i in order:
        k, label = items[i]
        need = len(k) + 1 + len(label) + (sep if keep else 0)
        if used + need <= width:
            keep.append(i)
            used += need
    return [items[i] for i in sorted(keep)]


def footer(cv: Canvas, hints: Sequence[Tuple[str, str]], ui: UI) -> None:
    y = cv.h - 1
    x = 2
    for i, (k, label) in enumerate(fit_hints(hints, cv.w - 4, ui.u)):
        if i:
            x = cv.text(x, y, "  ·  " if ui.u else "  |  ", NIGHT)
        x = cv.text(x, y, k, BEAM, True)
        x = cv.text(x + 1, y, label, DUST)


def _hard_split(word: str, width: int) -> List[str]:
    """A word longer than `width` in chunks, breaking after '/', '-' or '.' when one is close enough."""
    out: List[str] = []
    while len(word) > width:
        cut = width
        for j in range(width, max(0, width // 2) - 1, -1):
            if word[j - 1] in "/-.":
                cut = j
                break
        out.append(word[:cut])
        word = word[cut:]
    out.append(word)
    return out


def wrap(text: str, width: int) -> List[str]:
    lines: List[str] = []
    width = max(1, width)
    for para in text.split("\n"):
        cur = ""
        for word in para.split(" "):
            if len(word) > width:
                if cur:
                    lines.append(cur)
                parts = _hard_split(word, width)
                lines.extend(parts[:-1])
                cur = parts[-1]
                continue
            if len(cur) + len(word) + (1 if cur else 0) > width:
                if cur:
                    lines.append(cur)
                cur = word
            else:
                cur = cur + (" " if cur else "") + word
        lines.append(cur)
    return lines


def wrap_clip(text: str, width: int, rows: int, u: bool = True) -> List[str]:
    """wrap() limited to `rows` lines; the last one ends with an ellipsis when text was left out."""
    lines = wrap(text, width)
    if len(lines) <= rows:
        return lines
    out = lines[:rows]
    ell = "…" if u else "~"
    last = out[-1][: max(0, width - 1)]
    out[-1] = last + ell
    return out


# ---------------------------------------------------------------------------
# BOOT
# ---------------------------------------------------------------------------

@dataclass
class Task:
    label: str
    fn: Callable[[], Tuple[str, str]]          # -> (status ok|warn|fail, detail)
    status: str = "run"
    detail: str = ""
    done_at: float = 0.0
    thread: Optional[threading.Thread] = None

    def start(self) -> None:
        def run() -> None:
            try:
                self.status, self.detail = self.fn()
            except Exception as exc:  # a preload step must never crash the launcher
                self.status, self.detail = "fail", "%s: %s" % (type(exc).__name__, exc)
            self.done_at = time.time()
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    @property
    def done(self) -> bool:
        return self.status != "run"


STATUS_GLYPH = {"ok": ("✓", OK), "warn": ("!", WARN), "fail": ("✗", BAD)}
STATUS_ASCII = {"ok": "+", "warn": "!", "fail": "x"}


class _Timeline:
    """Every beat of the boot sequence, in seconds since launch. Quick boots compress it.

    first boot  ignition 0-0.4 · rain 0-1.85 · decrypt 0.4-1.26 · burst ~1.26 · beams 1.1+
                shimmer + scanline 1.42-1.97 · SYSTEMS panel 0.3+ · nominal flash from 2.02
    quick boot  the same story in about 0.8 s
    """

    def __init__(self, quick: bool) -> None:
        self.quick = quick
        if quick:
            self.ignite = 0.16
            self.rain_until, self.rain_fade, self.rain_density = 0.28, (0.28, 0.60), 0.32
            self.decrypt, self.decrypt_span, self.jitter = 0.03, 0.24, 0.10
            self.beams = 0.20
            self.shimmer, self.shimmer_dur = 0.36, 0.30
            self.tag, self.tag_cps = 0.20, 120.0
            self.panel, self.panel_in, self.row_gap = 0.0, 0.10, 0.03
            self.min_t = 0.52
            self.flash = 0.24
            self.glitches: Tuple[Tuple[float, float, float], ...] = ((0.08, 0.06, 0.8), (0.24, 0.05, 0.6))
            self.burst, self.burst_life, self.tau = 70, (0.3, 0.7), 0.22
        else:
            self.ignite = 0.40
            self.rain_until, self.rain_fade, self.rain_density = 1.10, (1.20, 1.85), 0.62
            self.decrypt, self.decrypt_span, self.jitter = 0.40, 0.66, 0.20
            self.beams = 1.10
            self.shimmer, self.shimmer_dur = 1.42, 0.55
            self.tag, self.tag_cps = 1.22, 80.0
            self.panel, self.panel_in, self.row_gap = 0.30, 0.24, 0.07
            self.min_t = 1.96
            self.flash = 0.42
            self.glitches = ((0.52, 0.08, 1.0), (0.80, 0.06, 0.7), (1.08, 0.09, 1.0), (1.34, 0.05, 0.6))
            self.burst, self.burst_life, self.tau = 170, (0.55, 1.25), 0.45
        self.decrypt_end = self.decrypt + self.decrypt_span + self.jitter


class _Row:
    __slots__ = ("label", "task", "hw", "shown", "done_t")

    def __init__(self, label: str, task: Task, hw: bool = False) -> None:
        self.label = label
        self.task = task
        self.hw = hw
        self.shown = 0.0
        self.done_t: Optional[float] = None


_ASCII_FOLD = {"\u00b7": "-", "\u2022": "*", "\u2026": "~", "\u2192": "->", "\u2190": "<-", "\u2014": "-",
               "\u2013": "-", "\u2713": "+", "\u2717": "x", "\u201c": '"', "\u201d": '"', "\u2018": "'",
               "\u2019": "'", "\u00d7": "x"}


def _fold(s: str, u: bool) -> str:
    """Text for the terminal: unchanged with Unicode, folded to plain ASCII without it."""
    if u or all(ord(c) < 128 for c in s):
        return s
    return "".join(_ASCII_FOLD.get(c, c if ord(c) < 128 else "?") for c in s)


def _clip(s: str, n: int, u: bool) -> str:
    s = _fold(s, u)
    if n <= 0:
        return ""
    if len(s) <= n:
        return s
    return s[: max(0, n - 1)] + ("…" if u else "~")


_HOST_PORT = re.compile(r"^[A-Za-z0-9._:\[\]-]*:\d{1,5}$")


def clip_label(s: str, n: int, u: bool = True) -> str:
    """A provider label clipped to `n` columns. Endpoint labels (host:port) are clipped from the left so
    the port, the only part that tells 127.0.0.1:8000 from 127.0.0.1:8001, always stays: '…0.1:18951'."""
    s = _fold(s, u)
    if n <= 0:
        return ""
    if len(s) <= n:
        return s
    if _HOST_PORT.match(s):
        return ("…" if u else "~") + s[len(s) - (n - 1):] if n > 1 else s[-1:]
    return _clip(s, n, u)


def endpoint_of(url: str) -> str:
    """host[:port] of a base URL, for labels and messages ("" when there is none). Never the userinfo."""
    import urllib.parse
    try:
        parts = urllib.parse.urlsplit((url or "").strip())
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return ""
    if not host:
        return ""
    if ":" in host:
        host = "[" + host + "]"               # IPv6 literal
    return host + (":%d" % port if port else "")


class _Boot:
    """The BOOT v3 frame builder. Pure: frame(w, h, t) -> Canvas, so it is testable without a tty.

    Layers, back to front: pulsar beams, glyph rain, ignition flare, wordmark (decrypt, glitch,
    scanline, shimmer), sparks, tagline, SYSTEMS panel, verdict line, HUD.
    """

    def __init__(self, tasks: Sequence[Task], quick: bool, u: bool, rng: random.Random, level: str = "full",
                 calm: bool = False, hw_task: Optional[Task] = None, hw_box: Optional[Dict[str, Any]] = None,
                 demo: bool = False) -> None:
        self.tl = _Timeline(quick)
        self.demo = demo
        self.quick = quick
        self.u = u
        self.rng = rng
        self.calm = calm
        self.budget = fx.FrameBudget("minimal" if calm else level)
        self.rows = [_Row(t.label, t) for t in tasks]
        if hw_task is not None:
            self.rows.append(_Row(hw_task.label, hw_task, hw=True))
        self.hw_box = hw_box if hw_box is not None else {}
        self._retargeted = False
        tl = self.tl
        self.reveal = fx.reveal_map(u, tl.decrypt, tl.decrypt_span, rng, jitter=tl.jitter)
        self.reveal_items = sorted(self.reveal.items(), key=lambda kv: kv[1])
        self.reveal_end = self.reveal_items[-1][1] if self.reveal_items else tl.decrypt_end
        self.core_cells = [(x, y) for x, y, _, role in fx.logo_cells(u) if role == "core"]
        self.glitch = fx.Glitch(rng, tl.glitches)
        self.sparks = fx.Particles(rng, gravity=30.0 if quick else 26.0)
        self.burst_done = False
        self.burst_t = 0.0
        self.size = (0, 0)
        self.lay: Dict[str, Any] = {}
        self.rain: Optional[fx.Rain] = None
        self.beams: Optional[fx.Beams] = None
        self.last_t: Optional[float] = None
        self.nominal_t: Optional[float] = None
        self.offset = 0.0
        self.skipped = False

    # -- layout -------------------------------------------------------------------
    def _layout(self, w: int, h: int) -> Dict[str, Any]:
        lw, lh = fx.logo_size(self.u)
        n = len(self.rows)
        roomy = 1 if h >= 34 else 0
        L: Dict[str, Any] = {"w": w, "h": h, "lw": lw, "lh": lh, "hud": h >= 10}
        if w >= lw + 6 and h >= lh + n + 9:
            pw = min(84 if w >= 100 else 76, w - 4)
            ph = n + 2
            block = lh + 3 + roomy + ph + 2 + roomy
            top = 1 + max(0, (h - 2 - block) // 2)
            ox = (w - lw) // 2
            tag_y = top + lh + 1
            py = tag_y + 2 + roomy
            L.update(big=True, ox=ox, top=top, logo_box=(ox, top, ox + lw, top + lh), tag_y=tag_y,
                     panel=((w - pw) // 2, py, pw, ph, True), nom_y=py + ph + 1 + roomy,
                     core=(ox + (lw - 1) / 2.0, top + (lh - 2) / 2.0), brand_y=None)
            return L
        avail = h - 2 if L["hud"] else h
        pw = min(76, w - 2)
        boxed = h >= n + 8 and w >= 44
        ph = n + 2 if boxed else n
        show_tag = h >= ph + 8 and w >= 38
        gap = 1 if avail >= ph + 4 else 0
        total = 1 + gap + (2 if show_tag else 0) + ph + (2 if avail >= ph + gap + (2 if show_tag else 0) + 3 else 0)
        y = (1 if L["hud"] else 0) + max(0, (avail - total) // 2)
        brand_y = y
        y += 1 + gap
        tag_y = None
        if show_tag:
            tag_y = y
            y += 2
        py = y
        nom_y = py + ph + 1 if py + ph + 1 <= (h - 2 if L["hud"] else h - 1) else None
        L.update(big=False, ox=0, top=brand_y, logo_box=None, tag_y=tag_y, panel=((w - pw) // 2, py, pw, ph, boxed),
                 nom_y=nom_y, core=(w / 2.0 - 0.5, float(brand_y)), brand_y=brand_y)
        return L

    def _brand(self) -> Tuple[str, str]:
        w = self.lay["w"]
        mk = fx.MARK if self.u else "@"
        if w >= 36:
            return mk + "  P U L S A R", "  C O D E"
        return mk + " PULSAR", " CODE"

    # -- state ----------------------------------------------------------------------
    def skip(self, t: float) -> None:
        """Any key: fast-forward to the settled frame and keep the verdict flash short."""
        if self.skipped:
            return
        self.skipped = True
        self.offset += max(0.0, self.tl.min_t - t)
        self.tl.flash = min(self.tl.flash, 0.16)

    def finished(self, t: float) -> bool:
        if self.nominal_t is None:
            return False
        hold = 0.3 if self.calm else self.tl.flash
        return t - self.nominal_t >= hold

    def all_done(self) -> bool:
        return all(r.task.done for r in self.rows)

    def verdict(self) -> Tuple[str, Tuple[fx.Color, ...]]:
        fails = sum(1 for r in self.rows if r.task.status == "fail")
        warns = sum(1 for r in self.rows if r.task.status == "warn")
        if fails:
            return ("%d SYSTEM%s DOWN" % (fails, "" if fails == 1 else "S")), (BAD, FLARE, BAD)
        if warns:
            return ("SYSTEMS READY  %s  %d ADVISOR%s" % ("·" if self.u else "-", warns, "Y" if warns == 1 else "IES"),
                    (WARN, EMBER, WARN))
        return "ALL SYSTEMS NOMINAL", (OK, BEAM, PLASMA)

    def _update_rows(self, st: float, dt: float) -> None:
        tl = self.tl
        for i, r in enumerate(self.rows):
            appear = 0.0 if self.calm else tl.panel + tl.panel_in * 0.5 + i * tl.row_gap
            if r.task.done and r.done_t is None:
                r.done_t = max(st, appear + (0.0 if self.calm else 0.12))
            if self.calm:
                r.shown = 1.0 if r.task.done else 0.0
                continue
            if st < appear:
                continue
            target = 1.0 if r.task.done else 0.92 * (1.0 - math.exp(-max(0.0, st - appear) / tl.tau))
            r.shown += (target - r.shown) * min(1.0, dt * (14.0 if r.task.done else 8.0))
            if r.task.done and r.shown > 0.995:
                r.shown = 1.0

    # -- frame ----------------------------------------------------------------------
    def frame(self, w: int, h: int, t: float) -> fx.Canvas:
        tl = self.tl
        B = self.budget
        dt = 1.0 / 30 if self.last_t is None else max(0.0, min(0.1, t - self.last_t))
        self.last_t = t
        if (w, h) != self.size:
            self.size = (w, h)
            self.lay = self._layout(w, h)
            dens = tl.rain_density * (1.0 if B.level == "full" else 0.55)
            self.rain = fx.Rain(w, h, self.rng, self.u, density=dens)
            if t > tl.rain_until:
                self.rain.drops = []
            cx, cy = self.lay["core"]
            self.beams = fx.Beams(w, h, cx, cy, step=1 if B.level == "full" else 2)
        hw = self.hw_box.get("hw")
        if hw is not None and not self._retargeted:
            self._retargeted = True
            if not self.calm:
                try:
                    from pulsar import hardware
                    B.retarget(hardware.fx_budget(hw))
                except Exception:
                    pass
        L = self.lay
        u = self.u
        calm = self.calm
        st = t
        cv = fx.Canvas(w, h)
        self._update_rows(st, dt)
        if self.nominal_t is None and self.all_done() and (calm or st >= tl.min_t):
            self.nominal_t = t
            if not calm:
                self.glitch.add(st, 0.07, 0.8)
        nk = 0.0
        if self.nominal_t is not None and not calm:
            nk = max(0.0, 1.0 - (t - self.nominal_t) / max(0.12, tl.flash))
        lb = L["logo_box"]
        in_logo = (lambda x, y: lb[0] - 1 <= x <= lb[2] and lb[1] <= y < lb[3]) if lb else None
        by = L["brand_y"]

        # 1. pulsar beams, behind everything
        if not calm and B.on("beams") and st >= tl.beams and self.beams is not None:
            amt = fx.ease_out((st - tl.beams) / 0.45) * (0.9 + 0.7 * nk)
            self.beams.draw(cv, st - tl.beams + 0.6, amount=amt, unicode=u, wake=B.on("wake"), mask=in_logo)

        # 2. spectrum glyph rain
        if not calm and B.on("rain") and self.rain is not None:
            self.rain.spawning = st < tl.rain_until
            self.rain.update(dt)
            f0, f1 = tl.rain_fade
            fade = 1.0 if st < f0 else max(0.0, 1.0 - (st - f0) / (f1 - f0))
            if fade > 0:
                self.rain.draw(cv, mask=in_logo if st > tl.decrypt + 0.12 else None, fade=fade)

        # 3. beacon ignition: flare, pulse rings, a CRT scanline raking the screen
        cxf, cyf = L["core"]
        cx, cy = int(round(cxf)), int(round(cyf))
        if not calm and st < tl.ignite:
            k = st / tl.ignite
            inten = fx.ease_out(k / 0.18) if k < 0.18 else (1.0 - (k - 0.18) / 0.82) ** 1.4
            reach = max(w, h * 2) * (0.5 if self.quick else 0.72)
            fx.shockwave(cv, cxf, cyf, fx.ease_out(k) * reach, 1.0 - k, u)
            if k > 0.22:
                kk = (k - 0.22) / 0.78
                fx.shockwave(cv, cxf, cyf, fx.ease_out(kk) * reach * 0.62, (1.0 - kk) * 0.85, u, (ICE, FLARE, PLASMA))
            if B.on("flare"):
                fx.flare(cv, cx, cy, inten, u, length=max(3, int(w * 0.5 * fx.ease_out(k * 1.8))))
            if B.on("scanline") and not self.quick:
                fx.scanline(cv, k * (h + 4) - 2, None, BEAM, 0.85 * (1.0 - k), u)

        # 4. the wordmark
        if L["big"]:
            ox, top, lw, lh = L["ox"], L["top"], L["lw"], L["lh"]
            shimmer = None
            if not calm and tl.shimmer <= st <= tl.shimmer + tl.shimmer_dur:
                shimmer = (st - tl.shimmer) / tl.shimmer_dur * (lw + 16) - 8
            glow = 0.0
            if self.burst_done and not calm:
                glow = max(0.0, 1.0 - (st - self.burst_t) / 0.24) * 0.55
            glow = max(glow, nk * 0.45)
            fx.draw_logo(cv, ox, top, u, 0.0 if calm else st, shimmer=shimmer,
                         reveal=None if calm else self.reveal, rng=self.rng, glow=glow,
                         lead=0.14 if self.quick else 0.3)
            if not calm and B.on("particles") and st <= self.reveal_end + 0.05:
                prev = st - dt
                p = 0.2 * B.quality
                for (x, y), tr in self.reveal_items:
                    if tr > st:
                        break
                    if tr > prev and self.rng.random() < p:
                        col = fx.logo_color(x, y, "core", lw, st)[0]
                        a = -math.pi / 2 + self.rng.uniform(-1.4, 1.4)
                        sp = self.rng.uniform(5.0, 15.0)
                        self.sparks.emit(ox + x, top + y, math.cos(a) * sp, math.sin(a) * sp * 0.5,
                                         self.rng.uniform(0.18, 0.45), col)
            if not calm and not self.burst_done and st >= self.reveal_end:
                self._burst(st)
            if not calm and B.on("glitch"):
                self.glitch.apply(cv, (ox - 3, top, ox + lw + 3, top + lh), st, u)
            if not calm and B.on("scanline") and tl.shimmer <= st <= tl.shimmer + tl.shimmer_dur:
                pz = (st - tl.shimmer) / tl.shimmer_dur
                fx.scanline(cv, top - 1 + pz * (lh + 2), (ox - 2, top - 1, ox + lw + 2, top + lh + 1), BEAM,
                            0.8, u, line=True, trail=2.0)
        elif L["brand_y"] is not None:
            a, b = self._brand()
            text = a + b
            bx = max(0, (w - len(text)) // 2)
            by = L["brand_y"]
            cv.clear(bx - 2, by, len(text) + 4, 1)
            if calm:
                cv.gtext(bx, by, a, SPECTRUM, bold=True)
                cv.text(bx + len(a), by, b, ICE, True)
            else:
                fx.decode_text(cv, bx, by, a, st, tl.decrypt, tl.decrypt_span, self.rng, SPECTRUM, True, u)
                fx.decode_text(cv, bx + len(a), by, b, st, tl.decrypt + tl.decrypt_span * 0.7,
                               tl.decrypt_span * 0.4, self.rng, (ICE, ICE), True, u)
                if not self.burst_done and st >= tl.decrypt + tl.decrypt_span * 1.1:
                    self._burst(st)
                if B.on("glitch"):
                    self.glitch.apply(cv, (bx - 2, by, bx + len(text) + 2, by + 1), st, u, ghost=False, scale_by=0.4)

        # 5. sparks (they fly over the backdrop, never over glyphs of the wordmark)
        if not calm:
            self.sparks.update(dt)
            if B.on("particles") or len(self.sparks):
                cells = cv.cells
                if in_logo is not None:   # sparks fly over the backdrop, never over the wordmark's strokes
                    label_y = L["top"] + L["lh"] - 1
                    self.sparks.draw(cv, u, mask=lambda x, y: 0 <= y < h and 0 <= x < w and in_logo(x, y)
                                     and (y == label_y or cells[y][x][0] != " "))
                else:                     # the compact brand line keeps its whole row
                    self.sparks.draw(cv, u, mask=lambda x, y: y == by)

        # 6. tagline
        if L["tag_y"] is not None:
            tag = TAGLINE if u else "any intelligence / one terminal"
            ty = L["tag_y"]
            tx = max(0, (w - len(tag)) // 2)
            cv.clear(tx - 2, ty, len(tag) + 4, 1)
            nch = len(tag) if calm else int(max(0.0, st - tl.tag) * tl.tag_cps)
            cv.text(tx, ty, tag[:nch], ASH)
            if 0 < nch < len(tag) and int(t * 10) % 2 == 0:
                cv.put(tx + nch, ty, "▌" if u else "_", BEAM)

        # 7. SYSTEMS panel
        self._panel(cv, st, t, nk)

        # 8. verdict
        if self.nominal_t is not None and L["nom_y"] is not None:
            self._verdict_line(cv, t - self.nominal_t, nk)

        # 9. HUD
        if L["hud"]:
            mk = fx.MARK if u else "@"
            pulse = 1.0 if calm else 0.5 + 0.5 * math.sin(t * 7.0)
            cv.clear(0, 0, w, 1)
            tc = "" if calm else "T+%.2fs" % max(0.0, (t if self.nominal_t is None else self.nominal_t) - self.offset)
            right = w - len(tc) - 2 if tc else w
            cv.put(2, 0, mk, fx.lerp(BEAM, ICE, pulse * 0.7), True)
            x = cv.text(4, 0, "PULSAR CODE", fx.scale(ASH, 0.85), True)
            if self.demo:
                # The demo marker outranks the section, the version and the timer: canned values must
                # never read as live data, at any width.
                for tag in ("[ " + DEMO_TAG + " ]", DEMO_TAG, "DEMO"):
                    if x + 2 + len(tag) <= w - 1:
                        x = cv.text(x + 2, 0, tag, EMBER, True)
                        break
            sect = ("·" if u else "-") + " BOOT"
            if x + 1 + len(sect) + 2 <= right:
                x = cv.text(x + 1, 0, sect, DUST)
            ver = "v" + __version__
            if x + 2 + len(ver) + 2 <= right:
                x = cv.text(x + 2, 0, ver, NIGHT)
            if tc and right > x + 1:
                cv.text(right, 0, tc, BEAM if self.nominal_t is None else OK)
            cv.clear(0, h - 1, w, 1)
            footer(cv, [("M", "models"), ("K", "key"), ("any key", "skip")], self)
        return cv

    def _burst(self, st: float) -> None:
        self.burst_done = True
        self.burst_t = st
        if self.calm or not self.budget.on("particles"):
            return
        L = self.lay
        tl = self.tl
        n = self.budget.count(tl.burst * (1.0 if L["big"] else 0.45))
        rng = self.rng
        cxf, cyf = L["core"]
        lo, hi = tl.burst_life
        if L["big"]:
            ox, top, lw = L["ox"], L["top"], L["lw"]
            for _ in range(n):
                x, y = rng.choice(self.core_cells)
                X, Y = ox + x, top + y
                a = math.atan2((Y - cyf) * 2.0, X - cxf) + rng.uniform(-0.55, 0.55)
                sp = rng.uniform(12.0, 42.0) * (0.75 if self.quick else 1.0)
                col = fx.logo_color(x, y, "core", lw, st)[0] if rng.random() < 0.8 else ICE
                self.sparks.emit(X, Y, math.cos(a) * sp, math.sin(a) * sp * 0.5 - sp * 0.14, rng.uniform(lo, hi), col)
        else:
            self.sparks.burst(cxf, cyf, n, (8.0, 26.0), SPECTRUM, (lo * 0.8, hi * 0.8), up=0.3)

    def _panel(self, cv: fx.Canvas, st: float, t: float, nk: float) -> None:
        L = self.lay
        tl = self.tl
        u = self.u
        h = L["h"]
        px, py, pw, ph, boxed = L["panel"]
        if not self.calm and st < tl.panel:
            return
        k = 1.0 if self.calm else fx.ease_out((st - tl.panel) / tl.panel_in)
        limit = h - 1 if L["hud"] else h
        if boxed:
            cv.clear(px, py, pw, min(ph, limit - py))
            base = (fx.scale(PLASMA, 0.85), fx.scale(BEAM, 0.75), fx.scale(FLARE, 0.7))
            stops = tuple(fx.lerp(c, OK, nk * 0.8) for c in base) if nk else base
            if self.nominal_t is not None and not nk and self.all_done():
                v = self.verdict()[1]
                stops = (fx.scale(v[0], 0.55), fx.scale(PLASMA, 0.7), fx.scale(BEAM, 0.6))
            cv.gbox(px, py, pw, ph, stops, phase=0.0 if self.calm else st * 0.16, unicode=u,
                    bright=0.75 + 0.25 * nk, draw=k)
            if k > 0.6:
                title = cv.text(px + 2, py, " SYSTEMS ", ICE, True)
                if self.demo:
                    cv.text(title, py, DEMO_TAG + " ", EMBER, True)
                done = sum(1 for r in self.rows if r.task.done)
                cnt = " %d/%d " % (done, len(self.rows))
                cv.text(px + pw - len(cnt) - 2, py, cnt, OK if done == len(self.rows) else DUST)
        else:
            cv.clear(px, py, pw, min(ph, limit - py))
            top_row = 1 if L["hud"] else 0
            if self.demo and not L["hud"] and py - 1 >= top_row:
                cv.clear(px, py - 1, pw, 1)          # no HUD to carry the marker: a DEMO line above the rows
                cv.text(px, py - 1, _clip(DEMO_TAG, pw, u), EMBER, True)
        ix = px + 2 if boxed else px
        iw = pw - 4 if boxed else pw
        label_w = min(16, max([len(r.label) for r in self.rows] + [4]))
        rest = iw - 2 - label_w - 1
        bar_w = 0 if rest < 30 else max(8, min(22, int(rest * 0.3)))
        bx = ix + 2 + label_w + 1
        dx = bx + bar_w + (2 if bar_w else 0)
        dw = ix + iw - dx
        for i, r in enumerate(self.rows):
            ry = py + (1 if boxed else 0) + i
            if ry >= limit or (boxed and ry >= py + ph - 1):
                break
            appear = 0.0 if self.calm else tl.panel + tl.panel_in * 0.5 + i * tl.row_gap
            if st < appear:
                continue
            rk = 1.0 if self.calm else fx.ease_out((st - appear) / 0.14)
            task = r.task
            # A row resolves only once its label has landed (done_t), so checks cascade in order.
            vdone = task.done and (self.calm or r.done_t is None or st >= r.done_t)
            if vdone:
                g, col = STATUS_GLYPH.get(task.status, ("?", WARN))
                if not u:
                    g = STATUS_ASCII.get(task.status, "?")
                pop = 0.0 if self.calm or r.done_t is None else max(0.0, 1.0 - (st - r.done_t) / 0.28)
                cv.put(ix, ry, g, fx.lerp(col, ICE, pop), True)
            else:
                if self.calm:
                    cv.put(ix, ry, "·" if u else ".", fx.grad(SPECTRUM, (i * 0.15) % 1.0), True)
                else:
                    g = fx.SPINNER[int(t * 14 + i) % len(fx.SPINNER)] if u else "-\\|/"[int(t * 10 + i) % 4]
                    cv.put(ix, ry, g, fx.grad(SPECTRUM, (t * 0.7 + i * 0.15) % 1.0), True)
            label = _clip(r.label, label_w, u)
            nl = len(label) if rk >= 1 else int(len(label) * rk + 0.5)
            lcol = ICE if vdone else ASH
            cv.text(ix + 2, ry, label[:nl], fx.lerp(NIGHT, lcol, rk), vdone)
            if bar_w:
                stops: Sequence[fx.Color] = SPECTRUM
                if task.status == "warn":
                    stops = (WARN, EMBER)
                elif task.status == "fail":
                    stops = (BAD, FLARE)
                fl = 0.0
                if vdone and not self.calm and r.done_t is not None:
                    fl = max(0.0, 1.0 - (st - r.done_t) / 0.4) * 0.8
                fx.bar(cv, bx, ry, max(1, int(bar_w * rk)), r.shown, st + i * 0.21, u, stops,
                       shimmer=not self.calm and not vdone, flash=fl, head=not vdone,
                       bright=1.0 if vdone else 0.9)
            if dw > 2:
                if vdone:
                    text = demo_detail(task.detail) if self.demo else task.detail
                    if r.hw and self.hw_box.get("hw") is not None:
                        try:
                            from pulsar import hardware
                            text = hardware.summary(self.hw_box["hw"], dw)
                        except Exception:
                            text = task.detail
                    text = _clip(text, dw, u)
                    nch = len(text) if self.calm or r.done_t is None else max(0, int((st - r.done_t) * 260) + 1)
                    col = (ASH if r.hw else DUST) if task.status == "ok" else STATUS_GLYPH.get(task.status, ("", WARN))[1]
                    cv.text(dx, ry, text[:nch], col)
                elif not self.calm and r.hw:
                    cv.text(dx, ry, "probing" + "." * (1 + int(t * 4) % 3), NIGHT)

    def _verdict_line(self, cv: fx.Canvas, p: float, nk: float) -> None:
        L = self.lay
        w = L["w"]
        y = L["nom_y"]
        text, stops = self.verdict()
        text = _clip(text, w - 2, self.u)
        x = max(0, (w - len(text)) // 2)
        side = max(0, min(18, (w - len(text)) // 2 - 4))
        grow = 1.0 if self.calm else fx.ease_out(p / 0.22)
        lr = int(side * grow)
        cv.clear(x - lr - 3, y, len(text) + 2 * lr + 6, 1)
        rule = "━" if self.u else "="
        for j in range(lr):
            f = 1.0 - j / max(1, side)
            col = fx.scale(fx.grad(stops, j / max(1, side)), 0.25 + 0.75 * f)
            cv.put(x - 2 - j, y, rule, col)
            cv.put(x + len(text) + 1 + j, y, rule, col)
        if self.calm:
            cv.gtext(x, y, text, stops, bold=True)
        else:
            fx.decode_text(cv, x, y, text, p, 0.0, 0.14, self.rng, stops, True, self.u, offset=p * 0.3)
            if nk > 0.5:
                for i in range(len(text)):
                    ch, fg, b = cv.get(x + i, y)
                    if ch != " " and fg is not None:
                        cv.put(x + i, y, ch, fx.lerp(fg, ICE, (nk - 0.5) * 1.2), b)


DEMO_TAG = "DEMO MODE"
_LIVE_WORD = re.compile(r"\blive\b", re.IGNORECASE)


def demo_detail(text: str) -> str:
    """A SYSTEMS detail as demo mode shows it: canned values never claim to be live."""
    return _LIVE_WORD.sub("demo", text or "")


def _demo_invocation() -> bool:
    """True when this process is `pulsarcode demo` (argv[0] is the module, argv[1] the subcommand)."""
    argv = sys.argv[1:]
    return bool(argv) and argv[0] == "demo"


def _boot_mode(ui: UI) -> str:
    if not ui.interactive:
        return "plain"
    if ui.caps.motion:
        return "anim"
    caps = ui.caps
    if getattr(caps, "reduced", False) and getattr(caps, "fx", "auto") not in FX_OFF and not getattr(caps, "ci", False):
        return "calm"
    return "plain"


def _hardware_task(quick: bool, box: Dict[str, Any]) -> Task:
    def fn() -> Tuple[str, str]:
        from pulsar import hardware
        hw = hardware.probe(timeout=0.5 if quick else 1.4)
        box["hw"] = hw
        return "ok", hardware.summary(hw, 64)
    return Task("Hardware", fn)


def _fx_level() -> str:
    try:
        from pulsar import hardware
        return hardware.fx_budget(hardware.peek())
    except Exception:
        return "lite"


def boot(ui: UI, tasks: List[Task], quick: bool = False, demo: Optional[bool] = None) -> str:
    """Splash + real preload. Returns 'go', 'pick', 'key' or 'quit'.

    demo=True labels the screen as demo mode (`pulsarcode demo` replays canned values, so its rows
    must never read as live data). None means: demo mode when the command line is `pulsarcode demo`.
    """
    if demo is None:
        demo = _demo_invocation()
    for task in tasks:
        task.start()
    mode = _boot_mode(ui)
    if mode == "plain":
        return _boot_plain(ui, tasks, demo)
    box: Dict[str, Any] = {}
    hw_task = _hardware_task(quick, box)
    hw_task.start()
    try:
        return _boot_run(ui, tasks, quick, hw_task, box, calm=mode == "calm", demo=demo)
    except KeyboardInterrupt:
        raise
    except Exception:          # an effect bug must never stand between the user and Claude Code
        if os.environ.get("PULSAR_DEBUG") == "1":
            import traceback
            traceback.print_exc()
        return _boot_plain(ui, tasks, demo)


def _boot_run(ui: UI, tasks: List[Task], quick: bool, hw_task: Task, box: Dict[str, Any], calm: bool = False,
              demo: bool = False) -> str:
    b = _Boot(tasks, quick, ui.u, ui.rng, _fx_level(), calm=calm, hw_task=hw_task, hw_box=box, demo=demo)
    intent = "go"
    leaving = False
    t0 = time.time()
    with fx.Screen(ui.out, ui.caps.color) as scr, Keyboard(ui.out) as kb:
        while True:
            b.budget.begin()
            t = time.time() - t0 + b.offset
            w, h = ui.caps.size
            scr.present(b.frame(w, h, t))
            b.budget.end()
            if leaving:
                if all(task.done for task in tasks):
                    break
            elif b.finished(t):
                break
            key = kb.read(0.1 if calm else max(0.004, b.budget.remaining()))
            if key is None:
                continue
            k = key.lower()
            if k in ("ctrl-c", "q", "ctrl-d"):
                intent = "quit"
            elif k == "m":
                intent = "pick"
            elif k == "k":
                intent = "key"
            b.skip(time.time() - t0 + b.offset)
            if intent != "go":
                leaving = True
    return intent


def _boot_plain(ui: UI, tasks: List[Task], demo: bool = False) -> str:
    for task in tasks:
        if task.thread:
            task.thread.join()
    if demo:
        ui.say(ui.ink.gradient(ui.mark() + " PULSAR CODE", bold=True) + ui.ink.paint("  v" + __version__, DUST) +
               ui.ink.paint("  " + DEMO_TAG, EMBER, True))
        for task in tasks:
            ui.say("  " + _fold(task.label, ui.u) + ui.ink.paint("  " + _fold(demo_detail(task.detail), ui.u), DUST))
        return "go"
    if ui.caps.tty:
        ui.say(ui.ink.gradient(ui.mark() + " PULSAR CODE", bold=True) + ui.ink.paint("  v" + __version__, DUST))
        for task in tasks:
            if task.status != "ok":
                g, col = STATUS_GLYPH.get(task.status, ("?", WARN))
                if not ui.u:
                    g = STATUS_ASCII.get(task.status, "?")
                ui.say("  " + ui.ink.paint(g, col, True) + " " + _fold(task.label, ui.u) +
                       ui.ink.paint("  " + _fold(task.detail, ui.u), DUST))
    return "go"


# ---------------------------------------------------------------------------
# UPLINK (connect a provider)
# ---------------------------------------------------------------------------

@dataclass
class Link:
    provider: P.Provider
    key: str
    profile_id: str
    base_url: str = ""
    native: bool = False
    models: List[C.Model] = field(default_factory=list)
    verified: bool = False


def profile_id_for(prov: P.Provider, base_url: str = "") -> str:
    """A registry provider's own id, or custom-<host>[-<port>][-<path>] for an endpoint pasted as a URL.

    The path counts unless it is the usual /v1, so one server's /v1 and /anthropic (or a gateway's
    /openai/v1 and /groq/v1) become two profiles, each with its own key, and ids saved before still match.
    """
    if prov.id != "custom":
        return prov.id
    host, _, path = re.sub(r"^https?://", "", base_url).partition("/")
    pid = "custom-" + re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")
    path = re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-")
    return pid + ("-" + path[:40].strip("-") if path and path != "v1" else "")


_USERINFO = re.compile(r"(?i)^([a-z][a-z0-9+.-]*://)([^/@\s]+)@")


def masked_live(buf: str, u: bool = True) -> str:
    """What the uplink field shows while typing: never a key, not even after a URL (screen shares, recordings).

    `u` False masks with '*' (ASCII terminals)."""
    det = P.detect(buf)
    if det.kind == "cloud" and not det.key:
        return buf                     # "vertex <project> [region]", "bedrock <region>": no secret in it
    dot = "•" if u else "*"
    if det.kind in ("url", "local", "cloud") or buf.lower() in ("ollama", "local", "lmstudio", "lm-studio"):
        url, sep, rest = buf.partition(" ")
        url = _USERINFO.sub(lambda m: m.group(1) + dot * 3 + "@", url)        # https://user:pass@host
        return url + (sep + _mask_token(rest.strip(), u) if rest.strip() else sep)
    return _mask_token(buf, u)


def _mask_token(buf: str, u: bool = True) -> str:
    keep = 0
    for p in P.PROVIDERS:
        for prefix in p.prefixes:
            if buf.startswith(prefix):
                keep = max(keep, len(prefix))
    if "=" in buf and buf.split("=", 1)[0].isupper():
        keep = len(buf.split("=", 1)[0]) + 1
    if keep == 0 and buf.startswith("sk-"):
        keep = 3
    tail = buf[keep:]
    dot = "•" if u else "*"
    return buf[:keep] + dot * max(0, len(tail) - 4) + tail[-4:] if len(tail) > 10 else buf[:keep] + dot * len(tail)


def _local_servers() -> List[Dict[str, Any]]:
    """Model servers answering on this machine's loopback (pulsar.local.discover_local: short timeouts, no
    key, no proxy). [] with PULSAR_OFFLINE=1, for a server this registry does not know, or on any error."""
    if os.environ.get("PULSAR_OFFLINE") == "1":
        return []
    try:
        from pulsar import local
        found = local.discover_local()
    except Exception:
        return []
    return [item for item in found if P.get(str(item.get("provider_id"))) is not None]


def local_server_line(item: Dict[str, Any]) -> str:
    """'Found Ollama on 127.0.0.1:11434 (2 models)' for one discover_local() result."""
    prov = P.get(str(item.get("provider_id")))
    where = re.sub(r"^https?://", "", str(item.get("base_url") or "")).split("/")[0]
    n = len(item.get("models") or [])
    return "Found %s on %s (%d model%s)" % (prov.name if prov else "a model server", P.clean_text(where, 60), n,
                                            "" if n == 1 else "s")


def _local_detection(item: Dict[str, Any]) -> P.Detection:
    prov = P.BY_ID[str(item["provider_id"])]
    return P.Detection("local", candidates=[prov], base_url=str(item["base_url"]))


def handshake_url(prov: P.Provider, key: str = "") -> str:
    """Where the key check lists models (catalog.fetch_live), for the uplink's GET line: an Anthropic Messages
    endpoint lists them beside /v1/messages, the others under their base URL. Providers without a list show
    their base URL."""
    if prov.catalog == "none" or prov.route == "cloud":
        return prov.base_url.rstrip("/")
    if not key and prov.models_url:
        return prov.models_url
    return C._authed(prov, key)[0].split("?", 1)[0]


def probe(link: Link, timeout: float = 12.0) -> Tuple[bool, str, int]:
    """Validate a key by listing models. Only contacts link.provider."""
    try:
        models = C.fetch_live(link.provider, link.profile_id, link.key, timeout=timeout)
    except C.CatalogError as exc:
        return False, str(exc), exc.status
    link.models = models
    link.verified = True
    if link.provider.catalog != "none":
        C.write_cache(link.profile_id, models)
    return True, "%d models online" % len(models), 200


def uplink(ui: UI, env: Optional[Dict[str, str]] = None, reason: str = "") -> Optional[Link]:
    env = env if env is not None else dict(os.environ)
    found = P.env_keys(env)
    if not ui.interactive:
        return _uplink_plain(ui, found)
    rng = ui.rng
    stars = fx.Stars(*ui.caps.size, rng)
    sparks = fx.Particles(rng, gravity=20.0)
    beam_box: Dict[str, Any] = {}
    burst_fired = False
    last_now = time.time()
    buf = ""
    phase = "input"
    message = reason
    msg_color = ASH
    choices: List[P.Provider] = []
    cursor = 0
    link: Optional[Link] = None
    worker: Dict[str, Any] = {}
    t0 = time.time()
    phase_t = t0
    result: Optional[Link] = None
    unopened: List[str] = []          # key pages Ctrl-O could not open in a browser

    free = [p for p in advertised_providers() if p.free]
    works = advertised_providers()
    local_box: Dict[str, Any] = {}           # model servers found on loopback, offered when no key is in the shell
    threading.Thread(target=lambda: local_box.update(found=_local_servers()), daemon=True).start()

    def begin_probe(prov: P.Provider, det: P.Detection) -> None:
        nonlocal phase, link, phase_t
        base = det.base_url if det.base_url else ""
        key = det.key
        if prov.local and not key:
            key = ""
        link = Link(prov, key, profile_id_for(prov, base), base_url=base,
                    native=prov.route == "native" and prov.id == "custom")
        if base and prov.id != "custom":
            from dataclasses import replace
            link.provider = replace(prov, base_url=base)
        worker.clear()

        def run() -> None:
            worker["res"] = probe(link)  # type: ignore[arg-type]
        th = threading.Thread(target=run, daemon=True)
        th.start()
        worker["th"] = th
        phase = "probe"
        phase_t = time.time()

    with fx.Screen(ui.out, ui.caps.color) as scr, Keyboard(ui.out) as kb:
        while True:
            now = time.time()
            t = now - t0
            cw, ch = ui.caps.size
            if (cw, ch) != (stars.w, stars.h):
                stars = fx.Stars(cw, ch, rng)
            cv = Canvas(cw, ch)
            quiet: set = set()            # text rows sparks must not cross
            sky = Canvas(cw, ch)
            stars.draw(sky, t, ui.u)      # copied in at the end, onto rows that hold no text
            header(cv, ui, "UPLINK", t)
            ui.haptics.update(1 / FPS)
            shake = ui.haptics.offset()
            pw = min(cw - 4, 74)
            px = (cw - pw) // 2 + shake
            small = ch < 16 or cw < 60
            y = 2 if ch < 16 else 3 if ch < 26 else 4
            sub_text = "Paste an API key from any provider. Pulsar Code recognises it."
            if small and len(sub_text) > pw:
                sub_text = "Paste any provider's API key."     # one row on small screens, said in full
            sub_lines = wrap_clip(sub_text, pw, 1 if small else 2, ui.u)
            _blank(cv, 0, y, cw, 1 + len(sub_lines))       # no stars inside text rows
            cv.text(px, y, "Connect any intelligence.", ICE, True)
            for i, line in enumerate(sub_lines):
                cv.text(px, y + 1 + i, line, ASH)
            y += 2 + len(sub_lines) if not small else 1 + len(sub_lines)
            det = P.detect(buf)
            badge_color = fx.hex_rgb(det.best.color) if det.best and det.certain else BEAM
            bright = 0.55 + 0.45 * ui.haptics.flash
            if phase == "input" or phase == "choose":
                fx_stops = (fx.lerp(badge_color, ui.haptics.flash_color, ui.haptics.flash), PLASMA, badge_color)
                cv.gbox(px, y, pw, 3, fx_stops, phase=t * 0.25, unicode=ui.u, bright=bright)
            else:
                cv.box(px, y, pw, 3, scale_c(BEAM, 0.6), unicode=ui.u)
            _blank(cv, px + 1, y + 1, pw - 2, 1)           # the input row stays clear of stars
            prompt = "▸ " if ui.u else "> "
            cv.text(px + 2, y + 1, prompt, BEAM, True)
            shown = masked_live(buf, ui.u) if buf else ""
            room = pw - 7
            if len(shown) > room:
                shown = ("…" if ui.u else "~") + shown[-(room - 1):]
            if buf:
                cv.text(px + 4, y + 1, shown, ICE)
            else:
                # The placeholder starts after the cursor cell (as in the picker's search box), so the
                # blinking cursor never covers its first character.
                ph = _fold("sk-…  nvapi-…  AIza…  gsk_…  https://host/v1 key  ollama", ui.u)
                cv.text(px + 6, y + 1, _clip(ph, room - 2, ui.u), NIGHT)
            if phase == "input" and int(t * 2.4) % 2 == 0:
                cv.put(px + 4 + len(shown) if buf else px + 4, y + 1, "▌" if ui.u else "_", BEAM)
            y += 3

            # live detection line
            det_y = y
            det_drawn = False
            if buf and phase == "input":
                if det.kind in ("key", "local", "url") and det.certain:
                    b = det.best
                    assert b is not None
                    x = cv.text(px + 2, y, ui.mark() + " ", fx.hex_rgb(b.color), True)
                    x = cv.text(x, y, _clip(b.name, max(4, pw - 4), ui.u), fx.hex_rgb(b.color), True)
                    x = cv.text(x, y, detection_note(det, b, ui.u, room=max(0, px + pw - x)), ASH)
                    det_drawn = True
                elif det.kind == "key" and det.candidates:
                    cv.text(px + 2, y, "? could be " + ", ".join(p.name for p in det.candidates[:3]) +
                            "  (Enter to choose)", WARN)
                    det_drawn = True
                elif det.kind == "unknown":
                    cv.text(px + 2, y, "? " + (det.note or "unrecognised") + "  (Enter to pick the provider)", WARN)
                    det_drawn = True
            elif not buf and phase == "input" and found:
                p0, var, _ = found[0]
                cv.text(px + 2, y, "Found %s in your shell. Press Enter to use it." % var, fx.hex_rgb(p0.color))
            elif not buf and phase == "input" and local_box.get("found"):
                item = local_box["found"][0]
                cv.text(px + 2, y, _clip(local_server_line(item) + ". Press Enter to use it.", max(4, pw - 2), ui.u),
                        fx.hex_rgb(P.BY_ID[item["provider_id"]].color))
            y += 2

            if phase == "choose":
                cv.text(px, y, "Which provider issued this key?", ICE, True)
                y += 1
                vis = max(3, ch - y - 3)
                start = max(0, min(cursor - vis // 2, len(choices) - vis))
                for i, prov in enumerate(choices[start:start + vis]):
                    idx = start + i
                    sel = idx == cursor
                    col = fx.hex_rgb(prov.color)
                    cv.text(px, y + i, " " * pw)
                    cv.text(px, y + i, ("▸ " if ui.u else "> ") if sel else "  ", BEAM, True)
                    cv.text(px + 2, y + i, ui.mark(), col, True)
                    cv.text(px + 4, y + i, prov.name, ICE if sel else ASH, sel)
                    cv.text(px + 22, y + i, prov.blurb[: max(0, pw - 24)], DUST)
                footer(cv, [("↑↓", "choose"), ("enter", "connect"), ("esc", "back")], ui)
            elif phase == "probe" and link is not None:
                cx = px + 10
                cy = y + 3
                # Narrow terminals stack the handshake under the sonar; when even that leaves no room
                # the sonar is skipped so the status lines stay on screen.
                stacked = cw < 60 or pw < 50
                sonar = not stacked or ch - y >= 13
                ty = (y + 8 if sonar else y) if stacked else y + 1
                ty = max(2, min(ty, ch - 5))
                tx = px if stacked else px + 22
                if not sonar:
                    pass
                elif ui.caps.motion:   # pulsar beams spin up behind the sonar while the handshake runs
                    bkey = (cw, ch, cx, cy)
                    if beam_box.get("key") != bkey:
                        beam_box["key"] = bkey
                        beam_box["beams"] = fx.Beams(cw, ch, cx, cy, radius=14, region=(px, y - 1, px + 21, y + 8))
                    pcol = fx.hex_rgb(link.provider.color)
                    beam_box["beams"].draw(cv, now - phase_t, amount=min(1.0, (now - phase_t) / 0.3), unicode=ui.u,
                                           speed=1.7, colors=((ICE, pcol, PLASMA), (ICE, BEAM, PLASMA)))
                if sonar:
                    fx.sweep(cv, cx, cy, 4, now - phase_t, ui.u, fx.hex_rgb(link.provider.color))
                tw = max(8, px + pw - tx)
                _blank(cv, tx, ty, tw, 4)
                cv.text(tx, ty, "Handshake", ICE, True)
                cv.text(tx + 10, ty, _clip("with " + link.provider.name, max(1, tw - 10), ui.u),
                        fx.hex_rgb(link.provider.color), True)
                dots = "." * (1 + int((now - phase_t) * 3) % 3)
                cv.text(tx, ty + 1, "listing models" + dots, ASH)
                ep = handshake_url(link.provider, link.key)
                cv.text(tx, ty + 2, _clip(("→ GET " if ui.u else "> GET ") + ep, tw, ui.u), DUST)
                # Name the endpoint for a pasted URL ("Custom" says nothing); a long host keeps its port.
                dest = (endpoint_of(link.provider.base_url) if link.provider.id == "custom" else "") or link.provider.short
                cv.text(tx, ty + 3, "only %s gets your key" % clip_label(dest, max(4, tw - 19), ui.u), NIGHT)
                if "res" in worker and now - phase_t > 0.9:
                    ok, detail, status = worker["res"]
                    if ok:
                        ui.haptics.confirm()
                        phase = "done"
                        phase_t = now
                        message = detail
                    else:
                        ui.haptics.error()
                        if status in (401, 403):
                            message, msg_color = "%s rejected that key (%d). Check it and paste again." % (
                                link.provider.name, status), BAD
                            buf = ""
                            phase = "input"
                        else:
                            message, msg_color = "Could not verify: %s" % detail, WARN
                            phase = "unverified"
                            phase_t = now
            elif phase == "unverified" and link is not None:
                if small:
                    y -= 2                        # no detection line in this phase: use its rows
                room = max(2, ch - 1 - y)
                msg_lines = wrap_clip(_fold(message, ui.u), pw - 4, max(1, min(3, room - 2)), ui.u)
                for i, line in enumerate(msg_lines):
                    cv.text(px + 2, y + i, line, WARN)
                hy = y + len(msg_lines) + (1 if room - len(msg_lines) > 2 else 0)
                for i, line in enumerate(wrap_clip("Enter saves it anyway (you can fix it later).  Esc edits the key.",
                                                   pw - 4, max(1, ch - 1 - hy), ui.u)):
                    cv.text(px + 2, hy + i, line, ASH)
                footer(cv, [("enter", "save anyway"), ("esc", "edit")], ui)
            elif phase == "done" and link is not None:
                k = min(1.0, (now - phase_t) / 0.35)
                title = "LINK ESTABLISHED"
                tx = (cw - len(title)) // 2
                y = max(2, min(y, ch - 4))        # title and summary stay above the footer on short screens
                if not burst_fired and ui.caps.motion:
                    burst_fired = True
                    sparks.burst(cw / 2.0, y + 1, 70, (10.0, 32.0), (OK, BEAM, PLASMA, ICE, fx.hex_rgb(link.provider.color)),
                                 (0.4, 0.95), up=0.45)
                sub = _clip("%s  ·  %s" % (link.provider.name, message), cw - 2, ui.u)
                sub_y = min(y + 3, ch - 2)
                # Clear the rows first (no stars or leftovers inside them), then draw: blanking after the
                # title erased it, and _merge_sky then filled the empty row with stars.
                _blank(cv, 0, y + 1, cw, 1)
                _blank(cv, 0, sub_y, cw, 1)
                cv.gtext(tx, y + 1, title[: max(1, int(len(title) * k))], (OK, BEAM, PLASMA), bold=True,
                         offset=(now * 0.4) % 1)
                cv.center(sub_y, sub, fx.hex_rgb(link.provider.color))
                quiet.update((y + 1, sub_y))
                if now - phase_t > 1.0:
                    result = link
                    break
            else:
                if message:
                    # The message takes the detection line's rows when that line is empty (after a rejected
                    # key the input is cleared) and every free row above the footer, so a 401 or a link to
                    # open (Ctrl-O without a browser: the URL is wrapped at '/', never cut) is said in full.
                    text = _fold(message, ui.u)
                    my = y if det_drawn or len(wrap(text, pw - 4)) <= ch - 1 - y else det_y
                    msg_lines = wrap_clip(text, pw - 4, max(1, ch - 1 - my), ui.u)
                    for i, line in enumerate(msg_lines):
                        cv.text(px + 2, my + i, line, msg_color)
                    y = max(y, my + len(msg_lines) + 1)
                if ch - y > 8:
                    cv.text(px, y, "FREE TO START", EMBER, True)
                    x = px
                    yy = y + 1
                    for prov in free:
                        item = "%s %s (%s)" % (ui.mark(), prov.name, prov.free)
                        if x + len(item) + 2 > px + pw:
                            x = px
                            yy += 1
                        if yy >= ch - 2:
                            break
                        cv.put(x, yy, ui.mark(), fx.hex_rgb(prov.color), True)
                        cv.text(x + 2, yy, prov.name, ICE)
                        cv.text(x + 3 + len(prov.name), yy, "(" + prov.free + ")", DUST)
                        x += len(item) + 3
                    y = yy + 2
                if ch - y > 4:
                    cv.text(px, y, "WORKS WITH", PLASMA, True)
                    x, yy = px, y + 1
                    last = ch - 2                      # the footer row is never written over
                    for n, prov in enumerate(works):
                        if x + len(prov.name) + 3 > px + pw:
                            x = px
                            yy += 1
                        if yy > last or (yy == last and n < len(works) - 1 and
                                         x + len(prov.name) + 4 + 8 > px + pw):
                            more = "+%d more" % (len(works) - n)
                            if yy > last:
                                yy, x = last, px + pw - len(more)
                            cv.text(x, yy, more, DUST)
                            break
                        cv.put(x, yy, "•" if ui.u else "*", fx.hex_rgb(prov.color))
                        cv.text(x + 2, yy, prov.name, ASH)
                        x += len(prov.name) + 4
                    if yy + 1 <= last:
                        cv.text(px, yy + 1, _clip("+ any OpenAI- or Anthropic-compatible URL:  https://host/v1 <key>",
                                                  pw, ui.u), DUST)
                key_url = det.best.key_url if det.best and det.certain and det.best.key_url else P.BY_ID["nvidia"].key_url
                footer(cv, [("enter", "connect"), ("ctrl-o", "open " + (P._host_of(key_url) or "the key page")),
                            ("esc", "back"), ("ctrl-c", "quit")], ui)
            _merge_sky(cv, sky)
            if len(sparks):
                sparks.update(max(0.0, min(0.1, now - last_now)))
                cells = cv.cells
                sparks.draw(cv, ui.u, mask=lambda sx, sy: not (0 <= sy < ch and 0 <= sx < cw) or sy in quiet
                            or cells[sy][sx][0] != " ")
            last_now = now
            scr.present(cv)

            key = kb.read(1 / FPS)
            if key is None:
                continue
            if key == "ctrl-c":
                result = None
                break
            if phase == "input":
                if key.startswith("paste:"):
                    buf += key[6:].strip().replace("\n", " ").replace("\r", "")
                    ui.haptics.tap()
                elif len(key) == 1:
                    buf += key
                    ui.haptics.tap()
                elif key == "backspace":
                    buf = buf[:-1]
                    ui.haptics.tap()
                elif key in ("ctrl-u",):
                    buf = ""
                elif key == "ctrl-w":
                    buf = buf.rstrip()
                    buf = buf[: max(0, max(buf.rfind(" "), buf.rfind("-")) )] if buf else ""
                elif key == "ctrl-o":
                    target = det.best.key_url if det.best and det.certain and det.best.key_url else P.BY_ID["nvidia"].key_url
                    message, msg_color = open_link(target)
                    if msg_color == WARN:
                        unopened.append(target)        # repeated on one line after the screen closes
                elif key == "esc":
                    if buf:
                        buf = ""
                    else:
                        result = None
                        break
                elif key == "enter":
                    if not buf and found:
                        buf = found[0][2]
                        det = P.detect(found[0][1] + "=" + found[0][2])
                        begin_probe(found[0][0], det)
                        continue
                    if not buf and local_box.get("found"):
                        det = _local_detection(local_box["found"][0])
                        buf = det.base_url
                        begin_probe(det.candidates[0], det)
                        continue
                    if not buf:
                        ui.haptics.error()
                        message, msg_color = "Paste a key first, or type ollama for local models.", WARN
                        continue
                    det = P.detect(buf)
                    if det.certain and det.best is not None:
                        begin_probe(det.best, det)
                    else:
                        choices = key_choices(det, everything=True)
                        cursor = 0
                        phase = "choose"
            elif phase == "choose":
                if key in ("up", "shift-tab"):
                    cursor = max(0, cursor - 1)
                    ui.haptics.tap()
                elif key in ("down", "tab"):
                    cursor = min(len(choices) - 1, cursor + 1)
                    ui.haptics.tap()
                elif key == "esc":
                    phase = "input"
                elif key == "enter":
                    det = P.detect(buf)
                    begin_probe(choices[cursor], P.Detection("key", key=det.key or buf.strip()))
            elif phase == "unverified":
                if key == "enter" and link is not None:
                    result = link
                    ui.haptics.confirm()
                    break
                if key == "esc":
                    phase = "input"
                    message = ""
            elif phase == "done":
                result = link
                break
    for url in dict.fromkeys(unopened):
        # Wrapped on screen at narrow widths; here it is one line you can copy.
        try:
            ui.out.write("pulsarcode: open this link for a key: %s\n" % url)
            ui.out.flush()
        except (OSError, ValueError):
            pass
    return result


def detection_note(det: P.Detection, prov: P.Provider, u: bool = True, room: int = 0) -> str:
    """The text after the provider name on the uplink's detection line, e.g. ' · OpenAI-compatible ·
    127.0.0.1:18931'. Never repeats 'endpoint'.

    With `room` (columns left on the line) the note fits it: the dialect goes first, then the host is
    clipped from the left so its port stays ('…0.1:18951'); other notes end with an ellipsis."""
    sep = "  ·  " if u else "  -  "
    if det.kind == "key":
        note = "  detected" + (sep + prov.blurb if prov.blurb else "")
        return _clip(note, room, u) if room else note
    parts = []
    if not prov.name.lower().endswith("endpoint"):
        parts.append("endpoint")
    dialect = ""
    if prov.id == "custom":
        dialect = "Anthropic-compatible" if prov.route == "native" else "OpenAI-compatible"
        parts.append(dialect)
    base = det.base_url or prov.base_url
    host = endpoint_of(base) if base else ""
    if host:
        parts.append(host)
    elif prov.blurb:
        parts.append(prov.blurb)
    note = sep + sep.join(parts) if parts else ""
    if not room or len(_fold(note, u)) <= room:
        return note
    if not host:
        return _clip(note, room, u)
    if dialect:
        parts.remove(dialect)                 # the host tells two endpoints apart; the dialect does not
    lead = sep + sep.join(parts[:-1]) + sep if len(parts) > 1 else sep
    if len(_fold(lead, u)) + len(host) > room and len(parts) > 1:
        lead = sep                            # 'endpoint' goes before the host does
    return _fold(lead, u) + clip_label(host, max(1, room - len(_fold(lead, u))), u)


def open_link(url: str) -> Tuple[str, fx.Color]:
    """Open `url` in a browser; (message, colour) that says what really happened."""
    opened = False
    if _has_browser():
        box: Dict[str, bool] = {}

        def run() -> None:
            try:
                box["ok"] = bool(webbrowser.open(url))
            except Exception:
                box["ok"] = False
        th = threading.Thread(target=run, daemon=True)
        th.start()
        th.join(2.0)
        opened = box.get("ok", th.is_alive())     # still starting after 2 s: a browser is coming up
    if opened:
        return "Opened " + url + " in your browser.", BEAM
    return "No browser here; open this link: " + url, WARN


def _has_browser(environ: Optional[Dict[str, str]] = None) -> bool:
    """False where no graphical browser can open (an SSH or console session without $BROWSER): there,
    webbrowser would start a text browser over the TUI or fail."""
    env = os.environ if environ is None else environ
    if env.get("BROWSER"):
        return True
    if sys.platform in ("darwin", "win32") or not hasattr(os, "uname"):
        return True
    if "microsoft" in os.uname().release.lower():          # WSL opens the Windows browser
        return True
    return bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


def _merge_sky(cv: Canvas, sky: Canvas) -> None:
    """Stars only on rows that hold no text, box or footer: never inside a sentence or the input box."""
    for y in range(min(cv.h, sky.h)):
        row = cv.cells[y]
        if any(c[0] != " " for c in row):
            continue
        for x, cell in enumerate(sky.cells[y][: cv.w]):
            if cell[0] != " ":
                row[x] = cell


def _blank(cv: Canvas, x: int, y: int, w: int, h: int) -> None:
    """Clear a rectangle (stars and sparks never show inside text rows)."""
    for yy in range(max(0, y), min(cv.h, y + h)):
        for xx in range(max(0, x), min(cv.w, x + w)):
            cv.put(xx, yy, " ")


def scale_c(c: fx.Color, f: float) -> fx.Color:
    return fx.scale(c, f)


def _uplink_plain(ui: UI, found) -> Optional[Link]:
    import getpass
    if not sys.stdin.isatty():
        ui.say("pulsarcode: no provider connected. Run `pulsarcode key` in a terminal, or export a key such as "
               "NVIDIA_NIM_API_KEY / OPENROUTER_API_KEY / OPENAI_API_KEY.")
        return None
    box: Dict[str, Any] = {}
    finder = threading.Thread(target=lambda: box.update(found=_local_servers()), daemon=True)
    finder.start()
    ui.say("")
    u = getattr(ui, "u", True)
    ui.say(_fold("PULSAR CODE  ·  UPLINK", u), BEAM, True)
    ui.say("Paste an API key from any provider (or a URL, or `ollama`). Free keys:")
    for p in advertised_providers():
        if p.free and not p.local and p.key_url:
            ui.say("  %-14s %s" % (p.name, p.key_url))
    finder.join(2.0)
    local_found = [] if found else box.get("found") or []
    if found:
        ui.say("Found %s in your shell; press Enter to use it." % found[0][1])
    elif local_found:
        ui.say(_fold(local_server_line(local_found[0]), u) + "; press Enter to use it.")
    try:
        raw = getpass.getpass("key > ")
    except (EOFError, KeyboardInterrupt):
        return None
    if raw.strip():
        det = P.detect(raw)
    elif found:
        det = P.detect(found[0][1] + "=" + found[0][2])
    else:
        det = _local_detection(local_found[0]) if local_found else P.Detection("empty")
    if det.kind == "empty":
        return None
    prov = det.best
    if not det.certain:
        options = key_choices(det)
        for i, p in enumerate(options, 1):
            ui.say("  %2d. %s" % (i, p.name))
        try:
            n = int(input("provider number > ").strip())
            prov = options[n - 1]
        except (ValueError, IndexError, EOFError, KeyboardInterrupt):
            return None
    assert prov is not None
    link = Link(prov, det.key, profile_id_for(prov, det.base_url), base_url=det.base_url)
    ui.say("handshake with %s ..." % prov.name)
    ok, detail, status = probe(link)
    ui.say(("  ok  " if ok else "  !!  ") + detail)
    if not ok and status in (401, 403):
        return None
    return link


# ---------------------------------------------------------------------------
# PICKER
# ---------------------------------------------------------------------------

@dataclass
class Source:
    profile_id: str
    provider: P.Provider
    label: str
    key: str
    result: Optional[C.CatalogResult] = None
    thread: Optional[threading.Thread] = None

    def load(self, refresh: bool = False) -> None:
        def run() -> None:
            self.result = C.load_catalog(self.provider, self.profile_id, self.key, refresh=refresh)
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()


@dataclass
class Choice:
    profile_id: str
    model: str
    context: int = 0


def _fmt_ctx(n: int) -> str:
    if not n:
        return ""
    if n >= 1_000_000:
        return "%gM" % round(n / 1_000_000, 1)
    return "%dK" % round(n / 1000)


def picker(ui: UI, sources: List[Source], current: Optional[Tuple[str, str]], recent: Sequence[Tuple[str, str]],
           active: str) -> Any:
    """Returns a Choice, 'uplink' to add a provider, or None to keep current."""
    for s in sources:
        if s.result is None and s.thread is None:
            s.load()
    if not ui.interactive:
        for s in sources:
            if s.thread:
                s.thread.join()
        return _picker_plain(ui, sources, current, recent, active)

    labels = {s.profile_id: s.label for s in sources}
    provs = {s.profile_id: s.provider for s in sources}
    # Profiles that must never be contacted (Azure/Foundry still on the placeholder URL): nothing to pick there,
    # and no "+ use" row; their tab says what to do instead.
    blocked = blocked_profiles(sources)
    query = ""
    filt: Optional[str] = None
    cursor_key: Optional[Tuple[str, str]] = current
    t0 = time.time()
    confirm_at = 0.0
    chosen: Any = None
    rows: List[Tuple[str, Any]] = []
    sig = None
    top = 0
    cur = 0
    tags_w = 0

    def build() -> List[Tuple[str, Any]]:
        models: List[C.Model] = []
        for s in sources:
            if filt and s.profile_id != filt:
                continue
            if s.result and s.profile_id not in blocked:
                models.extend(s.result.models)
        out: List[Tuple[str, Any]] = []
        if query.strip():
            for m in C.search(models, query, labels)[:400]:
                out.append(("model", m))
            target = filt or active
            if not P.valid_model_id(query.strip()):
                out.append(("note", query.strip()))      # never offered as an id: config.load would drop it
            elif target in blocked:
                out.append(("warn", blocked[target]))
            elif target in provs:
                out.append(("custom", (target, query.strip())))
            return out
        for tier, items in C.group(models, provs, recent):
            out.append(("header", tier))
            for m in items:
                out.append(("model", m))
        return out

    with fx.Screen(ui.out, ui.caps.color) as scr, Keyboard(ui.out) as kb:
        while True:
            now = time.time()
            t = now - t0
            ui.haptics.update(1 / FPS)
            new_sig = (query, filt, tuple(id(s.result) for s in sources))
            if new_sig != sig:
                sig = new_sig
                rows = build()
                sel = [i for i, r in enumerate(rows) if r[0] in PICKABLE]
                cur = sel[0] if sel else 0
                tags_w = _tags_width(r[1] for r in rows if r[0] == "model")
                if cursor_key and not query:
                    for i, r in enumerate(rows):
                        if r[0] == "model" and (r[1].profile, r[1].id) == tuple(cursor_key):
                            cur = i
                            break
            cw, ch = ui.caps.size
            cv = Canvas(cw, ch)
            total = sum(len(s.result.models) for s in sources if s.result and s.profile_id not in blocked)
            loading = [s for s in sources if s.result is None]
            header(cv, ui, "MODEL", t, right="%d models  ·  %d uplink%s" % (
                total, len(sources), "" if len(sources) == 1 else "s"))

            # search
            sx = 2
            cv.text(sx, 2, "⌕ " if ui.u else "/ ", BEAM, True)
            if query:
                cv.text(sx + 2, 2, query, ICE, True)
            else:
                cv.text(sx + 4, 2, _clip("type to search every model you can reach", cw - sx - 6, ui.u), NIGHT)
            if int(t * 2.4) % 2 == 0:
                cv.put(sx + 2 + len(query), 2, "▌" if ui.u else "_", BEAM)
            # tabs: they stop before the right edge (scrolled so the selected one shows), and the
            # ctrl-n hint is drawn only where it fits after them
            tabs = [(None, "All")] + [(s.profile_id, clip_label(s.label, 24, ui.u)) for s in sources]
            hint = "+ ctrl-n add"
            tab_end = cw - 2
            first = _tab_start(tabs, filt, tab_end - 2)
            x = 2
            if first:
                x = cv.text(x, 3, "‹ " if ui.u else "< ", DUST)
            for n, (pid, lab) in enumerate(tabs[first:], first):
                if x + len(lab) + 3 > tab_end - (2 if n < len(tabs) - 1 else 0):
                    cv.text(x, 3, "›" if ui.u else ">", DUST)
                    x += 2
                    break
                on = pid == filt
                col = fx.hex_rgb(provs[pid].color) if pid in provs else ICE
                txt = " %s " % lab
                if on:
                    cv.text(x, 3, "[" + lab + "]", col, True)
                else:
                    cv.text(x, 3, txt, DUST)
                if pid in provs and any(s.profile_id == pid and s.result is None for s in sources):
                    cv.put(x + len(txt), 3, fx.SPINNER[int(t * 14) % 10] if ui.u else "*", BEAM)
                x += len(lab) + 4
            if x + 1 + len(hint) <= cw - 2:
                cv.text(x + 1, 3, hint, NIGHT)
            cv.hline(0, 4, cw, "─" if ui.u else "-", NIGHT)
            # the provider column before the tags (tags are the first thing a narrow terminal drops)
            label_w = _label_width(labels.values(), list_w=cw - (38 if cw >= 112 else 0), tags_w=0)

            detail_w = 36 if cw >= 112 else 0
            list_w = cw - detail_w - (2 if detail_w else 0)
            ly0 = 5
            lh = max(3, ch - ly0 - 2)
            if cur < top:
                top = cur
            if cur >= top + lh:
                top = cur - lh + 1
            if top > 0 and rows and rows[top - 1][0] == "header" and cur - (top - 1) < lh:
                top -= 1
            top = max(0, min(top, max(0, len(rows) - lh)))

            if not rows:
                if loading:
                    msg = "scanning %s" % ", ".join(s.label for s in loading)
                    fx.sweep(cv, cw // 2 - len(msg) // 2 - 6, ly0 + 3, 2, t, ui.u)
                    cv.text(cw // 2 - len(msg) // 2, ly0 + 3, msg + "." * (1 + int(t * 3) % 3), ASH)
                elif filt in blocked:
                    cv.center(ly0 + 2, _clip(blocked_hint(provs[filt]), cw - 4, ui.u), WARN)
                else:
                    cv.center(ly0 + 2, _clip("nothing matches. Enter uses your text as a model id." if query else
                                             "no models yet", cw - 4, ui.u), ASH)
            for i in range(lh):
                ri = top + i
                if ri >= len(rows):
                    break
                kind, val = rows[ri]
                yy = ly0 + i
                if kind == "header":
                    title, sub = C.TIER_TITLES[val]
                    xx = cv.gtext(2, yy, title, SPECTRUM if val != C.TIER_RECENT else (BEAM, ICE), bold=True)
                    cv.text(xx + 2, yy, sub, DUST)
                    continue
                sel = ri == cur
                flash = sel and confirm_at and now - confirm_at < 0.4
                if kind == "note":
                    cv.text(4, yy, _clip(not_an_id(val, ui.u), list_w - 6, ui.u), WARN)
                    continue
                if kind == "warn":
                    cv.text(4, yy, _clip(val, list_w - 6, ui.u), WARN)
                    continue
                if kind == "custom":
                    pid, q = val
                    txt = "use “%s” as a model id on %s" % (q, labels.get(pid, pid)) if ui.u else \
                        "use '%s' as a model id on %s" % (q, labels.get(pid, pid))
                    cv.text(4, yy, _clip("+ " + txt, list_w - 6, ui.u), ICE if sel else ASH, sel)
                    if sel:
                        cv.put(1, yy, "▌" if ui.u else ">", fx.grad(SPECTRUM, (t * 0.6) % 1), True)
                    continue
                m: C.Model = val
                is_cur = current is not None and (m.profile, m.id) == tuple(current)
                pcol = fx.hex_rgb(provs[m.profile].color) if m.profile in provs else ICE
                if sel:
                    bar = OK if flash else fx.grad(SPECTRUM, (t * 0.6) % 1)
                    cv.put(1, yy, "▌" if ui.u else ">", bar, True)
                    cv.put(2, yy, "▸" if ui.u else ">", bar, True)
                cv.put(4, yy, "●" if is_cur else ("·" if ui.u else " "), OK if is_cur else pcol, is_cur)
                id_w = _id_width(list_w, label_w, tags_w)
                mid = _clip(m.id, id_w, ui.u)
                if sel:
                    sheen = (t * 30) % (len(mid) + 30) - 10
                    for j, chh in enumerate(mid):
                        d = abs(j - sheen)
                        col = fx.lerp(ICE, BEAM, 0.0 if d > 5 else (1 - d / 5) * 0.8)
                        cv.put(6 + j, yy, chh, OK if flash else col, True)
                else:
                    cv.text(6, yy, mid, ICE if is_cur else ASH)
                meta_x = 6 + id_w + 2
                cv.text(meta_x, yy, clip_label(labels.get(m.profile, m.profile), label_w, ui.u),
                        pcol if sel else fx.scale(pcol, 0.7))
                cv.text(meta_x + label_w + 1, yy, _fmt_ctx(m.context).rjust(5), DUST)
                tx = meta_x + label_w + 8
                for label, col in _row_tags(m):
                    if tx + len(label) > list_w - 2:
                        break
                    cv.text(tx, yy, label, col)
                    tx += len(label) + 1
            if len(rows) > lh:
                frac = top / max(1, len(rows) - lh)
                thumb = ly0 + int(frac * (lh - 1))
                for j in range(lh):
                    cv.put(list_w - 1, ly0 + j, "│" if ui.u else "|", NIGHT)
                cv.put(list_w - 1, thumb, "┃" if ui.u else "#", BEAM)

            # detail pane
            if detail_w and rows and 0 <= cur < len(rows) and rows[cur][0] == "model":
                m = rows[cur][1]
                dx = cw - detail_w - 1
                prov = provs.get(m.profile)
                id_lines = wrap_clip(m.id, detail_w - 4, 3, ui.u)
                box_h = min(lh, len(id_lines) + 4 + (2 if prov else 0) + bool(m.context)
                            + bool(m.reasoning) + bool(m.free) + ("no-tools" in m.tags))
                cv.box(dx, ly0, detail_w, box_h, fx.scale(fx.hex_rgb(prov.color), 0.6) if prov else DUST,
                       unicode=ui.u, fill=True)
                yy = ly0 + 1
                for line in id_lines:
                    cv.text(dx + 2, yy, line, ICE, True)
                    yy += 1
                yy += 1
                if prov:
                    cv.text(dx + 2, yy, prov.name, fx.hex_rgb(prov.color), True)
                    yy += 1
                    cv.text(dx + 2, yy, ("native Anthropic route" if prov.route == "native" else "via loopback adapter"),
                            DUST)
                    yy += 1
                if m.context:
                    cv.text(dx + 2, yy, "context  " + _fmt_ctx(m.context), ASH)
                    yy += 1
                if m.reasoning:
                    cv.text(dx + 2, yy, "reasons before answering", PLASMA)
                    yy += 1
                if m.free:
                    cv.text(dx + 2, yy, "free to use", OK)
                    yy += 1
                if "no-tools" in m.tags:
                    cv.text(dx + 2, yy, "no tool calling: weak agent", BAD)

            footer(cv, [("↑↓" if ui.u else "up/dn", "move"), ("type", "search"), ("tab", "provider"),
                        ("enter", "launch"), ("ctrl-n", "add key"), ("esc", "keep current")], ui)
            scr.present(cv)

            if confirm_at:
                if now - confirm_at > 0.35:
                    break
                time.sleep(1 / FPS)
                continue
            key = kb.read(1 / FPS)
            if key is None:
                continue
            sels = [i for i, r in enumerate(rows) if r[0] in PICKABLE]
            if key == "ctrl-c":
                chosen = None
                break
            if key in ("up", "down", "pgup", "pgdn", "home", "end") and sels:
                pos = sels.index(cur) if cur in sels else 0
                step = {"up": -1, "down": 1, "pgup": -(lh - 2), "pgdn": lh - 2, "home": -10 ** 6, "end": 10 ** 6}[key]
                cur = sels[max(0, min(len(sels) - 1, pos + step))]
                if rows[cur][0] == "model":
                    cursor_key = (rows[cur][1].profile, rows[cur][1].id)
                ui.haptics.tap()
            elif key in ("tab", "shift-tab"):
                order = [None] + [s.profile_id for s in sources]
                i = order.index(filt) if filt in order else 0
                filt = order[(i + (1 if key == "tab" else -1)) % len(order)]
            elif key == "ctrl-n":
                chosen = "uplink"
                break
            elif key == "ctrl-l":
                for s in sources:
                    s.result = None
                    s.load(refresh=True)
            elif key == "esc":
                if query:
                    query = ""
                else:
                    chosen = None
                    break
            elif key == "backspace":
                query = query[:-1]
            elif key == "ctrl-u":
                query = ""
            elif key.startswith("paste:"):
                query += key[6:].strip()
            elif len(key) == 1:
                query += key
            elif key == "enter":
                if not rows or rows[cur][0] not in PICKABLE:
                    if query.strip() and P.valid_model_id(query.strip()) and (filt or active) not in blocked:
                        chosen = Choice(filt or active, query.strip())
                        confirm_at = now
                        ui.haptics.confirm()
                    elif query.strip():
                        ui.haptics.error()            # not a model id: nothing to launch with
                    continue
                kind, val = rows[cur]
                if kind == "custom":
                    chosen = Choice(val[0], val[1])
                else:
                    chosen = Choice(val.profile, val.id, val.context)
                confirm_at = now
                ui.haptics.confirm()
    return chosen


# Picker rows the cursor can stop on (headers and the "not a model id" note cannot be chosen).
PICKABLE = ("model", "custom")
ID_CHARS = "letters, digits . _ / : @ + = , -"


def not_an_id(text: str, u: bool = True) -> str:
    """The picker's answer to search text that cannot be a model id (a space, a quote, ...)."""
    q = P.clean_text(text, 60)
    return ("“%s” is not a model id (%s)" % (q, ID_CHARS)) if u else ("'%s' is not a model id (%s)" % (q, ID_CHARS))


def _label_width(labels: Iterable[str], list_w: int = 80, tags_w: int = 16) -> int:
    """The provider column: as wide as the widest label (8 to 24 columns), never leaving the model id
    column under 16 (the rest is shown with an ellipsis, never cut to a different port).

    tags_w: the width of the tag column (_tags_width; 0 when no row has a tag, so no room is kept for it)."""
    widest = max([len(x) for x in labels] or [8])
    tags = 2 + tags_w if tags_w else 0
    return max(8, min(widest, 24, max(8, list_w - 6 - 2 - 1 - 5 - tags - 2 - 16)))


def _id_width(list_w: int, label_w: int, tags_w: int) -> int:
    """The picker's model id column. A row is: marker (6), id, 2, provider (label_w), 1, context (5), then the
    tags (2 + tags_w) and a column before the scroll bar. The id comes first: it takes the room no row's tags
    need (all of it when the list has no tag at all), keeps 24 columns before tags get any (and all of the
    room when not even "free" would fit after it), never less than 16, and never less than it had when 18
    columns were kept for tags."""
    room = list_w - 6 - 2 - label_w - 1 - 5 - 2
    keep = min(2 + tags_w, 17, room - 24) if tags_w > 0 else 0
    if keep < 2 + len("free"):
        keep = 0
    return max(16, room - keep)


def _row_tags(m: C.Model) -> List[Tuple[str, fx.Color]]:
    """The tags a picker row shows after the context column."""
    tags: List[Tuple[str, fx.Color]] = []
    if m.free:
        tags.append(("free", OK))
    if m.reasoning:
        tags.append(("thinks", PLASMA))
    if "no-tools" in m.tags:
        tags.append(("no tools", BAD))
    return tags


def _tags_width(models: Iterable[C.Model]) -> int:
    """The widest tag text among `models` (0 when none has a tag): the room the picker keeps for tags."""
    widest = 0
    for m in models:
        tags = _row_tags(m)
        if tags:
            widest = max(widest, sum(len(t) for t, _ in tags) + len(tags) - 1)
    return widest


def blocked_profiles(sources: Iterable[Source]) -> Dict[str, str]:
    """{profile id: why} for sources that must never be contacted (catalog.placeholder_problem: an Azure or
    Foundry profile still on the placeholder URL): the picker offers nothing there."""
    out: Dict[str, str] = {}
    for s in sources:
        if C.placeholder_problem(s.provider):
            out[s.profile_id] = blocked_hint(s.provider)
    return out


def blocked_hint(prov: P.Provider) -> str:
    """One line for a provider that has no resource URL saved: what to run instead."""
    return "no resource URL: pulsarcode key %s" % (C.url_example(prov) or "https://<your-resource>...")


def _tab_start(tabs: Sequence[Tuple[Any, str]], filt: Any, room: int) -> int:
    """The first tab to draw so the selected one is visible within `room` columns."""
    idx = next((i for i, (pid, _) in enumerate(tabs) if pid == filt), 0)
    start = 0
    while start < idx and sum(len(lab) + 4 for _, lab in tabs[start:idx + 1]) + 2 > room:
        start += 1
    return start


PLAIN_ROWS = 30


def _picker_plain(ui: UI, sources: List[Source], current, recent, active) -> Any:
    """The picker without the full-screen UI: at most PLAIN_ROWS rows, labelled by provider; text that is
    not a number or a listed id filters the list."""
    provs = {s.profile_id: s.provider for s in sources}
    labels = {s.profile_id: s.label for s in sources}
    blocked = blocked_profiles(sources)
    models: List[C.Model] = []
    for s in sources:
        if s.result and s.profile_id not in blocked:
            models.extend(s.result.models)
    lw = _label_width(labels.values())
    for pid, why in blocked.items():
        ui.say("  ! %s: %s" % (P.clean_text(labels.get(pid, pid), 60), why), WARN)

    def show(query: str) -> List[C.Model]:
        flat: List[C.Model] = []
        groups = ([("", C.search(models, query, labels))] if query else C.group(models, provs, recent))
        for tier, items in groups:
            if len(flat) >= PLAIN_ROWS:
                break
            if tier:
                ui.say("")
                ui.say(C.TIER_TITLES[tier][0], BEAM, True)
            for m in items:
                if len(flat) >= PLAIN_ROWS:
                    break
                flat.append(m)
                marker = "*" if current and (m.profile, m.id) == tuple(current) else " "
                ui.say(" %s %3d. %-56s %s" % (marker, len(flat), P.clean_text(m.id, 120),
                                               clip_label(P.clean_text(labels.get(m.profile, m.profile), 80), lw,
                                                          getattr(ui, "u", True))))
        hidden = (len(C.search(models, query, labels)) if query else len(models)) - len(flat)
        if hidden > 0:
            ui.say("  ... %d more; type part of a name to filter" % hidden, DUST)
        elif query and not flat:
            ui.say("  nothing matches %r" % P.clean_text(query, 60), DUST)
        return flat

    flat = show("")
    if not sys.stdin.isatty():
        return None
    query = ""
    while True:
        try:
            raw = input("number, text to filter, or a model id (Enter keeps current) > ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not raw:
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(flat):
            m = flat[int(raw) - 1]
            return Choice(m.profile, m.id, m.context)
        exact = next((m for m in models if m.id == raw), None)
        if exact is not None:
            return Choice(exact.profile, exact.id, exact.context)
        valid = P.valid_model_id(raw) and active not in blocked
        if raw == query and valid:
            return Choice(active, raw)              # typed twice: use it as an id
        if C.search(models, raw, labels):
            query = raw
            flat = show(query)
            continue
        # Nothing matches: a filter, never saved as the model unless it is an id and typed again.
        if active in blocked and P.valid_model_id(raw):
            ui.say("  no match for %r, and %s cannot be used: %s" % (
                P.clean_text(raw, 60), P.clean_text(labels.get(active, active), 60), blocked[active]), DUST)
        elif not valid:
            ui.say("  no match for %r; it is not a model id either (%s)" % (P.clean_text(raw, 60), ID_CHARS), DUST)
        else:
            ui.say("  no match for %r. Type it again to use it as a model id on %s." % (
                P.clean_text(raw, 60), P.clean_text(labels.get(active, active), 60)), DUST)
        query = raw


# ---------------------------------------------------------------------------
# WARP (launch transition)
# ---------------------------------------------------------------------------

def _logo_streak(cv: Canvas, u: bool, k: float, label: str) -> None:
    """The brand line accelerating into hyperspace: motion-blur streaks grow out of every glyph."""
    y = cv.h // 2
    x0 = (cv.w - len(label)) // 2
    n = max(1, len(label) - 1)
    reach = int((cv.w // 2) * fx.ease_in_out(min(1.0, k / 0.9)))
    rule = "━" if u else "="
    thin = "─" if u else "-"
    for j in range(1, reach + 1):
        f = (1.0 - j / max(1, reach + 1)) ** 1.6
        if f < 0.05:
            break
        left = fx.scale(fx.lerp(EMBER, FLARE, j / max(1, reach)), 0.25 + 0.75 * f)
        right = fx.scale(fx.lerp(BEAM, PLASMA, j / max(1, reach)), 0.25 + 0.75 * f)
        cv.put(x0 - 1 - j, y, rule if f > 0.35 else thin, left, f > 0.7)
        cv.put(x0 + len(label) + j, y, rule if f > 0.35 else thin, right, f > 0.7)
    for i, ch in enumerate(label):
        col = fx.lerp(fx.grad(SPECTRUM, i / n), ICE, min(1.0, k * 1.2))
        cv.put(x0 + i, y, ch, col, True)


def warp(ui: UI, lines: Sequence[Tuple[str, Optional[fx.Color]]], duration: float = 0.55) -> None:
    if ui.interactive and ui.caps.motion and os.environ.get("PULSAR_WARP", "1") != "0":
        try:
            _warp_run(ui, duration)
        except KeyboardInterrupt:
            raise
        except Exception:
            if os.environ.get("PULSAR_DEBUG") == "1":
                import traceback
                traceback.print_exc()
    hud(ui, lines)


def _warp_run(ui: UI, duration: float) -> None:
    level = _fx_level()
    stars = fx.warp_stars(ui.rng, 90 if level == "full" else 50)
    budget = fx.FrameBudget(level, fps=40 if level == "full" else None)
    beams: Optional[fx.Beams] = None
    label = (fx.MARK if ui.u else "@") + "  PULSAR CODE"
    t0 = time.time()
    with fx.Screen(ui.out, ui.caps.color) as scr:
        while True:
            budget.begin()
            t = time.time() - t0
            if t > duration:
                break
            cv = ui.canvas()
            k = t / duration
            if beams is None or (beams.w, beams.h) != (cv.w, cv.h):
                beams = fx.Beams(cv.w, cv.h, cv.w / 2.0 - 0.5, cv.h // 2, step=1 if level == "full" else 2)
            if budget.on("beams") and k < 0.5:
                beams.draw(cv, 1.0 + t * 5.0, amount=1.25 * (1.0 - k / 0.5), unicode=ui.u, wake=budget.on("wake"))
            fx.warp(cv, t, ui.rng, stars, ui.u)
            if k < 0.62:
                _logo_streak(cv, ui.u, k, label)
            else:
                kk = (k - 0.62) / 0.38
                engage = "ENGAGE"
                cv.gtext((cv.w - len(engage)) // 2, cv.h // 2, engage, (fx.scale(ICE, 1 - kk * 0.5), BEAM), bold=True)
                if budget.on("flare"):
                    fx.flare(cv, cv.w // 2, cv.h // 2 + 2, max(0.0, 1.0 - kk) * 0.8, ui.u, length=cv.w // 2,
                             vertical=False)
            scr.present(cv)
            budget.end()
            time.sleep(max(0.0, budget.remaining()))


def hud(ui: UI, lines: Sequence[Tuple[str, Optional[fx.Color]]]) -> None:
    if not ui.caps.tty:
        return
    ui.out.write("\n")
    ui.out.write("  " + ui.ink.paint(ui.mark(), BEAM, True) + " " + ui.ink.gradient("PULSAR CODE", bold=True) +
                 ui.ink.paint("  v" + __version__, DUST) + "\n")
    for text, color in lines:
        ui.out.write("    " + ui.ink.paint("▸ " if ui.u else "> ", FLARE, True) +
                     ui.ink.paint(_fold(text, ui.u), color or ASH) + "\n")
    ui.out.write("\n")
    ui.out.flush()
