#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Posters for the terminal replays: each SVG's reduced-motion render (its last frame), as a PNG.

    posters.py [--src assets/demo] [--out assets/demo/posters] [--copy docs/demo/posters] [SCENE ...]

Chromium (Playwright) opens assets/demo/<scene>.svg with prefers-reduced-motion: reduce, which is
exactly what the replay shows a viewer who asked for less motion: the last frame, camera at 1.0, no
caption unless the scene marks one as its poster. The capture is supersampled, scaled to 860 px wide
and palette-quantised until it fits 90 KB (README <details> tables and the site player's poster).

Needs `pip install playwright` and a Chromium for it (`playwright install chromium`, or set
PLAYWRIGHT_CHROMIUM to a chrome binary).
"""

from __future__ import annotations

import glob
import io
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WIDTH = 860
BUDGET = 90 * 1024
SCENES = ("boot", "uplink", "picker", "session", "stage", "top")


def chromium_path():
    env = os.environ.get("PLAYWRIGHT_CHROMIUM")
    if env and Path(env).exists():
        return env
    for pat in ("/opt/pw-browsers/chromium-*/chrome-linux/chrome",
                os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux/chrome")):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    return None


def quantise(png: bytes, width: int = WIDTH, budget: int = BUDGET) -> bytes:
    from PIL import Image
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    h = round(im.height * width / im.width)
    im = im.resize((width, h), Image.LANCZOS)
    data = b""
    for colors in (160, 128, 96, 64, 48, 32):
        q = im.quantize(colors=colors, method=Image.FASTOCTREE, dither=Image.NONE)
        buf = io.BytesIO()
        q.save(buf, "PNG", optimize=True)
        data = buf.getvalue()
        if len(data) <= budget:
            return data
    return data


def capture(svgs, scale: float = 2.0):
    from playwright.sync_api import sync_playwright
    from PIL import Image
    out = {}
    with sync_playwright() as p:
        kw = {"args": ["--no-sandbox"]}
        exe = chromium_path()
        if exe:
            kw["executable_path"] = exe
        b = p.chromium.launch(**kw)
        for svg in svgs:
            text = Path(svg).read_text()
            import re
            m = re.search(r'width="(\d+)" height="(\d+)"', text)
            w, h = int(m.group(1)), int(m.group(2))
            ctx = b.new_context(viewport={"width": w, "height": h}, device_scale_factor=scale, reduced_motion="reduce")
            page = ctx.new_page()
            page.goto(Path(svg).resolve().as_uri())
            page.wait_for_timeout(150)
            png = page.screenshot(omit_background=True)
            ctx.close()
            assert Image.open(io.BytesIO(png)).width == round(w * scale)
            out[svg] = png
        b.close()
    return out


def main(argv) -> int:
    src, dst, copy, names = REPO / "assets" / "demo", None, None, []
    it = iter(argv)
    for a in it:
        if a == "--src":
            src = Path(next(it))
        elif a == "--out":
            dst = Path(next(it))
        elif a == "--copy":
            copy = Path(next(it))
        elif a in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            names.append(a)
    dst = dst or src / "posters"
    names = names or [n for n in SCENES if (src / (n + ".svg")).exists()]
    svgs = [str(src / (n + ".svg")) for n in names]
    try:
        shots = capture(svgs)
    except Exception as e:                    # noqa: BLE001 - report and fail the build step
        print("posters.py: capture failed: %s" % e, file=sys.stderr)
        return 1
    dst.mkdir(parents=True, exist_ok=True)
    bad = []
    for n, svg in zip(names, svgs):
        data = quantise(shots[svg])
        (dst / (n + ".png")).write_bytes(data)
        if copy:
            copy.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(dst / (n + ".png"), copy / (n + ".png"))
        print("poster %-8s %3.0f KB" % (n, len(data) / 1024))
        if len(data) > BUDGET:
            bad.append(n)
    if bad:
        print("posters.py: over %d KB: %s" % (BUDGET // 1024, " ".join(bad)), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
