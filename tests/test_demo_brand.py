# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""No replay, poster or still carries Claude Code's block-art mascot (Anthropic's mark).

The session scene crops Claude Code's welcome banner away; these tests check the shipped assets
themselves: the site player's frame JSON (glyph text), the SVG replays (whose block glyphs are drawn
as geometry, so the rectangles are decoded back to quadrant glyphs) and the PNG posters and stills
(a patch of the mascot's default colour, or its shape in either body colour: Pulsar's own theme
paints it purple)."""

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "tools" / "demo"
sys.path.insert(0, str(DEMO))

import svgterm  # noqa: E402

SCENES = ("boot", "uplink", "picker", "session", "stage", "top")

# the classic banner and other poses of the mascot, as Claude Code draws them
MASCOT_POSES = [
    [" ▐▛███▜▌ ", "▝▜█████▛▘", "  ▘▘ ▝▝  "],
    [" ▐█▜██▛█ ", "▝▜██████▀", " ▝▝   ▝▝ "],
    ["  ██▛██▛▌ ", " ▝█████▛ ", "  ▘▘  ▘▘ "],
    ["  ▐█▛█▜  ", "  ▐████  ", "  ▝▝ ▝▝  "],
    ["   ▐██   ", "   ▐██   ", "   ▝ ▝   "],
    ["▗▟███▟█▄", " ▜██████▘", " ▝▝   ▝▝ "],
]

# quarter cells (left/right, top/bottom) to a glyph, the inverse of svgterm.QUAD
GLYPH = {tuple(int(b) for b in bits): ch for ch, bits in svgterm.QUAD.items()}
RECT = re.compile(r"M(-?[\d.]+) (-?[\d.]+)h([\d.]+)v([\d.]+)h-[\d.]+z")
ROW = re.compile(r'<g id="r(\w+)">(.*?)(?=<g id="r|</defs>)', re.S)


def svg_block_lines(svg: str):
    """Each row symbol of a replay SVG decoded back to its block glyphs: every filled rectangle at
    least a quarter cell in size marks the quarter cells whose centres it covers. Dots, rules and
    text are ignored; a cell with no block geometry reads as a space."""
    hw, hh = svgterm.CW / 2, svgterm.CH / 2
    lines = []
    for _rid, body in ROW.findall(svg):
        quads = set()
        for d in re.findall(r'<path[^>]*\sd="([^"]*)"', body):
            for x, y, w, h in RECT.findall(d):
                x, y, w, h = float(x), float(y), float(w), float(h)
                if w < hw - .05 or h < hh - .05:
                    continue
                for qx in range(int(x // hw), int((x + w) // hw) + 1):
                    for qy in (0, 1):
                        cx, cy = (qx + .5) * hw, (qy + .5) * hh
                        if x <= cx <= x + w and y <= cy <= y + h:
                            quads.add((qx, qy))
        if not quads:
            continue
        cols = max(qx for qx, _ in quads) // 2 + 1
        line = ""
        for c in range(cols):
            key = ((2 * c, 0) in quads, (2 * c + 1, 0) in quads, (2 * c, 1) in quads, (2 * c + 1, 1) in quads)
            line += GLYPH.get(tuple(int(k) for k in key), " ")
        lines.append(line)
    return lines


def _rows(pose, colour="#d77757"):
    return [[(10, line, colour, False)] for line in pose]


@pytest.mark.parametrize("pose", MASCOT_POSES)
def test_the_pattern_knows_every_pose(pose):
    assert svgterm.mascot_lines("\n".join(pose))


@pytest.mark.parametrize("pose", MASCOT_POSES)
def test_the_svg_decoder_finds_a_drawn_mascot(pose):
    """Positive control: the mascot rendered by svgterm is found again in the geometry."""
    frames = [(0.0, _rows(pose) + [[]] * 3, None)]
    svg = svgterm.to_svg(frames, {"cols": 30, "rows": 6, "title": "t", "alt": "a", "hold_last": 1.0})
    assert any(svgterm.CLI_MASCOT.search(line) for line in svg_block_lines(svg))


@pytest.mark.parametrize("name", SCENES)
def test_frame_json_shows_no_mascot(name):
    doc = json.loads((ROOT / "docs" / "demo" / (name + ".json")).read_text(encoding="utf-8"))
    for f in doc["frames"]:
        hit = svgterm.mascot_lines(svgterm.frame_text(f["rows"]))
        assert not hit, "%s.json at %ss shows Claude Code's mascot: %r" % (name, f["t"], hit[0])


@pytest.mark.parametrize("name", SCENES)
def test_svg_replay_shows_no_mascot(name):
    svg = (ROOT / "assets" / "demo" / (name + ".svg")).read_text(encoding="utf-8")
    hit = [line for line in svg_block_lines(svg) if svgterm.CLI_MASCOT.search(line)]
    assert not hit, "%s.svg draws Claude Code's mascot: %r" % (name, hit[0])
    assert not svgterm.mascot_lines(re.sub(r"<[^>]+>", "\n", svg))     # nor as text


def _mascot_pixels(path: Path) -> int:
    """Pixels of the mascot's body colour (rgb 215,119,87) in a PNG."""
    Image = pytest.importorskip("PIL.Image")
    im = Image.open(path).convert("RGBA").convert("RGB")
    im.thumbnail((720, 720))
    px = im.tobytes()
    return sum(1 for i in range(0, len(px), 3)
               if abs(px[i] - 215) <= 10 and abs(px[i + 1] - 119) <= 10 and abs(px[i + 2] - 87) <= 10)


def _body_mask(im, rgb, tol=24):
    """The pixels within `tol` of `rgb` in every channel, as an L image (255 where they are)."""
    from PIL import ImageChops
    r, g, b = (c.point(lambda v, t=t: 255 if abs(v - t) <= tol else 0) for c, t in zip(im.split(), rgb))
    return ImageChops.multiply(ImageChops.multiply(r, g), b)


def _blobs(mask):
    """The 4-connected regions of a mask's set pixels, as lists of (x, y)."""
    w = mask.size[0]
    on = {i for i, v in enumerate(mask.tobytes()) if v}
    out = []
    while on:
        stack = [on.pop()]
        blob = list(stack)
        while stack:
            i = stack.pop()
            x = i % w
            for j in (i - 1 if x else -1, i + 1 if x < w - 1 else -1, i - w, i + w):
                if j in on:
                    on.discard(j)
                    stack.append(j)
                    blob.append(j)
        out.append([(i % w, i // w) for i in blob])
    return out


def _holes(blob):
    """The background regions a blob encloses (4-connected), as lists of (x, y)."""
    xs, ys = [p[0] for p in blob], [p[1] for p in blob]
    x0, y0 = min(xs) - 1, min(ys) - 1
    w, h = max(xs) - x0 + 2, max(ys) - y0 + 2
    body = {(x - x0, y - y0) for x, y in blob}
    seen, regions = set(), []
    for start in [(0, 0)] + [(x, y) for y in range(h) for x in range(w)]:
        if start in body or start in seen:
            continue
        seen.add(start)
        stack, region = [start], [start]
        while stack:
            x, y = stack.pop()
            for q in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if 0 <= q[0] < w and 0 <= q[1] < h and q not in body and q not in seen:
                    seen.add(q)
                    stack.append(q)
                    region.append(q)
        regions.append(region)
    return regions[1:]          # the first region is the outside: (0, 0) lies in the one-pixel margin


def _centre(region):
    ys = [p[1] for p in region]
    return sum(p[0] for p in region) / len(region), sum(ys) / len(ys), max(ys) - min(ys) + 1


def _mascot_shapes(path: Path):
    """Blobs in a mascot body colour that have the mascot's face: a body that is not tall, with two
    enclosed holes of about the same size side by side, each small next to the body (its eyes). The
    purple Pulsar's theme gives the mascot is also the brand's plasma, so colour alone cannot tell the
    mascot from Pulsar's own art (chart bars, wordmark letters, frames); this shape can. The side poses
    have no enclosed eyes: the glyph checks on the frame JSON and the SVG replays cover them."""
    Image = pytest.importorskip("PIL.Image")
    im = Image.open(path).convert("RGBA").convert("RGB")
    found = []
    for rgb in MASCOT_BODY:
        for blob in _blobs(_body_mask(im, rgb)):
            if len(blob) < 150:
                continue
            xs, ys = [p[0] for p in blob], [p[1] for p in blob]
            bw, bh = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
            if bh < 10 or bw < .8 * bh:
                continue
            eyes = sorted((e for e in _holes(blob) if len(e) >= 6), key=len, reverse=True)[:2]
            if len(eyes) < 2 or len(eyes[0]) > 3 * len(eyes[1]) or len(eyes[0]) > .15 * len(blob):
                continue
            (ax, ay, ah), (ex, ey, eh) = _centre(eyes[0]), _centre(eyes[1])
            if abs(ay - ey) <= max(ah, eh) and abs(ax - ex) >= bw / 5:
                found.append("#%02X%02X%02X shape at %d,%d" % (rgb + (min(xs), min(ys))))
    return found


def _mascot_problems(path: Path):
    """Why a PNG looks like it shows the mascot (empty when it does not): a patch of its default colour,
    or its shape in either body colour."""
    n = _mascot_pixels(path)
    return (["%d pixels of the mascot's default colour" % n] if n >= 40 else []) + _mascot_shapes(path)


@pytest.mark.parametrize("path", sorted((ROOT / "assets" / "demo" / "posters").glob("*.png"))
                         + sorted((ROOT / "docs" / "demo" / "posters").glob("*.png"))
                         + sorted((ROOT / "assets" / "screens").glob("*.png")), ids=lambda p: p.name)
def test_posters_and_stills_show_no_mascot(path):
    problems = _mascot_problems(path)
    assert not problems, "%s shows Claude Code's mascot: %s" % (path.relative_to(ROOT), problems)


# The mascot's body colours: Claude Code's default, and the one Pulsar's own theme gives it
# (pulsar.plugin.THEME, "clawd_body").
def _rgb(hexcolour: str):
    h = hexcolour.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


MASCOT_BODY = ((215, 119, 87), _rgb(__import__("pulsar.plugin", fromlist=["THEME"]).THEME["overrides"]["clawd_body"]))


def _draw_pose(path: Path, pose, colour, cell=(10, 20), extra=None) -> Path:
    """A synthetic picture (never a real capture): the pose's quadrant glyphs drawn as rectangles in
    `colour` on the night backdrop, at about a poster's cell size; `extra(draw)` adds more shapes."""
    Image = pytest.importorskip("PIL.Image")
    from PIL import ImageDraw
    im = Image.new("RGB", (860, 556), (12, 10, 20))
    d = ImageDraw.Draw(im)
    cw, ch = cell
    for r, line in enumerate(pose):
        for c, glyph in enumerate(line):
            for k, bit in enumerate(svgterm.QUAD.get(glyph, "0000")):     # quarters: UL, UR, LL, LR
                if bit == "1":
                    x, y = 40 + c * cw + (k % 2) * (cw // 2), 60 + r * ch + (k // 2) * (ch // 2)
                    d.rectangle([x, y, x + cw // 2 - 1, y + ch // 2 - 1], fill=colour)
    if extra:
        extra(d)
    im.save(path)
    return path


@pytest.mark.parametrize("colour", MASCOT_BODY, ids=["default", "pulsar-theme"])
@pytest.mark.parametrize("pose", MASCOT_POSES[:4], ids=["pose%d" % i for i in range(4)])
def test_the_png_check_finds_a_drawn_mascot(tmp_path, pose, colour):
    """Positive control for the PNG check, in both body colours: Pulsar's theme paints the mascot purple."""
    path = _draw_pose(tmp_path / "mascot.png", pose, colour)
    assert _mascot_problems(path), "the PNG check misses a mascot drawn in %s" % (colour,)


def test_the_png_check_ignores_pulsar_art_in_the_same_purple(tmp_path):
    """Negative control: chart bars, a block letter with a counter (A), one with two stacked counters
    (B) and a framed box with a divider, all in the theme's purple, are not the mascot."""
    purple = MASCOT_BODY[1]

    def art(d):
        for k in range(6):
            d.rectangle([300 + 40 * k, 300, 330 + 40 * k, 480], fill=purple)
        for x0, rows in ((600, ("01110", "10001", "10001", "11111", "10001", "10001", "10001")),
                         (700, ("11110", "10001", "10001", "11110", "10001", "10001", "11110"))):
            for r, row in enumerate(rows):
                for c, bit in enumerate(row):
                    if bit == "1":
                        d.rectangle([x0 + c * 15, 100 + r * 15, x0 + c * 15 + 14, 100 + r * 15 + 14], fill=purple)
        d.rectangle([60, 300, 260, 400], outline=purple, width=3)
        d.line([160, 300, 160, 400], fill=purple, width=3)

    path = _draw_pose(tmp_path / "art.png", [], purple, extra=art)
    assert not _mascot_problems(path)


def test_session_scene_skips_the_banner():
    scene = json.loads((DEMO / "scenes" / "session.json").read_text())
    assert scene["crop"]["top"] >= 4 and scene["crop"]["must_contain"] == "Claude Code v"
    doc = json.loads((ROOT / "docs" / "demo" / "session.json").read_text(encoding="utf-8"))
    first = svgterm.frame_text(doc["frames"][0]["rows"])
    assert "Pulsar Code" in first and "SessionStart" in first      # opens on Pulsar's own lines


def test_record_scene_fails_a_frame_with_the_mascot():
    sys.path.insert(0, str(DEMO / "scenes" / "fixtures"))
    import record_scene
    clean = (0.0, [[(0, "Pulsar Code 3.0.0", "#d7d6e6", False)]], None)
    assert not record_scene.mascot_problems([clean])
    bad = (1.5, _rows(MASCOT_POSES[0]), None)
    problems = record_scene.mascot_problems([clean, bad])
    assert problems and "mascot" in problems[0] and "1.50s" in problems[0]
