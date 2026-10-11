# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Brand generators (tools/brand): one clock from fx, computed counts, budgets, validator rules."""

import json
import math
import random
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "brand"))

import check  # noqa: E402
import hero  # noqa: E402
import mark  # noqa: E402
import motion as M  # noqa: E402
import readme_art as RA  # noqa: E402
import svgkit as K  # noqa: E402

from pulsar import fx  # noqa: E402
from pulsar import providers as P  # noqa: E402
from pulsar.screens import advertised_providers  # noqa: E402


# -- clock and easing ---------------------------------------------------------------------

def test_clock_comes_from_fx_beams():
    a0, a1 = fx.Beams.angles(0), fx.Beams.angles(1)
    assert M.P1 == pytest.approx(2 * math.pi / abs(a1[0] - a0[0]))
    assert M.P2 == pytest.approx(2 * math.pi / abs(a1[1] - a0[1]))
    assert M.A1_0 == pytest.approx(math.degrees(a0[0]))
    assert M.A2_0 == pytest.approx(math.degrees(a0[1]))
    assert M.L == pytest.approx(2 * M.P1) and M.Q == pytest.approx(M.P1 / 4)


@pytest.mark.parametrize("os_, ms_, w", [(.12, 620, 15.0), (.03, 520, 11.0)])
def test_spring_has_exactly_one_visible_overshoot(os_, ms_, w):
    pts = M.spring_points(os_, ms_, w)
    assert len(pts) == 32 and pts[0] == 0 and pts[-1] == 1
    assert max(pts) == pytest.approx(1 + os_, abs=.01)
    # after the peak the curve never dips more than OS^2 (+ rounding) below 1: no second visible bounce
    peak = pts.index(max(pts))
    assert min(pts[peak:]) >= 1 - os_ ** 2 - .006
    assert M.damping_for(.12) == pytest.approx(.559, abs=.001)


def test_spring_css_declares_bezier_first():
    css = M.timing("spring")
    assert css.index("cubic-bezier(.34,1.56,.64,1)") < css.index("linear(")


def test_shock_ease_matches_overlay_ease_out():
    for p in (.1, .25, .5, .75):
        assert M.bezier_y(M.E_SHOCK, p) == pytest.approx(M.ease_out(p), abs=.02)


# -- computed truth -----------------------------------------------------------------------

def test_provider_groups_are_computed_and_complete():
    g = M.provider_groups()
    assert sum(len(v) for v in g.values()) == len(advertised_providers())
    assert all(p.local for p in g["local"]) and all(p.route == "native" for p in g["native"])
    assert all(p.route == "cloud" for p in g["cloud"])


def test_readout_entries_resolve_by_longest_prefix():
    for e in M.readout_entries():
        assert P.get(e["id"]) is not None
        if e["keyless"]:
            # a reader who types what the hero shows gets that engine (detect() is offline)
            d = P.detect(e["prefix"])
            assert d.kind == "local" and d.candidates[0].id == e["id"]
        else:
            assert P._by_prefix(e["prefix"] + "x" * 10).id == e["id"]
    assert "localhost" not in hero.build("dark") and ">ollama<" in hero.build("dark")
    assert M.readout_entries()[0]["id"] == "openrouter"


def test_providers_json_is_safe_and_ordered():
    pj = M.providers_json()
    assert pj["count"] == len(advertised_providers())
    lens = [len(p[0]) for p in pj["prefixes"]]
    assert lens == sorted(lens, reverse=True)
    text = json.dumps(pj)
    assert "http" not in text and "claude" not in [p[1] for p in pj["prefixes"]]
    assert all(note.endswith(".") and "http" not in note for _, note in pj["reject"])
    assert pj["ambiguous"] == ["sk-"]


def test_tokens_json_is_current():
    assert M.TOKENS_PATH.read_text(encoding="utf-8") == M.tokens_json()


# -- hero ---------------------------------------------------------------------------------

def test_hero_is_deterministic_and_within_budget():
    a, b = hero.build("dark"), hero.build("dark")
    assert a == b
    for theme in ("dark", "light"):
        svg = hero.build(theme)
        assert len(svg.encode()) <= 110 * 1024 and K.gz_size(svg) <= 16 * 1024
        assert svg.count('class="cell ') == len(M.wordmark_cells()) * hero.HIT_STRIPS == 130 * hero.HIT_STRIPS
        assert hero.build(theme, anim=False).count('class="cell ') == 130
        assert not check.check_text("assets/brand/hero-%s.v3.svg" % theme, svg, M.provider_count())


def test_hero_cells_use_forwards_only_and_reduced_motion_is_last():
    svg = hero.build("dark")
    css = check.css_blocks(svg)
    cell_rules = [b for s, b in re.findall(r"([^{}]*)\{([^{}]*)\}", css) if re.search(r"\.cell\b", s) and "animation" in b]
    assert cell_rules and all("forwards" in b and "both" not in b and "backwards" not in b for b in cell_rules)
    assert check._reduced_motion_ok(css) is None
    assert css.rstrip().endswith("}}") and ".cell{opacity:1}" in css[css.rfind("@media"):]


def test_ignition_flash_is_local_not_a_flood():
    """No full flood and no letter wash above .10: the ignition flash (S1) is a radial lift on the core."""
    for theme in ("dark", "light"):
        svg = hero.build(theme, hero.README, True)
        ign = re.search(r'<g class="fx0 ign">(.*?)</g>', svg).group(1)
        assert "<rect" not in ign
        assert 'fill="url(#flashv)"' in ign
        grad = re.search(r'<radialGradient id="flashv"[^>]*>(.*?)</radialGradient>', svg).group(1)
        stops = [(float(o), float(a)) for o, a in re.findall(r'offset="([\d.]+)"[^>]*stop-opacity="([\d.]+)"', grad)]
        assert stops[-1][1] == 0
        d = (hero.README.ox - hero.README.cx) / hero.IGN_R     # nearest letter edge, as a gradient offset
        lo = max(s for s in stops if s[0] <= d)
        assert lo[1] <= .10
        assert hero.IGN_BLOOM < hero.README.ox - hero.README.cx  # the core bloom stops before the wordmark


def test_poster_names_follow_one_rule():
    assert K.themed_name("signal", "dark", True) == "signal-dark-static.v3.svg"
    assert K.themed_name("hero", "light", True) == "hero-static-light.v3.svg"
    assert K.parse_themed("signal-static-dark.v3.svg") == ("signal", "dark", True, "v3")
    pic = K.picture("assets/readme/card-stage", "x")
    assert 'srcset="assets/readme/card-stage-dark-static.v3.svg"' in pic
    assert 'srcset="assets/brand/hero-static-dark.v3.svg"' in K.picture("assets/brand/hero", "x")
    for p in list(RA.outputs()) + list(hero.outputs()):
        stem, theme, static, v = K.parse_themed(p.name)
        assert p.name == K.themed_name(stem, theme, static, v)
    errs = check.poster_names(["assets/readme/signal-dark.v3.svg", "assets/readme/signal-static-dark.v3.svg"])
    assert any("should be signal-dark-static.v3.svg" in e for e in errs)
    assert any("missing poster assets/readme/signal-light-static.v3.svg" in e for e in errs)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for src in re.findall(r'srcset="(assets/(?:brand|readme)/[^"]+-static[^"]*)"', readme):
        assert (ROOT / src).is_file(), src


def test_static_hero_has_no_animation_and_fits():
    for theme in ("dark", "light"):
        svg = hero.build(theme, anim=False)
        assert "@keyframes" not in svg and "<animate" not in svg and "animation" not in svg
        assert len(svg.encode()) <= 50 * 1024


def test_strike_times_follow_the_shockwave():
    cx, cy = hero.README.cx, hero.README.cy
    ts = []
    for x, y in M.wordmark_cells():
        mx = hero.README.ox + (x + .5) * hero.README.cw
        my = hero.README.oy + (y + .5) * hero.README.ch
        ts.append(hero.strike_time(math.hypot(mx - cx, my - cy)))
    assert .36 <= min(ts) <= .38 and .70 <= max(ts) <= .73
    assert max(ts) + .62 * .1 < .75 + .1   # last cell struck (white-hot, opacity 1) before 750 ms


def test_pulse_is_phase_locked_to_the_pen():
    lo = hero.README
    assert lo.theta_pen() == pytest.approx(math.degrees(math.atan2(412 - 190, 1180 - 250)))
    t = lo.first_pulse()
    # beam 1's north end points at theta_pen at the first pulse
    assert (M.beam1_angle(t) - lo.theta_pen()) % 360 == pytest.approx(0, abs=1e-6)
    f = (t - M.CLOCK0) / M.P1
    main = (lo.tele["pen"] + hero.PAPER * f) % hero.PAPER
    # paper offset at t puts a main spike exactly under the pen
    s = hero.PAPER * f
    assert hero.trace_profile(lo.tele["pen"] + s, main) == pytest.approx(1.0, abs=1e-3)


def test_inline_hero_is_prefixed_without_prolog():
    svg = hero.site_outputs()[hero.SITE / "hero-inline-dark.svg"]
    assert not svg.startswith("<?xml")
    ids = re.findall(r'\sid="([^"]+)"', svg)
    assert ids and all(i.startswith("h-") for i in ids)
    assert all(c.startswith("h-") for cls in re.findall(r'\sclass="([^"]+)"', svg) for c in cls.split())
    assert all(k.startswith("h-") for k in re.findall(r"@keyframes\s+([\w-]+)", svg))


def test_social_counts_are_computed():
    html = hero.social_overlay_html()
    n = len(advertised_providers())
    assert M.count_phrase(hero.SOCIAL_UNIT) in html and "<span>+%d</span>" % (n - len(hero.SOCIAL_CHIPS)) in html
    assert M.count_phrase() == "%d provider profiles in v%s" % (n, __import__("pulsar").__version__)
    names = {p.name for p in advertised_providers()}
    assert all(c in names for c in hero.SOCIAL_CHIPS)


def test_social_text_hangs_from_the_wordmark_axis():
    svg = hero.build("dark", hero.SOCIAL, True)
    xs = re.findall(r'<text x="([\d.]+)" y="(?:383|426)"', svg)
    assert xs and all(float(x) == hero.SOCIAL.ox for x in xs)
    assert "text-anchor=\"middle\"" not in hero.social_text(M.DARK, hero.SOCIAL)
    assert "left:%dpx" % hero.SOCIAL.ox in hero.SOCIAL_CSS


def test_hero_kick_is_rigid_per_letter():
    svg = hero.build("dark", hero.README, True)
    letters = re.findall(r'<g class="lt" style="animation-delay:[\d.]+s;--kx:[-\d.]+px;--ky:[-\d.]+px">', svg)
    assert len(letters) == 2 * len(M.wordmark_letters()) == 12          # cells + shadows
    assert "--kx" not in "".join(re.findall(r'<rect [^>]*class="cell[^>]*>', svg))
    assert sum(len(l_) for l_ in M.wordmark_letters()) == len(M.wordmark_cells())
    # the kick returns to 0 0, so it needs no fill mode (a finished animation costs nothing)
    assert ".lt{animation:kick .7s linear}" in svg


def test_hero_hit_keys_on_the_light_plate():
    assert hero.HIT_KEYS["light"] == (6.0, 35.0)
    assert "6%{fill:#1B1530}35%{fill:#3A2A66" in hero.build("light", hero.README, True)


def test_light_hero_needs_no_blending_on_the_beams():
    svg = hero.build("light", hero.README, True)
    assert ".add{mix-blend-mode" not in svg and 'id="grid"' in svg


# -- README art ---------------------------------------------------------------------------

def test_footer_decay_is_baked_into_the_paper():
    env = RA.footer_envelope()
    assert env[0] == (0.0, 1.0) and env[-1] == (100.0, 0.0)
    assert all(a1 <= a0 for (_, a0), (_, a1) in zip(env, env[1:]))
    assert RA.footer_amp(2 * M.P1) == 0 and RA.footer_amp(-1) == 1
    # geometric: ratio FOOTER_DECAY per half rotation before the normalisation to 0
    r = RA.FOOTER_DECAY ** 4
    assert (RA.footer_amp(M.H) + r / (1 - r)) / (1 + r / (1 - r)) == pytest.approx(RA.FOOTER_DECAY, rel=1e-6)
    svg = RA.footer("dark")
    # nothing rescales the recording: the strip only translates, the seam is a cross-fade of two copies
    assert "scale" not in check.css_blocks(svg).split("@media")[0]
    assert re.search(r"@keyframes pa\{0%\{translate:0 0;opacity:1\}", svg) and "@keyframes pb{" in svg
    assert "-%spx 0;opacity:0}" % K.n(4 * RA.hero.PAPER) in svg
    static = RA.footer("dark", False)
    assert "@keyframes" not in static and 'class="pb"' not in static


def test_footer_strip_spikes_only_shrink_at_the_pen():
    pen, spike = 1180.0, 26.0
    main = (pen + RA.FOOTER_PHASE * RA.hero.PAPER) % RA.hero.PAPER
    d = RA.footer_strip(main, spike, 1120.0, pen, 2000.0)
    # walk the relative path: left of the pen full height, then decaying, then a flat line
    x, y, pts = 0.0, 0.0, []
    for cmd, a, b in re.findall(r"([Ml])(-?[\d.]+) (-?[\d.]+)", d):
        x, y = (float(a), float(b)) if cmd == "M" else (x + float(a), y + float(b))
        pts.append((x, y))
    peak = lambda x0, x1: -min(yy for xx, yy in pts if x0 <= xx < x1)  # noqa: E731
    v = RA.hero.PAPER
    # main spikes right of the pen (one per paper period) get smaller, geometrically
    mains = [peak(pen + 40 + k * v - 20, pen + 40 + k * v + 20) for k in range(2)]
    assert mains[0] > 15 and mains[1] < mains[0] * .5
    assert d.endswith("H2000") and pts[-1] == (pytest.approx(pen + 2 * v), pytest.approx(0, abs=.06))


def test_stage_bars_start_empty_every_loop():
    svg = RA.card_stage("dark")
    assert not re.search(r'class="br[^"]*" style="animation-delay', svg)
    for k in range(7):
        assert re.search(r"@keyframes br%d\{0%%,[\d.]+%%\{scale:1 0" % k, svg)
    assert "illustrative, tokens/s" in svg and "sample session" not in svg


def test_signal_counter_and_loop_seam():
    svg = RA.signal("dark")
    assert ">1 tokens<" not in svg and ">1 token<" in svg
    # one fade for every dynamic element, from 9.6 s to the loop end, and nothing pops at the wrap
    assert re.search(r"@keyframes xf\{0%,95\.[\d]+%\{opacity:1;animation-timing-function:cubic-bezier\(\.16,1,\.3,1\)\}100%\{opacity:0\}\}", svg)
    for cls in ("kp", "kb", "dt", "t1e", "t1c", "t2c", "st", "ct", "tl", "ph"):
        assert re.search(r'<g class="xf">(?:(?!</g></g></g>).)*class="%s"' % cls, svg, re.S), cls
    # the timeline axis and labels are chrome: never inside a fading group
    axis = re.search(r'<text[^>]*>STREAM</text>', svg).start()
    assert svg.rfind('<g class="xf">', 0, axis) < svg.rfind("</g>", 0, axis)
    # the stream fills 3.0 to 7.8 s: the last token lands at 7.8 s and "hold" is ticked there
    assert ">HOLD<" in svg and RA.STREAM == (3.0, 7.8)


def test_observatory_wires_come_from_the_code():
    svg = RA.card_observatory("dark")
    for pid in ("openrouter", "groq", "ollama"):
        assert M.wire_label(M.provider(pid)).replace(" wire", "") in svg
    assert ">local</text>" not in svg
    assert "stroke-dasharray" not in svg.split("tokens/s", 1)[1].split("SESSION", 1)[0]


def test_pilot_light_button_contrast():
    svg = RA.card_pilot("light")
    assert re.search(r'fill="#6B3FD1"/><text[^>]*fill="#FFFFFF"[^>]*>Deploy', svg)
    assert M.contrast("#6B3FD1", "#FFFFFF") >= 4.5
    assert 'class="tr"' not in svg and 'class="tls"' in svg


def test_constellation_orbits_are_named():
    svg = RA.constellation("dark")
    for name in RA.RING_NAMES.values():
        assert ">%s</text>" % name in svg
    assert M.count_phrase() in svg


def test_dividers_are_offset_by_a_quarter_turn():
    a, b = RA.divider(1, "dark"), RA.divider(2, "dark")
    d1 = float(re.search(r"\.pp\{animation:pp [\d.]+s linear (-?[\d.]+)s", a).group(1))
    d2 = float(re.search(r"\.pp\{animation:pp [\d.]+s linear (-?[\d.]+)s", b).group(1))
    assert (d1 - d2) == pytest.approx(M.Q, abs=.002)
    assert (d1 - d2) / M.P1 * hero.PAPER == pytest.approx(40, abs=.1)


def test_constellation_labels_never_overlap_and_stay_inside():
    nodes = RA.constellation_layout()
    assert len(nodes) == len(advertised_providers())
    boxes = [nd["label"] for nd in nodes]
    for i, a in enumerate(boxes):
        assert a[0] >= 0 and a[2] <= RA.C_W and a[1] >= 0 and a[3] <= RA.C_H
        for b in boxes[i + 1:]:
            assert not RA._overlap(a, b, 0)


def test_constellation_flares_exactly_where_the_beam_is():
    nodes = RA.constellation_layout()
    win = RA.FLARE_WINDOW / math.degrees(abs(M.W1))
    hold = 2 * win
    rng = random.Random(5)
    for _ in range(200):
        t = rng.uniform(0, 30)
        beam = (M.A1_0 + math.degrees(M.W1) * t) % 180
        for nd in nodes:
            delay = RA.flare_time(nd["angle"]) - win - M.H
            phase = (t - delay) % M.H
            full = phase < hold - 1e-6
            diff = abs(((nd["angle"] % 180) - beam + 90) % 180 - 90)
            if abs(diff - RA.FLARE_WINDOW) > .05:
                assert full == (diff < RA.FLARE_WINDOW), (nd["name"], t, diff)


def test_constellation_counts_and_site_variant():
    n = len(advertised_providers())
    svg = RA.constellation("dark")
    assert ">%d</text>" % n in svg and "provider profiles</text>" in svg and "%d provider profiles in v" % n in svg
    site = RA.constellation("dark", site=True)
    assert site.count("data-angle=") == n and "@keyframes" not in site
    assert all(i.startswith("c-") for i in re.findall(r'\sid="([^"]+)"', site))


def test_pilot_ripple_radius_matches_the_overlay():
    k = RA.PILOT_SCALE
    r = (10 + 58 * M.bezier_y(M.E_SHOCK, .5)) * k
    assert r == pytest.approx(60.75 * k, abs=2)
    assert M.ripple_radius(.5) == pytest.approx(60.75)


def test_cards_label_synthetic_content_and_fit():
    total = 0
    for name, fn in RA.CARD_BUILDERS.items():
        svg = fn("dark")
        assert len(svg.encode()) <= 45 * 1024
        total += len(svg.encode())
        assert "@keyframes" not in fn("dark", False)
    assert total <= 160 * 1024
    assert "DEMO DATA" in RA.card_observatory("dark") and "DEMO DATA" in RA.card_observatory("dark", False)
    assert "ILLUSTRATIVE" in RA.card_council("dark") and "ILLUSTRATIVE" in RA.card_council("light", False)
    council = RA.card_council("dark")
    assert "model A" in council and "model B" in council and "model C" in council
    assert "ILLUSTRATIVE" in RA.signal("dark") and "TIME COMPRESSED" in RA.signal("dark", False)


def test_signal_wires_come_from_the_engine():
    assert RA.engine_wires() == ["chat", "responses", "native"]


# -- mark ---------------------------------------------------------------------------------

def test_mark_is_two_rays_and_a_ring():
    svg = mark.mark_svg()
    assert svg.count("<path") == 4                 # two ring halves + two wedges, nothing radiating
    assert "url(#mn)" in svg and "url(#ms)" in svg  # the two beam ends differ in colour
    assert len(svg.encode()) <= 3 * 1024 and len(mark.favicon_svg().encode()) <= 1536
    assert "prefers-color-scheme" in mark.favicon_svg() and "@keyframes" not in mark.favicon_svg()
    assert len(mark.logo_svg().encode()) <= 8 * 1024


# -- validator ----------------------------------------------------------------------------

BAD = {
    "script": '<svg xmlns="http://www.w3.org/2000/svg"><script>1</script></svg>',
    "handler": '<svg xmlns="http://www.w3.org/2000/svg"><rect onload="x()"/></svg>',
    "external": '<svg xmlns="http://www.w3.org/2000/svg"><image href="https://e.com/a.png"/></svg>',
    "em": '<svg xmlns="http://www.w3.org/2000/svg"><text>a ' + chr(0x2014) + ' b</text></svg>',
    "key": '<svg xmlns="http://www.w3.org/2000/svg"><text>sk-or-v1-0123456789abcdef</text></svg>',
    "count": '<svg xmlns="http://www.w3.org/2000/svg"><text>37 providers</text></svg>',
    "phrase": '<svg xmlns="http://www.w3.org/2000/svg"><text>free Claude Code</text></svg>',
    "no-rm": '<svg xmlns="http://www.w3.org/2000/svg"><style>@keyframes a{to{opacity:0}}</style></svg>',
}


@pytest.mark.parametrize("kind", sorted(BAD))
def test_validator_rejects(kind):
    assert check.check_text("assets/readme/x.svg", BAD[kind], M.provider_count())


def test_validator_static_and_cell_rules():
    anim = '<svg><style>@keyframes a{to{opacity:0}}@media (prefers-reduced-motion: reduce){*{animation:none!important}}</style></svg>'
    assert not check.check_text("assets/readme/x.svg", anim, 36)
    assert check.check_text("assets/readme/x-static.v3.svg", anim, 36)
    cell = anim.replace("<style>", "<style>.cell{animation:hit .6s both}")
    assert check.check_text("assets/brand/hero-dark.v3.svg", cell, 36)
    late = anim.replace("</style>", ".x{opacity:1}</style>")
    assert check.check_text("assets/readme/x.svg", late, 36)
    fake = '<svg><text>%s</text></svg>' % M.FAKE_KEY
    assert not check.check_text("assets/readme/x.svg", fake, 36)


def test_committed_brand_assets_pass_the_validator():
    failures, table = check.check(["assets/brand", "assets/readme", "docs/assets/brand"])
    assert not failures, failures
    assert len(table) > 40


def test_committed_vector_assets_are_up_to_date():
    for group in (hero.outputs(), hero.site_outputs(), RA.outputs(), RA.site_outputs(), mark.svg_outputs()):
        for path, svg in group.items():
            assert path.read_text(encoding="utf-8") == svg, "%s is stale: run tools/brand/make_assets.py" % path
    for legacy in ("hero.svg", "hero-dark.svg", "hero-light.svg"):
        assert not (ROOT / "assets" / "brand" / legacy).exists()


def test_constellation_ring_names_sit_on_one_clear_spoke():
    nodes = RA.constellation_layout()
    rl = RA.ring_labels(nodes)
    angles = set()
    for g, (x, y, box) in rl.items():
        rx, ry = RA.RING_RX[g], RA.RING_RX[g] * RA.RING_RATIO
        cx, cy = RA.C_CORE
        angles.add(round(math.degrees(math.atan2((y - 3.2 - cy) / ry, (x - cx) / rx)), 3))
        pad = RA.RING_PAD - 1e-6
        for nd in nodes:
            assert not RA._overlap((box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad), nd["label"], 0), (g, nd["name"])
    assert len(angles) == 1
    svg = RA.constellation("dark")
    assert 'mask="url(#om)"' in svg and svg.count("<ellipse") == 4


def test_stage_flip_folds_then_unfolds_with_the_counter_at_the_swap():
    svg = RA.card_stage("dark")
    css = check.css_blocks(svg)
    s1 = re.search(r"@keyframes s1\{([^@]*?)\}\}", css).group(1)
    s2 = re.search(r"@keyframes s2\{([^@]*?)\}\}", css).group(1)
    pn = re.search(r"@keyframes pn\{([^@]*?)\}\}", css).group(1)
    swap = K.pct((RA.STAGE_FLIPS[0] + RA.STAGE_FLIP) / M.L * 100)
    assert "%s{opacity:1;scale:0 1" % swap in s1 and "%s{opacity:1;scale:0 1;" % swap in s2
    assert "%s{translate:0 -16px" % swap in pn and M.E_SPRING in s2
    # the pane is docked at frame 0 and the border is drawn above the clipped content
    assert "animation:pane %s linear %s" % (K.ms(M.L), K.ms(-RA.STAGE_LEAD)) in css
    assert re.search(r'</g><rect x="\.5" y="\.5" width="599" height="339" rx="14" fill="none"', svg)


def test_pilot_types_bullets_and_answers_keys_like_the_overlay():
    svg = RA.card_pilot("dark")
    assert M.BULLET * 24 in svg and "steps(24,end)" in svg
    assert ">saved</text>" in svg and "@keyframes kp{" in svg
    # the overlay clears the chip on a key op: there is no KEY chip, only keycaps and the pulse
    assert ">KEY</text>" not in svg
    static = RA.card_pilot("dark", False)
    assert ".fb{opacity:0}" in static


def test_observatory_breaker_is_per_target_and_drops_throughput():
    svg = RA.card_observatory("dark")
    assert "groq|%s" % M.default_model(M.provider("groq")) in svg
    assert '<text x="290" y="242" font-size="12" fill="#9692AC" class="bo">0</text>' in svg
    assert "SESSION" not in svg and "DEMO DATA" in svg
    # provider names are ink (route colour only on the dot): red-family text is status only
    for name in ("openrouter", "groq", "ollama"):
        assert re.search(r'fill="%s">%s</text>' % (M.ICE, name), svg)


def test_hero_paper_starts_blank_and_inks_from_the_pen():
    svg = hero.build("dark")
    assert 'mask="url(#rvm)"' in svg and "@keyframes rv{from{translate:0 0}}" in svg
    rv = re.search(r"\.rv\{translate:-([\d.]+)px 0;animation:rv ([\d.]+)s linear ([\d.]+)s backwards\}", svg)
    travel, dur, delay = (float(v) for v in rv.groups())
    assert travel / dur == pytest.approx(hero.PAPER / M.P1, rel=1e-3) and delay == pytest.approx(hero.PEN_DOWN, abs=1e-3)
    assert "rvm" not in hero.build("dark", anim=False)


def test_bloom_streak_fades_inside_the_frame():
    svg = hero.build("dark")
    g = re.search(r'<linearGradient id="ana" gradientUnits="userSpaceOnUse" x1="([\d.-]+)" y1="0" x2="([\d.]+)"', svg)
    x1, x2 = float(g.group(1)), float(g.group(2))
    assert x1 > 0 and x2 < hero.README.cx + hero.STREAK_RX
    assert '<g class="fx0 pl add">' in svg


def test_posterize_is_deterministic_and_does_not_diffuse():
    Image = pytest.importorskip("PIL.Image")   # dev-only: posters are built with Pillow
    import capture
    import tempfile
    rng = random.Random(1)
    im = Image.new("RGB", (64, 32))
    im.putdata([(x * 4, (x + y) * 3 % 256, rng.randrange(256)) for y in range(32) for x in range(64)])
    with tempfile.TemporaryDirectory() as td:
        a, b, o1, o2 = (Path(td) / n_ for n_ in ("a.png", "b.png", "o1.png", "o2.png"))
        im.save(a)
        im2 = im.copy()
        im2.putpixel((10, 10), (255, 255, 255))
        im2.save(b)
        capture.posterize(a, o1, 1)          # a 1-byte budget forces the coarsest step
        capture.posterize(b, o2, 1)
        p1, p2 = Image.open(o1).convert("RGB"), Image.open(o2).convert("RGB")
        diff = [i for i, (u, v) in enumerate(zip(p1.tobytes()[0::3], p2.tobytes()[0::3])) if u != v]
        assert diff == [10 * 64 + 10]       # one changed input pixel changes exactly one output pixel
        capture.posterize(a, o2, 1)
        assert o1.read_bytes() == o2.read_bytes()


def _chromium() -> bool:
    try:
        __import__("playwright.sync_api")
    except ImportError:
        return False
    import capture
    return bool(capture.find_chromium())


@pytest.mark.skipif(not _chromium(), reason="needs Playwright and Chromium")
def test_social_png_renders_the_same_twice(tmp_path):
    import capture
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    capture.render_social(a)
    capture.render_social(b)
    assert capture.same_picture(a, b)
    assert a.stat().st_size <= 300 * 1024
    # an existing file within raster noise is left untouched (make_assets never dirties git for noise)
    before = a.read_bytes()
    capture.render_social(a)
    assert a.read_bytes() == before
