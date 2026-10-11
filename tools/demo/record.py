#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Record the REAL Pulsar Code TUI into README-ready animations.

    python tools/demo/record.py tools/demo/scenes/boot.json /tmp/try/boot [--gif]

drives the command in a pseudo-terminal, emulates the screen with pyte,
samples frames (screen rows plus pyte's cursor), and writes:

    <out>.svg     animated SVG (svgterm.py: row symbols toggled with steps(1,end); renders in GitHub READMEs)
    <out>.json    frames for the web player in docs/ (schema: cols, rows, frames[{t, rows}], plus the
                  optional keys title, alt, demo, cursor, camera, captions, enters)
    GIF           only with --gif, and never into the repository: $PULSAR_LAUNCH_DIR/demo/<name>.gif
                  (default /tmp/pulsar-launch/demo/), for release media

Scene file:
    {"cols": 100, "rows": 30, "cmd": ["./pulsarcode", "demo"], "env": {...},
     "keys": [[1.2, "\\u001b[B"], ...], "type": [[4.0, "what does this do?\\r"]],
     "duration": 6.0, "fps": 12, "start": 0.0, "title": "pulsarcode demo", "hold_last": 1.5,
     "cut": 12.0, "max_frames": 60}

"type" is expanded by expand_typing() into keys with a seeded, humanised rhythm (seed: the scene's
env.PULSAR_SEED, else 1). Record through make_all.sh (scenes/fixtures/record_scene.py), which also
writes the expanded keys back into the scene file as "typed_keys" so recordings stay reviewable.

Dev-only tool: needs `pip install pyte pillow`. Not shipped to users.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import select
import struct
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import svgterm  # noqa: E402

DEFAULT_BG = (10, 9, 16)
DEFAULT_FG = (215, 214, 230)
NAMED = {"black": (0, 0, 0), "red": (205, 49, 49), "green": (13, 188, 121), "yellow": (229, 229, 16),
         "blue": (36, 114, 200), "magenta": (188, 63, 188), "cyan": (17, 168, 205), "white": (229, 229, 229),
         "brightblack": (102, 102, 102), "brightred": (241, 76, 76), "brightgreen": (35, 209, 139),
         "brightyellow": (245, 245, 67), "brightblue": (59, 142, 234), "brightmagenta": (214, 112, 214),
         "brightcyan": (41, 184, 219), "brightwhite": (255, 255, 255)}

Run = Tuple[int, str, str, bool]          # x, text, hex color, bold
Frame = List[List[Run]]                   # rows of runs
Cursor = Optional[Tuple[int, int]]        # (col, row) or None when no cursor shows
Shot = Tuple[float, Frame, Cursor]        # t, rows, cursor

FRAME_QUANTUM = 1.0 / 30                  # timestamps snap to 33.3 ms (svgterm keyframes stay short)

# ---------------------------------------------------------------------------
# humanised typing
# ---------------------------------------------------------------------------

BIGRAMS = ("th", "he", "in", "er", "an", "re", "on")
PUNCT = set(".,;:!?)]}\"'")


def expand_typing(entries: Sequence[Sequence], seed: int = 1) -> List[List]:
    """[[t0, "text"], ...] -> [[t, key], ...] with a seeded human rhythm.

    Base interval 55 ms x lognormal(sigma .35), clamped to 30-160 ms; the second key of a common
    bigram (th he in er an re on) follows at 38 ms; +90 ms after a space; +140 ms before
    punctuation; the first key lands 250 ms after t0; Enter ("\\r") follows a 420 ms think.
    """
    rng = random.Random(seed)
    keys: List[List] = []
    for t0, text in entries:
        t = float(t0) + 0.25
        prev = ""
        for i, ch in enumerate(text):
            if i:
                if ch in "\r\n":
                    dt = 0.42
                else:
                    if (prev + ch).lower() in BIGRAMS:
                        dt = 0.038
                    else:
                        dt = min(0.16, max(0.03, 0.055 * rng.lognormvariate(0.0, 0.35)))
                    if prev == " ":
                        dt += 0.09
                    if ch in PUNCT:
                        dt += 0.14
                t += dt
            keys.append([round(t, 3), ch])
            prev = ch
    return keys


def typing_intervals(keys: Sequence[Sequence]) -> Tuple[List[float], List[float]]:
    """(intervals between typed characters, gaps before each Enter) of an expanded key list."""
    inter, enter = [], []
    for (a, _ka), (b, kb) in zip(keys, keys[1:]):
        (enter if kb in ("\r", "\n") else inter).append(b - a)
    return inter, enter


def coefficient_of_variation(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))
    return sd / m if m else 0.0


def scene_seed(scene: Dict) -> int:
    try:
        return int((scene.get("env") or {}).get("PULSAR_SEED", 1))
    except (TypeError, ValueError):
        return 1


def scene_keys(scene: Dict) -> List[List]:
    """Every key the scene sends: its manual "keys" plus its expanded "type" entries, by time."""
    typed = expand_typing(scene.get("type") or [], scene_seed(scene))
    return sorted([[float(t), s] for t, s in list(scene.get("keys") or []) + typed], key=lambda k: k[0])


# ---------------------------------------------------------------------------
# screen capture
# ---------------------------------------------------------------------------

def _hex(c, default) -> str:
    if c in (None, "default"):
        rgb = default
    elif c in NAMED:
        rgb = NAMED[c]
    else:
        try:
            rgb = tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
        except (ValueError, TypeError):
            rgb = default
    return "#%02x%02x%02x" % tuple(rgb)


def snapshot(screen) -> Frame:
    rows: Frame = []
    for y in range(screen.lines):
        line = screen.buffer[y]
        runs: List[Run] = []
        cur_text, cur_x, cur_style = "", 0, None
        for x in range(screen.columns):
            ch = line[x]
            data = ch.data or " "
            style = (_hex(ch.fg, DEFAULT_FG), bool(ch.bold))
            if data == " ":
                if cur_text:
                    runs.append((cur_x, cur_text, cur_style[0], cur_style[1]))
                    cur_text, cur_style = "", None
                continue
            if cur_style == style and cur_text:
                cur_text += data
            else:
                if cur_text:
                    runs.append((cur_x, cur_text, cur_style[0], cur_style[1]))
                cur_text, cur_x, cur_style = data, x, style
        if cur_text:
            runs.append((cur_x, cur_text, cur_style[0], cur_style[1]))
        rows.append(runs)
    return rows


def cursor_of(screen) -> Cursor:
    """Where a viewer sees the cursor: pyte's terminal cursor when the program shows it; otherwise the
    one-cell reverse-video block a full-screen program draws itself (Claude Code's prompt does this),
    when there is exactly one; otherwise None."""
    c = screen.cursor
    if not c.hidden and 0 <= c.x < screen.columns and 0 <= c.y < screen.lines:
        return (int(c.x), int(c.y))
    found = None
    for y in range(screen.lines):
        line = screen.buffer[y]
        x = 0
        while x < screen.columns:
            if line[x].reverse:
                j = x
                while j < screen.columns and line[j].reverse:
                    j += 1
                if j - x == 1:
                    if found is not None:
                        return None
                    found = (x, y)
                x = j
            else:
                x += 1
    return found


def record(scene: Dict) -> Tuple[List[Shot], List[Tuple[float, str]]]:
    """Run the scene's command in a pty. Returns (shots, keys sent as (t, key))."""
    cols, rows = int(scene.get("cols", 100)), int(scene.get("rows", 30))
    env = dict(os.environ)
    env.update({"TERM": "xterm-256color", "COLORTERM": "truecolor", "LANG": "C.UTF-8",
                "COLUMNS": str(cols), "LINES": str(rows)})
    env.update({k: str(v) for k, v in (scene.get("env") or {}).items()})
    import pyte                                 # dev-only dependency, needed to record (not to render)
    import fcntl                                # POSIX only: recording drives a pty (rendering does not)
    import pty
    import termios
    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe(scene["cmd"][0], scene["cmd"], env)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    screen = pyte.Screen(cols, rows)
    stream = pyte.ByteStream(screen)
    fps = float(scene.get("fps", 12))
    duration = float(scene.get("duration", 6))
    start = float(scene.get("start", 0))
    keys = scene_keys(scene)
    shots: List[Shot] = []
    sent: List[Tuple[float, str]] = []
    t0 = time.time()
    next_shot = start
    while True:
        now = time.time() - t0
        if now > duration:
            break
        r, _, _ = select.select([fd], [], [], 0.005)
        if r:
            try:
                data = os.read(fd, 65536)
            except OSError:
                data = b""
            if not data:
                break
            stream.feed(data)
        while keys and now >= keys[0][0]:
            k = keys.pop(0)[1]
            os.write(fd, k.encode())
            sent.append((now - start, k))
        if now >= next_shot:
            shots.append((now - start, snapshot(screen), cursor_of(screen)))
            next_shot += 1.0 / fps
    shots.append((time.time() - t0 - start, snapshot(screen), cursor_of(screen)))
    try:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
    except OSError:
        pass
    return shots, sent


def dedupe(frames: List[Shot]) -> List[Shot]:
    out: List[Shot] = []
    for f in frames:
        if out and tuple(out[-1][1:]) == tuple(f[1:]):
            continue
        out.append(f)
    return out


def quantise(frames: List[Shot], quantum: float = FRAME_QUANTUM) -> List[Shot]:
    """Snap timestamps to the quantum; of frames that land on the same tick, the later one wins."""
    out: List[Shot] = []
    for f in frames:
        t = round(round(f[0] / quantum) * quantum, 3)
        g = (t,) + tuple(f[1:])
        if out and out[-1][0] >= t:
            out[-1] = (out[-1][0],) + tuple(f[1:])
        else:
            out.append(g)
    return dedupe(out)


def row_key(runs: List[Run]) -> str:
    return hashlib.sha1(json.dumps(runs).encode()).hexdigest()[:10]


# ---------------------------------------------------------------------------
# outputs
# ---------------------------------------------------------------------------

def to_svg(frames: List[Shot], scene: Dict, **kw) -> str:
    """Geometry-based SVG with brand chrome, camera, cursor and captions (see svgterm.py)."""
    return svgterm.to_svg(frames, scene, **kw)


def launch_dir() -> Path:
    return Path(os.environ.get("PULSAR_LAUNCH_DIR") or "/tmp/pulsar-launch") / "demo"


def to_gif(frames: List[Shot], cols: int, rows: int, path: Path, hold_last: float = 1.5,
           scale: float = 1.0) -> None:
    from PIL import Image, ImageDraw, ImageFont
    font_dir = "/usr/share/fonts/truetype/dejavu/"
    reg = ImageFont.truetype(font_dir + "DejaVuSansMono.ttf", int(14 * scale))
    bold = ImageFont.truetype(font_dir + "DejaVuSansMono-Bold.ttf", int(14 * scale))
    cw, ch = int(8.5 * scale), int(18 * scale)
    pad = int(14 * scale)
    images, durations = [], []
    for i, fr in enumerate(frames):
        t, f = fr[0], fr[1]
        img = Image.new("RGB", (cols * cw + 2 * pad, rows * ch + 2 * pad), DEFAULT_BG)
        d = ImageDraw.Draw(img)
        for y, runs in enumerate(f):
            for x, text, color, b in runs:
                d.text((pad + x * cw, pad + y * ch), text, font=bold if b else reg, fill=color)
        images.append(img.convert("P", palette=Image.ADAPTIVE, colors=128))
        nxt = frames[i + 1][0] if i + 1 < len(frames) else t + hold_last
        durations.append(max(20, int((nxt - t) * 1000)))
    path.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(path, save_all=True, append_images=images[1:], duration=durations, loop=0, optimize=True,
                   disposal=1)


def to_json(frames: List[Shot], cols: int, rows: int, meta: Optional[Dict] = None) -> str:
    """The site player's format. Unchanged core (cols, rows, frames[{t, rows}]); new optional keys:
    cursor (one [col, row] or null per frame), and from meta: title, alt, demo, camera, captions,
    enters, hold_last."""
    doc: Dict = {"cols": cols, "rows": rows}
    for k, v in (meta or {}).items():
        if v not in (None, [], ""):
            doc[k] = v
    doc["frames"] = [{"t": round(f[0], 3), "rows": f[1]} for f in frames]
    if any(len(f) > 2 and f[2] is not None for f in frames):
        doc["cursor"] = [list(f[2]) if len(f) > 2 and f[2] is not None else None for f in frames]
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False)


def main(argv: List[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if len(args) < 2:
        print(__doc__)
        return 2
    scene = json.loads(Path(args[0]).read_text())
    out = Path(args[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    frames, sent = record(scene)
    frames = quantise(dedupe(frames))
    if scene.get("cut"):                                   # end on the best frame, not the post-exit screen
        frames = [f for f in frames if f[0] <= float(scene["cut"])]
    frames = svgterm.limit_frames(frames, int(scene.get("max_frames", 60)))
    cols, rows = int(scene.get("cols", 100)), int(scene.get("rows", 30))
    meta = dict(scene)
    meta["enters"] = [t for t, k in sent if k in ("\r", "\n")]
    svg = to_svg(frames, meta)
    out.with_suffix(".svg").write_text(svg)
    out.with_suffix(".json").write_text(to_json(frames, cols, rows, {"title": scene.get("title")}))
    if "--gif" in argv:
        to_gif(frames, cols, rows, launch_dir() / (out.name + ".gif"), float(scene.get("hold_last", 1.5)))
    st = svgterm.stats(svg)
    print("%d frames  svg %.0f KB  (gzip %.0f KB, %d elements)" % (
        len(frames), st["bytes"] / 1024, st["gzip"] / 1024, st["elements"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
