# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Pilot MCP tools: computer use with the pulsar cursor.

specs() returns the tool table pulsar.mcp.server registers when Pilot is
enabled. Every coordinate the model sends is in the pixel space of its most
recent pilot_screenshot; Geometry maps it to real screen units (HiDPI and
downscaling included). Each action runs:

  enabled? -> rate limit -> failsafe corner (real pointer) -> map coords
  -> pulsar cursor glides there (the corner is watched meanwhile)
  -> failsafe corner and front window checked again -> real input
  -> ripple -> audit line

The front window check compares what has the keyboard focus with what had it
at the last screenshot, where the backend can tell (macOS, Windows, an X11
window manager): input never goes to an app the model has not seen. The
overlay is best effort: if it cannot start, tools work without it.
"""

from __future__ import annotations

import atexit
import base64
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from pulsar import python_argv
from pulsar.pilot import backends as B
from pulsar.pilot import safety as S

Content = List[Dict[str, Any]]


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(os.environ.get(name, default))))
    except ValueError:
        return default


def max_size() -> Tuple[int, int]:
    return _env_int("PULSAR_PILOT_MAX_W", 1280, 320, 4096), _env_int("PULSAR_PILOT_MAX_H", 1280, 240, 4096)


def tool_error(msg: str) -> Exception:
    try:
        from pulsar.mcp.server import ToolError
    except Exception:  # pragma: no cover - the MCP server always exists in-tree
        return RuntimeError(msg)
    return ToolError(msg)


def _text(s: str) -> Dict[str, Any]:
    return {"type": "text", "text": s}


# ---------------------------------------------------------------------------
# Overlay client
# ---------------------------------------------------------------------------

def overlay_python() -> str:
    return os.environ.get("PULSAR_PILOT_PYTHON") or sys.executable


def overlay_blocker(env: Optional[Dict[str, str]] = None) -> str:
    """Why the overlay will not run here ('' = try it)."""
    env = os.environ if env is None else env
    if env.get("PULSAR_PILOT_OVERLAY", "1").lower() in ("0", "off", "false", "no"):
        return "disabled (PULSAR_PILOT_OVERLAY=0)"
    if env.get("PULSAR_FX", "").lower() in ("0", "off", "false", "none", "plain"):
        return "disabled (PULSAR_FX=%s)" % env.get("PULSAR_FX")
    kind = B.platform_kind(env)
    if kind in ("wayland", "none"):
        return "no X11/macOS/Windows display"
    if not env.get("PULSAR_PILOT_PYTHON") and importlib.util.find_spec("_tkinter") is None:
        return "this Python has no tkinter (set PULSAR_PILOT_PYTHON to a Python that has it)"
    return ""


def _package_root() -> str:
    import pulsar
    return os.path.dirname(os.path.dirname(os.path.abspath(pulsar.__file__)))


_CREDENTIAL_NAME = re.compile(r"(?i)(_KEY|_TOKEN|_SECRET|_PASSWORD|_CREDENTIALS?)$|^ANTHROPIC_|^AWS_BEARER")


def overlay_env(environ: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The overlay's environment: this one without credentials (session and admin tokens, provider keys).

    The cursor process only draws; it never talks to Pulsar Core or a provider.
    """
    env = dict(os.environ if environ is None else environ)
    for k in list(env):
        v = env[k] or ""
        if _CREDENTIAL_NAME.search(k) or v.startswith(("pls_", "pla_")):
            del env[k]
    return env


class OverlayClient:
    """Spawns and talks to the pulsar cursor process (JSON lines)."""

    def __init__(self, python: Optional[str] = None, hello_timeout: float = 8.0,
                 module: str = "pulsar.pilot.overlay") -> None:
        self.python = python
        self.module = module
        self.hello_timeout = hello_timeout
        self.proc: Optional[subprocess.Popen] = None
        self.disabled = ""
        self.hello: Dict[str, Any] = {}
        self.clickthrough = False
        self.visible = False
        self.lock = threading.Lock()
        self.cv = threading.Condition()
        self.acks: Dict[int, Dict[str, Any]] = {}
        self.seq = 0
        self.fails = 0
        self.spawned_at = 0.0

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def pid(self) -> int:
        """The cursor process's id while it runs, else 0."""
        p = self.proc
        return p.pid if p is not None and p.poll() is None else 0

    def start(self) -> bool:
        with self.lock:
            if self.alive():
                return True
            if self.disabled:
                return False
            if self.spawned_at and time.monotonic() - self.spawned_at < 20:
                self.fails += 1             # died soon after starting: a crash, not an idle exit
            if self.fails >= 3:
                self.disabled = "overlay keeps exiting"
                return False
            reason = overlay_blocker() if self.python is None else ""
            if reason:
                self.disabled = reason
                return False
            env = overlay_env()
            root = _package_root()
            env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            kw: Dict[str, Any] = {}
            if os.name == "nt":
                kw["creationflags"] = 0x08000000   # CREATE_NO_WINDOW
            else:
                kw["start_new_session"] = True     # a terminal Ctrl-C must not kill the cursor mid-ripple
            try:
                self.proc = subprocess.Popen(python_argv(module=self.module, python=self.python or overlay_python()),
                                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL, env=env, cwd=root, **kw)
            except OSError as exc:
                self.disabled = "cannot start overlay: %s" % exc
                return False
            self.spawned_at = time.monotonic()
            self.hello = {}
            self.visible = False
            got = threading.Event()

            def read() -> None:
                assert self.proc is not None and self.proc.stdout is not None
                for raw in self.proc.stdout:
                    try:
                        msg = json.loads(raw.decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    if not isinstance(msg, dict):
                        continue
                    if "hello" in msg:
                        self.hello = msg
                        self.clickthrough = bool(msg.get("clickthrough"))
                        got.set()
                        continue
                    if "ack" in msg:
                        with self.cv:
                            self.visible = bool(msg.get("visible"))
                            if "clickthrough" in msg:
                                self.clickthrough = bool(msg["clickthrough"])
                            try:
                                self.acks[int(msg["ack"])] = msg
                            except (TypeError, ValueError):
                                pass
                            self.cv.notify_all()
                got.set()
                with self.cv:
                    self.cv.notify_all()
            threading.Thread(target=read, daemon=True).start()
            got.wait(self.hello_timeout)
            if not self.hello.get("ok"):
                self.disabled = "overlay unavailable: %s" % (self.hello.get("error") or "no reply")
                self._kill()
                return False
            return True

    def _kill(self) -> None:
        p = self.proc
        self.proc = None
        if p is None:
            return
        try:
            if p.stdin:
                p.stdin.close()
        except OSError:
            pass
        try:
            p.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            p.kill()

    def send(self, op: Dict[str, Any], wait: float = 0.0) -> Optional[Dict[str, Any]]:
        """Send one op. With wait > 0, block until its ack (or the timeout). Never raises."""
        if not self.start():
            return None
        msg = dict(op)
        aid = 0
        if wait > 0:
            with self.cv:
                self.seq += 1
                aid = self.seq
            msg["ack"] = aid
        try:
            assert self.proc is not None and self.proc.stdin is not None
            self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except (OSError, ValueError, AssertionError):
            self._kill()            # it exited (idle timeout or crash); the next action respawns it
            return None
        if not aid:
            return None
        end = time.monotonic() + wait
        with self.cv:
            while aid not in self.acks:
                left = end - time.monotonic()
                if left <= 0 or not self.alive():
                    return None
                self.cv.wait(min(left, 0.05))
            return self.acks.pop(aid)

    def close(self) -> None:
        if self.alive():
            try:
                assert self.proc is not None and self.proc.stdin is not None
                self.proc.stdin.write(b'{"op":"quit"}\n')
                self.proc.stdin.flush()
            except (OSError, ValueError, AssertionError):
                pass
        self._kill()


# ---------------------------------------------------------------------------
# Pilot
# ---------------------------------------------------------------------------

def _num(args: Dict[str, Any], key: str, default: Optional[float] = None) -> float:
    v = args.get(key, default)
    if v is None:
        raise B.PilotError("%s is required" % key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        try:
            v = float(str(v).strip())
        except ValueError:
            raise B.PilotError("%s must be a number" % key)
    v = float(v)
    if math.isnan(v) or math.isinf(v):
        raise B.PilotError("%s must be a finite number" % key)
    return v


NO_VISION = ("The active model cannot view images; Pilot needs a vision model. Switch with /pulsar:switch. "
             "No screenshot was taken.")


def active_vision(environ: Optional[Dict[str, str]] = None) -> Tuple[Optional[bool], str]:
    """(can the session's active model view images, model id). The vision flag is None when unknown.

    The session file is read on every call so a /pulsar:switch is seen at once; PULSAR_MODEL_VISION
    (0 or 1, set by the launcher) answers when there is no session to read.
    """
    env = os.environ if environ is None else environ
    sid = env.get("PULSAR_SESSION", "")
    if sid:
        try:
            from pulsar import catalog as C
            from pulsar import providers as P
            from pulsar.core import state as ST
            sess = ST.load_session(sid)
            t = sess.primary if sess is not None else None
            if t is not None and t.model:
                return C.model_vision(t.profile, t.model, P.get(t.provider)), t.model
        except Exception:        # never let a broken session file block Pilot outright
            pass
    flag = (env.get("PULSAR_MODEL_VISION") or "").strip()
    return ({"1": True, "0": False}.get(flag), env.get("PULSAR_MODEL", "") or "")


def _label(args: Dict[str, Any]) -> str:
    return " ".join(str(args.get("label") or "").split())[:48]


FRONT_CHANGED = ("The app or window in front changed since the last pilot_screenshot (the user, a notification "
                 "or your last action switched it), so nothing was sent. Take a new pilot_screenshot and check "
                 "what is in front before acting.")
FOCUS_ON_OVERLAY = ("The keyboard focus is on Pulsar's cursor overlay, so nothing was typed. Click the target "
                    "field first (pilot_click), then type.")
WATCH_EVERY = 0.05          # seconds between looks at the failsafe corner while an action waits


def _front(b: B.Backend) -> Optional[Tuple[int, int]]:
    try:
        f = b.front()
    except Exception:
        return None
    return (int(f[0]), int(f[1])) if f else None


class CornerWatch:
    """Watches the real pointer while an action waits before its input (the overlay starting, the glide).

    A human who slams the mouse into the failsafe corner meanwhile trips the guard, even when the pointer
    has left the corner again by the time the input would go out. Stopped before Pilot moves the pointer
    itself, so its own drags never trip it.
    """

    def __init__(self, guard: S.Guard, b: B.Backend, every: float = WATCH_EVERY) -> None:
        self.guard, self.b, self.every = guard, b, every
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self.stop.wait(self.every):
            try:
                pos, origin = self.b.cursor(), self.b.origin()
            except Exception:
                continue
            if B.in_failsafe(pos[0], pos[1], origin):
                try:
                    self.guard.failsafe(pos, origin)       # trips and stays tripped
                except S.SafetyError:
                    pass
                return

    def __enter__(self) -> "CornerWatch":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop.set()
        self.thread.join(1.0)


class Pilot:
    def __init__(self, backend_factory: Callable[[], B.Backend] = B.get_backend, guard: Optional[S.Guard] = None,
                 overlay: Optional[OverlayClient] = None, settle: float = 0.07) -> None:
        self.backend_factory = backend_factory
        self.guard = guard or S.Guard()
        self.overlay = overlay if overlay is not None else OverlayClient()
        self.lock = threading.RLock()
        self._backend: Optional[B.Backend] = None
        self.geom: Optional[B.Geometry] = None
        self.front: Optional[Tuple[int, int]] = None     # what had the keyboard focus at the last screenshot
        self.settle = settle

    # -- plumbing ----------------------------------------------------------------------
    def backend(self) -> B.Backend:
        if self._backend is None:
            self._backend = self.backend_factory()
        return self._backend

    def geometry(self) -> B.Geometry:
        b = self.backend()
        if self.geom is not None:
            if b.can_act and b.size() != (self.geom.screen_w, self.geom.screen_h):
                self.geom = None
                raise B.PilotError("The display changed size since the last screenshot; take a new pilot_screenshot.")
            return self.geom
        sw, sh = b.size()
        iw, ih = _fit(sw, sh)
        ox, oy = b.origin()
        return B.Geometry(sw, sh, iw, ih, ox, oy)

    def _pre(self, action: str, needs_input: bool = True) -> Tuple[B.Backend, Optional[Tuple[int, int]]]:
        self.guard.require_enabled()
        self.guard.throttle()
        b = self.backend()
        if needs_input and not b.can_act:
            raise B.Unsupported(B.WAYLAND_HELP if b.name == "wayland" else "this backend cannot send input")
        pos: Optional[Tuple[int, int]] = None
        try:
            pos = b.cursor()
        except B.Unsupported:
            if needs_input:
                raise
        if pos is not None:
            self.guard.failsafe(pos, b.origin())
        return b, pos

    def _ready(self, b: B.Backend, typing: bool = False) -> None:
        """The last look before input goes out: still enabled, the pointer out of the failsafe corner, and
        the keyboard focus where it was at the last screenshot (where the backend can tell)."""
        self.guard.require_enabled()
        self.guard.failsafe(b.cursor(), b.origin())
        now = _front(b)
        if now is None:
            return
        overlay_pid = getattr(self.overlay, "pid", lambda: 0)()
        if typing and overlay_pid and now[0] == overlay_pid:
            raise B.PilotError(FOCUS_ON_OVERLAY)
        if self.front is not None and now != self.front:
            raise B.PilotError(FRONT_CHANGED)

    def _check(self, b: B.Backend) -> Callable[[], None]:
        """_ready for keyboard input, run between chunks of typing and before each key combo."""
        def check() -> None:
            self._ready(b, typing=True)
        return check

    def _target(self, args: Dict[str, Any], kx: str = "x", ky: str = "y") -> Tuple[Tuple[int, int], Tuple[int, int]]:
        g = self.geometry()
        x, y = _num(args, kx), _num(args, ky)
        g.check(x, y, "(%s, %s) " % (kx, ky) if kx != "x" else "")
        sx, sy = g.to_screen(x, y)
        sx, sy = B.nudge_out_of_corner(sx, sy, (g.origin_x, g.origin_y))
        return (int(round(x)), int(round(y))), (sx, sy)

    def _aim(self, sx: int, sy: int, pos: Optional[Tuple[int, int]], label: str, verb: str) -> None:
        self.overlay.start()   # learn whether it is click-through before deciding to hide it
        dist = math.hypot(sx - pos[0], sy - pos[1]) if pos else 0
        self.overlay.send({"op": "aim", "x": sx, "y": sy, "label": label, "verb": verb,
                           "from": list(pos) if pos else None, "hide": not self.overlay.clickthrough},
                          wait=0.25 + 0.2 * min(1.0, dist / 900.0) + 0.16 + 0.6)

    def _run(self, action: str, fn: Callable[[], Content]) -> Content:
        with self.lock:
            try:
                out = fn()
            except B.PilotError as exc:
                S.audit(action, ok=0, error=type(exc).__name__)
                note = S.audit_notice()
                raise tool_error(str(exc) + (" " + note if note else ""))
            note = S.audit_notice()
            return list(out) + [_text(note)] if note else out

    # -- tools ---------------------------------------------------------------------------
    def screenshot(self, args: Dict[str, Any]) -> Content:
        return self._run("screenshot", lambda: self._screenshot(args))

    def _screenshot(self, args: Dict[str, Any]) -> Content:
        from pulsar.pilot import png as PNG
        self.guard.require_enabled()
        vision, _model = active_vision()
        if vision is False:
            raise B.PilotError(NO_VISION)        # checked before the screen is captured
        b, pos = self._pre("screenshot", needs_input=False)
        mw, mh = max_size()
        zoom = args.get("zoom")
        region = None
        g = self.geometry()
        if zoom is not None:
            if not isinstance(zoom, (list, tuple)) or len(zoom) != 4:
                raise B.PilotError("zoom must be [x0, y0, x1, y1] in screenshot coordinates")
            x0, y0, x1, y1 = zx = tuple(_num({"v": v}, "v") for v in zoom)
            if x1 - x0 < 4 or y1 - y0 < 4:
                raise B.PilotError("zoom region is too small")
            region = g.region_to_screen(max(0.0, x0), max(0.0, y0), min(g.image_w, x1), min(g.image_h, y1))
            if region[2] - region[0] < 2 or region[3] - region[1] < 2:
                raise B.PilotError("zoom region is outside the screenshot")
        ack = self.overlay.send({"op": "hide"}, wait=0.6) if self.overlay.alive() else None
        if ack and ack.get("was_visible"):
            time.sleep(self.settle)       # let the apps underneath repaint
        front = _front(b)
        cap = b.capture(region, mw, mh)
        if cap.png:
            blob, iw, ih = cap.png, cap.width, cap.height
        else:
            rgb, _ = PNG.to_rgb(cap.data, cap.width, cap.height, cap.mode)
            iw, ih = PNG.fit(cap.width, cap.height, mw, mh)
            rgb = PNG.resample(rgb, cap.width, cap.height, iw, ih,
                               smooth=os.environ.get("PULSAR_PILOT_RESAMPLE", "smooth") != "nearest")
            blob = PNG.encode(iw, ih, rgb, "RGB")
        sw, sh = cap.screen if cap.screen != (0, 0) else (cap.width, cap.height)
        shot = B.Geometry(sw, sh, iw, ih, cap.origin[0], cap.origin[1])
        lines = []
        if region is None:
            self.geom = shot
            self.front = front
            lines.append("Screenshot %dx%d of the %dx%d %s display (scale %.3f)." % (
                iw, ih, sw, sh, "pt" if b.name == "macos" else "px", shot.scale))
        else:
            lines.append("Zoomed view of screenshot region (%d, %d)-(%d, %d), shown at %dx%d. Coordinates for "
                         "pilot actions still refer to the full screenshot." % (
                             int(zx[0]), int(zx[1]), int(zx[2]), int(zx[3]), iw, ih))
        full = self.geom or g
        if pos is not None:
            cx, cy = full.to_image(*pos)
            lines.append("Pointer at (%d, %d)." % (cx, cy))
        if region is None:
            lines.append("Use this image's pixel coordinates (origin top-left) for pilot_click, pilot_move and the "
                         "rest.")
        if vision is None:
            lines.append("Note: Pulsar cannot tell whether the active model views images. If no image reached you, "
                         "do not guess coordinates: stop and ask the user to switch to a vision model.")
        if b.name == "macos":
            can = getattr(b, "can_record", lambda: None)()
            if can is False:
                from pulsar.pilot.macos import SR_HINT
                lines.append("WARNING: " + SR_HINT)
        self.overlay.send({"op": "scan", "label": "%dx%d" % (iw, ih), "from": list(pos) if pos else None})
        S.audit("screenshot", w=iw, h=ih, region=1 if region else None)
        return [{"type": "image", "data": base64.b64encode(blob).decode("ascii"), "mimeType": "image/png"},
                _text(" ".join(lines))]

    def move(self, args: Dict[str, Any]) -> Content:
        def go() -> Content:
            b, pos = self._pre("move")
            (x, y), (sx, sy) = self._target(args)
            dist = math.hypot(sx - pos[0], sy - pos[1]) if pos else 0
            with CornerWatch(self.guard, b):
                self.overlay.send({"op": "glide", "x": sx, "y": sy, "label": _label(args), "verb": "move",
                                   "from": list(pos) if pos else None},
                                  wait=0.25 + 0.2 * min(1.0, dist / 900.0) + 0.5)
            self._ready(b)
            b.move(sx, sy)
            S.audit("move", x=sx, y=sy)
            return [_text("Moved the pointer to (%d, %d)." % (x, y))]
        return self._run("move", go)

    def click(self, args: Dict[str, Any]) -> Content:
        def go() -> Content:
            b, pos = self._pre("click")
            button = str(args.get("button") or "left").lower()
            if button not in ("left", "right", "middle"):
                raise B.PilotError("button must be left, right or middle")
            count = 2 if args.get("double") is True else 1
            if "clicks" in args:
                count = int(max(1, min(3, _num(args, "clicks"))))
            (x, y), (sx, sy) = self._target(args)
            label = _label(args)
            with CornerWatch(self.guard, b):
                self._aim(sx, sy, pos, label, ("double " if count == 2 else "") +
                          ("click" if button == "left" else button + " click"))
            self._ready(b)
            b.click(sx, sy, button, count)
            self.overlay.send({"op": "click", "x": sx, "y": sy, "button": button, "count": count, "label": label})
            S.audit("click", x=sx, y=sy, button=button, count=count, label_chars=len(label) or None)
            what = {1: "Clicked", 2: "Double-clicked", 3: "Triple-clicked"}[count]
            return [_text("%s %s at (%d, %d)." % (what, button, x, y))]
        return self._run("click", go)

    def type_text(self, args: Dict[str, Any]) -> Content:
        def go() -> Content:
            text = args.get("text")
            if not isinstance(text, str) or text == "":
                raise B.PilotError("text is required")
            if len(text) > B.MAX_TYPE_CHARS:
                raise B.PilotError("at most %d characters per call" % B.MAX_TYPE_CHARS)
            b, pos = self._pre("type")
            with CornerWatch(self.guard, b):
                if pos is not None:
                    self.overlay.send({"op": "type", "x": pos[0], "y": pos[1], "chars": len(text),
                                       "dur": len(text) * 0.011 + 0.3, "from": list(pos)})
            b.type_text(text, check=self._check(b))           # checked again before the first character
            S.audit("type", chars=len(text))
            return [_text("Typed %d character%s." % (len(text), "" if len(text) == 1 else "s"))]
        return self._run("type", go)

    def key(self, args: Dict[str, Any]) -> Content:
        def go() -> Content:
            combos = B.parse_keys(args.get("keys"))   # type: ignore[arg-type]
            b, pos = self._pre("key")
            B.refuse_keys(combos, b.name if b.name in ("macos", "windows", "x11") else B.platform_kind())
            label = " ".join(c.label() for c in combos)
            with CornerWatch(self.guard, b):
                if pos is not None:
                    self.overlay.send({"op": "key", "x": pos[0], "y": pos[1], "label": label, "from": list(pos)})
            b.keys(combos, check=self._check(b))              # checked again before each combo
            S.audit("key", keys=B.audit_label(combos).replace(" ", ","))
            return [_text("Pressed %s." % label)]
        return self._run("key", go)

    def scroll(self, args: Dict[str, Any]) -> Content:
        def go() -> Content:
            dy = int(round(_num(args, "dy", 0)))
            dx = int(round(_num(args, "dx", 0)))
            if not dx and not dy:
                raise B.PilotError("dy (positive scrolls down) or dx (positive scrolls right) is required")
            dy, dx = max(-50, min(50, dy)), max(-50, min(50, dx))
            b, pos = self._pre("scroll")
            (x, y), (sx, sy) = self._target(args)
            dist = math.hypot(sx - pos[0], sy - pos[1]) if pos else 0
            with CornerWatch(self.guard, b):
                self.overlay.send({"op": "glide", "x": sx, "y": sy, "from": list(pos) if pos else None},
                                  wait=0.25 + 0.2 * min(1.0, dist / 900.0) + 0.5)
                if self.overlay.alive() and not self.overlay.clickthrough:
                    self.overlay.send({"op": "hide"}, wait=0.5)
            self._ready(b)
            self.overlay.send({"op": "scroll", "x": sx, "y": sy, "dx": dx, "dy": dy})
            b.scroll(sx, sy, dx, dy)
            S.audit("scroll", x=sx, y=sy, dx=dx, dy=dy)
            parts = []
            if dy:
                parts.append("%s %d" % ("down" if dy > 0 else "up", abs(dy)))
            if dx:
                parts.append("%s %d" % ("right" if dx > 0 else "left", abs(dx)))
            return [_text("Scrolled %s at (%d, %d)." % (" and ".join(parts), x, y))]
        return self._run("scroll", go)

    def drag(self, args: Dict[str, Any]) -> Content:
        def go() -> Content:
            b, pos = self._pre("drag")
            (x1, y1), (s1x, s1y) = self._target(args, "x1", "y1")
            (x2, y2), (s2x, s2y) = self._target(args, "x2", "y2")
            label = _label(args)
            with CornerWatch(self.guard, b):
                self._aim(s1x, s1y, pos, label, "drag")
            self._ready(b)
            steps = B.drag_steps(s1x, s1y, s2x, s2y)
            self.overlay.send({"op": "drag", "x1": s1x, "y1": s1y, "x2": s2x, "y2": s2y, "label": label,
                               "dur": 0.08 + steps * 0.012 + 0.06})
            b.drag(s1x, s1y, s2x, s2y)
            S.audit("drag", x1=s1x, y1=s1y, x2=s2x, y2=s2y)
            return [_text("Dragged from (%d, %d) to (%d, %d)." % (x1, y1, x2, y2))]
        return self._run("drag", go)

    def cursor(self, args: Dict[str, Any]) -> Content:
        def go() -> Content:
            b, pos = self._pre("cursor")
            assert pos is not None
            g = self.geometry()
            ix, iy = g.to_image(*pos)
            return [_text("Pointer at (%d, %d) in screenshot coordinates (%dx%d image; screen %d, %d)." % (
                ix, iy, g.image_w, g.image_h, pos[0], pos[1]))]
        return self._run("cursor", go)

    def close(self) -> None:
        self.overlay.close()


def _fit(sw: int, sh: int) -> Tuple[int, int]:
    from pulsar.pilot import png as PNG
    mw, mh = max_size()
    return PNG.fit(sw, sh, mw, mh)


# ---------------------------------------------------------------------------
# Tool table
# ---------------------------------------------------------------------------

_PILOT: Optional[Pilot] = None


def pilot() -> Pilot:
    global _PILOT
    if _PILOT is None:
        _PILOT = Pilot()
        atexit.register(shutdown)
    return _PILOT


def shutdown(timeout: float = 1.5) -> None:
    """Close the cursor overlay and the backend (X11 hands back the keycodes it borrowed for typing).

    Called by the MCP server as it exits (it leaves with os._exit, so atexit never runs there). An action
    still running holds the backend: give up after `timeout` instead of hanging the exit.
    """
    def close() -> None:
        try:
            if _PILOT is not None:
                _PILOT.close()
        finally:
            B.reset_backend()
    t = threading.Thread(target=close, daemon=True)
    t.start()
    t.join(timeout)


def _platform_note() -> str:
    kind = B.platform_kind()
    if kind == "macos":
        return "This computer runs macOS: use cmd for shortcuts (cmd+c, cmd+s, cmd+space)."
    if kind == "windows":
        return "This computer runs Windows: use ctrl for shortcuts (ctrl+c, ctrl+s); cmd means the Windows key."
    return "This computer runs Linux: use ctrl for shortcuts (ctrl+c, ctrl+s); cmd means the Super key."


_XY = {"x": {"type": "number", "description": "X in pixels of the latest pilot_screenshot (0 = left edge)."},
       "y": {"type": "number", "description": "Y in pixels of the latest pilot_screenshot (0 = top edge)."}}
_LABEL = {"label": {"type": "string", "maxLength": 48,
                    "description": "Short name of the target (e.g. 'Save button'), shown to the user on the pulsar cursor."}}
_ACT = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True}


def specs(p: Optional[Pilot] = None) -> List[Dict[str, Any]]:
    """MCP tool specs. Handlers are bound to one Pilot (created lazily on first call)."""
    def bind(name: str) -> Callable[[Dict[str, Any]], Content]:
        def handler(args: Dict[str, Any]) -> Content:
            return getattr(p or pilot(), name)(args if isinstance(args, dict) else {})
        return handler

    note = _platform_note()
    return [
        {"name": "pilot_screenshot",
         "description": "See the user's screen. ALWAYS call this first and again after anything changes: every "
                        "pilot_* coordinate is a pixel position in the most recent screenshot (origin top-left), "
                        "and the image is already scaled for you. When the app or window in front changes after "
                        "a screenshot, actions are refused until the next one. Optional zoom [x0,y0,x1,y1] "
                        "returns a sharper close-up of that region for reading small text; coordinates still "
                        "refer to the full screenshot. The image goes to the model answering this session. "
                        + note,
         "inputSchema": {"type": "object", "properties": {
             "zoom": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4,
                      "description": "Optional [x0, y0, x1, y1] region of the last screenshot to magnify."}},
             "additionalProperties": False},
         "handler": bind("screenshot"),
         # Not read-only: the whole screen leaves this machine for the model (hints are only ever conservative).
         "annotations": {"title": "Pilot: screenshot", "readOnlyHint": False, "destructiveHint": False,
                         "openWorldHint": True}},
        {"name": "pilot_click",
         "description": "Click at (x, y) in screenshot pixels. Take a pilot_screenshot first, aim at the centre "
                        "of the target, then take another screenshot to confirm the result. button: left (default), "
                        "right or middle; double: true for a double click, or clicks: 1 to 3.",
         "inputSchema": {"type": "object", "properties": dict(_XY, **{
             "button": {"type": "string", "enum": ["left", "right", "middle"]},
             "double": {"type": "boolean"}, "clicks": {"type": "integer", "minimum": 1, "maximum": 3}}, **_LABEL),
             "required": ["x", "y"], "additionalProperties": False},
         "handler": bind("click"), "annotations": dict(_ACT, title="Pilot: click")},
        {"name": "pilot_type",
         "description": "Type text into whatever has keyboard focus (click the field first). Newlines press "
                        "Enter. Use pilot_key for shortcuts and special keys. Max %d characters." % B.MAX_TYPE_CHARS,
         "inputSchema": {"type": "object", "properties": {"text": {"type": "string", "minLength": 1,
                                                                    "maxLength": B.MAX_TYPE_CHARS}},
                         "required": ["text"], "additionalProperties": False},
         "handler": bind("type_text"), "annotations": dict(_ACT, title="Pilot: type")},
        {"name": "pilot_key",
         "description": "Press keys or shortcuts, e.g. 'enter', 'escape', 'tab', 'ctrl+s', 'cmd+shift+4', "
                        "'alt+f4', 'pagedown', 'f5'. Separate a sequence with spaces: 'ctrl+a ctrl+c'. "
                        "Modifiers: ctrl, alt (option), shift, cmd (super/windows key). " + note,
         "inputSchema": {"type": "object", "properties": {"keys": {"type": "string", "minLength": 1}},
                         "required": ["keys"], "additionalProperties": False},
         "handler": bind("key"), "annotations": dict(_ACT, title="Pilot: key")},
        {"name": "pilot_move",
         "description": "Move the pointer to (x, y) in screenshot pixels without clicking (hover menus, tooltips).",
         "inputSchema": {"type": "object", "properties": dict(_XY, **_LABEL), "required": ["x", "y"],
                         "additionalProperties": False},
         "handler": bind("move"), "annotations": dict(_ACT, title="Pilot: move", destructiveHint=False)},
        {"name": "pilot_scroll",
         "description": "Scroll at (x, y) in screenshot pixels. dy > 0 scrolls down, dy < 0 up; dx > 0 right, "
                        "dx < 0 left. Units are wheel notches (about 3 lines each), at most 50.",
         "inputSchema": {"type": "object", "properties": dict(_XY, **{
             "dy": {"type": "integer", "minimum": -50, "maximum": 50},
             "dx": {"type": "integer", "minimum": -50, "maximum": 50}}), "required": ["x", "y"],
             "additionalProperties": False},
         "handler": bind("scroll"), "annotations": dict(_ACT, title="Pilot: scroll", destructiveHint=False)},
        {"name": "pilot_drag",
         "description": "Press the left button at (x1, y1), drag to (x2, y2) and release (move windows, select "
                        "text, sliders). Screenshot pixels.",
         "inputSchema": {"type": "object", "properties": dict({
             "x1": {"type": "number"}, "y1": {"type": "number"}, "x2": {"type": "number"},
             "y2": {"type": "number"}}, **_LABEL), "required": ["x1", "y1", "x2", "y2"],
             "additionalProperties": False},
         "handler": bind("drag"), "annotations": dict(_ACT, title="Pilot: drag")},
        {"name": "pilot_cursor",
         "description": "Report where the pointer is, in screenshot pixels.",
         "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
         "handler": bind("cursor"), "annotations": {"title": "Pilot: cursor", "readOnlyHint": True,
                                                     "openWorldHint": False}},
    ]
