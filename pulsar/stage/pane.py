# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""The Stage pane inside Claude Code: Core's side and the launcher's side.

Pulsar Stage can draw as a live, animated pane inside Claude Code's own
terminal UI through Claude Code's early-access function-hooks plugin API
(2.1.287 and later). The plugin lives in `pulsar/fnplug` (name
`pulsar-stage`); it polls Core over loopback as the session and paints the
frames this module serves.

  raster_route(scenes, query) -> (status, body)   GET /pulsar/stage/raster, one line in Core
  plugin_dir() -> Path | None                     the plugin folder for `--plugin-dir`, or None when off
                                                  (a copy under PULSAR_HOME/cache/fnplug, see below)
  supported(claude_version) -> bool               does this Claude Code have the API
  PaneSeen                                        which sessions have a pane up (X-Pulsar-Viewer)

Query of /pulsar/stage/raster (all optional, all clamped):
  cols 1..512, rows 1..256   the raster size in cells (default 80 x 24)
  slide                      deck slide index (clamped to the deck)
  t                          seconds since the scene/slide appeared (default settled)
  id                         a scene id, or `latest` (default)
  ascii                      1 for ASCII-only cells
  meta                       1 for the cheap version check: no cells, only what changed
  idle                       1 to get the pane's idle screen (instead of 204) while the stage is empty

Answers: 204 with {} when the caller's session has no scene (or not that id);
otherwise 200 with `raster.frame(...)` plus `version` (when Core accepted
this state of the scene) and `count` (scenes on the stage). With meta=1 the
body has no `cells`. With idle=1 and an empty stage: 200, the idle screen,
`idle: true`.

Viewer header. The plugin sends `X-Pulsar-Viewer` on every request:
  pane          the pane is open and placed: it shows each new scene
  pane-auto     the pane is closed (or not placed) but an open of its own would be placed now:
                a fullscreen terminal at least 144 columns wide (110 once the person opened the
                pane this session), the person has not closed it, and its last unasked open at
                this width was placed. It then docks itself as a sidebar for the next scene.
  pane-standby  anything else: the plugin runs but only toasts a pointer at /stage-pane
Core notes it per session (`PaneSeen.note`) in this route and counts an active
pane as a viewer, so the MCP server opens no tmux or browser viewer while one
is showing.

Ids. Scene ids reach this route as Core stored them; one holding a lone
surrogate (schema.clean removes them now; an id an earlier build stored may hold one) would make Core's JSON answer fail on
every poll, so ids leave here well formed (U+FFFD in place of a lone
surrogate) and an `id=` query matches a scene by that same form.

The plugin folder. Claude Code writes its generated types
(`.claude-plugin/types/`, about 740 KB) into every `--plugin-dir` it
loads, so the launcher never hands it the package's own `pulsar/fnplug`
(a wheel built from a checkout would ship them, and site-packages may be
read-only): `plugin_dir()` keeps a copy of the shipped files (manifest,
hooks, types, tsconfig; not the plugin's tests) in
PULSAR_HOME/cache/fnplug, rewriting only what changed, and loads no pane
rather than the package folder when that copy cannot be made.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

MIN_VERSION = (2, 1, 287)
ENV_SWITCH = "PULSAR_STAGE_PANE"
VIEWER_HEADER = "X-Pulsar-Viewer"
PLUGIN_NAME = "pulsar-stage"

_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


# ---------------------------------------------------------------------------
# Launcher side
# ---------------------------------------------------------------------------

SHIPPED = (".claude-plugin/plugin.json", "tsconfig.json", "hooks/*.json", "hooks/*.ts", "hooks/*.tsx", "types/*.d.ts")


def package_plugin_dir() -> Optional[Path]:
    """The plugin as shipped inside the package (pulsar/fnplug), or None when it is missing."""
    root = Path(__file__).resolve().parent.parent / "fnplug"
    if not (root / ".claude-plugin" / "plugin.json").is_file() or not (root / "hooks" / "hooks.json").is_file():
        return None
    return root


def plugin_dir() -> Optional[Path]:
    """The function-hooks plugin folder for `claude --plugin-dir` when the pane is wanted, else None.

    Off with PULSAR_STAGE_PANE=off|0|false|no. The caller also checks `supported(claude --version)`.
    Always PULSAR_HOME/cache/fnplug, a copy of the shipped files that Claude Code may write its generated
    types into (the cache folder is made when the home has none yet). Never the package's own folder:
    Claude Code would write those types into the package (a checkout, a read-only site-packages). None,
    so no pane for this launch, when there is no Pulsar home yet or the copy cannot be made there.
    """
    switch = (os.environ.get(ENV_SWITCH) or "").strip().lower()
    if switch in ("off", "0", "false", "no"):
        return None
    src = package_plugin_dir()
    if src is None:
        return None
    try:
        from pulsar import config as K
        if not K.home().is_dir():
            return None                                # never create a home from here (the launcher does)
        cache = K.ensure_home() / "cache"
        if cache.is_symlink():
            return None                                # never follow a planted link out of the home
        cache.mkdir(mode=0o700, exist_ok=True)
        dst = _sync_copy(src, cache / "fnplug")
        return None if dst == src else dst
    except Exception:  # never stop a launch over the pane: this launch simply has none
        return None


def _sync_copy(src: Path, dst: Path) -> Path:
    """Mirror the shipped files of `src` into `dst`, writing only what differs; returns `dst`."""
    want: Dict[str, bytes] = {}
    for pattern in SHIPPED:
        for f in sorted(src.glob(pattern)):
            if f.is_file() and not f.is_symlink():
                want[f.relative_to(src).as_posix()] = f.read_bytes()
    if ".claude-plugin/plugin.json" not in want or "hooks/hooks.json" not in want:
        return src
    if dst.is_symlink() or (dst.exists() and not dst.is_dir()):
        return src
    for rel, data in want.items():
        p = dst / rel
        try:
            if p.is_file() and not p.is_symlink() and p.read_bytes() == data:
                continue
        except OSError:
            pass
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(".%s.%d.tmp" % (p.name, os.getpid()))
        tmp.write_bytes(data)
        os.replace(str(tmp), str(p))
    # a file a newer build no longer ships would still load (hooks) or type-check (types): drop it;
    # .claude-plugin/types/ is the engine's own and stays
    for sub in ("hooks", "types"):
        folder = dst / sub
        if folder.is_dir():
            for f in folder.iterdir():
                if f.is_file() and (sub + "/" + f.name) not in want:
                    try:
                        f.unlink()
                    except OSError:
                        pass
    return dst


def parse_version(text: str) -> Optional[Tuple[int, int, int]]:
    m = _VERSION.search(str(text or ""))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def supported(claude_version: str) -> bool:
    """True for Claude Code 2.1.287 and later (`claude --version` output or a bare version)."""
    v = parse_version(claude_version)
    return v is not None and v >= MIN_VERSION


# ---------------------------------------------------------------------------
# Core side
# ---------------------------------------------------------------------------

def _q(query: Any, name: str) -> str:
    try:
        v = query.get(name) if query is not None else None
    except Exception:
        v = None
    if isinstance(v, (list, tuple)):
        v = v[-1] if v else None
    return "" if v is None else str(v)[:128]


def _int(text: str, lo: int, hi: int, default: int) -> int:
    try:
        f = float(text)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return max(lo, min(hi, int(f)))


def _float(text: str, default: float) -> float:
    try:
        f = float(text)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return max(0.0, min(1e6, f))


def _truthy(text: str) -> bool:
    return text.strip().lower() in ("1", "true", "yes", "on")


_SURROGATE = re.compile("[\ud800-\udfff]")


def wellformed(text: Any) -> str:
    """`text` as a str that encodes as UTF-8: a lone surrogate becomes U+FFFD."""
    return _SURROGATE.sub("\ufffd", "" if text is None else str(text))


def _sid(scene: Dict[str, Any]) -> str:
    return wellformed(scene.get("id") or "")


def _pick(scenes: Any, want: str) -> Tuple[Optional[Dict[str, Any]], float, int]:
    """(scene, version, count) for the newest item, or the newest state of scene id `want`."""
    items: List[Dict[str, Any]] = []
    for it in scenes or []:
        if not isinstance(it, dict):
            continue
        if isinstance(it.get("scene"), dict):
            if it.get("op") == "clear" or not it["scene"]:
                continue
            items.append(it)
        elif it.get("kind"):                       # a bare scene dict
            items.append({"scene": it, "t": it.get("at") or 0})
    ids = {_sid(i["scene"]) for i in items}
    want = wellformed(want)
    for it in reversed(items):
        sc = it["scene"]
        if want and want != "latest" and _sid(sc) != want:
            continue
        try:
            version = float(it.get("t") or sc.get("at") or 0)
        except (TypeError, ValueError):
            version = 0.0
        if version != version or version in (float("inf"), float("-inf")):
            version = 0.0                          # Core's JSON answers refuse NaN and infinities
        return sc, version, len(ids)
    return None, 0.0, len(ids)


IDLE_SCENE: Dict[str, Any] = {
    "kind": "deck", "id": "pulsar-idle", "title": "Pulsar Stage",
    "slides": [{"title": "Pulsar Stage", "subtitle": "awaiting transmission", "layout": "title",
                "body": "Slides, charts and diagrams the model presents appear here, live."}],
}


def raster_route(scenes: list, query: Mapping[str, Any]) -> Tuple[int, Dict[str, Any]]:
    """GET /pulsar/stage/raster for a caller already scoped to its own scenes (as /pulsar/stage/scenes is).

    `scenes` is Core's stage items ({sid, op, scene, t}) filtered to the caller; `query` the
    request's query parameters. Returns (204, {}) when there is nothing to show, else (200, frame).
    Never raises: a scene that cannot be drawn becomes an error card inside the frame, and a bug in
    here answers (500, {"error": <exception name>}).
    """
    try:
        want = _q(query, "id")[:64] or "latest"
        scene, version, count = _pick(scenes, want)
        idle = scene is None and want == "latest" and _truthy(_q(query, "idle")) and not _truthy(_q(query, "meta"))
        if scene is None and not idle:
            return 204, {}
        cols = _int(_q(query, "cols"), 1, 512, 80)
        rows = _int(_q(query, "rows"), 1, 256, 24)
        from pulsar.stage import raster as RS
        if idle:
            from pulsar.stage.schema import normalize
            body = RS.frame(normalize(IDLE_SCENE), cols, rows, 0, _float(_q(query, "t"), 99.0) if _q(query, "t") else 99.0,
                            _truthy(_q(query, "ascii")))
            body.update(idle=True, version=0.0, count=0)
            return 200, body
        slides = RS.slide_count(scene)
        slide = _int(_q(query, "slide"), 0, slides - 1, 0)
        if _truthy(_q(query, "meta")):
            return 200, {
                "id": _sid(scene), "version": version, "count": count, "slides": slides,
                "slide": slide, "kind": wellformed(scene.get("kind") or ""),
                "title": RS._label(scene.get("title") or scene.get("kind") or ""),
            }
        from pulsar.stage.render import SETTLED
        t = _float(_q(query, "t"), SETTLED) if _q(query, "t") else SETTLED
        body = RS.frame(scene, cols, rows, slide, t, _truthy(_q(query, "ascii")))   # id, kind: well formed
        body["version"] = version
        body["count"] = count
        return 200, body
    except Exception as exc:  # never a traceback into Core's log loop; the plugin shows a quiet notice
        return 500, {"error": type(exc).__name__}


class PaneSeen:
    """Which sessions have a Claude Code Stage pane polling right now, from the X-Pulsar-Viewer header.

    Core calls `note(sid, request.headers.get("x-pulsar-viewer"))` in the raster route; the plugin
    polls about every 0.6 s, so an entry older than `ttl` seconds means the pane went away.
    """

    STATES = ("pane", "pane-auto", "pane-standby")

    def __init__(self, ttl: float = 4.0) -> None:
        self.ttl = ttl
        self._seen: Dict[str, Tuple[float, str]] = {}

    def note(self, sid: str, header: Optional[str], now: Optional[float] = None) -> None:
        state = (header or "").strip().lower()
        if state not in self.STATES or not sid:
            return
        self._seen[str(sid)[:64]] = (time.time() if now is None else now, state)
        if len(self._seen) > 512:
            cut = (time.time() if now is None else now) - self.ttl
            self._seen = {k: v for k, v in self._seen.items() if v[0] >= cut}

    def state(self, sid: str, now: Optional[float] = None) -> str:
        hit = self._seen.get(str(sid or "")[:64])
        if not hit:
            return ""
        at, state = hit
        if (time.time() if now is None else now) - at > self.ttl:
            return ""
        return state

    def active(self, sid: str, now: Optional[float] = None) -> bool:
        """True when a pane shows (or will open itself, placed, for) this session's next scene."""
        return self.state(sid, now) in ("pane", "pane-auto")
