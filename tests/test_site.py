# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Static checks for the GitHub Pages site (docs/index.html).

No browser needed: every local asset the page references exists, in-page
anchors resolve, the brand wording rules hold and the trademark disclaimer
is present verbatim.
"""
import gzip
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "tools" / "brand")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

DOCS = Path(__file__).resolve().parent.parent / "docs"
INDEX = DOCS / "index.html"

DISCLAIMER = (
    "Claude and Claude Code are trademarks of Anthropic PBC. Pulsar Code is independent and not "
    "affiliated with or endorsed by Anthropic. Anthropic does not support routing Claude Code to "
    "non-Claude models; a Claude Code update can change behavior."
)

# Hosts a page may load scripts or styles from (none are used today).
ALLOWED_ASSET_HOSTS = {
    "cdnjs.cloudflare.com", "cdn.jsdelivr.net", "unpkg.com",
    "fonts.googleapis.com", "fonts.gstatic.com",
}


class _Collect(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.ids = set()
        self.dup_ids = set()
        self.refs = []        # (tag, attr, value, line)
        self.handlers = []    # (tag, attr, line)
        self.imgs = []        # (attrs dict, line)
        self.scripts = []     # (src, line)
        self.text = []

    def handle_starttag(self, tag, attrs):
        line = self.getpos()[0]
        d = dict(attrs)
        if d.get("id"):
            if d["id"] in self.ids:
                self.dup_ids.add(d["id"])
            self.ids.add(d["id"])
        for name, value in attrs:
            if name.lower().startswith("on"):
                self.handlers.append((tag, name, line))
            if name in ("src", "href", "data-full", "poster", "srcset") and value is not None:
                self.refs.append((tag, name, value, line))
        if tag == "img":
            self.imgs.append((d, line))
        if tag == "script":
            self.scripts.append((d.get("src"), line))
        if tag == "meta" and d.get("property") == "og:image":
            self.refs.append((tag, "og:image", d.get("content", ""), line))

    handle_startendtag = handle_starttag

    def handle_data(self, data):
        self.text.append(data)


@pytest.fixture(scope="module")
def html():
    assert INDEX.is_file(), "docs/index.html is missing"
    return INDEX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def parsed(html):
    p = _Collect()
    p.feed(html)
    p.close()
    return p


def _flat(s):
    return re.sub(r"\s+", " ", s)


def _local_path(value):
    """Map a reference to a file under docs/, or None for remote, anchor and data refs."""
    value = value.strip()
    if not value or value.startswith(("#", "data:", "mailto:")):
        return None
    u = urlparse(value)
    if u.scheme in ("http", "https"):
        # The published site's own absolute URLs (og:image) map back into docs/.
        if u.netloc == "pulsarosdevteam.github.io" and u.path.startswith("/pulsarcode/"):
            rel = u.path[len("/pulsarcode/"):]
            return DOCS / (rel or "index.html")
        return None
    if u.scheme:
        return None
    return DOCS / u.path.lstrip("/")


def test_local_references_exist(html, parsed):
    refs = [(t, a, v, ln) for (t, a, v, ln) in parsed.refs]
    # url(...) in inline CSS and fetch("...") in inline JS
    for m in re.finditer(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", html):
        refs.append(("css", "url", m.group(1), html.count("\n", 0, m.start()) + 1))
    for m in re.finditer(r"fetch\(\s*['\"]([^'\"]+)['\"]", html):
        refs.append(("js", "fetch", m.group(1), html.count("\n", 0, m.start()) + 1))
    missing = []
    checked = 0
    for tag, attr, value, line in refs:
        if attr == "srcset":
            values = [part.strip().split(" ")[0] for part in value.split(",")]
        else:
            values = [value]
        for v in values:
            path = _local_path(v)
            if path is None:
                continue
            checked += 1
            if not (path.is_file() or (v.endswith("/") and path.is_dir())):
                missing.append("line %d: <%s %s=%r>" % (line, tag, attr, v))
    assert checked >= 10, "expected the page to reference its local assets"
    assert not missing, "missing local assets:\n" + "\n".join(missing)


def test_in_page_anchors_resolve(html, parsed):
    bad = []
    for tag, attr, value, line in parsed.refs:
        if attr == "href" and value.startswith("#") and len(value) > 1:
            if value[1:] not in parsed.ids:
                bad.append("line %d: %s" % (line, value))
    assert not bad, "anchors without a target id:\n" + "\n".join(bad)
    assert not parsed.dup_ids, "duplicate ids: %s" % sorted(parsed.dup_ids)


def test_no_em_dash_anywhere_in_docs_site():
    em = chr(0x2014)
    offenders = []
    for p in [INDEX] + sorted(DOCS.glob("assets/**/*.svg")) + sorted(DOCS.glob("assets/**/*.css")) \
            + sorted(DOCS.glob("assets/**/*.js")):
        text = p.read_text(encoding="utf-8")
        for n, line in enumerate(text.splitlines(), 1):
            if em in line:
                offenders.append("%s:%d" % (p.relative_to(DOCS), n))
    assert not offenders, "em dash found: %s" % offenders


@pytest.mark.parametrize("phrase", [
    "Claude subscription",
    "Free Claude Code",
    "Claude for free",
    "Claude Code killer",
    "unlimited",
])
def test_banned_phrases_absent(html, phrase):
    assert phrase.lower() not in _flat(html).lower(), "banned phrase on the site: %r" % phrase


def test_no_claude_code_x_lockup(html):
    times = chr(0x00D7)
    pat = re.compile(r"Claude Code\s*(?:x|X|%s)\s+[A-Z]" % times)
    hits = pat.findall(_flat(html))
    assert not hits, "name lockup pairing Claude Code with another name: %s" % hits


def test_trademark_disclaimer_present_verbatim(html):
    assert DISCLAIMER in _flat(html)


INDEPENDENT = "Independent project, not affiliated with or endorsed by Anthropic."


def test_h1_is_the_tagline(html):
    from tools.brand import motion
    m = re.search(r"<h1[^>]*>(.*?)</h1>\s*<p[^>]*>([^<]*)</p>", html, re.S)
    assert m and _flat(re.sub(r"<[^>]+>", "", m.group(1))).strip() == motion.TAGLINE
    assert _flat(m.group(2)).strip() == INDEPENDENT     # directly under the heading


def test_link_previews_carry_the_tagline_and_independence(html):
    """Search results and link previews show the title and og tags without the page around them."""
    from tools.brand import motion
    assert "<title>Pulsar Code · %s</title>" % motion.TAGLINE in html
    desc = re.search(r'<meta property="og:description" content="([^"]+)">', html).group(1)
    assert desc.startswith(motion.TAGLINE + ".") and "not affiliated with Anthropic" in desc
    alt = re.search(r'<meta property="og:image:alt" content="([^"]+)">', html).group(1)
    assert motion.TAGLINE in alt


def test_no_inline_event_handlers_or_js_urls(html, parsed):
    assert not parsed.handlers, "inline event handlers: %s" % parsed.handlers
    assert not re.search(r"""(?:href|src|action)\s*=\s*["']\s*javascript:""", html, re.I)


def test_scripts_only_inline_or_allowed_cdn(parsed):
    for src, line in parsed.scripts:
        if not src:
            continue
        u = urlparse(src)
        if u.scheme in ("http", "https"):
            assert u.scheme == "https" and u.netloc in ALLOWED_ASSET_HOSTS, (line, src)
        else:
            assert _local_path(src) is not None and _local_path(src).is_file(), (line, src)


def test_external_stylesheets_only_google_fonts(parsed):
    for tag, attr, value, line in parsed.refs:
        if tag == "link" and attr == "href" and value.startswith("http"):
            host = urlparse(value).netloc
            if host in ("fonts.googleapis.com", "fonts.gstatic.com"):
                continue
            # canonical and other non-stylesheet links may point at the published site
            assert host in ("pulsarosdevteam.github.io",), (line, value)


def test_images_have_alt_and_dimensions(parsed):
    bad = []
    for attrs, line in parsed.imgs:
        if attrs.get("id") == "lbi":   # lightbox target, filled in by script
            continue
        if not (attrs.get("alt") or "").strip():
            bad.append("line %d: no alt" % line)
        if not (attrs.get("width") and attrs.get("height")):
            bad.append("line %d: no width/height" % line)
    assert not bad, "\n".join(bad)


def test_target_blank_links_have_noopener(html):
    for m in re.finditer(r"<a\b[^>]*target=[\"']_blank[\"'][^>]*>", html):
        assert re.search(r"rel=[\"'][^\"']*noopener", m.group(0)), m.group(0)


def test_demo_recording_is_valid_json():
    data = json.loads((DOCS / "demo" / "boot.json").read_text(encoding="utf-8"))
    assert data.get("cols") and data.get("rows")
    frames = data.get("frames")
    assert frames, "the replay needs frames"
    ts = [f["t"] for f in frames]
    assert ts == sorted(ts), "frame times must be non-decreasing"


def test_has_viewport_and_lang(html):
    assert '<html lang="en">' in html
    assert 'name="viewport"' in html
    assert "<title>" in html


def test_font_licenses_ship_with_fonts():
    fonts = sorted(DOCS.glob("assets/fonts/*.woff2"))
    if fonts:
        assert list(DOCS.glob("assets/fonts/OFL*.txt")), "self-hosted fonts need their OFL text"


# ---------------------------------------------------------------------------
# v3 site: generated regions, detector data, privacy, budgets
# ---------------------------------------------------------------------------

SITE_TOOLS = ROOT / "tools" / "site"
MORE = DOCS / "assets" / "site" / "more.js"


def _build():
    if str(SITE_TOOLS) not in sys.path:
        sys.path.insert(0, str(SITE_TOOLS))
    import build  # type: ignore
    return build


def _region(html, name):
    m = re.search(r"<!-- %s:begin -->(.*?)<!-- %s:end -->" % (name, name), html, re.S)
    assert m, "missing region %s" % name
    return m.group(1)


def _inline(html):
    return re.search(r"<script>(.*?)</script>", _region(html, "js"), re.S).group(1)


def _script(html):
    """All the page's own JavaScript: the inline part and the deferred assets/site/more.js."""
    return _inline(html) + "\n" + MORE.read_text(encoding="utf-8")


def test_inline_script_and_detector_data_are_current(html):
    """tools/site/build.py output: the scripts and the provider table come from tools/site/*.js and the code."""
    b = _build()
    assert MORE.read_text(encoding="utf-8") == b.more_js(), "docs/assets/site/more.js is stale: run tools/site/build.py"
    assert _region(html, "js") == b.more_tag(b.more_js()) + b.script()
    data = json.loads(re.search(r'<script type="application/json" id="pdata">(.*?)</script>', html, re.S).group(1))
    assert data == json.loads(json.dumps(b.detector_data(), ensure_ascii=False))


def test_page_matches_the_build_output():
    """docs/index.html is exactly what tools/site/build.py renders from the current brand files.

    Fails when tools/brand regenerates hero-inline-dark.svg, constellation-site.svg or mark.svg
    (or the provider table changes) without a site rebuild."""
    b = _build()
    old = INDEX.read_text(encoding="utf-8")
    new, _ = b.render(old)
    if new != old:
        a, c = old.splitlines(), new.splitlines()
        i = next((k for k in range(min(len(a), len(c))) if a[k] != c[k]), min(len(a), len(c)))
        pytest.fail("docs/index.html is stale (first difference at line %d): run python tools/site/build.py" % (i + 1))


def test_counts_on_the_page_come_from_the_code(html):
    from pulsar.screens import advertised_providers
    from tools.brand import motion
    n = len(advertised_providers())
    groups = {k: len(v) for k, v in motion.provider_groups().items()}
    for m in re.finditer(r'data-count="(\w+)"[^>]*>([^<]*)<', html):
        want = n if m.group(1) == "total" else groups[m.group(1)]
        assert m.group(2) == str(want), m.group(0)
    text = _flat(re.sub(r"<script.*?</script>|<style.*?</style>|<svg.*?</svg>", " ", html, flags=re.S))
    text = re.sub(r"<[^>]+>", " ", text)
    for m in re.finditer(r"(\d+)\s+provider", text):
        assert int(m.group(1)) == n, m.group(0)
    # a provider count in prose is dated (the advertised list changes between releases)
    for m in re.finditer(r"\b%d\s+provider(?: profile)?s\b(.{0,16})" % n, _flat(text)):
        assert "v3.0.0" in m.group(1), m.group(0)


def test_detector_matches_the_cli_prefix_table(html):
    from pulsar import providers as P
    data = json.loads(re.search(r'id="pdata">(.*?)</script>', html, re.S).group(1))
    pre = [r[0] for r in data["p"]] + data["u"]
    assert sorted(pre) == sorted(p for p, _ in P._BY_PREFIX)
    assert [r[0] for r in data["r"]] == [p for p, _ in P._REJECT]
    lens = [len(r[0]) for r in data["p"]]
    assert lens == sorted(lens, reverse=True), "prefixes must stay longest first"
    for r in data["r"]:
        assert "http" not in r[1]
    assert data["amb"] and all(i in data["names"] for i in data["amb"])


def test_only_network_call_is_replay_json(html):
    js = _script(html)
    for bad in ("XMLHttpRequest", "sendBeacon", "WebSocket", "EventSource", "importScripts", "import("):
        assert bad not in js, bad
    calls = re.findall(r"fetch\(([^)]*)\)", js)
    assert calls, "the replay player fetches its JSON"
    for c in calls:
        assert c.strip().startswith('"./demo/"'), c


def test_key_field_contract(html):
    m = re.search(r"<input[^>]*id=\"key\"[^>]*>", html)
    assert m
    tag = m.group(0)
    for attr in ('maxlength="12"', 'autocomplete="off"', 'spellcheck="false"', 'autocapitalize="off"', 'aria-describedby="keynote"'):
        assert attr in tag, attr
    note = re.search(r'id="keynote">(.*?)</p>', html, re.S).group(1)
    assert "Nothing is sent or stored" in note and "first 12 characters" in note
    js = _script(html)
    assert 'addEventListener("blur",wipe)' in js and "visibilitychange" in js
    assert "localStorage" in js and "try" in js
    # no key value ever goes to storage: only the two sense toggles are stored
    assert set(re.findall(r'store\.set\("([^"]+)"', js)) <= {"pc.listen", "pc.haptics"}


def test_senses_off_by_default(html):
    for i in ("listen", "haptics"):
        tag = re.search(r'<button[^>]*id="%s"[^>]*>' % i, html).group(0)
        assert 'aria-pressed="false"' in tag
    assert "navigator.vibrate" in _script(html)


def test_hero_is_an_image_with_a_reduced_motion_poster(html):
    """The inline hero cost the page 20-40 fps; as an <img> its CSS animations stay off the main thread."""
    hero = _region(html, "hero")
    assert hero.startswith("<picture>") and "<svg" not in hero
    src = re.search(r'<source media="\(prefers-reduced-motion: reduce\)" srcset="([^"]+)">', hero)
    img = re.search(r'<img class="hero-art" src="([^"]+)" width="1280" height="440"[^>]* alt="([^"]+)">', hero)
    assert src and img and src.group(1).endswith("hero-static-dark.v3.svg")
    art = (DOCS / img.group(1)).read_text(encoding="utf-8")
    assert "prefers-reduced-motion" in art and "@keyframes" in art
    assert "@keyframes" not in (DOCS / src.group(1)).read_text(encoding="utf-8")
    # the Listen toggle clicks on the pen's main pulse: its timing is read from the artwork
    m = re.search(r"\.h-pr\{[^}]*animation:h-pr ([\d.]+)s linear ([\d.]+)s infinite", art)
    data = json.loads(re.search(r'id="pdata">(.*?)</script>', html, re.S).group(1))
    assert m and data["clk"]["pr"] == [float(m.group(2)), float(m.group(1))]


def test_hero_alt_is_the_brand_alt(html):
    """The page's hero alt is tools/brand/hero.ALT, the words the README hero carries."""
    import html as _html
    from tools.brand import hero as H
    img = re.search(r'<img class="hero-art"[^>]* alt="([^"]+)">', _region(html, "hero"))
    assert img and _html.unescape(img.group(1)) == H.ALT


def test_narrow_hero_readout_matches_the_detector(html):
    """Below 600 px the artwork is cropped to the wordmark and its readout line is HTML text."""
    from pulsar import providers as P
    ro = re.search(r'<p class="wrap hero-ro">(.*?)</p>', html, re.S).group(1)
    text = _flat(re.sub(r"<[^>]+>", " ", ro))
    pre = re.match(r"\s*([a-z-]+)•+", text).group(1)
    prov = dict(P._BY_PREFIX)[pre]
    data = json.loads(re.search(r'id="pdata">(.*?)</script>', html, re.S).group(1))
    row = [r for r in data["p"] if r[0] == pre][0]
    assert row[2] in text and (" %s " % row[3]) in text and ("%s wire" % row[4]) in text
    assert "Pulsar Core " + data["core"] in text
    assert prov.id == row[1] and prov.route == row[3]


def test_constellation_inlined(html):
    con = _region(html, "constellation")
    nodes = re.findall(r'data-id="([^"]+)" data-angle="[-\d.]+"', con)
    from pulsar.screens import advertised_providers
    assert sorted(nodes) == sorted(p.id for p in advertised_providers())
    assert all(i.startswith("c-") for i in re.findall(r'\sid="([^"]+)"', con))


def test_privacy_truth(html):
    flat = _flat(re.sub(r"<[^>]+>", " ", html))
    assert "Successful model output is passed through as is" in flat
    assert "Your prompts, code and tool results" in flat
    assert "pulsarcode relay off" in flat and "/pulsar:council" in flat
    assert "tested with Claude Code 2.1.287" in flat
    assert "tests new Claude Code releases" not in flat and "tests every" not in flat.lower()
    assert "PULSAR_REF=v3.0.0" in flat and "SHA256SUMS" in flat and "gh attestation verify" in flat
    assert "coming with the first public release" in flat and "arrive with the v3.0.0 release" not in flat
    assert "skip your HTTP proxy by default" in flat and "never go through your HTTP proxy" not in flat
    # deny rules (pulsar/plugin.py _deny_rules) are written on Windows too, and cover more than run/
    assert "run/" in flat and "on Windows too" in flat and "not on native Windows" not in flat
    assert "the isolated profile's credentials" in flat and "on the whole Pulsar home" not in flat
    # Core refuses a key over plain http to a public host unless the user opts in
    assert "never sent over plain http" in flat and "PULSAR_ALLOW_HTTP=1" in flat
    # the arming checks are not the boundary: the page never promises that only a human can arm Pilot
    assert "Only you can arm it" not in flat and "refuses inside a Claude Code session" in flat
    assert "AGPL-3.0-or-later" in flat
    assert not re.search(r"AGPL-3\.0(?!-or-later)", flat)
    assert "Replies, errors and council answers are scrubbed" not in flat


def test_claims_match_the_code(html):
    flat = _flat(re.sub(r"<[^>]+>", " ", html))
    # the web view's CSP blocks external requests but allows inline script: never call it strict
    assert "strict CSP" not in flat and "blocks all external requests" in flat
    # Claude Code gets the real window only up to its 1M mode; 200k to 1M is used as 200k
    assert "told the model's real window" not in flat and "between 200k and 1M" in flat
    # a session with Pilot armed never relays; updates refuse older versions unless asked
    assert "never relays" in flat and "--allow-downgrade" in flat


def test_no_absolute_claims(html):
    text = re.sub(r"<style.*?</style>|<svg.*?</svg>", " ", html, flags=re.S)
    text = _flat(re.sub(r"<[^>]+>", " ", text))
    for word in ("fastest", "guaranteed", "always", "every"):
        assert not re.search(r"\b%s\b" % word, text, re.I), word


def test_brand_links(html):
    assert 'property="og:image" content="https://pulsarosdevteam.github.io/pulsarcode/assets/brand/social.png"' in html
    assert 'href="assets/brand/favicon.svg"' in html and 'href="assets/brand/favicon-32.png"' in html
    assert 'rel="apple-touch-icon"' in html


def test_pilot_sandbox_uses_overlay_numbers(html):
    from pulsar.pilot import overlay
    js = _script(html)
    m = re.search(r"glideDur=\(x1,y1,x2,y2\)=>([\d.]+)\+([\d.]+)\*Math\.min\(1,Math\.hypot\(x2-x1,y2-y1\)/(\d+)\)", js)
    assert m, "glide duration formula"
    a, b, d = float(m.group(1)), float(m.group(2)), float(m.group(3))
    for dist in (0, 300, 900, 2000):
        assert abs((a + b * min(1, dist / d)) - overlay.glide_duration(0, 0, dist, 0)) < 1e-9
    assert "This is the overlay Pilot draws on your desktop. Here nothing is clicked." in html


def test_reduced_motion_paths(html):
    js = _script(html)
    assert "prefers-reduced-motion: reduce" in js
    assert "@media (prefers-reduced-motion:reduce)" in html
    assert "no-preference" in html  # scroll-driven motion is opt-in to motion


def test_inline_script_budget(html):
    """Inline JS at most 24 KB, so the first paint stays fast; the player and the sandbox load later from the
    same origin."""
    js = _inline(html)
    assert len(js.encode("utf-8")) <= 24 * 1024, len(js.encode("utf-8"))
    tag = re.search(r'<i id="more" hidden data-src="(assets/site/more\.js)\?v=([0-9a-f]{10})"></i>', html)
    assert tag and (DOCS / tag.group(1)).is_file()
    import hashlib
    assert tag.group(2) == hashlib.sha256(MORE.read_bytes()).hexdigest()[:10]
    assert 'sc.src=$("#more").dataset.src' in js
    assert not re.search(r"<script[^>]+src=", html), "no external script tags; more.js is injected by the inline script"


def test_pages_root_ships_no_build_sources():
    shipped = sorted(p.name for p in (DOCS / "assets" / "site").iterdir())
    assert shipped == ["more.js"], shipped
    assert not list(DOCS.rglob("*.py")) and not list(DOCS.rglob("__pycache__"))


@pytest.mark.parametrize("src,want", [
    ("a = b / c / d;\nx = s.replace(/\\s+/g, '');", "a=b/c/d;x=s.replace(/\\s+/g,'');"),
    ("for (let i = 0; i < n; i++) y = i + +z - -w;", "for(let i=0;i<n;i++)y=i+ +z- -w;"),
    ("const a = 1\nconst b = [1, 2]\n(f)()", "const a=1\nconst b=[1,2]\n(f)()"),
    ("if (/^[/]x/.test(q)) { return 'a // b' } /* c */ // d", "if(/^[/]x/.test(q)){return'a // b'}"),
    ("return x\n}", "return x}"),
])
def test_minifier_keeps_meaning(src, want):
    assert _build().minify(src) == want


def test_extras_copy_matches_the_readme(html):
    flat = _flat(re.sub(r"<[^>]+>", " ", html))
    assert "documented plugin and hooks" not in flat
    assert "its function-hooks plugin API is early access" in flat


def test_size_budget():
    raw = INDEX.read_bytes()
    gz = len(gzip.compress(raw, 9))
    assert gz <= 75 * 1024, "index.html is %d bytes gzip (budget 75 KB)" % gz


def test_replay_text_alternatives_are_the_scene_alts(html):
    """The scene alt is the text alternative, readable before the JSON is fetched."""
    import html as _html
    tabs = re.findall(r'<button\b[^>]*\brole="tab"[^>]*\bdata-s="(\w+)"[^>]*\bdata-alt="([^"]*)"', html)
    assert [t[0] for t in tabs] == ["session", "uplink", "boot", "picker", "stage", "top"]
    for name, alt in tabs:
        want = " ".join(json.loads((DOCS / "demo" / (name + ".json")).read_text(encoding="utf-8"))["alt"].split())
        assert _html.unescape(alt) == want
    palt = re.search(r'<p class="alt" id="palt">([^<]*)</p>', html).group(1)
    assert _html.unescape(palt) == _html.unescape(tabs[0][1])
