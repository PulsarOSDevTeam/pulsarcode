#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Regenerate every Pulsar Code brand asset, deterministically, then validate them.

    python tools/brand/make_assets.py              # everything (PNG steps need Playwright + Chromium)
    python tools/brand/make_assets.py --no-png     # vector assets only (no browser needed)
    python tools/brand/make_assets.py --mp4        # also an MP4 clip into $PULSAR_LAUNCH_DIR (outside the repo)

Steps: tokens.json (motion.py), hero (hero.py), README art (readme_art.py), mark family (mark.py),
social.png and the PNG stills of the README posters (capture.py), the docs/assets/brand copies and
providers.json, removal of the pre-v3 hero files, then check.py. See tools/brand/README.md.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import List, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import motion as M  # noqa: E402

ROOT = M.ROOT
BRAND = ROOT / "assets" / "brand"
SITE = ROOT / "docs" / "assets" / "brand"
LEGACY = ("hero.svg", "hero-dark.svg", "hero-light.svg")


def _hero():
    import hero
    return hero


# Named chips on social.png; the "+N" chip and every count are computed from
# pulsar.screens.advertised_providers(), so they never drift from the code.
SOCIAL_CHIPS = _hero().SOCIAL_CHIPS


def public_provider_count() -> int:
    return M.provider_count()


def social_html() -> str:
    """The capture page for social.png: the social hero layout plus the chips and footer line."""
    import capture
    h = _hero()
    return capture.social_html(h.build("dark", h.SOCIAL, True))


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def vector_assets() -> List[Path]:
    import hero
    import mark
    import readme_art
    out = [M.write_tokens()]
    for group in (hero.outputs(), hero.site_outputs(), readme_art.outputs(), readme_art.site_outputs(), mark.svg_outputs()):
        for path, svg in group.items():
            write(path, svg)
            out.append(path)
    write(SITE / "providers.json", json.dumps(M.providers_json(), indent=1, ensure_ascii=False) + "\n")
    out.append(SITE / "providers.json")
    shutil.copyfile(M.TOKENS_PATH, SITE / "tokens.json")
    out.append(SITE / "tokens.json")
    for name in ("mark.svg", "favicon.svg"):
        shutil.copyfile(BRAND / name, SITE / name)
        out.append(SITE / name)
    return out


def raster_assets() -> List[Path]:
    import capture
    import mark
    out = mark.png_outputs()
    out.append(capture.render_social(BRAND / "social.png"))
    out += capture.render_stills()
    for name in ("favicon-32.png", "apple-touch-icon.png", "social.png"):
        shutil.copyfile(BRAND / name, SITE / name)
        out.append(SITE / name)
    return out


def remove_legacy() -> None:
    if all((BRAND / ("hero-%s.%s.svg" % (t, _hero().VERSION))).exists() for t in ("dark", "light")):
        for name in LEGACY:
            p = BRAND / name
            if p.exists():
                p.unlink()


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    paths = vector_assets()
    if "--no-png" not in argv:
        import capture
        if capture.find_chromium() or _has_playwright_browser():
            paths += raster_assets()
        else:
            print("make_assets: no Chromium found; PNG assets left as they are", file=sys.stderr)
    if "--mp4" in argv:
        import capture
        print(capture.render_mp4(Path(__import__("os").environ.get("PULSAR_LAUNCH_DIR", "/tmp/pulsar-launch/"))))
    remove_legacy()
    import check
    return check.main(["assets/brand", "assets/readme", "docs/assets/brand"] + (["--quiet"] if "--quiet" in argv else []))


def _has_playwright_browser() -> bool:
    try:
        __import__("playwright")
    except ImportError:
        return False
    return True


if __name__ == "__main__":
    sys.exit(main())
