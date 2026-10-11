#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Fail when any repository text file contains an em dash (U+2014).

    python tools/release/em_dash_audit.py [paths...]   (default: the whole repository)

House style: no em dash characters anywhere. Every file git tracks (plus untracked files
that are not ignored) is checked whatever its name or extension: CODEOWNERS, .gitignore,
LICENSE, .css, .mjs, ... Binary files are skipped; UTF-16/UTF-32 text is decoded first (see
repofiles.py). Exit status 0 when clean, 1 when an em dash is found, 2 when a file could not
be read (a failure, never "clean").
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

import repofiles  # noqa: E402

EM_DASH = chr(0x2014)  # spelled out so this file passes its own audit
MAX_BYTES = 50 * 1024 * 1024


def audit(paths=None, root: str = REPO, git: str = None):
    """(hits, errors): hits are (relpath, line numbers), errors are (relpath, reason)."""
    targets = []
    if paths:
        for top in paths:
            if os.path.isdir(top):
                targets += [os.path.join(top, rel) for rel in repofiles.list_files(top, git=git)]
            else:
                targets.append(top)
    else:
        targets = [os.path.join(root, rel) for rel in repofiles.list_files(root, git=git)]
    hits, errors = [], []
    for path in targets:
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        if not os.path.lexists(path) or os.path.isdir(path):
            continue  # deleted in the worktree, or a submodule
        try:
            text, why = repofiles.read_text(path, MAX_BYTES)
        except OSError as exc:
            errors.append((rel, str(exc)))
            continue
        if text is None:
            if why and why.startswith("larger"):
                errors.append((rel, why))
            continue
        if EM_DASH in text:
            lines = [n for n, line in enumerate(text.splitlines(), 1) if EM_DASH in line]
            hits.append((rel, lines))
    return hits, errors


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    hits, errors = audit(argv or None)
    gh = bool(os.environ.get("GITHUB_ACTIONS"))
    for rel, lines in hits:
        for n in lines[:20]:
            print(("::error file=%s,line=%d::em dash (U+2014)" % (rel, n)) if gh else
                  "%s:%d: em dash (U+2014)" % (rel, n))
    for rel, why in errors:
        print("%s: could not check: %s" % (rel, why), file=sys.stderr)
    if hits:
        print("em-dash audit: %d file(s) contain U+2014; use a colon, comma, parentheses or a "
              "plain hyphen instead." % len(hits), file=sys.stderr)
        return 1
    if errors:
        return 2
    print("em-dash audit clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
