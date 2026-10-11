# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Tiny SVG builder for the brand generators: numbers, rect runs, wedges, gradients, pixel text,
the reduced-motion block, id/class prefixing for inline use and the <picture> markup emitter.

Python 3.9 compatible; no third-party dependencies.
"""

from __future__ import annotations

import gzip
import re
from html import escape
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import motion as M


def n(v: float, nd: int = 2) -> str:
    """Compact number: 2 decimals at most, no trailing zeros, no leading zero."""
    s = ("%." + str(nd) + "f") % v
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s.startswith("0.") and len(s) > 2:
        s = s[1:]
    elif s.startswith("-0.") and len(s) > 3:
        s = "-" + s[2:]
    if s in ("-0", ""):
        s = "0"
    return s


def ms(t: float) -> str:
    """Seconds as a CSS time with millisecond precision."""
    return n(t, 3) + "s"


def pct(p: float) -> str:
    return n(max(0.0, min(100.0, p)), 3) + "%"


def esc(s: str) -> str:
    return escape(s, quote=True)


def attrs(**kw: object) -> str:
    out = []
    for k, v in kw.items():
        if v is None or v is False:
            continue
        k = k.rstrip("_").replace("_", "-")
        if isinstance(v, float):
            v = n(v)
        out.append('%s="%s"' % (k, esc(str(v))))
    return (" " + " ".join(out)) if out else ""


def tag(name: str, body: str = "", **kw: object) -> str:
    if body:
        return "<%s%s>%s</%s>" % (name, attrs(**kw), body, name)
    return "<%s%s/>" % (name, attrs(**kw))


def g(body: str, **kw: object) -> str:
    return "<g%s>%s</g>" % (attrs(**kw), body)


def rect(x: float, y: float, w: float, h: float, **kw: object) -> str:
    return '<rect x="%s" y="%s" width="%s" height="%s"%s/>' % (n(x), n(y), n(w), n(h), attrs(**kw))


def circle(cx: float, cy: float, r: float, **kw: object) -> str:
    return '<circle cx="%s" cy="%s" r="%s"%s/>' % (n(cx), n(cy), n(r), attrs(**kw))


def stops(pairs: Iterable[Tuple[float, str, float]]) -> str:
    out = []
    for off, col, op in pairs:
        out.append('<stop offset="%s" stop-color="%s"%s/>' % (n(off, 3), col, ' stop-opacity="%s"' % n(op, 3) if op < 1 else ""))
    return "".join(out)


def linear_user(id_: str, x1: float, x2: float, pairs: Iterable[Tuple[float, str, float]], y1: float = 0, y2: float = 0) -> str:
    """userSpaceOnUse linear gradient (works on zero-height rules and rotating wedges)."""
    return '<linearGradient id="%s" gradientUnits="userSpaceOnUse" x1="%s" y1="%s" x2="%s" y2="%s">%s</linearGradient>' % (
        id_, n(x1), n(y1), n(x2), n(y2), stops(pairs))


def radial(id_: str, pairs: Iterable[Tuple[float, str, float]], cx: Optional[float] = None, cy: Optional[float] = None,
           r: Optional[float] = None) -> str:
    if cx is None:
        return '<radialGradient id="%s">%s</radialGradient>' % (id_, stops(pairs))
    return '<radialGradient id="%s" gradientUnits="userSpaceOnUse" cx="%s" cy="%s" r="%s">%s</radialGradient>' % (
        id_, n(cx), n(cy), n(r or 1), stops(pairs))


def spectrum_pairs(theme: Dict[str, object], op: float = 1.0) -> List[Tuple[float, str, float]]:
    cols = theme["spectrum"]  # type: ignore[index]
    return [(o, c, op) for o, c in zip(M.SPECTRUM_STOPS, cols)]  # type: ignore[arg-type]


def wedge_path(length: float, hw: float, base: float = 0.0) -> str:
    """A tapered beam along +x from the origin: base half-width `base`, tip half-width `hw` at `length`."""
    if base:
        return "M0 %sL%s %sL%s %sL0 %sZ" % (n(-base), n(length), n(-hw), n(length), n(hw), n(base))
    return "M0 0L%s %sL%s %sZ" % (n(length), n(-hw), n(length), n(hw))


def pixel_glyph_path(ch: str, x: float, y: float, px: float, bitmap: Optional[Sequence[str]] = None,
                     over: float = 0.0) -> str:
    """One 5x7 glyph as a single path of horizontal runs (much smaller than one rect per pixel)."""
    rows = bitmap or M.PIXEL_FONT.get(ch.upper(), M.PIXEL_FONT[" "])
    d = []
    for r, line in enumerate(rows):
        c = 0
        while c < len(line):
            if line[c] != "1":
                c += 1
                continue
            e = c
            while e < len(line) and line[e] == "1":
                e += 1
            d.append("M%s %sh%sv%sh%sz" % (n(x + c * px), n(y + r * px), n((e - c) * px + over), n(px + over),
                                           n(-(e - c) * px - over)))
            c = e
    return "".join(d)


def pixel_text_width(text: str, px: float, pitch: Optional[float] = None) -> float:
    pitch = pitch if pitch is not None else 6 * px
    return pitch * (len(text) - 1) + 5 * px if text else 0.0


def pixel_text_path(text: str, x: float, y: float, px: float, pitch: Optional[float] = None) -> str:
    pitch = pitch if pitch is not None else 6 * px
    return "".join(pixel_glyph_path(ch, x + i * pitch, y, px) for i, ch in enumerate(text) if ch != " ")


def mono_text(x: float, y: float, text: str, size: float, fill: str, anchor: str = "start", weight: str = "",
              pin: bool = True, **kw: object) -> str:
    """Monospace run pinned with textLength so every fallback font occupies the same width."""
    length = len(text) * size * M.MONO_ADVANCE
    extra = ' textLength="%s" lengthAdjust="spacingAndGlyphs"' % n(length) if pin and text else ""
    return '<text x="%s" y="%s" font-size="%s" fill="%s"%s%s%s%s>%s</text>' % (
        n(x), n(y), n(size), fill, ' text-anchor="%s"' % anchor if anchor != "start" else "",
        ' font-weight="%s"' % weight if weight else "", extra, attrs(**kw), esc(text))


def keyframes(name: str, frames: Sequence[Tuple[float, str]]) -> str:
    return "@keyframes %s{%s}" % (name, "".join("%s{%s}" % (pct(p), body) for p, body in frames))


def reduced_motion(overrides: str = "") -> str:
    """The last rule block of every animated SVG: *{animation:none!important} first, then the poster."""
    return "@media (prefers-reduced-motion: reduce){*{animation:none!important}%s}" % overrides


def svg_doc(w: int, h: int, body: str, css: str = "", defs: str = "", title: str = "", desc: str = "",
            label: str = "", prolog: bool = False, extra: str = "") -> str:
    head = '<?xml version="1.0" encoding="UTF-8"?>\n' if prolog else ""
    lab = ' role="img" aria-label="%s"' % esc(label) if label else ""
    t = "<title>%s</title>" % esc(title) if title else ""
    d = "<desc>%s</desc>" % esc(desc) if desc else ""
    style = "<style>%s</style>" % css if css else ""
    df = "<defs>%s</defs>" % defs if defs else ""
    return ('%s<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d"%s%s>%s%s%s%s%s</svg>\n'
            % (head, w, h, w, h, lab, extra, t, d, style, df, body))


def strip_animation(svg: str) -> str:
    """A -static poster: drop every @keyframes rule and animation declaration, keep the base state."""
    def clean_css(css: str) -> str:
        css = re.sub(r"@keyframes\s+[\w-]+\s*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", css)
        i = css.find("@media (prefers-reduced-motion")
        if i >= 0:
            css = css[:i]
        css = re.sub(r"animation(?:-[a-z-]+)?\s*:[^;}]*;?", "", css)
        css = re.sub(r"(?:will-change|transform-box|transform-origin)\s*:[^;}]*;?", "", css)
        css = re.sub(r"[^{}]+\{\s*\}", "", css)
        return css
    svg = re.sub(r"<style>(.*?)</style>", lambda m: "<style>%s</style>" % clean_css(m.group(1)) if clean_css(m.group(1)).strip()
                 else "", svg, flags=re.S)
    svg = re.sub(r"\s*animation-delay:[^;\"]*;?", "", svg)
    svg = re.sub(r'\sstyle="\s*"', "", svg)
    return svg


def prefix_ids(svg: str, p: str, classes: bool = True) -> str:
    """Prefix every id, url(#id), href="#id", class name and @keyframes name (for inlining in HTML)."""
    ids = set(re.findall(r'\sid="([^"]+)"', svg))
    for i in sorted(ids, key=len, reverse=True):
        svg = re.sub(r'(\sid=")%s(")' % re.escape(i), r"\g<1>%s%s\2" % (p, i), svg)
        svg = svg.replace("url(#%s)" % i, "url(#%s%s)" % (p, i)).replace('href="#%s"' % i, 'href="#%s%s"' % (p, i))
    if not classes:
        return svg

    def fix_css(css: str) -> str:
        names = set(re.findall(r"@keyframes\s+([\w-]+)", css))
        css = re.sub(r"(?<![\w.#-])\.([A-Za-z][\w-]*)", lambda m: "." + p + m.group(1), css)

        def anim(m: "re.Match[str]") -> str:
            val = m.group(2)
            for k in sorted(names, key=len, reverse=True):
                val = re.sub(r"(?<![\w-])%s(?![\w-])" % re.escape(k), p + k, val)
            return m.group(1) + val
        css = re.sub(r"(animation(?:-name)?\s*:)([^;}]*)", anim, css)
        css = re.sub(r"@keyframes\s+([\w-]+)", lambda m: "@keyframes " + p + m.group(1), css)
        return css
    svg = re.sub(r"<style>(.*?)</style>", lambda m: "<style>%s</style>" % fix_css(m.group(1)), svg, flags=re.S)
    svg = re.sub(r'(\sclass=")([^"]+)(")', lambda m: m.group(1) + " ".join(p + c for c in m.group(2).split()) + m.group(3), svg)
    return svg


# Static poster naming. One rule, used by the generators, check.py and picture():
#   animated  <stem>-<theme>.<v>.svg
#   poster    <stem>-<theme>-static.<v>.svg
# except the hero, whose spec name (section 4.1) is hero-static-<theme>.<v>.svg; the Pages build
# (tools/site/build.py) and the README already depend on that, so it stays the one named exception.
STATIC_FIRST = ("hero",)


def themed_name(stem: str, theme: str, static: bool = False, version: str = "v3") -> str:
    """File name of a themed asset: themed_name("signal", "dark", True) -> signal-dark-static.v3.svg."""
    if not static:
        return "%s-%s.%s.svg" % (stem, theme, version)
    if stem in STATIC_FIRST:
        return "%s-static-%s.%s.svg" % (stem, theme, version)
    return "%s-%s-static.%s.svg" % (stem, theme, version)


THEMED_RE = re.compile(r"^(?P<stem>.+?)-(?:static-)?(?P<theme>dark|light)(?P<static>-static)?\.(?P<v>v\d+)\.svg$")


def parse_themed(name: str) -> Optional[Tuple[str, str, bool, str]]:
    """(stem, theme, static, version) of a themed file name, or None. Accepts both poster spellings
    so check.py can report a non-canonical one instead of silently skipping it."""
    m = THEMED_RE.match(name)
    if not m:
        return None
    static = bool(m.group("static")) or name.startswith(m.group("stem") + "-static-")
    return m.group("stem"), m.group("theme"), static, m.group("v")


# PNG stills of the posters, shown on narrow screens. Seen in October 2026: GitHub's phone app drew the PNG
# files of a private repository's README and left every SVG file of the repository as a broken image.
# A still's name has no dot before its extension.
NARROW = "(max-width: 767px)"


def still_name(stem: str, theme: str, version: str = "v3") -> str:
    """File name of a poster's PNG still: still_name("signal", "dark") -> signal-dark-still-v3.png."""
    return "%s-%s-still-%s.png" % (stem, theme, version)


def picture(base: str, alt: str, width: str = "100%", static: bool = True, version: str = "v3",
            stills: bool = True) -> str:
    """The 6-source <picture> for a README asset: PNG stills on narrow screens, then reduced motion
    and colour scheme.

    base: path without the theme suffix, e.g. "assets/readme/signal" -> signal-dark.v3.svg,
    posters signal-dark-static.v3.svg (names from themed_name()), stills signal-dark-still-v3.png
    (still_name()). The narrow sources come first, so a narrow screen never selects an SVG.
    """
    head, _, stem = base.rpartition("/")
    pre = head + "/" if head else ""
    d, l_ = pre + themed_name(stem, "dark", False, version), pre + themed_name(stem, "light", False, version)
    sd, sl = pre + themed_name(stem, "dark", True, version), pre + themed_name(stem, "light", True, version)
    lines = ["<picture>"]
    if stills:
        lines.append('  <source media="%s and (prefers-color-scheme: dark)" srcset="%s">'
                     % (NARROW, pre + still_name(stem, "dark", version)))
        lines.append('  <source media="%s" srcset="%s">' % (NARROW, pre + still_name(stem, "light", version)))
    if static:
        lines.append('  <source media="(prefers-reduced-motion: reduce) and (prefers-color-scheme: dark)" srcset="%s">' % sd)
        lines.append('  <source media="(prefers-reduced-motion: reduce)" srcset="%s">' % sl)
    lines.append('  <source media="(prefers-color-scheme: dark)" srcset="%s">' % d)
    lines.append('  <img src="%s" width="%s" alt="%s">' % (l_, width, esc(alt)))
    lines.append("</picture>")
    return "\n".join(lines)


def gz_size(s: str) -> int:
    return len(gzip.compress(s.encode("utf-8"), 9))
