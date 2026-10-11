# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""End-to-end regression tests for Pulsar Stage, for bugs seen inside the real Claude Code (one test or more per bug)."""
import io
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from pulsar.core import state as ST
from pulsar.core.server import make_app
from pulsar import fx
from pulsar.fx import Canvas
from pulsar.stage import render as R
from pulsar.stage import schema as SCH
from pulsar.stage import term as T
from pulsar.stage import web as W
from pulsar.stage.export import export_html

ROOT = Path(__file__).resolve().parent.parent
ADMIN = ST.ADMIN_TOKEN_PREFIX + "admin-test-not-real"


# -- 1. malformed list entries are a SceneError (Core 400), never an AttributeError (Core 500) -------------

MALFORMED = [
    {"kind": "diagram", "title": "x", "nodes": [5]},
    {"kind": "diagram", "nodes": [None]},
    {"kind": "diagram", "nodes": ["a"], "edges": ["ab"]},
    {"kind": "diagram", "nodes": ["a"], "edges": [7]},
    {"kind": "diagram", "nodes": ["a"], "edges": [["a"]]},
    {"kind": "timeline", "events": [7]},
    {"kind": "timeline", "events": [["a", "b"]]},
    {"kind": "progress", "steps": [None, 5]},
    {"kind": "progress", "steps": [True]},
    {"kind": "deck", "slides": [{"title": "s", "embed": {"kind": "diagram", "nodes": [5]}}]},
]


@pytest.mark.parametrize("scene", MALFORMED)
def test_malformed_entries_are_scene_errors(scene):
    with pytest.raises(SCH.SceneError) as info:
        SCH.normalize(scene)
    msg = str(info.value)
    assert re.search(r"(nodes|edges|events|steps) (given as a list )?(must|need)", msg), msg
    assert "object has no attribute" not in msg


def test_well_formed_shorthands_still_pass():
    sc = SCH.normalize({"kind": "diagram", "nodes": ["a", {"id": "b"}], "edges": [["a", "b"], ("b", "c", "x"),
                                                                             {"from": "c", "to": "a"}]})
    assert [n["id"] for n in sc["nodes"]] == ["a", "b", "c"] and len(sc["edges"]) == 3
    assert SCH.normalize({"kind": "timeline", "events": ["x", {"title": "y"}]})["events"][1]["title"] == "y"
    assert SCH.normalize({"kind": "progress", "steps": ["x", {"title": "y", "status": "done"}]})["steps"][1]["status"] == "done"


def test_core_answers_400_for_malformed_entries(pulsar_home):
    c = TestClient(make_app(ADMIN, 0, transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    h = {"authorization": "Bearer " + ADMIN}
    for scene in MALFORMED:
        r = c.post("/pulsar/stage", json={"scene": scene, "sid": "e2e"}, headers=h)
        assert r.status_code == 400, (scene, r.status_code)
        assert re.search(r"(nodes|edges|events|steps) (given as a list )?(must|need)", r.json()["error"]["message"])


# -- 2. U+2028 / U+2029 / U+0085 never reach the terminal --------------------------------------------------

LS, PS, NEL = chr(0x2028), chr(0x2029), chr(0x85)


def test_clean_maps_line_separators_to_newlines():
    assert SCH.clean("a" + LS + "b" + PS + "c" + NEL + "d") == "a\nb\nc\nd"
    sc = SCH.normalize({"kind": "deck", "title": "Q3" + LS * 4 + "plan" + PS * 4 + "review",
                        "slides": [{"title": "one" + LS + "two", "body": "a" + LS + "b"}]})
    assert sc["title"] == "Q3 plan review"
    assert sc["slides"][0]["title"] == "one two" and sc["slides"][0]["body"] == "a\nb"


def test_cwidth_is_zero_where_terminals_disagree():
    for o in (0x2028, 0x2029, 0xE000, 0xF8FF, 0x0378, 0x1F3FB, 0x1F3FD, 0x1F3FF, 0x85):
        assert R.cwidth(chr(o)) == 0, hex(o)
    assert R.cwidth("a") == 1 and R.cwidth(chr(0x754C)) == 2 and R.cwidth(chr(0x1F44D)) == 2


def test_disputed_code_points_never_reach_a_cell():
    cv = Canvas(40, 2)
    v = R.View(cv, 0, 0, 40, 2)
    end = v.text(0, 0, "Q3" + LS + "plan" + PS + chr(0xE000) + chr(0x1F44D) + chr(0x1F3FD) + "x")
    cells = "".join(ch for ch, _, _ in cv.cells[0])
    for o in (0x2028, 0x2029, 0xE000, 0x1F3FD):
        assert chr(o) not in cells
    assert cells.startswith("Q3plan" + chr(0x1F44D)) and end == len("Q3plan") + 2 + 1


def test_scene_header_keeps_the_whole_title_after_a_line_separator():
    sc = SCH.normalize({"kind": "card", "title": "Q3" + LS * 4 + "plan" + PS * 4 + "review", "body": "a" + LS + "b"})
    text = T.scene_text(sc, width=100, color=0)
    assert "Q3 plan review" in text and LS not in text and PS not in text


# -- 3. long unbroken tokens wrap in the web view and the export --------------------------------------------

def _css_rules(html):
    css = re.sub(r"/\*.*?\*/", "", html.split("<style", 1)[1].split("</style>", 1)[0], flags=re.S)
    return [(sel.strip(), body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]


@pytest.mark.parametrize("cls", [".h-title", ".h-sub", ".md", ".md li", "tbody td", "thead th", ".legend span", ".qtext",
                                 ".code .fname", ".timeline .when"])
def test_web_and_export_break_long_tokens(cls):
    for html in (W.page(), export_html([{"kind": "markdown", "body": "x"}])):
        rules = [body for sel, body in _css_rules(html) if cls in [s.strip() for s in sel.split(",")]]
        # break-word breaks an overlong token at the box edge without lowering min-content (round 2, mid-word bug)
        assert any("overflow-wrap:break-word" in b and "min-width:0" in b for b in rules), cls


# -- 4. extreme finite chart values are clamped where every renderer can still subtract them -----------------

def test_chart_and_trend_values_are_clamped():
    sc = SCH.normalize({"kind": "chart", "series": [{"values": [1e308, -1e308, 0, 5e-324, -5e-324, 3]}]})
    vals = sc["series"][0]["values"]
    assert vals == [SCH.MAX_VALUE, -SCH.MAX_VALUE, 0, 0, 0, 3]
    assert vals[0] - vals[1] != float("inf")
    met = SCH.normalize({"kind": "metrics", "metrics": [{"label": "a", "value": "1", "trend": [1e308, -1e308]}]})
    t = met["metrics"][0]["trend"]
    assert t == [SCH.MAX_VALUE, -SCH.MAX_VALUE] and abs(t[0] - t[1]) < float("inf")


def test_web_guards_spans_with_isfinite():
    js = W.page()
    nice = js.split("function niceTicks", 1)[1].split("\nfunction ", 1)[0]
    assert "isFinite(lo)" in nice and "hi / 2 - lo / 2" in nice
    spark = js.split("function sparkSvg", 1)[1].split("\nfunction ", 1)[0]
    assert "isFinite(span)" in spark


# -- 5. combining-mark floods are capped ------------------------------------------------------------------

def test_combining_mark_runs_are_capped():
    zalgo = "a" + chr(0x0E47) * 30 + "b" + chr(0x301) * 9
    out = SCH.clean(zalgo)
    assert out == "a" + chr(0x0E47) * SCH.MAX_MARKS + "b" + chr(0x301) * SCH.MAX_MARKS
    thai = chr(0x0E01) + chr(0x0E34) + chr(0x0E48)        # base + vowel + tone mark: real text is untouched
    assert SCH.clean(thai) == thai
    sc = SCH.normalize({"kind": "card", "title": "t" + chr(0x0E47) * 30, "body": "x"})
    assert sc["title"].count(chr(0x0E47)) == SCH.MAX_MARKS


# -- 6. the export toast keeps the file name ----------------------------------------------------------------

def test_elide_path_keeps_the_file_name():
    p = "/tmp/pulsar-export/" + "d" * 40 + "/stage/stage-20261003-120000.html"
    out = T.elide_path(p, 60)
    assert R.swidth(out) <= 60 and out.endswith("/stage-20261003-120000.html") and out.startswith("/tmp/")
    assert "…" in out
    assert T.elide_path("~/x/a.html", 60) == "~/x/a.html"
    tiny = T.elide_path(p, 12)
    assert R.swidth(tiny) <= 12 and tiny.endswith("0000.html")
    assert "~" in T.elide_path(p, 40, u=False)


def test_export_toast_shows_the_file_name(monkeypatch, tmp_path):
    long_dir = tmp_path / ("d" * 70) / ("e" * 70)
    long_dir.mkdir(parents=True)
    target = long_dir / "stage-20261003-120000.html"
    import pulsar.stage.export as EX
    monkeypatch.setattr(EX, "write_export", lambda items: target)
    app = T.Viewer.__new__(T.Viewer)
    app.items = [{"scene": {"kind": "card", "body": "x"}}]
    app.u = True
    app.toasts = []

    class _H:
        def confirm(self):
            pass

        def error(self):
            pass

    app.haptics = _H()
    monkeypatch.setattr(T.Viewer, "_term_size", lambda self: (120, 36))
    app._do_export()
    text = app.toasts[-1][0]
    assert text.startswith("exported ") and text.endswith("stage-20261003-120000.html")
    assert R.swidth(text) <= 110


# -- 7. bars start at the zero gridline, which sits on the axis ---------------------------------------------

def _rows(cv):
    return ["".join(ch or "" for ch, _, _ in row) for row in cv.cells]


@pytest.mark.parametrize("h", [12, 16, 24])
def test_bar_chart_zero_line_is_the_axis(h):
    cv = Canvas(60, h)
    R.draw_bar_chart(cv, 0, 0, 60, h, ["a", "b", "c"], [{"name": "", "values": [3, 7, 5]}], orient="vertical")
    rows = _rows(cv)
    axis = max(i for i, r in enumerate(rows) if "╰" in r)
    zero = [i for i, r in enumerate(rows) if re.match(r"^\s*0 ?[┤╰┼]", r)]
    assert zero == [axis], (zero, axis, rows)
    for r in rows[axis:]:
        assert "█" not in r and not any(ch in r for ch in R.SPARK_U)


def test_bar_chart_negative_and_mixed_baseline():
    cv = Canvas(60, 18)
    R.draw_bar_chart(cv, 0, 0, 60, 18, ["a", "b", "c"], [{"name": "", "values": [4, -4, 2]}], orient="vertical")
    rows = _rows(cv)
    zero = [i for i, r in enumerate(rows) if re.match(r"^\s*0 ?[┤╰┼]", r)]
    assert len(zero) == 1
    zr = zero[0]
    assert "─" in rows[zr] and "█" not in rows[zr]
    assert any("█" in r for r in rows[:zr]) and any("█" in r for r in rows[zr + 1:])


# -- 8. an unfocused Stage pane says how to focus it -----------------------------------------------------------

STAGE_TSX = ROOT / "pulsar" / "fnplug" / "hooks" / "stage.tsx"

FOCUS_TEST = """

test('an unfocused pane says how to focus it instead of advertising hotkeys', async ($, on) => {
  const w = world(on, { PULSAR_CORE: CORE, PULSAR_SESSION_TOKEN: TOKEN })
  await $.session.start(START)
  await w.clock.advance(700)
  const mount = (isFocused: boolean) =>
    $.ui.mount({
      plugin: 'pulsar-stage',
      surface: 'terminal',
      component: 'Pane',
      requestId: PANE,
      props: { title: 'stage', isFocused, bodyColumns: 80, placement: 'dock', scroll: { offset: 0, bodyRows: 12 }, view: {} },
      viewport: { columns: 160, rows: 40, isFullscreen: true },
    })
  const idle = await mount(false)
  expect(JSON.stringify(await idle.drawn())).toContain('ctrl+x tab to control')
  expect(await idle.find({ type: 'Button' })).toBeUndefined()
  expect(await idle.find({ type: 'Raster' })).toBeDefined()
  await idle.unmount()
  const live = await mount(true)
  expect(JSON.stringify(await live.drawn())).not.toContain('ctrl+x tab to control')
  expect(await live.find({ type: 'Button' })).toBeDefined()
})
"""


def test_pane_source_gates_hotkeys_on_focus():
    src = STAGE_TSX.read_text("utf-8")
    assert "ctrl+x tab to control" in src and "e.props.isFocused" in src
    for key in ("prev", "next", "replay", "close"):
        assert 'key="%s"' % key in src
    assert "focused && scene && total > 1" in src and "focused && scene ?" in src


def test_pane_unfocused_hint_in_the_engine(tmp_path):
    claude = shutil.which("claude")
    if not claude:
        pytest.skip("claude is not installed")
    fn = tmp_path / "fn"
    shutil.copytree(ROOT / "pulsar" / "fnplug", fn, ignore=shutil.ignore_patterns("node_modules"))
    test_file = fn / "tests" / "stage.test.tsx"
    test_file.write_text(test_file.read_text("utf-8") + FOCUS_TEST, "utf-8")
    # The real HOME with a scratch Claude Code profile: a made-up HOME can make the system credential
    # store ask the person at the screen for a password, and CLAUDE_CONFIG_DIR already keeps the run
    # away from their own Claude Code configuration.
    profile = tmp_path / "claude_config"
    profile.mkdir()
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "CLAUDE_CONFIG_DIR": str(profile)}
    for var in ("HOME", "USERPROFILE", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP"):
        if os.environ.get(var):
            env[var] = os.environ[var]
    try:
        p = subprocess.run([claude, "plugin", "test", str(fn)], cwd=str(tmp_path), env=env,
                           capture_output=True, text=True, timeout=240)
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.skip("claude plugin test did not run: %s" % type(exc).__name__)
    out = p.stdout + p.stderr
    if "an unfocused pane" not in out:
        pytest.skip("this claude build has no plugin test runner")
    assert "(pass) an unfocused pane says how to focus it" in out, out[-2000:]


# ===========================================================================================================
# Round 2 regressions
# ===========================================================================================================

# -- R2.1 U+2028 / U+2029 / U+0085 in any one-line field never reach a terminal cell as a line break ---------

def _sep_scenes():
    s = lambda a, b, sep=LS: a + sep + b  # noqa: E731
    return [
        {"kind": "table", "columns": [s("re", "gion"), "owner"],
         "rows": [[s("EMEA", "north"), s("bob", "smith", NEL)], {"owner": s("on", "track", PS)}]},
        {"kind": "chart", "labels": [s("Q1", "2026")], "series": [{"name": s("rev", "enue", PS), "values": [3]}],
         "unit": s("m", "s")},
        {"kind": "metrics", "metrics": [{"label": s("p95", "latency"), "value": s("120", "ms", PS),
                                         "delta": s("-3", "%", NEL)}]},
        {"kind": "diagram", "nodes": [{"id": s("api", "gw"), "label": s("API", "gateway")}],
         "edges": [[s("api", "gw"), "db", s("reads", "writes", PS)]]},
        {"kind": "timeline", "events": [{"when": s("Mon", "9am"), "title": s("kick", "off"), "detail": "a\nb"}]},
        {"kind": "progress", "steps": [{"title": s("build", "image"), "detail": s("4m", "12s", NEL)}]},
        {"kind": "code", "code": {"text": "x = 1\ny = 2", "filename": s("a", "b.py"), "lang": s("py", "thon")}},
        {"kind": "deck", "slides": [{"title": "t", "stat": {"value": s("42", "ms"), "label": s("p", "50")},
                                     "bullets": [s("one", "two"), "three"]}]},
    ]


def _one_line_fields(sc):
    """Every field a viewer draws on one row (multi-line bodies, notes, code text and timeline details excluded)."""
    out = [sc.get("id"), sc.get("title"), sc.get("subtitle"), sc.get("unit")]
    out += sc.get("columns") or []
    for r in sc.get("rows") or []:
        out += r
    out += sc.get("labels") or []
    out += [x.get("name") for x in sc.get("series") or []]
    for m in sc.get("metrics") or []:
        out += [m["label"], m["value"], m["delta"]]
    for n in sc.get("nodes") or []:
        out += [n["id"], n["label"]]
    for e in sc.get("edges") or []:
        out += [e["from"], e["to"], e["label"]]
    for e in sc.get("events") or []:
        out += [e["when"], e["title"]]
    for st in sc.get("steps") or []:
        out += [st["title"], st["detail"]]
    if sc.get("code"):
        out += [sc["code"]["filename"], sc["code"]["lang"]]
    for sl in sc.get("slides") or []:
        out += [sl["title"], sl["subtitle"]]
        if sl.get("stat"):
            out += [sl["stat"]["value"], sl["stat"]["label"]]
        out += [ln for ln in sl["body"].split("\n")]
    return [x for x in out if x is not None]


@pytest.mark.parametrize("raw", _sep_scenes(), ids=lambda r: r["kind"])
def test_one_line_fields_never_keep_a_line_break(raw):
    sc = SCH.normalize(raw)
    fields = _one_line_fields(sc)
    assert fields
    for f in fields:
        assert "\n" not in f and LS not in f and PS not in f and NEL not in f, repr(f)
    if raw["kind"] == "table":
        assert sc["columns"][0] == "re gion" and sc["rows"][0] == ["EMEA north", "bob smith"]
        assert sc["rows"][1] == ["", "on track"]
    if raw["kind"] == "deck":
        assert sc["slides"][0]["body"] == "- one two\n- three"
    if raw["kind"] == "timeline":
        assert sc["events"][0]["detail"] == "a\nb"            # a detail is wrapped text: its line break stays


def _present(cv):
    buf = io.StringIO()
    fx.Screen(out=buf, depth=0).present(cv)
    return buf.getvalue()


def test_table_cell_with_a_line_separator_sends_no_newline_to_the_tty():
    sc = SCH.normalize({"kind": "table", "columns": ["region", "status"],
                        "rows": [["a" + LS + "b", "on" + PS + "track"], ["EMEA" + NEL + "north", "late"]]})
    cv = Canvas(60, 8)
    R.draw_table(cv, 0, 0, 60, 8, sc["columns"], sc["rows"])
    frame = _present(cv)
    assert "\n" not in frame and "\r" not in frame
    rows = _rows(cv)
    assert any("a b" in r for r in rows) and any("on track" in r for r in rows) and any("EMEA north" in r for r in rows)
    # the left border of every body row is intact (a raw LF used to overprint it)
    assert all(r.lstrip().startswith(("│", "╭", "┝", "╰")) for r in rows if r.strip())


def test_view_text_never_writes_a_control_character_to_a_cell():
    # backstop for text that never went through the schema: the ASCII fast path writes a space instead
    cv = Canvas(20, 2)
    v = R.View(cv, 0, 0, 20, 2)
    end = v.text(0, 0, "a\nb\tc\x7fd\x1bx")
    assert _rows(cv)[0].startswith("a b c d x") and end == 9
    assert all(ord(ch) >= 0x20 and ch != "\x7f" for row in cv.cells for ch, _, _ in row if ch)
    cv2 = Canvas(40, 6)
    R.draw_table(cv2, 0, 0, 40, 6, ["k", "v"], [["one\ntwo", "x\ry"]])
    frame = _present(cv2)
    assert "\n" not in frame and "\r" not in frame and "one two" in "".join(_rows(cv2))


def test_terminal_scene_text_keeps_separated_fields_on_their_row():
    for raw in _sep_scenes():
        sc = SCH.normalize(raw)
        text = T.scene_text(sc, width=100, color=0)
        assert LS not in text and PS not in text and NEL not in text
    sc = SCH.normalize(_sep_scenes()[0])
    text = T.scene_text(sc, width=100, color=0)
    assert any("EMEA north" in line and "bob smith" in line for line in text.split("\n"))


# -- R2.2 the combining-mark cap keeps real scripts intact ----------------------------------------------------

REAL_SCRIPTS = [
    "שִּׁיר הַשִּׁירִים",  # shir hashirim
    "བསྒྲུབས",                    # Tibetan bsgrubs: 3 marks on one base
    "རྒྱུ",                                    # Tibetan rgyu
    "ཧཱུྃ",                                    # Tibetan hum: 3 marks
    "בְּֽ֥רֵאשִ֑ית",  # cantillated Hebrew
    "שָּֽׁ֖מַיִם",            # shin + dagesh, dot, qamats, meteg, accent
    "Việt Nam",                                        # Vietnamese, decomposed
    "กิ่",                                          # Thai: base + vowel + tone
    "1️⃣ ❤️",                                  # keycap, emoji presentation
]


@pytest.mark.parametrize("text", REAL_SCRIPTS)
def test_real_scripts_survive_normalization(text):
    assert SCH.clean(text) == text
    sc = SCH.normalize({"kind": "markdown", "title": text, "body": text})
    assert sc["title"] == text and sc["body"] == text
    cell = SCH.normalize({"kind": "table", "rows": [[text]]})["rows"][0][0]
    assert cell == text


def test_tibetan_and_hebrew_survive_core(pulsar_home):
    heb, tib = REAL_SCRIPTS[0], REAL_SCRIPTS[1]
    c = TestClient(make_app(ADMIN, 0, transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    h = {"authorization": "Bearer " + ADMIN}
    r = c.post("/pulsar/stage", json={"scene": {"kind": "markdown", "id": "s", "body": heb + "\n\n" + tib},
                                      "sid": "e2e"}, headers=h)
    assert r.status_code == 200
    got = c.get("/pulsar/stage/scenes?sid=e2e", headers=h).json()["scenes"]
    body = [it["scene"]["body"] for it in got if it["scene"]["id"] == "s"][0]
    assert body == heb + "\n\n" + tib


# -- R2.3 an integer too large for a float is a SceneError (Core 400), not an OverflowError (Core 500) ---------

BIG = 10 ** 400
BIG_SCENES = [
    {"kind": "chart", "values": [BIG]},
    {"kind": "chart", "series": [{"name": "a", "values": [1, -BIG]}]},
    {"kind": "metrics", "metrics": [{"label": "a", "value": "1", "trend": [BIG]}]},
    {"kind": "code", "code": {"text": "x", "highlight": [BIG]}},
    {"kind": "deck", "slides": [{"title": "t", "embed": {"kind": "chart", "values": [1, BIG]}}]},
]


@pytest.mark.parametrize("scene", BIG_SCENES, ids=lambda s: s["kind"])
def test_huge_integers_are_scene_errors(scene):
    with pytest.raises(SCH.SceneError) as info:
        SCH.normalize(scene)
    msg = str(info.value)
    assert msg.startswith("numbers must be finite") and len(msg) < 120, msg


def test_huge_integer_messages_stay_short():
    with pytest.raises(SCH.SceneError) as info:
        SCH.normalize({"kind": "diagram", "nodes": [BIG]})
    assert "an integer of about 401 digits" in str(info.value) and len(str(info.value)) < 140
    with pytest.raises(SCH.SceneError) as info:
        SCH.normalize({"kind": "chart", "values": ["x" * 5000]})
    assert len(str(info.value)) < 80


def test_core_answers_400_for_huge_integers(pulsar_home):
    c = TestClient(make_app(ADMIN, 0, transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    h = {"authorization": "Bearer " + ADMIN, "content-type": "application/json"}
    lit = "1" + "0" * 400
    for scene in ('{"kind":"chart","values":[%s]}' % lit,
                  '{"kind":"metrics","metrics":[{"label":"a","value":"1","trend":[%s]}]}' % lit,
                  '{"kind":"code","code":{"text":"x","highlight":[%s]}}' % lit,
                  '{"kind":"deck","slides":[{"title":"t","embed":{"kind":"chart","values":[1,%s]}}]}' % lit):
        r = c.post("/pulsar/stage", content=('{"sid":"e2e","scene":%s}' % scene).encode(), headers=h)
        assert r.status_code == 400, (scene[:40], r.status_code)
        assert "numbers must be finite" in r.json()["error"]["message"]


def test_export_drops_a_scene_with_a_huge_integer():
    items = export_html([{"kind": "chart", "values": [BIG]}, {"kind": "card", "body": "ok"}])
    m = re.search(r'<script type="application/json" id="pulsar-data">(.*?)</script>', items, re.S)
    assert [it["scene"]["kind"] for it in json.loads(m.group(1))["items"]] == ["card"]


# -- R2.4 invisible format characters do not restart the mark count; the web view bounds what text paints ----

@pytest.mark.parametrize("sep", [chr(0xAD), chr(0x1D173), chr(0xE0020), chr(0x180E), chr(0x61C), chr(0xFFF9), ""],
                         ids=["shy", "musical", "tag", "mvs", "alm", "interlinear", "plain"])
def test_format_characters_do_not_restart_the_mark_count(sep):
    z = "Z" + (chr(0x0E47) + chr(0x0E49) + sep) * 20 + "ap"
    out = SCH.clean(z)
    head = out[: out.index("ap")]
    assert sum(1 for ch in head if ch in (chr(0x0E47), chr(0x0E49))) == SCH.MAX_MARKS
    assert out.endswith("ap") and out.startswith("Z")


def test_mark_cap_still_cuts_a_flood():
    out = SCH.clean("a" + chr(0x0E47) * 30 + "b" + chr(0x301) * 9 + " c" + chr(0x20DD) * 9)
    assert out == "a" + chr(0x0E47) * SCH.MAX_MARKS + "b" + chr(0x301) * SCH.MAX_MARKS + " c" + chr(0x20DD) * SCH.MAX_MARKS
    assert 3 <= SCH.MAX_MARKS <= 6


@pytest.mark.parametrize("cls", [".h-title", ".h-sub", ".md p", ".md li", ".qtext", ".timeline .tt", ".steps .t",
                                 ".tile .val", ".legend", ".stat-l"])
def test_web_text_boxes_clip_what_they_paint(cls):
    for html in (W.page(), export_html([{"kind": "markdown", "body": "x"}])):
        rules = [body for sel, body in _css_rules(html) if cls in [s.strip() for s in sel.split(",")]]
        assert any(re.search(r"clip-path:inset\(-\.\d+em -100vmax\)", b) for b in rules), cls
        assert any("line-height:" in b for sel, b in _css_rules(html) if sel == ".view .stack")


def test_web_marks_stacks_and_long_tokens():
    js = W.page()
    assert "function markText" in js and "var stacks = markText(v);" in js and js.count("fitStacks(stacks);") == 2
    assert "\\\\p{Cf}*" in js and "\\\\p{Mn}" in js and "LONGTOK = /\\S{24,}/" in js
    # combining marks never count toward a token's length (a 5-letter word with 6 marks each is not 'overlong')
    assert 'MARKS = new RegExp("\\\\p{M}", "gu")' in js and "n.data.replace(MARKS" in js


# -- R2.5 long tokens in the code bar, table columns, timeline and diagram stay inside their box --------------

def test_code_bar_and_table_columns_shrink_instead_of_overflowing():
    rules = _css_rules(W.page())
    flat = {sel: body for sel, body in rules}
    # round 3: short cells keep their tokens whole again; free text (.prose) and overlong tokens (.brk) wrap
    assert "white-space:nowrap" in flat["tbody td"] and "tbody td:last-child:not(.num)" not in flat
    assert "white-space:nowrap" in [b for s, b in rules if s.startswith("thead th{") or s == "thead th"][0]
    wrap = [b for s, b in rules if "tbody td.prose" in s and "tbody td.brk" in s and ".timeline .when.brk" in s]
    assert wrap == ["white-space:normal"]
    assert "white-space:nowrap" in flat["td.num,th.num"]
    assert "flex:none" in flat[".code .dots,.code .lang,.code .copy"]
    assert "min-width:0" in flat[".code .fname"]
    assert "fit-content(34%)" in [b for s, b in rules if s == ".timeline li"][0]
    assert "white-space:nowrap" in flat[".timeline .when"]
    assert "fname.title = " in W.page()


def test_diagram_labels_are_measured_and_clipped():
    js = W.page()
    body = js.split("function diagram(sc)", 1)[1].split("\n/* ---------- other kinds", 1)[0]
    assert "fitText(" in body and "clipPath" in body and "maxLw" in body
    assert "Math.max(70, Math.ceil(maxLw) + 24 - 74)" in body


# -- R2.6 only overlong tokens break: no 'anywhere' / 'break-word' word-break on ordinary text -----------------

def test_ordinary_words_are_not_broken_mid_word():
    for html in (W.page(), export_html([{"kind": "markdown", "body": "x"}])):
        rules = _css_rules(html)
        anywhere = [sel for sel, b in rules if "overflow-wrap:anywhere" in b]
        assert ".brk" in anywhere
        for sel in anywhere:   # anywhere only where the box is bounded by something else, never on every text box
            assert sel in (".brk", "#notes,.toast") or "fname.brk" in sel, sel
        assert not any("word-break:break-word" in b for _, b in rules)
        steps = [b for s, b in rules if s == ".steps li"][0]
        assert "30px 1fr fit-content(40%)" in steps          # the detail column is bounded, titles keep priority
        assert [b for s, b in rules if s == ".steps .t,.steps .d"] == ["overflow-wrap:break-word"]


# -- R2.8 web chart axis labels for extreme values read as plain exponents --------------------------------------

def _node():
    return shutil.which("node") or ("/opt/node22/bin/node" if os.path.exists("/opt/node22/bin/node") else "")


def _js_function(src, name):
    start = src.index("function %s(" % name)
    depth, i = 0, src.index("{", start)
    while True:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1


def _fmt_prog(tail):
    js = W.page()
    return "\n".join(["function str(v) { return v === null || v === undefined ? '' : String(v); }",
                      "function clamp(v, a, b) { return v < a ? a : v > b ? b : v; }"]
                     + [_js_function(js, n) for n in ("trim", "trimExp", "expOf", "withUnit", "fmtNum", "decimalsOf",
                                                      "axisFmt", "tickList", "niceTicks")]
                     + ["function axis(lo, hi, unit) { var t = niceTicks(lo, hi, 6), f = axisFmt(t[0], t[1], t[2], unit);"
                        " return tickList(t[0], t[1], t[2]).map(function (v) { return f(v); }); }", tail])


def _run_node(prog):
    node = _node()
    if not node:
        pytest.skip("node is not installed")
    out = subprocess.run([node, "-e", prog], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_fmtnum_formats_extremes_as_exponents():
    got = _run_node(_fmt_prog(
        "console.log(JSON.stringify([fmtNum(1e300), fmtNum(-5e299), fmtNum(1.5e15), fmtNum(2e12),"
        " fmtNum(12345), fmtNum(0), axis(-1e300, 1e300), tickList(0, 1, 0.1).length]));"))
    assert got[:6] == ["1e+300", "-5e+299", "1500T", "2T", "12.3k", "0"]
    assert got[6] == ["-1e+300", "-5e+299", "0", "5e+299", "1e+300"]
    assert got[7] == 11


# -- R3.3 values from 1e15 to 1e21 keep their precision; one axis, one format, no two ticks alike ----------------

AXIS_CASES = [(1.21e15, 1.27e15), (0, 2.47e15), (1.21e18, 1.27e18), (1.21e21, 1.27e21), (-2.5, 7.5), (0, 1),
              (0, 12345), (0, 0.0005), (3, 3), (1e17, 9e17), (-1e300, 1e300), (999, 1001), (0.1, 0.2)]


def test_close_values_from_1e15_up_get_distinct_labels():
    got = _run_node(_fmt_prog(
        "console.log(JSON.stringify([[1.21e15, 1.23e15, 2.41e15, 2.44e15, 2.47e15, 1.21e18, 1.22e18].map(function (v) {"
        " return fmtNum(v); }), %s.map(function (c) { return axis(c[0], c[1], 'B'); })]));" % json.dumps(AXIS_CASES)))
    values, axes = got
    assert values[:5] == ["1210T", "1230T", "2410T", "2440T", "2470T"]
    assert len(set(values)) == len(values), values
    assert axes[0] == ["1200TB", "1220TB", "1240TB", "1260TB", "1280TB"]
    assert axes[1] == ["0B", "500TB", "1000TB", "1500TB", "2000TB", "2500TB"]
    for case, ticks in zip(AXIS_CASES, axes):
        assert len(set(ticks)) == len(ticks) > 1, (case, ticks)                 # adjacent ticks never read the same
        kinds = {("e" if "e" in t else "T" if "T" in t[:-1] else "k" if "k" in t else "n") for t in ticks if t != "0B"}
        assert len(kinds) == 1, (case, ticks)                                    # one format per axis


# -- R2.5 / R2.6 in a real browser (Chromium through Playwright, skipped when it is not installed) ------------

def _chrome():
    try:
        __import__("playwright.sync_api")
    except ImportError:
        return ""
    for cand in [os.environ.get("CHROME", ""), "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"]:
        if cand and os.path.exists(cand):
            return cand
    for p in sorted(Path("/opt/pw-browsers").glob("chromium-*/chrome-linux/chrome")):
        return str(p)
    return ""


@pytest.fixture(scope="module")
def browser():
    chrome = _chrome()
    if not chrome:
        pytest.skip("needs Playwright and Chromium")
    import asyncio
    from playwright.sync_api import sync_playwright
    # A Playwright session that another test started and never stopped (its browser failed to launch) leaves its
    # event loop marked as running on this thread, and the sync API then refuses to start. That loop sits in a
    # suspended greenlet and never runs again: set the mark aside for this module and put it back afterwards.
    stale = asyncio.events._get_running_loop()
    if stale is not None:
        asyncio.events._set_running_loop(None)
    pw = sync_playwright().start()
    try:
        b = pw.chromium.launch(executable_path=chrome, args=["--no-sandbox", "--disable-gpu"])
    except Exception as exc:  # pragma: no cover - a broken browser install is not a Stage regression
        pw.stop()
        asyncio.events._set_running_loop(stale)
        pytest.skip("Chromium did not start: %s" % type(exc).__name__)
    yield b
    b.close()
    pw.stop()
    asyncio.events._set_running_loop(stale)


def _open(browser, tmp_path, scenes, w, h, where="1.1"):
    f = tmp_path / ("s%d.html" % len(list(tmp_path.iterdir())))
    f.write_text(export_html(scenes), "utf-8")
    ctx = browser.new_context(viewport={"width": w, "height": h}, reduced_motion="reduce")
    page = ctx.new_page()
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.goto(f.as_uri() + "#" + where)
    page.wait_for_timeout(300)
    return page, errs


LINES_JS = """sel => { const t = [...document.querySelectorAll(sel)].find(e => e.textContent.startsWith('Canary'));
  const tn = t.firstChild, lines = {};
  for (let i = 0; i < tn.length; i++) { const r = document.createRange(); r.setStart(tn, i); r.setEnd(tn, i + 1);
    const k = Math.round(r.getBoundingClientRect().top); lines[k] = (lines[k] || '') + tn.data[i]; }
  return Object.values(lines).map(s => s.trim()); }"""


def test_browser_progress_titles_wrap_at_spaces_on_a_phone(browser, tmp_path):
    sc = {"kind": "progress", "title": "Release 3.1 rollout", "steps": [
        {"title": "Build containers", "detail": "completed in 4m 12s", "status": "done"},
        {"title": "Canary deployment", "detail": "5% of traffic, monitoring", "status": "active"},
        {"title": "Full rollout", "detail": "waiting for canary", "status": "pending"}]}
    for w, h in ((390, 844), (320, 640), (1280, 800)):
        page, errs = _open(browser, tmp_path, [sc], w, h)
        lines = page.evaluate(LINES_JS, ".steps .t")
        assert lines in (["Canary deployment"], ["Canary", "deployment"]), (w, lines)
        assert not errs
        page.context.close()


OVERFLOW_JS = r"""() => {
  const v = document.querySelector('.view'), fr = v.querySelector('.frame'), F = fr.getBoundingClientRect(), bad = [];
  for (const e of v.querySelectorAll('*')) {
    if (![...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim())) continue;
    if (e.closest('svg')) continue;
    const r = e.getBoundingClientRect(); if (!r.width) continue;
    const cs = getComputedStyle(e);
    const clipped = e.scrollWidth > e.clientWidth + 1 && cs.overflowX !== 'visible' && cs.textOverflow !== 'ellipsis';
    if (r.right > F.right + 1 || r.left < F.left - 1 || clipped) bad.push([e.className, e.textContent.slice(0, 30)]);
  }
  for (const box of v.querySelectorAll('.panel,.tile,.code .bar,.steps li,.timeline li,.tablewrap')) {
    const B = box.getBoundingClientRect();
    for (const e of box.querySelectorAll('*')) { const r = e.getBoundingClientRect();
      if (r.width && r.right > B.right + 1) bad.push(['hidden in ' + box.className, e.textContent.slice(0, 30)]); }
  }
  return {bad: bad.slice(0, 6), docW: document.documentElement.scrollWidth, tableW: (v.querySelector('table') || {}).scrollWidth || 0,
          wrapW: (v.querySelector('.tablewrap') || {}).clientWidth || 0};
}"""

URL = ("https://github.com/example-org/example-repo/blob/0123456789abcdef0123456789abcdef01234567/src/components/very/"
       "deeply/nested/module/path/ComponentWithAQuiteLongNameForTesting.tsx#L120-L180")
PATH = "src/components/very/deeply/nested/module/path/to/the/handler/ComponentWithAQuiteLongNameForTesting.test.tsx"
LONG_SCENES = [
    {"kind": "code", "title": "code", "code": {"lang": "tsx", "filename": PATH + "/" + PATH[:60], "text": "x = 1\n"}},
    {"kind": "table", "title": "table", "columns": ["file", "owner", "note"],
     "rows": [[PATH, "alice", "short"], ["b.ts", "bob", URL]]},
    {"kind": "timeline", "title": "timeline", "events": [
        {"when": "2026-10-03T06:54:01.123456+00:00/run#4", "title": URL, "detail": URL, "status": "done"},
        {"when": "Tue", "title": "review", "status": "active"}]},
    {"kind": "progress", "title": "progress", "steps": [{"title": URL, "detail": PATH, "status": "done"}]},
    {"kind": "deck", "title": "deck", "slides": [
        {"title": "split", "layout": "split", "body": URL, "code": {"text": "x = 1", "filename": PATH}}]},
]


@pytest.mark.parametrize("w,h", [(1280, 800), (390, 844)])
def test_browser_long_tokens_stay_inside_their_box(browser, tmp_path, w, h):
    for i, sc in enumerate(LONG_SCENES):
        page, errs = _open(browser, tmp_path, [sc], w, h)
        got = page.evaluate(OVERFLOW_JS)
        assert got["bad"] == [] and got["docW"] == w and not errs, (sc["kind"], w, got)
        if sc["kind"] == "table":
            assert got["tableW"] <= got["wrapW"] + 1, got           # the table fits: no column pushed out of view
        page.context.close()


DIAGRAM_JS = r"""() => {
  const rect = e => { const r = e.getBoundingClientRect(); return [r.left, r.top, r.right, r.bottom]; };
  const hit = (a, b) => a[0] < b[2] - 1 && b[0] < a[2] - 1 && a[1] < b[3] - 1 && b[1] < a[3] - 1;
  const nodes = [...document.querySelectorAll('.diagram .node')].map(g => [rect(g.querySelector('rect')),
                  [...g.querySelectorAll('text')].map(rect)]);
  const labels = [...document.querySelectorAll('.diagram .elabel rect')].map(rect), out = [];
  const F = rect(document.querySelector('.frame'));
  for (const l of labels) if (l[0] < F[0] - 1 || l[2] > F[2] + 1) out.push('label outside the card');
  for (const [nr, texts] of nodes) {
    for (const t of texts) if (t[0] < nr[0] - 1 || t[2] > nr[2] + 1) out.push('text outside node');
    for (const l of labels) if (hit(l, nr)) out.push('label covers node');
  }
  return {problems: out, nodes: nodes.length, labels: labels.length};
}"""


@pytest.mark.parametrize("w,h", [(1280, 800), (390, 844), (320, 640)])
def test_browser_diagram_labels_stay_off_nodes(browser, tmp_path, w, h):
    sc = {"kind": "diagram", "title": "d", "nodes": [{"id": "a", "label": PATH[:80]}, {"id": "b", "label": "db"}],
          "edges": [["a", "b", PATH[:40]], ["b", "c", "x" * 40]]}
    page, errs = _open(browser, tmp_path, [sc], w, h)
    got = page.evaluate(DIAGRAM_JS)
    assert got["nodes"] == 3 and got["labels"] == 2 and got["problems"] == [] and not errs, got
    page.context.close()


# The line height that the ink of an element's own text needs, from the browser's ink metrics (the fonts on the
# machine decide how high marks stack, so checks that need a tall stack first ask whether this font makes one).
NEED_JS = r"""e => { const cs = getComputedStyle(e), c = document.createElement('canvas').getContext('2d');
  c.font = [cs.fontStyle, cs.fontWeight, cs.fontSize, cs.fontFamily].join(' ');
  const m = c.measureText(e.textContent.replace(/\p{Cf}/gu, ''));
  return Math.max(2 * m.actualBoundingBoxAscent - m.fontBoundingBoxAscent + m.fontBoundingBoxDescent,
                  2 * m.actualBoundingBoxDescent + m.fontBoundingBoxAscent - m.fontBoundingBoxDescent); }"""


def test_browser_axis_labels_and_mark_stacks(browser, tmp_path):
    sc = {"kind": "chart", "chart": "line", "labels": ["a", "b", "c"], "series": [{"name": "x", "values": [1e308, -1e308, 0]}]}
    page, errs = _open(browser, tmp_path, [sc], 1280, 800)
    assert page.evaluate("[...document.querySelectorAll('.chart .axis text')].map(t => t.textContent)") == \
        ["-1e+300", "-5e+299", "0", "5e+299", "1e+300"]
    page.context.close()
    z = "Z" + (chr(0x0E47) + chr(0x0E49) + chr(0xAD)) * 20 + "ap"
    md = {"kind": "markdown", "title": "t", "body": "before\n\n" + z + "\n\n" + REAL_SCRIPTS[1] + "\n\nplain words"}
    page, errs = _open(browser, tmp_path, [md], 1280, 800)
    got = page.evaluate("""need => [...document.querySelectorAll('.md p')].map(p => [p.textContent.slice(0, 6),
        p.classList.contains('stack'), getComputedStyle(p).clipPath !== 'none', parseFloat(getComputedStyle(p).lineHeight),
        (0, eval)('(' + need + ')')(p)])""", NEED_JS)
    # each line holds its ink: the capped flood gets a taller line, a real Tibetan stack only what its ink needs
    assert all(g[2] and g[3] >= g[4] - 1 for g in got) and not errs, got
    assert not got[0][1] and not got[3][1] and got[2][3] <= got[0][3] * 1.3, got
    if got[1][4] > got[0][3] * 1.3:                  # this machine's font stacks the flood high
        assert got[1][1] and got[1][3] > got[0][3] * 1.3, got
    page.context.close()


# -- R3.1 table cells keep short tokens whole; only free text and overlong tokens wrap; phone tables scroll ------

MODELS = {"kind": "table", "title": "Model comparison", "columns": ["model", "provider", "context", "released", "price in/out"],
          "rows": [["claude-sonnet-4.5", "anthropic", "200k", "2025-09-29", "$3 / $15"],
                   ["kimi-k2-instruct", "moonshot", "128k", "2025-07-11", "$0.60 / $2.50"],
                   ["qwen3-coder-480b", "alibaba", "256k", "2025-07-22", "$0.45 / $1.80"]]}
INCIDENTS = {"kind": "table", "title": "Open incidents", "columns": ["id", "service", "opened", "owner", "summary"],
             "rows": [["INC-2041", "payments-api", "2026-10-03 06:54", "a.martin", "Elevated 5xx on checkout after config push"]]}
WIDE = {"kind": "table", "title": "Benchmark", "columns": ["model", "provider", "context", "released", "swe-bench",
                                                            "latency p50", "tokens/s", "price in/out", "notes"],
        "rows": [["claude-sonnet-4.5", "anthropic", "200k", "2025-09-29", "77.2%", "1.9 s", "63", "$3 / $15",
                  "default for long refactors"],
                 ["qwen3-coder-480b", "alibaba", "256k", "2025-07-22", "67.0%", "1.6 s", "95", "$0.45 / $1.80",
                  "self-hostable"]]}
RELEASE = {"kind": "timeline", "title": "Release train", "events": [
    {"when": "2026-10-03 14:00", "title": "Release candidate to staging", "status": "active"},
    {"when": "Mon 09:00", "title": "Production rollout", "status": "pending"},
    {"when": "Tuesday 14:00 to 18:00", "title": "Freeze window", "status": "pending"}]}

CELL_LINES_JS = r"""() => {
  const v = document.querySelector('.view'), tw = v.querySelector('.tablewrap'), out = {};
  for (const td of v.querySelectorAll('tbody td, thead th, .timeline .when')) {
    const r = document.createRange(); r.selectNodeContents(td);
    out[td.textContent] = new Set([...r.getClientRects()].map(x => Math.round(x.top))).size;
  }
  return {lines: out, scrollW: tw ? tw.scrollWidth : 0, clientW: tw ? tw.clientWidth : 0, docW: document.documentElement.scrollWidth};
}"""


@pytest.mark.parametrize("w,h", [(1280, 800), (390, 844)])
def test_browser_short_table_tokens_stay_on_one_line(browser, tmp_path, w, h):
    short = ["claude-sonnet-4.5", "2025-09-29", "$3 / $15", "kimi-k2-instruct", "qwen3-coder-480b", "2025-07-22",
             "$0.60 / $2.50", "INC-2041", "payments-api", "2026-10-03 06:54", "PRICE IN/OUT", "2026-10-03 14:00"]
    seen = set()
    for sc in (MODELS, INCIDENTS, WIDE, RELEASE):
        page, errs = _open(browser, tmp_path, [sc], w, h)
        got = page.evaluate(CELL_LINES_JS)
        assert not errs and got["docW"] == w, got
        for text, n in got["lines"].items():
            if text.upper() in [t.upper() for t in short]:
                assert n == 1, (sc["title"], w, text, n)
                seen.add(text.upper())
        if sc is INCIDENTS and w < 500:
            assert got["lines"]["Elevated 5xx on checkout after config push"] > 1, got   # free text wraps at spaces
            assert got["scrollW"] > got["clientW"], got                                  # the table scrolls sideways
        if sc is RELEASE and w < 500:
            assert got["lines"]["Tuesday 14:00 to 18:00"] > 1, got                       # free text in a time wraps
        if sc is WIDE and w > 1000:
            assert got["scrollW"] <= got["clientW"] + 1, got                             # and fits on a desktop
        page.context.close()
    assert seen == {t.upper() for t in short}, seen


# -- R3.2 edge labels stay off nodes and off each other in a top-to-bottom layout (phones) ------------------------

PILLS_JS = r"""() => {
  const rect = e => { const r = e.getBoundingClientRect(); return [r.left, r.top, r.right, r.bottom]; };
  const hit = (a, b) => a[0] < b[2] - 1 && b[0] < a[2] - 1 && a[1] < b[3] - 1 && b[1] < a[3] - 1;
  const nodes = [...document.querySelectorAll('.diagram .node > rect')].map(rect);
  const pills = [...document.querySelectorAll('.diagram .elabel rect')].map(rect), out = [];
  pills.forEach((p, i) => { nodes.forEach(n => { if (hit(p, n)) out.push('label covers node'); });
    pills.slice(i + 1).forEach(q => { if (hit(p, q)) out.push('labels overlap'); }); });
  return {problems: out, pills: pills.length};
}"""

CYCLE = {"kind": "diagram", "title": "Retry loop", "direction": "LR", "nodes": ["queued", "running", "failed", "done"],
         "edges": [["queued", "running", "start"], ["running", "failed", "error"], ["failed", "queued", "retry (max 3)"],
                   ["running", "done", "ok"]]}
FANOUT = {"kind": "diagram", "title": "Services", "direction": "LR",
          "nodes": [{"id": "a", "label": "Customer notification dispatcher"}, {"id": "b", "label": "Email delivery provider (SES)"},
                    {"id": "c", "label": "SMS gateway"}, {"id": "d", "label": "Push service"}],
          "edges": [["a", "b", "templated email"], ["a", "c", "short message"], ["a", "d", "push payload"]]}
TREE = {"kind": "diagram", "title": "Build graph", "direction": "TB", "nodes": ["app", "core", "ui", "net", "store"],
        "edges": [["app", "core", "imports"], ["app", "ui", "renders"], ["core", "net", "fetches"], ["core", "store", "writes"],
                  ["ui", "core", "uses"]]}


@pytest.mark.parametrize("w,h", [(390, 844), (320, 640), (1280, 800)])
def test_browser_edge_labels_never_cover_nodes_or_each_other(browser, tmp_path, w, h):
    for sc in (CYCLE, FANOUT, TREE):
        page, errs = _open(browser, tmp_path, [sc], w, h)
        got = page.evaluate(PILLS_JS)
        assert got["pills"] == len(sc["edges"]) and got["problems"] == [] and not errs, (sc["title"], w, got)
        page.context.close()


# -- R3.3 in the page: axis and value labels for values near 1e15 stay distinct ---------------------------------

def test_browser_axis_labels_near_1e15_stay_distinct(browser, tmp_path):
    line = {"kind": "chart", "chart": "line", "unit": "B", "labels": ["w1", "w2", "w3", "w4"],
            "series": [{"name": "bytes", "values": [1.21e15, 1.23e15, 1.25e15, 1.27e15]}]}
    bar = {"kind": "chart", "chart": "bar", "labels": ["Mon", "Tue", "Wed"], "series": [{"name": "t", "values": [2.41e15, 2.44e15, 2.47e15]}]}
    page, errs = _open(browser, tmp_path, [line], 1280, 800)
    axis = page.evaluate("[...document.querySelectorAll('.chart .axis text')].map(t => t.textContent)")
    assert axis == ["1200TB", "1220TB", "1240TB", "1260TB", "1280TB"] and not errs, axis
    page.context.close()
    page, errs = _open(browser, tmp_path, [bar], 1280, 800)
    got = page.evaluate("[[...document.querySelectorAll('.chart g.axis text')].map(t => t.textContent),"
                        " [...document.querySelectorAll('.chart .vl')].map(t => t.textContent)]")
    assert got == [["0", "500T", "1000T", "1500T", "2000T", "2500T"], ["2410T", "2440T", "2470T"]] and not errs, got
    page.context.close()


# -- R3.4 marks never paint over a neighbouring line: code lines, cells, chart labels, dock pills ----------------

M6 = (chr(0x0E47) + chr(0x0E49)) * 3            # 6 marks per base: exactly what the schema keeps


def _m6(word):
    return "".join(c + M6 for c in word)


def test_browser_marks_stay_inside_their_line_box(browser, tmp_path):
    code = {"kind": "code", "title": "Code lines", "code": {"lang": "py", "text": "def f():\n    a = 1\n    b = '%s'\n    return a\n" % _m6("zalgo")}}
    table = {"kind": "table", "title": "Cells", "columns": [_m6("one"), "two"], "rows": [[_m6("abc"), "b"], ["c", "d"]]}
    chart = {"kind": "chart", "chart": "bar", "title": "x labels", "labels": [_m6("Mon"), "Tue", "Wed"], "series": [{"values": [3, 5, 4]}]}
    rows = {"kind": "chart", "chart": "bar", "title": "row labels", "labels": [_m6("Monday shift"), "b", "c", "d", "e", "f"],
            "series": [{"values": [3, 5, 4, 1, 2, 3]}, {"values": [1, 2, 3, 4, 5, 6]}, {"values": [1, 2, 3, 4, 5, 6]}]}
    line = {"kind": "chart", "chart": "line", "title": "line labels", "labels": [_m6("Mon"), "Tue", "Wed"], "series": [{"values": [3, 5, 4]}]}
    page, errs = _open(browser, tmp_path, [code, table, chart, rows, line], 1280, 800)
    got = page.evaluate("""need => { const ln = [...document.querySelectorAll('.view .code .ln')];
      const s = document.querySelector('.view .code .tk-str');
      return {h: ln.map(l => l.getBoundingClientRect().height), clip: ln.map(l => getComputedStyle(l).clipPath),
              cls: s.className, stack: ln[2].classList.contains('stack'), lines: ln.map(l => { const r = l.getBoundingClientRect(); return [r.top, r.bottom]; }),
              str: (r => [r.left, r.right])(s.getBoundingClientRect()), need: (0, eval)('(' + need + ')')(s)}; }""", NEED_JS)
    assert not errs and got["h"][2] >= got["need"] - 1, got                    # the zalgo line holds its ink
    if got["need"] > got["h"][1] * 1.3:
        assert got["stack"] and got["h"][2] > got["h"][1] * 1.3, got
    assert all(c != "none" for c in got["clip"]) and "brk" not in got["cls"], got   # marks do not make a word 'overlong'
    shot = page.screenshot()
    try:
        from PIL import Image
    except ImportError:
        Image = None
    if Image is not None:                    # no string ink (orange) inside line 2's box, above the zalgo line
        im = Image.open(io.BytesIO(shot)).convert("RGB")
        top, bottom = got["lines"][1]
        ink = [(x, y) for y in range(int(top) + 1, int(bottom)) for x in range(int(got["str"][0]), int(got["str"][1]))
               if (lambda p: p[0] > 180 and 120 < p[1] < 210 and p[2] < 140)(im.getpixel((x, y)))]
        assert ink == [], ink[:5]
    clips = []
    for where in ("2.1", "3.1", "4.1", "5.1"):
        page.goto(page.url.split("#")[0] + "#" + where)
        page.wait_for_timeout(250)
        clips.append(page.evaluate("""() => { const v = document.querySelector('.view');
          return [[...v.querySelectorAll('tbody td, thead th')].every(e => getComputedStyle(e).clipPath !== 'none'),
                  [...v.querySelectorAll('svg text')].filter(t => /[a-z]/i.test(t.textContent) && !t.closest('.vl'))
                     .every(t => t.closest('[clip-path]') !== null),
                  [...document.querySelectorAll('#dock button > *')].every(e => getComputedStyle(e).clipPath !== 'none')]; }"""))
    assert clips[0][0] and all(c[1] and c[2] for c in clips[1:]), clips
    page.context.close()


# -- R3.5 pointed Hebrew keeps normal line spacing; only an unusual stack gets room ------------------------------

def test_browser_pointed_hebrew_title_keeps_normal_spacing(browser, tmp_path):
    pointed = "\u05e9\u05b4\u05c1\u05d9\u05e8 \u05d4\u05b7\u05e9\u05bc\u05b4\u05c1\u05d9\u05e8\u05b4\u05d9\u05dd: " * 3
    plain = "".join(c for c in pointed if not 0x591 <= ord(c) <= 0x5c7)
    deck = {"kind": "deck", "title": "Hebrew", "slides": [{"title": pointed, "body": "a"}, {"title": plain, "body": "b"},
                                                         {"title": _m6("Header"), "body": "c"}]}
    hs = []
    for where in ("1.1", "1.2", "1.3"):
        page, errs = _open(browser, tmp_path, [deck], 390, 844, where)
        hs.append(page.evaluate("need => { const t = document.querySelector('.view .h-title');"
                                " return [parseFloat(getComputedStyle(t).lineHeight), t.getBoundingClientRect().height,"
                                " (0, eval)('(' + need + ')')(t)]; }", NEED_JS))
        assert not errs
        page.context.close()
    assert hs[0][0] <= hs[1][0] * 1.3 and hs[0][1] <= hs[1][1] * 1.3, hs     # niqqud fits in a normal line
    assert all(h[0] >= h[2] - 1 for h in hs), hs                                # every title line holds its ink
    if hs[2][2] > hs[1][0] * 1.3:
        assert hs[2][0] > hs[1][0] * 1.3, hs                                    # a 6-mark flood gets room
