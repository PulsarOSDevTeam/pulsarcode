# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Standalone HTML export of Pulsar Stage scenes.

The export is the web viewer in static mode: one self-contained file with
the scenes embedded as JSON in a <script type="application/json"> block.
The JSON is escaped so no scene text can close that block (`<`, `>`, `&`,
U+2028/2029 become \\uXXXX), and the page renders every string through
textContent, never as markup. Files land in ~/.pulsarcode/stage/ (0600).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

from pulsar.stage.schema import SceneError, normalize


def _home() -> Path:
    return Path(os.path.expanduser(os.environ.get("PULSAR_HOME") or "~/.pulsarcode"))   # empty means the default


def stage_dir() -> Path:
    return _home() / "stage"


def collect(scenes: Optional[Iterable[Any]]) -> List[Dict[str, Any]]:
    """Accept normalized scenes, raw scenes or Core stream items ({sid, op, scene, t}); return items.

    Every scene is re-validated; invalid ones are dropped. A scene that appears
    again with the same id replaces the earlier version in place.
    """
    items: List[Dict[str, Any]] = []
    index: Dict[str, int] = {}
    for raw in scenes or []:
        if not isinstance(raw, dict):
            continue
        sid, t, sc = "", None, raw
        if isinstance(raw.get("scene"), dict) and ("op" in raw or "sid" in raw or "t" in raw):
            if raw.get("op") == "clear":
                continue
            sc, sid, t = raw["scene"], str(raw.get("sid") or ""), raw.get("t")
        if not sc:
            continue
        try:
            norm = normalize(sc)
        except (SceneError, TypeError, ValueError):
            continue
        item = {"sid": sid[:64], "scene": norm, "t": float(t) if isinstance(t, (int, float)) else norm["at"]}
        sid_key = norm["id"]
        if sid_key in index:
            items[index[sid_key]] = item
        else:
            index[sid_key] = len(items)
            items.append(item)
    return items


def embed_json(data: Any) -> str:
    """JSON that is safe inside <script type="application/json"> (no tag breakout, no line separators)."""
    s = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return (s.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def export_html(scenes: Iterable[Any]) -> str:
    """The web viewer in static mode with `scenes` embedded."""
    from pulsar.stage.web import page
    return page(collect(scenes))


def _default_path() -> Path:
    d = stage_dir()
    stamp = time.strftime("stage-%Y%m%d-%H%M%S")
    p = d / (stamp + ".html")
    n = 2
    while p.exists():
        p = d / ("%s-%d.html" % (stamp, n))
        n += 1
    return p


def _earlier_export(p: Path) -> bool:
    """A file an earlier export wrote (the only kind of existing file an export may replace)."""
    try:
        if p.is_symlink() or not p.is_file():
            return False
        with open(p, "rb") as fh:
            head = fh.read(1 << 20)
    except OSError:
        return False
    return b"<title>Pulsar Stage</title>" in head and b'id="pulsar-data"' in head


def write_export(scenes: Iterable[Any], path: Optional[Union[str, Path]] = None) -> Path:
    """Write the export (mode 0600). Default location: ~/.pulsarcode/stage/stage-<time>.html.

    An existing file at `path` is replaced only when an earlier export wrote it: anything else (your
    notes.md, a page of your own) is refused with FileExistsError and left as it was.
    """
    html = export_html(scenes)
    if path is None:
        target = _default_path()
    else:
        target = Path(os.path.expanduser(str(path)))
        if target.is_dir():
            target = target / _default_path().name
    if (target.exists() or target.is_symlink()) and not _earlier_export(target):
        raise FileExistsError("%s already exists and is not a Pulsar Stage export; choose another name" % target)
    from pulsar.config import ensure_home, write_private
    in_home = target.parent == stage_dir()
    if in_home:
        ensure_home()   # a fresh home is claimed while still empty; stage/ inside an unclaimed one is refused
    target.parent.mkdir(parents=True, exist_ok=True)
    if in_home:
        try:
            os.chmod(target.parent, 0o700)
        except OSError:
            pass
    write_private(target, html)
    return target
