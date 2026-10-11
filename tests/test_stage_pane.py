# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""The Stage pane inside Claude Code: raster cells, Core's raster route, launcher helpers, the plugin's files."""

import base64
import json
import os
import re
import struct
import sys
import time
import unicodedata
from pathlib import Path

import pytest

from pulsar.stage import pane as P
from pulsar.stage import raster as RS
from pulsar.stage import render as R
from pulsar.stage.schema import KINDS, normalize
from pulsar import fx

ROOT = Path(__file__).resolve().parent.parent
FNPLUG = ROOT / "pulsar" / "fnplug"

HOSTILE = ("\u4e2d\u6587\u6807\u9898 \U0001F600\U0001F680 emoji \u05e9\u05dc\u05d5\u05dd \u0645\u0631\u062d\u0628\u0627 "
           "\u200f\u202eRTL\u202c \x1b[31mred\x1b[0m \x1b]0;t\x07 fullwidth \uff21\uff22\uff23 "
           "combining e\u0301 zwj a\u200d\u200bb astral \U0001D400 private \ue000 filler \u3164\uffa0 "
           "nbsp\u00a0x soft\u00adhyphen tab\tz bom\ufeff vs\ufe0f \u2028sep \u0007bell \x7f\x85\x9b")

DECK = {"kind": "deck", "title": "Pane deck", "slides": [
    {"title": "Pulsar Code", "subtitle": "any model", "body": "One router.", "layout": "title"},
    {"title": "Bullets", "body": "- **one**\n- `two`\n- three " + HOSTILE},
    {"title": "Chart", "embed": {"kind": "chart", "chart": "bar", "labels": ["a", "b", "c"],
                                 "series": [{"name": "p50", "values": [3, 1, 2]}], "unit": "ms"}},
    {"title": "Flow", "embed": {"kind": "diagram", "nodes": ["cc", "core", "nv"],
                                "edges": [["cc", "core", "messages"], ["core", "nv"]]}},
    {"title": "Code", "body": "short", "code": {"lang": "py", "text": "def f(x):\n    return x  # \u4e2d\n"}},
    {"title": "Speed", "stat": {"value": "412ms", "label": "p50"}},
    {"body": "Make the common case fast. " + HOSTILE, "subtitle": "Lampson", "layout": "quote"},
    {"title": "Split", "layout": "split", "body": "- a\n- b", "code": {"lang": "ts", "text": "const a = 1;"}},
    {"title": HOSTILE, "subtitle": HOSTILE, "body": HOSTILE},
]}
SCENES = [
    {"kind": "card", "title": "Heads up " + HOSTILE, "tone": "warn", "body": "**Bold** " + HOSTILE},
    {"kind": "markdown", "body": "# H1 " + HOSTILE + "\n\n1. one\n2. two\n\n> q\n\n```py\nx = 1\n```"},
    {"kind": "code", "title": "C", "code": {"lang": "go", "text": "\n".join("line %d // \u4e2d" % i for i in range(40))}},
    {"kind": "table", "title": "T", "columns": ["\u540d\u524d", "emoji", "rtl"],
     "rows": [["\u53ea\u6709\u4e2d\u6587", "\U0001F600", "\u0645\u0631\u062d\u0628\u0627"]] * 5},
    {"kind": "chart", "title": "Line", "chart": "line", "series": [{"name": "in", "values": list(range(30))}]},
    {"kind": "chart", "title": "Spark", "chart": "spark", "series": [{"name": "rps", "values": [1, 5, 2, 8, 3]}]},
    {"kind": "chart", "title": "Bars", "chart": "bar", "labels": ["\u4e2d", "\U0001F680", "c"],
     "series": [{"name": "a", "values": [1, -2, 3]}, {"name": "b", "values": [3, 4, 1]}]},
    {"kind": "diagram", "title": "TB", "direction": "TB", "nodes": ["lint", "\u4e2d\u6587", "e2e"],
     "edges": [["lint", "\u4e2d\u6587", "\U0001F600"], ["\u4e2d\u6587", "e2e"]]},
    {"kind": "metrics", "metrics": [{"label": "Req " + HOSTILE, "value": "1,284", "delta": "+12%", "trend": [1, 2, 3]}]},
    {"kind": "timeline", "events": [{"when": "Sep", "title": HOSTILE, "detail": HOSTILE, "status": "done"}, "Later"]},
    {"kind": "progress", "title": "Ship", "steps": [{"title": "a", "status": "done"}, {"title": HOSTILE, "status": "active"}]},
]
SIZES = [(1, 1), (7, 3), (40, 12), (80, 24), (120, 40), (160, 45), (200, 50)]


def all_scenes():
    return [normalize(DECK)] + [normalize(s) for s in SCENES]


def frames_for(scene):
    return list(range(len(scene["slides"]))) if scene["kind"] == "deck" else [0]


def words(cells, cols, rows):
    assert len(cells) == cols * rows * 12
    return struct.unpack("<%dI" % (cols * rows * 3), cells)


# Code points Claude Code 2.1.287 refused as Raster cells although Python 3.9 to 3.12 call them
# printable and narrow (Unicode 16 made the trigrams, digrams and hexagrams wide); one of them in a
# frame blanked the whole pane. pulsar/fnplug/tests/cells.test.tsx asks the engine about the rest.
ENGINE_REFUSED = ([0x0980, 0x0C80, 0x0D3A] + list(range(0x2630, 0x2638)) + list(range(0x268A, 0x2690))
                  + list(range(0x4DC0, 0x4E00)))
CELLS_TSX = FNPLUG / "tests" / "cells.test.tsx"


def cell_ok(cp):
    """The Raster contract: printable ASCII or a code point of the engine-verified table, and never
    anything Python itself knows is a control, mark, format character or wide."""
    if not (0x20 <= cp <= 0xFFFF) or 0x7F <= cp <= 0x9F or 0xD800 <= cp <= 0xDFFF:
        return False
    if not RS.in_cell_table(cp):
        return False
    ch = chr(cp)
    if unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Mn", "Me", "Mc", "Zl", "Zp"):
        return False
    return unicodedata.east_asian_width(ch) not in ("W", "F")


def colour_ok(c):
    return c <= 0xFFFFFF or c == RS.DEFAULT


def assert_cells_valid(cells, cols, rows, ascii=False):
    w = words(cells, cols, rows)
    for i in range(cols * rows):
        cp, fg, bg = w[3 * i], w[3 * i + 1], w[3 * i + 2]
        assert cell_ok(cp), "cell %d holds U+%04X" % (i, cp)
        if ascii:
            assert cp < 0x7F, "cell %d holds U+%04X in ASCII mode" % (i, cp)
        assert colour_ok(fg) and colour_ok(bg), (i, hex(fg), hex(bg))


# ---------------------------------------------------------------------------
# raster.render / encode / frame
# ---------------------------------------------------------------------------

def test_every_kind_is_covered():
    assert {s["kind"] for s in all_scenes()} == set(KINDS)


@pytest.mark.parametrize("cols,rows", SIZES)
@pytest.mark.parametrize("ascii", [False, True])
def test_render_length_codepoints_and_colours_for_every_scene(cols, rows, ascii):
    for sc in all_scenes():
        for slide in frames_for(sc):
            for t in (0.0, 0.3, 0.9, 9.0):
                cells = RS.render(sc, cols, rows, slide, t, ascii)
                assert isinstance(cells, bytes)
                assert_cells_valid(cells, cols, rows, ascii)


def test_hostile_text_never_reaches_a_cell():
    sc = normalize({"kind": "deck", "title": HOSTILE, "slides": [{"title": HOSTILE, "body": HOSTILE}]})
    cells = RS.render(sc, 120, 30, 0, 9.0)
    assert_cells_valid(cells, 120, 30)
    cps = set(words(cells, 120, 30)[0::3])
    for bad in (0x1B, 0x07, 0x200F, 0x202E, 0x200D, 0x200B, 0xFEFF, 0xFE0F, 0x0301, 0xE000, 0x3164, 0xFFA0, 0x7F):
        assert bad not in cps, hex(bad)
    assert RS.PLACEHOLDER in cps            # CJK and emoji stand in as a placeholder, never dropped
    assert ord("A") in cps                  # fullwidth Latin folds to plain Latin


def test_safe_codepoint_rules():
    s = RS.safe_codepoint
    assert s("a") == ord("a") and s(" ") == 0x20 and s("\u2500") == 0x2500 and s("\u28ff") == 0x28FF
    assert s("") == RS.SPACE                       # second cell of a wide glyph
    assert s("\u4e2d") == RS.PLACEHOLDER and s("\U0001F600") == RS.PLACEHOLDER and s("\u26a1") == RS.PLACEHOLDER
    assert s("\uff21") == ord("A")
    assert s("\x1b") == RS.SPACE and s("\x9b") == RS.SPACE and s("\u202e") == RS.SPACE and s("\u0301") == RS.SPACE
    assert s("ab") == RS.PLACEHOLDER               # never forward a cluster
    assert s("\ue000") == RS.PLACEHOLDER and s("\U0001D400") == RS.PLACEHOLDER and s("\u3164") == RS.PLACEHOLDER
    assert s("\u00a0") == RS.SPACE and s("\u3000") == RS.SPACE
    assert s("\u2500", ascii=True) == RS.ASCII_PLACEHOLDER and s("x", ascii=True) == ord("x")


def table_codepoints():
    out = set(range(0x20, 0x7F))
    for lo, hi in RS.CELL_RANGES:
        out.update(range(lo, hi + 1))
    return out


def test_cell_table_is_sorted_bmp_and_narrow():
    prev = 0x7E
    for lo, hi in RS.CELL_RANGES:
        assert prev < lo <= hi <= 0xFFFF, (hex(lo), hex(hi))
        assert not (lo <= 0xDFFF and hi >= 0xD800)
        prev = hi
    table = table_codepoints()
    assert RS.PLACEHOLDER in table and RS.SPACE in table
    assert not table & set(ENGINE_REFUSED)
    for cp in table - {RS.SPACE}:
        ch = chr(cp)
        cat = unicodedata.category(ch)
        if cat == "Cn":                      # assigned after this Python's Unicode tables (U+20C0 on 3.9/3.10)
            assert cp == 0x20C0 and sys.version_info < (3, 11), hex(cp)
            continue
        assert cat not in ("Cc", "Cf", "Cs", "Co", "Mn", "Me", "Mc", "Zs", "Zl", "Zp"), hex(cp)
        assert unicodedata.east_asian_width(ch) not in ("W", "F") and not unicodedata.combining(ch), hex(cp)


def test_raster_emits_only_the_engine_verified_table():
    """Whatever the canvas holds, on any Python: ASCII, CELL_RANGES (every one mounted by the engine)."""
    table = table_codepoints()
    emitted = {RS._classify(chr(o)) for o in range(0x110000)}
    assert emitted == table
    assert {RS._safe_a(chr(o)) for o in range(0, 0x110000, 3)} <= set(range(0x20, 0x7F))


def test_engine_refused_code_points_become_placeholders():
    for cp in ENGINE_REFUSED:
        assert RS.safe_codepoint(chr(cp)) == RS.PLACEHOLDER, hex(cp)
    # the reviewer's case: a hamburger icon in a deck bullet, a markdown body and a table cell
    burger = "Tap \u2630 to open the side menu \u4dc0 \u0980"
    scenes = [normalize({"kind": "deck", "title": "Nav", "slides": [{"title": "Menu", "body": "- " + burger}]}),
              normalize({"kind": "markdown", "body": "# Menu\n\n" + burger}),
              normalize({"kind": "table", "columns": ["icon", "what"], "rows": [["\u2630", burger]]})]
    for sc in scenes:
        for cols, rows in ((160, 44), (80, 24)):
            st, body = P.raster_route([item(sc)], {"cols": str(cols), "rows": str(rows)})
            assert st == 200
            cells = base64.b64decode(body["cells"])
            assert_cells_valid(cells, cols, rows)
            cps = set(words(cells, cols, rows)[0::3])
            assert not cps & set(ENGINE_REFUSED), sc["kind"]
            assert RS.PLACEHOLDER in cps and ord("T") in cps, sc["kind"]


def _tsx_ranges():
    src = CELLS_TSX.read_text("utf-8")
    body = src[src.index("// cells:begin"):src.index("// cells:end")]
    pairs = re.findall(r"\[0x([0-9a-fA-F]+), 0x([0-9a-fA-F]+)\]", body)
    return tuple((int(a, 16), int(b, 16)) for a, b in pairs)


def test_engine_oracle_test_mounts_exactly_the_table():
    """cells.test.tsx (run by `claude plugin test pulsar/fnplug`) holds raster.CELL_RANGES, no more, no less."""
    assert _tsx_ranges() == RS.CELL_RANGES
    src = CELLS_TSX.read_text("utf-8")
    assert "for (let cp = 0x20; cp <= 0x7e; cp++)" in src and "U+2630" in src


def test_pack_keeps_the_grid_whatever_the_canvas_holds():
    cv = fx.Canvas(6, 2)
    cv.cells[0] = [("\u4e2d", (255, 0, 0), True), ("", (255, 0, 0), True), ("ab", None, False),
                   ("\x1b", (300, -5, 7), False), ("\U0001F600", None, False), ("x", "junk", False)]
    cv.cells[1] = [("\u2588", (1, 2, 3), False)] * 6
    cells = RS.pack(cv)
    assert_cells_valid(cells, 6, 2)
    w = words(cells, 6, 2)
    assert w[0::3][:6] == (RS.PLACEHOLDER, RS.SPACE, RS.PLACEHOLDER, RS.SPACE, RS.PLACEHOLDER, ord("x"))
    assert w[1] == 0xFF0000 and w[3 * 3 + 1] == 0xFF0007   # channels clamp
    assert w[3 * 2 + 1] == RS.FG and w[3 * 5 + 1] == RS.FG  # uncoloured or bad colour: the body colour
    assert w[3 * 6 + 1] == 0x010203 and w[3 * 6 + 2] == RS.BG


def test_render_clamps_geometry_and_survives_garbage():
    sc = normalize(DECK)
    assert len(RS.render(sc, 0, 0)) == 12
    assert len(RS.render(sc, 9999, 9999, 0, 9.0)) == RS.MAX_COLS * RS.MAX_ROWS * 12
    for t in (float("nan"), float("inf"), -5, "x", None):
        assert len(RS.render(sc, 20, 5, 0, t)) == 20 * 5 * 12
    assert len(RS.render(sc, 20, 5, 999, 9.0)) == 20 * 5 * 12
    assert len(RS.render({"kind": "chart", "series": "nope"}, 20, 5)) == 20 * 5 * 12   # draws an error card
    assert len(RS.render(None, 20, 5)) == 20 * 5 * 12                                  # type: ignore[arg-type]


def test_encode_is_padded_base64_of_the_cells():
    cells = RS.render(normalize(DECK), 7, 3, 0, 9.0)
    b64 = RS.encode(cells)
    assert base64.b64decode(b64) == cells
    assert len(b64) == -(-7 * 3 * 12 // 3) * 4


def test_frame_fields_and_animation_window():
    sc = normalize(DECK)
    f = RS.frame(sc, 80, 24, 1, 0.2)
    assert set(f) >= {"cells", "cols", "rows", "slides", "slide", "animating", "title"}
    assert (f["cols"], f["rows"], f["slides"], f["slide"]) == (80, 24, len(DECK["slides"]), 1)
    assert f["title"] == "Pane deck" and f["slide_title"] == "Bullets"
    assert f["animating"] is True
    assert_cells_valid(base64.b64decode(f["cells"]), 80, 24)
    assert RS.frame(sc, 80, 24, 1, 9.0)["animating"] is False
    assert RS.frame(sc, 80, 24, 1, RS.SETTLE_CAP + 1)["animating"] is False
    assert RS.frame(sc, 80, 24, 99, 9.0)["slide"] == len(DECK["slides"]) - 1
    assert RS.frame(sc, 80, 24, -3, 9.0)["slide"] == 0
    # the same moment is the same frame; the settled frame is where every intro ends
    assert RS.frame(sc, 80, 24, 2, 0.4)["cells"] == RS.frame(sc, 80, 24, 2, 0.4)["cells"]
    assert RS.frame(sc, 80, 24, 2, 9.0)["cells"] == RS.frame(sc, 80, 24, 2, R.SETTLED)["cells"]


def test_every_intro_settles_inside_the_window():
    for sc in all_scenes():
        for slide in frames_for(sc):
            assert RS.frame(sc, 100, 30, slide, 0.05)["animating"] or sc["kind"] in ("chart",), (sc["kind"], slide)
            assert RS.frame(sc, 100, 30, slide, 6.0)["animating"] is False, (sc["kind"], slide)


def test_frame_labels_are_printable():
    f = RS.frame(normalize({"kind": "deck", "title": "a\u202eb\u200bc\x1bd", "slides": [{"title": "x"}]}), 10, 3)
    assert not re.search("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\ufeff]", f["title"])
    assert "a" in f["title"] and "d" in f["title"]


def test_frame_is_fast():
    sc = normalize(DECK)
    RS.frame(sc, 120, 40, 2, 0.4)           # warm the settled frame and the layout caches, as a pane does
    best = 1e9
    for _ in range(5):
        t0 = time.perf_counter()
        RS.frame(sc, 120, 40, 2, 0.5)
        best = min(best, time.perf_counter() - t0)
    assert best < 0.05, best                # ~3 ms on a laptop; generous for slow CI


# ---------------------------------------------------------------------------
# pane.raster_route
# ---------------------------------------------------------------------------

def item(scene, t=1.0, sid="s1", op="show"):
    return {"sid": sid, "op": op, "scene": normalize(scene), "t": t}


def test_route_answers_204_without_a_scene():
    assert P.raster_route([], {}) == (204, {})
    assert P.raster_route(None, {"cols": "10"}) == (204, {})   # type: ignore[arg-type]
    assert P.raster_route([{"sid": "s1", "op": "clear", "scene": {}, "t": 1}], {}) == (204, {})
    assert P.raster_route([item(DECK)], {"id": "nope"}) == (204, {})


def test_route_idle_screen_only_when_asked():
    status, body = P.raster_route([], {"idle": "1", "cols": "60", "rows": "20"})
    assert status == 200 and body["idle"] is True and body["count"] == 0 and body["slides"] == 1
    assert_cells_valid(base64.b64decode(body["cells"]), 60, 20)
    assert body["animating"] is False                       # settled unless the caller plays it
    assert P.raster_route([], {"idle": "1", "meta": "1"}) == (204, {})
    assert P.raster_route([], {"idle": "1", "id": "gone"}) == (204, {})
    status, body = P.raster_route([item(DECK)], {"idle": "1", "cols": "20", "rows": "5"})
    assert status == 200 and "idle" not in body and body["title"] == "Pane deck"


def test_route_picks_latest_or_by_id_and_reports_version():
    a = item({"kind": "markdown", "id": "a", "title": "A", "body": "x"}, t=1.0)
    b = item({"kind": "markdown", "id": "b", "title": "B", "body": "y"}, t=2.0)
    a2 = item({"kind": "markdown", "id": "a", "title": "A2", "body": "z"}, t=3.0)
    status, body = P.raster_route([a, b], {"cols": "20", "rows": "5"})
    assert status == 200 and body["id"] == "b" and body["version"] == 2.0 and body["count"] == 2
    status, body = P.raster_route([a, b, a2], {"id": "a", "cols": "20", "rows": "5"})
    assert body["title"] == "A2" and body["version"] == 3.0
    status, body = P.raster_route([a, b], {"id": "latest", "cols": "20", "rows": "5"})
    assert body["id"] == "b"


def test_route_clamps_every_parameter():
    items = [item(DECK)]
    cases = [({"cols": "0", "rows": "0"}, (1, 1)), ({"cols": "9999", "rows": "9999"}, (512, 256)),
             ({"cols": "abc", "rows": ""}, (80, 24)), ({"cols": "nan", "rows": "inf"}, (80, 24)),
             ({"cols": ["30", "40"], "rows": ["9"]}, (40, 9)), ({}, (80, 24))]
    for q, (c, r) in cases:
        status, body = P.raster_route(items, q)
        assert status == 200 and (body["cols"], body["rows"]) == (c, r), q
        assert len(base64.b64decode(body["cells"])) == c * r * 12
    assert P.raster_route(items, {"slide": "99", "cols": "8", "rows": "2"})[1]["slide"] == len(DECK["slides"]) - 1
    assert P.raster_route(items, {"slide": "-4", "cols": "8", "rows": "2"})[1]["slide"] == 0
    for t in ("-1", "nan", "1e309", "x"):
        assert P.raster_route(items, {"t": t, "cols": "8", "rows": "2"})[0] == 200
    assert P.raster_route(items, {"t": "0.1", "cols": "60", "rows": "20"})[1]["animating"] is True
    assert P.raster_route(items, {"cols": "60", "rows": "20"})[1]["animating"] is False   # no t: settled
    ascii_body = P.raster_route(items, {"ascii": "1", "cols": "60", "rows": "20", "slide": "8"})[1]
    assert_cells_valid(base64.b64decode(ascii_body["cells"]), 60, 20, ascii=True)


def test_route_meta_is_cheap_and_has_no_cells():
    status, body = P.raster_route([item(DECK, t=7.5)], {"meta": "1", "cols": "500", "rows": "200"})
    assert status == 200 and "cells" not in body
    assert body == {"id": body["id"], "version": 7.5, "count": 1, "slides": len(DECK["slides"]), "slide": 0,
                    "kind": "deck", "title": "Pane deck"}


def _as_core_json(body):
    """What Core's JSONResponse does with a body: UTF-8 JSON, no NaN."""
    return json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def test_route_bodies_encode_with_a_lone_surrogate_id():
    sc = normalize(json.loads('{"kind":"card","id":"deck\\ud83d","title":"T\\udc00x","body":"b\\ud800"}'))
    assert sc["id"] == "deck"                         # the schema drops lone surrogates now
    sc = dict(sc, id="deck\ud83d")                # the route must still cope if one slips through
    items = [{"sid": "s1", "op": "show", "scene": sc, "t": 5.0}]   # as stored, no re-normalizing
    for q in ({"meta": "1"}, {"cols": "20", "rows": "4"}, {"cols": "20", "rows": "4", "t": "0.1"}):
        status, body = P.raster_route(items, q)
        assert status == 200
        _as_core_json(body)
        assert body["id"] == "deck\ufffd"
    # the plugin asks for that id back (encodeURIComponent of U+FFFD): it is the same scene
    status, body = P.raster_route(items, {"id": "deck\ufffd", "cols": "20", "rows": "4"})
    assert status == 200 and body["id"] == "deck\ufffd"
    assert P.raster_route(items, {"id": "deck\ud83d", "meta": "1"})[0] == 200
    _as_core_json(RS.frame(sc, 10, 3))
    assert P.wellformed("a\ud800b\U0001F600") == "a\ufffdb\U0001F600"


def test_route_version_is_always_finite():
    for t in (float("nan"), float("inf"), float("-inf")):
        status, body = P.raster_route([{"sid": "s", "op": "show", "scene": normalize({"kind": "card", "body": "x"}),
                                        "t": t}], {"meta": "1"})
        assert status == 200 and body["version"] == 0.0
        _as_core_json(body)
    sc = normalize({"kind": "card", "body": "x"})
    sc["at"] = float("nan")
    for t in ("x", None, 0):
        status, body = P.raster_route([{"sid": "s", "op": "show", "scene": sc, "t": t}], {"cols": "8", "rows": "2"})
        assert status == 200 and body["version"] == 0.0
        _as_core_json(body)


def test_route_accepts_bare_scenes_and_never_raises(monkeypatch):
    status, body = P.raster_route([normalize({"kind": "markdown", "body": "hi"})], {"cols": "10", "rows": "3"})
    assert status == 200 and body["kind"] == "markdown"
    assert P.raster_route(["junk", 3, {"scene": "x"}], {}) == (204, {})

    def boom(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(RS, "frame", boom)
    assert P.raster_route([item(DECK)], {}) == (500, {"error": "RuntimeError"})


# ---------------------------------------------------------------------------
# pane: launcher helpers and the viewer header
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["off", "OFF", "0", "false", "no", " off "])
def test_plugin_dir_switch_off(monkeypatch, value):
    monkeypatch.setenv("PULSAR_STAGE_PANE", value)
    assert P.plugin_dir() is None


@pytest.mark.parametrize("value", [None, "", "on", "1", "auto"])
def test_plugin_dir_on(monkeypatch, tmp_path, value):
    from pulsar import config as K
    monkeypatch.setenv("PULSAR_HOME", str(tmp_path / "home"))
    if value is None:
        monkeypatch.delenv("PULSAR_STAGE_PANE", raising=False)
    else:
        monkeypatch.setenv("PULSAR_STAGE_PANE", value)
    assert P.plugin_dir() is None                          # no home yet: no pane, and no home made from here
    assert not (tmp_path / "home").exists()
    K.ensure_home()
    d = P.plugin_dir()
    assert d is not None and d == tmp_path / "home" / "cache" / "fnplug" and d != P.package_plugin_dir()
    assert (d / ".claude-plugin" / "plugin.json").is_file()


def shipped(root):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                  if p.is_file() and "/.claude-plugin/types/" not in "/" + p.relative_to(root).as_posix())


def test_plugin_dir_is_a_copy_the_engine_may_write_into(monkeypatch, tmp_path):
    """Claude Code writes ~740 KB of generated types into a --plugin-dir: never into the package."""
    monkeypatch.delenv("PULSAR_STAGE_PANE", raising=False)
    home = tmp_path / "home"
    monkeypatch.setenv("PULSAR_HOME", str(home))
    from pulsar import config as K
    K.ensure_home()                  # the launcher claims the home before the pane is set up
    d = P.plugin_dir()
    assert d == home / "cache" / "fnplug"
    files = shipped(d)
    assert files == sorted([".claude-plugin/plugin.json", "hooks/hooks.json", "hooks/stage.tsx",
                            "tsconfig.json", "types/index.d.ts"])
    for rel in files:
        assert (d / rel).read_bytes() == (FNPLUG / rel).read_bytes(), rel
    assert not (d / "tests").exists()                      # the plugin's own tests stay in the package
    # the engine's types survive; an unchanged file is not rewritten; a stale hook module is dropped
    (d / ".claude-plugin" / "types" / "claude-code").mkdir(parents=True)
    (d / ".claude-plugin" / "types" / "claude-code" / "index.d.ts").write_text("// engine", "utf-8")
    (d / "hooks" / "old.ts").write_text("export {}", "utf-8")
    past = time.time() - 1000
    os.utime(str(d / "hooks" / "stage.tsx"), (past, past))
    (d / "tsconfig.json").write_text("{}", "utf-8")
    assert P.plugin_dir() == d
    assert (d / ".claude-plugin" / "types" / "claude-code" / "index.d.ts").read_text("utf-8") == "// engine"
    assert not (d / "hooks" / "old.ts").exists()
    assert abs((d / "hooks" / "stage.tsx").stat().st_mtime - past) < 1
    assert (d / "tsconfig.json").read_bytes() == (FNPLUG / "tsconfig.json").read_bytes()
    assert not list(d.rglob("*.tmp"))


def test_plugin_dir_never_falls_back_to_the_package(monkeypatch, tmp_path):
    """Claude Code would write its generated types into the package folder: no pane rather than that."""
    monkeypatch.delenv("PULSAR_STAGE_PANE", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (home / "cache").symlink_to(elsewhere)                 # never follow a planted link out of the home
    monkeypatch.setenv("PULSAR_HOME", str(home))
    assert P.plugin_dir() is None
    assert not list(elsewhere.iterdir())
    (home / "cache").unlink()
    (home / "cache").mkdir()
    (home / "cache" / "fnplug").write_text("not a folder", "utf-8")
    assert P.plugin_dir() is None
    (home / "cache" / "fnplug").unlink()
    (home / "cache").rmdir()                                # a home without its cache folder gets one
    assert P.plugin_dir() == home / "cache" / "fnplug"


@pytest.mark.parametrize("version,ok", [
    ("2.1.287 (Claude Code)", True), ("2.1.287", True), ("2.1.288", True), ("2.2.0", True), ("3.0.0", True),
    ("v2.1.300", True), ("2.1.286", False), ("2.1.42 (Claude Code)", False), ("1.99.999", False),
    ("", False), ("garbage", False), (None, False)])
def test_supported_versions(version, ok):
    assert P.supported(version) is ok   # type: ignore[arg-type]


def test_pane_seen_tracks_viewer_header():
    seen = P.PaneSeen(ttl=4.0)
    assert seen.state("s1", now=100) == "" and not seen.active("s1", now=100)
    seen.note("s1", "pane-standby", now=100)
    assert seen.state("s1", now=101) == "pane-standby" and not seen.active("s1", now=101)
    seen.note("s1", "Pane-Auto", now=102)
    assert seen.active("s1", now=103)
    seen.note("s1", "pane", now=104)
    assert seen.state("s1", now=105) == "pane" and seen.active("s1", now=105)
    assert not seen.active("s1", now=109)            # the pane stopped polling
    seen.note("s2", "evil\r\nheader", now=110)
    seen.note("", "pane", now=110)
    assert seen.state("s2", now=110) == "" and seen.state("", now=110) == ""
    for i in range(600):
        seen.note("x%d" % i, "pane", now=200 + i)
    assert len(seen._seen) <= 600 and seen.active("x599", now=800)


# ---------------------------------------------------------------------------
# the function-hooks plugin's files
# ---------------------------------------------------------------------------

def test_plugin_manifest_and_module():
    manifest = json.loads((FNPLUG / ".claude-plugin" / "plugin.json").read_text("utf-8"))
    assert manifest["name"] == P.PLUGIN_NAME == "pulsar-stage"
    assert (FNPLUG / manifest["types"]).is_file()
    hooks = json.loads((FNPLUG / "hooks" / "hooks.json").read_text("utf-8"))
    assert hooks["modules"] == ["./stage.tsx"]
    src = (FNPLUG / "hooks" / "stage.tsx").read_text("utf-8")
    for name in ("PULSAR_CORE", "PULSAR_SESSION_TOKEN", "ANTHROPIC_AUTH_TOKEN"):
        assert "$.env.get('%s')" % name in src
    assert "X-Pulsar-Viewer" in src and "/pulsar/stage/raster" in src
    assert "import(" not in src and "console." not in src
    # the token rides headers (or curl's stdin), never a URL, argv, toast, log line or drawing
    assert not re.search(r"(toast|log|trace)\([^)]*token", src)
    assert "'-H', '@-'" in src
    # curl reads no .curlrc (-q works only as the first argument) and speaks plain http only; it is the
    # system's own (an absolute path), never a curl that a relative PATH entry finds first
    assert "curl, '-q', '-sS', '--proto', '=http'" in src
    assert "const CURLS = ['/usr/bin/curl', '/bin/curl', 'C:\\\\Windows\\\\System32\\\\curl.exe']" in src
    assert "'curl', '-q'" not in src
    # pane-auto only where the engine places an unasked open: fullscreen and 144 columns (110 once asked)
    assert "const UNASKED_FLOOR = 144" in src and "const ASKED_FLOOR = 110" in src
    assert "(await opensItself($)) ? 'pane-auto' : 'pane-standby'" in src


def test_shipped_files_have_no_em_dash():
    paths = [ROOT / "pulsar" / "stage" / "raster.py", ROOT / "pulsar" / "stage" / "pane.py", Path(__file__)]
    paths += [p for p in FNPLUG.rglob("*") if p.is_file() and ".claude-plugin/types" not in p.as_posix()]
    for p in paths:
        assert "\u2014" not in p.read_text("utf-8"), p
