# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""README layout: v3 artwork order, <picture> routing, paths, budgets, counts and copy rules."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
TEXT = README.read_text(encoding="utf-8")
sys.path.insert(0, str(ROOT / "tools" / "brand"))

DISCLAIMER = (
    "Claude and Claude Code are trademarks of Anthropic PBC. Pulsar Code is independent and not\n"
    "affiliated with or endorsed by Anthropic. Anthropic does not support routing Claude Code to\n"
    "non-Claude models; a Claude Code update can change behavior.\n"
    "\n"
    "Other product names belong to their owners and are used only to say what Pulsar Code can connect\n"
    "to. You need your own access to each provider you connect, and their terms apply.\n"
)

# Directly under the heading: the tagline has a verb, so it cannot read as a product name.
INDEPENDENT = "Independent project, not affiliated with or endorsed by Anthropic."

PICTURE = re.compile(r"<picture>(.*?)</picture>", re.S)
THEMED = re.compile(r'<img src="(assets/(?:brand|readme)/[^"]+)-light\.v(\d+)\.svg"')


def pictures():
    return PICTURE.findall(TEXT)


def image_refs():
    refs = re.findall(r'(?:src|srcset)="([^"]+)"', TEXT)
    refs += re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", TEXT)
    refs += re.findall(r'<a href="(assets/[^"]+)"', TEXT)
    return [r for r in refs if not re.match(r"^[a-z]+:", r)]


def test_every_relative_image_path_exists():
    refs = image_refs()
    assert len(refs) > 40
    missing = [r for r in refs if not (ROOT / r).is_file()]
    assert not missing, missing


def test_no_reference_to_deleted_files():
    for bad in ("hero.svg", "hero-dark.svg", "hero-light.svg", ".gif", "hero.png"):
        assert bad not in TEXT, bad
    assert not re.search(r"assets/demo/[\w-]+\.json", TEXT)


DIVIDER = re.compile(r'<img src="assets/readme/divider-\d-light\.v\d+\.svg"')


def test_every_image_has_alt_text():
    for tag in re.findall(r"<img\b[^>]*>", TEXT):
        m = re.search(r'alt="([^"]*)"', tag)
        assert m, tag
        if DIVIDER.match(tag):
            # decorative: the heading right after it says the same, so it must not be announced twice
            assert m.group(1) == "", tag
        else:
            assert len(m.group(1)) >= 12, tag


NARROW = "(max-width: 767px)"


def test_themed_pictures_have_six_sources_in_order():
    themed = [p for p in pictures() if THEMED.search(p)]
    assert len(themed) == 1 + 4 + 4 + 2 + 1  # hero, dividers, cards, signal + constellation, footer
    for p in themed:
        base, ver = THEMED.search(p).groups()
        if base.endswith("/hero"):
            sd = base.replace("/hero", "/hero-static") + "-dark.v%s.svg" % ver
            sl = base.replace("/hero", "/hero-static") + "-light.v%s.svg" % ver
        else:
            sd, sl = base + "-dark-static.v%s.svg" % ver, base + "-light-static.v%s.svg" % ver
        sources = re.findall(r'<source media="([^"]+)" srcset="([^"]+)">', p)
        assert sources == [
            (NARROW + " and (prefers-color-scheme: dark)", base + "-dark-still-v%s.png" % ver),
            (NARROW, base + "-light-still-v%s.png" % ver),
            ("(prefers-reduced-motion: reduce) and (prefers-color-scheme: dark)", sd),
            ("(prefers-reduced-motion: reduce)", sl),
            ("(prefers-color-scheme: dark)", base + "-dark.v%s.svg" % ver),
        ], p


def test_a_narrow_screen_never_selects_an_svg():
    """GitHub's phone app left a private repository's SVG files as broken images (October 2026): on a
    narrow screen every picture resolves to a PNG, whatever the colour scheme or the motion setting."""
    assert len(pictures()) == 13
    for p in pictures():
        sources = re.findall(r'<source media="([^"]+)" srcset="([^"]+)">', p)
        narrow = [(m, s) for m, s in sources if m.startswith(NARROW)]
        assert narrow and sources[:len(narrow)] == narrow, p     # first, so they win over every other source
        assert narrow[-1][0] == NARROW, p                        # and the last of them has no other condition
        assert all(s.endswith(".png") for _, s in narrow), p


def test_animated_replays_have_a_reduced_motion_poster():
    animated = re.findall(r'<img src="(assets/demo/[\w-]+\.svg)"', TEXT)
    assert animated == ["assets/demo/session.svg"]
    for p in pictures():
        m = re.search(r'<img src="assets/demo/([\w-]+)\.svg"', p)
        if m:
            assert '<source media="(prefers-reduced-motion: reduce)" srcset="assets/demo/posters/%s.png">' % m.group(1) in p
            assert '<source media="%s" srcset="assets/demo/posters/%s.png">' % (NARROW, m.group(1)) in p


def test_only_the_session_replay_animates_outside_details():
    outside = re.sub(r"<details>.*?</details>", "", TEXT, flags=re.S)
    assert re.findall(r'<img src="assets/demo/[\w-]+\.svg"', outside) == ['<img src="assets/demo/session.svg"']


@pytest.mark.parametrize("name", ["uplink", "picker"])
def test_side_replays_are_posters_linked_to_the_recording(name):
    assert re.search(r'<a href="assets/demo/%s\.svg"><img src="assets/demo/posters/%s\.png" width="100%%" alt="[^"]{40,}"></a>'
                     % (name, name), TEXT)
    assert '<a href="assets/demo/%s.svg">Play the recording</a>.</sub>' % name in TEXT
    assert (ROOT / ("assets/demo/posters/%s.png" % name)).is_file() and (ROOT / ("assets/demo/%s.svg" % name)).is_file()


def test_no_absolute_pilot_or_licence_claims():
    assert "Every action goes through" not in TEXT and "Source-available" not in TEXT
    assert ("Pilot actions are not pre-approved: Claude Code's permission system decides, "
            "and in its default mode it asks you each time.") in TEXT
    assert "Open source under AGPL-3.0-or-later" in TEXT
    assert "every connected provider" not in TEXT.lower()


def test_section_order_and_dividers():
    marks = [
        "assets/brand/hero-light.v3.svg", "<h3 align=\"center\">", "## Install",
        "divider-1-light", "## See it run", "assets/demo/session.svg", "assets/demo/posters/uplink.png",
        "assets/demo/posters/picker.png", "<summary><b>More recordings: boot, Stage, Observatory</b></summary>",
        "divider-2-light", "## Features", "card-stage-light", "card-council-light", "card-pilot-light",
        "card-observatory-light", "divider-3-light", "## How it works", "signal-light",
        "constellation-light", "```mermaid", "divider-4-light", "## Do you need Pulsar Code?",
        "## Privacy", "## FAQ", "## Commands", "## Configuration", "## Requirements", "## Uninstall",
        "## Project", "footer-light", "## Disclaimer", "## License",
    ]
    pos = [TEXT.find(m) for m in marks]
    assert -1 not in pos, [m for m, p in zip(marks, pos) if p < 0]
    assert pos == sorted(pos), [m for m, p in zip(marks, pos)]
    # each divider is decorative and sits directly before the heading it introduces
    dividers = re.findall(r'<img src="assets/readme/divider-(\d)-light\.v3\.svg" width="100%" alt="">\n</picture>\n\n## (.+)',
                          TEXT)
    assert dividers == [("1", "See it run"), ("2", "Features"), ("3", "How it works"),
                        ("4", "Do you need Pulsar Code?")]


def test_tagline_and_signal_caption():
    import motion
    assert motion.TAGLINE.startswith("Run the official Claude Code CLI")
    head = '<h3 align="center">%s</h3>' % motion.TAGLINE
    assert head in TEXT
    first = [line for line in TEXT.split(head, 1)[1].splitlines() if line.strip()][0]
    assert INDEPENDENT in first, first
    assert ("Before the first 200, any target may fail and the next in the session plan takes over. After it, "
            "Pulsar Core streams only from that target.") in TEXT


def test_counts_are_the_computed_ones():
    import motion
    from pulsar.screens import advertised_providers
    n = len(advertised_providers())
    g = {k: len(v) for k, v in motion.provider_groups().items()}
    caption = "%d provider profiles in v3.0.0, by route: local %d · native %d · cloud %d · keyed adapters %d." % (
        n, g["local"], g["native"], g["cloud"], g["adapter"])
    assert caption in TEXT
    for m in re.finditer(r"(\d+) provider(?: profile)?s?\b", TEXT):
        assert int(m.group(1)) == n, m.group(0)


def test_nav_anchors_resolve_to_headings():
    def slug(h):
        return re.sub(r"[^\w\- ]", "", h.strip().lower()).replace(" ", "-")
    anchors = {slug(h) for h in re.findall(r"(?m)^#{2,3} (.+)$", TEXT)}
    for a in re.findall(r"\]\(#([^)]+)\)", TEXT):
        assert a in anchors, a


def test_disclaimer_verbatim_and_copy_rules():
    assert DISCLAIMER in TEXT
    assert chr(0x2014) not in TEXT
    low = TEXT.lower()
    assert "free claude code" not in low and "claude code x " not in low
    import check
    assert check.main(["--quiet", "README.md"]) == 0


def test_first_view_budget():
    """Hero, 4 dividers, session, uplink poster, picker poster, 4 cards, signal, constellation, footer <= 1.9 MB."""
    for theme in ("dark", "light"):
        files = ["assets/brand/hero-%s.v3.svg" % theme, "assets/demo/session.svg",
                 "assets/demo/posters/uplink.png", "assets/demo/posters/picker.png", "assets/readme/signal-%s.v3.svg" % theme,
                 "assets/readme/constellation-%s.v3.svg" % theme, "assets/readme/footer-%s.v3.svg" % theme]
        files += ["assets/readme/divider-%d-%s.v3.svg" % (k, theme) for k in range(1, 5)]
        files += ["assets/readme/card-%s-%s.v3.svg" % (c, theme) for c in ("stage", "council", "pilot", "observatory")]
        total = sum((ROOT / f).stat().st_size for f in files)
        assert total <= 1.9 * 1024 * 1024, (theme, total)
        for f in files:
            assert f in TEXT, f


def test_narrow_first_view_budget():
    """What a narrow screen loads for one theme: the 12 stills, the session poster and the two side posters <= 1.2 MB."""
    for theme in ("dark", "light"):
        files = ["assets/brand/hero-%s-still-v3.png" % theme, "assets/demo/posters/session.png",
                 "assets/demo/posters/uplink.png", "assets/demo/posters/picker.png"]
        files += ["assets/readme/%s-%s-still-v3.png" % (s, theme) for s in (
            "divider-1", "divider-2", "divider-3", "divider-4", "card-stage", "card-council", "card-pilot",
            "card-observatory", "signal", "constellation", "footer")]
        total = sum((ROOT / f).stat().st_size for f in files)
        assert total <= 1.2 * 1024 * 1024, (theme, total)
        for f in files:
            assert f in TEXT, f


@pytest.mark.parametrize("ref", ["assets/screens/%s.png" % s for s in (
    "boot", "stage-pane", "stage-deck", "stage-diagram", "stage-chart", "stage-web", "observatory", "pilot-cursor")])
def test_screenshots_kept_with_their_alt_text(ref):
    assert re.search(r'<img src="%s" width="100%%" alt="[^"]{20,}">' % re.escape(ref), TEXT)


def test_install_and_update_claims_match_the_code():
    # `pulsarcode update` never falls back to an unreleased branch on its own (pulsar/cli.py cmd_update)
    assert "falls back to `main`" not in TEXT and "pulsarcode update main" in TEXT
    assert "--allow-downgrade" in TEXT
    # package channels and release tags are announced, never shown as available before they exist
    for cmd in ("pipx install pulsarcode", "uv tool install pulsarcode", "npm install -g pulsarcode",
                "brew install", "scoop install"):
        assert cmd not in TEXT, cmd
    assert "coming with the first public release" in TEXT
    assert "gh repo clone PulsarOSDevTeam/pulsarcode && bash pulsarcode/install.sh --local" in TEXT


def test_provider_count_cites_its_source():
    # The public count is advertised_providers(), not len(PROVIDERS) (which also holds the subscription profile).
    assert "`pulsar.screens.advertised_providers()`" in TEXT
    assert "from `pulsar/providers.py`" not in TEXT


def test_footer_alt_matches_svg_label():
    root = Path(__file__).resolve().parent.parent
    for theme in ("dark", "light"):
        svg = (root / ("assets/readme/footer-%s.v3.svg" % theme)).read_text(encoding="utf-8")
        label = re.search(r'aria-label="([^"]+)"', svg).group(1)
        assert 'alt="%s"' % label in TEXT, (theme, label)


def test_hero_alt_is_the_brand_alt():
    import hero

    m = re.search(r'<img src="assets/brand/hero-light\.v\d+\.svg" width="100%" alt="([^"]+)">', TEXT)
    assert m, "hero <img> fallback missing"
    assert m.group(1) == hero.ALT


def test_every_picture_source_exists():
    for body in pictures():
        srcs = re.findall(r'(?:srcset|src)="([^"]+)"', body)
        assert len(srcs) in (3, 6), body        # a replay and its two posters; a themed picture
        for s in srcs:
            assert (ROOT / s).is_file(), s
