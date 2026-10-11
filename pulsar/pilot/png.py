# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Pure-python screenshot pipeline: pixel formats, resampling, PNG.

Everything here runs at C speed without numpy:

  * channel shuffles (BGRA -> RGB, ...) are extended-slice assignments,
  * nearest resampling is one operator.itemgetter call per output row,
  * 2x2 box averaging adds whole rows as big integers whose 16-bit lanes
    hold one channel each (no carry can cross a lane),
  * the PNG "Up" filter is a lane-wise subtraction done the same way.

A 1920x1080 frame is resampled to 1280x720 and encoded in a fraction of a
second, which keeps pilot_screenshot snappy on every OS.
"""

from __future__ import annotations

import operator
import struct
import zlib
from typing import Dict, List, Tuple

PNG_SIG = b"\x89PNG\r\n\x1a\n"

# Channel byte offsets inside one source pixel, in output (R, G, B[, A]) order.
_LAYOUT: Dict[str, Tuple[int, Tuple[int, ...]]] = {
    "RGB": (3, (0, 1, 2)),
    "BGR": (3, (2, 1, 0)),
    "RGBA": (4, (0, 1, 2)),
    "RGBX": (4, (0, 1, 2)),
    "BGRA": (4, (2, 1, 0)),
    "BGRX": (4, (2, 1, 0)),
    "XRGB": (4, (1, 2, 3)),
    "ARGB": (4, (1, 2, 3)),
}
_ALPHA = {"RGBA": 3, "BGRA": 3, "ARGB": 0}


class ImageError(ValueError):
    pass


def bpp(mode: str) -> int:
    try:
        return _LAYOUT[mode][0]
    except KeyError:
        raise ImageError("unsupported pixel format " + mode)


def strip_stride(data: bytes, width: int, height: int, stride: int, pixel_bytes: int) -> bytes:
    """Drop per-row padding (X11 bytes_per_line, Windows DWORD alignment)."""
    row = width * pixel_bytes
    if stride == row:
        return bytes(data[:row * height])
    if stride < row:
        raise ImageError("stride %d is shorter than a row (%d)" % (stride, row))
    mv = memoryview(data)
    return b"".join(mv[y * stride:y * stride + row] for y in range(height))


def to_rgb(data: bytes, width: int, height: int, mode: str = "RGB", keep_alpha: bool = False) -> Tuple[bytes, str]:
    """Convert a packed buffer to RGB (or RGBA when keep_alpha and the source has alpha)."""
    n = bpp(mode)
    stride, order = _LAYOUT[mode]
    count = width * height
    if len(data) < count * n:
        raise ImageError("buffer too small: %d bytes for %dx%d %s" % (len(data), width, height, mode))
    if mode == "RGB":
        return bytes(data[:count * 3]), "RGB"
    if keep_alpha and mode in _ALPHA:
        if mode == "RGBA":
            return bytes(data[:count * 4]), "RGBA"
        out = bytearray(count * 4)
        for i, src in enumerate(order):
            out[i::4] = data[src:count * 4:4]
        a = _ALPHA[mode]
        out[3::4] = data[a:count * 4:4]
        return bytes(out), "RGBA"
    out = bytearray(count * 3)
    for i, src in enumerate(order):
        out[i::3] = data[src:count * stride:stride]
    return bytes(out), "RGB"


# ---------------------------------------------------------------------------
# Lane arithmetic
# ---------------------------------------------------------------------------

def _lanes(b: bytes) -> int:
    """Spread bytes into 16-bit little-endian lanes and read them as one integer."""
    z = bytearray(2 * len(b))
    z[0::2] = b
    return int.from_bytes(z, "little")


_CONST: Dict[Tuple[int, int], int] = {}


def _lane_const(value: int, n: int) -> int:
    key = (value, n)
    c = _CONST.get(key)
    if c is None:
        c = int.from_bytes(struct.pack("<H", value) * n, "little")
        if len(_CONST) > 64:
            _CONST.clear()
        _CONST[key] = c
    return c


def _pairs(data: bytes, width: int, height: int, ch: int) -> Tuple[bytes, bytes]:
    """Even and odd pixels of every row (width must be even)."""
    n = len(data) // 2
    even = bytearray(n)
    odd = bytearray(n)
    step = 2 * ch
    for c in range(ch):
        even[c::ch] = data[c::step]
        odd[c::ch] = data[c + ch::step]
    return bytes(even), bytes(odd)


def halve(data: bytes, width: int, height: int, ch: int = 3) -> Tuple[bytes, int, int]:
    """2x2 box average (rounded). An odd last column or row is dropped."""
    if width < 2 or height < 2:
        return data, width, height
    w2, h2 = width // 2, height // 2
    rowb = width * ch
    if width % 2 or height % 2:
        mv = memoryview(data)
        data = b"".join(mv[y * rowb:y * rowb + w2 * 2 * ch] for y in range(h2 * 2))
        width, rowb = w2 * 2, w2 * 2 * ch
    out: List[bytes] = []
    band = 64  # rows per band: bounds memory on 4K/5K screens
    for y0 in range(0, h2 * 2, band):
        y1 = min(h2 * 2, y0 + band)
        chunk = data[y0 * rowb:y1 * rowb]
        even, odd = _pairs(chunk, width, y1 - y0, ch)
        hsum = (_lanes(even) + _lanes(odd)).to_bytes(len(even) * 2, "little")
        lrow = w2 * ch * 2
        rows = (y1 - y0) // 2
        mv = memoryview(hsum)
        top = b"".join(mv[(2 * j) * lrow:(2 * j + 1) * lrow] for j in range(rows))
        bot = b"".join(mv[(2 * j + 1) * lrow:(2 * j + 2) * lrow] for j in range(rows))
        n = len(top) // 2
        total = int.from_bytes(top, "little") + int.from_bytes(bot, "little") + _lane_const(2, n)
        out.append((total << 6).to_bytes(n * 2 + 1, "little")[1:2 * n:2])
    return b"".join(out), w2, h2


def nearest(data: bytes, width: int, height: int, tw: int, th: int, ch: int = 3) -> bytes:
    """Nearest-neighbour resample to tw x th (sampling pixel centres)."""
    if (tw, th) == (width, height):
        return data
    if tw <= 0 or th <= 0:
        raise ImageError("bad target size")
    cols = [min(width - 1, int((i + 0.5) * width / tw)) for i in range(tw)]
    idx = [x * ch + c for x in cols for c in range(ch)]
    rowb = width * ch
    mv = memoryview(data)
    if len(idx) == 1:
        def take(row):
            return bytes((row[idx[0]],))
    else:
        getter = operator.itemgetter(*idx)

        def take(row):
            return bytes(getter(row))
    cache: Dict[int, bytes] = {}
    out = []
    for j in range(th):
        y = min(height - 1, int((j + 0.5) * height / th))
        r = cache.get(y)
        if r is None:
            r = take(bytes(mv[y * rowb:(y + 1) * rowb]))
            cache = {y: r}
        out.append(r)
    return b"".join(out)


def fit(width: int, height: int, max_w: int, max_h: int = 0) -> Tuple[int, int]:
    """Largest size <= (max_w, max_h) with the same aspect ratio (never upscales)."""
    if width <= 0 or height <= 0:
        raise ImageError("empty image")
    s = 1.0
    if max_w and width > max_w:
        s = min(s, max_w / width)
    if max_h and height > max_h:
        s = min(s, max_h / height)
    if s >= 1.0:
        return width, height
    return max(1, int(round(width * s))), max(1, int(round(height * s)))


def resample(data: bytes, width: int, height: int, tw: int, th: int, ch: int = 3, smooth: bool = True) -> bytes:
    """Downscale. smooth=True: box-halve, nearest to 2x target, halve (area-like, anti-aliased)."""
    if (tw, th) == (width, height):
        return data
    if not smooth or tw * 2 > 8192 or th * 2 > 8192:
        return nearest(data, width, height, tw, th, ch)
    while width >= 4 * tw and height >= 4 * th:
        data, width, height = halve(data, width, height, ch)
    data = nearest(data, width, height, tw * 2, th * 2, ch)
    data, _, _ = halve(data, tw * 2, th * 2, ch)
    return data


def crop(data: bytes, width: int, height: int, box: Tuple[int, int, int, int], ch: int = 3) -> Tuple[bytes, int, int]:
    x0, y0, x1, y1 = box
    x0, x1 = max(0, min(width, x0)), max(0, min(width, x1))
    y0, y1 = max(0, min(height, y0)), max(0, min(height, y1))
    if x1 - x0 < 1 or y1 - y0 < 1:
        raise ImageError("empty crop")
    mv = memoryview(data)
    rowb = width * ch
    out = b"".join(mv[y * rowb + x0 * ch:y * rowb + x1 * ch] for y in range(y0, y1))
    return out, x1 - x0, y1 - y0


# ---------------------------------------------------------------------------
# PNG
# ---------------------------------------------------------------------------

def _chunk(tag: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)


def _up_filter(raw: bytes, rowb: int, height: int) -> bytes:
    """Rows prefixed with filter byte 2 (Up): each byte minus the byte above, mod 256."""
    n = len(raw)
    prev = bytes(rowb) + raw[:n - rowb]
    diff = (_lanes(raw) + _lane_const(256, n) - _lanes(prev)).to_bytes(2 * n, "little")[0::2]
    mv = memoryview(diff)
    return b"".join(b"\x02" + mv[y * rowb:(y + 1) * rowb] for y in range(height))


def encode(width: int, height: int, data: bytes, mode: str = "RGB", level: int = 6, keep_alpha: bool = False) -> bytes:
    """Encode a packed buffer (RGB, RGBA, BGRA, BGRX, BGR, ...) as an 8-bit PNG."""
    if width <= 0 or height <= 0:
        raise ImageError("empty image")
    pix, out_mode = to_rgb(data, width, height, mode, keep_alpha=keep_alpha)
    ch = 4 if out_mode == "RGBA" else 3
    rowb = width * ch
    if height > 1:
        body = _up_filter(pix, rowb, height)
    else:
        body = b"\x00" + pix
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6 if ch == 4 else 2, 0, 0, 0)
    return PNG_SIG + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(body, level)) + _chunk(b"IEND", b"")


def png_size(blob: bytes) -> Tuple[int, int]:
    """(width, height) from a PNG's IHDR."""
    if len(blob) < 24 or blob[:8] != PNG_SIG or blob[12:16] != b"IHDR":
        raise ImageError("not a PNG")
    w, h = struct.unpack(">II", blob[16:24])
    return int(w), int(h)


def bmp_decode(blob: bytes) -> Tuple[bytes, int, int]:
    """Uncompressed 24/32-bit BMP (BITMAPINFOHEADER or later) -> (RGB, w, h)."""
    if len(blob) < 54 or blob[:2] != b"BM":
        raise ImageError("not a BMP")
    off = struct.unpack("<I", blob[10:14])[0]
    w, h, planes, bits, comp = struct.unpack("<iiHHI", blob[18:34])
    if comp not in (0, 3) or bits not in (24, 32) or w <= 0 or h == 0:
        raise ImageError("unsupported BMP (bits=%d compression=%d)" % (bits, comp))
    top_down = h < 0
    h = abs(h)
    pb = bits // 8
    stride = ((w * pb + 3) // 4) * 4
    pixels = blob[off:off + stride * h]
    if len(pixels) < stride * h:
        raise ImageError("truncated BMP")
    if not top_down:
        mv = memoryview(pixels)
        pixels = b"".join(mv[(h - 1 - y) * stride:(h - y) * stride] for y in range(h))
    packed = strip_stride(pixels, w, h, stride, pb)
    rgb, _ = to_rgb(packed, w, h, "BGRX" if pb == 4 else "BGR")
    return rgb, w, h
