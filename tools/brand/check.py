#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Validate brand and README artwork (and terminal replays): safety, copy rules, budgets.

    python tools/brand/check.py                         # assets/brand assets/readme docs/assets/brand
    python tools/brand/check.py assets docs/assets/brand README.md
    python tools/brand/check.py --quiet assets/demo

Fails (exit 1) on: <script, on*= attributes, foreignObject, @import, url(http, external href or
<image>, em dashes, banned phrases ("free Claude Code", "Claude Code x"), anything shaped like a
real key (the obvious fake sk-or-v1-fake-key-not-real excepted), a "<N> providers" count that
differs from len(advertised_providers()), the size budgets in BUDGETS below, animation inside a
-static file, an animated SVG without its prefers-reduced-motion block (with *{animation:none}
first), a hero whose .cell rules use animation-fill-mode both/backwards, and (in assets/brand and
assets/readme) a themed poster not named by svgkit.themed_name() or an animated themed SVG
without its two posters.
Prints a size table (raw and gzip) for every file it checks. Python 3.9 compatible.
"""

from __future__ import annotations

import fnmatch
import gzip
import re
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EM_DASH = chr(0x2014)
FAKE_KEY = "sk-or-v1-fake-key-not-real"
KEY_SHAPE = re.compile(r"(?<![A-Za-z0-9])(?:sk|gsk|nvapi|xai|hf|AIza|ghp|github_pat)[-_A-Za-z0-9]{12,}")
BANNED = ("free claude code", "claude code x ", "claude code ×", "claude code x\n")
TEXT_EXT = {".svg", ".html", ".htm", ".md", ".json", ".css", ".js", ".txt"}
BINARY_EXT = {".png", ".mp4", ".gif", ".woff2", ".ico", ".jpg", ".webp"}

KB = 1024
# (glob on the path relative to the repo, raw budget bytes, gzip budget bytes or None)
BUDGETS: Tuple[Tuple[str, int, Optional[int]], ...] = (
    ("*hero-static-*.svg", 50 * KB, None),
    ("*hero-inline-*.svg", 110 * KB, 16 * KB),
    ("*hero-dark.v*.svg", 110 * KB, 16 * KB),
    ("*hero-light.v*.svg", 110 * KB, 16 * KB),
    ("*divider-*.svg", 10 * KB, None),
    ("*signal-*.svg", 30 * KB, None),
    ("*card-*.svg", 45 * KB, None),
    ("*constellation*.svg", 35 * KB, None),
    ("*footer-*.svg", 12 * KB, None),
    ("*brand/mark.svg", 3 * KB, None),
    ("*brand/favicon.svg", int(1.5 * KB), None),
    ("*brand/favicon-*.png", 2 * KB, None),
    ("*brand/logo*.svg", 8 * KB, None),
    ("*brand/avatar-512.png", 40 * KB, None),
    ("*brand/apple-touch-icon.png", 24 * KB, None),
    ("*brand/social.png", 300 * KB, None),
    ("*demo/session.svg", 600 * KB, 120 * KB),
    ("*demo/*.svg", 650 * KB, 120 * KB),
    ("*demo/posters/*.png", 90 * KB, None),
)
SET_BUDGETS: Tuple[Tuple[str, int], ...] = (
    ("*readme/card-*-dark.v*.svg", 160 * KB),
    ("*readme/card-*-light.v*.svg", 160 * KB),
    ("assets/demo/*.svg", int(3.6 * 1024 * KB)),
)


def provider_count() -> int:
    from pulsar.screens import advertised_providers
    return len(advertised_providers())


def rel(p: Path) -> str:
    try:
        return p.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return p.as_posix()


def budget_for(path: str) -> Optional[Tuple[int, Optional[int]]]:
    for pat, raw, gz in BUDGETS:
        if fnmatch.fnmatch(path, pat):
            return raw, gz
    return None


def flat_text(text: str) -> str:
    """Markup removed (so '36</tspan> providers' still reads as '36 providers')."""
    t = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    t = re.sub(r"<style[^>]*>.*?</style>", " ", t, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    t = t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#183;", "·")
    return re.sub(r"\s+", " ", t)


def css_blocks(text: str) -> str:
    return "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", text, flags=re.S))


def _reduced_motion_ok(css: str) -> Optional[str]:
    i = css.rfind("@media (prefers-reduced-motion: reduce)")
    if i < 0:
        i = css.rfind("@media (prefers-reduced-motion:reduce)")
    if i < 0:
        return "animated SVG without a prefers-reduced-motion block"
    block = css[i:]
    body = block[block.find("{") + 1:]
    star = re.search(r"\*\s*\{\s*animation\s*:\s*none\s*!important", body)
    if not star:
        return "reduced-motion block lacks *{animation:none!important}"
    first_rule = re.search(r"[^\s{}][^{}]*\{", body)
    if first_rule and not first_rule.group(0).strip().startswith("*"):
        return "reduced-motion block must start with *{animation:none!important} (before the poster overrides)"
    # it must be the last rule block of the stylesheet: nothing but closing braces after its end
    depth, end = 0, None
    for k, ch in enumerate(block):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = k
                break
    if end is not None and block[end + 1:].strip():
        return "the reduced-motion block must be the last rule block of the stylesheet"
    return None


def check_text(path: str, text: str, count: Optional[int]) -> List[str]:
    errs: List[str] = []
    low = text.lower()
    is_markup = path.endswith((".svg", ".html", ".htm"))
    is_svg = path.endswith(".svg")
    if is_markup or path.endswith(".md"):
        if "<script" in low:
            errs.append("contains <script")
        if re.search(r"<[^>]*\son[a-z]+\s*=", text, flags=re.I):
            errs.append("event handler attribute (on*=)")
        if "foreignobject" in low:
            errs.append("contains foreignObject")
    if is_svg:
        if "@import" in low:
            errs.append("contains @import")
        if re.search(r"url\(\s*['\"]?https?:", text, flags=re.I):
            errs.append("external url(http...)")
        for m in re.finditer(r"""(?:xlink:)?href\s*=\s*["']https?://[^"']*["']""", text):
            errs.append("external href: %s" % m.group(0)[:80])
        for m in re.finditer(r"<image\b[^>]*>", text, flags=re.I):
            if re.search(r"""href\s*=\s*["'](?!data:|#)""", m.group(0)):
                errs.append("<image> with an external href")
    if EM_DASH in text:
        line = text[:text.index(EM_DASH)].count("\n") + 1
        errs.append("em dash (U+2014) at line %d" % line)
    flat = flat_text(text) if (is_markup or path.endswith(".md")) else text
    flow = " ".join(flat.lower().split()) + " "
    for phrase in BANNED:
        if phrase.strip() and phrase in flow:
            errs.append("banned phrase %r" % phrase.strip())
    scrub = text.replace(FAKE_KEY, "")
    for m in KEY_SHAPE.finditer(scrub):
        errs.append("key-shaped string %r" % (m.group(0)[:6] + "..."))
    if count is not None and (is_markup or path.endswith((".md", ".json"))):
        for m in re.finditer(r"(?<![\d.+])(\d+)\s+providers?\b", flat):
            if int(m.group(1)) != count:
                errs.append("'%s' but advertised_providers() has %d" % (m.group(0), count))
    if is_svg:
        css = css_blocks(text)
        animated = "@keyframes" in css or "<animate" in text
        if "-static" in Path(path).name and animated:
            errs.append("-static file contains animation (@keyframes or <animate>)")
        if animated:
            why = _reduced_motion_ok(css)
            if why:
                errs.append(why)
        if "hero" in Path(path).name:
            for sel, body in re.findall(r"([^{}]*)\{([^{}]*)\}", css):
                if re.search(r"\.cell\b", sel) and re.search(r"\b(both|backwards)\b", body) and "animation" in body:
                    errs.append(".cell animation uses fill-mode both/backwards (forwards only)")
    return errs


POSTER_DIRS = ("assets/brand/", "assets/readme/")


def poster_names(paths: Iterable[str]) -> List[str]:
    """Themed posters follow svgkit.themed_name(); every animated themed SVG has both posters."""
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    import svgkit as K
    errs: List[str] = []
    have = set(paths)
    for r in sorted(have):
        if not r.startswith(POSTER_DIRS):
            continue
        d, _, name = r.rpartition("/")
        got = K.parse_themed(name)
        if not got:
            continue
        stem, theme, static, v = got
        want = K.themed_name(stem, theme, static, v)
        if name != want:
            errs.append("%s: poster name should be %s (svgkit.themed_name)" % (r, want))
        if not static and theme == "dark":
            for t in ("dark", "light"):
                sib = "%s/%s" % (d, K.themed_name(stem, t, True, v))
                if sib not in have:
                    errs.append("%s: missing poster %s" % (r, sib))
    return errs


def iter_files(targets: Iterable[str]) -> List[Path]:
    out: List[Path] = []
    for t in targets:
        p = Path(t)
        if not p.is_absolute():
            p = (Path.cwd() / p) if (Path.cwd() / p).exists() else ROOT / t
        if p.is_dir():
            out += sorted(f for f in p.rglob("*") if f.is_file() and "__pycache__" not in f.parts)
        elif p.is_file():
            out.append(p)
    return out


def check(targets: Iterable[str], quiet: bool = False) -> Tuple[List[str], List[Tuple[str, int, int]]]:
    count = provider_count()
    failures: List[str] = []
    table: List[Tuple[str, int, int]] = []
    files = iter_files(targets)
    for f in files:
        r = rel(f)
        ext = f.suffix.lower()
        data = f.read_bytes()
        gz = len(gzip.compress(data, 9))
        if ext in TEXT_EXT or ext in BINARY_EXT:
            table.append((r, len(data), gz))
        if ext in TEXT_EXT:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                failures.append("%s: not UTF-8" % r)
                continue
            for e in check_text(r, text, count):
                failures.append("%s: %s" % (r, e))
        b = budget_for(r)
        if b:
            raw, gzb = b
            if len(data) > raw:
                failures.append("%s: %.1f KB raw > budget %.1f KB" % (r, len(data) / KB, raw / KB))
            if gzb and gz > gzb:
                failures.append("%s: %.1f KB gzip > budget %.1f KB" % (r, gz / KB, gzb / KB))
    failures += poster_names(r for r, _, _ in table)
    for pat, cap in SET_BUDGETS:
        members = [(r, n) for r, n, _ in table if fnmatch.fnmatch(r, pat)]
        total = sum(n for _, n in members)
        if members and total > cap:
            failures.append("set %s: %.1f KB > budget %.1f KB" % (pat, total / KB, cap / KB))
    return failures, table


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    quiet = "--quiet" in argv
    targets = [a for a in argv if not a.startswith("--")] or ["assets/brand", "assets/readme", "docs/assets/brand"]
    failures, table = check(targets, quiet)
    if not quiet:
        w = max([len(r) for r, _, _ in table] + [10])
        print("%-*s %9s %9s" % (w, "file", "raw KB", "gzip KB"))
        for r, n, gz in table:
            print("%-*s %9.1f %9.1f" % (w, r, n / KB, gz / KB))
    for f in failures:
        print("FAIL " + f, file=sys.stderr)
    print("check.py: %d file(s), %d failure(s)" % (len(table), len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
