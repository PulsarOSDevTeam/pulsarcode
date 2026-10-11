#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""The mark and its family: mark.svg, favicon.svg (+ 16/32 px PNGs), apple-touch-icon, avatar,
and the horizontal logo lockups.

The mark is a still of the hero star: an ICE core, ONE bipolar beam (beam 1 only) at fx's t = 0
angle, and one thin spectrum ring tilted into an ellipse whose long axis is 90 degrees off the beam.
Two rays plus a ring: never a starburst, never petals. Rotational symmetry is order 2 at most
(and the two beam ends differ in colour, so in colour it is order 1). Python 3.9 compatible.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import motion as M  # noqa: E402
import svgkit as K  # noqa: E402
from svgkit import n  # noqa: E402

ROOT = M.ROOT
BRAND = ROOT / "assets" / "brand"
SITE = ROOT / "docs" / "assets" / "brand"
INK = "#1B1530"
PLASMA_INK = "#6B3FD1"


def _beam_rot() -> float:
    return M.A1_0 + 90          # wedges are drawn along -y; this points the north (cyan) end at A1_0


def mark_parts(theme: str, p: str, c: float, unit: float, glow: bool = True, ring: bool = True,
               core_r: float = 7, glow_r: float = 20, half: float = 7, tip: float = 30, ring_rx: float = 22,
               ring_w: float = 2) -> (str, str):
    """(defs, body) for the mark centred at (c, c); lengths in mark units times `unit`."""
    dark = theme == "dark"
    core = M.ICE if dark else INK
    u = unit
    cols = M.THEMES[theme]["spectrum"]
    defs = []
    if glow:
        defs.append(K.radial(p + "g", [(0, core, .85 if dark else .35), (.45, M.BEAM if dark else PLASMA_INK, .28 if dark else .12),
                                       (1, M.PLASMA if dark else PLASMA_INK, 0)]))
    north = M.BEAM if dark else "#0A8FB0"
    south = M.PLASMA if dark else "#7A2FF0"
    defs.append(K.linear_user(p + "n", 0, 0, [(0, core, 1), (.35, north, 1), (1, north, .2)], y1=0, y2=-tip * u))
    defs.append(K.linear_user(p + "s", 0, 0, [(0, core, 1), (.35, south, 1), (1, south, .2)], y1=0, y2=tip * u))
    if ring and dark:
        defs.append(K.linear_user(p + "r", -ring_rx * u, ring_rx * u, [(o, col, 1) for o, col in zip(M.SPECTRUM_STOPS, cols)]))  # type: ignore[arg-type]
    body = []
    if glow:
        body.append(K.circle(c, c, glow_r * u, fill="url(#%sg)" % p))
    rx, ry = ring_rx * u, ring_rx * .42 * u
    ring_g = '<g transform="translate(%s %s) rotate(%s)" fill="none" stroke="%s" stroke-width="%s">' % (
        n(c), n(c), n(M.A1_0 + 90, 2), "url(#%sr)" % p if dark else PLASMA_INK, n(ring_w * u))
    if ring:
        # back half of the ring under the beam, front half over it: the beam passes through the ring
        body.append(ring_g + '<path d="M%s 0A%s %s 0 0 1 %s 0"/></g>' % (n(-rx), n(rx), n(ry), n(rx)))
    hw, tw, L = half * u, .5 * u, tip * u
    body.append('<g transform="translate(%s %s) rotate(%s)"><path d="M%s 0L%s %sH%sL%s 0Z" fill="url(#%sn)"/>'
                '<path d="M%s 0L%s %sH%sL%s 0Z" fill="url(#%ss)"/></g>' % (
                    n(c), n(c), n(_beam_rot(), 2), n(-hw), n(-tw), n(-L), n(tw), n(hw), p,
                    n(-hw), n(-tw), n(L), n(tw), n(hw), p))
    if ring:
        body.append(ring_g + '<path d="M%s 0A%s %s 0 0 0 %s 0"/></g>' % (n(-rx), n(rx), n(ry), n(rx)))
    body.append(K.circle(c, c, core_r * u, fill=core))
    return "".join(defs), "".join(body)


def mark_svg(theme: str = "dark") -> str:
    defs, body = mark_parts(theme, "m", 32, 1.0)
    return K.svg_doc(64, 64, body, defs=defs, title="Pulsar Code", label="Pulsar Code mark")


def favicon_svg(png: bool = False) -> str:
    """32 grid, hinted: core r 3.5 on the pixel centre, beam base 7 px, ring 1.5 px; never animated.
    The light scheme swaps the ICE core and glow for ink. png=True: the raster source (no media query;
    a dark rim keeps the ICE core visible on light tabs)."""
    a = n(M.A1_0 + 90, 2)
    css = "" if png else "<style>@media (prefers-color-scheme:light){.c{fill:%s}.g{opacity:.3}}</style>" % INK
    rim = ' stroke="#2A1452"' if png else ""
    sp = "".join('<stop offset="%s" stop-color="%s"/>' % (n(o), c) for o, c in zip(M.SPECTRUM_STOPS, M.THEMES["dark"]["spectrum"]))  # type: ignore[arg-type]
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32">%s<defs>'
            '<radialGradient id="g"><stop offset="0" stop-color="%s" stop-opacity=".8"/><stop offset="1" stop-color="%s" stop-opacity="0"/></radialGradient>'
            '<linearGradient id="n" x1="0" x2="0" y1="1" y2="0"><stop offset="0" stop-color="%s"/><stop offset="1" stop-color="%s" stop-opacity=".3"/></linearGradient>'
            '<linearGradient id="s" x1="0" x2="0" y2="1"><stop offset="0" stop-color="%s"/><stop offset="1" stop-color="%s" stop-opacity=".3"/></linearGradient>'
            '<linearGradient id="r">%s</linearGradient></defs>'
            '<circle class="g" cx="16" cy="16" r="9" fill="url(#g)"/>'
            '<g transform="translate(16 16) rotate(%s)"><ellipse rx="11" ry="4.6" fill="none" stroke="url(#r)" stroke-width="1.5"/>'
            '<path d="M-3.5 0L-.3-15h.6L3.5 0Z" fill="url(#n)"/><path d="M-3.5 0L-.3 15h.6L3.5 0Z" fill="url(#s)"/></g>'
            '<circle class="c" cx="16" cy="16" r="3.5" fill="%s"%s/></svg>\n'
            % (css, M.ICE, M.BEAM, M.BEAM, M.BEAM, M.PLASMA, M.PLASMA, sp, a, M.ICE, rim))


def logo_svg(theme: str = "dark") -> str:
    """Horizontal lockup: mark + PULSAR (run-length rects) + spaced CODE; clear space = 4 core diameters."""
    t = M.THEMES[theme]
    cw, ch = 8.0, 16.0
    s = 1.7                                    # mark scale: the ring spans the wordmark block
    core_d = 14 * s
    pad = 4 * core_d
    mark = 64 * s
    gap = 2 * core_d
    wm_w = M.WORDMARK_COLS * cw
    code_px = 2.0
    code_pitch = 20.0
    code_h = 7 * code_px
    block_h = 5 * ch + 14 + code_h
    W = int(round(pad * 2 + mark + gap + wm_w))
    H = int(round(pad * 2 + max(mark, block_h)))
    mx, my = pad, (H - mark) / 2
    ox = pad + mark + gap
    oy = (H - block_h) / 2
    defs, body = mark_parts(theme, "m", 32, 1.0)
    out = ['<g transform="translate(%s %s) scale(%s)">%s</g>' % (n(mx), n(my), n(s), body)]
    shadow = "".join(K.rect(ox + c * cw + 3, oy + r * ch + 3, ln * cw, ch) for r, c, ln in M.wordmark_runs())
    cells = "".join(K.rect(ox + c * cw, oy + r * ch, ln * cw, ch) for r, c, ln in M.wordmark_runs())
    out.append('<g fill="%s" opacity="%s">%s</g>' % (t["shadow"], n(t["shadow_op"]), shadow))
    out.append('<g fill="url(#ls)" shape-rendering="crispEdges">%s</g>' % cells)
    cy = oy + 5 * ch + 14
    code_w = K.pixel_text_width("CODE", code_px, code_pitch)
    mid = ox + wm_w / 2
    cx0 = mid - code_w / 2
    out.append('<path d="%s" fill="%s"/>' % (K.pixel_text_path("CODE", cx0, cy, code_px, code_pitch), t["ink"]))
    ry = cy + 3.5 * code_px - .75
    out.append(K.rect(ox, ry, cx0 - 16 - ox, 1.5, fill="url(#ls)"))
    out.append(K.rect(cx0 + code_w + 16, ry, ox + wm_w - (cx0 + code_w + 16), 1.5, fill="url(#ls)"))
    defs += K.linear_user("ls", ox, ox + wm_w, K.spectrum_pairs(t))
    return K.svg_doc(W, H, "".join(out), defs=defs, title="Pulsar Code", label="Pulsar Code")


def avatar_svg(size: int, mark_frac: float = .62) -> str:
    s = size * mark_frac / 64
    defs, body = mark_parts("dark", "a", 32, 1.0)
    defs += K.radial("neb", [(0, "#2A0F4A", .9), (.55, "#150A2A", .6), (1, "#07060B", 0)], size * .45, size * .42, size * .75)
    out = [K.rect(0, 0, size, size, fill="#07060B"), K.rect(0, 0, size, size, fill="url(#neb)"),
           '<g transform="translate(%s %s) scale(%s)">%s</g>' % (n((size - 64 * s) / 2), n((size - 64 * s) / 2), n(s, 4), body)]
    return K.svg_doc(size, size, "".join(out), defs=defs, label="Pulsar Code")


def favicon16_png(out: Path) -> Path:
    """16 px pixel art: a 2x2 ICE core, beam pixels on the exact diagonal (cyan down-right toward the
    north end at fx's t = 0, violet up-left), ring dropped; a dark rim keeps the core on light tabs."""
    from PIL import Image
    im = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
    px = im.load()
    rim = (42, 20, 82, 255)
    for x, y in ((6, 7), (6, 8), (9, 7), (9, 8), (7, 6), (8, 6), (7, 9), (8, 9), (6, 6), (9, 9)):
        px[x, y] = rim
    for x, y in ((7, 7), (8, 7), (7, 8), (8, 8)):
        px[x, y] = (236, 246, 255, 255)
    cyan, violet = (54, 226, 255), (157, 77, 255)
    for i, a in enumerate((255, 220, 160, 100, 55)):
        px[10 + i, 10 + i] = cyan + (a,)
        px[5 - i, 5 - i] = violet + (a,)
    px[9, 9] = (180, 240, 255, 255)
    px[6, 6] = (200, 170, 255, 255)
    im.save(out, optimize=True)
    return out


def render_png(svg: str, size: int, out: Path, transparent: bool = True, budget: int = 0) -> Path:
    import capture
    sh = capture.Shooter()
    try:
        with tempfile.TemporaryDirectory() as td:
            page_path = Path(td) / "m.html"
            page_path.write_text("<!doctype html><html><head><meta charset='utf-8'><style>html,body{margin:0;background:transparent}"
                                 "svg{display:block;width:%dpx;height:%dpx}</style></head><body>%s</body></html>" % (size, size, svg),
                                 encoding="utf-8")
            page = sh.page(size, size)
            page.goto(page_path.as_uri())
            page.wait_for_timeout(80)
            raw = Path(td) / "raw.png"
            page.screenshot(path=str(raw), omit_background=transparent)
            page.context.close()
            capture.quantise(raw, out, budget, alpha=transparent)
    finally:
        sh.close()
    return out


def svg_outputs() -> Dict[Path, str]:
    return {
        BRAND / "mark.svg": mark_svg("dark"),
        BRAND / "favicon.svg": favicon_svg(),
        BRAND / "logo.svg": logo_svg("dark"),
        BRAND / "logo-light.svg": logo_svg("light"),
    }


def png_outputs() -> List[Path]:
    out = []
    out.append(favicon16_png(BRAND / "favicon-16.png"))
    out.append(render_png(favicon_svg(png=True), 32, BRAND / "favicon-32.png", budget=2048))
    out.append(render_png(avatar_svg(180, .66), 180, BRAND / "apple-touch-icon.png", transparent=False, budget=24 * 1024))
    out.append(render_png(avatar_svg(512), 512, BRAND / "avatar-512.png", transparent=False, budget=40 * 1024))
    return out


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    BRAND.mkdir(parents=True, exist_ok=True)
    for path, svg in svg_outputs().items():
        path.write_text(svg, encoding="utf-8")
        print("%-36s %6.1f KB" % (path.relative_to(ROOT), len(svg.encode()) / 1024))
    if "--no-png" not in argv:
        for p in png_outputs():
            print("%-36s %6.1f KB" % (p.relative_to(ROOT), p.stat().st_size / 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
