# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""X11 backend: libX11 + libXtst through ctypes (no python-xlib, no xdotool).

  pointer   XTestFakeMotionEvent / XTestFakeButtonEvent, XQueryPointer
  keyboard  XTestFakeKeyEvent; characters missing from the keymap are bound
            to a spare keycode for the duration of the call (xdotool's trick)
  capture   XGetImage(root, ZPixmap) -> BGRX bytes -> pulsar.pilot.png

Xlib's default error handler calls exit(); a quiet handler is installed so
a bad request becomes a PilotError instead of killing the MCP server.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import threading
import time
from typing import Dict, List, Optional, Sequence, Tuple

from pulsar.pilot.backends import (Backend, Capture, Combo, Info, PilotError, Unsupported, drag_steps, lerp_path)

# Keysyms (X11/keysymdef.h) for the portable key names in backends.NAMED_KEYS.
KEYSYMS: Dict[str, int] = {
    "enter": 0xFF0D, "tab": 0xFF09, "escape": 0xFF1B, "space": 0x0020, "backspace": 0xFF08,
    "delete": 0xFFFF, "up": 0xFF52, "down": 0xFF54, "left": 0xFF51, "right": 0xFF53, "home": 0xFF50,
    "end": 0xFF57, "pageup": 0xFF55, "pagedown": 0xFF56, "insert": 0xFF63, "capslock": 0xFFE5,
    "printscreen": 0xFF61, "menu": 0xFF67,
}
KEYSYMS.update({"f%d" % i: 0xFFBE + i - 1 for i in range(1, 25)})
MOD_KEYSYMS = {"ctrl": 0xFFE3, "shift": 0xFFE1, "alt": 0xFFE9, "cmd": 0xFFEB}
BUTTONS = {"left": 1, "middle": 2, "right": 3}
TYPE_DELAY = 0.008
SCRATCH_HOLD = 1.5


def char_keysym(ch: str) -> int:
    cp = ord(ch)
    if ch in ("\n", "\r"):
        return KEYSYMS["enter"]
    if ch == "\t":
        return KEYSYMS["tab"]
    if ch == "\b":
        return KEYSYMS["backspace"]
    if 0x20 <= cp <= 0x7E or 0xA0 <= cp <= 0xFF:
        return cp
    return 0x01000000 | cp


def combo_keysyms(combo: Combo, lookup_name=None) -> Tuple[List[int], Optional[int]]:
    """(modifier keysyms, key keysym or None). lookup_name resolves 'raw:' X keysym names."""
    mods = [MOD_KEYSYMS[m] for m in combo.mods]
    key = combo.key
    if not key:
        return mods, None
    if key in KEYSYMS:
        return mods, KEYSYMS[key]
    if key.startswith("raw:"):
        ks = lookup_name(key[4:]) if lookup_name else 0
        if not ks:
            raise PilotError("unknown X keysym '%s'" % key[4:])
        return mods, ks
    if len(key) == 1:
        return mods, char_keysym(key)
    raise PilotError("unknown key '%s'" % key)


class XImage(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_int), ("height", ctypes.c_int), ("xoffset", ctypes.c_int), ("format", ctypes.c_int),
        ("data", ctypes.c_void_p), ("byte_order", ctypes.c_int), ("bitmap_unit", ctypes.c_int),
        ("bitmap_bit_order", ctypes.c_int), ("bitmap_pad", ctypes.c_int), ("depth", ctypes.c_int),
        ("bytes_per_line", ctypes.c_int), ("bits_per_pixel", ctypes.c_int), ("red_mask", ctypes.c_ulong),
        ("green_mask", ctypes.c_ulong), ("blue_mask", ctypes.c_ulong), ("obdata", ctypes.c_void_p),
        ("f", ctypes.c_void_p * 6),
    ]


class XErrorEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("display", ctypes.c_void_p), ("resourceid", ctypes.c_ulong),
                ("serial", ctypes.c_ulong), ("error_code", ctypes.c_ubyte), ("request_code", ctypes.c_ubyte),
                ("minor_code", ctypes.c_ubyte)]


def pixel_mode(bits_per_pixel: int, byte_order: int, red: int, green: int, blue: int) -> str:
    """Map an XImage pixel layout to a png.py mode name (8-bit channels only)."""
    if bits_per_pixel not in (24, 32):
        raise Unsupported("unsupported X display depth (%d bits per pixel); use a 24-bit display" % bits_per_pixel)
    size = bits_per_pixel // 8

    def byte_of(mask: int) -> int:
        shift = (mask & -mask).bit_length() - 1 if mask else 0
        if not mask or shift % 8 or (mask >> shift) != 0xFF:
            raise Unsupported("unsupported X visual (channel mask 0x%x)" % mask)
        idx = shift // 8
        return idx if byte_order == 0 else size - 1 - idx
    names = {byte_of(red): "R", byte_of(green): "G", byte_of(blue): "B"}
    layout = "".join(names.get(i, "X") for i in range(size))
    if layout not in ("RGB", "BGR", "RGBX", "BGRX", "XRGB"):
        raise Unsupported("unsupported X pixel layout " + layout)
    return layout


_ERR = {"code": 0}
_HANDLERS: List[object] = []


def _load(name: str, fallback: str):
    path = ctypes.util.find_library(name) or fallback
    try:
        return ctypes.CDLL(path)
    except OSError:
        raise Unsupported("lib%s is not installed (needed for Pilot on X11)" % name)


class X11Backend(Backend):
    name = "x11"
    label = "X11 (XTest)"

    def __init__(self, display: Optional[str] = None) -> None:
        self.lock = threading.RLock()
        x = self.x = _load("X11", "libX11.so.6")
        t = self.xt = _load("Xtst", "libXtst.so.6")
        vp, ul, ci, cu = ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_uint
        pi, pu, pul = ctypes.POINTER(ci), ctypes.POINTER(cu), ctypes.POINTER(ul)
        sig = {
            "XInitThreads": (ci, []),
            "XOpenDisplay": (vp, [ctypes.c_char_p]), "XCloseDisplay": (ci, [vp]),
            "XDefaultScreen": (ci, [vp]), "XRootWindow": (ul, [vp, ci]),
            "XGetGeometry": (ci, [vp, ul, pul, pi, pi, pu, pu, pu, pu]),
            "XQueryPointer": (ci, [vp, ul, pul, pul, pi, pi, pi, pi, pu]),
            "XStringToKeysym": (ul, [ctypes.c_char_p]),
            "XDisplayKeycodes": (ci, [vp, pi, pi]),
            "XGetKeyboardMapping": (pul, [vp, ctypes.c_ubyte, ci, pi]),
            "XChangeKeyboardMapping": (ci, [vp, ci, ci, pul, ci]),
            "XGetImage": (ctypes.POINTER(XImage), [vp, ul, ci, ci, cu, cu, ul, ci]),
            "XFree": (ci, [vp]), "XSync": (ci, [vp, ci]), "XFlush": (ci, [vp]),
            "XInternAtom": (ul, [vp, ctypes.c_char_p, ci]),
            "XGetWindowProperty": (ci, [vp, ul, ul, ctypes.c_long, ctypes.c_long, ci, ul, pul, pi, pul, pul,
                                        ctypes.POINTER(ctypes.c_void_p)]),
        }
        for fname, (res, args) in sig.items():
            fn = getattr(x, fname)
            fn.restype, fn.argtypes = res, args
        for fname, args in {"XTestFakeMotionEvent": [vp, ci, ci, ci, ul],
                            "XTestFakeButtonEvent": [vp, cu, ci, ul],
                            "XTestFakeKeyEvent": [vp, cu, ci, ul],
                            "XTestQueryExtension": [vp, pi, pi, pi, pi]}.items():
            fn = getattr(t, fname)
            fn.restype, fn.argtypes = ci, args
        self._destroy = getattr(x, "XDestroyImage", None)
        if self._destroy is not None:
            self._destroy.restype, self._destroy.argtypes = ci, [ctypes.POINTER(XImage)]
        if not _HANDLERS:
            x.XInitThreads()
            proto = ctypes.CFUNCTYPE(ci, vp, vp)

            def on_error(dpy, event):
                try:
                    _ERR["code"] = int(ctypes.cast(event, ctypes.POINTER(XErrorEvent)).contents.error_code)
                except Exception:
                    _ERR["code"] = -1
                return 0
            handler = proto(on_error)
            _HANDLERS.append(handler)
            x.XSetErrorHandler.restype = vp
            x.XSetErrorHandler.argtypes = [proto]
            x.XSetErrorHandler(handler)
        self.dpy = x.XOpenDisplay(display.encode() if display else None)
        if not self.dpy:
            raise Unsupported("cannot open X display %s" % (display or "(DISPLAY)"))
        self.screen = x.XDefaultScreen(self.dpy)
        self.root = x.XRootWindow(self.dpy, self.screen)
        a, b, c, d = ci(), ci(), ci(), ci()
        if not t.XTestQueryExtension(self.dpy, ctypes.byref(a), ctypes.byref(b), ctypes.byref(c), ctypes.byref(d)):
            x.XCloseDisplay(self.dpy)
            self.dpy = None
            raise Unsupported("the X server has no XTEST extension")
        self._keymap: List[List[int]] = []
        self._index: Dict[int, Tuple[int, int]] = {}
        self._min_kc = 8
        self._per = 2
        self._scratch: Dict[int, int] = {}       # keysym -> borrowed keycode
        self._scratch_order: List[int] = []
        self._release_timer: Optional[threading.Timer] = None

    # -- geometry ----------------------------------------------------------------------
    def size(self) -> Tuple[int, int]:
        with self.lock:
            r = ctypes.c_ulong()
            xx, yy = ctypes.c_int(), ctypes.c_int()
            w, h, bw, dp = ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint()
            if not self.x.XGetGeometry(self.dpy, self.root, ctypes.byref(r), ctypes.byref(xx), ctypes.byref(yy),
                                       ctypes.byref(w), ctypes.byref(h), ctypes.byref(bw), ctypes.byref(dp)):
                raise PilotError("XGetGeometry failed")
            return int(w.value), int(h.value)

    def cursor(self) -> Tuple[int, int]:
        with self.lock:
            r, c = ctypes.c_ulong(), ctypes.c_ulong()
            rx, ry, wx, wy = ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
            m = ctypes.c_uint()
            self.x.XQueryPointer(self.dpy, self.root, ctypes.byref(r), ctypes.byref(c), ctypes.byref(rx),
                                 ctypes.byref(ry), ctypes.byref(wx), ctypes.byref(wy), ctypes.byref(m))
            return int(rx.value), int(ry.value)

    def front(self) -> Optional[Tuple[int, int]]:
        """(0, window) for the window manager's active window (_NET_ACTIVE_WINDOW); None without one."""
        with self.lock:
            try:
                atom = self.x.XInternAtom(self.dpy, b"_NET_ACTIVE_WINDOW", 1)   # only if it exists
                if not atom:
                    return None
                kind, fmt = ctypes.c_ulong(), ctypes.c_int()
                count, after = ctypes.c_ulong(), ctypes.c_ulong()
                data = ctypes.c_void_p()
                if self.x.XGetWindowProperty(self.dpy, self.root, atom, 0, 1, 0, 33, ctypes.byref(kind),   # XA_WINDOW
                                             ctypes.byref(fmt), ctypes.byref(count), ctypes.byref(after),
                                             ctypes.byref(data)) != 0:
                    return None
                try:
                    if fmt.value != 32 or count.value < 1 or not data.value:
                        return None
                    win = int(ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))[0])
                    return (0, win) if win else None
                finally:
                    if data.value:
                        self.x.XFree(data)
            except Exception:
                return None

    # -- pointer -----------------------------------------------------------------------
    def _motion(self, x: int, y: int) -> None:
        self.xt.XTestFakeMotionEvent(self.dpy, self.screen, int(x), int(y), 0)

    def _button(self, button: int, down: bool) -> None:
        self.xt.XTestFakeButtonEvent(self.dpy, button, 1 if down else 0, 0)

    def move(self, x: int, y: int) -> None:
        with self.lock:
            self._motion(x, y)
            self.x.XSync(self.dpy, 0)

    def click(self, x: int, y: int, button: str = "left", count: int = 1) -> None:
        btn = BUTTONS.get(button)
        if btn is None:
            raise PilotError("button must be left, right or middle")
        with self.lock:
            self._motion(x, y)
            self.x.XSync(self.dpy, 0)
            time.sleep(0.02)
            for i in range(max(1, min(3, count))):
                if i:
                    time.sleep(0.06)
                self._button(btn, True)
                self.x.XFlush(self.dpy)
                time.sleep(0.012)
                self._button(btn, False)
                self.x.XSync(self.dpy, 0)

    def scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        with self.lock:
            self._motion(x, y)
            self.x.XSync(self.dpy, 0)
            for amount, neg, pos in ((dy, 4, 5), (dx, 6, 7)):
                btn = pos if amount > 0 else neg
                for _ in range(min(50, abs(int(amount)))):
                    self._button(btn, True)
                    self._button(btn, False)
                    self.x.XFlush(self.dpy)
                    time.sleep(0.01)
            self.x.XSync(self.dpy, 0)

    def drag(self, x1: int, y1: int, x2: int, y2: int) -> None:
        with self.lock:
            self._motion(x1, y1)
            self.x.XSync(self.dpy, 0)
            time.sleep(0.03)
            self._button(1, True)
            self.x.XSync(self.dpy, 0)
            time.sleep(0.05)
            try:
                for px, py in lerp_path(x1, y1, x2, y2, drag_steps(x1, y1, x2, y2)):
                    self._motion(px, py)
                    self.x.XFlush(self.dpy)
                    time.sleep(0.012)
                time.sleep(0.03)
            finally:
                self._button(1, False)
                self.x.XSync(self.dpy, 0)

    # -- keyboard ----------------------------------------------------------------------
    def _load_keymap(self) -> None:
        lo, hi = ctypes.c_int(), ctypes.c_int()
        self.x.XDisplayKeycodes(self.dpy, ctypes.byref(lo), ctypes.byref(hi))
        per = ctypes.c_int()
        count = hi.value - lo.value + 1
        ptr = self.x.XGetKeyboardMapping(self.dpy, lo.value, count, ctypes.byref(per))
        if not ptr:
            raise PilotError("cannot read the X keyboard mapping")
        try:
            n = per.value
            flat = [int(ptr[i]) for i in range(count * n)]
        finally:
            self.x.XFree(ptr)
        self._min_kc = lo.value
        self._per = n
        self._keymap = [flat[i * n:(i + 1) * n] for i in range(count)]
        self._index = {}
        for level in (1, 0):  # level 0 wins
            for i, syms in enumerate(self._keymap):
                if level < len(syms) and syms[level]:
                    self._index[syms[level]] = (self._min_kc + i, level)

    def _find(self, keysym: int) -> Optional[Tuple[int, int]]:
        """(keycode, level) where level 0 = plain, 1 = shifted."""
        return self._index.get(keysym)

    def _bind_scratch(self, keysym: int) -> int:
        if keysym in self._scratch:
            return self._scratch[keysym]
        used = set(self._scratch.values())
        free = [self._min_kc + i for i, syms in enumerate(self._keymap)
                if not any(syms) and self._min_kc + i not in used]
        if free:
            kc = free[-1]
        elif self._scratch_order:
            old = self._scratch_order.pop(0)
            kc = self._scratch.pop(old)
            self._index.pop(old, None)
            time.sleep(0.03)  # let the target app consume events typed with the old binding
        else:
            raise PilotError("cannot type U+%04X: no spare keycode in the X keymap" % (keysym & 0xFFFFFF))
        arr = (ctypes.c_ulong * self._per)(*([keysym, keysym] + [0] * (self._per - 2)))
        self.x.XChangeKeyboardMapping(self.dpy, kc, self._per, arr, 1)
        self.x.XSync(self.dpy, 0)
        self._keymap[kc - self._min_kc] = [keysym, keysym] + [0] * (self._per - 2)
        self._index[keysym] = (kc, 0)
        self._scratch[keysym] = kc
        self._scratch_order.append(keysym)
        time.sleep(0.02)
        return kc

    def _schedule_release(self) -> None:
        """Keep borrowed keycodes bound for a while: an app translates a key event with the keymap it
        fetches when it gets to the event, so restoring right away could turn late keys into nothing."""
        if self._release_timer is not None:
            self._release_timer.cancel()
            self._release_timer = None
        if self._scratch:
            timer = threading.Timer(SCRATCH_HOLD, self._release_later)
            timer.daemon = True
            timer.start()
            self._release_timer = timer

    def _release_later(self) -> None:
        with self.lock:
            if self.dpy:
                self._release_scratch()

    def release_keys(self) -> None:
        """Return borrowed keycodes now (normally done SCRATCH_HOLD seconds after the last use)."""
        with self.lock:
            if self._release_timer is not None:
                self._release_timer.cancel()
                self._release_timer = None
            if self.dpy:
                self._release_scratch()

    def _release_scratch(self) -> None:
        if not self._scratch:
            return
        for ks, kc in self._scratch.items():
            self._index.pop(ks, None)
            arr = (ctypes.c_ulong * self._per)(*([0] * self._per))
            self.x.XChangeKeyboardMapping(self.dpy, kc, self._per, arr, 1)
            self._keymap[kc - self._min_kc] = [0] * self._per
        self.x.XSync(self.dpy, 0)
        self._scratch.clear()
        self._scratch_order = []

    def _resolve(self, keysym: int) -> Tuple[int, bool]:
        hit = self._find(keysym)
        if hit is None:
            return self._bind_scratch(keysym), False
        return hit[0], hit[1] == 1

    def _key(self, kc: int, down: bool) -> None:
        self.xt.XTestFakeKeyEvent(self.dpy, kc, 1 if down else 0, 0)

    def _tap(self, keysym: int, mods: Sequence[int] = ()) -> None:
        kc, need_shift = self._resolve(keysym)
        held = [self._resolve(m)[0] for m in mods]
        shift_kc = self._resolve(MOD_KEYSYMS["shift"])[0]
        if need_shift and shift_kc not in held:
            held.append(shift_kc)
        for m in held:
            self._key(m, True)
        self._key(kc, True)
        self.x.XFlush(self.dpy)
        time.sleep(0.004)
        self._key(kc, False)
        for m in reversed(held):
            self._key(m, False)
        self.x.XFlush(self.dpy)

    def type_text(self, text: str, check=None) -> None:
        with self.lock:
            self._load_keymap()
            try:
                prev = ""
                for i, ch in enumerate(text):
                    if ch == "\n" and prev == "\r":
                        prev = ch
                        continue
                    prev = ch
                    if check is not None and i % 24 == 0:
                        check()
                    self._tap(char_keysym(ch))
                    time.sleep(TYPE_DELAY)
                self.x.XSync(self.dpy, 0)
            finally:
                self._schedule_release()

    def keys(self, combos: Sequence[Combo], check=None) -> None:
        with self.lock:
            self._load_keymap()
            try:
                for combo in combos:
                    if check is not None:
                        check()
                    mods, key = combo_keysyms(combo, self._name_to_keysym)
                    if key is None:
                        kcs = [self._resolve(m)[0] for m in mods]
                        for kc in kcs:
                            self._key(kc, True)
                        time.sleep(0.01)
                        for kc in reversed(kcs):
                            self._key(kc, False)
                        self.x.XFlush(self.dpy)
                    else:
                        self._tap(key, mods)
                    time.sleep(0.03)
                self.x.XSync(self.dpy, 0)
            finally:
                self._schedule_release()

    def _name_to_keysym(self, name: str) -> int:
        return int(self.x.XStringToKeysym(name.encode("ascii", "replace")))

    # -- capture -------------------------------------------------------------------------
    def capture(self, region: Optional[Tuple[int, int, int, int]] = None, max_w: int = 0, max_h: int = 0) -> Capture:
        with self.lock:
            sw, sh = self.size()
            x0, y0, x1, y1 = region if region else (0, 0, sw, sh)
            x0, y0 = max(0, int(x0)), max(0, int(y0))
            x1, y1 = min(sw, int(x1)), min(sh, int(y1))
            w, h = x1 - x0, y1 - y0
            if w <= 0 or h <= 0:
                raise PilotError("empty capture region")
            _ERR["code"] = 0
            img = self.x.XGetImage(self.dpy, self.root, x0, y0, w, h, ctypes.c_ulong(-1).value, 2)
            if not img:
                raise PilotError("XGetImage failed (X error %s)" % _ERR["code"])
            try:
                im = img.contents
                mode = pixel_mode(im.bits_per_pixel, im.byte_order, im.red_mask, im.green_mask, im.blue_mask)
                raw = ctypes.string_at(im.data, im.bytes_per_line * im.height)
                from pulsar.pilot import png as PNG
                data = PNG.strip_stride(raw, im.width, im.height, im.bytes_per_line, len(mode))
                return Capture(im.width, im.height, data, mode, screen=(w, h), origin=(x0, y0))
            finally:
                if self._destroy is not None:
                    self._destroy(img)
                else:  # pragma: no cover - very old libX11
                    self.x.XFree(img.contents.data)
                    self.x.XFree(ctypes.cast(img, ctypes.c_void_p))

    def info(self) -> Info:
        w, h = self.size()
        return Info("x11", "X11 (XTest)", detail="%dx%d" % (w, h))

    def close(self) -> None:
        self.release_keys()
        with self.lock:
            if self.dpy:
                self.x.XCloseDisplay(self.dpy)
                self.dpy = None
