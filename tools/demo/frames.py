#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Capture exact frames of a replay SVG, for review and acceptance checks.

    frames.py assets/demo/session.svg --at 0,2.5,6.2,12 [--out DIR] [--sheet sheet.png] [--reduced]
    frames.py assets/demo/session.svg --cursor 10       # check the cursor rect against the recording

Chromium (Playwright) opens the SVG as a document, pauses every animation and sets its currentTime,
so a frame is the state at exactly T seconds (not wherever a real-time screenshot happened to land).
--reduced renders with prefers-reduced-motion: reduce. --sheet tiles the captures into one PNG.
--cursor N picks N random frames and compares the cursor rect's position with the recorded cursor
in the matching JSON (docs/demo/<scene>.json, or --json FILE); exit 1 on a mismatch.
"""

from __future__ import annotations

import json
import random
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

import posters  # noqa: E402
import svgterm  # noqa: E402


def _launch(p):
    kw = {"args": ["--no-sandbox"]}
    exe = posters.chromium_path()
    if exe:
        kw["executable_path"] = exe
    return p.chromium.launch(**kw)


def capture(svg: Path, times, out: Path, reduced: bool = False, scale: float = 1.0):
    from playwright.sync_api import sync_playwright
    text = svg.read_text()
    w, h = (int(v) for v in re.search(r'width="(\d+)" height="(\d+)"', text).groups())
    files = []
    with sync_playwright() as p:
        b = _launch(p)
        ctx = b.new_context(viewport={"width": w, "height": h}, device_scale_factor=scale,
                            reduced_motion="reduce" if reduced else "no-preference")
        page = ctx.new_page()
        page.goto(svg.resolve().as_uri())
        page.wait_for_timeout(100)
        out.mkdir(parents=True, exist_ok=True)
        for t in times:
            page.evaluate("t => document.getAnimations().forEach(a => { a.pause(); a.currentTime = t * 1000; })", t)
            page.wait_for_timeout(30)
            f = out / ("%s_%s%06d.png" % (svg.stem, "rm_" if reduced else "", int(round(t * 1000))))
            page.screenshot(path=str(f))
            files.append(f)
        b.close()
    return files


def cursor_check(svg: Path, doc: dict, n: int, seed: int = 1):
    """[(t, expected (x, y) or None, seen (x, y) or None)] at n random frames."""
    from playwright.sync_api import sync_playwright
    frames, cursor = doc["frames"], doc.get("cursor") or []
    text = svg.read_text()
    w, h = (int(v) for v in re.search(r'width="(\d+)" height="(\d+)"', text).groups())
    lay = svgterm.Layout(doc["cols"], doc["rows"], bool(doc.get("captions")))
    rng = random.Random(seed)
    picks = sorted(rng.sample(range(len(frames)), min(n, len(frames))))
    out = []
    with sync_playwright() as p:
        b = _launch(p)
        page = b.new_context(viewport={"width": w, "height": h}).new_page()
        page.goto(svg.resolve().as_uri())
        for i in picks:
            t = frames[i]["t"] + 0.001
            page.evaluate("t => document.getAnimations().forEach(a => { a.pause(); a.currentTime = t * 1000; })", t)
            seen = page.evaluate("""() => { const g = document.querySelector('.cur'); if (!g) return null;
                const cs = getComputedStyle(g); if (parseFloat(cs.opacity) < .5) return null;
                const m = new DOMMatrix(cs.transform); return [m.e, m.f]; }""")
            c = cursor[i] if i < len(cursor) else None
            exp = list(lay.cell(c[0], c[1])) if c else None
            out.append((frames[i]["t"], exp, seen))
        b.close()
    return out


def sheet(files, path: Path, cols: int = 2, width: int = 640):
    from PIL import Image, ImageDraw
    ims = [Image.open(f).convert("RGB") for f in files]
    th = [im.resize((width, round(im.height * width / im.width))) for im in ims]
    ch = max(t.height for t in th) + 18
    rows = (len(th) + cols - 1) // cols
    out = Image.new("RGB", (width * cols, ch * rows), (32, 30, 40))
    d = ImageDraw.Draw(out)
    for k, (t, f) in enumerate(zip(th, files)):
        x, y = (k % cols) * width, (k // cols) * ch
        out.paste(t, (x, y + 18))
        d.text((x + 6, y + 3), Path(f).stem, fill=(255, 210, 90))
    out.save(path)
    return path


def main(argv) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    svg = Path(argv[0])
    opts = {"--at": "0", "--out": "/tmp/pulsar-frames", "--sheet": "", "--cursor": "", "--json": ""}
    flags = set()
    it = iter(argv[1:])
    for a in it:
        if a in opts:
            opts[a] = next(it)
        else:
            flags.add(a)
    if opts["--cursor"]:
        jf = Path(opts["--json"] or REPO / "docs" / "demo" / (svg.stem + ".json"))
        res = cursor_check(svg, json.loads(jf.read_text()), int(opts["--cursor"]))
        bad = 0
        for t, exp, seen in res:
            ok = (exp is None and seen is None) or (exp and seen and abs(exp[0] - seen[0]) < .6
                                                     and abs(exp[1] - seen[1]) < .6)
            bad += not ok
            print("%7.3fs  recorded %-16s rendered %-16s %s" % (t, exp, seen, "ok" if ok else "MISMATCH"))
        return 1 if bad else 0
    times = [float(x) for x in opts["--at"].split(",") if x]
    files = capture(svg, times, Path(opts["--out"]), reduced="--reduced" in flags)
    for f in files:
        print(f)
    if opts["--sheet"]:
        print(sheet(files, Path(opts["--sheet"])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
