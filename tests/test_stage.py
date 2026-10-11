# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Pulsar Stage viewers: layout kit, terminal viewer, web page, export, launcher."""
import io
import json
import os
import re
import select
import shlex
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from pulsar import fx
from pulsar.stage import export as EX
from pulsar.stage import launcher as LA
from pulsar.stage import render as R
from pulsar.stage import term as T
from pulsar.stage import web as W
from pulsar.stage.schema import normalize

ROOT = str(Path(__file__).resolve().parent.parent)

DECK = {"kind": "deck", "title": "Pulsar Core", "slides": [
    {"title": "Pulsar Core", "subtitle": "One router", "body": "Any intelligence, one terminal.", "layout": "title"},
    {"title": "Why a router", "body": "- **One process** per user\n- Sessions hot-swap models\n- Relay chain with `top`",
     "notes": "say hi"},
    {"title": "Flow", "embed": {"kind": "diagram", "nodes": ["cc", "core", "nv", "or"],
                                "edges": [["cc", "core", "messages"], ["core", "nv", "primary"], ["core", "or"]]}},
    {"title": "Latency", "embed": {"kind": "chart", "chart": "line", "labels": ["a", "b", "c"],
                                   "series": [{"name": "p50", "values": [3, 1, 2]}], "unit": "ms"}},
    {"title": "Code", "body": "short", "code": {"lang": "py", "text": "def f(x):\n    return x  # hi\n", "highlight": [2]}},
    {"title": "Speed", "stat": {"value": "412ms", "label": "p50"}},
    {"body": "Make the common case fast.", "subtitle": "Lampson", "layout": "quote"},
    {"title": "Split", "layout": "split", "body": "- a\n- b", "code": {"lang": "ts", "text": "const a: number = 1;"}},
    {"title": "Table", "embed": {"kind": "table", "columns": ["a", "b"], "rows": [["1", "ok"], ["2", "fail"]]}},
]}
SCENES = [
    {"kind": "chart", "title": "Bars", "chart": "bar", "labels": ["NVIDIA", "OpenRouter", "Groq"],
     "series": [{"values": [12, 30, 7]}]},
    {"kind": "chart", "title": "Neg", "chart": "bar", "labels": ["a" * 30, "b", "c", "d", "e", "f", "g", "h", "i"],
     "series": [{"values": [1, -2, 3, -4, 5, 6, 7, 8, 9]}]},
    {"kind": "chart", "title": "Multi", "chart": "bar", "labels": ["x", "y"],
     "series": [{"name": "a", "values": [1, 2]}, {"name": "b", "values": [3, 4]}]},
    {"kind": "chart", "chart": "line", "series": [{"name": "in", "values": list(range(50))},
                                                 {"name": "out", "values": [5] * 50}]},
    {"kind": "chart", "chart": "line", "series": [{"values": [7]}]},
    {"kind": "chart", "chart": "spark", "series": [{"name": "rps", "values": [1, 5, 2, 8, 3]}, {"values": [0, 0]}]},
    {"kind": "diagram", "title": "TB", "direction": "TB", "nodes": ["lint", "test", "e2e", "pkg", "pub"],
     "edges": [["lint", "test"], ["test", "e2e"], ["test", "pkg"], ["e2e", "pub", "gate"], ["pkg", "pub"]]},
    {"kind": "diagram", "nodes": [{"id": "a", "label": "A very long node label that needs wrapping"}, "b", "c"],
     "edges": [["a", "b"], ["b", "c"], ["c", "a", "loop"], ["a", "c"]]},
    {"kind": "table", "title": "T", "columns": ["Model", "Provider", "Ctx", "Notes"],
     "rows": [["kimi", "OpenRouter", "262K", "x" * 80]] * 40},
    {"kind": "table", "rows": [["只有中文", "😀 emoji", "ok"]]},
    {"kind": "code", "title": "C", "code": {"lang": "go", "filename": "a.go", "highlight": [30],
                                            "text": "\n".join("line %d // c" % i for i in range(60))}},
    {"kind": "metrics", "metrics": [{"label": "Req", "value": "1,284", "delta": "+12%", "trend": [1, 2, 3]},
                                    {"label": "TTFT", "value": "412ms", "delta": "-8%", "trend": [3, 2, 1]},
                                    {"label": "Cost", "value": "$1.92"}]},
    {"kind": "timeline", "events": [{"when": "Sep 12", "title": "Draft", "detail": "d " * 60, "status": "done"},
                                    {"when": "Oct", "title": "Now", "status": "active"}, "Later"]},
    {"kind": "progress", "id": "p1", "title": "Ship", "steps": [{"title": "a", "status": "done"},
                                                                 {"title": "b", "status": "active", "detail": "x"},
                                                                 {"title": "c", "status": "failed"}, "d"]},
    {"kind": "card", "title": "Heads up", "tone": "warn", "body": "**Bold** and `code`\n\n- one\n- two"},
    {"kind": "markdown",
     "body": "# H1\n## H2\ntext *em* [link](http://x)\n\n1. one\n2. two\n\n> q\n\n---\n```py\nx = 1\n```"},
]
SIZES = [(30, 10), (40, 12), (60, 18), (100, 30), (160, 45), (250, 70)]


def all_scenes():
    return [normalize(DECK)] + [normalize(s) for s in SCENES]


def frames_for(scene):
    if scene["kind"] == "deck":
        return list(range(len(scene["slides"])))
    return [0]


# ---------------------------------------------------------------------------
# render: geometry + safety at every size
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("w,h", SIZES)
@pytest.mark.parametrize("u", [True, False])
def test_every_scene_kind_renders_inside_its_region(w, h, u):
    ox, oy = 3, 2
    for sc in all_scenes():
        for slide in frames_for(sc):
            for t, motion in ((0.25, True), (R.SETTLED, False)):
                cv = fx.Canvas(w + 6, h + 4)
                R.draw_scene(cv, ox, oy, w, h, sc, slide=slide, u=u, t=t, motion=motion)
                for y in range(cv.h):
                    for x in range(cv.w):
                        ch = cv.cells[y][x][0]
                        inside = ox <= x < ox + w and oy <= y < oy + h
                        if not inside:
                            assert cv.cells[y][x] == fx.BLANK, (sc["kind"], slide, w, h, x, y)
                        assert "\x1b" not in ch
                        if not u:
                            assert ch.isascii(), (sc["kind"], slide, repr(ch))


def test_hostile_text_never_reaches_the_canvas_as_escapes():
    sc = normalize({"kind": "card", "title": "\x1b[2Jboom\x07", "body": "a\x1b]0;pwned\x07b \x1b[31mred"})
    cv = fx.Canvas(80, 20)
    R.draw_scene(cv, 0, 0, 80, 20, sc)
    flat = "".join(c[0] for row in cv.cells for c in row)
    assert "\x1b" not in flat and "\x07" not in flat and "pwned" not in flat


def test_wrap_clip_and_width_are_display_aware():
    for w in range(2, 40):   # a 2-cell glyph cannot fit a 1-cell line
        for line in R.wrap("Pulsar 日本語のテキスト routes every request, supercalifragilistic", w):
            assert R.swidth(line) <= w
        assert R.swidth(R.clip("日本語のテキストとPulsar", w)) <= w
    assert R.swidth("日本") == 4 and R.swidth("abc") == 3
    assert R.asc("“quote” → ✓ é") == '"quote" -> v e'


def test_wide_glyphs_get_placeholders_and_compositing_is_repaired():
    cv = fx.Canvas(10, 1)
    v = R.View(cv)
    end = v.text(0, 0, "日x")
    assert end == 3 and cv.cells[0][0][0] == "日" and cv.cells[0][1][0] == "" and cv.cells[0][2][0] == "x"
    v.put(1, 0, "y")                       # overwrite the right half: the glyph is dropped, never misaligned
    assert cv.cells[0][0][0] == " "
    cv2 = fx.Canvas(4, 1)
    cv2.cells[0][0] = ("", None, False)    # orphan placeholder from a sliding blit
    cv2.cells[0][3] = ("日", None, False)  # wide glyph cut at the edge
    R.fix_wide(cv2)
    assert cv2.cells[0][0][0] == " " and cv2.cells[0][3][0] == " "


def test_view_clips_to_parent_and_negative_offsets():
    cv = fx.Canvas(10, 4)
    outer = R.View(cv, 2, 1, 5, 2)
    inner = R.View(outer, -3, 0, 20, 5)
    inner.text(0, 0, "abcdefghijklmnop")
    row = "".join(c[0] for c in cv.cells[1])
    assert row == "  defgh   "
    assert all(c == fx.BLANK for c in cv.cells[0] + cv.cells[3])


# ---------------------------------------------------------------------------
# markdown, big type, numbers, highlighting
# ---------------------------------------------------------------------------

def test_markdown_blocks_and_inline():
    blocks = R.parse_md("# Title\n\npara one\nstill para\n- a\n  more a\n  - nested\n1. first\n> quote\n---\n"
                        "```py\nx=1\n```")
    kinds = [b[0] for b in blocks]
    assert kinds == ["h", "blank", "p", "ul", "ul", "ol", "quote", "hr", "code"]
    assert blocks[2] == ("p", "para one still para")
    assert blocks[3] == ("ul", 0, "a more a") and blocks[4][1] == 1
    assert blocks[-1] == ("code", "py", "x=1")
    spans = R.parse_inline("a **b** `c` *d* [e](http://x) snake_case_name 2*3*4")
    assert ("b", "b") in spans and ("c", "c") in spans and ("d", "i") in spans and ("e", "l") in spans
    assert any("snake_case_name" in s for s, k in spans if k == "n")
    for w in (8, 20, 47):
        for ln in R.md_lines("- **bold** words that wrap a lot over the edge\n1. numbered entry here", w):
            assert R.line_width(ln) <= w


def test_big_font_glyphs_are_rectangular_and_widths_match_drawing():
    for ch, rows in R.BIG_GLYPHS.items():
        assert len(rows) == 5 and len({len(r) for r in rows}) == 1, ch
        assert set("".join(rows)) <= {"#", "."}, ch
    for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.,:;!?-+=/()%$#&":
        assert c in R.BIG_GLYPHS
    for mode in R.BIG_MODES:
        for text in ("PULSAR", "1,284", "A B"):
            cv = fx.Canvas(200, 8)
            w = R.draw_big(cv, 1, 1, text, mode)
            xs = [x for y in range(8) for x in range(200) if cv.cells[y][x][0] != " "]
            assert w == R.big_width(text, mode) and max(xs) - min(xs) + 1 <= w
            ys = [y for y in range(8) for x in range(200) if cv.cells[y][x][0] != " "]
            assert max(ys) - 1 < R.big_height(mode)
    fit = R.big_fit("why a router", 60, 3, ("compact",))
    assert fit and fit[0] == "compact" and R.big_width(fit[1][0], "compact") <= 60
    two = R.big_fit("why a router", 40, 7, ("compact",))
    assert two and two[1] == ["WHY A", "ROUTER"]
    assert R.big_fit("why a router", 40, 3, ("compact",)) is None
    assert R.big_fit("x" * 60, 400, 9) is None
    assert R.split_value("412ms") == ("412", "ms") and R.split_value("98%") == ("98%", "")


def test_numbers_ticks_and_sparklines():
    assert R.fmt_num(1234) == "1234" and R.fmt_num(12345) == "12.3k" and R.fmt_num(2.5e6) == "2.5M"
    assert R.fmt_num(0.25) == "0.25" and R.fmt_num(5, "$") == "$5" and R.fmt_num(-5, "$") == "-$5"
    lo, hi, step = R.nice_ticks(3, 97, 5)
    assert lo <= 3 and hi >= 97 and step in (20, 25)
    assert R.nice_ticks(5, 5) != (5, 5, 0)
    assert len(R.sparkline(list(range(100)), 10)) == 10 and R.sparkline([], 5) == ""
    assert R.count_up("$1,284.50", 0.5) == "$642.25" and R.count_up("n/a", 0.5) == "n/a"


def test_highlighting_tokens_and_multiline_state():
    lines = R.highlight('def f():\n    """doc\n    more"""\n    return 1  # c', "python")
    flat = [(t, c) for ln in lines for t, c, _ in ln]
    assert ("def", R.C_KW) in flat and ("1", R.C_NUM) in flat and ("# c", R.C_COM) in flat
    assert all(c == R.C_STR for t, c, _ in lines[2] if t.strip().startswith("more"))
    js = R.highlight("/* a\n b */ const x = `t`;", "typescript")
    assert js[0][0][1] == R.C_COM and any(t == "const" for t, _, _ in js[1])
    j = R.highlight('{"k": true}', "json")
    assert any(t == '"k"' and c == R.C_KEY for t, c, _ in j[0])
    assert R.norm_lang("bash") == "sh" and R.norm_lang("weird") == "generic"


# ---------------------------------------------------------------------------
# diagrams
# ---------------------------------------------------------------------------

def test_diagram_ranks_longest_path_and_cycles():
    nodes = [{"id": i, "label": i} for i in "ABCD"]
    rank, dag = R.diagram_ranks(nodes, [{"from": "A", "to": "B"}, {"from": "B", "to": "C"}, {"from": "A", "to": "C"},
                                        {"from": "D", "to": "D"}])
    assert rank == {"A": 0, "B": 1, "C": 2, "D": 0}
    rank2, dag2 = R.diagram_ranks(nodes[:3], [{"from": "A", "to": "B"}, {"from": "B", "to": "C"},
                                              {"from": "C", "to": "A"}])
    assert sorted(rank2.values()) == [0, 1, 2] and len(dag2) == 3


@pytest.mark.parametrize("direction", ["LR", "TB"])
def test_diagram_layout_fits_and_boxes_do_not_overlap(direction):
    sc = normalize(SCENES[6])
    L = R.layout_diagram(sc["nodes"], sc["edges"], direction, 120, 40)
    assert L is not None and L.w <= 120 and L.h <= 40
    boxes = list(L.boxes.values())
    for i, (x, y, w, h, _) in enumerate(boxes):
        assert x >= 0 and y >= 0 and x + w <= L.w and y + h <= L.h
        for (x2, y2, w2, h2, _) in boxes[i + 1:]:
            assert x + w <= x2 or x2 + w2 <= x or y + h <= y2 or y2 + h2 <= y
    assert L.ranks["lint"] == 0 and L.ranks["pub"] == 3
    assert L.arrows and all(0 <= ax < L.w and 0 <= ay < L.h for ax, ay, _, _ in L.arrows)
    assert R.layout_diagram(sc["nodes"], sc["edges"], direction, 12, 4) is None


def test_table_columns_always_fit():
    cols = ["Model", "Provider", "Context", "Notes"]
    rows = [["kimi-k2.7-code", "OpenRouter", "262K", "x" * 90]]
    for w in range(8, 140):
        widths, shown, numeric = R.table_geometry(cols, rows, w)
        assert sum(widths) + 3 * shown + 1 <= max(w, 4 * shown + 1)
    widths, shown, numeric = R.table_geometry(cols, rows, 200)
    assert shown == 4 and numeric == [False, False, True, False]


# ---------------------------------------------------------------------------
# web page + export
# ---------------------------------------------------------------------------

HOSTILE = '</script><img src=x onerror=alert(1)><!-- \u2028 & "q"'


def test_live_page_is_self_contained_and_never_assigns_markup():
    html = W.page()
    assert html.startswith("<!doctype html>") and "EventSource(" in html and "/pulsar/stage/stream" in html
    assert "innerHTML" not in html and "outerHTML" not in html and "insertAdjacentHTML" not in html
    assert "document.write" not in html and "eval(" not in html
    urls = re.findall(r"https?://[^\s\"')]+", html)
    assert all(u.startswith("http://www.w3.org/2000/svg") for u in urls), urls
    assert 'id="pulsar-data"' not in html and "connect-src 'self'" in html
    assert "\u2014" not in html


def test_page_scripts_run_only_by_their_hash():
    import base64
    import hashlib
    for html in (W.page(), EX.export_html([DECK])):
        csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', html).group(1)
        script_src = csp.split("script-src", 1)[1].split(";", 1)[0]
        assert "'unsafe-inline'" not in script_src                 # an injected inline script would not run
        assert html.count("<script>") == 1                         # the app; the data block is application/json
        body = html.split("<script>", 1)[1].split("</script>", 1)[0]
        assert "'sha256-%s'" % base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode() in script_src


def _run_page_js(snippet):
    import shutil as _shutil
    node = _shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    js = W.page().split("<script>", 1)[1].split("</script>", 1)[0]

    def cut(start, end):
        return js[js.index(start):js.index(end)]
    lib = ("var Q3 = '\"\"\"';\n" + cut("function str(", "function clamp(") +
           cut("function dict(", "/* ---------- dom helpers") +
           cut("/* ---------- code highlighting", "function codePanel(") +
           cut("/* ---------- diagram: layered", "function wrapLabel("))
    out = subprocess.run([node, "-e", lib + snippet], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_page_survives_prototype_names_in_code_languages_and_diagram_ids():
    bad = _run_page_js("""
var bad = [];
["constructor", "__proto__", "toString", "hasOwnProperty", "valueOf", "py", "sql"].forEach(function (l) {
  try { var lines = highlight("x = 1", l); if (lines.length !== 1) bad.push("lang " + l); }
  catch (e) { bad.push("lang " + l + ": " + e); }
});
[["__proto__", "a"], ["constructor", "b"], ["toString", "valueOf"]].forEach(function (p) {
  try { var g = layered([{id: p[0]}, {id: p[1]}], [{from: p[0], to: p[1]}]); if (g.nl !== 2) bad.push("ranks " + p); }
  catch (e) { bad.push("diagram " + p + ": " + e); }
});
console.log(JSON.stringify(bad));
""")
    assert bad == []
    js = W.page().split("<script>", 1)[1]
    printing = js.split('addEventListener("beforeprint"', 1)[1].split('addEventListener("afterprint"', 1)[0]
    assert "catch (e)" in printing                          # one bad scene never drops the rest of a printout


def test_export_embeds_scenes_without_breakout():
    deck = dict(DECK, title=HOSTILE)
    html = EX.export_html([deck, {"kind": "card", "body": HOSTILE}, {"kind": "bogus"}])
    assert html.lower().count("</script") == 2           # the data block and the app script, nothing more
    assert "<img src=x" not in html and "<!-- " not in html.split('id="pulsar-data"', 1)[1].split("</script>")[0]
    m = re.search(r'<script type="application/json" id="pulsar-data">(.*?)</script>', html, re.S)
    data = json.loads(m.group(1))
    assert data["mode"] == "static" and len(data["items"]) == 2   # the bogus scene was dropped
    # U+2028 becomes a line break (schema.clean); a title is one line, so it collapses to one space there
    assert data["items"][0]["scene"]["title"] == HOSTILE.replace(" \u2028 ", " ") and "\u2028" not in m.group(1)
    assert data["items"][1]["scene"]["body"] == HOSTILE.replace("\u2028", "\n")
    assert "connect-src" not in html.split("Content-Security-Policy", 1)[1][:400]
    # normalize() no longer lets U+2028/2029 through, so the embedder's own escaping is checked directly
    raw = EX.embed_json({"t": "a\u2028b\u2029c</script><!--"})
    assert "\u2028" not in raw and "\u2029" not in raw and "<" not in raw and ">" not in raw
    assert json.loads(raw) == {"t": "a\u2028b\u2029c</script><!--"}


def test_export_accepts_core_items_and_dedupes_by_id(tmp_path):
    items = [{"sid": "s1", "op": "show", "scene": {"kind": "progress", "id": "p", "steps": ["a"]}, "t": 1.0},
             {"sid": "s1", "op": "show", "scene": {"kind": "progress", "id": "p", "steps": ["a", "b"]}, "t": 2.0},
             {"sid": "s1", "op": "clear", "scene": {}, "t": 3.0}]
    got = EX.collect(items)
    assert len(got) == 1 and len(got[0]["scene"]["steps"]) == 2 and got[0]["sid"] == "s1"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_write_export_is_private(pulsar_home):
    p = EX.write_export([DECK])
    assert p.parent == pulsar_home / "stage" and p.suffix == ".html"
    assert (p.stat().st_mode & 0o777) == 0o600 and (p.parent.stat().st_mode & 0o777) == 0o700
    p2 = EX.write_export([DECK])
    assert p2 != p
    custom = EX.write_export(SCENES[:2], pulsar_home / "out.html")
    assert custom.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_export_never_replaces_a_file_it_did_not_write(pulsar_home, tmp_path):
    mine = tmp_path / "notes.md"
    mine.write_text("my notes")
    with pytest.raises(OSError):
        EX.write_export([DECK], mine)
    assert mine.read_text() == "my notes"
    page = tmp_path / "index.html"
    page.write_text("<html>mine</html>")
    with pytest.raises(OSError):
        EX.write_export([DECK], page)
    assert page.read_text() == "<html>mine</html>"
    out = tmp_path / "deck.html"
    EX.write_export([DECK], out)
    EX.write_export(SCENES[:1], out)                       # an earlier export at that path is replaced
    assert "Bars" in out.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# launcher
# ---------------------------------------------------------------------------

class Calls:
    def __init__(self, codes=None, which=("tmux", "zellij", "wezterm", "kitty", "osascript", "wt", "xdg-open", "open")):
        self.runs = []
        self.spawns = []
        self.codes = list(codes or [])
        self.which = set(which)

    def install(self, monkeypatch):
        monkeypatch.setattr(LA, "_which", lambda n: ("/usr/bin/" + n) if n in self.which else None)
        monkeypatch.setattr(LA, "_run", self.run)
        monkeypatch.setattr(LA, "_spawn", self.spawn)
        monkeypatch.setattr(LA, "has_terminal", lambda: True)     # a person at a terminal, as in a real session
        return self

    def run(self, argv, timeout=4.0, detach=False):
        self.runs.append(list(argv))
        return self.codes.pop(0) if self.codes else 0

    def spawn(self, argv):
        self.spawns.append(list(argv))
        return True


def test_launcher_tmux_split_quotes_everything(monkeypatch):
    c = Calls().install(monkeypatch)
    env = {"TMUX": "/tmp/tmux-1/default,1,0", "TMUX_PANE": "%3", "PULSAR_HOME": "/tmp/my home/'q'",
           "ANTHROPIC_AUTH_TOKEN": "pls_secret", "OPENAI_API_KEY": "sk-secret"}
    out = LA.open_viewer("http://127.0.0.1:4747/stage?t=plv_x", "s1a2b3", env, "linux")
    assert out == "opened in a tmux split"
    argv = c.runs[0]
    assert argv[:4] == ["/usr/bin/tmux", "split-window", "-h", "-d"] and "-t" in argv and "%3" in argv
    line = shlex.split(argv[-1])
    assert line[0] == "/usr/bin/env" and "PYTHONPATH=" + ROOT in line and "PULSAR_HOME=/tmp/my home/'q'" in line
    assert line[-4:] == ["pulsar", "stage", "--sid", "s1a2b3"] and sys.executable in line
    assert "-m" not in line and "-c" in line       # the cwd-safe bootstrap, never `-m pulsar`
    assert "secret" not in argv[-1]


def test_launcher_tmux_old_version_falls_back_to_percent_flag(monkeypatch):
    c = Calls(codes=[1, 0]).install(monkeypatch)
    assert LA.open_viewer("", "s1", {"TMUX": "x"}, "linux") == "opened in a tmux split"
    assert "-l" in c.runs[0] and "-p" in c.runs[1]


def test_launcher_rejects_hostile_sid(monkeypatch):
    c = Calls().install(monkeypatch)
    LA.open_viewer("", "s1; rm -rf ~", {"TMUX": "x"}, "linux")
    cmd = c.runs[0][-1]
    # the hostile sid never reaches the command, as an argument or as shell text ("rm" alone would also
    # match paths such as .../arm64/bin/python on ARM runners)
    tokens = shlex.split(cmd)
    assert "--sid" not in tokens and "rm" not in tokens and "-rf" not in tokens
    assert "s1;" not in cmd and "rm -rf" not in cmd


@pytest.mark.parametrize("env,platform,expect,first", [
    ({"ZELLIJ": "0"}, "linux", "opened in a zellij pane", ["/usr/bin/zellij", "run"]),
    ({"WEZTERM_PANE": "7"}, "linux", "opened in a WezTerm split", ["/usr/bin/wezterm", "cli", "split-pane"]),
    ({"KITTY_LISTEN_ON": "unix:/tmp/k"}, "linux", "opened in a kitty split", ["/usr/bin/kitty", "@", "--to"]),
    ({"TERM_PROGRAM": "iTerm.app", "ITERM_SESSION_ID": "w0t0p0:ABCD-1234", "PULSAR_STAGE_VIEWER": "split"}, "darwin",
     "opened in an iTerm2 split", ["/usr/bin/osascript", "-e"]),
    ({"WT_SESSION": "1"}, "win32", "opened in a Windows Terminal split", ["/usr/bin/wt", "-w", "0", "split-pane"]),
])
def test_launcher_terminal_strategies(monkeypatch, env, platform, expect, first):
    c = Calls().install(monkeypatch)
    assert LA.open_viewer("http://127.0.0.1:1/stage", "s9", env, platform) == expect
    assert c.runs[0][:len(first)] == first and not c.spawns
    if first[0].endswith("wezterm"):
        assert c.runs[1][:3] == ["/usr/bin/wezterm", "cli", "activate-pane"]
    if first[0].endswith("osascript"):
        assert "split vertically" in c.runs[0][2] and "ABCD-1234" in c.runs[0][2]


def test_launcher_auto_never_drives_iterm_and_opens_the_browser_behind(monkeypatch):
    c = Calls().install(monkeypatch)
    url = "http://127.0.0.1:4747/stage?t=plv_x"
    env = {"TERM_PROGRAM": "iTerm.app", "ITERM_SESSION_ID": "w0t0p0:ABCD-1234"}
    # auto: no command typed into another terminal session; the browser opens without taking the focus
    assert LA.open_viewer(url, "s9", env, "darwin") == "opened in your browser"
    assert not c.runs and c.spawns == [["/usr/bin/open", "-g", url]]
    assert "iterm" not in LA.choose(env, "darwin") and "iterm" in LA.choose(dict(env, PULSAR_STAGE_VIEWER="split"),
                                                                             "darwin")


def test_launcher_opens_nothing_without_a_terminal(monkeypatch):
    c = Calls().install(monkeypatch)
    monkeypatch.setattr(LA, "has_terminal", lambda: False)        # a scripted run: cron, CI, no terminal
    url = "http://127.0.0.1:4747/stage?t=plv_x"
    for env, platform in (({"TMUX": "x", "DISPLAY": ":0"}, "linux"), ({}, "darwin"), ({"WT_SESSION": "1"}, "win32"),
                          ({"TERM_PROGRAM": "iTerm.app", "PULSAR_STAGE_VIEWER": "split"}, "darwin")):
        assert LA.open_viewer(url, "s9", env, platform) == LA.INSTRUCTION
    assert not c.runs and not c.spawns


@pytest.mark.skipif(os.name == "nt", reason="POSIX executables")
def test_launcher_never_runs_a_helper_found_through_a_relative_path_entry(monkeypatch, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    marker = tmp_path / "PLANTED"
    for name in ("tmux", "zellij", "xdg-open", "gio", "open", "osascript", "env"):
        exe = proj / name
        exe.write_text("#!/bin/sh\necho ran > %s\n" % shlex.quote(str(marker)))
        exe.chmod(0o755)
    monkeypatch.chdir(str(proj))
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", "bin-rel"]))
    monkeypatch.setattr(LA, "has_terminal", lambda: True)
    ran = []
    monkeypatch.setattr(LA, "_run", lambda argv, timeout=4.0, detach=False: ran.append(list(argv)) or 0)
    monkeypatch.setattr(LA, "_spawn", lambda argv: ran.append(list(argv)) or True)
    for platform, env in (("linux", {"TMUX": "x", "ZELLIJ": "0", "DISPLAY": ":0"}), ("darwin", {})):
        LA.open_viewer("http://127.0.0.1:1/stage?t=x", "s1", env, platform)
    assert not marker.exists()
    for argv in ran:
        assert os.path.isabs(argv[0]) and not argv[0].startswith(str(proj)), argv
        if "split-window" in argv:
            assert shlex.split(argv[-1])[0] == "/usr/bin/env"


def test_launcher_kitty_without_socket_needs_remote_control(monkeypatch):
    c = Calls(codes=[1]).install(monkeypatch)
    out = LA.open_viewer("http://127.0.0.1:1/stage", "s", {"KITTY_WINDOW_ID": "1"}, "linux")
    assert c.runs == [["/usr/bin/kitty", "@", "ls"]] and out == LA.INSTRUCTION


def test_launcher_browser_and_fallbacks(monkeypatch):
    c = Calls().install(monkeypatch)
    url = "http://127.0.0.1:4747/stage?t=plv_x"
    assert LA.open_viewer(url, "s", {"DISPLAY": ":0"}, "linux") == "opened in your browser"
    assert c.spawns == [["/usr/bin/xdg-open", url]]
    assert LA.open_viewer(url, "s", {}, "linux") == LA.INSTRUCTION
    assert LA.open_viewer(url, "s", {"TMUX": "x", "PULSAR_STAGE_VIEWER": "browser", "DISPLAY": ":0"},
                          "linux") == "opened in your browser"
    c.runs.clear()
    none = LA.open_viewer(url, "s", {"TMUX": "x", "PULSAR_STAGE_VIEWER": "none"}, "linux")
    assert "PULSAR_STAGE_VIEWER=none" in none and not c.runs
    assert LA.open_viewer(url, "s", {"PULSAR_STAGE_VIEWER": "split", "DISPLAY": ":0"}, "linux") == LA.INSTRUCTION
    assert LA.choose({"TMUX": "1", "ZELLIJ": "1", "DISPLAY": ":0"}, "linux") == ["tmux", "zellij", "browser"]
    assert not LA.open_browser("https://evil.example/", {"DISPLAY": ":0"}, "linux")
    assert "plv_" not in LA.INSTRUCTION


def test_launcher_never_raises(monkeypatch):
    def boom(*a, **k):
        raise OSError("nope")
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(LA, "_which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(LA, "has_terminal", lambda: True)
    assert LA.open_viewer("http://127.0.0.1:1/stage", "s", {"TMUX": "1", "DISPLAY": ":0"}, "linux") == LA.INSTRUCTION
    monkeypatch.setattr(LA, "choose", boom)
    assert LA.open_viewer("u", "s", {}, "linux") == LA.INSTRUCTION


# ---------------------------------------------------------------------------
# terminal viewer (in-process)
# ---------------------------------------------------------------------------

class FakeCaps:
    def __init__(self, unicode=True, motion=True, color=24):
        self.unicode, self.motion, self.color, self.tty = unicode, motion, color, True
        self.out = io.StringIO()
        self.size = (100, 30)


def item(sc, sid="s1"):
    return {"sid": sid, "op": "show", "scene": sc, "t": time.time()}


def test_parse_args_and_usage(capsys):
    o = T.parse_args(["--sid", "s1", "--export", "/tmp/x.html"])
    assert o.sid == "s1" and o.do_export and o.export == "/tmp/x.html"
    o = T.parse_args(["--sid=s;rm", "--web", "--export"])
    assert o.sid == "srm" and o.web and o.do_export and o.export is None
    with pytest.raises(ValueError):
        T.parse_args(["--bogus"])
    with pytest.raises(ValueError):
        T.parse_args(["--sid"])
    assert T.cmd_stage(["--help"]) == 0 and "pulsarcode stage" in capsys.readouterr().out
    assert T.cmd_stage(["--nope"]) == 2


def test_viewer_events_follow_update_clear_and_keys():
    v = T.Viewer(FakeCaps())
    v._term_size = lambda: (100, 30)
    v._event("history", [item(DECK), item({"kind": "card", "body": "x", "id": "c1"})])
    assert v.cur == 1 and len(v.items) == 2
    v._key("[")
    assert v.cur == 0 and v.scene["kind"] == "deck"
    for _ in range(3):
        v._key("right")
    assert v.slide == 3
    v._key("end")
    assert v.slide == len(DECK["slides"]) - 1
    v._key("home")
    v._key("5")
    assert v.slide == 4
    v._key("f")                                 # pin
    assert not v.follow
    v._event("scene", item({"kind": "markdown", "body": "new", "id": "m1"}))
    assert v.cur == 0 and v.unseen == 1 and v.toasts
    v._event("scene", item(dict(DECK, id=v.scene["id"], slides=DECK["slides"][:6])))   # live update keeps place
    assert v.cur == 0 and v.slide == 4
    v._key("f")                                 # follow again: jump to newest
    assert v.follow and v.cur == len(v.items) - 1
    v._event("clear", {"sid": "s1", "op": "clear", "scene": {}})
    assert v.items == [] and v.cur == -1
    v._event("scene", {"bogus": True})
    v._event("scene", item({"kind": "chart"}))  # invalid scene is ignored, never raises
    assert v.items == []
    assert v._key("q") == "quit"


@pytest.mark.parametrize("size", [(40, 12), (100, 30), (160, 45), (20, 6)])
@pytest.mark.parametrize("unicode,motion,color", [(True, True, 24), (False, False, 0), (True, False, 8)])
def test_viewer_frames_render_everything(size, unicode, motion, color):
    v = T.Viewer(FakeCaps(unicode, motion, color))
    v._term_size = lambda: size
    v.feed.state = "nocore"
    cv = v._frame()                              # idle screen
    assert (cv.w, cv.h) == size
    v.feed.state = "live"
    v._event("history", [item(sc) for sc in [DECK] + SCENES])
    for idx in range(len(v.items)):
        v.cur, v.slide = idx, 0
        v.shown_at = time.time() - 0.3
        v._frame()
    v.cur, v.slide = 0, 1
    v.help, v.notes = True, True
    v._toast("exported ~/x.html", fx.OK)
    v._goto(1, 0, "scene")
    v.last_body = fx.Canvas(size[0], max(1, size[1] - 3))
    cv = v._frame()
    if not unicode:
        assert all(c[0].isascii() for row in cv.cells for c in row)


def test_viewer_shows_session_ids_without_control_characters():
    v = T.Viewer(FakeCaps())
    v._term_size = lambda: (120, 30)
    hostile = "op\x1b]52;c;SGVsbG8=\x07"
    v._event("history", [item({"kind": "card", "body": "x", "id": "c1"}, sid=hostile)])
    assert v.items[0]["sid"] == "op52cSGVsbG8"
    v._event("clear", {"sid": hostile, "op": "clear", "scene": {}})
    assert v.items == [] and "\x1b" not in v.toasts[-1][0] and "\x07" not in v.toasts[-1][0]
    v._event("history", [item({"kind": "card", "body": "x", "id": "c2"}, sid=hostile)])
    cv = v._frame()
    assert not [c for row in cv.cells for c in row if not c[0].isprintable()]


def test_web_key_never_puts_the_tokenized_link_on_the_clipboard(monkeypatch):
    v = T.Viewer(FakeCaps())
    monkeypatch.setattr(T, "web_url", lambda sid="": "http://127.0.0.1:1/stage?t=plv_secret")
    monkeypatch.setattr(LA, "open_browser", lambda url, env=None, platform=None: False)     # no GUI here
    v._key("w")
    written = v.caps.out.getvalue()
    assert "\x1b]52" not in written and "plv_secret" not in written       # clipboard managers keep what lands there
    assert "pulsarcode stage --web" in v.toasts[-1][0]


def test_scene_text_plain_rendering():
    txt = T.scene_text(normalize(DECK), 80, 0, True)
    assert "DECK" in txt and "\x1b" not in txt and "1 / 9" in txt and "One process" in txt
    colored = T.scene_text(normalize(SCENES[8]), 80, 24, False)
    assert "\x1b[" in colored and all(ord(c) < 128 for c in colored)


def test_feed_reports_missing_core(pulsar_home):
    f = T.Feed("s1").start()
    deadline = time.time() + 3
    while f.state != "nocore" and time.time() < deadline:
        time.sleep(0.02)
    f.stop.set()
    assert f.state == "nocore"


# ---------------------------------------------------------------------------
# live Core: --web/--export/--once in-process, the full viewer in a pty
# ---------------------------------------------------------------------------

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def live_core(pulsar_home):
    pytest.importorskip("uvicorn")
    import uvicorn
    from pulsar import __version__
    from pulsar.core import state as ST
    from pulsar.core.client import CoreClient
    from pulsar.core.server import make_app
    port = _free_port()
    admin = ST.new_token(ST.ADMIN_TOKEN_PREFIX)
    server = uvicorn.Server(uvicorn.Config(make_app(admin, port), host="127.0.0.1", port=port, log_level="error",
                                           lifespan="off"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    client = CoreClient("http://127.0.0.1:%d" % port, admin)
    deadline = time.time() + 15
    while not client.healthy() and time.time() < deadline:
        time.sleep(0.05)
    assert client.healthy()
    ST.write_core(ST.CoreInfo(port, os.getpid(), __version__, admin, time.time()))
    try:
        yield client
    finally:
        server.should_exit = True
        th.join(10)


def test_web_export_and_once_against_live_core(live_core, pulsar_home, capsys, monkeypatch):
    # the opener is recorded, never run: no browser opens on the machine running the tests
    opened = []
    monkeypatch.setattr(LA, "open_browser", lambda url, env=None, platform=None: opened.append(url) or False)
    live_core.post("/pulsar/stage", {"scene": SCENES[8], "sid": "sT"})
    live_core.post("/pulsar/stage", {"scene": dict(DECK, title=HOSTILE), "sid": "sOther"})
    assert T.cmd_stage(["--web", "--sid", "sT"]) == 0
    url = capsys.readouterr().out.strip()
    assert "/stage?t=plv_" in url and url.endswith("&sid=sT")
    assert opened == [url] and opened[0].startswith("http://127.0.0.1:")
    out = pulsar_home / "x.html"
    assert T.cmd_stage(["--export", str(out), "--sid", "sT"]) == 0
    html = out.read_text(encoding="utf-8")
    assert "kimi" in html and "alert(1)" not in html and "One router" not in html   # only sT scenes
    capsys.readouterr()
    assert T.cmd_stage(["--once", "--sid", "sT"]) == 0
    text = capsys.readouterr().out
    assert "kimi" in text and "OpenRouter" in text


@pytest.mark.skipif(os.name == "nt" or sys.platform.startswith("win"), reason="needs a POSIX pty")
@pytest.mark.parametrize("quit_with", [b"q", b"\x03"])
def test_viewer_runs_in_a_pty_against_live_core(live_core, pulsar_home, quit_with):
    import fcntl
    import pty
    import struct
    import termios
    live_core.post("/pulsar/stage", {"scene": dict(SCENES[8], title="Pulsar QA Table"), "sid": "sT"})
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    # the real HOME: PULSAR_HOME already isolates Pulsar, and a made-up HOME can make the system credential
    # store ask the person at the screen for a password
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.path.expanduser("~"), "TERM": "xterm-256color",
           "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "COLORTERM": "truecolor", "PULSAR_HOME": str(pulsar_home),
           "PYTHONPATH": ROOT, "PYTHONIOENCODING": "utf-8"}
    # make the pty our controlling terminal (as a real one is), so Ctrl-C arrives as SIGINT
    wrap = "import fcntl,os,sys,termios; fcntl.ioctl(0, termios.TIOCSCTTY, 0); os.execv(sys.argv[1], sys.argv[1:])"
    proc = subprocess.Popen([sys.executable, "-c", wrap, sys.executable, "-m", "pulsar", "stage", "--sid", "sT"],
                            stdin=slave, stdout=slave, stderr=slave, env=env, cwd=ROOT, start_new_session=True,
                            close_fds=True)
    os.close(slave)
    buf = b""
    try:
        deadline = time.time() + 25
        while b"Pulsar QA Table" not in buf and time.time() < deadline:
            r, _, _ = select.select([master], [], [], 0.2)
            if r:
                try:
                    buf += os.read(master, 65536)
                except OSError:
                    break
        assert b"Pulsar QA Table" in buf, buf[-2000:]
        assert b"\x1b[?1049h" in buf                      # alternate screen
        live_core.post("/pulsar/stage", {"scene": {"kind": "markdown", "title": "Second Live Scene", "body": "hi"},
                                         "sid": "sT"})
        deadline = time.time() + 10
        while b"Second Live Scene" not in buf and time.time() < deadline:
            r, _, _ = select.select([master], [], [], 0.2)
            if r:
                buf += os.read(master, 65536)
        assert b"Second Live Scene" in buf
        os.write(master, quit_with)
        deadline = time.time() + 10
        while proc.poll() is None and time.time() < deadline:
            r, _, _ = select.select([master], [], [], 0.2)
            if r:
                try:
                    buf += os.read(master, 65536)
                except OSError:
                    break
        assert proc.wait(timeout=5) == 0
        assert b"\x1b[?1049l" in buf and b"\x1b[?25h" in buf   # terminal restored
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(5)
        os.close(master)


def test_viewer_never_imports_from_the_project_directory(tmp_path):
    """A repo that ships its own pulsar/ must not run when the model opens a Stage split there."""
    import subprocess
    from pulsar.stage import launcher as L
    evil = tmp_path / "repo"
    (evil / "pulsar").mkdir(parents=True)
    sentinel = tmp_path / "PWNED"
    (evil / "pulsar" / "__init__.py").write_text("open(%r, 'w').write('x')\n" % str(sentinel))
    (evil / "pulsar" / "__main__.py").write_text("open(%r, 'w').write('x')\n" % str(sentinel))
    (evil / "json.py").write_text("open(%r, 'w').write('x')\n" % str(sentinel))
    argv = L.viewer_argv("s1")[:-3] + ["version"]          # same bootstrap, a command that exits at once
    env = {"PATH": "/usr/bin:/bin", "PULSAR_HOME": str(tmp_path / "home"), "PULSAR_FX": "off"}
    out = subprocess.run(argv, cwd=str(evil), env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and "pulsarcode" in out.stdout
    assert not sentinel.exists()


def test_scene_text_from_a_model_that_escapes_its_json_twice():
    # "- a\\n- b" on the wire: the two characters backslash and n where a line break was meant
    deck = normalize({"slides": [{"title": "T", "body": "Intro\\n- one\\n- two"}]})
    assert deck["slides"][0]["body"] == "Intro\n- one\n- two" and deck["slides"][0]["layout"] == "bullets"
    assert normalize({"kind": "markdown", "body": "a\\n\\nb"})["body"] == "a\n\nb"
    # a path is not a line break, text that has real line breaks is left as it is, and code is never touched
    assert normalize({"kind": "markdown", "body": "open C:\\new\\notes.txt"})["body"] == "open C:\\new\\notes.txt"
    assert normalize({"kind": "markdown", "body": "a\nb\\n- c"})["body"] == "a\nb\\n- c"
    assert normalize({"kind": "code", "code": {"text": 'print("a\\n- b")'}})["code"]["text"] == 'print("a\\n- b")'


def test_progress_states_as_other_tools_name_them():
    from pulsar.stage.schema import SceneError
    steps = normalize({"kind": "progress", "steps": [
        {"title": "a", "status": "completed"}, {"label": "b", "status": "in_progress"}, {"title": "c", "state": "todo"},
        {"title": "d", "status": "error"}, {"title": "e", "status": "odd"}, "f"]})["steps"]
    assert [(s["title"], s["status"]) for s in steps] == [
        ("a", "done"), ("b", "active"), ("c", "pending"), ("d", "failed"), ("e", "pending"), ("f", "pending")]
    with pytest.raises(SceneError, match="needs a title"):
        normalize({"kind": "progress", "steps": [{"status": "done"}]})
    with pytest.raises(SceneError, match="needs steps"):
        normalize({"kind": "progress", "title": "Build"})
    # a slide that embeds an empty visual is refused with the same message
    with pytest.raises(SceneError, match="needs steps"):
        normalize({"slides": [{"title": "T", "embed": {"kind": "progress"}}]})


def test_scene_text_drops_lone_surrogates_and_bidi_controls():
    import json
    from pulsar.stage import schema as SCH
    hostile = "deck" + chr(0xD83D) + " ok" + chr(0x202E) + "txe.exe" + chr(0x200B) + chr(0xFEFF)
    out = SCH.clean(hostile)
    assert out == "deck oktxe.exe"
    json.dumps({"t": out})                    # encodable: Core's JSON responses never fail on it
    scene = SCH.normalize({"kind": "card", "id": "c" + chr(0xDC00), "title": hostile, "body": "x"})
    json.dumps(scene)
