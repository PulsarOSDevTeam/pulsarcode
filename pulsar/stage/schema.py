# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Stage scene contract. Every producer (MCP tools) and consumer (terminal
viewer, web viewer, HTML export) goes through `normalize()`.

Scene kinds
  deck      {"slides": [Slide]}                                     a presentation
  card      {"body", "tone"}                                         a callout
  markdown  {"body"}                                                 free text
  code      {"code": {"lang", "text", "filename", "highlight": [int]}}
  table     {"columns": [str], "rows": [[str|num]]}
  chart     {"chart": "bar"|"line"|"spark", "labels": [str], "series": [{"name", "values": [num]}], "unit"}
  diagram   {"nodes": [{"id", "label"}], "edges": [{"from", "to", "label"}], "direction": "LR"|"TB"}
  metrics   {"metrics": [{"label", "value", "delta", "trend": [num]}]}
  timeline  {"events": [{"when", "title", "detail", "status"}]}
  progress  {"steps": [{"title", "status": pending|active|done|failed, "detail"}]}

Slide: {"title", "subtitle", "body" (markdown-lite), "layout", "code", "stat", "notes", and any scene kind's
fields under "embed": {"kind", ...}} so a slide can carry a chart, table, diagram or code panel.

Text is sanitized: control characters (including ESC) are stripped so a model
can never inject terminal escape sequences into the viewer. Fields drawn on one
row (titles, labels, table cells, names, units) hold no line break either: a
newline there would spill onto the next row of the terminal viewer.
"""

from __future__ import annotations

import re
import time
import unicodedata
import uuid
from typing import Any, Dict, List, Optional

KINDS = ("deck", "card", "markdown", "code", "table", "chart", "diagram", "metrics", "timeline", "progress")
LAYOUTS = ("auto", "title", "bullets", "split", "code", "quote", "stat", "embed")
TONES = ("info", "success", "warn", "error", "neutral")
STATUSES = ("pending", "active", "done", "failed")

MAX_SLIDES = 60
MAX_TEXT = 6000
MAX_SHORT = 200
MAX_ROWS = 200
MAX_COLS = 12
MAX_POINTS = 400
MAX_NODES = 60
MAX_ITEMS = 60

_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_ESC_SEQ = re.compile(r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\x1b[@-Z\\-_]")


class SceneError(ValueError):
    """Raised with a message written for the model, so it can fix its call."""


# Lone surrogates break JSON encoding in Core; bidi overrides/isolates and zero-width characters can
# make scene text display differently from what it says. Built from code points, never literals.
_INVISIBLE = re.compile("[%s-%s%s-%s%s-%s%s-%s%s]" % (
    chr(0xD800), chr(0xDFFF), chr(0x200B), chr(0x200F), chr(0x202A), chr(0x202E), chr(0x2060), chr(0x2069),
    chr(0xFEFF)))


# Unicode line and paragraph separators (and NEL, a C1 control) break lines in some renderers and have no
# agreed terminal width: they become plain newlines before anything else looks at the text.
_LINE_SEP = re.compile("[%s%s%s]" % (chr(0x2028), chr(0x2029), chr(0x85)))
# Combining marks kept after one base character. Real text needs several: Tibetan stacks subjoined consonants
# and a vowel (bsgrubs carries 3 marks, Sanskrit stacks more), pointed and cantillated Hebrew puts a dagesh, a
# shin dot, a vowel, an accent and a meteg on one letter, Vietnamese decomposes to 2. A flood ("zalgo") is cut
# here, and the web view also clips what a text box paints, so a capped stack still stays near its line.
MAX_MARKS = 6


def _cap_marks(s: str) -> str:
    """Keep at most MAX_MARKS combining marks (Mn, Me) per base character.

    Invisible format characters (Cf: soft hyphen, tag and musical format characters, ...) neither count nor end
    a run: browsers stack the marks on both sides of them on the same base, so they must not reset the count.
    """
    if s.isascii():
        return s
    out: List[str] = []
    run = 0
    for ch in s:
        o = ord(ch)
        if o >= 0x300 or o == 0xAD:
            cat = unicodedata.category(ch)
            if cat in ("Mn", "Me"):
                run += 1
                if run <= MAX_MARKS:
                    out.append(ch)
                continue
            if cat == "Cf":
                out.append(ch)
                continue
        run = 0
        out.append(ch)
    return "".join(out)


def clean(text: Any, limit: int = MAX_TEXT) -> str:
    if text is None:
        return ""
    s = str(text)
    s = _LINE_SEP.sub("\n", s)
    s = _ESC_SEQ.sub("", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    s = _CTRL.sub("", s)
    s = _INVISIBLE.sub("", s)
    s = _cap_marks(s)
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return s


_BREAKS = re.compile(r"[ ]*\n\s*")


def one_line(text: Any, limit: int = MAX_SHORT) -> str:
    """`clean` for one-line fields (ids, titles, labels, cells, names): line breaks become a single space.

    Every field that a viewer draws on one row goes through here, so no line break (nor a U+2028 / U+2029 /
    U+0085 turned into one) can reach a terminal cell, where it would move the cursor down a row.
    """
    return _BREAKS.sub(" ", clean(text, limit))


# A model that escapes its JSON twice sends the two characters backslash and n where it meant a line break.
# Text with no real line break and such a pair before a list marker, a blank line or the end is read that way;
# a lone pair inside a path (C:\new) is left alone.
_ESCAPED_BREAK = re.compile(r"\\n(?=\\n|[ \t]*(?:[-*#>|]|\d+[.)])|$)")


def prose(text: Any, limit: int = MAX_TEXT) -> str:
    """`clean` for running text (a body, a detail, notes), with doubly escaped line breaks restored."""
    s = "" if text is None else str(text)
    if "\n" not in s and _ESCAPED_BREAK.search(s):
        s = s.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "    ")
    return clean(s, limit)


# The names other tools give these states (Claude Code's own task list says in_progress and completed).
_STATUS_ALIASES = {"in_progress": "active", "in-progress": "active", "in progress": "active", "running": "active",
                   "completed": "done", "complete": "done", "finished": "done",
                   "todo": "pending", "queued": "pending", "not_started": "pending",
                   "error": "failed", "failure": "failed"}


def _status(v: Any, default: str = "") -> str:
    s = str(v or "").strip().lower()
    s = _STATUS_ALIASES.get(s, s)
    return s if s in STATUSES else default


def _short_repr(v: Any, limit: int = 40) -> str:
    """repr() for an error message, kept short (a huge integer is described, never converted to digits)."""
    if isinstance(v, int) and not isinstance(v, bool) and abs(v) >= 10 ** 30:
        return "an integer of about %d digits" % int(abs(v).bit_length() * 0.30103 + 1)
    r = repr(v)
    return r if len(r) <= limit else r[: limit - 3] + "..."


def _num(v: Any) -> float:
    try:
        f = float(v)
    except OverflowError:
        # A JSON integer literal too large for a float (401 digits): like 1e400, it is not a finite number.
        raise SceneError("numbers must be finite, got %s" % _short_repr(v))
    except (TypeError, ValueError):
        raise SceneError("expected a number, got %s" % _short_repr(v))
    if f != f or f in (float("inf"), float("-inf")):
        raise SceneError("numbers must be finite")
    return f


# Plotted values stay where a renderer can still subtract them: hi - lo of +-1e308 overflows to infinity,
# and a span of a subnormal divides to infinity. Larger magnitudes are clamped, tinier ones are zero.
MAX_VALUE = 1e300
MIN_VALUE = 1e-300


def _value(v: Any) -> float:
    f = _num(v)
    if f > MAX_VALUE:
        return MAX_VALUE
    if f < -MAX_VALUE:
        return -MAX_VALUE
    if -MIN_VALUE < f < MIN_VALUE:
        return 0.0
    return f


def _entry(v: Any, what: str, shape: str) -> Dict[str, Any]:
    """An object entry of a list field; a plain string is accepted as its title by the callers."""
    if not isinstance(v, dict):
        raise SceneError("%s must be %s objects or strings, got %s" % (what, shape, _typename(v)))
    return v


def _typename(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "a boolean"
    if isinstance(v, (int, float)):
        r = _short_repr(v)
        return r if r.startswith("an integer") else "a number " + r
    if isinstance(v, str):
        return "a string"
    if isinstance(v, (list, tuple)):
        return "a list"
    if isinstance(v, dict):
        return "an object"
    return type(v).__name__


def _list(v: Any, name: str, cap: int) -> List[Any]:
    if v is None:
        return []
    if not isinstance(v, list):
        raise SceneError("%s must be a list" % name)
    return v[:cap]


def _code(c: Any) -> Optional[Dict[str, Any]]:
    if not c:
        return None
    if isinstance(c, str):
        c = {"text": c}
    if not isinstance(c, dict):
        raise SceneError("code must be an object {lang, text}")
    return {
        "lang": one_line(c.get("lang") or c.get("language") or "", 24).lower(),
        "text": clean(c.get("text") or c.get("source") or "", 20000),
        "filename": one_line(c.get("filename") or "", MAX_SHORT),
        "highlight": [int(_num(n)) for n in _list(c.get("highlight"), "highlight", 200)],
    }


def _payload(kind: str, d: Dict[str, Any]) -> Dict[str, Any]:
    """Kind-specific fields, validated and clipped."""
    out: Dict[str, Any] = {}
    if kind in ("card", "markdown"):
        out["body"] = prose(d.get("body") or d.get("text") or d.get("content"))
        if not out["body"].strip() and not one_line(d.get("title")):
            raise SceneError("a %s scene needs text in body (or at least a title)" % kind)
        if kind == "card":
            tone = str(d.get("tone") or "info").lower()
            out["tone"] = tone if tone in TONES else "info"
    elif kind == "code":
        out["code"] = _code(d.get("code") or {"text": d.get("text"), "lang": d.get("lang"),
                                              "filename": d.get("filename"), "highlight": d.get("highlight")})
        if not out["code"] or not out["code"]["text"]:
            raise SceneError("code scene needs code.text")
    elif kind == "table":
        cols = [one_line(c, MAX_SHORT) for c in _list(d.get("columns"), "columns", MAX_COLS)]
        rows = []
        for r in _list(d.get("rows"), "rows", MAX_ROWS):
            if isinstance(r, dict):
                r = [r.get(c, "") for c in cols]
            if not isinstance(r, list):
                raise SceneError("table rows must be lists (or objects keyed by column)")
            rows.append([one_line(c, 400) for c in r[:MAX_COLS]])
        if not rows:
            raise SceneError('a table needs rows, for example {"kind": "table", "columns": ["Model", "Speed"], '
                             '"rows": [["kimi-k2.6", "fast"], ["glm-5", "faster"]]}')
        if not cols:
            cols = ["" for _ in rows[0]]
        out.update(columns=cols, rows=rows)
    elif kind == "chart":
        chart = str(d.get("chart") or d.get("type") or "bar").lower()
        out["chart"] = chart if chart in ("bar", "line", "spark") else "bar"
        out["labels"] = [one_line(x, 60) for x in _list(d.get("labels"), "labels", MAX_POINTS)]
        series = []
        raw_series = d.get("series")
        if raw_series is None and "values" in d:
            raw_series = [{"name": d.get("name") or "", "values": d.get("values")}]
        for s in _list(raw_series, "series", 8):
            if isinstance(s, list):
                s = {"name": "", "values": s}
            if not isinstance(s, dict):
                raise SceneError("series entries must be {name, values}")
            series.append({"name": one_line(s.get("name"), 60),
                           "values": [_value(v) for v in _list(s.get("values"), "values", MAX_POINTS)]})
        if not series or not any(s["values"] for s in series):
            raise SceneError("chart needs at least one series with values")
        out["series"] = series
        out["unit"] = one_line(d.get("unit"), 16)
    elif kind == "diagram":
        nodes, ids = [], set()
        for n in _list(d.get("nodes"), "nodes", MAX_NODES):
            if isinstance(n, str):
                n = {"id": n, "label": n}
            n = _entry(n, "diagram nodes", "{id, label}")
            nid = one_line(n.get("id") or n.get("label"), 60)
            if not nid or nid in ids:
                continue
            ids.add(nid)
            nodes.append({"id": nid, "label": one_line(n.get("label") or nid, 80),
                          "tone": str(n.get("tone") or "") if n.get("tone") in TONES else ""})
        edges = []
        for e in _list(d.get("edges"), "edges", MAX_NODES * 3):
            if isinstance(e, (list, tuple)):
                if len(e) < 2:
                    raise SceneError("diagram edges given as a list need [from, to] or [from, to, label]")
                e = {"from": e[0], "to": e[1], "label": e[2] if len(e) > 2 else ""}
            if not isinstance(e, dict):
                raise SceneError("diagram edges must be {from, to, label} or [from, to], got %s" % _typename(e))
            f, t = one_line(e.get("from"), 60), one_line(e.get("to"), 60)
            for x in (f, t):
                if x and x not in ids:
                    ids.add(x)
                    nodes.append({"id": x, "label": x, "tone": ""})
            if f and t:
                edges.append({"from": f, "to": t, "label": one_line(e.get("label"), 40)})
        if not nodes:
            raise SceneError("diagram needs nodes (or edges)")
        direction = str(d.get("direction") or "LR").upper()
        out.update(nodes=nodes, edges=edges, direction=direction if direction in ("LR", "TB") else "LR")
    elif kind == "metrics":
        ms = []
        for m in _list(d.get("metrics"), "metrics", 12):
            if not isinstance(m, dict):
                raise SceneError("metrics entries must be {label, value}")
            ms.append({"label": one_line(m.get("label"), 60), "value": one_line(m.get("value"), 40),
                       "delta": one_line(m.get("delta"), 24),
                       "trend": [_value(v) for v in _list(m.get("trend"), "trend", 120)]})
        if not ms:
            raise SceneError("metrics scene needs metrics")
        out["metrics"] = ms
    elif kind == "timeline":
        ev = []
        for e in _list(d.get("events"), "events", MAX_ITEMS):
            if isinstance(e, str):
                e = {"title": e}
            e = _entry(e, "timeline events", "{when, title, detail, status}")
            ev.append({"when": one_line(e.get("when"), 40),
                       "title": one_line(e.get("title") or e.get("label") or e.get("name"), MAX_SHORT),
                       "detail": prose(e.get("detail"), 600), "status": _status(e.get("status"))})
        if not ev:
            raise SceneError('a timeline needs events, for example {"kind": "timeline", "events": '
                             '[{"when": "09:00", "title": "Deploy started", "status": "done"}]}')
        out["events"] = ev
    elif kind == "progress":
        steps = []
        for s in _list(d.get("steps"), "steps", MAX_ITEMS):
            if isinstance(s, str):
                s = {"title": s}
            s = _entry(s, "progress steps", "{title, status, detail}")
            title = one_line(s.get("title") or s.get("label") or s.get("name") or s.get("text"), MAX_SHORT)
            if not title:
                raise SceneError("progress step %d needs a title" % (len(steps) + 1))
            steps.append({"title": title, "detail": one_line(s.get("detail"), 400),
                          "status": _status(s.get("status") or s.get("state"), "pending")})
        if not steps:
            raise SceneError('a progress scene needs steps, for example {"kind": "progress", "title": "Release", '
                             '"steps": [{"title": "Build", "status": "done"}, {"title": "Test", "status": "active"}, '
                             '{"title": "Ship", "status": "pending"}]}')
        out["steps"] = steps
    return out


def _slide(raw: Any, i: int) -> Dict[str, Any]:
    if isinstance(raw, str):
        raw = {"body": raw}
    if not isinstance(raw, dict):
        raise SceneError("slide %d must be an object" % (i + 1))
    bullets = raw.get("bullets")
    body = prose(raw.get("body") or raw.get("content") or "")
    if bullets and isinstance(bullets, list):
        body = (body + "\n" if body else "") + "\n".join("- " + one_line(b, 400) for b in bullets[:MAX_ITEMS])
    slide: Dict[str, Any] = {
        "title": one_line(raw.get("title"), MAX_SHORT),
        "subtitle": one_line(raw.get("subtitle"), MAX_SHORT),
        "body": body,
        "notes": prose(raw.get("notes"), 2000),
        "code": _code(raw.get("code")),
        "stat": None,
        "embed": None,
    }
    stat = raw.get("stat")
    if isinstance(stat, dict) and stat.get("value") is not None:
        slide["stat"] = {"value": one_line(stat.get("value"), 24), "label": one_line(stat.get("label"), 80)}
    embed = raw.get("embed")
    if isinstance(embed, dict) and embed.get("kind") in KINDS and embed.get("kind") != "deck":
        slide["embed"] = dict(kind=embed["kind"], **_payload(embed["kind"], embed))
    layout = str(raw.get("layout") or "auto").lower()
    if layout not in LAYOUTS:
        layout = "auto"
    if layout == "auto":
        if slide["embed"]:
            layout = "embed"
        elif slide["code"]:
            layout = "code"
        elif slide["stat"]:
            layout = "stat"
        elif i == 0 and not slide["body"]:
            layout = "title"
        elif "\n- " in "\n" + slide["body"] or "\n* " in "\n" + slide["body"]:
            layout = "bullets"
        else:
            layout = "auto"
    slide["layout"] = layout
    if not any((slide["title"], slide["body"], slide["code"], slide["stat"], slide["embed"])):
        raise SceneError("slide %d is empty" % (i + 1))
    return slide


def normalize(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize a scene. Raises SceneError with a model-readable message."""
    if not isinstance(raw, dict):
        raise SceneError("scene must be a JSON object")
    kind = str(raw.get("kind") or ("deck" if "slides" in raw else "markdown")).lower()
    if kind not in KINDS:
        raise SceneError("unknown kind %r; use one of %s" % (kind, ", ".join(KINDS)))
    scene: Dict[str, Any] = {
        "id": one_line(raw.get("id"), 64) or uuid.uuid4().hex[:12],
        "kind": kind,
        "title": one_line(raw.get("title"), MAX_SHORT),
        "subtitle": one_line(raw.get("subtitle"), MAX_SHORT),
        "at": time.time(),
    }
    if kind == "deck":
        slides = _list(raw.get("slides"), "slides", MAX_SLIDES)
        if not slides:
            raise SceneError("a deck needs slides: [{title, body}, ...]")
        scene["slides"] = [_slide(s, i) for i, s in enumerate(slides)]
        if not scene["title"]:
            scene["title"] = scene["slides"][0]["title"]
    else:
        scene.update(_payload(kind, raw))
    return scene


# JSON Schemas advertised to the model through MCP tools/list.
SLIDE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "subtitle": {"type": "string"},
        "body": {"type": "string", "description": "Markdown-lite: paragraphs, '- ' bullets, '1.' lists, **bold**, `code`, '# ' heading."},
        "layout": {"type": "string", "enum": list(LAYOUTS)},
        "code": {"type": "object", "properties": {"lang": {"type": "string"}, "text": {"type": "string"},
                                                  "filename": {"type": "string"},
                                                  "highlight": {"type": "array", "items": {"type": "integer"}}}},
        "stat": {"type": "object", "properties": {"value": {"type": "string"}, "label": {"type": "string"}}},
        "embed": {"type": "object", "description": "Any non-deck scene (kind + its fields) shown on the slide."},
        "notes": {"type": "string"},
    },
}
