# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""macOS backend: CoreGraphics events through ctypes, screencapture + sips.

Screen space is CoreGraphics points (what CGEvent takes). screencapture
returns retina pixels, so a 1440x900-point display yields a 2880x1800 PNG;
sips resamples it to the size the model sees and Geometry maps back to
points. The planning helpers (mouse_plan, combo_plan, utf16_units) are pure
so they are unit-tested on every OS.

Permissions belong to the app hosting the terminal (Terminal, iTerm2,
Ghostty, ...): Accessibility for input, Screen Recording for screenshots.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import tempfile
import threading
import time
from typing import List, Optional, Sequence, Tuple

from pulsar.pilot.backends import (Backend, Capture, Combo, Info, PilotError, Unsupported, drag_steps, lerp_path)

# kVK_* virtual keycodes (Carbon HIToolbox/Events.h), ANSI layout.
KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12, "w": 13,
    "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25,
    "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "l": 37, "j": 38,
    "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "`": 50,
    "enter": 36, "tab": 48, "space": 49, "backspace": 51, "escape": 53, "delete": 117, "home": 115, "end": 119,
    "pageup": 116, "pagedown": 121, "left": 123, "right": 124, "down": 125, "up": 126, "capslock": 57,
    "insert": 114, "menu": 110, "printscreen": 105,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101, "f10": 109,
    "f11": 103, "f12": 111, "f13": 105, "f14": 107, "f15": 113, "f16": 106, "f17": 64, "f18": 79, "f19": 80,
    "f20": 90,
}
# Shifted punctuation on the ANSI layout -> its base key (key_for adds the shift)
SHIFTED = {"!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
           "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\", ":": ";", "\"": "'", "<": ",", ">": ".", "?": "/",
           "~": "`"}
MOD_KEYCODES = {"cmd": 55, "shift": 56, "alt": 58, "ctrl": 59}
MOD_FLAGS = {"shift": 0x00020000, "ctrl": 0x00040000, "alt": 0x00080000, "cmd": 0x00100000}

# CGEventType / CGMouseButton
MOUSE = {"left": (1, 2, 6, 0), "right": (3, 4, 7, 1), "middle": (25, 26, 27, 2)}   # down, up, dragged, button
MOUSE_MOVED = 5
CLICK_STATE = 1          # kCGMouseEventClickState
HID_TAP = 0              # kCGHIDEventTap
SCROLL_LINE = 1          # kCGScrollEventUnitLine

AX_HINT = ("macOS blocked synthetic input. Allow your terminal app in System Settings > Privacy & Security > "
           "Accessibility, then restart the terminal.")
SR_HINT = ("Screen Recording is not granted to your terminal app, so screenshots show only the desktop. Allow it in "
           "System Settings > Privacy & Security > Screen Recording, then restart the terminal.")


def mouse_plan(button: str, count: int) -> List[Tuple[int, int, int]]:
    """[(event type, CGMouseButton, click state)] for a click of `count` presses."""
    if button not in MOUSE:
        raise PilotError("button must be left, right or middle")
    down, up, _, btn = MOUSE[button]
    out = []
    for i in range(1, max(1, min(3, count)) + 1):
        out.append((down, btn, i))
        out.append((up, btn, i))
    return out


def key_for(key: str) -> Tuple[int, bool]:
    """(virtual keycode, needs shift) for a portable key name or character."""
    if key in KEYCODES:
        return KEYCODES[key], False
    if len(key) == 1 and key.lower() in KEYCODES and key.isalpha():
        return KEYCODES[key.lower()], key.isupper()
    if key in SHIFTED:
        return KEYCODES[SHIFTED[key]], True
    if key.startswith("raw:"):
        raise PilotError("unknown key '%s' on macOS" % key[4:])
    raise PilotError("no macOS keycode for '%s' (type it with pilot_type instead)" % key)


def combo_plan(combo: Combo) -> List[Tuple[int, bool, int]]:
    """[(keycode, key down?, CGEventFlags)] pressing modifiers, the key, then releasing in reverse."""
    mods = list(combo.mods)
    code: Optional[int] = None
    if combo.key:
        code, shift = key_for(combo.key)
        if shift and "shift" not in mods:
            mods.append("shift")
    order = [m for m in ("cmd", "ctrl", "alt", "shift") if m in mods]
    plan: List[Tuple[int, bool, int]] = []
    flags = 0
    for m in order:
        flags |= MOD_FLAGS[m]
        plan.append((MOD_KEYCODES[m], True, flags))
    if code is not None:
        plan.append((code, True, flags))
        plan.append((code, False, flags))
    for m in reversed(order):
        flags &= ~MOD_FLAGS[m]
        plan.append((MOD_KEYCODES[m], False, flags))
    return plan


def utf16_units(ch: str) -> List[int]:
    b = ch.encode("utf-16-le")
    return [b[i] | (b[i + 1] << 8) for i in range(0, len(b), 2)]


def scroll_wheels(dx: int, dy: int) -> Tuple[int, int]:
    """(wheel1 vertical, wheel2 horizontal) in lines. Our dy>0 means 'scroll down' (content moves up)."""
    return -int(dy), -int(dx)


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class CGRect(ctypes.Structure):
    _fields_ = [("origin", CGPoint), ("size", CGSize)]


def _framework(name: str):
    path = "/System/Library/Frameworks/%s.framework/%s" % (name, name)
    try:
        return ctypes.CDLL(path)
    except OSError:
        raise Unsupported("cannot load %s" % name)


class MacBackend(Backend):
    name = "macos"
    label = "macOS (CoreGraphics)"

    def __init__(self, cg=None, cf=None, ax=None, run=None) -> None:
        self.lock = threading.RLock()
        self.cg = cg or _framework("CoreGraphics")
        self.cf = cf or _framework("CoreFoundation")
        self.ax = ax if ax is not None else self._load_ax()
        self.run = run or subprocess.run
        if cg is None:
            self._bind()

    @staticmethod
    def _load_ax():
        try:
            return _framework("ApplicationServices")
        except Unsupported:
            return None

    def _bind(self) -> None:
        vp, u32, c = ctypes.c_void_p, ctypes.c_uint32, self.cg
        sig = {
            "CGMainDisplayID": (u32, []),
            "CGDisplayBounds": (CGRect, [u32]),
            "CGEventCreate": (vp, [vp]),
            "CGEventGetLocation": (CGPoint, [vp]),
            "CGEventCreateMouseEvent": (vp, [vp, u32, CGPoint, u32]),
            "CGEventCreateKeyboardEvent": (vp, [vp, ctypes.c_uint16, ctypes.c_bool]),
            "CGEventKeyboardSetUnicodeString": (None, [vp, ctypes.c_ulong, ctypes.POINTER(ctypes.c_uint16)]),
            "CGEventSetFlags": (None, [vp, ctypes.c_uint64]),
            "CGEventSetIntegerValueField": (None, [vp, u32, ctypes.c_int64]),
            "CGEventPost": (None, [u32, vp]),
            "CGEventCreateScrollWheelEvent2": (vp, [vp, u32, u32, ctypes.c_int32, ctypes.c_int32, ctypes.c_int32]),
        }
        for fname, (res, args) in sig.items():
            fn = getattr(c, fname)
            fn.restype, fn.argtypes = res, args
        pre = getattr(c, "CGPreflightScreenCaptureAccess", None)
        if pre is not None:
            pre.restype, pre.argtypes = ctypes.c_bool, []
        self.cf.CFRelease.restype, self.cf.CFRelease.argtypes = None, [vp]
        self.cf.CFStringCreateWithCString.restype = vp
        self.cf.CFStringCreateWithCString.argtypes = [vp, ctypes.c_char_p, u32]
        if self.ax is not None:
            self.ax.AXIsProcessTrusted.restype, self.ax.AXIsProcessTrusted.argtypes = ctypes.c_bool, []
            self.ax.AXUIElementCreateSystemWide.restype, self.ax.AXUIElementCreateSystemWide.argtypes = vp, []
            self.ax.AXUIElementCopyAttributeValue.restype = ctypes.c_int32
            self.ax.AXUIElementCopyAttributeValue.argtypes = [vp, vp, ctypes.POINTER(vp)]
            self.ax.AXUIElementGetPid.restype = ctypes.c_int32
            self.ax.AXUIElementGetPid.argtypes = [vp, ctypes.POINTER(ctypes.c_int32)]
            timeout = getattr(self.ax, "AXUIElementSetMessagingTimeout", None)
            if timeout is not None:
                timeout.restype, timeout.argtypes = ctypes.c_int32, [vp, ctypes.c_float]

    # -- permissions -------------------------------------------------------------------
    def trusted(self) -> Optional[bool]:
        if self.ax is None:
            return None
        try:
            return bool(self.ax.AXIsProcessTrusted())
        except Exception:
            return None

    def can_record(self) -> Optional[bool]:
        fn = getattr(self.cg, "CGPreflightScreenCaptureAccess", None)
        if fn is None:
            return None
        try:
            return bool(fn())
        except Exception:
            return None

    def _require_input(self) -> None:
        if self.trusted() is False:
            raise PilotError(AX_HINT)

    # -- geometry ----------------------------------------------------------------------
    def size(self) -> Tuple[int, int]:
        r = self.cg.CGDisplayBounds(self.cg.CGMainDisplayID())
        return int(r.size.width), int(r.size.height)

    def cursor(self) -> Tuple[int, int]:
        ev = self.cg.CGEventCreate(None)
        if not ev:
            raise PilotError("CGEventCreate failed")
        try:
            p = self.cg.CGEventGetLocation(ev)
            return int(round(p.x)), int(round(p.y))
        finally:
            self.cf.CFRelease(ev)

    def front(self) -> Optional[Tuple[int, int]]:
        """(pid, 0) of the app with the keyboard focus, from the Accessibility API (live, unlike
        NSWorkspace in a process without a run loop). None without Accessibility access or on any error."""
        try:
            if self.ax is None or self.trusted() is False:
                return None
            if getattr(self, "_focused_attr", None) is None:
                self._focused_attr = self.cf.CFStringCreateWithCString(None, b"AXFocusedApplication", 0x08000100)
            wide = self.ax.AXUIElementCreateSystemWide()
            if not wide or not self._focused_attr:
                return None
            try:
                timeout = getattr(self.ax, "AXUIElementSetMessagingTimeout", None)
                if timeout is not None:
                    timeout(wide, 0.5)        # a hung app answers nothing: never wait the default 6 s on it
                app = ctypes.c_void_p()
                if self.ax.AXUIElementCopyAttributeValue(wide, self._focused_attr, ctypes.byref(app)) != 0 \
                        or not app.value:
                    return None
                try:
                    pid = ctypes.c_int32()
                    if self.ax.AXUIElementGetPid(app, ctypes.byref(pid)) != 0 or pid.value <= 0:
                        return None
                    return int(pid.value), 0
                finally:
                    self.cf.CFRelease(app)
            finally:
                self.cf.CFRelease(wide)
        except Exception:
            return None

    # -- events ------------------------------------------------------------------------
    def _post(self, ev) -> None:
        if not ev:
            raise PilotError("CoreGraphics refused to create the event")
        try:
            self.cg.CGEventPost(HID_TAP, ev)
        finally:
            self.cf.CFRelease(ev)

    def _mouse(self, etype: int, x: float, y: float, button: int = 0, state: int = 0) -> None:
        ev = self.cg.CGEventCreateMouseEvent(None, etype, CGPoint(float(x), float(y)), button)
        if ev and state:
            self.cg.CGEventSetIntegerValueField(ev, CLICK_STATE, state)
        self._post(ev)

    def move(self, x: int, y: int) -> None:
        with self.lock:
            self._require_input()
            self._mouse(MOUSE_MOVED, x, y)

    def click(self, x: int, y: int, button: str = "left", count: int = 1) -> None:
        plan = mouse_plan(button, count)
        with self.lock:
            self._require_input()
            self._mouse(MOUSE_MOVED, x, y)
            time.sleep(0.02)
            for i, (etype, btn, state) in enumerate(plan):
                self._mouse(etype, x, y, btn, state)
                time.sleep(0.012 if i % 2 == 0 else 0.05)

    def scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        w1, w2 = scroll_wheels(dx, dy)
        with self.lock:
            self._require_input()
            self._mouse(MOUSE_MOVED, x, y)
            steps = max(abs(w1), abs(w2), 1)
            for i in range(min(50, steps)):
                a = (1 if w1 > 0 else -1) if i < abs(w1) else 0
                b = (1 if w2 > 0 else -1) if i < abs(w2) else 0
                self._post(self.cg.CGEventCreateScrollWheelEvent2(None, SCROLL_LINE, 2, a, b, 0))
                time.sleep(0.01)

    def drag(self, x1: int, y1: int, x2: int, y2: int) -> None:
        down, up, dragged, btn = MOUSE["left"]
        with self.lock:
            self._require_input()
            self._mouse(MOUSE_MOVED, x1, y1)
            time.sleep(0.03)
            self._mouse(down, x1, y1, btn, 1)
            time.sleep(0.05)
            try:
                for px, py in lerp_path(x1, y1, x2, y2, drag_steps(x1, y1, x2, y2)):
                    self._mouse(dragged, px, py, btn)
                    time.sleep(0.012)
                time.sleep(0.03)
            finally:
                self._mouse(up, x2, y2, btn, 1)

    def _keyev(self, code: int, down: bool, flags: int = 0) -> None:
        ev = self.cg.CGEventCreateKeyboardEvent(None, code, down)
        if ev:
            self.cg.CGEventSetFlags(ev, flags)
        self._post(ev)

    def type_text(self, text: str, check=None) -> None:
        with self.lock:
            self._require_input()
            prev = ""
            for i, ch in enumerate(text):
                if ch == "\n" and prev == "\r":
                    prev = ch
                    continue
                prev = ch
                if check is not None and i % 24 == 0:
                    check()
                if ch in ("\n", "\r"):
                    self._keyev(36, True)
                    self._keyev(36, False)
                elif ch == "\t":
                    self._keyev(48, True)
                    self._keyev(48, False)
                else:
                    units = utf16_units(ch)
                    buf = (ctypes.c_uint16 * len(units))(*units)
                    for down in (True, False):
                        ev = self.cg.CGEventCreateKeyboardEvent(None, 0, down)
                        if ev:
                            self.cg.CGEventSetFlags(ev, 0)
                            self.cg.CGEventKeyboardSetUnicodeString(ev, len(units), buf)
                        self._post(ev)
                time.sleep(0.006)

    def keys(self, combos: Sequence[Combo], check=None) -> None:
        plans = [combo_plan(c) for c in combos]   # validate everything before pressing anything
        with self.lock:
            self._require_input()
            for plan in plans:
                if check is not None:
                    check()
                for code, down, flags in plan:
                    self._keyev(code, down, flags)
                    time.sleep(0.008)
                time.sleep(0.03)

    # -- capture -------------------------------------------------------------------------
    def capture(self, region: Optional[Tuple[int, int, int, int]] = None, max_w: int = 0, max_h: int = 0) -> Capture:
        from pulsar.pilot import png as PNG
        sc = "/usr/sbin/screencapture"
        if not os.path.exists(sc):
            raise Unsupported("screencapture not found")
        sw, sh = self.size()
        x0, y0, x1, y1 = region if region else (0, 0, sw, sh)
        tmp = tempfile.mkdtemp(prefix="pulsar-pilot-")
        try:
            src = os.path.join(tmp, "shot.png")
            cmd = [sc, "-x", "-t", "png"]
            if region:
                cmd += ["-R", "%d,%d,%d,%d" % (x0, y0, x1 - x0, y1 - y0)]
            else:
                cmd += ["-m"]
            res = self.run(cmd + [src], capture_output=True, timeout=20)
            if getattr(res, "returncode", 1) != 0 or not os.path.exists(src):
                raise PilotError("screencapture failed (is Screen Recording allowed for your terminal?)")
            with open(src, "rb") as fh:
                blob = fh.read()
            w, h = PNG.png_size(blob)
            tw, th = PNG.fit(w, h, max_w, max_h)
            if (tw, th) != (w, h):
                dst = os.path.join(tmp, "fit.png")
                sips = "/usr/bin/sips"        # the system's own, never a sips found first on PATH
                res = self.run([sips, "-z", str(th), str(tw), src, "--out", dst], capture_output=True, timeout=20)
                if getattr(res, "returncode", 1) != 0 or not os.path.exists(dst):
                    raise PilotError("sips could not resize the screenshot")
                with open(dst, "rb") as fh:
                    blob = fh.read()
                tw, th = PNG.png_size(blob)
            return Capture(tw, th, png=blob, screen=(x1 - x0, y1 - y0), origin=(x0, y0))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def info(self) -> Info:
        ax, sr = self.trusted(), self.can_record()
        hints = []
        if ax is False:
            hints.append(AX_HINT)
        if sr is False:
            hints.append(SR_HINT)
        w, h = self.size()
        return Info("macos", self.label, ok=ax is not False, detail="%dx%d pt" % (w, h), hints=hints,
                    perms={"accessibility": ax, "screen_recording": sr})
