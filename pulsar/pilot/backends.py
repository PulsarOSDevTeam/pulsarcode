# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Pilot backends: shared types, coordinate mapping, key parsing, detection.

Coordinate spaces
  screen    what the OS input APIs take: X11 root pixels, macOS points
            (CGEvent), Windows physical pixels (the process is DPI aware).
  capture   pixels of the raw grab. Equal to screen except on HiDPI macOS,
            where screencapture returns retina pixels (2x points).
  image     the (possibly downscaled) PNG the model sees. Every coordinate
            the model sends is in image space; Geometry maps it to screen.

The OS modules (x11, macos, windows) are imported lazily by get_backend(),
so importing this file never touches ctypes libraries on any platform.
"""

from __future__ import annotations

import math
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

FAILSAFE_ZONE = 5          # px/pt: the top-left kill-switch square
MAX_TYPE_CHARS = 8000
MAX_COMBOS = 32


class PilotError(Exception):
    """Any backend failure. The message is safe to show to the model."""


class Unsupported(PilotError):
    pass


@dataclass
class Capture:
    """A screen grab. Either raw pixels (data + mode) or an encoded PNG (png)."""
    width: int                 # capture pixels
    height: int
    data: bytes = b""
    mode: str = "RGB"          # pixel layout of data
    png: bytes = b""           # already-encoded PNG (macOS/Wayland tools)
    screen: Tuple[int, int] = (0, 0)   # screen-space size this grab covers
    origin: Tuple[int, int] = (0, 0)   # screen-space top-left of the grab


@dataclass
class Geometry:
    """Maps image coordinates (what the model sees) to screen coordinates."""
    screen_w: int
    screen_h: int
    image_w: int
    image_h: int
    origin_x: int = 0
    origin_y: int = 0

    @property
    def scale(self) -> float:
        """image pixels per screen unit."""
        return self.image_w / float(self.screen_w) if self.screen_w else 1.0

    def to_screen(self, x: float, y: float) -> Tuple[int, int]:
        """The screen pixel under the centre of image pixel (x, y)."""
        sx = int(math.floor((float(x) + 0.5) * self.screen_w / self.image_w))
        sy = int(math.floor((float(y) + 0.5) * self.screen_h / self.image_h))
        sx = min(max(sx, 0), self.screen_w - 1) + self.origin_x
        sy = min(max(sy, 0), self.screen_h - 1) + self.origin_y
        return sx, sy

    def region_to_screen(self, x0: float, y0: float, x1: float, y1: float) -> Tuple[int, int, int, int]:
        """Image-space box [x0, x1) x [y0, y1) -> screen box covering it (edges, not pixel centres)."""
        fx, fy = self.screen_w / float(self.image_w), self.screen_h / float(self.image_h)
        a = max(0, min(self.screen_w, int(math.floor(x0 * fx))))
        b = max(0, min(self.screen_h, int(math.floor(y0 * fy))))
        c = max(0, min(self.screen_w, int(math.ceil(x1 * fx))))
        d = max(0, min(self.screen_h, int(math.ceil(y1 * fy))))
        return a + self.origin_x, b + self.origin_y, c + self.origin_x, d + self.origin_y

    def to_image(self, sx: float, sy: float) -> Tuple[int, int]:
        x = (float(sx) - self.origin_x + 0.5) * self.image_w / self.screen_w - 0.5
        y = (float(sy) - self.origin_y + 0.5) * self.image_h / self.screen_h - 0.5
        return (int(round(min(max(x, 0), self.image_w - 1))), int(round(min(max(y, 0), self.image_h - 1))))

    def check(self, x: float, y: float, name: str = "") -> None:
        if not (0 <= x < self.image_w and 0 <= y < self.image_h):
            raise PilotError("%s(%s, %s) is outside the %dx%d screenshot; use coordinates from pilot_screenshot." % (
                (name + " ") if name else "", _num(x), _num(y), self.image_w, self.image_h))


def _num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else "%.1f" % v


def nudge_out_of_corner(x: int, y: int, origin: Tuple[int, int] = (0, 0)) -> Tuple[int, int]:
    """Keep our own moves out of the failsafe square so only a human can trip it."""
    if x - origin[0] < FAILSAFE_ZONE and y - origin[1] < FAILSAFE_ZONE:
        return origin[0] + FAILSAFE_ZONE, y
    return x, y


def in_failsafe(x: int, y: int, origin: Tuple[int, int] = (0, 0)) -> bool:
    """True when (x, y) is inside the kill-switch square at the main display's top-left corner."""
    return 0 <= x - origin[0] < FAILSAFE_ZONE and 0 <= y - origin[1] < FAILSAFE_ZONE


# ---------------------------------------------------------------------------
# Key combos
# ---------------------------------------------------------------------------

MODIFIERS = ("ctrl", "alt", "shift", "cmd")
_MOD_ALIASES = {
    "ctrl": "ctrl", "control": "ctrl", "ctl": "ctrl",
    "alt": "alt", "option": "alt", "opt": "alt", "⌥": "alt", "altgr": "alt",
    "shift": "shift", "⇧": "shift",
    "cmd": "cmd", "command": "cmd", "⌘": "cmd", "super": "cmd", "win": "cmd", "windows": "cmd", "meta": "cmd",
}
NAMED_KEYS = ("enter", "tab", "escape", "space", "backspace", "delete", "up", "down", "left", "right", "home",
              "end", "pageup", "pagedown", "insert", "capslock", "printscreen", "menu") + \
    tuple("f%d" % i for i in range(1, 25))
_KEY_ALIASES = {
    "return": "enter", "ret": "enter", "kp_enter": "enter", "↵": "enter", "⏎": "enter",
    "esc": "escape",
    "spacebar": "space",
    "back_space": "backspace", "bksp": "backspace", "bs": "backspace",
    "del": "delete", "forwarddelete": "delete",
    "arrowup": "up", "arrowdown": "down", "arrowleft": "left", "arrowright": "right",
    "↑": "up", "↓": "down", "←": "left", "→": "right",
    "page_up": "pageup", "pgup": "pageup", "prior": "pageup", "page_down": "pagedown", "pgdn": "pagedown",
    "next": "pagedown", "ins": "insert", "caps": "capslock", "caps_lock": "capslock", "print": "printscreen",
    "prtsc": "printscreen", "print_screen": "printscreen", "apps": "menu", "contextmenu": "menu",
    "plus": "+", "minus": "-", "equal": "=", "equals": "=", "comma": ",", "period": ".", "dot": ".",
    "slash": "/", "backslash": "\\", "semicolon": ";", "apostrophe": "'", "quote": "'", "grave": "`",
    "backtick": "`", "bracketleft": "[", "bracketright": "]", "leftbracket": "[", "rightbracket": "]",
}


@dataclass
class Combo:
    mods: Tuple[str, ...] = ()
    key: str = ""             # "" = modifiers only (e.g. "super"), a NAMED_KEYS name, a single char, or "raw:<name>"

    def label(self) -> str:
        parts = list(self.mods)
        if self.key:
            parts.append(self.key[4:] if self.key.startswith("raw:") else self.key)
        return "+".join(parts)

    @property
    def is_shortcut(self) -> bool:
        return any(m in ("ctrl", "alt", "cmd") for m in self.mods)


def _split_plus(token: str) -> List[str]:
    parts: List[str] = []
    cur = ""
    for i, ch in enumerate(token):
        if ch == "+" and cur:
            parts.append(cur)
            cur = ""
        elif ch == "+" and not cur and (i == len(token) - 1 or token[i + 1] == "+"):
            cur = "+"
        else:
            cur += ch
    if cur:
        parts.append(cur)
    return parts


def parse_combo(token: str) -> Combo:
    parts = _split_plus(token.strip())
    if not parts:
        raise PilotError("empty key")
    mods: List[str] = []
    key = ""
    for i, raw in enumerate(parts):
        m = _MOD_ALIASES.get(raw.lower())
        if m is not None:
            if m not in mods:
                mods.append(m)
            continue
        if i != len(parts) - 1:
            raise PilotError("'%s' is not a modifier in '%s' (use ctrl, alt, shift, cmd)" % (raw, token))
        key = _canon_key(raw)
    return Combo(tuple(m for m in MODIFIERS if m in mods), key)


def _canon_key(raw: str) -> str:
    low = raw.lower()
    if len(raw) == 1:
        return low if raw.isalpha() and raw.isascii() else raw
    if low in NAMED_KEYS:
        return low
    if low in _KEY_ALIASES:
        return _KEY_ALIASES[low]
    if low.startswith("kp_") or low.startswith("xf86") or raw[:1].isupper():
        return "raw:" + raw
    raise PilotError("unknown key '%s' (try enter, tab, escape, space, backspace, delete, arrows, home, end, "
                     "pageup, pagedown, f1-f24, or a single character)" % raw)


def parse_keys(spec: str) -> List[Combo]:
    """'ctrl+s', 'cmd+shift+4', 'enter', 'ctrl+a ctrl+c' (space separated sequence)."""
    if not isinstance(spec, str) or not spec.strip():
        raise PilotError("keys is required, e.g. 'ctrl+s' or 'enter'")
    tokens = spec.split()
    if len(tokens) > MAX_COMBOS:
        raise PilotError("at most %d key combos per call" % MAX_COMBOS)
    return [parse_combo(t) for t in tokens]


# Shortcuts that end the user's session or stop the machine: log out (macOS logs out at once with
# cmd+alt+shift+q, and after a 60 s countdown with cmd+shift+q), lock the screen, Force Quit, the X server's
# zap, switching to a text console, and the power and session keys. Pilot never presses them: the user can.
_ENDS_SESSION = {
    "all": ((("ctrl", "alt"), "delete"),),
    "macos": ((("shift", "cmd"), "q"), (("alt", "shift", "cmd"), "q"), (("ctrl", "cmd"), "q"),
              (("alt", "cmd"), "escape")),
    "windows": ((("cmd",), "l"),),
    "x11": ((("cmd",), "l"), (("ctrl", "alt"), "backspace")) + tuple(
        (("ctrl", "alt"), "f%d" % i) for i in range(1, 13)),
}
_POWER_KEYSYM = re.compile(r"(?i)^xf86(power|sleep|suspend|hibernate|standby|logoff|logout|screensaver|reboot|"
                           r"shutdown|halt)")


def refuse_keys(combos: Sequence[Combo], kind: str) -> None:
    """PilotError when a combo would log out, lock, force-quit or power off on platform `kind`."""
    rules = _ENDS_SESSION["all"] + _ENDS_SESSION.get(kind, ())
    for c in combos:
        raw = c.key[4:] if c.key.startswith("raw:") else ""
        if (raw and _POWER_KEYSYM.match(raw)) or any(set(c.mods) == set(m) and c.key == k for m, k in rules):
            raise PilotError("Pilot does not press %s: on this computer it logs out, locks the screen, forces apps "
                             "to quit or turns the machine off. Ask the user to do it." % c.label())


def audit_label(combos: Sequence[Combo]) -> str:
    """Log-safe description: shortcuts verbatim, plain typing keys masked."""
    out = []
    for c in combos:
        if c.key and len(c.key) == 1 and not c.is_shortcut:
            out.append("+".join(c.mods + ("*",)))
        else:
            out.append(c.label())
    return " ".join(out)


# ---------------------------------------------------------------------------
# Backend interface
# ---------------------------------------------------------------------------

@dataclass
class Info:
    name: str                       # x11 | macos | windows | wayland | none
    label: str                      # human readable
    ok: bool = True
    detail: str = ""
    hints: List[str] = field(default_factory=list)
    perms: Dict[str, Optional[bool]] = field(default_factory=dict)


class Backend:
    """Abstract input + capture backend. All coordinates are screen space."""

    name = "none"
    label = "none"
    can_act = True

    def size(self) -> Tuple[int, int]:
        raise NotImplementedError

    def origin(self) -> Tuple[int, int]:
        return (0, 0)

    def cursor(self) -> Tuple[int, int]:
        raise NotImplementedError

    def front(self) -> Optional[Tuple[int, int]]:
        """(process id or 0, window id or 0) of what has the keyboard focus, or None when this backend
        cannot tell. Pilot refuses input when it differs from the one at the last screenshot."""
        return None

    def move(self, x: int, y: int) -> None:
        raise NotImplementedError

    def click(self, x: int, y: int, button: str = "left", count: int = 1) -> None:
        raise NotImplementedError

    def scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        raise NotImplementedError

    def drag(self, x1: int, y1: int, x2: int, y2: int) -> None:
        raise NotImplementedError

    def type_text(self, text: str, check=None) -> None:
        raise NotImplementedError

    def keys(self, combos: Sequence[Combo], check=None) -> None:
        """Press each combo in turn; check (when given) runs before each one and raises to stop."""
        raise NotImplementedError

    def capture(self, region: Optional[Tuple[int, int, int, int]] = None, max_w: int = 0, max_h: int = 0) -> Capture:
        raise NotImplementedError

    def info(self) -> Info:
        return Info(self.name, self.label)

    def close(self) -> None:
        pass


def lerp_path(x1: int, y1: int, x2: int, y2: int, steps: int) -> List[Tuple[int, int]]:
    steps = max(1, steps)
    return [(int(round(x1 + (x2 - x1) * i / steps)), int(round(y1 + (y2 - y1) * i / steps))) for i in range(1, steps + 1)]


def drag_steps(x1: int, y1: int, x2: int, y2: int) -> int:
    d = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    return int(min(40, max(8, d / 20)))


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def platform_kind(env: Optional[Dict[str, str]] = None, plat: Optional[str] = None) -> str:
    env = os.environ if env is None else env
    plat = plat or sys.platform
    if plat == "darwin":
        return "macos"
    if plat in ("win32", "cygwin"):
        return "windows"
    wayland = bool(env.get("WAYLAND_DISPLAY")) or env.get("XDG_SESSION_TYPE") == "wayland"
    # GNOME/KDE Wayland sessions and WSLg also set DISPLAY (XWayland), but X11 input there only
    # reaches XWayland windows and screenshots come back black: treat them as Wayland unless asked.
    if env.get("DISPLAY") and (not wayland or env.get("PULSAR_PILOT_X11") == "1"):
        return "x11"
    if wayland:
        return "wayland"
    return "none"


def platform_label(kind: str) -> str:
    return {"macos": "macOS", "windows": "Windows", "x11": "Linux (X11)", "wayland": "Linux (Wayland)",
            "none": "no display"}.get(kind, kind)


_BACKEND: Optional[Backend] = None


def get_backend(kind: str = "") -> Backend:
    """The process-wide backend (created on first use)."""
    global _BACKEND
    if _BACKEND is not None and (not kind or _BACKEND.name == kind):
        return _BACKEND
    kind = kind or platform_kind()
    if kind == "x11":
        from pulsar.pilot.x11 import X11Backend
        b: Backend = X11Backend()
    elif kind == "macos":
        from pulsar.pilot.macos import MacBackend
        b = MacBackend()
    elif kind == "windows":
        from pulsar.pilot.windows import WinBackend
        b = WinBackend()
    elif kind == "wayland":
        b = WaylandBackend()
    else:
        raise Unsupported("no graphical display found (DISPLAY is not set). Pilot needs a desktop session.")
    _BACKEND = b
    return b


def reset_backend() -> None:
    global _BACKEND
    if _BACKEND is not None:
        try:
            _BACKEND.close()
        except Exception:
            pass
    _BACKEND = None


WAYLAND_HELP = ("Wayland does not let apps synthesize global input. Log in with an X11 session "
                "(e.g. 'GNOME on Xorg') to use Pilot, or set PULSAR_PILOT_X11=1 to drive XWayland apps through DISPLAY.")


class WaylandBackend(Backend):
    """Screenshots only (via grim on wlroots compositors). Input is unsupported by design of Wayland."""

    name = "wayland"
    label = "Wayland"
    can_act = False

    def _deny(self) -> None:
        raise Unsupported(WAYLAND_HELP)

    def size(self) -> Tuple[int, int]:
        cap = self.capture()
        return cap.screen

    def cursor(self) -> Tuple[int, int]:
        self._deny()
        return (0, 0)

    def move(self, x, y):
        self._deny()

    def click(self, x, y, button="left", count=1):
        self._deny()

    def scroll(self, x, y, dx, dy):
        self._deny()

    def drag(self, x1, y1, x2, y2):
        self._deny()

    def type_text(self, text, check=None):
        self._deny()

    def keys(self, combos, check=None):
        self._deny()

    def capture(self, region=None, max_w=0, max_h=0) -> Capture:
        import subprocess
        from pulsar.hardware import which
        from pulsar.pilot import png as PNG
        grim = which("grim")              # absolute PATH entries only: never a grim in the project folder
        if not grim:
            self._deny()
        try:
            blob = subprocess.run([grim, "-t", "png", "-"], capture_output=True, timeout=15).stdout
            w, h = PNG.png_size(blob)
        except (OSError, subprocess.SubprocessError, PNG.ImageError) as exc:
            raise PilotError("grim failed: %s" % exc)
        tw, th = PNG.fit(w, h, max_w, max_h)
        if (tw, th) != (w, h):
            try:
                blob = subprocess.run([grim, "-t", "png", "-s", "%.4f" % (tw / float(w)), "-"],
                                      capture_output=True, timeout=15).stdout
                tw, th = PNG.png_size(blob)
            except (OSError, subprocess.SubprocessError, PNG.ImageError) as exc:
                raise PilotError("grim failed: %s" % exc)
        return Capture(tw, th, png=blob, screen=(w, h))

    def info(self) -> Info:
        from pulsar.hardware import which
        return Info("wayland", "Wayland", ok=False, detail="input unsupported",
                    hints=[WAYLAND_HELP] + ([] if which("grim") else
                                            ["Screenshots need grim (wlroots compositors only)."]))
