# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Build the Pages site: paste the brand artwork, the provider data and the scripts into docs/index.html.

The Pages site makes no network calls besides the replay JSON, so everything the page shows about
providers is inlined at build time, from the code:

    <!-- hero:begin -->           a <picture> of docs/assets/brand/hero-inline-dark.svg (tools/brand/hero.py),
                                  the static poster for reduced motion. As an <img> its CSS animations
                                  run on the compositor; inlined, the same artwork cost the page 20-40 fps.
    <!-- constellation:begin -->  docs/assets/brand/constellation-site.svg  (ids "c-", nodes data-id/data-angle)
    <!-- mark:begin -->           docs/assets/brand/mark.svg as a <symbol id="pm"> (ids prefixed "pm-")
    <!-- providers:begin -->      detector data: tools/brand/motion.providers_json() plus the shapes and
                                  local words of pulsar/providers.py
    data-count="..."              text of any element carrying it: total, local, native, cloud, adapter
    <!-- js:begin -->             tools/site/site.js, minified (budget 24 KB, so the first paint stays fast)
    docs/assets/site/more.js      tools/site/more.js, minified: the replay player and the Pilot sandbox,
                                  loaded by the page when either comes near the viewport

When a brand file is missing (tools/brand/make_assets.py has not written it yet), the generator in
tools/brand is called directly, so the page is never built from stale numbers.

    python tools/site/build.py           # rewrite docs/index.html and docs/assets/site/more.js in place
    python tools/site/build.py --check   # exit 1 when either is out of date

Python 3.9 compatible; standard library only (plus the repository's own modules).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DOCS = ROOT / "docs"
INDEX = DOCS / "index.html"
BRAND = DOCS / "assets" / "brand"
MORE = DOCS / "assets" / "site" / "more.js"
JS_BUDGET = 24 * 1024
HERO_SRC = "assets/brand/hero-inline-dark.svg"
HERO_POSTER = "assets/brand/hero-static-dark.v3.svg"
for _p in (ROOT, ROOT / "tools" / "brand"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _strip_prolog(svg: str) -> str:
    svg = re.sub(r"^\s*<\?xml[^>]*\?>\s*", "", svg)
    svg = re.sub(r"<!DOCTYPE[^>]*>\s*", "", svg)
    return svg.strip()


def _hero_text() -> str:
    p = BRAND / "hero-inline-dark.svg"
    if not p.is_file():  # pragma: no cover - only before make_assets.py has run
        raise SystemExit("docs/assets/brand/hero-inline-dark.svg is missing: run tools/brand/make_assets.py")
    return p.read_text(encoding="utf-8")


def hero_pulse() -> List[float]:
    """[delay, period] in seconds of the pen's main-pulse ring (class h-pr) in the hero artwork.

    The Listen toggle clicks in step with it; read from the file, never copied."""
    m = re.search(r"\.h-pr\{[^}]*animation:h-pr ([\d.]+)s linear ([\d.]+)s infinite", _hero_text())
    if not m:
        raise SystemExit("hero-inline-dark.svg: no h-pr pulse animation found")
    return [float(m.group(2)), float(m.group(1))]


def hero_picture() -> str:
    import html as _html
    from tools.brand import hero  # type: ignore
    svg = _hero_text()
    w = re.search(r'\bwidth="(\d+)"', svg)
    h = re.search(r'\bheight="(\d+)"', svg)
    assert w and h and (BRAND / "hero-static-dark.v3.svg").is_file()
    # the alt text is tools/brand/hero.ALT, the same words the README hero carries
    return ('<picture><source media="(prefers-reduced-motion: reduce)" srcset="%s">'
            '<img class="hero-art" src="%s" width="%s" height="%s" fetchpriority="high" decoding="async" alt="%s"></picture>'
            % (HERO_POSTER, HERO_SRC, w.group(1), h.group(1), _html.escape(hero.ALT, quote=True)))


def constellation_svg() -> str:
    p = BRAND / "constellation-site.svg"
    if p.is_file():
        svg = p.read_text(encoding="utf-8")
    else:
        from tools.brand import readme_art  # type: ignore
        svg = readme_art.constellation("dark", anim=False, site=True)
    svg = _strip_prolog(svg)
    # The page prints its own dated legend under the figure; drop the undated one readme_art draws after the
    # centre counter.
    m = re.search(r"profiles</text>", svg)
    if m:
        head, tail = svg[:m.end()], svg[m.end():]
        tail = re.sub(r"<text\b[^>]*>[^<]*</text>|<circle\b[^>]*/>", "", tail)
        svg = head + tail
    return re.sub(r"^<svg\b", '<svg class="const-art" focusable="false"', svg, count=1)


def mark_symbol() -> str:
    """mark.svg as one <symbol>, reused by the nav, the docking mark and the footer."""
    p = BRAND / "mark.svg"
    if p.is_file():
        svg = _strip_prolog(p.read_text(encoding="utf-8"))
    else:
        from tools.brand import readme_art  # type: ignore
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><defs>%s</defs>%s</svg>'
               % (readme_art.mark_defs("dark"), readme_art.mark_body("dark")))
    from tools.brand import svgkit  # type: ignore
    svg = svgkit.prefix_ids(svg, "pm-")
    vb = re.search(r'viewBox="([^"]+)"', svg)
    inner = re.sub(r"^<svg\b[^>]*>", "", svg, count=1)
    inner = re.sub(r"</svg>\s*$", "", inner)
    inner = re.sub(r"<title>.*?</title>", "", inner, flags=re.S)
    inner = re.sub(r"<desc>.*?</desc>", "", inner, flags=re.S)
    return ('<svg width="0" height="0" style="position:absolute" aria-hidden="true" focusable="false">'
            '<symbol id="pm" viewBox="%s">%s</symbol></svg>' % (vb.group(1) if vb else "0 0 64 64", inner))


def detector_data() -> Dict[str, object]:
    from pulsar import providers as P
    from tools.brand import motion  # type: ignore
    data = motion.providers_json()
    shipped = BRAND / "providers.json"
    if shipped.is_file():
        on_disk = json.loads(shipped.read_text(encoding="utf-8"))
        if on_disk.get("count") != data["count"] or on_disk.get("prefixes") != data["prefixes"]:
            raise SystemExit("docs/assets/brand/providers.json is stale: run tools/brand/make_assets.py")
    names = data["names"]  # type: ignore[index]
    # Candidates the CLI offers for a bare sk- key, in the CLI's own order (pulsar/providers.py _SHAPES).
    amb: List[str] = []
    for pattern, ids, _label in P._SHAPES:
        if pattern.startswith("^sk-"):
            for i in ids:
                if i in names and i not in amb:  # type: ignore[operator]
                    amb.append(i)
    local_words = {w: i for w, i in P._LOCAL_WORDS.items() if len(w) <= 12 and " " not in w}
    tok = motion.tokens()
    clk = tok["clock"]  # type: ignore[index]
    return {
        # the CLI's beam clock (pulsar/fx.py Beams.angles), the Pilot ripple (pulsar/pilot/overlay.py)
        "clk": {"w1": round(math.degrees(motion.W1), 4), "a0": clk["a1_0_deg"], "P1": clk["P1"], "H": clk["H"],
                "pr": hero_pulse()},
        "rip": tok["pilot"]["ripple"],  # type: ignore[index]
        "n": data["count"],
        "g": data["groups"],
        "names": names,
        "p": data["prefixes"],
        "u": data.get("unlisted", []),
        "r": data["reject"],
        "amb": amb,
        "lw": local_words,
        "core": data.get("core", "127.0.0.1"),
    }


GROUP_TITLES = (("adapter", "Keyed adapters"), ("native", "Native Anthropic Messages"), ("cloud", "Clouds"),
                ("local", "On your machine"))


def provider_list(d: Dict[str, object]) -> str:
    """The advertised providers by route, names and counts from the code."""
    import html as _html
    g, names = d["g"], d["names"]  # type: ignore[assignment]
    rows = []
    for key, title in GROUP_TITLES:
        ids = g[key]  # type: ignore[index]
        rows.append('<li><b>%s <span class="count">%d</span>:</b> %s</li>' % (
            title, len(ids), ", ".join('<span class="pn">%s</span>' % _html.escape(names[i]) for i in ids)))  # type: ignore[index]
    return '<ul class="plist">%s</ul>' % "".join(rows)


def counts(d: Dict[str, object]) -> Dict[str, int]:
    g = d["g"]  # type: ignore[assignment]
    out = {"total": int(d["n"])}  # type: ignore[arg-type]
    for k in ("local", "native", "cloud", "adapter"):
        out[k] = len(g[k])  # type: ignore[index]
    assert sum(out[k] for k in ("local", "native", "cloud", "adapter")) == out["total"]
    return out


def _region(html: str, name: str, body: str) -> str:
    pat = re.compile(r"(<!-- %s:begin -->)(.*?)(<!-- %s:end -->)" % (name, name), re.S)
    if not pat.search(html):
        raise SystemExit("docs/index.html has no %s region" % name)
    return pat.sub(lambda m: m.group(1) + body + m.group(3), html, count=1)


_WORD = re.compile(r"[A-Za-z0-9_$]")
_REGEX_AFTER = set("(,=:[!&|?{};+-*%<>~^") | {"return", "typeof", "case", "else", "in", "of", "void", "delete", ""}
_DROP_NL_AFTER = set(";{,([")
_DROP_NL_BEFORE = set("})];,.")


def _tokens(src: str) -> List[Tuple[str, bool, bool]]:
    """(token, newline_before, space_before) triples; comments and whitespace dropped. Strings, regex literals and words stay whole."""
    out: List[Tuple[str, bool, bool]] = []
    i, n, nl, ws = 0, len(src), False, False
    prev = ""
    while i < n:
        c = src[i]
        if c in " \t\r":
            ws, i = True, i + 1
            continue
        if c == "\n":
            nl, ws, i = True, True, i + 1
            continue
        if src.startswith("//", i):
            ws = True
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            if j < 0:
                raise ValueError("unterminated comment")
            nl, ws = nl or "\n" in src[i:j], True
            i = j + 2
            continue
        if c in "'\"`":
            if c == "`":
                raise ValueError("template literals are not supported by the minifier")
            j = i + 1
            while src[j] != c:
                if src[j] == "\n":
                    raise ValueError("newline in string")
                j += 2 if src[j] == "\\" else 1
            tok = src[i:j + 1]
        elif c == "/" and prev in _REGEX_AFTER:
            j, cls = i + 1, False
            while cls or src[j] != "/":
                if src[j] == "\n":
                    raise ValueError("newline in regex")
                if src[j] == "\\":
                    j += 1
                elif src[j] == "[":
                    cls = True
                elif src[j] == "]":
                    cls = False
                j += 1
            j += 1
            while j < n and _WORD.match(src[j]):
                j += 1
            tok = src[i:j]
            j -= 1
        elif _WORD.match(c):
            j = i
            while j + 1 < n and _WORD.match(src[j + 1]):
                j += 1
            tok = src[i:j + 1]
        else:
            j, tok = i, c
        out.append((tok, nl, ws))
        # what a following "/" means: a regex after punctuation or keywords, a division after a value
        prev = tok if tok in _REGEX_AFTER or (len(tok) == 1 and not _WORD.match(tok)) else "x"
        nl = ws = False
        i = j + 1
    return out


def minify(src: str) -> str:
    """A small, conservative JS minifier (standard library only, so CI and tests can rebuild the page).

    Drops comments and whitespace; keeps a newline wherever the source had one unless the neighbouring
    punctuation makes it redundant, so automatic semicolon insertion behaves exactly as in the source.
    Identifiers are not renamed: the source is written compact by hand."""
    out: List[str] = []
    last = ""
    for tok, nl, ws in _tokens(src):
        if last:
            if nl and not (last[-1] in _DROP_NL_AFTER or tok[0] in _DROP_NL_BEFORE):
                out.append("\n")
            elif (_WORD.match(last[-1]) and _WORD.match(tok[0])) or (ws and last[-1] in "+-" and tok[0] == last[-1]):
                out.append(" ")
        out.append(tok)
        last = tok
    return "".join(out)


def script() -> str:
    """tools/site/site.js, minified and inlined."""
    js = minify((HERE / "site.js").read_text(encoding="utf-8"))
    assert "</script" not in js
    if len(js.encode("utf-8")) > JS_BUDGET:
        raise SystemExit("inline script is %d bytes (budget %d)" % (len(js.encode("utf-8")), JS_BUDGET))
    return "<script>%s</script>" % js


def more_js() -> str:
    """tools/site/more.js, minified: shipped as docs/assets/site/more.js."""
    head = "/* Pulsar Code site: replay player and Pilot sandbox. Generated from tools/site/more.js by tools/site/build.py. */\n"
    return head + minify((HERE / "more.js").read_text(encoding="utf-8")) + "\n"


def more_tag(js: str) -> str:
    v = hashlib.sha256(js.encode("utf-8")).hexdigest()[:10]
    return '<i id="more" hidden data-src="assets/site/more.js?v=%s"></i>' % v


def replay_alts(html: str) -> str:
    """The text alternative of each replay is the scene's own alt (docs/demo/<scene>.json, from tools/demo):
    pasted on its tab as data-alt, and the first tab's under the canvas, so it reads right before any fetch."""
    import html as _html
    first = [None]

    def tab(m: "re.Match[str]") -> str:
        name = m.group(2)
        p = DOCS / "demo" / (name + ".json")
        if not p.is_file():
            return m.group(0)
        alt = " ".join(str(json.loads(p.read_text(encoding="utf-8")).get("alt", "")).split())
        if not alt:
            return m.group(0)
        if first[0] is None:
            first[0] = alt
        tag = re.sub(r'\sdata-alt="[^"]*"', "", m.group(1))
        return tag + ' data-alt="%s"' % _html.escape(alt, quote=True)

    html = re.sub(r'(<button\b[^>]*\brole="tab"[^>]*\bdata-s="(\w+)"[^>]*?)(?=>)', tab, html)
    if first[0]:
        html = re.sub(r'(<p class="alt" id="palt">)[^<]*(</p>)',
                      lambda m: m.group(1) + _html.escape(first[0], quote=False) + m.group(2), html, count=1)
    return html


def render(html: str) -> Tuple[str, Dict[str, int]]:
    d = detector_data()
    n = counts(d)
    html = _region(html, "hero", hero_picture())
    html = _region(html, "constellation", constellation_svg())
    html = _region(html, "mark", mark_symbol())
    html = _region(html, "js", more_tag(more_js()) + script())
    html = _region(html, "plist", provider_list(d))
    html = replay_alts(html)
    payload = json.dumps(d, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = _region(html, "providers", '<script type="application/json" id="pdata">%s</script>' % payload)
    html = re.sub(r'(<[^>]*\bdata-count="(\w+)"[^>]*>)[^<]*(<)',
                  lambda m: m.group(1) + str(n[m.group(2)]) + m.group(3), html)
    return html, n


def main(argv: List[str]) -> int:
    old = INDEX.read_text(encoding="utf-8")
    new, n = render(old)
    more = more_js()
    old_more = MORE.read_text(encoding="utf-8") if MORE.is_file() else ""
    if "--check" in argv:
        if new != old or more != old_more:
            print("docs/index.html or docs/assets/site/more.js is out of date: run python tools/site/build.py")
            return 1
        return 0
    if new != old:
        INDEX.write_text(new, encoding="utf-8")
    if more != old_more:
        MORE.parent.mkdir(parents=True, exist_ok=True)
        MORE.write_text(more, encoding="utf-8")
    js = re.search(r"<!-- js:begin -->.*?<script>(.*?)</script>", new, re.S)
    print("docs/index.html: %d bytes, inline script %d bytes, more.js %d bytes, %d providers (%s)" % (
        len(new.encode("utf-8")), len(js.group(1).encode("utf-8")) if js else 0, len(more.encode("utf-8")), n["total"],
        ", ".join("%s %d" % (k, n[k]) for k in ("local", "native", "cloud", "adapter"))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
