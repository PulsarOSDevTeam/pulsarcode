# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Pulsar Pilot: PNG pipeline, coordinate mapping, key parsing, per-OS planning
(mocked ctypes), safety rails, MCP tools, the cursor scene, and live X11
end-to-end tests against a private Xvfb (skipped when Xvfb is unavailable)."""

import base64
import ctypes
import ctypes.util
import io
import json
import math
import os
import random
import shutil
import struct
import subprocess
import sys
import textwrap
import time
import zlib

import pytest

import pulsar

from pulsar.pilot import backends as B
from pulsar.pilot import png as PNG
from pulsar.pilot import safety as S


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def png_decode(blob):
    """Minimal PNG decoder (8-bit RGB/RGBA, filters 0-4) used to verify the encoder."""
    assert blob[:8] == PNG.PNG_SIG
    pos, idat, w = 8, b"", 0
    while pos < len(blob):
        n = struct.unpack(">I", blob[pos:pos + 4])[0]
        tag = blob[pos + 4:pos + 8]
        body = blob[pos + 8:pos + 8 + n]
        crc = struct.unpack(">I", blob[pos + 8 + n:pos + 12 + n])[0]
        assert crc == zlib.crc32(tag + body) & 0xFFFFFFFF
        if tag == b"IHDR":
            w, h, depth, ctype = struct.unpack(">IIBB", body[:10])
            assert depth == 8
            ch = {2: 3, 6: 4}[ctype]
        elif tag == b"IDAT":
            idat += body
        pos += 12 + n
    raw = zlib.decompress(idat)
    stride = w * ch
    out = bytearray()
    prev = bytearray(stride)
    for y in range(h):
        f = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - ch] if i >= ch else 0
            b = prev[i]
            c = prev[i - ch] if i >= ch else 0
            if f == 1:
                line[i] = (line[i] + a) & 255
            elif f == 2:
                line[i] = (line[i] + b) & 255
            elif f == 3:
                line[i] = (line[i] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        out += line
        prev = line
    return w, h, ch, bytes(out)


def solid(w, h, rgb, mode="RGB"):
    r, g, b = rgb
    px = {"RGB": bytes((r, g, b)), "BGRX": bytes((b, g, r, 0)), "BGRA": bytes((b, g, r, 255)),
          "RGBA": bytes((r, g, b, 255))}[mode]
    return px * (w * h)


class FakeBackend(B.Backend):
    name = "fake"
    label = "Fake"

    def __init__(self, w=2560, h=1600, cursor=(800, 600)):
        self.w, self.h = w, h
        self.pos = cursor
        self.calls = []
        self.focus = None                  # (pid, window) of the focused app; None: this backend cannot tell
        self.reads = 0                     # how often the pointer was read

    def size(self):
        return self.w, self.h

    def cursor(self):
        pos = self.pos
        self.reads += 1
        return pos

    def front(self):
        return self.focus

    def move(self, x, y):
        self.calls.append(("move", x, y))
        self.pos = (x, y)

    def click(self, x, y, button="left", count=1):
        self.calls.append(("click", x, y, button, count))
        self.pos = (x, y)

    def scroll(self, x, y, dx, dy):
        self.calls.append(("scroll", x, y, dx, dy))

    def drag(self, x1, y1, x2, y2):
        self.calls.append(("drag", x1, y1, x2, y2))

    def type_text(self, text, check=None):
        if check:
            check()
        self.calls.append(("type", text))

    def keys(self, combos, check=None):
        for c in combos:
            if check:
                check()
            self.calls.append(("keys", [c.label()]))

    def capture(self, region=None, max_w=0, max_h=0):
        x0, y0, x1, y1 = region or (0, 0, self.w, self.h)
        w, h = x1 - x0, y1 - y0
        data = bytearray(solid(w, h, (10, 20, 30), "BGRX"))
        data[0:4] = bytes((255, 255, 255, 0))      # a white top-left pixel
        self.calls.append(("capture", region))
        return B.Capture(w, h, bytes(data), "BGRX", screen=(w, h), origin=(x0, y0))


class OpenGuard(S.Guard):
    def __init__(self):
        S.Guard.__init__(self, state_fn=lambda: (True, "test"))


class NoOverlay:
    clickthrough = True
    disabled = "test"

    def alive(self):
        return False

    def start(self):
        return False

    def send(self, op, wait=0.0):
        return None

    def close(self):
        pass


def make_pilot(backend=None, guard=None):
    from pulsar.pilot.tools import Pilot
    b = backend or FakeBackend()
    return Pilot(lambda: b, guard=guard or OpenGuard(), overlay=NoOverlay(), settle=0), b


# ---------------------------------------------------------------------------
# png.py
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["RGB", "BGRX", "BGRA", "RGBA"])
def test_png_roundtrip_formats(mode):
    rng = random.Random(3)
    w, h = 7, 5
    rgb = bytes(rng.randrange(256) for _ in range(w * h * 3))
    n = PNG.bpp(mode)
    data = bytearray(w * h * n)
    for i in range(w * h):
        r, g, b = rgb[i * 3:i * 3 + 3]
        px = {"RGB": (r, g, b), "BGRX": (b, g, r, 9), "BGRA": (b, g, r, 77), "RGBA": (r, g, b, 77)}[mode]
        data[i * n:(i + 1) * n] = bytes(px)
    dw, dh, ch, pix = png_decode(PNG.encode(w, h, bytes(data), mode))
    assert (dw, dh, ch) == (w, h, 3) and pix == rgb


def test_png_keep_alpha_and_single_row():
    data = bytes([1, 2, 3, 4, 5, 6, 7, 8])
    w, h, ch, pix = png_decode(PNG.encode(2, 1, data, "BGRA", keep_alpha=True))
    assert (w, h, ch) == (2, 1, 4) and pix == bytes([3, 2, 1, 4, 7, 6, 5, 8])
    assert PNG.png_size(PNG.encode(3, 2, bytes(18))) == (3, 2)
    with pytest.raises(PNG.ImageError):
        PNG.png_size(b"nope")
    with pytest.raises(PNG.ImageError):
        PNG.encode(2, 2, bytes(5))
    with pytest.raises(PNG.ImageError):
        PNG.bpp("CMYK")


def test_png_decodes_with_pillow_if_present():
    pil = pytest.importorskip("PIL.Image")
    blob = PNG.encode(4, 3, solid(4, 3, (200, 10, 99), "BGRX"), "BGRX")
    im = pil.open(io.BytesIO(blob))
    assert im.size == (4, 3) and im.convert("RGB").getpixel((3, 2)) == (200, 10, 99)


def test_strip_stride_and_crop():
    rows = [bytes([y, y, y]) * 2 + b"PAD!" for y in range(3)]
    assert PNG.strip_stride(b"".join(rows), 2, 3, 10, 3) == b"".join(bytes([y] * 6) for y in range(3))
    data = bytes(range(4 * 3 * 3))
    out, w, h = PNG.crop(data, 4, 3, (1, 1, 3, 3))
    assert (w, h) == (2, 2) and out[:3] == data[(4 + 1) * 3:(4 + 1) * 3 + 3]


def test_halve_is_a_rounded_box_filter():
    rng = random.Random(1)
    w, h = 6, 4
    src = bytes(rng.randrange(256) for _ in range(w * h * 3))
    out, w2, h2 = PNG.halve(src, w, h)
    assert (w2, h2) == (3, 2)
    for y in range(h2):
        for x in range(w2):
            for c in range(3):
                vals = [src[((2 * y + dy) * w + 2 * x + dx) * 3 + c] for dy in (0, 1) for dx in (0, 1)]
                assert out[(y * w2 + x) * 3 + c] == (sum(vals) + 2) // 4
    out, w2, h2 = PNG.halve(bytes(5 * 3 * 3), 5, 3)        # odd edges are dropped
    assert (w2, h2) == (2, 1) and len(out) == 6


def test_resample_sizes_and_content():
    w, h = 1920, 1080
    rgb = solid(w, h, (40, 80, 120))
    tw, th = PNG.fit(w, h, 1280, 1280)
    assert (tw, th) == (1280, 720)
    for smooth in (True, False):
        out = PNG.resample(rgb, w, h, tw, th, smooth=smooth)
        assert len(out) == tw * th * 3 and out[:3] == bytes((40, 80, 120)) and out[-3:] == bytes((40, 80, 120))
    assert PNG.fit(800, 600, 1280, 1280) == (800, 600)
    assert PNG.fit(1080, 1920, 1280, 1280) == (720, 1280)
    assert PNG.nearest(bytes([1, 2, 3, 4, 5, 6]), 2, 1, 1, 1) == bytes([4, 5, 6])


def test_resample_speed_is_interactive():
    w, h = 1920, 1080
    rgb = solid(w, h, (1, 2, 3))
    t = time.time()
    out = PNG.resample(rgb, w, h, 1280, 720)
    PNG.encode(1280, 720, out)
    assert time.time() - t < 3.0


# ---------------------------------------------------------------------------
# geometry + keys
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("screen,image", [((2560, 1600), (1280, 800)), ((1920, 1080), (1280, 720)),
                                          ((1440, 900), (1280, 800)), ((1280, 800), (1280, 800)),
                                          ((3840, 2160), (1280, 720))])
def test_geometry_roundtrip(screen, image):
    g = B.Geometry(screen[0], screen[1], image[0], image[1])
    rng = random.Random(5)
    for _ in range(200):
        x, y = rng.randrange(image[0]), rng.randrange(image[1])
        sx, sy = g.to_screen(x, y)
        assert 0 <= sx < screen[0] and 0 <= sy < screen[1]
        bx, by = g.to_image(sx, sy)
        assert abs(bx - x) <= 1 and abs(by - y) <= 1
    assert max(g.to_screen(0, 0)) <= 1
    lx, ly = g.to_screen(image[0] - 1, image[1] - 1)
    assert screen[0] - 2 <= lx <= screen[0] - 1 and screen[1] - 2 <= ly <= screen[1] - 1
    assert g.region_to_screen(0, 0, image[0], image[1]) == (0, 0, screen[0], screen[1])


def test_geometry_hidpi_mac_points_and_origin():
    # 1440x900-point display captured at 2880x1800 retina pixels, shown to the model at 1280x800
    g = B.Geometry(1440, 900, 1280, 800)
    assert g.to_screen(640, 400) == (720, 450)
    assert abs(g.scale - 1280 / 1440.0) < 1e-9
    z = B.Geometry(200, 100, 400, 200, origin_x=50, origin_y=30)     # a zoomed region
    assert z.to_screen(0, 0) == (50, 30) and z.to_screen(399, 199) == (249, 129)
    with pytest.raises(B.PilotError):
        g.check(1280, 5)
    with pytest.raises(B.PilotError):
        g.check(-1, 5)
    g.check(1279.5, 799)


def test_failsafe_corner_helpers():
    assert B.in_failsafe(0, 0) and B.in_failsafe(4, 4)
    assert not B.in_failsafe(5, 0) and not B.in_failsafe(0, 5) and not B.in_failsafe(-3, -3)
    assert B.nudge_out_of_corner(1, 2) == (5, 2) and B.nudge_out_of_corner(9, 2) == (9, 2)
    assert not B.in_failsafe(*B.nudge_out_of_corner(0, 0))


def test_parse_keys():
    c = B.parse_keys("ctrl+s")[0]
    assert c.mods == ("ctrl",) and c.key == "s" and c.is_shortcut
    c = B.parse_keys("Shift+Cmd+4")[0]
    assert c.mods == ("shift", "cmd") and c.key == "4"
    assert [x.label() for x in B.parse_keys("ctrl+a  ctrl+c Return")] == ["ctrl+a", "ctrl+c", "enter"]
    assert B.parse_keys("ctrl++")[0].key == "+" and B.parse_keys("+")[0].key == "+"
    assert B.parse_keys("ctrl+plus")[0].key == "+"
    assert B.parse_keys("super")[0] == B.Combo(("cmd",), "")
    assert B.parse_keys("option+ArrowLeft")[0] == B.Combo(("alt",), "left")
    assert B.parse_keys("F12")[0].key == "f12" and B.parse_keys("page_down")[0].key == "pagedown"
    assert B.parse_keys("ctrl+S")[0].key == "s"
    assert B.parse_keys("KP_Enter")[0].key == "enter"
    assert B.parse_keys("XF86AudioPlay")[0].key == "raw:XF86AudioPlay"
    for bad in ("", "   ", "a+b", "ctrl+bogus", "x " * 40):
        with pytest.raises(B.PilotError):
            B.parse_keys(bad)
    with pytest.raises(B.PilotError):
        B.parse_keys(None)  # type: ignore[arg-type]


def test_audit_label_masks_typing_keys():
    assert B.audit_label(B.parse_keys("ctrl+s enter p a shift+x alt+f4")) == "ctrl+s enter * * shift+* alt+f4"


def test_platform_kind():
    assert B.platform_kind({}, "darwin") == "macos"
    assert B.platform_kind({}, "win32") == "windows"
    assert B.platform_kind({"DISPLAY": ":0"}, "linux") == "x11"
    assert B.platform_kind({"DISPLAY": ":0", "WAYLAND_DISPLAY": "w"}, "linux") == "wayland"     # XWayland
    assert B.platform_kind({"DISPLAY": ":0", "WAYLAND_DISPLAY": "w", "PULSAR_PILOT_X11": "1"}, "linux") == "x11"
    assert B.platform_kind({"DISPLAY": ":0", "XDG_SESSION_TYPE": "wayland"}, "linux") == "wayland"
    assert B.platform_kind({"WAYLAND_DISPLAY": "wayland-0"}, "linux") == "wayland"
    assert B.platform_kind({}, "linux") == "none"


def test_wayland_backend_refuses_input():
    w = B.WaylandBackend()
    assert not w.can_act
    with pytest.raises(B.Unsupported):
        w.click(1, 1)
    with pytest.raises(B.Unsupported):
        w.type_text("x")


def test_import_is_cheap_and_lazy():
    code = ("import sys; import pulsar.pilot, pulsar.pilot.tools, pulsar.pilot.backends, pulsar.pilot.safety, "
            "pulsar.pilot.overlay, pulsar.pilot.cli; "
            "bad=[m for m in ('tkinter','pulsar.pilot.x11','pulsar.pilot.macos','pulsar.pilot.windows','fastapi',"
            "'httpx') if m in sys.modules]; print(bad)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


# ---------------------------------------------------------------------------
# X11 pure logic
# ---------------------------------------------------------------------------

def test_x11_keysyms_and_pixel_modes():
    from pulsar.pilot import x11 as X
    assert X.char_keysym("a") == 0x61 and X.char_keysym("é") == 0xE9 and X.char_keysym("€") == 0x10020AC
    assert X.char_keysym("\n") == X.KEYSYMS["enter"] and X.char_keysym("\t") == X.KEYSYMS["tab"]
    mods, key = X.combo_keysyms(B.parse_keys("ctrl+shift+f5")[0])
    assert mods == [0xFFE3, 0xFFE1] and key == 0xFFC2
    assert X.combo_keysyms(B.parse_keys("super")[0]) == ([0xFFEB], None)
    assert X.combo_keysyms(B.parse_keys("KP_Home")[0], lambda n: 0xFF95)[1] == 0xFF95
    with pytest.raises(B.PilotError):
        X.combo_keysyms(B.parse_keys("KP_Bogus")[0], lambda n: 0)
    assert X.pixel_mode(32, 0, 0xFF0000, 0xFF00, 0xFF) == "BGRX"
    assert X.pixel_mode(32, 1, 0xFF0000, 0xFF00, 0xFF) == "XRGB"
    assert X.pixel_mode(24, 0, 0xFF0000, 0xFF00, 0xFF) == "BGR"
    for bad in ((16, 0, 0xF800, 0x7E0, 0x1F), (32, 0, 0x3FF00000, 0xFFC00, 0x3FF)):
        with pytest.raises(B.Unsupported):
            X.pixel_mode(*bad)


# ---------------------------------------------------------------------------
# macOS logic with mocked CoreGraphics
# ---------------------------------------------------------------------------

def test_mac_plans():
    from pulsar.pilot import macos as M
    assert M.mouse_plan("left", 2) == [(1, 0, 1), (2, 0, 1), (1, 0, 2), (2, 0, 2)]
    assert M.mouse_plan("right", 1) == [(3, 1, 1), (4, 1, 1)]
    with pytest.raises(B.PilotError):
        M.mouse_plan("thumb", 1)
    plan = M.combo_plan(B.parse_keys("cmd+shift+4")[0])
    assert plan[0] == (55, True, 0x100000) and plan[1] == (56, True, 0x120000)
    assert plan[2] == (21, True, 0x120000) and plan[3] == (21, False, 0x120000)
    assert plan[-1] == (55, False, 0)
    assert M.combo_plan(B.parse_keys("enter")[0]) == [(36, True, 0), (36, False, 0)]
    shifted = M.combo_plan(B.parse_keys("cmd+?")[0])
    assert (56, True, 0x120000) in shifted and (44, True, 0x120000) in shifted
    assert M.key_for("A") == (0, True)
    with pytest.raises(B.PilotError):
        M.key_for("é")
    assert M.utf16_units("a") == [0x61] and M.utf16_units("😀") == [0xD83D, 0xDE00]
    assert M.scroll_wheels(0, 3) == (-3, 0) and M.scroll_wheels(-2, 0) == (0, 2)


class FakeCG:
    def __init__(self, w=1440, h=900):
        self.events = []
        self.posted = []
        self.w, self.h = w, h
        self.loc = (700.0, 400.0)

    def _ev(self, *desc):
        self.events.append(list(desc))
        return len(self.events)

    def CGMainDisplayID(self):
        return 1

    def CGDisplayBounds(self, did):
        from pulsar.pilot.macos import CGRect, CGPoint, CGSize
        return CGRect(CGPoint(0, 0), CGSize(self.w, self.h))

    def CGEventCreate(self, src):
        return self._ev("null")

    def CGEventGetLocation(self, ev):
        from pulsar.pilot.macos import CGPoint
        return CGPoint(*self.loc)

    def CGEventCreateMouseEvent(self, src, etype, pt, button):
        return self._ev("mouse", etype, round(pt.x), round(pt.y), button)

    def CGEventCreateKeyboardEvent(self, src, code, down):
        return self._ev("key", code, bool(down))

    def CGEventSetFlags(self, ev, flags):
        self.events[ev - 1].append(("flags", flags))

    def CGEventSetIntegerValueField(self, ev, field, value):
        self.events[ev - 1].append(("field", field, value))

    def CGEventKeyboardSetUnicodeString(self, ev, n, buf):
        self.events[ev - 1].append(("unicode", [buf[i] for i in range(n)]))

    def CGEventCreateScrollWheelEvent2(self, src, units, count, w1, w2, w3):
        return self._ev("scroll", w1, w2)

    def CGEventPost(self, tap, ev):
        self.posted.append(self.events[ev - 1])

    def CGPreflightScreenCaptureAccess(self):
        return True


class FakeCF:
    def CFRelease(self, ev):
        pass


class FakeAX:
    def __init__(self, ok=True):
        self.ok = ok

    def AXIsProcessTrusted(self):
        return self.ok


def test_mac_backend_events_with_mocked_cg(tmp_path):
    from pulsar.pilot import macos as M
    cg = FakeCG()
    mb = M.MacBackend(cg=cg, cf=FakeCF(), ax=FakeAX(True))
    assert mb.size() == (1440, 900) and mb.cursor() == (700, 400)
    mb.click(100, 200, "left", 2)
    kinds = [(e[1], e[2], e[3]) for e in cg.posted if e[0] == "mouse"]
    assert kinds[0] == (M.MOUSE_MOVED, 100, 200)
    states = [e[-1] for e in cg.posted if e[0] == "mouse" and e[1] in (1, 2)]
    assert states == [("field", 1, 1), ("field", 1, 1), ("field", 1, 2), ("field", 1, 2)]
    cg.posted.clear()
    mb.type_text("hi\n😀")
    uni = [e for e in cg.posted if e[0] == "key" and e[1] == 0]
    assert len(uni) == 6 and uni[-1][-1] == ("unicode", [0xD83D, 0xDE00])
    assert any(e[0] == "key" and e[1] == 36 for e in cg.posted)
    cg.posted.clear()
    mb.keys(B.parse_keys("cmd+s"))
    assert [(e[1], e[2]) for e in cg.posted] == [(55, True), (1, True), (1, False), (55, False)]
    cg.posted.clear()
    mb.scroll(10, 10, 0, 2)
    assert [e[1:3] for e in cg.posted if e[0] == "scroll"] == [[-1, 0], [-1, 0]]
    cg.posted.clear()
    mb.drag(0, 0, 100, 0)
    types = [e[1] for e in cg.posted if e[0] == "mouse"]
    assert types[1] == 1 and types[-1] == 2 and 6 in types
    denied = M.MacBackend(cg=FakeCG(), cf=FakeCF(), ax=FakeAX(False))
    with pytest.raises(B.PilotError) as ei:
        denied.click(1, 1)
    assert "Accessibility" in str(ei.value)
    assert denied.info().perms["accessibility"] is False


def test_mac_front_reads_the_focused_app_through_accessibility():
    from pulsar.pilot import macos as M

    class AX(FakeAX):
        def AXUIElementCreateSystemWide(self):
            return 11

        def AXUIElementCopyAttributeValue(self, element, attr, ref):
            ref._obj.value = 22
            return 0

        def AXUIElementGetPid(self, element, ref):
            ref._obj.value = 4321
            return 0

    class CF(FakeCF):
        def CFStringCreateWithCString(self, alloc, text, encoding):
            return 33
    assert M.MacBackend(cg=FakeCG(), cf=CF(), ax=AX(True)).front() == (4321, 0)
    assert M.MacBackend(cg=FakeCG(), cf=CF(), ax=AX(False)).front() is None       # no Accessibility access
    assert M.MacBackend(cg=FakeCG(), cf=FakeCF(), ax=FakeAX(True)).front() is None   # cannot tell: None
    # the check runs before each combo: a stop after the first one keeps the second from being pressed
    cg = FakeCG()
    mb = M.MacBackend(cg=cg, cf=CF(), ax=AX(True))
    seen = []

    def check():
        seen.append(1)
        if len(seen) > 1:
            raise B.PilotError("the focus moved")
    with pytest.raises(B.PilotError):
        mb.keys(B.parse_keys("cmd+a cmd+c"), check=check)
    assert len(seen) == 2 and [(e[1], e[2]) for e in cg.posted] == [(55, True), (0, True), (0, False), (55, False)]


def test_mac_capture_maps_retina_pixels_to_points(tmp_path, monkeypatch):
    from pulsar.pilot import macos as M
    calls = []

    def run(cmd, capture_output=True, timeout=0):
        calls.append(cmd)
        if cmd[0].endswith("screencapture"):
            open(cmd[-1], "wb").write(PNG.encode(288, 180, bytes(288 * 180 * 3)))   # 2x of a 144x90-pt screen
        else:   # sips -z h w src --out dst
            h, w = int(cmd[2]), int(cmd[3])
            open(cmd[-1], "wb").write(PNG.encode(w, h, bytes(w * h * 3)))

        class R:
            returncode = 0
        return R()
    monkeypatch.setattr(M.os.path, "exists", lambda p: True if p == "/usr/sbin/screencapture" else os.path.isfile(p))
    mb = M.MacBackend(cg=FakeCG(144, 90), cf=FakeCF(), ax=FakeAX(), run=run)
    cap = mb.capture(None, 128, 128)
    assert (cap.width, cap.height) == (128, 80) and cap.screen == (144, 90) and PNG.png_size(cap.png) == (128, 80)
    assert "-m" in calls[0] and "-x" in calls[0] and calls[1][:2] == [calls[1][0], "-z"]
    g = B.Geometry(cap.screen[0], cap.screen[1], cap.width, cap.height)
    assert g.to_screen(64, 40) == (72, 45)
    cap = mb.capture((10, 20, 50, 60), 1280, 1280)
    assert "-R" in calls[2] and "10,20,40,40" in calls[2] and cap.origin == (10, 20)


# ---------------------------------------------------------------------------
# Windows logic with mocked user32/gdi32
# ---------------------------------------------------------------------------

def test_windows_structs_and_plans():
    from pulsar.pilot import windows as W
    assert ctypes.sizeof(W.INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
    assert ctypes.sizeof(W.BITMAPINFOHEADER) == 40
    assert W.click_plan("right", 2) == [0x8, 0x10, 0x8, 0x10]
    assert W.scroll_plan(0, 3) == [(W.MOUSEEVENTF_WHEEL, -360)]
    assert W.scroll_plan(-2, 0) == [(W.MOUSEEVENTF_HWHEEL, -240)]
    assert W.mouse_input(W.MOUSEEVENTF_WHEEL, -120).u.mi.mouseData == 0xFFFFFF88
    assert W.combo_plan(B.parse_keys("ctrl+shift+s")[0]) == [(0x11, True), (0x10, True), (0x53, True),
                                                             (0x53, False), (0x10, False), (0x11, False)]
    assert W.combo_plan(B.parse_keys("cmd")[0]) == [(0x5B, True), (0x5B, False)]
    assert W.vk_for("A") == (0x41, ("shift",))
    assert W.vk_for("!", lambda ch: 0x0131) == (0x31, ("shift",))
    with pytest.raises(B.PilotError):
        W.vk_for("é", lambda ch: -1)
    up = W.key_input(0x25)
    assert up.u.ki.dwFlags & W.KEYEVENTF_EXTENDEDKEY
    inputs = W.unicode_inputs("a\r\n😀")
    assert len(inputs) == 2 + 2 + 4
    assert inputs[2].u.ki.wVk == 0x0D and inputs[4].u.ki.wScan == 0xD83D
    assert all(i.u.ki.dwFlags & W.KEYEVENTF_UNICODE for i in inputs[4:])


class FakeUser32:
    def __init__(self):
        self.sent = []
        self.pos = (5, 5)

    def SendInput(self, n, arr, size):
        for i in range(n):
            it = arr[i]
            self.sent.append(("mouse", it.u.mi.dwFlags, it.u.mi.mouseData) if it.type == 0 else
                             ("key", it.u.ki.wVk, it.u.ki.wScan, it.u.ki.dwFlags))
        return n

    def SetCursorPos(self, x, y):
        self.pos = (x, y)
        self.sent.append(("pos", x, y))
        return 1

    def GetCursorPos(self, ref):
        ref._obj.x, ref._obj.y = self.pos
        return 1

    def GetSystemMetrics(self, i):
        return (1920, 1080)[i]

    def VkKeyScanW(self, ch):
        return -1


def test_windows_backend_with_mocked_user32():
    from pulsar.pilot import windows as W
    u = FakeUser32()
    wb = W.WinBackend(user32=u, gdi32=object())
    assert wb.size() == (1920, 1080) and wb.cursor() == (5, 5)
    wb.click(10, 20, "left", 2)
    assert u.sent[0] == ("pos", 10, 20)
    assert [s[1] for s in u.sent[1:]] == [2, 4, 2, 4]
    u.sent.clear()
    wb.keys(B.parse_keys("ctrl+c"))
    assert [(s[1], bool(s[3] & W.KEYEVENTF_KEYUP)) for s in u.sent] == [(0x11, False), (0x43, False), (0x43, True),
                                                                       (0x11, True)]
    u.sent.clear()
    wb.type_text("é")
    assert u.sent == [("key", 0, 0xE9, W.KEYEVENTF_UNICODE), ("key", 0, 0xE9, W.KEYEVENTF_UNICODE | W.KEYEVENTF_KEYUP)]

    class Refuse(FakeUser32):
        def SendInput(self, n, arr, size):
            return 0
    with pytest.raises(B.PilotError):
        W.WinBackend(user32=Refuse(), gdi32=object()).click(1, 1)

    class Focused(FakeUser32):
        def GetForegroundWindow(self):
            return 0x1234

        def GetWindowThreadProcessId(self, hwnd, ref):
            ref._obj.value = 77
            return 1
    assert W.WinBackend(user32=Focused(), gdi32=object()).front() == (77, 0x1234)
    assert W.WinBackend(user32=FakeUser32(), gdi32=object()).front() is None      # cannot tell: None


def test_windows_capture_with_mocked_gdi32():
    from pulsar.pilot import windows as W
    log = []

    class U(FakeUser32):
        def GetDC(self, h):
            log.append("GetDC")
            return 7

        def ReleaseDC(self, h, dc):
            log.append("ReleaseDC")
            return 1

    class G:
        selected = None

        def CreateCompatibleDC(self, dc):
            return 8

        def CreateCompatibleBitmap(self, dc, w, h):
            return 9

        def SelectObject(self, dc, obj):
            prev, self.selected = self.selected or 3, obj
            log.append(("select", obj))
            return prev

        def BitBlt(self, *a):
            log.append("BitBlt")
            return 1

        def GetDIBits(self, dc, bmp, start, lines, buf, bmi, usage):
            assert self.selected != 9, "bitmap must be deselected before GetDIBits"
            hdr = bmi._obj.bmiHeader
            w, h = hdr.biWidth, -hdr.biHeight
            ctypes.memmove(buf, bytes((1, 2, 3, 0)) * (w * h), w * h * 4)
            log.append("GetDIBits")
            return h

        def DeleteObject(self, o):
            log.append("DeleteObject")
            return 1

        def DeleteDC(self, d):
            log.append("DeleteDC")
            return 1
    wb = W.WinBackend(user32=U(), gdi32=G())
    cap = wb.capture((10, 10, 14, 13))
    assert (cap.width, cap.height, cap.mode, cap.origin) == (4, 3, "BGRX", (10, 10))
    assert cap.data[:4] == bytes((1, 2, 3, 0)) and len(cap.data) == 48
    assert log[-3:] == ["DeleteObject", "DeleteDC", "ReleaseDC"] and "GetDIBits" in log


# ---------------------------------------------------------------------------
# safety.py
# ---------------------------------------------------------------------------

def test_state_gating():
    assert S.state({}, {})[0] is False
    assert S.state({"PULSAR_PILOT": "1"}, {})[0] is True
    assert S.state({}, {"pilot": True})[0] is True
    assert S.state({"PULSAR_PILOT": "0"}, {"pilot": True})[0] is False
    # `pilot off` after this process started stops it even when the launcher exported PULSAR_PILOT=1
    assert S.state({"PULSAR_PILOT": "1"}, {"pilot": False, "pilot_off_at": 200.0}, started=100.0)[0] is False
    # ...but a session started after the switch-off and explicitly enabled by env works
    assert S.state({"PULSAR_PILOT": "1"}, {"pilot": False, "pilot_off_at": 50.0}, started=100.0)[0] is True
    # the session was armed at 40 and switched off at 50: an MCP server restarted at 100 stays off
    armed = {"PULSAR_PILOT": "1", S.ARMED_AT: "40"}
    assert S.state(armed, {"pilot": False, "pilot_off_at": 50.0}, started=100.0)[0] is False
    assert S.state(dict(armed, **{S.ARMED_AT: "garbage"}), {"pilot": False, "pilot_off_at": 50.0}, started=100.0)[0]
    assert S.state({}, {"pilot": "yes"})[0] is False
    assert S.state({}, {"pilot": True, "pilot_off_at": "garbage"})[0] is True


def test_set_enabled_roundtrip_and_session_env(monkeypatch):
    monkeypatch.delenv("PULSAR_PILOT", raising=False)
    assert S.session_env() == {}
    S.set_enabled(True)
    assert S.enabled() and S.session_env() == {"PULSAR_PILOT": "1"}
    from pulsar import config as K
    assert K.load().prefs["pilot"] is True and "pilot_off_at" not in K.load().prefs
    S.set_enabled(False)
    prefs = K.load().prefs
    assert prefs["pilot"] is False and prefs["pilot_off_at"] >= S.PROCESS_START
    assert not S.enabled()


def test_guard_failsafe_trips_and_stays_tripped():
    g = OpenGuard()
    g.failsafe((300, 300))
    with pytest.raises(S.SafetyError) as ei:
        g.failsafe((1, 3))
    assert "top-left corner" in str(ei.value)
    with pytest.raises(S.SafetyError):
        g.failsafe((900, 900))
    with pytest.raises(S.SafetyError):
        g.require_enabled()


def test_guard_off_message():
    g = S.Guard(state_fn=lambda: (False, "off"))
    with pytest.raises(S.SafetyError) as ei:
        g.require_enabled()
    assert "pulsarcode pilot on" in str(ei.value)


def test_rate_limit_token_bucket():
    now = [0.0]
    slept = []
    g = S.Guard(clock=lambda: now[0], rate=10, burst=3, state_fn=lambda: (True, ""))

    def sleep(d):
        slept.append(d)
        now[0] += d
    for _ in range(3):
        g.throttle(sleep=sleep)
    assert slept == []
    g.throttle(sleep=sleep)
    assert len(slept) == 1 and abs(slept[0] - 0.1) < 1e-9
    with pytest.raises(S.SafetyError):
        g.throttle(max_wait=0.01, sleep=sleep)
    now[0] += 5
    g.throttle(sleep=sleep)
    assert len(slept) == 1


def test_audit_log_is_private_and_never_holds_text(pulsar_home):
    S.audit("type", chars=11, text="hunter2 secret", data="AAAA")
    S.audit("click", x=1, y=2, button="left\nINJECT=1")
    path = S.log_path()
    body = path.read_text()
    assert "chars=11" in body and "hunter2" not in body and "AAAA" not in body
    assert "\nINJECT" not in body and body.count("\n") == 2
    if os.name == "posix":
        assert (os.stat(str(path)).st_mode & 0o777) == 0o600


def test_audit_log_rotates(monkeypatch):
    monkeypatch.setattr(S, "LOG_MAX", 200)
    for i in range(20):
        S.audit("move", x=i, y=i)
    assert os.path.exists(str(S.log_path()) + ".1")
    assert S.log_path().stat().st_size < 400


# ---------------------------------------------------------------------------
# tools.py
# ---------------------------------------------------------------------------

def test_specs_are_valid_mcp_tools():
    from pulsar.pilot import tools as T
    specs = T.specs()
    names = [s["name"] for s in specs]
    assert names == ["pilot_screenshot", "pilot_click", "pilot_type", "pilot_key", "pilot_move", "pilot_scroll",
                     "pilot_drag", "pilot_cursor"]
    for s in specs:
        schema = s["inputSchema"]
        assert schema["type"] == "object" and isinstance(schema["properties"], dict)
        assert set(schema.get("required", [])) <= set(schema["properties"])
        assert callable(s["handler"]) and isinstance(s["annotations"], dict) and s["annotations"]["title"]
        json.dumps({k: v for k, v in s.items() if k != "handler"})
        assert "\u2014" not in s["description"]
    assert "screenshot" in specs[1]["description"].lower()
    assert "first" in specs[0]["description"].lower()
    # the screenshot leaves this machine for the model: never announced as read-only
    assert specs[0]["annotations"]["readOnlyHint"] is False and specs[0]["annotations"]["openWorldHint"] is True


def test_screenshot_scales_and_maps_clicks():
    p, b = make_pilot()
    content = p.screenshot({})
    img, txt = content
    assert img["type"] == "image" and img["mimeType"] == "image/png"
    w, h, ch, pix = png_decode(base64.b64decode(img["data"]))
    assert (w, h) == (1280, 800) and pix[3:6] == bytes((10, 20, 30))
    assert "1280x800" in txt["text"] and "2560x1600" in txt["text"] and "0.500" in txt["text"]
    assert "Pointer at (400, 300)" in txt["text"]
    out = p.click({"x": 100, "y": 50, "label": "Save"})
    assert out[0]["text"] == "Clicked left at (100, 50)."
    _, sx, sy, button, count = b.calls[-1]
    assert abs(sx - 200) <= 1 and abs(sy - 100) <= 1 and (button, count) == ("left", 1)
    p.click({"x": 0, "y": 0, "button": "right", "double": True})
    assert b.calls[-1][1:] == (5, 1, "right", 2)          # nudged out of the failsafe corner
    log = S.log_path().read_text()
    assert "action=screenshot" in log and "action=click" in log and "Save" not in log


def test_tool_errors_are_mcp_tool_errors():
    from pulsar.mcp.server import ToolError
    p, b = make_pilot()
    p.screenshot({})
    for args in ({"x": 5000, "y": 1}, {"x": "nan", "y": 1}, {"x": True, "y": 1}, {"y": 3},
                 {"x": 1, "y": 1, "button": "thumb"}):
        with pytest.raises(ToolError):
            p.click(args)
    with pytest.raises(ToolError):
        p.type_text({"text": ""})
    with pytest.raises(ToolError):
        p.type_text({"text": "x" * (B.MAX_TYPE_CHARS + 1)})
    with pytest.raises(ToolError):
        p.key({"keys": "ctrl+nope"})
    with pytest.raises(ToolError):
        p.scroll({"x": 1, "y": 1})
    with pytest.raises(ToolError):
        p.screenshot({"zoom": [1, 2, 3]})
    assert not any(c[0] in ("click", "type", "keys", "scroll") for c in b.calls)


def test_resolution_change_requires_a_new_screenshot():
    from pulsar.mcp.server import ToolError
    p, b = make_pilot()
    p.screenshot({})
    b.w, b.h = 1920, 1080
    with pytest.raises(ToolError) as ei:
        p.click({"x": 10, "y": 10})
    assert "new pilot_screenshot" in str(ei.value)
    p.screenshot({})
    p.click({"x": 640, "y": 360})
    assert b.calls[-1][1:3] == (960, 540)


def test_disabled_pilot_refuses_everything(monkeypatch):
    from pulsar.mcp.server import ToolError
    from pulsar.pilot.tools import Pilot
    monkeypatch.delenv("PULSAR_PILOT", raising=False)
    b = FakeBackend()
    p = Pilot(lambda: b, overlay=NoOverlay())
    for call in (lambda: p.screenshot({}), lambda: p.click({"x": 1, "y": 1}), lambda: p.cursor({})):
        with pytest.raises(ToolError) as ei:
            call()
        assert "pulsarcode pilot on" in str(ei.value)
    assert b.calls == []


def test_failsafe_stops_the_session():
    from pulsar.mcp.server import ToolError
    b = FakeBackend(cursor=(2, 1))
    p, _ = make_pilot(b)
    with pytest.raises(ToolError) as ei:
        p.click({"x": 500, "y": 500})
    assert "failsafe" in str(ei.value)
    b.pos = (900, 900)
    with pytest.raises(ToolError):
        p.move({"x": 10, "y": 10})
    assert b.calls == []
    assert "action=failsafe" in S.log_path().read_text()


def test_failsafe_checked_while_typing():
    from pulsar.mcp.server import ToolError

    class Grab(FakeBackend):
        def type_text(self, text, check=None):
            self.pos = (0, 0)      # the human slams the mouse into the corner mid-typing
            check()
            self.calls.append(("type", text))
    b = Grab()
    p, _ = make_pilot(b)
    with pytest.raises(ToolError):
        p.type_text({"text": "hello world"})
    assert not any(c[0] == "type" for c in b.calls)


class SlamOverlay(NoOverlay):
    """The human slams the mouse into the corner while the pulsar cursor animates, maybe leaving it again."""
    disabled = ""

    def __init__(self, backend, leave_after=None):
        self.b, self.leave_after = backend, leave_after

    def alive(self):
        return True

    def start(self):
        return True

    def send(self, op, wait=0.0):
        if op.get("op") not in ("scan", "hide"):
            self.b.pos = (1, 1)
            if self.leave_after is not None:
                seen, end = self.b.reads, time.time() + 10
                while self.b.reads == seen and time.time() < end:     # until the watch has looked once
                    time.sleep(0.005)
                time.sleep(self.leave_after)
                self.b.pos = (300, 300)
        return None


def test_failsafe_corner_slammed_while_the_cursor_glides_stops_the_action():
    from pulsar.mcp.server import ToolError
    from pulsar.pilot.tools import Pilot
    for leave in (None, 0.0):                          # still in the corner, or already out of it again
        b = FakeBackend()
        p = Pilot(lambda: b, guard=OpenGuard(), overlay=SlamOverlay(b, leave), settle=0)
        with pytest.raises(ToolError) as ei:
            p.click({"x": 350, "y": 200})
        assert "failsafe" in str(ei.value) and not any(c[0] == "click" for c in b.calls), leave
        b.pos = (900, 900)
        with pytest.raises(ToolError):                 # and Pilot stays stopped
            p.move({"x": 10, "y": 10})
        assert not any(c[0] == "move" for c in b.calls)
    for name, args in (("move", {"x": 10, "y": 10}), ("scroll", {"x": 10, "y": 10, "dy": 1}),
                       ("drag", {"x1": 1, "y1": 1, "x2": 50, "y2": 50}), ("key", {"keys": "ctrl+s"}),
                       ("type_text", {"text": "hi"})):
        b = FakeBackend()
        p = Pilot(lambda: b, guard=OpenGuard(), overlay=SlamOverlay(b), settle=0)
        with pytest.raises(ToolError):
            getattr(p, name)(args)
        assert not [c for c in b.calls if c[0] != "capture"], name


def test_corner_watch_latches_a_brief_slam_and_stops_before_pilot_moves():
    from pulsar.pilot.tools import CornerWatch
    b = FakeBackend()
    g = OpenGuard()
    with CornerWatch(g, b, every=0.01):
        b.pos = (2, 2)
        end = time.time() + 10
        while not g.tripped and time.time() < end:
            time.sleep(0.005)
        b.pos = (400, 400)
    assert g.tripped                                   # and it stays tripped once the pointer has left
    calm = OpenGuard()
    with CornerWatch(calm, b, every=0.01):
        time.sleep(0.05)
    b.pos = (0, 0)                                     # Pilot's own moves come after the watch
    time.sleep(0.05)
    assert not calm.tripped


def test_input_is_refused_when_the_window_in_front_changed():
    from pulsar.mcp.server import ToolError
    p, b = make_pilot()
    b.focus = (101, 0)
    p.screenshot({})
    b.focus = (202, 0)                                 # the user switched apps after the screenshot
    for call in (lambda: p.type_text({"text": "deploy now\n"}), lambda: p.click({"x": 10, "y": 10}),
                 lambda: p.key({"keys": "enter"}), lambda: p.scroll({"x": 5, "y": 5, "dy": 1}),
                 lambda: p.drag({"x1": 1, "y1": 1, "x2": 9, "y2": 9}), lambda: p.move({"x": 3, "y": 3})):
        with pytest.raises(ToolError) as ei:
            call()
        assert "new pilot_screenshot" in str(ei.value)
    assert not [c for c in b.calls if c[0] != "capture"]
    p.screenshot({})
    assert p.type_text({"text": "ok"})[0]["text"] == "Typed 2 characters."

    class Switch(FakeBackend):
        def type_text(self, text, check=None):
            check()
            self.focus = (303, 0)                      # focus moves away halfway through the text
            check()
            self.calls.append(("type", text))
    b2 = Switch()
    b2.focus = (101, 0)
    p2, _ = make_pilot(b2)
    p2.screenshot({})
    with pytest.raises(ToolError):
        p2.type_text({"text": "x" * 50})
    assert not any(c[0] == "type" for c in b2.calls)
    p3, b3 = make_pilot()                              # a backend that cannot tell never blocks
    p3.screenshot({})
    p3.click({"x": 10, "y": 10})
    assert b3.calls[-1][0] == "click"


def test_typing_is_refused_while_the_cursor_overlay_has_the_focus():
    from pulsar.mcp.server import ToolError
    from pulsar.pilot.tools import Pilot

    class Overlay(NoOverlay):
        def pid(self):
            return 4242
    b = FakeBackend()
    b.focus = (4242, 0)
    p = Pilot(lambda: b, guard=OpenGuard(), overlay=Overlay(), settle=0)
    p.screenshot({})
    with pytest.raises(ToolError) as ei:
        p.type_text({"text": "hi"})
    assert "overlay" in str(ei.value) and not any(c[0] == "type" for c in b.calls)
    p.click({"x": 10, "y": 10})                        # a click still reaches the window under the pointer
    assert b.calls[-1][0] == "click"


def test_shortcuts_that_end_the_session_are_refused():
    from pulsar.mcp.server import ToolError
    refused = {"macos": ("cmd+alt+shift+q", "cmd+shift+q", "ctrl+cmd+q", "cmd+alt+escape", "ctrl+alt+delete",
                         "XF86PowerOff"),
               "windows": ("cmd+l", "ctrl+alt+delete", "XF86Sleep"),
               "x11": ("cmd+l", "ctrl+alt+backspace", "ctrl+alt+f2", "XF86Sleep", "XF86LogOff", "XF86Suspend")}
    for kind, specs in refused.items():
        for keys in specs:
            with pytest.raises(B.PilotError):
                B.refuse_keys(B.parse_keys(keys), kind)
    B.refuse_keys(B.parse_keys("cmd+l cmd+q ctrl+s alt+f4 ctrl+alt+t XF86AudioPlay"), "macos")
    B.refuse_keys(B.parse_keys("ctrl+l alt+f4 ctrl+shift+escape"), "windows")
    p, b = make_pilot()
    with pytest.raises(ToolError) as ei:
        p.key({"keys": "ctrl+a ctrl+alt+delete"})
    assert "Ask the user" in str(ei.value) and not any(c[0] == "keys" for c in b.calls)


def test_overlay_gets_no_credentials():
    from pulsar.pilot.tools import overlay_env
    env = overlay_env({"PATH": "/bin", "DISPLAY": ":0", "ANTHROPIC_AUTH_TOKEN": "pls_x", "PULSAR_SESSION_TOKEN": "pls_y",
                       "OPENAI_API_KEY": "sk-z", "HF_TOKEN": "hf_q", "MY_ROUTER": "pla_admin",
                       "PULSAR_PILOT_OVERLAY_SCALE": "2"})
    assert env == {"PATH": "/bin", "DISPLAY": ":0", "PULSAR_PILOT_OVERLAY_SCALE": "2"}


def test_a_broken_audit_log_is_reported_once(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(S, "_AUDIT_PROBLEM", {"error": "", "told": False})
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x")
    monkeypatch.setattr(S, "log_path", lambda: blocker / "pilot.log")
    p, b = make_pilot()
    out = p.click({"x": 10, "y": 10})
    assert "audit log could not be written" in out[-1]["text"] and b.calls[-1][0] == "click"
    assert "audit log could not be written" in capsys.readouterr().err
    out = p.click({"x": 20, "y": 20})
    assert all("audit" not in c.get("text", "") for c in out)


def test_type_key_scroll_drag_move_cursor_and_zoom():
    p, b = make_pilot()
    assert p.cursor({})[0]["text"].startswith("Pointer at (400, 300)")
    assert p.type_text({"text": "secret pass"})[0]["text"] == "Typed 11 characters."
    assert b.calls[-1] == ("type", "secret pass")
    assert p.key({"keys": "ctrl+a ctrl+c"})[0]["text"] == "Pressed ctrl+a ctrl+c."
    assert p.scroll({"x": 640, "y": 400, "dy": 99})[0]["text"] == "Scrolled down 50 at (640, 400)."
    p.drag({"x1": 10, "y1": 10, "x2": 100, "y2": 100})
    assert b.calls[-1][0] == "drag" and abs(b.calls[-1][3] - 200) <= 1
    p.move({"x": 1279, "y": 799})
    assert b.calls[-1] == ("move", 2559, 1599)
    p.screenshot({})
    z = p.screenshot({"zoom": [0, 0, 64, 40]})
    w, h, _, _ = png_decode(base64.b64decode(z[0]["data"]))
    assert (w, h) == (128, 80) and "Zoomed" in z[1]["text"]
    assert p.geom.image_w == 1280                       # zoom never changes the action mapping
    log = S.log_path().read_text()
    assert "chars=11" in log and "secret" not in log and "keys=ctrl+a,ctrl+c" in log


def test_mcp_server_registers_pilot_when_enabled(monkeypatch):
    from pulsar.mcp.server import Server
    from pulsar.pilot import tools as T
    monkeypatch.setenv("PULSAR_PILOT", "1")
    p, b = make_pilot()
    monkeypatch.setattr(T, "_PILOT", p)
    srv = Server(None)
    tools = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    assert {"pilot_screenshot", "pilot_click", "pilot_cursor"} <= {t["name"] for t in tools}
    json.dumps(tools)
    r = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "pilot_click", "arguments": {"x": 9999, "y": 1}}})
    assert r["result"]["isError"] is True and "outside" in r["result"]["content"][0]["text"]
    r = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "pilot_screenshot", "arguments": {}}})
    assert r["result"]["isError"] is False and r["result"]["content"][0]["type"] == "image"


def test_overlay_blocker_reasons():
    from pulsar.pilot import tools as T
    assert "PULSAR_PILOT_OVERLAY" in T.overlay_blocker({"PULSAR_PILOT_OVERLAY": "0", "DISPLAY": ":1"})
    assert "PULSAR_FX" in T.overlay_blocker({"PULSAR_FX": "off", "DISPLAY": ":1"})
    if sys.platform.startswith("linux"):
        assert T.overlay_blocker({"WAYLAND_DISPLAY": "w"}) != ""


FAKE_OVERLAY = textwrap.dedent('''
    import json, sys
    print(json.dumps({"hello": 1, "ok": True, "clickthrough": True, "w": 800, "h": 600}), flush=True)
    for line in sys.stdin:
        m = json.loads(line)
        if m.get("op") == "quit":
            break
        if "ack" in m:
            print(json.dumps({"ack": m["ack"], "visible": m.get("op") != "hide", "was_visible": True,
                              "clickthrough": True}), flush=True)
''')


def test_overlay_client_protocol(tmp_path, monkeypatch):
    from pulsar.pilot.tools import OverlayClient
    (tmp_path / "fake_pulsar_overlay.py").write_text(FAKE_OVERLAY)
    monkeypatch.setattr(pulsar, "ROOT", str(tmp_path))   # the bootstrap ignores PYTHONPATH (-E)
    c = OverlayClient(python=sys.executable, module="fake_pulsar_overlay")
    try:
        assert c.start() and c.hello["w"] == 800 and c.clickthrough
        ack = c.send({"op": "aim", "x": 1, "y": 2}, wait=5)
        assert ack and ack["visible"] is True
        ack = c.send({"op": "hide"}, wait=5)
        assert ack and ack["visible"] is False and ack["was_visible"] is True
        assert c.send({"op": "click", "x": 1, "y": 1}) is None
    finally:
        c.close()
    assert not c.alive()


def test_overlay_client_survives_a_broken_overlay(tmp_path, monkeypatch):
    from pulsar.pilot.tools import OverlayClient
    (tmp_path / "broken_pulsar_overlay.py").write_text(
        'import json; print(json.dumps({"hello": 1, "ok": False, "error": "no tkinter"}))')
    monkeypatch.setattr(pulsar, "ROOT", str(tmp_path))   # the bootstrap ignores PYTHONPATH (-E)
    c = OverlayClient(python=sys.executable, module="broken_pulsar_overlay")
    assert c.start() is False and "no tkinter" in c.disabled
    assert c.send({"op": "glide", "x": 1, "y": 1}, wait=1) is None
    c2 = OverlayClient(python=sys.executable, module="no_such_module_pulsar_xyz", hello_timeout=5)
    assert c2.start() is False and c2.send({"op": "show"}) is None


# ---------------------------------------------------------------------------
# overlay Scene (pure)
# ---------------------------------------------------------------------------

def _valid(prims, w, h):
    kinds = {"oval", "arc", "poly", "line", "rect", "text"}
    for p in prims:
        assert p[0] in kinds
        nums = []
        for v in p[1:]:
            if isinstance(v, (int, float)):
                nums.append(v)
            elif isinstance(v, list):
                nums.extend(v)
        assert all(math.isfinite(n) for n in nums), p
        if p[0] in ("poly", "line"):
            assert len(p[1]) % 2 == 0 and len(p[1]) >= 4


def test_scene_glide_click_ripple():
    from pulsar.pilot.overlay import Scene, TRAIL
    sc = Scene(1280, 800)
    dur = sc.apply({"op": "glide", "x": 900, "y": 500, "from": [100, 100], "label": "Save"}, 0.0)
    assert 0.25 <= dur <= 0.45
    mid = sc.frame(dur / 2)
    _valid(mid, 1280, 800)
    x, y = sc.pos_at(dur / 2)
    assert 100 < x < 900 and 100 < y < 500
    assert sc.pos_at(dur + 0.01) == (900.0, 500.0)
    assert len([p for p in mid if p[0] == "poly"]) > 15      # comet trail + beams
    assert any(p[0] == "text" and p[3] == "Save" for p in mid)
    assert sc.frame(dur + TRAIL + 0.05)
    t = dur + 0.5
    sc.apply({"op": "click", "x": 900, "y": 500, "button": "left"}, t)
    radii = []
    for dt in (0.1, 0.3, 0.5):
        fr = sc.frame(t + dt)
        _valid(fr, 1280, 800)
        radii.append(max(p[3] for p in fr if p[0] == "oval"))
    assert radii[0] < radii[1] < radii[2]


def test_scene_aim_lock_and_acks():
    from pulsar.pilot.overlay import Scene
    sc = Scene(1280, 800)
    due = sc.apply({"op": "aim", "x": 50, "y": 50, "from": [600, 600]}, 0.0)
    assert 0.4 <= due <= 0.62
    fr = sc.frame(due - 0.05)
    assert sum(1 for p in fr if p[0] == "line" and len(p[1]) == 6) >= 8      # 4 corners + ink
    assert sc.apply({"op": "ping"}, 1.0) == 0.0


def test_scene_reduced_motion_and_idle_hide():
    from pulsar.pilot.overlay import Scene
    sc = Scene(1280, 800, reduced=True)
    assert sc.apply({"op": "glide", "x": 10, "y": 10, "from": [900, 700]}, 0.0) == 0.0
    assert sc.pos_at(0.0) == (10.0, 10.0)
    fr = sc.frame(0.05)
    assert fr and sc.visible
    t = 0.1
    while sc.visible and t < 30:
        sc.frame(t)
        t += 0.5
    assert not sc.visible and t < sc.idle_hide + 3


def test_scene_every_op_and_garbage_is_safe():
    from pulsar.pilot.overlay import Scene, _key_groups
    rng = random.Random(11)
    sc = Scene(640, 480, k=1.5)
    ops = ["glide", "aim", "click", "type", "key", "scroll", "drag", "scan", "show", "hide", "ping", "bogus"]
    junk = [None, "x", -1e9, 1e9, float("nan"), [], {}, 3, "12"]
    t = 0.0
    for _ in range(400):
        op = {"op": rng.choice(ops)}
        for k in ("x", "y", "x1", "y1", "x2", "y2", "dx", "dy", "chars", "dur", "count", "label", "button"):
            if rng.random() < 0.5:
                op[k] = rng.choice(junk + [rng.uniform(-50, 700)])
        if op.get("label") is not None and not isinstance(op.get("label"), str):
            op["label"] = str(op["label"])
        try:
            sc.apply(op, t)
        except (ValueError, OverflowError):
            pass
        t += rng.uniform(0, 0.2)
        fr = sc.frame(t)
        for p in fr:
            assert p[0] in {"oval", "arc", "poly", "line", "rect", "text"}
    assert _key_groups("ctrl+shift+s") == [["ctrl", "shift", "S"]]
    assert _key_groups("ctrl++ enter") == [["ctrl", "+"], ["enter"]]


def test_scene_chip_flips_at_screen_edges():
    from pulsar.pilot.overlay import Scene
    sc = Scene(800, 600)
    sc.apply({"op": "glide", "x": 790, "y": 590, "from": [790, 590], "label": "Edge"}, 0.0)
    fr = sc.frame(1.0)
    chip = [p for p in fr if p[0] == "text" and p[3] == "Edge"][0]
    assert chip[1] < 790 and chip[2] < 590


def test_overlay_without_display_reports_cleanly():
    env = dict(os.environ)
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    if sys.platform == "darwin" or os.name == "nt":
        pytest.skip("headless overlay check is for X11 hosts")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = subprocess.run([sys.executable, "-m", "pulsar.pilot.overlay"], input="", capture_output=True, text=True,
                         env=env, cwd=root, timeout=30)
    hello = json.loads(out.stdout.strip().splitlines()[0])
    assert hello["hello"] == 1 and hello["ok"] is False and hello["error"]
    probe = json.loads(subprocess.run([sys.executable, "-m", "pulsar.pilot.overlay", "--probe"], capture_output=True,
                                      text=True, env=env, cwd=root, timeout=30).stdout)
    assert probe["ok"] is False


# ---------------------------------------------------------------------------
# cli.py
# ---------------------------------------------------------------------------

class Buf(io.StringIO):
    def isatty(self):
        return False


def test_cli_on_off_status_log(monkeypatch):
    from pulsar.pilot import cli as C
    monkeypatch.delenv("PULSAR_PILOT", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    for v in ("CLAUDECODE", "PULSAR_SESSION", "PULSAR_SESSION_TOKEN", "CLAUDE_CODE_ENTRYPOINT"):
        monkeypatch.delenv(v, raising=False)
    # never armable without a real terminal (a model's Bash tool has none), --yes or not
    out = C.Out(Buf())
    assert C.cmd_on(out, ["--yes"], ask=lambda q: "y") == 2 and not S.enabled()
    import io
    tty = type("T", (io.StringIO,), {"isatty": lambda self: True})
    monkeypatch.setattr(C.sys, "stdin", tty())
    monkeypatch.setattr(C.sys, "stdout", tty())
    monkeypatch.setenv("CLAUDECODE", "1")
    out = C.Out(Buf())
    assert C.cmd_on(out, [], ask=lambda q: "y") == 2 and not S.enabled()
    assert "your own terminal" in out.stream.getvalue()
    monkeypatch.delenv("CLAUDECODE")
    # the markers unset and a terminal faked, but Claude Code is still an ancestor process: refused
    from pulsar import launch as L
    monkeypatch.setattr(L, "claude_ancestor", lambda *a, **kw: True)
    out = C.Out(Buf())
    assert C.cmd_on(out, [], ask=lambda q: "y") == 2 and not S.enabled()
    assert "inside Claude Code" in out.stream.getvalue()
    monkeypatch.setattr(L, "claude_ancestor", lambda *a, **kw: False)
    out = C.Out(Buf())
    assert C.cmd_on(out, [], ask=lambda q: "y") == 0 and S.enabled()
    assert "Failsafe" in out.stream.getvalue() and "\x1b[" not in out.stream.getvalue()
    assert "never relays to another provider" in out.stream.getvalue()
    out = C.Out(Buf())
    assert C.cmd_on(out, [], ask=lambda q: "n") in (1, 2)
    out = C.Out(Buf())
    assert C.cmd_off(out) == 0 and not S.enabled()
    out = C.Out(Buf())
    assert C.cmd_log(out, []) == 0
    S.audit("click", x=1, y=2)
    out = C.Out(Buf())
    C.cmd_log(out, ["5"])
    assert "action=click" in out.stream.getvalue()
    out = C.Out(Buf())
    from pulsar.pilot import tools as T
    monkeypatch.setattr(T, "overlay_blocker", lambda env=None: "")
    monkeypatch.setattr(C, "probe_overlay", lambda *a, **kw: pytest.fail("status must not start a window"))
    assert C.cmd_status(out) == 0
    text = out.stream.getvalue()
    assert "PULSAR PILOT" in text and "failsafe" in text and "\u2014" not in text
    assert "pilot status --gui" in text
    assert C.cmd_pilot(["bogus"]) == 2


def test_cli_tour_drives_overlay_without_input(monkeypatch):
    from pulsar.pilot import cli as C
    # The tour starts where the cursor is. Here that is a fixed point, wherever the mouse of the machine
    # running the tests happens to be (beyond x=1000 on a wide display, which failed the range check below).
    monkeypatch.setattr(C.B, "get_backend", lambda: type("Fixed", (), {"cursor": staticmethod(lambda: (120, 80))})())
    sent = []

    class Rec:
        hello = {"w": 1000, "h": 500, "platform": "x11"}
        disabled = ""

        def start(self):
            return True

        def send(self, op, wait=0.0):
            sent.append(op)
            return {"ack": 1}

        def close(self):
            sent.append({"op": "closed"})
    out = C.Out(Buf())
    assert C.cmd_tour(out, client=Rec(), sleep=lambda s: None) == 0
    kinds = [o["op"] for o in sent]
    assert {"scan", "glide", "aim", "click", "type", "key", "scroll", "drag"} <= set(kinds)
    assert kinds[-1] == "closed"
    for o in sent:
        for k in ("x", "y", "x1", "y1", "x2", "y2"):
            if k in o:
                assert 0 <= o[k] <= 1000


# ---------------------------------------------------------------------------
# Live X11 (private Xvfb)
# ---------------------------------------------------------------------------

def _xvfb_ok():
    return (sys.platform.startswith("linux") and shutil.which("Xvfb") and ctypes.util.find_library("X11")
            and (ctypes.util.find_library("Xtst") or os.path.exists("/usr/lib/x86_64-linux-gnu/libXtst.so.6")))


@pytest.fixture(scope="module")
def xdisplay():
    if not _xvfb_ok():
        pytest.skip("Xvfb + libX11/libXtst not available")
    num = None
    for n in range(91, 140):
        if not os.path.exists("/tmp/.X%d-lock" % n) and not os.path.exists("/tmp/.X11-unix/X%d" % n):
            num = n
            break
    if num is None:
        pytest.skip("no free X display number")
    proc = subprocess.Popen(["Xvfb", ":%d" % num, "-screen", "0", "1024x768x24", "-nolisten", "tcp"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sock = "/tmp/.X11-unix/X%d" % num
    for _ in range(100):
        if os.path.exists(sock):
            break
        time.sleep(0.05)
    else:
        proc.kill()
        pytest.skip("Xvfb did not start")
    time.sleep(0.2)
    yield ":%d" % num
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


class XProbe:
    """A real X client window that records KeyPress/ButtonPress events (no tkinter needed)."""

    def __init__(self, display):
        x = self.x = ctypes.CDLL(ctypes.util.find_library("X11"))
        vp, ul, ci, cu = ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_uint
        for name, res, args in (
                ("XOpenDisplay", vp, [ctypes.c_char_p]), ("XDefaultScreen", ci, [vp]), ("XRootWindow", ul, [vp, ci]),
                ("XCreateSimpleWindow", ul, [vp, ul, ci, ci, cu, cu, cu, ul, ul]), ("XSelectInput", ci, [vp, ul, ctypes.c_long]),
                ("XMapRaised", ci, [vp, ul]), ("XSetInputFocus", ci, [vp, ul, ci, ul]), ("XPending", ci, [vp]),
                ("XNextEvent", ci, [vp, vp]), ("XSync", ci, [vp, ci]), ("XRefreshKeyboardMapping", ci, [vp]),
                ("XLookupString", ci, [vp, ctypes.c_char_p, ci, ctypes.POINTER(ul), vp]),
                ("XDestroyWindow", ci, [vp, ul]), ("XCloseDisplay", ci, [vp])):
            fn = getattr(x, name)
            fn.restype, fn.argtypes = res, args
        self.d = x.XOpenDisplay(display.encode())
        root = x.XRootWindow(self.d, x.XDefaultScreen(self.d))
        self.win = x.XCreateSimpleWindow(self.d, root, 100, 100, 400, 300, 0, 0, 0xFFFFFF)
        x.XSelectInput(self.d, self.win, 1 | 4)      # KeyPressMask | ButtonPressMask
        x.XMapRaised(self.d, self.win)
        x.XSync(self.d, 0)
        time.sleep(0.2)
        x.XSetInputFocus(self.d, self.win, 1, 0)
        x.XSync(self.d, 0)

    def events(self, settle=0.6):
        out = []
        ev = (ctypes.c_char * 192)()
        end = time.time() + settle
        while time.time() < end:
            while self.x.XPending(self.d):
                self.x.XNextEvent(self.d, ev)
                etype = ctypes.cast(ev, ctypes.POINTER(ctypes.c_int))[0]
                if etype == 34:                        # MappingNotify
                    self.x.XRefreshKeyboardMapping(ev)
                elif etype == 2:                       # KeyPress
                    buf = ctypes.create_string_buffer(16)
                    ks = ctypes.c_ulong()
                    n = self.x.XLookupString(ev, buf, 16, ctypes.byref(ks), None)
                    out.append(("key", ks.value, buf.raw[:n]))
                elif etype == 4:                       # ButtonPress: XButtonEvent x,y,button
                    lay = XButtonEvent.from_buffer_copy(bytes(ev)[:ctypes.sizeof(XButtonEvent)])
                    out.append(("button", lay.button, lay.x_root, lay.y_root))
            time.sleep(0.01)
        return out

    def close(self):
        self.x.XDestroyWindow(self.d, self.win)
        self.x.XCloseDisplay(self.d)


class XButtonEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("window", ctypes.c_ulong), ("root", ctypes.c_ulong),
                ("subwindow", ctypes.c_ulong), ("time", ctypes.c_ulong), ("x", ctypes.c_int), ("y", ctypes.c_int),
                ("x_root", ctypes.c_int), ("y_root", ctypes.c_int), ("state", ctypes.c_uint), ("button", ctypes.c_uint),
                ("same_screen", ctypes.c_int)]


@pytest.fixture
def xb(xdisplay):
    from pulsar.pilot.x11 import X11Backend
    b = X11Backend(xdisplay)
    yield b
    b.close()


def test_x11_live_pointer_and_capture(xb):
    assert xb.size() == (1024, 768)
    for target in ((10, 10), (512, 384), (1023, 767), (300, 41)):
        xb.move(*target)
        assert xb.cursor() == target
    cap = xb.capture()
    assert (cap.width, cap.height) == (1024, 768) and cap.mode in ("BGRX", "XRGB", "RGBX", "BGR", "RGB")
    assert len(cap.data) == 1024 * 768 * PNG.bpp(cap.mode)
    rgb, _ = PNG.to_rgb(cap.data, cap.width, cap.height, cap.mode)
    small = PNG.resample(rgb, 1024, 768, 512, 384)
    w, h, _, pix = png_decode(PNG.encode(512, 384, small))
    assert (w, h) == (512, 384) and len(pix) == 512 * 384 * 3
    part = xb.capture((100, 50, 164, 82))
    assert (part.width, part.height) == (64, 32) and part.origin == (100, 50)


def test_x11_live_click_and_scroll_reach_a_window(xb, xdisplay):
    probe = XProbe(xdisplay)
    try:
        xb.click(200, 150, "left", 1)
        xb.click(250, 160, "right", 1)
        xb.scroll(220, 170, 0, 2)
        ev = [e for e in probe.events() if e[0] == "button"]
        assert ("button", 1, 200, 150) in ev and ("button", 3, 250, 160) in ev
        assert [e for e in ev if e[1] == 5] and len([e for e in ev if e[1] == 5]) == 2
    finally:
        probe.close()


def test_x11_live_typing_and_shortcuts(xb, xdisplay):
    probe = XProbe(xdisplay)
    try:
        xb.click(150, 150)
        probe.events(0.2)
        xb.type_text("Hi! é€\n")
        xb.keys(B.parse_keys("ctrl+a escape"))
        ev = [e for e in probe.events(0.8) if e[0] == "key"]
        syms = [e[1] for e in ev]
        for ks in (ord("H"), ord("i"), ord("!"), 0xE9, 0x10020AC, 0xFF0D, ord("a"), 0xFF1B):
            assert ks in syms, (hex(ks), [hex(s) for s in syms])
        assert 0xFFE3 in syms                        # Control_L was held for ctrl+a
        assert syms.index(ord("H")) < syms.index(ord("i")) < syms.index(0xFF0D)
        # spare keycodes borrowed for é and € are returned (after a grace period, forced here)
        assert xb._scratch
        xb.release_keys()
        xb._load_keymap()
        assert xb._find(0x10020AC) is None and not xb._scratch
    finally:
        probe.close()


def test_x11_live_tools_end_to_end(xdisplay, monkeypatch):
    from pulsar.pilot.tools import Pilot
    from pulsar.pilot.x11 import X11Backend
    from pulsar.mcp.server import ToolError
    monkeypatch.setenv("PULSAR_PILOT_MAX_W", "800")
    b = X11Backend(xdisplay)
    try:
        p = Pilot(lambda: b, guard=OpenGuard(), overlay=NoOverlay(), settle=0)
        img, txt = p.screenshot({})
        w, h, _, _ = png_decode(base64.b64decode(img["data"]))
        assert (w, h) == (800, 600) and "scale 0.781" in txt["text"]
        p.move({"x": 400, "y": 300})
        assert abs(b.cursor()[0] - 512) <= 1 and abs(b.cursor()[1] - 384) <= 1
        p.click({"x": 0, "y": 0})
        assert b.cursor() == (5, 0)                   # never parks in the failsafe square itself
        b.move(1, 1)                                  # a human slams the pointer into the corner
        with pytest.raises(ToolError):
            p.click({"x": 100, "y": 100})
        assert b.cursor() == (1, 1)
    finally:
        b.close()
