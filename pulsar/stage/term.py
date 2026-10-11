# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""`pulsarcode stage`: the animated terminal viewer for Pulsar Stage.

  pulsarcode stage                 full-screen live viewer (follows the newest scene)
  pulsarcode stage --sid SID       only scenes from one session
  pulsarcode stage --web           print the browser URL and open it
  pulsarcode stage --export [P]    write the current scenes to a standalone HTML file
  pulsarcode stage --once          print the current scenes as text and exit

A background thread subscribes to Core's stage stream (reconnecting with
backoff; with no Core it shows an idle beacon and keeps retrying). The UI
thread renders at up to 30 fps through the diff renderer, so it is smooth
over SSH, and degrades to ASCII, 256/16 colors, NO_COLOR, reduced motion
and a plain line mode for pipes (PULSAR_FX=plain). Ctrl-C always restores
the terminal.
"""

from __future__ import annotations

import math
import os
import queue
import random
import shutil
import signal
import sys
import threading
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from pulsar import fx
from pulsar.fx import ASH, BAD, BEAM, DUST, FLARE, ICE, NIGHT, OK, PLASMA, SPECTRUM, WARN, Canvas, Color
from pulsar.stage import render as R

USAGE = """usage: pulsarcode stage [--sid SID] [--web] [--export [PATH]] [--once]

  (no flags)       live, animated stage viewer in this terminal
  --sid SID        only show scenes from one Claude Code session
  --web            print the browser URL for the live web stage and open it
  --export [PATH]  write the current scenes to a standalone HTML file
  --once           print the current scenes as text and exit

keys  ← → space pgup pgdn  slides   home end  first/last   1-9  jump
      [ ]  previous/next scene   ↑ ↓  scroll   f  follow newest   n  notes
      e  export HTML   w  web view   r  replay   ?  help   q  quit
"""

FPS = 30


class Opts:
    def __init__(self) -> None:
        self.sid = ""
        self.web = False
        self.export: Optional[str] = None
        self.do_export = False
        self.once = False
        self.help = False
        self.home = ""


def parse_args(args: List[str]) -> Opts:
    o = Opts()
    it = list(args or [])
    i = 0
    while i < len(it):
        a = it[i]
        if a in ("-h", "--help", "help"):
            o.help = True
        elif a == "--web":
            o.web = True
        elif a == "--once":
            o.once = True
        elif a == "--export":
            o.do_export = True
            if i + 1 < len(it) and not it[i + 1].startswith("-"):
                o.export = it[i + 1]
                i += 1
        elif a.startswith("--export="):
            o.do_export = True
            o.export = a.split("=", 1)[1] or None
        elif a == "--sid":
            if i + 1 >= len(it):
                raise ValueError("--sid needs a session id")
            o.sid = it[i + 1]
            i += 1
        elif a.startswith("--sid="):
            o.sid = a.split("=", 1)[1]
        elif a == "--home":
            if i + 1 >= len(it):
                raise ValueError("--home needs a path")
            o.home = it[i + 1]
            i += 1
        else:
            raise ValueError("unknown option %r" % a)
        i += 1
    o.sid = "".join(c for c in o.sid if c.isalnum() or c in "-_")[:64]
    return o


def cmd_stage(args: list) -> int:
    """Entry point for `pulsarcode stage`."""
    try:
        opts = parse_args(list(args or []))
    except ValueError as exc:
        sys.stderr.write("pulsarcode stage: %s\n\n%s" % (exc, USAGE))
        return 2
    if opts.help:
        sys.stdout.write(USAGE)
        return 0
    if opts.home:
        os.environ["PULSAR_HOME"] = opts.home
    if opts.web:
        return _web(opts.sid)
    if opts.do_export:
        return _export(opts.sid, opts.export)
    from pulsar.term import Caps
    caps = Caps()
    fxmode = os.environ.get("PULSAR_FX", "auto").lower()
    if opts.once or not caps.tty or fxmode == "plain":
        return _plain(opts.sid, once=opts.once)
    return Viewer(caps, opts.sid).run()


# ---------------------------------------------------------------------------
# Core access
# ---------------------------------------------------------------------------

def _clean_sid(sid: Any) -> str:
    """A session id as shown in the header and toasts: letters, digits, - and _ only (no terminal controls)."""
    return "".join(c for c in str(sid or "") if c.isalnum() or c in "-_")[:64]


def _query(sid: str) -> str:
    return ("?sid=" + urllib.parse.quote(sid)) if sid else ""


def web_url(sid: str = "") -> Optional[str]:
    """Read-only browser URL for the live web stage (None when Core is not running)."""
    from pulsar.core import state as ST
    from pulsar.core.client import CoreClient, CoreUnavailable
    info = ST.read_core()
    if info is None:
        return None
    client = CoreClient(info.url, info.admin_token)
    try:
        data = client.get("/pulsar/stage/viewer", timeout=3)
        base = str(data.get("url") or "").split("/stage?", 1)[0] or info.url
    except (CoreUnavailable, AttributeError):
        if not client.proven():
            return None                       # never hand a view token to an unproven listener
        base = info.url
    url = base + "/stage?t=" + ST.view_token(info.admin_token, sid)
    if sid:
        url += "&sid=" + urllib.parse.quote(sid)
    return url


def _web(sid: str) -> int:
    url = web_url(sid)
    if not url:
        sys.stderr.write("pulsarcode stage: Pulsar Core is not running. Start a session with `pulsarcode` first.\n")
        return 1
    print(url)
    from pulsar.stage.launcher import open_browser
    if open_browser(url):
        sys.stderr.write("opened in your browser\n")
    return 0


def _fetch_items(sid: str) -> Optional[List[Dict[str, Any]]]:
    from pulsar.core.client import CoreClient, CoreUnavailable
    client = CoreClient.admin()
    if client is None:
        return None
    try:
        data = client.get("/pulsar/stage/scenes" + _query(sid), timeout=5)
    except CoreUnavailable:
        return None
    items = data.get("scenes") if isinstance(data, dict) else None
    return [i for i in items or [] if isinstance(i, dict)]


def _export(sid: str, path: Optional[str]) -> int:
    items = _fetch_items(sid)
    if items is None:
        sys.stderr.write("pulsarcode stage: Pulsar Core is not running, nothing to export.\n")
        return 1
    if not items:
        sys.stderr.write("pulsarcode stage: the stage is empty, nothing to export yet.\n")
        return 1
    from pulsar.stage.export import write_export
    try:
        out = write_export(items, path)
    except OSError as exc:
        sys.stderr.write("pulsarcode stage: could not write the export: %s\n" % exc)
        return 1
    print(str(out))
    return 0


class Feed:
    """Background subscription to Core's stage stream. Events land on `q` as (event, payload)."""

    def __init__(self, sid: str = "") -> None:
        self.sid = sid
        self.q: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self.state = "connecting"      # connecting | live | offline | nocore
        self.retry_at = 0.0
        self.port = 0
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="pulsar-stage-feed", daemon=True)

    def start(self) -> "Feed":
        self.thread.start()
        return self

    def _run(self) -> None:
        backoff = 0.5
        while not self.stop.is_set():
            try:
                from pulsar.core.client import CoreClient
                client = CoreClient.admin()
            except Exception:
                client = None
            if client is None:
                self.state = "nocore"
            else:
                try:
                    self.port = int(client.base.rsplit(":", 1)[-1])
                except ValueError:
                    self.port = 0
                try:
                    data = client.get("/pulsar/stage/scenes" + _query(self.sid), timeout=3)
                    items = data.get("scenes") if isinstance(data, dict) else None
                    self.q.put(("history", items or []))
                    self.state = "live"
                    backoff = 0.5
                    for ev, payload in client.events("/pulsar/stage/stream" + _query(self.sid), timeout=45):
                        if self.stop.is_set():
                            return
                        self.q.put((ev, payload))
                except Exception:
                    pass
                self.state = "offline"
            self.retry_at = time.time() + backoff
            if self.stop.wait(backoff):
                return
            backoff = min(5.0, backoff * 2)


# ---------------------------------------------------------------------------
# Plain (line) mode
# ---------------------------------------------------------------------------

def elide_path(path: str, width: int, u: bool = True) -> str:
    """`path` in at most `width` cells, cut in the middle so the file name (the last component) stays whole.

    The head keeps as much of the start as fits; only a file name wider than `width` alone is cut, and
    then from its start, so its end (the extension and the timestamp before it) stays visible.
    """
    path = R.prep(path, u)
    if R.swidth(path) <= width:
        return path
    ell = R.ellipsis(u)
    cut = max(path.rfind("/"), path.rfind("\\"))
    head, name = (path[:cut], path[cut:]) if cut >= 0 else ("", path)
    room = width - R.swidth(name) - R.swidth(ell)
    if room < 1:
        tail: List[str] = []
        used = R.swidth(ell)
        for ch in reversed(path if cut < 0 else name.lstrip("/\\")):
            cw = R.cwidth(ch)
            if used + cw > width:
                break
            tail.append(ch)
            used += cw
        return ell + "".join(reversed(tail))
    kept: List[str] = []
    used = 0
    for ch in head:
        cw = R.cwidth(ch)
        if used + cw > room:
            break
        kept.append(ch)
        used += cw
    return "".join(kept) + ell + name


def canvas_lines(cv: Canvas, ink: fx.Ink) -> List[str]:
    """Canvas rows as text (with color codes when ink has depth), trailing blank rows dropped."""
    rows: List[str] = []
    for y in range(cv.h):
        parts: List[str] = []
        style = None
        row = cv.cells[y]
        end = len(row)
        while end > 0 and row[end - 1][0] in (" ", ""):
            end -= 1
        for ch, fg, bold in row[:end]:
            if ch == "":
                continue
            if ink.depth and (fg, bold) != style:
                parts.append(ink.code(fg, bold) if (fg is not None or bold) else "\x1b[0m")
                style = (fg, bold)
            parts.append(ch)
        if ink.depth and style is not None:
            parts.append("\x1b[0m")
        rows.append("".join(parts))
    while rows and not rows[-1].strip():
        rows.pop()
    while rows and not rows[0].strip():
        rows.pop(0)
    return rows


def scene_text(scene: Dict[str, Any], width: int = 100, color: int = 0, u: bool = True) -> str:
    """Render a scene (every slide of a deck) as terminal text."""
    ink = fx.Ink(color)
    out: List[str] = []
    kind = scene.get("kind") or ""
    icon = R.KIND_ICON.get(kind, ("◉", "@"))[0 if u else 1]
    title = scene.get("title") or kind
    head = " %s %s  %s " % (icon, kind.upper(), R.clip(title, max(10, width - 20), u))
    rule = "━" if u else "="
    line = rule * 2 + head + rule * max(2, width - R.swidth(R.prep(head, u)) - 2)
    out.append(ink.gradient(R.prep(line, u), bold=True) if color else R.prep(line, u))
    if kind == "deck":
        slides = scene.get("slides") or []
        for i, sl in enumerate(slides):
            h = R.natural_height(scene, width, sl, u)
            cv = Canvas(width, h)
            R.draw_slide(cv, 0, 0, width, h, sl, u=u, index=i, total=len(slides))
            out.append("")
            tag = " %d / %d " % (i + 1, len(slides))
            sep = ("─" if u else "-") * 3 + tag + ("─" if u else "-") * max(0, width - len(tag) - 3)
            out.append(ink.paint(sep, DUST) if color else sep)
            out.extend(canvas_lines(cv, ink))
    else:
        h = R.natural_height(scene, width, None, u)
        cv = Canvas(width, h)
        R.draw_scene(cv, 0, 0, width, h, scene, u=u)
        out.extend(canvas_lines(cv, ink))
    out.append("")
    return "\n".join(out)


def _plain(sid: str, once: bool = False) -> int:
    from pulsar.stage.schema import SceneError, normalize
    out = sys.stdout
    tty = bool(getattr(out, "isatty", lambda: False)())
    color = 0
    if tty and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb":
        color = 24 if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit") else 8
        if os.environ.get("PULSAR_COLOR") in ("24", "8", "0"):
            color = int(os.environ["PULSAR_COLOR"])
    enc = (getattr(out, "encoding", "") or "").lower()
    u = ("utf" in enc or os.name == "nt") and os.environ.get("PULSAR_ASCII") != "1"
    width = max(40, min(shutil.get_terminal_size((100, 24)).columns, 120))

    def show(item: Dict[str, Any]) -> None:
        sc = item.get("scene") if isinstance(item, dict) else None
        if not isinstance(sc, dict):
            return
        try:
            sc = normalize(sc)
        except (SceneError, TypeError, ValueError):
            return
        try:
            out.write(scene_text(sc, width, color, u) + "\n")
            out.flush()
        except (BrokenPipeError, ValueError):
            raise SystemExit(0)

    if once:
        items = _fetch_items(sid)
        if items is None:
            sys.stderr.write("pulsarcode stage: Pulsar Core is not running.\n")
            return 1
        if not items:
            sys.stderr.write("pulsarcode stage: the stage is empty.\n")
            return 0
        for it in items:
            show(it)
        return 0
    feed = Feed(sid).start()
    mark = "◉" if u else "@"
    sys.stderr.write("%s Pulsar Stage (plain mode): waiting for scenes%s. Ctrl-C to stop.\n" % (
        mark, (" from " + sid) if sid else ""))
    seen: Dict[str, str] = {}
    try:
        while True:
            try:
                ev, payload = feed.q.get(timeout=0.5)
            except queue.Empty:
                continue
            batch = payload if ev == "history" else [payload]
            for it in batch or []:
                if not isinstance(it, dict):
                    continue
                if it.get("op") == "clear" or ev == "clear":
                    sys.stderr.write("(stage cleared)\n")
                    continue
                sc = it.get("scene") or {}
                key = str(sc.get("id"))
                sig = repr(sorted((k, repr(v)) for k, v in sc.items() if k != "at"))
                if seen.get(key) == sig:
                    continue
                seen[key] = sig
                show(it)
    except KeyboardInterrupt:
        return 0
    finally:
        feed.stop.set()


# ---------------------------------------------------------------------------
# The live viewer
# ---------------------------------------------------------------------------

class Starfield:
    """Very dim drifting stars for the margins (never behind content)."""

    def __init__(self, w: int, h: int, rng: random.Random) -> None:
        self.w, self.h = w, h
        self.stars = [(rng.uniform(0, w), rng.randrange(max(1, h)), rng.uniform(0, 6.28), rng.uniform(0.2, 1.0))
                      for _ in range(max(6, w * h // 85))]

    def draw(self, cv: Canvas, oy: int, t: float, u: bool, keep_out: Optional[Tuple[int, int, int, int]]) -> None:
        for x, y, ph, sp in self.stars:
            xx = int((x - t * sp * 0.45) % self.w)
            if keep_out and keep_out[0] - 2 <= xx <= keep_out[2] + 2 and keep_out[1] - 1 <= y <= keep_out[3] + 1:
                continue
            tw = 0.5 + 0.5 * math.sin(t * 1.6 * sp + ph)
            if tw < 0.25:
                continue
            cv.put(xx, oy + y, ("·" if tw < 0.8 else "∙") if u else ".", fx.lerp(NIGHT, DUST, tw))


def _bbox(cv: Canvas) -> Optional[Tuple[int, int, int, int]]:
    x0, y0, x1, y1 = cv.w, cv.h, -1, -1
    for y, row in enumerate(cv.cells):
        for x, cell in enumerate(row):
            if cell[0] != " ":
                if x < x0:
                    x0 = x
                if x > x1:
                    x1 = x
                if y < y0:
                    y0 = y
                y1 = y
    return None if x1 < 0 else (x0, y0, x1, y1)


def blit(dst: Canvas, src: Canvas, dx: int, dy: int, dim: float = 1.0, transparent: bool = True,
         clip: Optional[Tuple[int, int, int, int]] = None) -> None:
    """Copy src onto dst at (dx, dy); blank cells are transparent; clip = (x0, y0, x1, y1) in dst."""
    cx0, cy0, cx1, cy1 = clip or (0, 0, dst.w, dst.h)
    for y in range(src.h):
        ty = y + dy
        if ty < cy0 or ty >= cy1 or ty < 0 or ty >= dst.h:
            continue
        srow = src.cells[y]
        drow = dst.cells[ty]
        lo = max(0, cx0 - dx, -dx)
        hi = min(src.w, cx1 - dx, dst.w - dx)
        for x in range(lo, hi):
            ch, fg, bold = srow[x]
            if transparent and ch == " ":
                continue
            if dim != 1.0 and fg is not None:
                fg = fx.scale(fg, dim)
            drow[x + dx] = (ch, fg, bold)


class Viewer:
    def __init__(self, caps: Any, sid: str = "") -> None:
        self.caps = caps
        self.u = bool(caps.unicode)
        self.motion = bool(caps.motion)
        self.sid = sid
        self.items: List[Dict[str, Any]] = []
        self.cur = -1
        self.slide = 0
        self.scroll = 0
        self.overflow = 0
        self.follow = True
        self.unseen = 0
        self.t0 = time.time()
        self.shown_at = self.t0
        self.trans: Optional[Dict[str, Any]] = None
        self.last_body: Optional[Canvas] = None
        self.toasts: List[List[Any]] = []
        self.help = False
        self.notes = False
        self.digits = ""
        self.digits_at = 0.0
        self.size = (0, 0)
        self.rng = random.Random(int(os.environ.get("PULSAR_SEED", "0")) or None)
        self.stars: Optional[Starfield] = None
        self.haptics = fx.Haptics(caps.out, os.environ.get("PULSAR_HAPTICS", "on").lower())
        self.feed = Feed(sid)
        self.scr: Optional[fx.Screen] = None

    # -- lifecycle -------------------------------------------------------------
    def run(self) -> int:
        from pulsar.term import Keyboard
        self.feed.start()
        restore = self._signals()
        try:
            with fx.Screen(self.caps.out, self.caps.color) as scr, Keyboard(self.caps.out) as kb:
                self.scr = scr
                while True:
                    self._drain()
                    scr.present(self._frame())
                    key = kb.read(1 / FPS if self._busy() else (1 / 12 if self.motion else 0.25))
                    if key is not None and self._key(key) == "quit":
                        break
        except KeyboardInterrupt:
            pass
        finally:
            self.feed.stop.set()
            restore()
        return 0

    def _signals(self):
        prev = {}

        def bail(signum, frame):  # make SIGTERM/SIGHUP unwind through the context managers
            raise KeyboardInterrupt

        if threading.current_thread() is threading.main_thread():
            for name in ("SIGTERM", "SIGHUP"):
                sig = getattr(signal, name, None)
                if sig is not None:
                    try:
                        prev[sig] = signal.signal(sig, bail)
                    except (OSError, ValueError):
                        pass

        def restore() -> None:
            for sig, h in prev.items():
                try:
                    signal.signal(sig, h)
                except (OSError, ValueError):
                    pass
        return restore

    def _busy(self) -> bool:
        """30 fps while something is moving fast; 12 fps for ambient motion once a scene has settled."""
        if self.trans or self.haptics.flash > 0 or self.toasts:
            return True
        if not self.motion:
            return False
        return self.cur < 0 or time.time() - self.shown_at < 1.8

    def _term_size(self) -> Tuple[int, int]:
        try:
            s = os.get_terminal_size(self.caps.out.fileno())
            return max(1, s.columns), max(1, s.lines)
        except (OSError, AttributeError, ValueError):
            return self.caps.size

    # -- scene list ---------------------------------------------------------------
    @property
    def scene(self) -> Optional[Dict[str, Any]]:
        if 0 <= self.cur < len(self.items):
            return self.items[self.cur]["scene"]
        return None

    def _slides(self) -> int:
        sc = self.scene
        if sc and sc.get("kind") == "deck":
            return len(sc.get("slides") or [])
        return 1

    def _norm(self, item: Any) -> Optional[Dict[str, Any]]:
        from pulsar.stage.schema import SceneError, normalize
        if not isinstance(item, dict) or not isinstance(item.get("scene"), dict):
            return None
        try:
            sc = normalize(item["scene"])
        except (SceneError, TypeError, ValueError, KeyError):
            return None
        return {"sid": _clean_sid(item.get("sid")), "scene": sc, "t": item.get("t") or time.time()}

    def _drain(self) -> None:
        for _ in range(200):
            try:
                ev, payload = self.feed.q.get_nowait()
            except queue.Empty:
                return
            try:
                self._event(ev, payload)
            except Exception:
                continue

    def _event(self, ev: str, payload: Any) -> None:
        now = time.time()
        if ev == "history":
            fresh = [x for x in (self._norm(i) for i in payload or []) if x]
            for it in fresh:
                self._upsert(it, announce=False)
            if self.follow and self.items and self.cur != len(self.items) - 1:
                self._goto(len(self.items) - 1, kind="scene")
            return
        if ev == "clear" or (isinstance(payload, dict) and payload.get("op") == "clear"):
            sid = _clean_sid((payload or {}).get("sid"))
            cur_id = self.scene["id"] if self.scene else None
            self.items = [i for i in self.items if i["sid"] != sid] if sid else []
            ids = [i["scene"]["id"] for i in self.items]
            if cur_id in ids:
                self.cur = ids.index(cur_id)
            else:
                self.cur = len(self.items) - 1
                self.slide = 0
                self.shown_at = now
            self._toast("stage cleared" + ((" by " + sid) if sid else ""), DUST)
            return
        if ev == "scene":
            it = self._norm(payload)
            if it:
                self._upsert(it, announce=True)

    def _upsert(self, it: Dict[str, Any], announce: bool) -> None:
        sid_ = it["scene"]["id"]
        for i, old in enumerate(self.items):
            if old["scene"]["id"] == sid_:
                self.items[i] = it
                if i == self.cur:
                    self.slide = min(self.slide, self._slides() - 1)   # live update in place, no re-intro
                return
        self.items.append(it)
        if len(self.items) > 60:
            drop = len(self.items) - 60
            self.items = self.items[drop:]
            self.cur = max(-1, self.cur - drop)
        if not announce:
            return
        if self.follow or self.cur < 0:
            self._goto(len(self.items) - 1, kind="scene")
            self.haptics.tap()
        else:
            self.unseen += 1
            title = it["scene"].get("title") or it["scene"]["kind"]
            self._toast("new: %s   ] to view" % title, BEAM)

    # -- navigation ------------------------------------------------------------------
    def _goto(self, idx: int, slide: int = 0, kind: str = "scene", direction: int = 1) -> None:
        if not self.items:
            return
        idx = max(0, min(idx, len(self.items) - 1))
        if idx == self.cur and slide == self.slide:
            return
        if self.motion and self.last_body is not None and self.cur >= 0:
            self.trans = {"kind": kind, "start": time.time(), "dir": direction, "prev": self.last_body,
                          "dur": 0.32 if kind == "slide" else 0.55, "seed": self.rng.random()}
        if idx >= len(self.items) - 1:
            self.unseen = 0
        self.cur = idx
        self.slide = max(0, min(slide, self._slides() - 1))
        self.scroll = 0
        self.shown_at = time.time()

    def _step(self, d: int) -> None:
        n = self._slides()
        if self.cur < 0:
            return
        if 0 <= self.slide + d < n:
            self._goto(self.cur, self.slide + d, "slide", d)
            self.haptics.tap()
        elif 0 <= self.cur + d < len(self.items) and self.scene and self.scene.get("kind") != "deck":
            self._goto(self.cur + d, 0, "slide", d)
            self.haptics.tap()
        else:
            self.haptics.error()
            self._toast("nothing after this" if d > 0 else "nothing before this", DUST)

    def _key(self, key: str) -> Optional[str]:
        k = key
        if k.startswith("paste:"):
            return None
        if self.help:
            self.help = False
            if k in ("q", "Q", "ctrl-c", "ctrl-d"):
                return "quit"
            return None
        if k in ("q", "Q", "ctrl-c", "ctrl-d"):
            return "quit"
        if k == "esc":
            if self.notes:
                self.notes = False
                return None
            return "quit"
        if k in ("right", " ", "pgdn", "l", "enter"):
            self._step(1)
        elif k in ("left", "pgup", "h", "backspace"):
            self._step(-1)
        elif k in ("down", "j"):
            if self.overflow and self.scroll < 400:
                self.scroll += 1
            else:
                self._step(1)
        elif k in ("up", "k"):
            if self.scroll > 0:
                self.scroll -= 1
            else:
                self._step(-1)
        elif k == "home":
            if self._slides() > 1:
                self._goto(self.cur, 0, "slide", -1)
            elif self.items:
                self._goto(0, 0, "scene", -1)
        elif k == "end":
            if self._slides() > 1:
                self._goto(self.cur, self._slides() - 1, "slide", 1)
            elif self.items:
                self._goto(len(self.items) - 1, 0, "scene", 1)
        elif k in ("]", "tab"):
            if self.cur + 1 < len(self.items):
                self._goto(self.cur + 1, 0, "scene", 1)
                self.haptics.tap()
            else:
                self.haptics.error()
        elif k in ("[", "shift-tab"):
            if self.cur > 0:
                self._goto(self.cur - 1, 0, "scene", -1)
                self.haptics.tap()
            else:
                self.haptics.error()
        elif len(k) == 1 and k.isdigit():
            now = time.time()
            self.digits = (self.digits if now - self.digits_at < 0.8 else "") + k
            self.digits_at = now
            n = int(self.digits)
            total = self._slides()
            if n >= 1 and n * 10 > total:
                self._goto(self.cur, min(n, total) - 1, "slide", 1 if n - 1 >= self.slide else -1)
                self.digits = ""
        elif k == "f":
            self.follow = not self.follow
            self.haptics.confirm()
            if self.follow:
                self._toast("following the newest scene", OK)
                if self.items:
                    self._goto(len(self.items) - 1, 0, "scene")
            else:
                self._toast("pinned: new scenes will not take over", WARN)
        elif k == "n":
            sl = self._slide_obj()
            if sl and sl.get("notes"):
                self.notes = not self.notes
            else:
                self._toast("no speaker notes here", DUST)
        elif k == "?":
            self.help = True
        elif k == "r":
            self.shown_at = time.time()
        elif k == "e":
            self._do_export()
        elif k == "w":
            self._do_web()
        return None

    def _slide_obj(self) -> Optional[Dict[str, Any]]:
        sc = self.scene
        if sc and sc.get("kind") == "deck":
            slides = sc.get("slides") or []
            if 0 <= self.slide < len(slides):
                return slides[self.slide]
        return None

    def _do_export(self) -> None:
        if not self.items:
            self._toast("nothing to export yet", WARN)
            self.haptics.error()
            return
        try:
            from pulsar.stage.export import write_export
            p = write_export(self.items)
            home = os.path.expanduser("~")
            shown = str(p)
            if shown.startswith(home + os.sep):
                shown = "~" + shown[len(home):]
            # the toast holds max(10, width - 10) cells (see _toasts): the path gives way in its middle
            room = max(10, self._term_size()[0] - 10) - len("exported ")
            self._toast("exported " + elide_path(shown, room, self.u), OK)
            self.haptics.confirm()
        except Exception as exc:
            self._toast("export failed: %s" % type(exc).__name__, BAD)
            self.haptics.error()

    def _do_web(self) -> None:
        try:
            url = web_url(self.sid)
        except Exception:
            url = None
        if not url:
            self._toast("Pulsar Core is not running", WARN)
            self.haptics.error()
            return
        from pulsar.stage.launcher import open_browser
        if open_browser(url):
            self._toast("web stage opened in your browser", OK)
            self.haptics.confirm()
            return
        # no GUI here: the link carries a view token, so it never goes to the clipboard (clipboard managers
        # and synced clipboards keep it); `pulsarcode stage --web` prints it where it is wanted
        self._toast("no browser here; run: pulsarcode stage --web", BEAM)

    def _toast(self, text: str, color: Color) -> None:
        self.toasts = [t for t in self.toasts if t[0] != text][-2:]
        self.toasts.append([text, color, time.time()])

    # -- frames ----------------------------------------------------------------------
    def _frame(self) -> Canvas:
        w, h = self._term_size()
        now = time.time()
        if (w, h) != self.size:
            self.size = (w, h)
            self.trans = None
            self.last_body = None
            if self.scr is not None:
                self.scr.invalidate()
            self.stars = Starfield(w, max(1, h - 3), self.rng)
        cv = Canvas(w, h)
        if w < 24 or h < 8:
            v = R.View(cv, 0, 0, w, h, self.u)
            v.center(h // 2, ("◉ " if self.u else "@ ") + "enlarge", BEAM, True)
            return cv
        self.haptics.update(1 / FPS)
        gt = now - self.t0
        top, bh = 2, h - 3
        body = Canvas(w, bh)
        if self.cur < 0 or not self.items:
            self._idle(cv, body, now)
        else:
            self._body(body, now)
        shake = self.haptics.offset() if self.motion else 0
        tr = self.trans
        if tr is not None:
            p = (now - tr["start"]) / tr["dur"]
            if p >= 1 or tr["prev"].w != w or tr["prev"].h != bh:
                self.trans = tr = None
        keep = _bbox(body)
        if self.motion and self.stars is not None:
            self.stars.draw(cv, top, gt, self.u, keep)
        if tr is None:
            blit(cv, body, shake, top, transparent=True, clip=(0, top, w, top + bh))
        else:
            self._compose(cv, body, tr, now, top, bh)
        self.last_body = body
        self._header(cv, now)
        self._rail(cv, now)
        self._footer(cv)
        if self.notes:
            self._notes(cv)
        self._toasts(cv, now)
        if self.help:
            self._help(cv)
        if R.wide_seen():
            R.fix_wide(cv)
        return cv

    def _compose(self, cv: Canvas, body: Canvas, tr: Dict[str, Any], now: float, top: int, bh: int) -> None:
        w = cv.w
        p = max(0.0, min(1.0, (now - tr["start"]) / tr["dur"]))
        prev: Canvas = tr["prev"]
        clip = (0, top, w, top + bh)
        if tr["kind"] == "slide":
            k = R.ease(p)
            off = int(round(k * w))
            d = tr["dir"]
            blit(cv, prev, -d * off, top, dim=1 - 0.6 * k, clip=clip)
            blit(cv, body, d * (w - off), top, clip=clip)
            return
        # scene change: a short glitch on the old frame, then a beam wipe reveals the new one
        rng = random.Random(int(now * 60) + int(tr["seed"] * 1000))
        glyphs = fx.DECODE_GLYPHS if self.u else "#%&*+=-:01"
        if p < 0.28:
            for y in range(bh):
                shift = rng.choice((0, 0, 0, 1, -1, 2, -3)) if rng.random() < 0.35 else 0
                row = prev.cells[y]
                for x in range(w):
                    ch, fg, bold = row[x]
                    sx = x + shift
                    if 0 <= sx < w and ch != " ":
                        if rng.random() < 0.05:
                            ch, fg = rng.choice(glyphs), fx.grad(SPECTRUM, rng.random())
                        cv.cells[top + y][sx] = (ch, fg, bold)
            return
        q = R.ease((p - 0.28) / 0.72)
        edge = int(q * (w + 6)) - 3
        blit(cv, prev, 0, top, dim=0.35, clip=(max(0, edge + 1), top, w, top + bh))
        blit(cv, body, 0, top, clip=(0, top, max(0, min(w, edge)), top + bh))
        for y in range(bh):
            for k in range(3):
                x = edge - k
                if 0 <= x < w:
                    col = fx.lerp(ICE, fx.grad(SPECTRUM, y / max(1, bh - 1)), k / 3)
                    cv.cells[top + y][x] = (("█▓▒"[k] if self.u else "#=-"[k]), col, k == 0)
            if rng.random() < 0.18:
                x = edge + 1 + rng.randrange(4)
                if 0 <= x < w:
                    cv.cells[top + y][x] = (rng.choice(glyphs), fx.grad(SPECTRUM, rng.random()), False)

    def _body(self, body: Canvas, now: float) -> None:
        sc = self.scene
        if sc is None:
            return
        t = (now - self.shown_at) if self.motion else R.SETTLED
        try:
            self.overflow = R.draw_scene(body, 0, 0, body.w, body.h, sc, slide=self.slide, u=self.u, t=t,
                                         motion=self.motion, scroll=self.scroll) or 0
        except Exception as exc:  # a bad scene must never take the viewer down
            for row in body.cells:
                row[:] = [fx.BLANK] * body.w
            R.draw_card(body, 0, 0, body.w, body.h, "This scene could not be drawn here (%s). Try `w` for the web view."
                        % type(exc).__name__, "error", "", u=self.u)
            self.overflow = 0

    def _idle(self, cv: Canvas, body: Canvas, now: float) -> None:
        t = now - self.t0
        v = R.View(body, 0, 0, body.w, body.h, self.u)
        fit = R.big_fit("PULSAR STAGE", v.w - 4, 5 if not self.u else 3, ("compact",) if self.u else ("tall",), 1)
        block = (R.big_height(fit[0]) if fit else 1) + 9
        y0 = max(0, (v.h - block) // 2)
        cx, cy = v.w // 2, y0 + 1
        if self.motion:
            fx.rings(cv, cx, 2 + cy, t, period=2.2, count=4, unicode=self.u, max_r=max(v.w, v.h * 2) * 0.55)
            for y in range(cv.h):  # keep the rings faint
                row = cv.cells[y]
                for x in range(cv.w):
                    ch, fg, b = row[x]
                    if ch != " " and fg is not None and ch not in ("◉", "@"):
                        row[x] = (ch, fx.scale(fg, 0.45), False)
        yy = y0 + 3
        if fit:
            mode, _ = fit
            bw = R.big_width("PULSAR STAGE", mode)
            shimmer = ((t * 40) % (bw + 60)) - 20 if self.motion else None
            R.draw_big(v, (v.w - bw) // 2, yy, "PULSAR STAGE", mode, t=t if self.motion else R.SETTLED,
                       shimmer=shimmer, offset=(t * 0.05) if self.motion else 0.0)
            yy += R.big_height(mode) + 1
        else:
            v.center(yy, "PULSAR STAGE", ICE, True)
            yy += 2
        label = "A W A I T I N G   T R A N S M I S S I O N" if v.w >= 50 else "AWAITING TRANSMISSION"
        lx = max(0, (v.w - len(label)) // 2)
        if self.motion:
            cyc = t % 6.0
            R.draw_decrypt(v, lx, yy, label, cyc, dur=1.1, stops=(ASH, ICE, ASH), bold=True, seed=int(t // 6))
            if int(t * 2) % 2 == 0:
                v.put(lx + len(label) + 1, yy, "▌" if self.u else "_", BEAM)
        else:
            v.text(lx, yy, label, ASH, True)
        yy += 2
        st = self.feed.state
        if st == "live":
            msg, col, dot = ("listening on 127.0.0.1:%d" % self.feed.port if self.feed.port else "listening") + \
                ((" for session " + self.sid) if self.sid else ""), OK, "●"
        elif st == "nocore":
            left = max(0, int(math.ceil(self.feed.retry_at - now)))
            msg, col, dot = "Pulsar Core is not running yet (retrying%s)" % (" in %ds" % left if left else ""), WARN, "○"
        elif st == "offline":
            left = max(0, int(math.ceil(self.feed.retry_at - now)))
            msg, col, dot = "reconnecting to Pulsar Core%s" % (" in %ds" % left if left else ""), WARN, "○"
        else:
            msg, col, dot = "connecting to Pulsar Core", ASH, "◌"
        line = (dot if self.u else "*") + " " + msg
        v.center(yy, line, col)
        yy += 2
        tips = ["Ask Claude: \"present the architecture of this repo on Pulsar Stage\"",
                "Scenes, decks, charts and diagrams appear here live.",
                ]
        for i, tip in enumerate(tips):
            if yy + i < v.h:
                v.center(yy + i, R.clip(tip, v.w - 4, self.u), DUST if i else SOFT_TIP)

    # -- chrome -----------------------------------------------------------------------
    def _header(self, cv: Canvas, now: float) -> None:
        w = cv.w
        v = R.View(cv, 0, 0, w, 1, self.u)
        t = now - self.t0
        flash = self.haptics.flash
        mk = "◉" if self.u else "@"
        mcol = fx.lerp(BEAM, ICE, 0.5 + 0.5 * math.sin(t * 3)) if self.motion else BEAM
        if flash > 0:
            mcol = fx.lerp(mcol, self.haptics.flash_color, flash)
        v.put(1, 0, mk, mcol, True)
        x = v.gtext(3, 0, "PULSAR STAGE" if w >= 60 else "STAGE", SPECTRUM, True, offset=(t * 0.08) if self.motion else 0)
        # right side: status, session, position, clock
        parts: List[Tuple[str, Color, bool]] = []
        st = self.feed.state
        if st == "live":
            pulse = 0.5 + 0.5 * math.sin(t * 4) if self.motion else 1.0
            parts.append(("●" if self.u else "*", fx.lerp(fx.scale(FLARE, 0.5), FLARE, pulse), True))
            parts.append((" LIVE", FLARE, True))
        elif st in ("offline", "connecting"):
            parts.append((("◌ " if self.u else "~ ") + ("RECONNECTING" if st == "offline" else "CONNECTING"), WARN, True))
        else:
            parts.append((("○ " if self.u else "o ") + "NO CORE", DUST, True))
        item = self.items[self.cur] if 0 <= self.cur < len(self.items) else None
        sid = (item["sid"] if item else "") or self.sid
        if sid and w >= 90:
            parts.append(("  ·  " if self.u else "  |  ", NIGHT, False))
            parts.append((sid, ASH, False))
        n = self._slides()
        if item and n > 1:
            parts.append(("  ·  " if self.u else "  |  ", NIGHT, False))
            parts.append(("%d/%d" % (self.slide + 1, n), ICE, True))
        if w >= 70:
            parts.append(("  ·  " if self.u else "  |  ", NIGHT, False))
            parts.append((time.strftime("%H:%M:%S"), DUST, False))
        right_w = sum(R.swidth(R.prep(p[0], self.u)) for p in parts)
        rx = w - right_w - 2
        if item:
            sc = item["scene"]
            icon = R.KIND_ICON.get(sc.get("kind"), ("◉", "@"))[0 if self.u else 1]
            title = sc.get("title") or sc.get("kind") or ""
            room = rx - x - 8
            if room > 6:
                xx = v.text(x + 2, 0, "·" if self.u else "|", NIGHT)
                xx = v.text(xx + 2, 0, icon, PLASMA, True)
                v.text(xx + 1, 0, R.clip(title, room - R.swidth(R.prep(icon, self.u)) - 1, self.u), ASH)
        for txt, col, bold in parts:
            rx = v.text(rx, 0, txt, col, bold)

    def _rail(self, cv: Canvas, now: float) -> None:
        w = cv.w
        v = R.View(cv, 0, 1, w, 1, self.u)
        n = self._slides()
        flash = self.haptics.flash
        if self.cur >= 0 and n > 1:
            fill = int(round((self.slide + 1) / n * w))
            for x in range(w):
                if x < fill:
                    col = fx.grad(SPECTRUM, x / max(1, w - 1))
                    if flash > 0:
                        col = fx.lerp(col, ICE, flash * 0.6)
                    v.put(x, 0, "━" if self.u else "=", col)
                else:
                    v.put(x, 0, "─" if self.u else "-", NIGHT)
            if self.u and n <= min(40, w // 3):
                for i in range(n):
                    x = int(round((i + 0.5) / n * w))
                    if i == self.slide:
                        v.put(x, 0, "◆", ICE, True)
        else:
            col = NIGHT if flash <= 0 else fx.lerp(NIGHT, self.haptics.flash_color, flash * 0.7)
            v.hline(0, 0, w, "─" if self.u else "-", col)
            if self.motion and self.feed.state == "live":
                head = int(((now - self.t0) * 22) % (w + 30)) - 15
                for k in range(12):
                    x = head - k
                    if 0 <= x < w:
                        v.put(x, 0, "━" if self.u else "=", fx.scale(fx.grad(SPECTRUM, x / max(1, w - 1)), 1 - k / 12))

    def _footer(self, cv: Canvas) -> None:
        w, y = cv.w, cv.h - 1
        v = R.View(cv, 0, y, w, 1, self.u)
        sep = "  ·  " if self.u else "  |  "
        hints = [("←→" if self.u else "<>", "slides"), ("[ ]", "scenes"), ("f", "follow"), ("e", "export"),
                 ("w", "web"), ("?", "help"), ("q", "quit")]
        if self._slides() <= 1:
            hints[0] = ("←→" if self.u else "<>", "scenes")
            hints.pop(1)
        right = []
        if self.items:
            right.append(("scene %d/%d" % (self.cur + 1, len(self.items)), ASH, False))
            if self.unseen:
                right.append(("  +%d new" % self.unseen, BEAM, True))
            right.append(("  FOLLOW" if self.follow else "  PINNED", OK if self.follow else WARN, True))
        rw = sum(len(r[0]) for r in right)
        x = 2
        for i, (k, label) in enumerate(hints):
            need = (len(sep) if i else 0) + R.swidth(R.prep(k, self.u)) + 1 + len(label)
            if x + need > w - rw - 4:
                break
            if i:
                x = v.text(x, 0, sep, NIGHT)
            x = v.text(x, 0, k, BEAM, True)
            x = v.text(x + 1, 0, label, DUST)
        rx = w - rw - 2
        for txt, col, bold in right:
            rx = v.text(rx, 0, txt, col, bold)

    def _toasts(self, cv: Canvas, now: float) -> None:
        self.toasts = [t for t in self.toasts if now - t[2] < 3.2]
        y = cv.h - 3
        for text, col, born in reversed(self.toasts):
            age = now - born
            k = min(1.0, age / 0.15) * (1.0 if age < 2.7 else max(0.0, 1 - (age - 2.7) / 0.5))
            if k <= 0:
                continue
            msg = R.clip(text, max(10, cv.w - 10), self.u)
            bw = R.swidth(R.prep(msg, self.u)) + 4
            x = cv.w - bw - 2
            if y - 1 < 2:
                break
            v = R.View(cv, x, y - 1, bw, 3, self.u)
            v.box(0, 0, bw, 3, fx.scale(col, 0.4 + 0.6 * k), fill=True)
            v.text(2, 1, msg, fx.lerp(NIGHT, col, k), True)
            y -= 3

    def _notes(self, cv: Canvas) -> None:
        sl = self._slide_obj()
        notes = (sl or {}).get("notes") or ""
        if not notes:
            self.notes = False
            return
        w = cv.w
        lines = R.wrap(notes, w - 8, self.u)[: max(1, cv.h // 3)]
        hh = len(lines) + 2
        v = R.View(cv, 2, cv.h - 1 - hh, w - 4, hh, self.u)
        v.box(0, 0, v.w, hh, stops=(PLASMA, BEAM), fill=True)
        v.text(2, 0, " notes ", PLASMA, True)
        for i, ln in enumerate(lines):
            v.text(2, 1 + i, ln, R.SOFT)

    def _help(self, cv: Canvas) -> None:
        rows = [("← → space", "previous / next slide"), ("pgup pgdn", "previous / next slide"),
                ("home end", "first / last slide"), ("1-9", "jump to slide"), ("[ ]", "previous / next scene"),
                ("↑ ↓", "scroll long tables, code, text"), ("f", "follow the newest scene"),
                ("n", "speaker notes"), ("e", "export everything to HTML"), ("w", "open the web stage"),
                ("r", "replay the intro"), ("q esc", "quit")]
        bw = min(cv.w - 4, 56)
        bh = min(cv.h - 2, len(rows) + 4)
        v = R.View(cv, (cv.w - bw) // 2, max(1, (cv.h - bh) // 2), bw, bh, self.u)
        v.box(0, 0, bw, bh, stops=SPECTRUM, fill=True)
        v.text(2, 0, " ◉ KEYS " if self.u else " @ KEYS ", ICE, True)
        for i, (k, d) in enumerate(rows[: bh - 4]):
            v.text(3, 2 + i, R.prep(k, self.u), BEAM, True)
            v.text(16, 2 + i, R.clip(d, bw - 19, self.u), R.SOFT)


SOFT_TIP: Color = (176, 172, 200)
