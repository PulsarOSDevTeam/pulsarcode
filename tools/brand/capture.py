#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Frame-exact captures of the animated brand SVGs (Playwright + Chromium), contact sheets,
the social PNG and an optional MP4 clip of the social layout.

    python tools/brand/capture.py assets/brand/hero-dark.v3.svg --at 0,150,333,600,750 --pulse --out /tmp/h/d
    python tools/brand/capture.py assets/brand/hero-dark.v3.svg --sheet 12 --out /tmp/h/sheet.png
    python tools/brand/capture.py assets/brand/hero-dark.v3.svg --reduced --out /tmp/h/rm
    python tools/brand/capture.py --social                # assets/brand/social.png (+ docs copy)
    python tools/brand/capture.py --mp4                   # $PULSAR_LAUNCH_DIR/social.mp4, never in the repo
    python tools/brand/capture.py --accept --out /tmp/h/accept   # the hero's acceptance checks (see accept)

Each capture loads the document, pauses every animation (document.getAnimations()) and sets its
currentTime to T, so a frame is the exact state at T ms after load, not a race with the clock.
Python 3.9 compatible; needs playwright and Pillow (and ffmpeg for --mp4).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import motion as M  # noqa: E402

ROOT = M.ROOT
FONTS = ROOT / "docs" / "assets" / "fonts"

SEEK_JS = """(t) => {
  const all = document.getAnimations();
  for (const a of all) { a.pause(); a.currentTime = t; }
  return all.length;
}"""

SHAKE_JS = """(t) => {
  for (const a of document.getAnimations()) { a.pause(); a.currentTime = t; }
  const cam = document.querySelector('.cam') || document.querySelector('.h-cam');
  return cam ? getComputedStyle(cam).translate : null;
}"""


def find_chromium() -> str:
    for cand in [os.environ.get("CHROME", ""), "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
                 shutil.which("chromium") or "", shutil.which("google-chrome") or "", shutil.which("chromium-browser") or ""]:
        if cand and os.path.exists(cand):
            return cand
    for p in sorted(Path("/opt/pw-browsers").glob("chromium-*/chrome-linux/chrome")):
        return str(p)
    return ""


class Shooter:
    """One browser for many captures."""

    def __init__(self) -> None:
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        chrome = find_chromium()
        # software raster on one thread: frame captures (and social.png) are reproducible run to run
        kw = {"args": ["--no-sandbox", "--font-render-hinting=none", "--disable-gpu", "--num-raster-threads=1"]}
        if chrome:
            kw["executable_path"] = chrome
        self.browser = self._pw.chromium.launch(**kw)

    def page(self, w: int, h: int, scale: float = 1.0, reduced: bool = False):
        ctx = self.browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=scale,
                                       reduced_motion="reduce" if reduced else "no-preference")
        return ctx.new_page()

    def frames(self, url: str, w: int, h: int, times_ms: Sequence[float], out_prefix: str, scale: float = 1.0,
               reduced: bool = False) -> List[str]:
        page = self.page(w, h, scale, reduced)
        page.goto(url)
        page.wait_for_timeout(150)
        paths = []
        for t in times_ms:
            page.evaluate(SEEK_JS, float(t))
            page.wait_for_timeout(30)
            p = "%s_%05d.png" % (out_prefix, int(round(t)))
            page.screenshot(path=p)
            paths.append(p)
        page.context.close()
        return paths

    def shake_at(self, url: str, w: int, h: int, t_ms: float) -> Optional[str]:
        page = self.page(w, h)
        page.goto(url)
        page.wait_for_timeout(100)
        v = page.evaluate(SHAKE_JS, float(t_ms))
        page.context.close()
        return v

    def close(self) -> None:
        self.browser.close()
        self._pw.stop()


def svg_size(path: Path) -> Tuple[int, int]:
    import re
    head = path.read_text(encoding="utf-8")[:600]
    m = re.search(r'width="(\d+)" height="(\d+)"', head)
    return (int(m.group(1)), int(m.group(2))) if m else (1280, 440)


def contact_sheet(paths: Sequence[str], out: str, cols: int = 3, width: int = 640, labels: Optional[Sequence[str]] = None) -> str:
    from PIL import Image, ImageDraw
    ims = [Image.open(p).convert("RGB") for p in paths]
    k = width / ims[0].width
    tw, th = width, int(ims[0].height * k)
    rows = (len(ims) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tw + (cols + 1) * 6, rows * (th + 18) + (rows + 1) * 6), (24, 22, 32))
    d = ImageDraw.Draw(sheet)
    for i, im in enumerate(ims):
        x = 6 + (i % cols) * (tw + 6)
        y = 6 + (i // cols) * (th + 24)
        sheet.paste(im.resize((tw, th), Image.LANCZOS), (x, y + 16))
        d.text((x, y + 2), labels[i] if labels else Path(paths[i]).stem, fill=(200, 196, 220))
    sheet.save(out)
    return out


def hero_pulse_times() -> List[float]:
    import hero
    t = hero.README.first_pulse() * 1000
    return [t + 60, t + 300]


SOCIAL_SIZE = (1280, 640)


def social_html(svg: str) -> str:
    fonts = []
    for fam, f, wt in (("Inter", "inter-latin.woff2", "100 900"), ("JetBrains Mono", "jetbrains-mono-latin.woff2", "100 800")):
        p = FONTS / f
        if p.exists():
            fonts.append("@font-face{font-family:'%s';src:url('%s') format('woff2');font-weight:%s}" % (fam, p.as_uri(), wt))
    import hero
    return ("<!doctype html><html><head><meta charset='utf-8'><style>%s html,body{margin:0;width:1280px;height:640px;"
            "overflow:hidden;background:#07060B;position:relative}svg{display:block}%s</style></head><body>%s%s</body></html>"
            % ("".join(fonts), hero.SOCIAL_CSS, svg, hero.social_overlay_html()))


def social_time() -> float:
    import hero
    return hero.SOCIAL.first_pulse() * 1000 + 60


def _social_shot(sh: "Shooter", page_path: Path, raw: Path) -> bytes:
    page = sh.page(*SOCIAL_SIZE)
    try:
        page.goto(page_path.as_uri())
        # load both faces explicitly (a cold font cache must never fall back to a system face)
        page.evaluate("""() => Promise.all([document.fonts.load("600 34px Inter"), document.fonts.load("500 14px Inter"),
                         document.fonts.load("16px 'JetBrains Mono'")]).then(() => document.fonts.ready).then(() => 1)""")
        page.wait_for_timeout(300)
        page.evaluate(SEEK_JS, social_time())
        # two animation frames after the seek, so every paused animation has been composited
        page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => r(1))))")
        page.wait_for_timeout(50)
        page.screenshot(path=str(raw))
    finally:
        page.context.close()
    return raw.read_bytes()


def render_social(out: Path, shooter: Optional[Shooter] = None, tries: int = 4) -> Path:
    """social.png: the social layout at first_full_pulse + 60 ms. Shots are taken in fresh pages until two
    in a row are byte-identical (a Chromium raster can differ by a few pixels between runs), then reduced
    by `posterize`, which has no error diffusion: a stray raster pixel cannot spread across the image, so
    the committed PNG does not churn on every make_assets run."""
    import hero
    svg = hero.build("dark", hero.SOCIAL, True)
    own = shooter is None
    sh = shooter or Shooter()
    try:
        with tempfile.TemporaryDirectory() as td:
            page_path = Path(td) / "social.html"
            page_path.write_text(social_html(svg), encoding="utf-8")
            raw = Path(td) / "raw.png"
            prev = _social_shot(sh, page_path, raw)
            for _ in range(tries - 1):
                cur = _social_shot(sh, page_path, raw)
                if cur == prev:
                    break
                prev = cur
            fresh = Path(td) / "social.png"
            posterize(raw, fresh, 300 * 1024)
            if not (out.exists() and same_picture(out, fresh)):
                shutil.copyfile(fresh, out)
    finally:
        if own:
            sh.close()
    return out


def same_picture(a: Path, b: Path, level: int = 4, share: float = .001) -> bool:
    """True when two PNGs differ only by raster noise: no channel off by more than `level` and at most
    `share` of the pixels touched at all. Chromium's software raster can still move a thin anti-aliased
    gradient by one level in a small fraction of runs; the committed file then stays as it is."""
    from PIL import Image, ImageChops
    ia, ib = Image.open(a).convert("RGB"), Image.open(b).convert("RGB")
    if ia.size != ib.size:
        return False
    d = ImageChops.difference(ia, ib).convert("L").tobytes()
    return max(d) <= level and sum(1 for v in d if v) <= share * len(d)


BAYER4 = ((0, 8, 2, 10), (12, 4, 14, 6), (3, 11, 1, 9), (15, 7, 13, 5))


def posterize(src: Path, out: Path, budget: int) -> None:
    """Save an RGB `src` to `out` within `budget` bytes: lossless 24-bit when it fits, else each channel
    reduced to a coarser step with a 4x4 ordered (Bayer) threshold. Ordered dithering is a pure function
    of each pixel and its position: no palette search, no error diffusion, no speckle in smooth gradients,
    and identical input gives identical output."""
    from PIL import Image, ImageChops
    im = Image.open(src).convert("RGB")
    im.save(out, optimize=True)
    if out.stat().st_size <= budget:
        return
    for step in (2, 3, 4, 6, 8):
        tile = Image.new("L", (4, 4))
        tile.putdata([int((BAYER4[y][x] + .5) / 16.0 * step) for y in range(4) for x in range(4)])
        thr = Image.new("L", im.size)
        for y in range(0, im.size[1], 4):
            for x in range(0, im.size[0], 4):
                thr.paste(tile, (x, y))
        lut = [min(255, (v // step) * step) for v in range(256)]
        chans = [ImageChops.add(c, thr).point(lut) for c in im.split()]
        Image.merge("RGB", chans).save(out, optimize=True)
        if out.stat().st_size <= budget:
            return


def quantise(src: Path, out: Path, budget: int, alpha: bool = False) -> None:
    """Save `src` to `out` within `budget` bytes: lossless first, then a 256-colour octree palette
    remapped with Floyd-Steinberg dithering (smooth gradients, no banding)."""
    from PIL import Image
    im = Image.open(src).convert("RGBA" if alpha else "RGB")
    im.save(out, optimize=True)
    if not budget or out.stat().st_size <= budget:
        return
    for colors in (256, 192, 128, 96):
        pal = im.quantize(colors=colors, method=Image.Quantize.FASTOCTREE)
        q = pal if alpha else im.quantize(palette=pal, dither=Image.Dither.FLOYDSTEINBERG)
        q.save(out, optimize=True)
        if out.stat().st_size <= budget:
            return


def render_mp4(out_dir: Path, fps: int = 60) -> Path:
    """1280x720 clip of the social layout: real P1/P2, duration L + 0.4 s, the last 400 ms cross-fading
    into frame 0."""
    import hero
    from PIL import Image
    out_dir.mkdir(parents=True, exist_ok=True)
    svg = hero.build("dark", hero.SOCIAL, True)
    total = M.L + 0.4
    n = int(round(total * fps))
    sh = Shooter()
    with tempfile.TemporaryDirectory() as td:
        page_path = Path(td) / "social.html"
        page_path.write_text(social_html(svg).replace("height:640px", "height:720px").replace(
            "<body>", "<body style='padding-top:40px;box-sizing:border-box'>"), encoding="utf-8")
        page = sh.page(1280, 720)
        page.goto(page_path.as_uri())
        page.wait_for_timeout(300)
        frames = []
        for i in range(n):
            t = i * 1000.0 / fps
            page.evaluate(SEEK_JS, t)
            p = Path(td) / ("f%05d.png" % i)
            page.screenshot(path=str(p))
            frames.append(p)
        page.context.close()
        first = Image.open(frames[0]).convert("RGB")
        fade = int(0.4 * fps)
        for k in range(fade):
            p = frames[n - fade + k]
            a = (k + 1) / float(fade)
            Image.blend(Image.open(p).convert("RGB"), first, a).save(p)
        out = out_dir / "social.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps), "-i", str(Path(td) / "f%05d.png"),
                        "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)],
                       check=True)
    sh.close()
    return out


def _lum(px) -> float:
    def ch(v: float) -> float:
        v = v / 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(px[0]) + 0.7152 * ch(px[1]) + 0.0722 * ch(px[2])


def accept(out_dir: str) -> List[Tuple[str, bool, str]]:
    """Run the hero's acceptance checks on rendered frames; returns (id, ok, note) per check.

    A1  at 0 ms the unlit wordmark reads as a faint ghost and nothing outside the core is bright
    A3  at 600 ms the struck cells near the core are more saturated than the far ones (hot-to-colour)
    A4  by 750 ms every cell reaches 90 % of its final brightness (A4-light: no struck cell is still
        white on the light plate at 600 ms)
    A6  the pulse wash adds at most .10 luminance to the letters
    A8  7 % into the shake the camera sits on its first keyframe, (-6, 3) px
    A9  the reduced-motion render matches the static poster (dark and light)
    """
    import math
    import hero
    from PIL import Image, ImageChops
    lo = hero.README
    res: List[Tuple[str, bool, str]] = []
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    brand = ROOT / "assets" / "brand"
    sh = Shooter()
    try:
        tp = lo.first_pulse() * 1000
        times = [0, 333, 600, 750, 1650, 3000, tp + 60, tp + 360]
        dark = sh.frames((brand / "hero-dark.v3.svg").as_uri(), lo.w, lo.h, times, str(d / "a_dark"))
        light = sh.frames((brand / "hero-light.v3.svg").as_uri(), lo.w, lo.h, [600, 3000], str(d / "a_light"))
        ims = {t: Image.open(p).convert("RGB") for t, p in zip(times, dark)}
        centres = [(int(lo.ox + (x + .5) * lo.cw), int(lo.oy + (y + .5) * lo.ch)) for x, y in M.wordmark_cells()]
        f0 = ims[0]
        bg = sum(_lum(f0.getpixel((x, 20))) for x in range(20, 200, 5)) / 36.0
        cell = sum(_lum(f0.getpixel(c)) for c in centres) / len(centres)
        hot = [(x, y) for x in range(lo.w) for y in range(lo.h) if math.hypot(x - lo.cx, y - lo.cy) > 12
               and _lum(f0.getpixel((x, y))) > .6]
        res.append(("A1", cell - bg >= .06 * max(bg, 1e-3) and not hot,
                    "ghost lum %.4f vs bg %.4f; %d bright px outside core" % (cell, bg, len(hot))))
        f3 = ims[3000]
        def at(c, t):  # where a world point lands under the dolly (1.04 -> 1 over 5 s, E_SETTLE)
            k = 1 + .04 * (1 - M.bezier_y(M.E_SETTLE, min(1.0, t / 5000.0)))
            ox, oy = lo.dolly_origin
            return (int(round(ox + (c[0] - ox) * k)), int(round(oy + (c[1] - oy) * k)))

        def peak(im, c, t):  # struck cells are mid-kick (7 px): read the brightest pixel of the cell's footprint
            q = at(c, t)
            return max(_lum(im.getpixel((q[0] + dx, q[1] + dy))) for dx in range(-6, 7, 2) for dy in range(-12, 13, 3))
        lows = [c for c in centres if peak(ims[750], c, 750) < .9 * _lum(f3.getpixel(at(c, 3000)))]
        res.append(("A4", not lows, "%d cells below 90%% of final at 750 ms" % len(lows)))
        l600 = Image.open(light[0]).convert("RGB")
        struck = [c for c in centres if hero.strike_time(math.hypot(c[0] - lo.cx, c[1] - lo.cy)) < .6]
        white = [c for c in struck if min(l600.getpixel(c)) > 235]
        res.append(("A4-light", not white, "%d of %d struck cells white at 600 ms on the plate" % (len(white), len(struck))))
        near = [c for c in centres if c[0] < lo.ox + 120]
        far = [c for c in centres if c[0] > lo.ox + lo.wm_w - 200]
        sat = lambda p: max(p) - min(p)  # noqa: E731
        ns = sum(sat(ims[600].getpixel(c)) for c in near) / len(near)
        fs = sum(sat(ims[600].getpixel(c)) for c in far) / len(far)
        res.append(("A3", ns > fs + 40, "saturation near %.0f vs far %.0f at 600 ms" % (ns, fs)))
        pulse, after = ims[tp + 60], ims[tp + 360]
        worst = max(_lum(pulse.getpixel(c)) - _lum(after.getpixel(c)) for c in centres)
        res.append(("A6", worst <= .10, "max letter luminance added by the wash %.3f" % worst))
        v = sh.shake_at((brand / "hero-dark.v3.svg").as_uri(), lo.w, lo.h, M.IGNITION * 1000 + .07 * M.SHAKE_MS)
        res.append(("A8", (v or "").replace("px", "").split() == ["-6", "3"], "cam translate %r" % v))
        for theme in ("dark", "light"):
            st = sh.frames((brand / ("hero-static-%s.v3.svg" % theme)).as_uri(), lo.w, lo.h, [0], str(d / ("a_static_" + theme)))[0]
            rm = sh.frames((brand / ("hero-%s.v3.svg" % theme)).as_uri(), lo.w, lo.h, [2000], str(d / ("a_rm_" + theme)), reduced=True)[0]
            a, b = Image.open(st).convert("RGB"), Image.open(rm).convert("RGB")
            diff = ImageChops.difference(a, b).convert("L")
            changed = sum(1 for p in diff.tobytes() if p > 8) / float(lo.w * lo.h)
            res.append(("A9-" + theme, changed <= .01, "%.2f%% pixels differ" % (changed * 100)))
        contact_sheet(dark, str(d / "accept_sheet.png"), labels=["%d ms" % t for t in times])
    finally:
        sh.close()
    return res


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    def opt(name: str, default: str = "") -> str:
        return argv[argv.index(name) + 1] if name in argv else default
    if "--social" in argv:
        out = Path(opt("--out", str(ROOT / "assets" / "brand" / "social.png")))
        render_social(out)
        print(out, "%.1f KB" % (out.stat().st_size / 1024))
        return 0
    if "--accept" in argv:
        ok = True
        for k, good, note in accept(opt("--out", tempfile.gettempdir() + "/pulsar-accept")):
            ok &= good
            print("%-9s %s  %s" % (k, "PASS" if good else "FAIL", note))
        return 0 if ok else 1
    if "--mp4" in argv:
        out = render_mp4(Path(os.environ.get("PULSAR_LAUNCH_DIR", "/tmp/pulsar-launch/")))
        print(out)
        return 0
    files = [a for a in argv if a.endswith(".svg")]
    if not files:
        print(__doc__)
        return 2
    path = Path(files[0]).resolve()
    w, h = svg_size(path)
    out = opt("--out", str(Path(tempfile.gettempdir()) / path.stem))
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    sh = Shooter()
    try:
        if "--sheet" in argv:
            k = int(opt("--sheet", "12"))
            times = [0, 150, 333, 450, 600, 750, 900, 1250, 1650, 3000] + hero_pulse_times()
            times = times[:k]
            paths = sh.frames(path.as_uri(), w, h, times, out.replace(".png", ""))
            print(contact_sheet(paths, out if out.endswith(".png") else out + "_sheet.png",
                                labels=["%d ms" % t for t in times]))
            return 0
        if "--reduced" in argv:
            print(sh.frames(path.as_uri(), w, h, [2000], out + "_rm", reduced=True)[0])
            return 0
        times = [float(x) for x in opt("--at", "0,300,750,1650,3000").split(",") if x]
        if "--pulse" in argv:
            times += hero_pulse_times()
        for p in sh.frames(path.as_uri(), w, h, times, out):
            print(p)
        if "--shake" in argv:
            print("cam translate at 7%% of the shake:", sh.shake_at(path.as_uri(), w, h, M.IGNITION * 1000 + .07 * M.SHAKE_MS))
    finally:
        sh.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
