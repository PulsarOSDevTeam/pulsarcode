# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Terminal replays (tools/demo): renderer, recorder rhythm, scene direction and the shipped assets."""

import gzip
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "tools" / "demo"
sys.path.insert(0, str(DEMO))

import record  # noqa: E402
import svgterm  # noqa: E402

SCENES = ("boot", "uplink", "picker", "session", "stage", "top")
FAKE = "sk-or-v1-fake-key-not-real"
EM_DASH = chr(0x2014)
KEY_SHAPE = re.compile(r"(sk|gsk|nvapi|xai|hf|AIza|ghp|github_pat)[-_A-Za-z0-9]{12,}")


def scene(name):
    return json.loads((DEMO / "scenes" / (name + ".json")).read_text())


def frame(rows_text, cursor=None, t=0.0, colour="#d7d6e6"):
    return (t, [[(0, line, colour, False)] if line else [] for line in rows_text], cursor)


def meta(**kw):
    m = {"cols": 40, "rows": 6, "title": "pulsarcode", "alt": "a replay", "hold_last": 1.0}
    m.update(kw)
    return m


# ---------------------------------------------------------------------------
# recorder: humanised typing
# ---------------------------------------------------------------------------

def test_typing_is_seeded_and_deterministic():
    a = record.expand_typing([[1.0, "what does this project do?\r"]], seed=3)
    b = record.expand_typing([[1.0, "what does this project do?\r"]], seed=3)
    c = record.expand_typing([[1.0, "what does this project do?\r"]], seed=4)
    assert a == b and a != c
    assert a[0] == [1.25, "w"]                       # 250 ms before the first key
    assert [k for _t, k in a] == list("what does this project do?\r")


def test_typing_rhythm_rules():
    keys = record.expand_typing([[0.0, "the end.\r"]], seed=1)
    gaps = {k: round(b - a, 3) for (a, _), (b, k) in zip(keys, keys[1:])}
    assert gaps["h"] == 0.038                        # bigram "th"
    inter, enter = record.typing_intervals(keys)
    assert all(0.03 <= g <= 0.16 + 0.09 + 0.14 + 1e-9 for g in inter)
    assert enter == [pytest.approx(0.42)]
    period = [round(b - a, 3) for (a, _), (b, k) in zip(keys, keys[1:]) if k == "."][0]
    assert period >= 0.03 + 0.14 - 1e-9              # +140 ms before punctuation


def test_session_typing_reads_human():
    sc = scene("session")
    keys = record.expand_typing(sc["type"], record.scene_seed(sc))
    inter, enter = record.typing_intervals(keys)
    assert record.coefficient_of_variation(inter) >= 0.25
    assert enter and min(enter) >= 0.4
    assert sc.get("typed_keys") in (None, keys)      # written back by record_scene.py, never stale


def test_quantise_snaps_to_33ms_and_keeps_the_later_frame():
    f = [(0.0, [["a"]], None), (0.01, [["b"]], None), (0.04, [["c"]], None)]
    q = record.quantise(f)
    assert [round(t, 3) for t, *_ in q] == [0.0, 0.033]
    assert q[0][1] == [["b"]] and q[1][1] == [["c"]]


def test_json_schema_is_backwards_compatible():
    doc = json.loads(record.to_json([frame(["hi"], (2, 0)), frame(["hi!"], None, 0.5)], 40, 6,
                                    {"camera": [], "captions": [[0, 1, "x", "hi"]], "demo": True}))
    assert doc["cols"] == 40 and doc["rows"] == 6
    assert [f["t"] for f in doc["frames"]] == [0.0, 0.5]
    assert set(doc["frames"][0]) == {"t", "rows"}
    assert doc["cursor"] == [[2, 0], None]
    assert "camera" not in doc                       # empty optional keys are left out
    assert doc["demo"] is True and doc["captions"]


# ---------------------------------------------------------------------------
# renderer
# ---------------------------------------------------------------------------

def _svg(frames, **kw):
    return svgterm.to_svg(frames, meta(**kw))


def test_reduced_motion_block_is_last_and_disables_everything_first():
    svg = _svg([frame(["one"]), frame(["two"], t=0.5)])
    css = svg[svg.index("<style>") + 7:svg.index("</style>")]
    i = css.rindex("@media (prefers-reduced-motion: reduce){")
    assert css[i:].startswith("@media (prefers-reduced-motion: reduce){*{animation:none!important}")
    assert css.endswith("}}")
    assert "#f1{visibility:visible}" in css[i:]      # the last frame, nothing else


def test_no_scripts_filters_or_external_refs():
    svg = _svg([frame(["x"]), frame(["xy"], t=.4)], camera=[[0.1, 1.5, 5, 2, "SETTLE"]],
               captions=[[0.0, 0.5, "a caption", "x"]], enters=[0.2], demo=True)
    for bad in ("<script", "foreignObject", "filter", "@import", "url(http", "xlink:href"):
        assert bad not in svg
    assert not re.search(r"\son[a-z]+=", svg)
    assert EM_DASH not in svg


def test_brand_chrome_replaces_traffic_lights():
    svg = _svg([frame(["x"])], demo=True)
    assert "REPLAY" in svg and "DEMO" in svg
    for light in ("#ff5f57", "#febc2e", "#28c840"):
        assert light not in svg
    assert "DEMO" not in _svg([frame(["x"])])


def test_camera_compiles_to_one_track_and_caps_zoom():
    svg = _svg([frame(["x"]), frame(["y"], t=1)], camera=[[0.2, 1.0, 5, 2, "SETTLE"], [0.8, 1.6, 5, 2, "GLIDE"]])
    assert svg.count("@keyframes cam{") == 1
    assert "cubic-bezier(.65,0,.35,1)" in svg       # E_GLIDE into the 1.6 key
    with pytest.raises(ValueError):
        _svg([frame(["x"])], camera=[[0.2, 2.0, 5, 2, "SETTLE"]])


def test_camera_never_hides_an_error():
    rows = ["ok", "one", "two", "three", "four", "    error: upstream refused"]
    with pytest.raises(ValueError, match="hides"):
        _svg([frame(rows), frame(rows, t=1)], camera=[[0.1, 1.8, 0, 0, "SETTLE"]])
    _svg([frame(rows), frame(rows, t=1)], camera=[[0.1, 1.2, 12, 5, "SETTLE"]])


def test_camera_viewport_stays_inside_the_screen():
    lay = svgterm.Layout(40, 6, False)
    for col, row in ((0, 0), (39, 5), (20, 3)):
        tx, ty, z = lay.cam(1.8, col, row)
        x0, y0, x1, y1 = lay.viewport(tx, ty, z)
        assert x0 >= lay.scr_x - 1e-6 and y0 >= lay.scr_y - 1e-6
        assert x1 <= lay.scr_x + lay.scr_w + 1e-6 and y1 <= lay.scr_y + lay.scr_h + 1e-6


def test_caption_must_show_text_is_validated():
    frames = [frame(["hello"]), frame(["bye"], t=1.0)]
    _svg(frames, captions=[[0.0, 0.8, "greeting", "hello"]])
    with pytest.raises(ValueError, match="not on screen"):
        _svg(frames, captions=[[0.0, 1.2, "greeting", "hello"]])
    with pytest.raises(ValueError):
        _svg(frames, captions=[[0.0, 0.5, "greeting", ""]])


def test_captions_live_in_the_letterbox_outside_the_camera():
    svg = _svg([frame(["hello"])], captions=[[0.0, 0.5, "greeting", "hello", {"poster": True}]],
               camera=[[0.1, 1.2, 1, 1, "SETTLE"]])
    cam = svg.index('class="cam"')
    end_clip = svg.index("</g>", svg.index('class="pr"'))
    assert svg.index('class="cap cp0 capP"') > end_clip > cam
    assert ".cp0{opacity:1}" in svg[svg.rindex("@media"):]       # the poster caption stays for reduced motion


def test_cursor_rect_follows_the_recorded_cursor():
    svg = _svg([frame(["ab"], (2, 0)), frame(["abc"], (3, 0), t=0.1), frame(["abc"], None, t=1.0)])
    assert svg.count('class="cur"') == 1
    lay = svgterm.Layout(40, 6, False)
    for col in (2, 3):
        x, y = lay.cell(col, 0)
        assert "transform:translate(%spx,%spx)" % (svgterm._n(x), svgterm._n(y)) in svg
    assert "opacity:0" in svg[svg.index("@keyframes cur{"):]


def test_enter_thud_shakes_the_world_not_the_frame():
    svg = _svg([frame(["> hi"], (4, 0)), frame(["> hi", "ok"], (0, 2), t=0.6)], enters=[0.3], hold_last=2)
    assert "@keyframes shk{" in svg and 'class="rg rg0"' in svg
    shake = svg[svg.index("@keyframes shk{"):]
    assert "translate(-2px,1px)" in shake            # (-6, 3) x .33
    i = svg.index('<g clip-path="url(#scr)"><g class="shk">')
    assert svg.index('stroke="url(#pg)"') > i        # the window stroke is drawn outside the shaking group


def test_geometry_glow_is_opt_in_and_filter_free():
    rows = ["███"]
    plain = _svg([frame(rows, colour="#ff6a00")])
    glow = _svg([frame(rows, colour="#ff6a00")], glow=True)
    assert "paint-order" in glow and (' g"' in glow or 'class="g"' in glow)
    assert 'class="g"' not in plain and " g\"" not in plain
    assert "filter" not in glow


def test_rows_are_deduplicated_and_colours_compact():
    frames = [frame(["same", "a"]), frame(["same", "b"], t=.2), frame(["same", "a"], t=.4)]
    svg = _svg(frames)
    assert svg.count('<g id="r') == 3                # "same", "a", "b"
    assert svgterm.quantise_colour("#ecf6ff") == "#f0f8ff"
    assert re.search(r'<use href="#r', svg)


def test_limit_frames_keeps_first_and_last():
    fr = [(i / 10, [], None) for i in range(100)]
    out = svgterm.limit_frames(fr, 10)
    assert len(out) == 10 and out[0] is fr[0] and out[-1] is fr[-1]


# ---------------------------------------------------------------------------
# scene files
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", SCENES)
def test_scene_budget_and_frame_caps(name):
    sc = scene(name)
    assert sc["budget"] == {"svg_kb": 650, "gzip_kb": 120, "elements": 16000}
    assert sc.get("max_frames", 60) <= (48 if name in ("stage", "top") else 60)
    for cam in sc.get("camera") or []:
        assert cam[1] <= svgterm.MAX_ZOOM and str(cam[4]).upper() in ("GLIDE", "SETTLE")
    for cap in sc.get("captions") or []:
        assert cap[3], "every caption names the text it describes"
        assert EM_DASH not in cap[2]


def test_scene_direction_tags():
    assert scene("top").get("demo") is True
    assert any("synthetic" in c[2] for c in scene("top")["captions"])
    assert scene("uplink").get("demo") is True and scene("session").get("demo") is True
    assert scene("boot").get("glow") is True


def test_scenes_type_only_fake_keys():
    for name in SCENES:
        text = json.dumps(scene(name))
        for m in KEY_SHAPE.finditer(text):
            assert m.group(0) == FAKE, (name, m.group(0))
    assert any(FAKE in t for _a, t in scene("uplink")["type"])


# ---------------------------------------------------------------------------
# shipped assets: no GIFs or frame JSON in assets/demo, size budgets, posters and site frames
# ---------------------------------------------------------------------------

def test_no_gifs_or_frame_json_in_assets_demo():
    left = [p.name for p in (ROOT / "assets" / "demo").glob("*") if p.suffix in (".gif", ".json")]
    assert not left, left


@pytest.mark.parametrize("name", SCENES)
def test_replay_assets_within_budget(name):
    svg_path = ROOT / "assets" / "demo" / (name + ".svg")
    raw = svg_path.read_bytes()
    cap = 600 if name == "session" else 650
    assert len(raw) <= cap * 1024, "%s.svg %d KB" % (name, len(raw) // 1024)
    assert len(gzip.compress(raw, 9)) <= 120 * 1024
    svg = raw.decode()
    assert "REPLAY" in svg and "prefers-reduced-motion" in svg
    assert EM_DASH not in svg and "free Claude Code" not in svg
    for m in KEY_SHAPE.finditer(svg):
        assert m.group(0) == FAKE, m.group(0)
    if scene(name).get("demo"):
        assert ">DEMO<" in svg
    poster = ROOT / "assets" / "demo" / "posters" / (name + ".png")
    assert poster.exists() and poster.stat().st_size <= 90 * 1024
    assert (ROOT / "docs" / "demo" / "posters" / (name + ".png")).read_bytes() == poster.read_bytes()


def test_replays_total_size():
    total = sum(p.stat().st_size for p in (ROOT / "assets" / "demo").glob("*.svg"))
    assert total <= 3.6 * 1024 * 1024


@pytest.mark.parametrize("name", SCENES)
def test_site_frames_load(name):
    doc = json.loads((ROOT / "docs" / "demo" / (name + ".json")).read_text(encoding="utf-8"))
    assert doc["cols"] and doc["rows"] and doc["frames"]
    ts = [f["t"] for f in doc["frames"]]
    assert ts == sorted(ts)
    if "cursor" in doc:
        assert len(doc["cursor"]) == len(doc["frames"])
    for cap in doc.get("captions") or []:
        assert all(isinstance(v, (int, float)) for v in cap[:2])


# ---------------------------------------------------------------------------
# rendering cost, framing and the opening frame
# ---------------------------------------------------------------------------

def _css(svg):
    return svg[svg.index("<style>") + 7:svg.index("</style>")]


def test_holds_never_interpolate():
    kf = svgterm.keyframes([(0.0, "opacity:0"), (1.0, "opacity:0"), (1.3, "opacity:1")], 2.0, "linear")
    assert kf.startswith("0%{opacity:0;animation-timing-function:steps(1,end)}")
    assert "50%{opacity:0;animation-timing-function:linear}" in kf
    assert "65%{opacity:1;animation-timing-function:steps(1,end)}" in kf and kf.endswith("100%{opacity:1}")


def test_no_free_running_or_linear_loops():
    frames = [frame(["> hi"], (4, 0)), frame(["> hi", "ok"], (0, 2), t=0.6), frame(["> hi", "ok!"], (0, 2), t=2.0)]
    svg = _svg(frames, enters=[0.3], hold_last=2, camera=[[0.5, 1.0, 5, 2, "SETTLE"], [0.9, 1.3, 5, 2, "SETTLE"]],
               captions=[[0.7, 2.5, "a caption", "ok"]])
    css = _css(svg)
    assert "linear infinite" not in css               # the old continuous progress hairline
    assert "1.06s" not in css                         # the blink is written into the timeline
    pr = css[css.index("@keyframes pr{"):]
    assert "steps(1,end)" in pr[:pr.index("}}")]
    # every segment between two equal keyframes is a hold
    for name in ("cam", "dip", "shk", "cp0"):
        block = css[css.index("@keyframes %s{" % name):]
        block = block[:block.index("}}") + 2]
        keys = re.findall(r"([\d.]+%)\{([^;}]*(?:;[^a][^;}]*)*);animation-timing-function:([^;}]+)", block)
        vals = re.findall(r"[\d.]+%\{(.*?)(?:;animation-timing-function:[^}]*)?\}", block)
        for (pc, v, tf), nxt in zip(keys, vals[1:]):
            if v == nxt:
                assert tf == "steps(1,end)", (name, pc, v)


def test_camera_holds_in_fixed_groups_and_moves_per_frame():
    rows = ["x"] * 4
    frames = [frame(rows, t=0), frame(rows + ["a"], t=0.2), frame(rows + ["b"], t=0.6), frame(rows + ["c"], t=1.5)]
    svg = _svg(frames, rows=8, camera=[[0.1, 1.0, 1, 1, "SETTLE"], [0.7, 1.3, 1, 1, "SETTLE"]])
    css = _css(svg)
    assert re.search(r"\.z\d+\{transform:translate\([^)]*\) scale\(1\.\d+\)\}", css)   # the zoomed hold
    assert re.search(r"@keyframes q1\{", css) and re.search(r"@keyframes q2\{", css)  # frames inside the move
    assert ".cz{transform:none}" in css[css.rindex("@media"):] or ",.cz{transform:none}" in css[css.rindex("@media"):]


def test_edge_never_slices_a_word():
    lay = svgterm.Layout(40, 6, False)
    ink = svgterm._ink([frame(["PULSAR CODE" + " " * 9 + "v3.0.0"])], 40, 6)
    tx, ty, z = lay.cam(1.6, 3, 0)
    tx2, ty2, z2, cuts = svgterm.snap_camera(lay, 1.6, 3, 0, ink)
    assert cuts == 0 and svgterm.edge_cuts(lay, tx2, ty2, z2, ink) == 0
    x0, _y0, x1, _y1 = lay.viewport(tx2, ty2, z2)
    c0, c1 = (x0 - lay.ox) / svgterm.CW, (x1 - lay.ox) / svgterm.CW
    assert c0 <= 0 or c0 >= 11                        # never inside "PULSAR CODE" (one space binds it)
    assert c1 <= 20 or c1 >= 26


def test_unframeable_hold_fails_the_build():
    dense = ["word " * 8] * 6
    with pytest.raises(ValueError, match="slices"):
        _svg([frame(dense), frame(dense, t=1.0)], camera=[[0.1, 1.8, 20, 3, "SETTLE"], [0.9, 1.8, 20, 3, "SETTLE"]])


def test_lead_opens_on_the_last_frame():
    frames = [frame(["noise"]), frame(["composed"], t=0.5)]
    svg = _svg(frames, lead=0.5, hold_last=1.0)
    css = _css(svg)
    assert svg.count('class="f"') == 3                # the lead frame, then the recording
    assert "@keyframes k0{0%{visibility:visible}" in css
    total = float(re.search(r"\.f\{visibility:hidden;animation:([\d.]+)s", css).group(1))
    assert total == pytest.approx(0.5 + 0.3 + 0.5 + 1.0)
    dip = css[css.index("@keyframes dip{"):]
    assert "opacity:1" in dip[:dip.index("}}")]


def test_limit_frames_keeps_anchor_frames():
    fr = [(i / 10, [], None) for i in range(100)]
    out = svgterm.limit_frames(fr, 10, keep=[37, 38])
    assert len(out) == 10 and fr[37] in out and fr[38] in out and out[0] is fr[0] and out[-1] is fr[-1]


def test_crop_is_validated():
    sys.path.insert(0, str(DEMO / "scenes" / "fixtures"))
    import record_scene
    frames = [frame(["banner Claude Code v9", "", "body"], (0, 2))]
    out, rows, problems = record_scene.crop(frames, {"rows": 3, "crop": {"top": 2, "must_contain": "Claude Code v"}})
    assert not problems and rows == 1 and out[0][2] == (0, 0)
    _o, _r, problems = record_scene.crop(frames, {"rows": 3, "crop": {"top": 1, "must_contain": "nope"}})
    assert problems
    bad = [frame(["Claude Code v9 error: refused", "body"])]
    _o, _r, problems = record_scene.crop(bad, {"rows": 2, "crop": {"top": 1, "must_contain": "Claude Code v"}})
    assert problems and "error" in problems[0]


def test_session_replay_shows_no_cli_banner():
    sc = scene("session")
    assert sc["crop"]["must_contain"] == "Claude Code v"
    svg = (ROOT / "assets" / "demo" / "session.svg").read_text()
    assert "v2.1." not in svg and "API Usage Billing" not in svg
    doc = json.loads((ROOT / "docs" / "demo" / "session.json").read_text(encoding="utf-8"))
    assert not any("Claude Code v" in svgterm.frame_text(f["rows"]) for f in doc["frames"])


def test_pinclock_shim_is_a_dev_fixture():
    src = (DEMO / "scenes" / "fixtures" / "pinclock.c").read_text()
    assert "DEMO_PIN_EXE" in src and "CLOCK_REALTIME" in src
    assert "pinclock.c" in (DEMO / "make_all.sh").read_text()


# ---------------------------------------------------------------------------
# camera framing, the Enter ring, pinned clocks, demo labels, reproducible builds
# ---------------------------------------------------------------------------

def _record_scene():
    sys.path.insert(0, str(DEMO / "scenes" / "fixtures"))
    import record_scene
    return record_scene


def test_enter_ring_sits_on_the_prompt_glyph_and_is_short():
    # the CLI clears the typed line on Enter: the ring belongs to the prompt glyph, not the last typed cell
    svg = _svg([frame(["", "> what does this do?"], (20, 1)), frame(["", "> "], (2, 1), t=0.6)], enters=[0.3],
               hold_last=2)
    lay = svgterm.Layout(40, 6, False)
    cx, cy = lay.cell(0.5, 1.5)
    assert 'cx="%s" cy="%s" r="12"' % (svgterm._n(cx), svgterm._n(cy)) in svg
    css = _css(svg)
    assert "vector-effect:non-scaling-stroke" in css and "stroke:#ECF6FF" in css
    ring = css[css.index("@keyframes ro0{"):]
    ring = ring[:ring.index("}}") + 2]
    total = 0.6 + 2 + svgterm.DIP
    keys = [(float(p), float(o)) for p, o in re.findall(r"([\d.]+)%\{opacity:([\d.]+)", ring)]
    t_on = next(p for p, o in keys if o > 0)
    t_off = next(p for p, o in keys if o == 0 and p > t_on)
    assert (t_off - t_on) / 100 * total == pytest.approx(svgterm.RING_MS / 1000, abs=.01)
    assert svgterm.E_IMPACT in ring


def test_camera_must_not_frame_empty_space():
    rows = ["header one", "header two", "header three", "header four"] + [""] * 7 + ["> prompt"]
    frames = [frame(rows), frame(rows, t=1.0)]
    with pytest.raises(ValueError, match="empty space"):    # the old session push: prompt at the bottom of a void
        _svg(frames, rows=12, camera=[[0.1, 1.8, 6, 8, "SETTLE"], [0.9, 1.8, 6, 8, "SETTLE"]])
    lay = svgterm.Layout(40, 12, False)
    ink = svgterm._ink(frames, 40, 12)
    assert svgterm.row_fill(lay, 0, 0, 1.0, ink) == pytest.approx(5 / 12.0)


def test_snap_aligns_an_edge_to_fit_a_full_width_phrase():
    # a phrase from column 0 to column 30 fits a 31-cell-wide viewport only with both edges on boundaries
    lay = svgterm.Layout(40, 6, False)
    text = "a" + " b" * 15
    ink = svgterm._ink([frame([text, "", "", "", "", ""])], 40, 6)
    z = lay.scr_w / (31 * svgterm.CW)
    tx, ty, zz, cuts = svgterm.snap_camera(lay, z, 15, 2, ink)
    assert cuts == 0 and zz == pytest.approx(z, rel=.001)


def test_session_camera_follows_the_storyboard():
    cam = scene("session")["camera"]
    zooms = [k[1] for k in cam]
    assert max(zooms) == 1.25 and 1.1 in zooms                 # 1.10 at Enter, 1.25 on the status line
    status = [k for k in cam if k[1] == 1.25]
    assert all(k[0][0] == "end" for k in status)                # the status push is the last move
    assert cam[-1][1] == 1.0 and cam[-1][0] == ["end", scene("session")["hold_last"] + svgterm.DIP]
    assert "Claude Code CLI" in scene("session")["captions"][0][2]


def test_shipped_session_pushes_onto_the_status_line():
    svg = (ROOT / "assets" / "demo" / "session.svg").read_text()
    track = svg[svg.index("@keyframes cam{"):]
    track = track[:track.index("}}")]
    scales = [float(s) for s in re.findall(r"scale\(([\d.]+)\)", track)]
    assert max(scales) == pytest.approx(1.25, abs=.03) and max(scales) <= 1.25 + 1e-9
    assert all(s <= 1.1 + 1e-9 for s in scales if s < 1.2)    # never the old 1.3 push


def test_clock_validator_accepts_only_the_pinned_time():
    rs = _record_scene()
    ok = [frame(["done 9:41 AM", "LIVE 09:41:07", "127.0.0.1:54557", "v3.0.0"])]
    assert rs.clock_problems(ok) == []
    bad = [frame(["LIVE 03:36:24"])]
    assert rs.clock_problems(bad) and "03:36:24" in rs.clock_problems(bad)[0]


@pytest.mark.parametrize("name", SCENES)
def test_shipped_replays_show_only_the_pinned_clock(name):
    rs = _record_scene()
    doc = json.loads((ROOT / "docs" / "demo" / (name + ".json")).read_text(encoding="utf-8"))
    frames = [(f["t"], f["rows"], None) for f in doc["frames"]]
    assert rs.clock_problems(frames) == []
    svg = (ROOT / "assets" / "demo" / (name + ".svg")).read_text()
    for m in re.finditer(r">([^<]*\b\d\d:\d\d:\d\d\b[^<]*)<", svg):
        assert "09:4" in m.group(1), m.group(1)


def test_top_shows_cores_default_port():
    doc = json.loads((ROOT / "docs" / "demo" / "top.json").read_text(encoding="utf-8"))
    assert "core :4747" in svgterm.frame_text(doc["frames"][-1]["rows"])


def test_stage_is_labelled_demo_everywhere():
    sc = scene("stage")
    assert sc.get("demo") is True and "scripted" in sc["captions"][0][2]
    assert ">DEMO<" in (ROOT / "assets" / "demo" / "stage.svg").read_text()
    assert json.loads((ROOT / "docs" / "demo" / "stage.json").read_text(encoding="utf-8")).get("demo") is True
    doc = json.loads((ROOT / "docs" / "demo" / "stage.json").read_text(encoding="utf-8"))
    assert not any("Ask Claude" in svgterm.frame_text(f["rows"]) for f in doc["frames"])
    import screens
    for name in ("stage-deck", "stage-chart", "stage-diagram", "observatory"):
        assert screens.STILLS[name][3] is True


def test_make_all_posters_and_checks_follow_out():
    sh = (DEMO / "make_all.sh").read_text()
    assert '--src "$OUT"' in sh
    assert 'if [ -f "$REPO/tools/brand/check.py" ]; then' in sh
    assert "recording it once more" in sh and "CORE_DEFAULT_PORT=4747" in sh
    assert "DEMO_PIN_ORIGIN" in sh and "DEMO_PIN_ORIGIN" in (DEMO / "scenes" / "fixtures" / "pinclock.c").read_text()


def test_fit_keeps_headroom_under_the_budget():
    rs = _record_scene()
    assert rs.HEADROOM <= 0.95
    for name in SCENES:
        assert (ROOT / "assets" / "demo" / (name + ".svg")).stat().st_size <= 650 * 1024 * rs.HEADROOM + 1


def test_recorder_stamps_frames_on_the_fps_grid_and_spawns_on_text(tmp_path):
    pytest.importorskip("pyte")
    rs = _record_scene()
    flag = tmp_path / "spawned"
    sc = {"cols": 20, "rows": 3, "cmd": ["sh", "-c", "sleep .3; printf READY; sleep 2"], "duration": 1.2,
          "fps": 10, "spawn": [["READY", 0.1, "touch '%s'" % flag]]}
    frames, _sent, problems = rs.record_synced(sc)
    assert not problems and flag.exists()
    for t, _rows, _cur in frames:
        assert abs(t * 10 - round(t * 10)) < 1e-6, t


def test_stage_opens_on_a_fully_decoded_title():
    # frame 0 (and every loop restart) must be legible: the title slide's decode glitch is trimmed off
    sc = scene("stage")
    doc = json.loads((ROOT / "docs" / "demo" / "stage.json").read_text(encoding="utf-8"))
    first = svgterm.frame_text(doc["frames"][0]["rows"])
    assert sc["start_at"] in first
    body = "\n".join(first.split("\n")[2:-1])
    assert not any(c in body for c in "◢◣◤◥▚▞▙▟▒░▓╱╲╳")
