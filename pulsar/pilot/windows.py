# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Windows backend: user32 SendInput / SetCursorPos, gdi32 BitBlt + GetDIBits.

The process opts into per-monitor DPI awareness first, so screen space is
physical pixels of the primary monitor for both input and capture. Structs
use fixed-width ctypes types (not ctypes.wintypes) so their layout can be
unit-tested on any OS; every handle is pointer-sized (c_void_p) so nothing
is truncated on 64-bit Windows.
"""

from __future__ import annotations

import ctypes
import threading
import time
from typing import List, Optional, Sequence, Tuple

from pulsar.pilot.backends import (Backend, Capture, Combo, Info, PilotError, Unsupported, drag_steps, lerp_path)

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0001, 0x0002, 0x0004
WHEEL_DELTA = 120
SM_CXSCREEN, SM_CYSCREEN = 0, 1
SRCCOPY, CAPTUREBLT = 0x00CC0020, 0x40000000

BUTTONS = {"left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP), "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
           "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP)}
VK = {
    "enter": 0x0D, "tab": 0x09, "escape": 0x1B, "space": 0x20, "backspace": 0x08, "delete": 0x2E, "left": 0x25,
    "up": 0x26, "right": 0x27, "down": 0x28, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "insert": 0x2D, "capslock": 0x14, "printscreen": 0x2C, "menu": 0x5D,
}
VK.update({"f%d" % i: 0x70 + i - 1 for i in range(1, 25)})
MOD_VK = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "cmd": 0x5B}
EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5D, 0x2C}

ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_int32), ("dy", ctypes.c_int32), ("mouseData", ctypes.c_uint32),
                ("dwFlags", ctypes.c_uint32), ("time", ctypes.c_uint32), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_uint16), ("wScan", ctypes.c_uint16), ("dwFlags", ctypes.c_uint32),
                ("time", ctypes.c_uint32), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_uint32), ("wParamL", ctypes.c_uint16), ("wParamH", ctypes.c_uint16)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("u", _INPUTUNION)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]


def mouse_input(flags: int, data: int = 0) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.u.mi = MOUSEINPUT(0, 0, ctypes.c_uint32(data & 0xFFFFFFFF).value, flags, 0, 0)
    return inp


def key_input(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    if vk in EXTENDED:
        flags |= KEYEVENTF_EXTENDEDKEY
    inp.u.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    return inp


def click_plan(button: str, count: int) -> List[int]:
    if button not in BUTTONS:
        raise PilotError("button must be left, right or middle")
    down, up = BUTTONS[button]
    return [down, up] * max(1, min(3, count))


def scroll_plan(dx: int, dy: int) -> List[Tuple[int, int]]:
    """[(flags, mouseData)] - WHEEL positive is away from the user (up); our dy>0 means down."""
    out = []
    if dy:
        out.append((MOUSEEVENTF_WHEEL, -int(dy) * WHEEL_DELTA))
    if dx:
        out.append((MOUSEEVENTF_HWHEEL, int(dx) * WHEEL_DELTA))
    return out


def vk_for(key: str, vkscan=None) -> Tuple[int, Tuple[str, ...]]:
    """(virtual key, extra modifiers) for a portable key name or character."""
    if key in VK:
        return VK[key], ()
    if len(key) == 1:
        if key.isascii() and key.isalnum():
            return ord(key.upper()), (("shift",) if key.isupper() else ())
        if vkscan is not None:
            res = int(vkscan(key))
            if res != -1 and (res & 0xFF) != 0xFF:
                extra = tuple(m for bit, m in ((1, "shift"), (2, "ctrl"), (4, "alt")) if (res >> 8) & bit)
                return res & 0xFF, extra
        raise PilotError("no key on this keyboard layout types '%s' (use pilot_type for text)" % key)
    raise PilotError("unknown key '%s' on Windows" % (key[4:] if key.startswith("raw:") else key))


def combo_plan(combo: Combo, vkscan=None) -> List[Tuple[int, bool]]:
    """[(vk, key down?)]: modifiers down, key down/up, modifiers up in reverse."""
    mods = list(combo.mods)
    vk = None
    if combo.key:
        vk, extra = vk_for(combo.key, vkscan)
        mods += [m for m in extra if m not in mods]
    order = [m for m in ("ctrl", "alt", "shift", "cmd") if m in mods]
    plan = [(MOD_VK[m], True) for m in order]
    if vk is not None:
        plan += [(vk, True), (vk, False)]
    plan += [(MOD_VK[m], False) for m in reversed(order)]
    return plan


def unicode_inputs(text: str) -> List[INPUT]:
    """KEYEVENTF_UNICODE down/up pairs, one per UTF-16 unit; newlines and tabs become real keys."""
    out: List[INPUT] = []
    prev = ""
    for ch in text:
        if ch == "\n" and prev == "\r":
            prev = ch
            continue
        prev = ch
        if ch in ("\r", "\n", "\t"):
            vk = VK["enter"] if ch != "\t" else VK["tab"]
            out += [key_input(vk), key_input(vk, flags=KEYEVENTF_KEYUP)]
            continue
        b = ch.encode("utf-16-le")
        for i in range(0, len(b), 2):
            unit = b[i] | (b[i + 1] << 8)
            out += [key_input(0, unit, KEYEVENTF_UNICODE), key_input(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]
    return out


def set_dpi_aware(user32) -> None:
    try:
        fn = user32.SetProcessDpiAwarenessContext
        fn.restype, fn.argtypes = ctypes.c_int, [ctypes.c_void_p]
        if fn(ctypes.c_void_p(-4)):   # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
            return
    except (AttributeError, OSError):
        pass
    try:
        user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


class WinBackend(Backend):
    name = "windows"
    label = "Windows (SendInput)"

    def __init__(self, user32=None, gdi32=None) -> None:
        self.lock = threading.RLock()
        if user32 is None:
            try:
                user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
                gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)  # type: ignore[attr-defined]
            except (AttributeError, OSError):
                raise Unsupported("user32/gdi32 unavailable")
            self.u, self.g = user32, gdi32
            self._bind()
            set_dpi_aware(user32)
        else:
            self.u, self.g = user32, gdi32

    def _bind(self) -> None:
        u, g, vp, ci, cu = self.u, self.g, ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32
        for fn, res, args in (
                (u.SendInput, cu, [cu, ctypes.POINTER(INPUT), ci]),
                (u.SetCursorPos, ci, [ci, ci]),
                (u.GetCursorPos, ci, [ctypes.POINTER(POINT)]),
                (u.GetSystemMetrics, ci, [ci]),
                (u.VkKeyScanW, ctypes.c_short, [ctypes.c_wchar]),
                (u.GetForegroundWindow, vp, []),
                (u.GetWindowThreadProcessId, cu, [vp, ctypes.POINTER(ctypes.c_uint32)]),
                (u.GetDC, vp, [vp]),
                (u.ReleaseDC, ci, [vp, vp]),
                (g.CreateCompatibleDC, vp, [vp]),
                (g.CreateCompatibleBitmap, vp, [vp, ci, ci]),
                (g.SelectObject, vp, [vp, vp]),
                (g.BitBlt, ci, [vp, ci, ci, ci, ci, vp, ci, ci, cu]),
                (g.GetDIBits, ci, [vp, vp, cu, cu, vp, ctypes.POINTER(BITMAPINFO), cu]),
                (g.DeleteObject, ci, [vp]),
                (g.DeleteDC, ci, [vp])):
            fn.restype, fn.argtypes = res, args

    def _send(self, inputs: Sequence[INPUT]) -> None:
        if not inputs:
            return
        arr = (INPUT * len(inputs))(*inputs)
        sent = self.u.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
        if sent != len(inputs):
            raise PilotError("Windows blocked synthetic input (an elevated window may be focused; "
                             "Pilot cannot drive apps running as administrator unless it runs elevated too)")

    def size(self) -> Tuple[int, int]:
        return int(self.u.GetSystemMetrics(SM_CXSCREEN)), int(self.u.GetSystemMetrics(SM_CYSCREEN))

    def cursor(self) -> Tuple[int, int]:
        p = POINT()
        if not self.u.GetCursorPos(ctypes.byref(p)):
            raise PilotError("GetCursorPos failed")
        return int(p.x), int(p.y)

    def front(self) -> Optional[Tuple[int, int]]:
        """(pid, window handle) of the foreground window; None when there is none or on any error."""
        try:
            hwnd = self.u.GetForegroundWindow()
            if not hwnd:
                return None
            pid = ctypes.c_uint32()
            self.u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            return int(pid.value), int(hwnd)
        except Exception:
            return None

    def move(self, x: int, y: int) -> None:
        with self.lock:
            if not self.u.SetCursorPos(int(x), int(y)):
                raise PilotError("SetCursorPos failed")

    def click(self, x: int, y: int, button: str = "left", count: int = 1) -> None:
        flags = click_plan(button, count)
        with self.lock:
            self.move(x, y)
            time.sleep(0.02)
            for i, f in enumerate(flags):
                self._send([mouse_input(f)])
                time.sleep(0.012 if i % 2 == 0 else 0.05)

    def scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        with self.lock:
            self.move(x, y)
            for flags, data in scroll_plan(max(-50, min(50, dx)), max(-50, min(50, dy))):
                self._send([mouse_input(flags, data)])
                time.sleep(0.01)

    def drag(self, x1: int, y1: int, x2: int, y2: int) -> None:
        with self.lock:
            self.move(x1, y1)
            time.sleep(0.03)
            self._send([mouse_input(MOUSEEVENTF_LEFTDOWN)])
            time.sleep(0.05)
            try:
                for px, py in lerp_path(x1, y1, x2, y2, drag_steps(x1, y1, x2, y2)):
                    self.u.SetCursorPos(px, py)
                    time.sleep(0.012)
                time.sleep(0.03)
            finally:
                self._send([mouse_input(MOUSEEVENTF_LEFTUP)])

    def type_text(self, text: str, check=None) -> None:
        with self.lock:
            for i in range(0, len(text), 24):
                if check is not None:
                    check()
                for inp in unicode_inputs(text[i:i + 24]):
                    self._send([inp])
                    time.sleep(0.002)

    def keys(self, combos: Sequence[Combo], check=None) -> None:
        plans = [combo_plan(c, self.u.VkKeyScanW) for c in combos]
        with self.lock:
            for plan in plans:
                if check is not None:
                    check()
                for vk, down in plan:
                    self._send([key_input(vk, flags=0 if down else KEYEVENTF_KEYUP)])
                    time.sleep(0.008)
                time.sleep(0.03)

    def capture(self, region: Optional[Tuple[int, int, int, int]] = None, max_w: int = 0, max_h: int = 0) -> Capture:
        sw, sh = self.size()
        x0, y0, x1, y1 = region if region else (0, 0, sw, sh)
        w, h = int(x1 - x0), int(y1 - y0)
        if w <= 0 or h <= 0:
            raise PilotError("empty capture region")
        with self.lock:
            sdc = self.u.GetDC(None)
            if not sdc:
                raise PilotError("GetDC failed")
            mdc = bmp = old = None
            try:
                mdc = self.g.CreateCompatibleDC(sdc)
                bmp = self.g.CreateCompatibleBitmap(sdc, w, h)
                if not mdc or not bmp:
                    raise PilotError("cannot allocate a %dx%d bitmap" % (w, h))
                old = self.g.SelectObject(mdc, bmp)
                if not self.g.BitBlt(mdc, 0, 0, w, h, sdc, int(x0), int(y0), SRCCOPY | CAPTUREBLT):
                    raise PilotError("BitBlt failed (is the session locked?)")
                self.g.SelectObject(mdc, old)      # GetDIBits needs the bitmap out of the DC
                old = None
                bmi = BITMAPINFO()
                bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
                bmi.bmiHeader.biWidth = w
                bmi.bmiHeader.biHeight = -h          # top-down rows
                bmi.bmiHeader.biPlanes = 1
                bmi.bmiHeader.biBitCount = 32
                bmi.bmiHeader.biCompression = 0      # BI_RGB
                buf = ctypes.create_string_buffer(w * h * 4)
                if self.g.GetDIBits(mdc, bmp, 0, h, ctypes.cast(buf, ctypes.c_void_p), ctypes.byref(bmi), 0) != h:
                    raise PilotError("GetDIBits failed")
                return Capture(w, h, buf.raw, "BGRX", screen=(w, h), origin=(int(x0), int(y0)))
            finally:
                if old:
                    self.g.SelectObject(mdc, old)
                if bmp:
                    self.g.DeleteObject(bmp)
                if mdc:
                    self.g.DeleteDC(mdc)
                self.u.ReleaseDC(None, sdc)

    def info(self) -> Info:
        w, h = self.size()
        return Info("windows", self.label, detail="%dx%d px" % (w, h),
                    hints=["Apps running as administrator ignore input from a non-elevated Pilot."])
