#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Still screenshots (assets/screens/*.png) from the recorded replays, in the replay's brand chrome.

    screens.py [--json-dir docs/demo] [--out assets/screens] [NAME ...]

Each still is one recorded frame (docs/demo/<scene>.json, written by make_all.sh) drawn by svgterm
with the brand chrome (and the DEMO chip where the scene is a demo), on the brand night backdrop,
1440 x 900. Nothing is typed or edited: a still is a frame of a recording made with fake keys and
the demo upstream. Stills whose scene shows synthetic data carry the DEMO chip on the frame.

    boot         the last frame of `pulsarcode demo` (DEMO MODE, pinned "Demo machine")
    observatory  the last frame of `pulsarcode top` over synthetic traffic
    stage-deck   the Stage title slide;  stage-diagram  the flow diagram slide;
    stage-chart  the bar chart slide (all from the stage replay: a scripted sample deck, so DEMO)
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

import posters  # noqa: E402
import svgterm  # noqa: E402

# name -> (scene, text that must be on the frame, "first" | "last", demo chip)
STILLS = {
    "boot": ("boot", "ALL SYSTEMS NOMINAL", "last", False),
    "observatory": ("top", "PULSAR OBSERVATORY", "last", True),
    "stage-deck": ("stage", "1/5", "last", True),          # the scripted sample deck: demo data
    "stage-diagram": ("stage", "4/5", "last", True),
    "stage-chart": ("stage", "3/5", "last", True),
}
W, H = 1440, 900


def pick(doc, text: str, which: str):
    idx = [i for i, f in enumerate(doc["frames"]) if text in svgterm.frame_text(f["rows"])]
    if not idx:
        raise ValueError("no frame shows %r" % text)
    i = idx[-1] if which == "last" else idx[0]
    f = doc["frames"][i]
    cur = (doc.get("cursor") or [None] * len(doc["frames"]))[i]
    return (0.0, f["rows"], tuple(cur) if cur else None)


def still_svg(doc, frame, demo: bool) -> str:
    scene = {"cols": doc["cols"], "rows": doc["rows"], "title": doc.get("title", "pulsarcode"),
             "alt": doc.get("alt", ""), "hold_last": 0.0, "demo": demo}
    return svgterm.to_svg([frame], scene, cursor=False, thud=False)


def page(svg: str) -> str:
    data = base64.b64encode(svg.encode()).decode()
    return ("<!doctype html><meta charset=utf-8><style>html,body{margin:0;height:100%%;background:#07060B}"
            "body{display:flex;align-items:center;justify-content:center;"
            "background:radial-gradient(ellipse at 30%% 20%%,#2A0F4A 0,#150A2A 38%%,#07060B 75%%)}"
            "img{width:%dpx;height:auto}</style><img src='data:image/svg+xml;base64,%s'>" % (W - 120, data))


def main(argv) -> int:
    json_dir, out, names = REPO / "docs" / "demo", REPO / "assets" / "screens", []
    it = iter(argv)
    for a in it:
        if a == "--json-dir":
            json_dir = Path(next(it))
        elif a == "--out":
            out = Path(next(it))
        elif a in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            names.append(a)
    names = names or list(STILLS)
    from playwright.sync_api import sync_playwright
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        kw = {"args": ["--no-sandbox"]}
        exe = posters.chromium_path()
        if exe:
            kw["executable_path"] = exe
        b = p.chromium.launch(**kw)
        ctx = b.new_context(viewport={"width": W, "height": H}, reduced_motion="reduce")
        for name in names:
            scene, text, which, demo = STILLS[name]
            doc = json.loads((json_dir / (scene + ".json")).read_text())
            svg = still_svg(doc, pick(doc, text, which), demo or bool(doc.get("demo")))
            pg = ctx.new_page()
            pg.set_content(page(svg))
            pg.wait_for_timeout(200)
            png = pg.screenshot()
            pg.close()
            (out / (name + ".png")).write_bytes(posters.quantise(png, W, 400 * 1024))
            print("screen %-14s %3.0f KB" % (name, (out / (name + ".png")).stat().st_size / 1024))
        b.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
