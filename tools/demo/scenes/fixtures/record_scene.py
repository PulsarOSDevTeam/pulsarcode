#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Record one scene with tools/demo/record.py, trimmed on what is on screen and fitted to a size budget.

    record_scene.py SCENE.json OUT_PREFIX [--json-out FILE] [--raw-out FILE] [--from-raw FILE] [--gif]

writes OUT_PREFIX.svg (the README replay) and the site player's frames (--json-out, default
OUT_PREFIX.json). --raw-out keeps the untrimmed recording; --from-raw re-renders one without running
the scene again (for iterating on direction). --gif writes a GIF for release media to
$PULSAR_LAUNCH_DIR/demo/<name>.gif (default /tmp/pulsar-launch/demo/), never into the repository.

Scene files use record.py's format. This wrapper reads a few optional keys on top, so a recording
does not depend on how fast a machine boots Python:

    "start_at":        "TEXT"   drop the frames before the first one that shows TEXT
    "keep_until_last": "TEXT"   end on the last frame that shows TEXT (instead of a fixed "cut")
    "cut_before":      "TEXT"   end just before the first frame (after the start) that shows TEXT
    "end_after":   ["TEXT", S]  end at most S seconds after the first frame that shows TEXT (a screen
                               that keeps ticking, like a live status dot, otherwise runs to the end)
    "alt":             "TEXT"   the SVG's accessible name (aria-label and <title>); "title" stays
                               the short window caption drawn in the chrome
    "demo":            true     a DEMO chip in the chrome (the scene shows a demo upstream or fixtures)
    "glow":            true     geometry glow under bright block glyphs (the boot wordmark and beams)
    "type": [[AT, "text\\r"], ...]
                               typed with a seeded human rhythm (record.expand_typing); the expanded
                               keys are written back into the scene file as "typed_keys"
    "camera": [[T, ZOOM, COL, ROW, "GLIDE"|"SETTLE"], ...]
                               keyframes of the replay camera; the ease names the move INTO a key
    "captions": [[T0, T1, "caption", "must_show", {"poster": true}], ...]
                               lower-third captions; the build fails unless must_show is on screen
                               for the whole time the caption shows
    "crop": {"top": N, "must_contain": "TEXT"}
                               drop the top N rows of every frame; fails unless the cropped rows
                               hold TEXT in every frame and no error text
    "lead": SECONDS            open on the last frame, dip, then play (svgterm.to_svg)
    "colour_step": N           colour precision of this replay (default svgterm.COLOUR_STEP, 8):
                               16 merges neighbouring cells of a gradient into fewer paths (boot)
    "budget": {"svg_kb": 650, "gzip_kb": 120, "elements": 16000}
                               lower max_frames until the SVG fits; fail when 8 frames still do not
    "sync": [[AT, "TEXT"], ...]
                               the "keys" timings assume TEXT shows up AT seconds after start;
                               keys from AT on (until the next anchor) are sent relative to the
                               moment TEXT really appears, so typing never races a slow start.
    "spawn": [["TEXT", DELAY, "shell command"], ...]
                               run the command (sh -c, the scene's environment, output discarded)
                               DELAY seconds after TEXT first shows: an event the scene waits for
                               (a deck posted to Stage) lands on what is on screen, not on a timer
    "clock": "09:41"           the pinned wall clock (make_all.sh pins it for every scene). Any time
                               of day on a kept frame (9:41, 09:41:07) must fall within
                               CLOCK_WINDOW minutes of it, so no replay shows when it was recorded.

Times in "camera" and "captions" are seconds of the finished replay, or anchors resolved on what was
recorded: "enter" (the first Enter; "enter#2" the third), "first:TEXT", "last:TEXT", "start", "end"
(the last frame), each optionally as [ANCHOR, OFFSET]. COL may be "at:TEXT" (ROW is then a row
offset): the camera centres on TEXT where it is on screen at that time. Camera and captions are
resolved on the frames the replay keeps, and the frames they are anchored on are always kept.

Fails (exit 1) when a scene's anchor text never appeared, a caption's must_show text is not on
screen, the camera would hide an error, or the budget cannot be met: a recording of the wrong screen
must not replace a good asset. A kept frame that shows Claude Code's mascot (svgterm.CLI_MASCOT)
fails too: no asset carries Anthropic's mark.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DEMO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(DEMO))

import record  # noqa: E402
import svgterm  # noqa: E402

DEFAULT_BUDGET = {"svg_kb": 650, "gzip_kb": 120, "elements": 16000}
MAX_FRAMES = {"stage": 48, "top": 48}
HEADROOM = 0.95                    # fit SVG bytes into 95 % of the budget: a rerun never lands on the edge
PINNED_CLOCK = "09:41"             # make_all.sh pins every scene's wall clock here (scenes/fixtures/pinclock.c)
CLOCK_WINDOW = 3                   # minutes: a scene's clock reads PINNED_CLOCK plus at most this
TIME_OF_DAY = re.compile(r"(?<![\d.:])([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?(?![\d:])")


def clock_problems(frames, pinned: str = PINNED_CLOCK, window: int = CLOCK_WINDOW):
    """Times of day on the frames that are not the pinned clock: a replay must never carry the time it
    was recorded at (and two runs must show the same clock)."""
    h0, m0 = (int(v) for v in pinned.split(":"))
    lo = h0 * 60 + m0
    bad = []
    for f in frames:
        for m in TIME_OF_DAY.finditer(screen_text(f[1])):
            h, mi = int(m.group(1)), int(m.group(2))
            mins = [h * 60 + mi] + ([(h + 12) * 60 + mi] if h < 12 else [])      # 9:41 PM reads as 21:41
            if not any(lo <= v < lo + window for v in mins) and m.group(0) not in bad:
                bad.append(m.group(0))
    return ["time of day %s on screen (the clock is pinned to %s): record through make_all.sh"
            % (", ".join(bad[:5]), pinned)] if bad else []


def mascot_problems(frames):
    """Kept frames that still show Claude Code's mascot (svgterm.CLI_MASCOT): crop the banner away."""
    for f in frames:
        hit = svgterm.mascot_lines(screen_text(f[1]))
        if hit:
            return ["frame at %.2fs shows Claude Code's mascot (%r): crop or skip the welcome banner"
                    % (f[0], hit[0].strip())]
    return []


def live_text(screen) -> str:
    return "\n".join(screen.display)


def record_synced(scene):
    """record.record with "sync" anchors (see the module docstring). Returns (shots, sent, problems)."""
    import fcntl
    import math
    import os
    import pty
    import select
    import struct
    import termios
    import time

    import pyte

    cols, rows = int(scene.get("cols", 100)), int(scene.get("rows", 30))
    env = dict(os.environ)
    env.update({"TERM": "xterm-256color", "COLORTERM": "truecolor", "LANG": "C.UTF-8",
                "COLUMNS": str(cols), "LINES": str(rows)})
    env.update({k: str(v) for k, v in (scene.get("env") or {}).items()})
    anchors = sorted([float(a), str(t)] for a, t in scene.get("sync") or [])
    spawns = [[str(t), float(d), str(c), None] for t, d, c in scene.get("spawn") or []]   # text, delay, cmd, due
    children = []
    keys = record.scene_keys(scene)
    seen = {}                                    # anchor index -> real time it appeared
    timeout = float(scene.get("sync_timeout", 30))
    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe(scene["cmd"][0], scene["cmd"], env)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    screen = pyte.Screen(cols, rows)
    stream = pyte.ByteStream(screen)
    fps = float(scene.get("fps", 12))
    duration = float(scene.get("duration", 6))
    start = float(scene.get("start", 0))
    frames, sent, problems = [], [], []
    t0 = time.time()
    next_shot = start

    def shift() -> float:
        done = [seen[i] - anchors[i][0] for i in sorted(seen)]
        return done[-1] if done else 0.0

    while True:
        now = time.time() - t0
        if now > duration + max(0.0, shift()):
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
        text = None
        for i, (at, want) in enumerate(anchors):
            if i in seen:
                continue
            if i and (i - 1) not in seen:
                break                            # anchors resolve in order
            text = live_text(screen) if text is None else text
            if want in text:
                seen[i] = now
            elif now > at + timeout:
                problems.append("sync text never appeared: %r" % want)
                anchors = anchors[:i]
                keys = [k for k in keys if k[0] < at]
            break
        for sp in spawns:
            if sp[3] is None:
                text = live_text(screen) if text is None else text
                if sp[0] in text:
                    sp[3] = now + sp[1]
            elif sp[3] is not False and now >= sp[3]:
                sp[3] = False
                import subprocess
                children.append(subprocess.Popen(["sh", "-c", sp[2]], env=env, stdin=subprocess.DEVNULL,
                                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        while keys:
            t, s = keys[0]
            gov = max([i for i, (at, _w) in enumerate(anchors) if at <= t], default=-1)
            if gov >= 0 and gov not in seen:
                break
            due = t if gov < 0 else seen[gov] + (t - anchors[gov][0])
            if now < due:
                break
            os.write(fd, keys.pop(0)[1].encode())
            sent.append((round(now - start, 3), s))
        if now >= next_shot:
            # stamped with the sample's scheduled time on the fps grid, not the poll's wall clock, so
            # a rerun's frames land on the same timestamps; a late poll skips to the current slot
            frames.append((round(next_shot - start, 4), record.snapshot(screen), record.cursor_of(screen)))
            next_shot += 1.0 / fps
            if next_shot <= now:
                next_shot = start + math.floor((now - start) * fps + 1) / fps
    frames.append((round(next_shot - start, 4), record.snapshot(screen), record.cursor_of(screen)))
    try:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
    except OSError:
        pass
    for ch in children:
        try:
            ch.kill()
            ch.wait(timeout=5)
        except Exception:                        # noqa: BLE001 - best effort; make_all.sh stops the rest
            pass
    for sp in spawns:
        if sp[3] is not False:
            problems.append("spawn text never appeared: %r" % sp[0])
    return frames, sent, problems


def screen_text(frame) -> str:
    """The text of a recorded frame, runs placed at their columns (so spaces between them survive)."""
    return svgterm.frame_text(frame)


def first(frames, text: str, after: int = 0) -> int:
    for i in range(after, len(frames)):
        if text in screen_text(frames[i][1]):
            return i
    return -1


def last(frames, text: str) -> int:
    for i in range(len(frames) - 1, -1, -1):
        if text in screen_text(frames[i][1]):
            return i
    return -1


def trim(frames, scene):
    """(frames, problems, t0) after the content anchors; times are rebased to the first kept frame (t0)."""
    problems = []
    start = 0
    if scene.get("start_at"):
        start = first(frames, scene["start_at"])
        if start < 0:
            problems.append("start_at text never appeared: %r" % scene["start_at"])
            start = 0
    end = len(frames) - 1
    if scene.get("keep_until_last"):
        i = last(frames, scene["keep_until_last"])
        if i < start:
            problems.append("keep_until_last text never appeared: %r" % scene["keep_until_last"])
        else:
            end = i
    if scene.get("cut_before"):
        i = first(frames, scene["cut_before"], start + 1)
        if i > start:
            end = min(end, i - 1)
    if scene.get("end_after"):
        text, secs = scene["end_after"][0], float(scene["end_after"][1])
        i = first(frames, text, start)
        if i < 0:
            problems.append("end_after text never appeared: %r" % text)
        else:
            while end > i and frames[end][0] > frames[i][0] + secs:
                end -= 1
    if scene.get("cut"):
        cut_t = frames[start][0] + float(scene["cut"])
        while end > start and frames[end][0] > cut_t:
            end -= 1
    # never end on an empty screen (a program clearing it on exit): the last frame is the one held
    # in the loop and the only one shown under prefers-reduced-motion in the SVG
    while end > start and not screen_text(frames[end][1]).strip():
        end -= 1
    if end >= start and not screen_text(frames[end][1]).strip():
        problems.append("every kept frame is blank")
    kept = frames[start:end + 1]
    t0 = kept[0][0] if kept else 0.0
    return [(round(t - t0, 3),) + tuple(rest) for t, *rest in kept], problems, t0


# ---------------------------------------------------------------------------
# direction: anchors -> numbers
# ---------------------------------------------------------------------------

def crop(frames, scene):
    """(frames, rows, problems): drop the top rows the scene's "crop" names. The crop is a framing
    choice, never an edit: every frame's cropped rows must hold crop.must_contain (so it removes what it
    was meant to and nothing that moved in) and no error text."""
    spec = scene.get("crop") or {}
    top = int(spec.get("top", 0))
    rows = int(scene.get("rows", 30))
    if not top:
        return frames, rows, []
    want = spec.get("must_contain", "")
    problems = []
    out = []
    for f in frames:
        gone = screen_text(f[1][:top])
        if want and want not in gone:
            problems.append("crop at %.2fs: the cropped rows do not hold %r" % (f[0], want))
            break
        if svgterm.ERROR_WORDS.search(gone):
            problems.append("crop at %.2fs would hide error text" % f[0])
            break
        cur = f[2] if len(f) > 2 else None
        if cur is not None:
            cur = (cur[0], cur[1] - top) if cur[1] >= top else None
        out.append((f[0], f[1][top:], cur))
    return out, rows - top, problems


def anchor_indexes(scene, frames):
    """Indexes of the frames the scene's camera and captions are anchored on (first:/last: TEXT), and
    of the first frame showing each caption's must_show text: these frames must survive limit_frames."""
    specs = [k[0] for k in scene.get("camera") or []]
    for c in scene.get("captions") or []:
        specs += [c[0], c[1], ["first:" + c[3], 0]]
    out = set()
    for spec in specs:
        s = spec[0] if isinstance(spec, (list, tuple)) else spec
        if isinstance(s, str) and (s.startswith("first:") or s.startswith("last:")):
            kind, text = s.split(":", 1)
            i = first(frames, text) if kind == "first" else last(frames, text)
            if i >= 0:
                out.add(i)
    return sorted(out)


def resolve_time(spec, frames, enters) -> float:
    off = 0.0
    if isinstance(spec, (list, tuple)):
        spec, off = spec[0], float(spec[1])
    if isinstance(spec, (int, float)):
        return float(spec) + off
    s = str(spec)
    if s == "start":
        base = 0.0
    elif s == "end":
        base = frames[-1][0]
    elif s.startswith("enter"):
        idx = int(s.split("#", 1)[1]) if "#" in s else 0
        if idx >= len(enters):
            raise ValueError("anchor %r: only %d Enter key(s) were sent" % (s, len(enters)))
        base = enters[idx]
    elif s.startswith("first:") or s.startswith("last:"):
        kind, text = s.split(":", 1)
        i = first(frames, text) if kind == "first" else last(frames, text)
        if i < 0:
            raise ValueError("anchor %r: text never on screen" % s)
        base = frames[i][0]
    else:
        raise ValueError("unknown time anchor %r" % s)
    return round(max(0.0, base + off), 3)


def frame_at(frames, t: float):
    cur = frames[0]
    for f in frames:
        if f[0] <= t + 1e-6:
            cur = f
    return cur


def resolve_focus(col, row, t: float, frames):
    """(col, row) numbers; "at:TEXT" centres on TEXT in the frame shown at t (or the next showing it)."""
    if isinstance(col, str) and col.startswith("at:"):
        text = col[3:]
        cands = [frame_at(frames, t)] + [f for f in frames if f[0] > t]
        for f in cands:
            for y, line in enumerate(screen_text(f[1]).split("\n")):
                x = line.find(text)
                if x >= 0:
                    return round(x + len(text) / 2.0 - .5, 1), y + float(row or 0)
        raise ValueError("camera focus %r: text never on screen after %.2fs" % (col, t))
    return float(col), float(row)


def direction(scene, frames, enters):
    camera = []
    for k in scene.get("camera") or []:
        t = resolve_time(k[0], frames, enters)
        col, row = resolve_focus(k[2], k[3], max(0.0, t - 0.15), frames)   # the screen as the move begins
        camera.append([t, float(k[1]), col, row, str(k[4] if len(k) > 4 else "SETTLE").upper()])
    captions = []
    for c in scene.get("captions") or []:
        item = [resolve_time(c[0], frames, enters), resolve_time(c[1], frames, enters), c[2], c[3]]
        if len(c) > 4:
            item.append(c[4])
        captions.append(item)
    return camera, captions


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

def fit(frames, scene, meta, name: str, enters):
    """(picked, svg, stats, problems, camera, captions): fewer frames until the SVG fits the budget.
    Camera and captions are resolved on the frames actually kept, so an anchor ("first:Read") always
    lands on a frame the replay shows, and must_show is validated against exactly those frames."""
    budget = dict(DEFAULT_BUDGET)
    budget.update(scene.get("budget") or {})
    n_max = min(int(scene.get("max_frames", 60)), MAX_FRAMES.get(name, 60), len(frames))
    keep = anchor_indexes(scene, frames)

    def build(n):
        picked = svgterm.limit_frames(frames, n, keep)
        camera, captions = direction(scene, picked, enters)
        m = dict(meta, camera=camera, captions=captions)
        svg = record.to_svg(picked, m)
        st = svgterm.stats(svg)
        over = [what for what, val, cap in (("svg", st["bytes"], budget["svg_kb"] * 1024 * HEADROOM),
                                            ("gzip", st["gzip"], budget["gzip_kb"] * 1024),
                                            ("elements", st["elements"], budget["elements"])) if val > cap]
        return picked, svg, st, over, camera, captions

    best = build(n_max)                       # a direction problem here is real: raise it
    if not best[3]:
        return best
    lo, hi = min(8, n_max), n_max - 1           # largest frame count that fits
    found = None
    while lo <= hi:
        mid = (lo + hi) // 2
        try:
            cand = build(mid)
        except ValueError:
            hi = mid - 1
            continue
        if cand[3]:
            hi = mid - 1
        else:
            found, lo = cand, mid + 1
    if found:
        return found
    picked, svg, st, over, camera, captions = build(min(8, n_max))
    return picked, svg, st, ["over budget at %d frames: %s (svg %.0f KB, gzip %.0f KB, %d elements)"
                             % (len(picked), ", ".join(over), st["bytes"] / 1024, st["gzip"] / 1024,
                                st["elements"])], camera, captions


def dump_scene(scene) -> str:
    """JSON with every list or object that fits on one line kept on one line (reviewable scene files)."""
    def fmt(o, ind: int) -> str:
        flat = json.dumps(o, ensure_ascii=True, separators=(", ", ": "))
        if not isinstance(o, (list, dict)) or (ind and len(flat) + ind <= 110):
            return flat
        pad = " " * (ind + 2)
        if isinstance(o, dict):
            items = ["%s%s: %s" % (pad, json.dumps(k), fmt(v, ind + 2)) for k, v in o.items()]
            return "{\n%s\n%s}" % (",\n".join(items), " " * ind)
        return "[\n%s\n%s]" % (",\n".join(pad + fmt(v, ind + 2) for v in o), " " * ind)
    return fmt(scene, 0) + "\n"


def write_back_typing(path: Path, scene) -> None:
    if not scene.get("type"):
        return
    typed = record.expand_typing(scene["type"], record.scene_seed(scene))
    if scene.get("typed_keys") != typed:
        scene["typed_keys"] = typed
        path.write_text(dump_scene(scene))


def main(argv) -> int:
    opts = {}
    args = []
    it = iter(argv)
    for a in it:
        if a in ("--json-out", "--raw-out", "--from-raw"):
            opts[a] = next(it, "")
        elif a == "--gif":
            opts[a] = True
        else:
            args.append(a)
    if len(args) != 2:
        print(__doc__)
        return 2
    scene_path = Path(args[0])
    scene = json.loads(scene_path.read_text())
    name = scene_path.stem
    out = Path(args[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    write_back_typing(scene_path, scene)
    if opts.get("--from-raw"):
        raw = json.loads(Path(opts["--from-raw"]).read_text())
        frames = [tuple(f[:2]) + ((tuple(f[2]) if f[2] is not None else None),) for f in raw["frames"]]
        sent, sync_problems = [tuple(s) for s in raw["sent"]], raw.get("problems", [])
    else:
        frames, sent, sync_problems = record_synced(scene)
        if opts.get("--raw-out"):
            Path(opts["--raw-out"]).parent.mkdir(parents=True, exist_ok=True)
            Path(opts["--raw-out"]).write_text(json.dumps(
                {"frames": [[t, f, c] for t, f, c in frames], "sent": sent, "problems": sync_problems},
                separators=(",", ":")))
    frames = record.dedupe(frames)
    frames, problems, t0 = trim(frames, scene)
    problems = list(sync_problems) + problems
    frames = record.quantise(frames)
    enters = [round(t - t0, 3) for t, k in sent if k in ("\r", "\n") and t - t0 >= 0]
    rows = int(scene.get("rows", 30))
    if not problems and frames:
        frames, rows, crop_problems = crop(frames, scene)
        problems += crop_problems
    if not problems and frames:
        problems += mascot_problems(frames)
    if not problems and frames and scene.get("clock", True):
        problems += clock_problems(frames, str(scene.get("clock") or PINNED_CLOCK))
    if problems or not frames:
        return fail(argv[0], out, frames, problems)
    cols = int(scene.get("cols", 100))
    meta = {"title": scene.get("title", "pulsarcode"), "alt": scene.get("alt", ""), "cols": cols, "rows": rows,
            "hold_last": float(scene.get("hold_last", 1.5)), "lead": float(scene.get("lead", 0)),
            "demo": bool(scene.get("demo")), "glow": bool(scene.get("glow")), "enters": enters,
            "colour_step": int(scene.get("colour_step") or 0)}
    try:
        picked, svg, st, over, camera, captions = fit(frames, scene, meta, name, enters)
    except ValueError as e:
        return fail(argv[0], out, frames, [str(e)])
    if over:
        return fail(argv[0], out, frames, over)
    out.with_suffix(".svg").write_text(svg)
    json_out = Path(opts.get("--json-out") or out.with_suffix(".json"))
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(record.to_json(picked, cols, rows, {
        "title": meta["title"], "alt": meta["alt"], "demo": meta["demo"] or None, "hold_last": meta["hold_last"],
        "enters": enters, "camera": camera, "captions": captions}))
    if opts.get("--gif"):
        record.to_gif(picked, cols, rows, record.launch_dir() / (name + ".gif"), meta["hold_last"])
    print("%-8s %2d frames  %4.1fs  svg %4.0f KB  gzip %3.0f KB  %5d elements  json %4.0f KB" % (
        name, len(picked), picked[-1][0], st["bytes"] / 1024, st["gzip"] / 1024, st["elements"],
        json_out.stat().st_size / 1024))
    return 0


def fail(scene_arg, out, frames, problems) -> int:
    for p in problems or ["no frames recorded"]:
        print("%s: %s" % (Path(scene_arg).name, p), file=sys.stderr)
    last_screen = screen_text(frames[-1][1]) if frames else ""
    dump = out.with_suffix(".failed.txt")
    dump.write_text(last_screen + "\n")
    print("  last screen written to %s" % dump, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
