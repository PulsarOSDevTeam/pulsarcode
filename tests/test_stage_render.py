# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Stage terminal renderer: every scene kind fits the width it is given, at narrow and wide sizes,
in Unicode and ASCII, with hostile text (CJK, emoji, very long labels) and awkward numbers
(negative, flat, single point, huge, subnormal, non-finite)."""

import re
import unicodedata

import pytest

from pulsar import fx
from pulsar.stage import render as R
from pulsar.stage import term as T
from pulsar.stage.schema import normalize

try:  # an independent width table when available; the renderer's own cwidth is checked either way
    import wcwidth as _wcw
except ImportError:  # pragma: no cover - optional
    _wcw = None

CJK = "".join(chr(c) for c in (0x6570, 0x636E, 0x4E2D, 0x5FC3, 0x5EF6, 0x8FDF))   # six wide ideographs
EMOJI = chr(0x1F600) + chr(0x1F680)
LONG = "Extraordinarily long label that keeps going and going " * 3

SCENES = [
    {"kind": "deck", "title": "Deck", "slides": [
        {"title": CJK * 10, "subtitle": EMOJI * 10, "layout": "title"},
        {"title": "Bullets", "body": "- **One** process\n- " + CJK * 20 + "\n- " + LONG},
        {"title": LONG, "stat": {"value": "1,234,567,890ms", "label": LONG}},
        {"title": "q", "layout": "quote", "body": CJK * 30, "subtitle": LONG},
        {"title": "s", "layout": "split", "body": "- " + CJK * 20, "code": {"lang": "py", "text": EMOJI * 40}},
        {"title": "Chart", "embed": {"kind": "chart", "chart": "bar", "labels": [CJK, EMOJI], "series": [{"values": [1, -1]}]}},
        {"title": "Line", "embed": {"kind": "chart", "chart": "line", "labels": ["a", "b", "c"],
                                    "series": [{"name": "p50", "values": [3, 1, 2]}], "unit": "ms"}},
        {"title": "Flow", "embed": {"kind": "diagram", "nodes": ["cc", "core", "nv"],
                                    "edges": [["cc", "core", "messages"], ["core", "nv", CJK]]}},
        {"title": "Table", "embed": {"kind": "table", "columns": ["a", CJK], "rows": [["1", EMOJI], ["2", "fail"]]}},
        {"title": "Metrics", "embed": {"kind": "metrics", "metrics": [{"label": "a", "value": "9", "trend": [1, 2]}]}},
        {"title": "Timeline", "embed": {"kind": "timeline", "events": ["a", {"when": CJK, "title": LONG}]}},
        {"title": "Progress", "embed": {"kind": "progress", "steps": ["a", {"title": "b", "status": "done"}]}},
    ]},
    {"kind": "chart", "chart": "bar", "title": "Bars", "labels": ["NVIDIA", "OpenRouter", "Groq"],
     "series": [{"values": [12, 30, 7]}]},
    {"kind": "chart", "chart": "bar", "title": CJK + EMOJI, "labels": [CJK, EMOJI + "x", LONG, "d"],
     "series": [{"name": CJK, "values": [-5, -1, -3, -2]}], "unit": "ms"},
    {"kind": "chart", "chart": "bar", "labels": ["a" * 30] + list("bcdefghi"),
     "series": [{"values": [1, -2, 3, -4, 5, 6, 7, 8, 9]}]},
    {"kind": "chart", "chart": "bar", "labels": ["x", "y"],
     "series": [{"name": "a", "values": [1, 2]}, {"name": LONG, "values": [3, 4]}]},
    {"kind": "chart", "chart": "bar", "labels": ["a"], "series": [{"values": [0]}]},
    {"kind": "chart", "chart": "bar", "series": [{"values": [1e308, -1e308, 3]}], "unit": "tokens"},
    {"kind": "chart", "chart": "bar", "series": [{"values": [5e-324, 0]}]},
    {"kind": "chart", "chart": "bar", "labels": [str(i) for i in range(120)],
     "series": [{"values": list(range(120))}, {"values": list(range(120))}]},
    {"kind": "chart", "chart": "line", "labels": ["%s 2026" % m for m in ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                                                                         "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")],
     "series": [{"name": LONG, "values": list(range(1, 13))}, {"name": CJK, "values": [-3] * 12}], "unit": "req/s"},
    {"kind": "chart", "chart": "line", "series": [{"values": [7]}]},
    {"kind": "chart", "chart": "line", "series": [{"values": [0, 0, 0]}]},
    {"kind": "chart", "chart": "line", "series": [{"values": [0.0001, 0.0002, 0.00015]}]},
    {"kind": "chart", "chart": "line", "series": [{"values": [1e308, -1e308]}]},
    {"kind": "chart", "chart": "line", "series": [{"name": "in", "values": list(range(400))}]},
    {"kind": "chart", "chart": "spark", "labels": [CJK],
     "series": [{"name": CJK * 4, "values": [1]}, {"name": LONG, "values": [-1, -2, -1e9]}], "unit": "x"},
    {"kind": "chart", "chart": "spark", "series": [{"values": [1e308, -1e308]}]},
    {"kind": "diagram", "nodes": [{"id": "a", "label": CJK * 3}, {"id": "b", "label": EMOJI * 5},
                                  {"id": "c", "label": LONG}], "edges": [["a", "b", CJK], ["b", "c", LONG[:40]], ["a", "c"]]},
    {"kind": "diagram", "direction": "TB", "nodes": [{"id": "a", "label": CJK * 3}, {"id": "b", "label": EMOJI * 5},
                                                     {"id": "c", "label": LONG}], "edges": [["a", "b"], ["b", "c"]]},
    {"kind": "diagram", "nodes": ["n%d" % i for i in range(40)], "edges": [["n%d" % i, "n%d" % (i + 1)] for i in range(39)]},
    {"kind": "table", "title": LONG, "columns": [CJK, EMOJI, LONG[:50]] + ["c%d" % i for i in range(9)],
     "rows": [[CJK * 5, EMOJI * 9, "-12.5"] + ["x" * 30] * 9] * 3},
    {"kind": "table", "columns": ["Model", "Notes"], "rows": [["kimi", "x" * 80]] * 60},
    {"kind": "code", "code": {"lang": "py", "filename": CJK + LONG, "highlight": [2],
                              "text": "x = '" + CJK * 20 + EMOJI * 10 + "'\n\tif True:\n" + "y" * 300}},
    {"kind": "code", "code": {"lang": "go", "text": "\n".join("line %d // c" % i for i in range(80))}},
    {"kind": "metrics", "metrics": [{"label": CJK * 4, "value": "1,234,567,890.12345", "delta": "+" + CJK, "trend": [1]},
                                    {"label": LONG, "value": EMOJI * 4, "delta": "-100%", "trend": []},
                                    {"label": "z", "value": "", "trend": [0, 0]}] * 4},
    {"kind": "metrics", "metrics": [{"label": "Req", "value": "1,284", "delta": "+12%", "trend": [1, 2, 3]}]},
    {"kind": "timeline", "title": EMOJI, "events": [{"when": CJK * 4, "title": LONG, "detail": CJK * 50,
                                                     "status": "failed"}] * 8},
    {"kind": "progress", "steps": [{"title": CJK * 20 + EMOJI * 10, "detail": LONG, "status": "active"}] * 12},
    {"kind": "card", "tone": "error", "title": CJK * 10, "body": CJK * 40 + "\n\n" + EMOJI * 30 + "\n\n- " + LONG},
    {"kind": "markdown", "body": "# " + CJK * 20 + "\n\n`" + "z" * 200 + "`\n\n> " + LONG + "\n\n1. " + EMOJI * 50},
]

WIDTHS = (60, 80, 100, 160)
HEIGHTS = (20, 30, 50)
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _emptied(scene, field):
    out = normalize(scene)
    out[field] = []
    return out


def scenes():
    # normalize() refuses a scene with nothing in it, but an older build may have stored one: the renderers
    # still draw an empty table, timeline and progress without breaking a row.
    return [normalize(s) for s in SCENES] + [
        _emptied({"kind": "table", "columns": ["only"], "rows": [["x"]]}, "rows"),
        _emptied({"kind": "timeline", "events": ["x"]}, "events"),
        _emptied({"kind": "progress", "steps": ["x"]}, "steps")]


def frames(sc):
    return list(range(len(sc["slides"]))) if sc["kind"] == "deck" else [0]


def ref_width(s):
    """Display width from an independent table (wcwidth when installed, else unicodedata)."""
    if _wcw is not None:
        return _wcw.wcswidth(s)
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def row_problems(cv):
    """Every row must be exactly the canvas width, with each wide glyph followed by its placeholder."""
    out = []
    for y, row in enumerate(cv.cells):
        for x, (ch, _fg, _b) in enumerate(row):
            if ch == "":
                if x == 0 or R.cwidth(row[x - 1][0][:1] or " ") != 2:
                    out.append("orphan placeholder at %d,%d" % (x, y))
                continue
            assert len(ch) == 1, (ch, x, y)
            cw = R.cwidth(ch)
            if cw == 0:
                out.append("zero-width cell %r at %d,%d" % (ch, x, y))
            if cw == 2 and (x + 1 >= len(row) or row[x + 1][0] != ""):
                out.append("wide %r at %d,%d without placeholder" % (ch, x, y))
        line = "".join(c[0] for c in row)
        if R.swidth(line) != cv.w:
            out.append("row %d is %d cells, not %d" % (y, R.swidth(line), cv.w))
        rw = ref_width(line)
        if rw != cv.w:
            out.append("row %d measures %d by the reference table, not %d" % (y, rw, cv.w))
    return out


@pytest.mark.parametrize("w", WIDTHS)
@pytest.mark.parametrize("u", [True, False])
def test_every_kind_fits_every_size(w, u):
    for sc in scenes():
        for fr in frames(sc):
            for h in HEIGHTS:
                cv = fx.Canvas(w, h)
                R.draw_scene(cv, 0, 0, w, h, sc, slide=fr, u=u)
                probs = row_problems(cv)
                assert not probs, (sc["kind"], sc.get("chart"), fr, w, h, u, probs[:3])
                if not u:
                    assert all(c[0].isascii() for row in cv.cells for c in row), (sc["kind"], fr, w, h)


@pytest.mark.parametrize("w", WIDTHS)
@pytest.mark.parametrize("color", [24, 8, 0])
@pytest.mark.parametrize("u", [True, False])
def test_plain_text_rendering_fits_the_width(w, color, u):
    for sc in scenes():
        txt = T.scene_text(sc, w, color, u)
        for ln in txt.split("\n"):
            plain = ANSI.sub("", ln)
            assert "\x1b" not in plain
            assert ref_width(plain) <= w, (sc["kind"], sc.get("chart"), w, color, u, plain[:60])
            if not u:
                assert ln.isascii()
        if not color:
            assert "\x1b" not in txt


def text_of(cv):
    return ["".join(c[0] for c in row) for row in cv.cells]


def render(scene, w, h, u=True):
    cv = fx.Canvas(w, h)
    R.draw_scene(cv, 0, 0, w, h, normalize(scene), u=u)
    return text_of(cv)


@pytest.mark.parametrize("values", [
    [float("nan"), 1], [float("inf"), -float("inf"), 2], [float("nan")], [1e308, -1e308], [5e-324, 0],
    [1e-300, 2e-300], [1e15, 1e15 + 1], [-0.0, 0.0], [], ["x", None, 3],
])
@pytest.mark.parametrize("chart", ["bar", "line", "spark"])
def test_charts_survive_awkward_numbers_drawn_directly(chart, values):
    sc = {"chart": chart, "labels": ["a"], "series": [{"name": "s", "values": values}], "unit": "$"}
    for w, h in ((80, 20), (60, 30), (12, 4), (7, 3)):
        for u in (True, False):
            cv = fx.Canvas(w, h)
            R.draw_chart(cv, 0, 0, w, h, sc, u=u)
            assert not row_problems(cv)
    R.sparkline(values, 20)
    cv = fx.Canvas(60, 20)
    R.draw_metrics(cv, 0, 0, 60, 20, [{"label": "a", "value": "1", "trend": values}])


def test_number_formatting_stays_short_and_finite():
    assert R.fmt_num(float("nan")) == "n/a" and R.fmt_num(float("inf"), "ms") == "n/a"
    assert len(R.fmt_num(1e300, "$")) <= 10 and len(R.fmt_num(-1e300)) <= 10
    assert R.finite([float("nan"), float("inf"), 1e308, 5e-324, "2", None]) == [R.VALUE_CAP, 0.0, 2.0]
    lo, hi, step = R.nice_ticks(0.0, 5e-324)
    assert hi > lo and step > 0


def test_tallest_vertical_bar_keeps_its_value_label():
    rows = render({"kind": "chart", "chart": "bar", "labels": ["NVIDIA", "OpenRouter", "Groq"],
                   "series": [{"values": [12, 30, 7]}]}, 80, 20)
    body = "\n".join(rows)
    for lab in ("12", "30", "7"):
        assert re.search(r"(?<![\d.])%s(?![\d.])" % lab, body), lab
    rows = render({"kind": "chart", "chart": "bar", "labels": list("abcd"),
                   "series": [{"values": [-5, -1, -3, -2]}]}, 80, 20)
    assert "-5" in "\n".join(rows)


def test_one_row_metric_tiles_still_show_their_values():
    ms = [{"label": "Requests", "value": "1,284", "delta": "+12%"}, {"label": "TTFT", "value": "412ms"},
          {"label": "Cost", "value": "$1.92", "delta": "-3%"}] * 4
    for u in (True, False):
        body = "\n".join(render({"kind": "metrics", "metrics": ms}, 60, 20, u))
        for val in ("1,284", "412ms", "$1.92"):
            assert val in body, (u, val)


def test_diagram_list_overflow_note_has_its_own_row():
    sc = {"kind": "diagram", "nodes": ["n%d" % i for i in range(40)],
          "edges": [["n%d" % i, "n%d" % (i + 1)] for i in range(39)]}
    for u in (True, False):
        rows = [r.strip() for r in render(sc, 60, 20, u) if r.strip()]
        note = [r for r in rows if "more" in r]
        assert len(note) == 1 and "n" not in note[0].replace("more", ""), note
        shown = sum(1 for r in rows if ((chr(0x2500) * 2 + chr(0x25B6)) if u else "-->") in r)
        assert shown + int(re.search(r"(\d+) more", note[0]).group(1)) == 39


def test_cjk_and_emoji_labels_are_never_cut_mid_glyph():
    for w in WIDTHS:
        for n in range(1, 12):
            s = R.clip(CJK * 4 + EMOJI * 4, n)
            assert R.swidth(s) <= n
            assert ref_width(s) == R.swidth(s)
            assert all(ord(c) < 0xD800 or ord(c) > 0xDFFF for c in s)
        for ln in R.wrap(CJK * 30 + " " + EMOJI * 30 + " " + LONG, w // 3):
            assert R.swidth(ln) <= w // 3


SINGLE_LINE = [
    ({"kind": "table", "columns": ["a\nINJECT"], "rows": [["x\nINJECT", "z"]]},
     lambda s: s["columns"] + s["rows"][0][:1]),
    ({"kind": "metrics", "metrics": [{"label": "l\nINJECT", "value": "1\nINJECT", "delta": "+\nINJECT"}]},
     lambda s: [s["metrics"][0][k] for k in ("label", "value", "delta")]),
    ({"kind": "code", "code": {"text": "x", "filename": "a\nINJECT"}}, lambda s: [s["code"]["filename"]]),
    ({"kind": "chart", "labels": ["a\nINJECT"], "series": [{"name": "s\nINJECT", "values": [1]}], "unit": "u\nINJECT"},
     lambda s: s["labels"] + [s["series"][0]["name"], s["unit"]]),
    ({"kind": "progress", "steps": [{"title": "t\nINJECT"}]}, lambda s: [s["steps"][0]["title"]]),
    ({"kind": "timeline", "events": [{"when": "w\nINJECT", "title": "t\nINJECT"}]},
     lambda s: [s["events"][0]["when"], s["events"][0]["title"]]),
    ({"kind": "diagram", "nodes": [{"id": "n\nINJECT", "label": "l\nINJECT"}], "edges": [["n\nINJECT", "m\nINJECT"]]},
     lambda s: [s["nodes"][0]["id"], s["nodes"][0]["label"], s["edges"][0]["from"], s["edges"][0]["to"]]),
]


@pytest.mark.parametrize("raw,fields", SINGLE_LINE)
def test_single_line_fields_never_carry_a_line_break(raw, fields):
    sc = normalize(raw)
    for f in fields(sc):
        assert "\n" not in f and "INJECT" in f, f
    for w, h in ((40, 12), (100, 30)):
        cv = fx.Canvas(w, h)
        R.draw_scene(cv, 0, 0, w, h, sc)
        assert not [c for row in cv.cells for c in row if not c[0].isprintable()]


def test_control_characters_never_reach_the_terminal_from_a_cell():
    import io
    cv = fx.Canvas(12, 2)
    end = R.View(cv).text(0, 0, "a\nb\x07c\x7fd")
    # each control takes a space (the width the caller was promised), and none reaches a cell
    assert "".join(c[0] for c in cv.cells[0][:end]) == "a b c d"
    cv.cells[1][0] = ("\n", None, False)                     # put there directly, past every other guard
    cv.cells[1][1] = ("x\x1b]52;c;QQ==\x07", None, False)
    out = io.StringIO()
    fx.Screen(out, 0).present(cv)
    painted = out.getvalue()
    assert "\n" not in painted and "\x07" not in painted and "]52;" not in painted
