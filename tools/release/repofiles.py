# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Which files make up the repository, and how to read them as text.

Shared by secret_scan.py and em_dash_audit.py. Python 3.9+, standard library only.

list_files() asks git for every tracked file plus every untracked file that is not ignored
(what the next commit could contain), so nothing is skipped because of its folder name: a
tracked docs/build/x.txt or pulsar/venv/x.py is scanned like any other file. Outside a git
checkout (an sdist, a release archive), or with PULSAR_SCAN_NO_GIT=1, it walks the folder
instead, skipping only VCS metadata, caches, node_modules and real virtualenvs (folders
holding a pyvenv.cfg).

read_text() decodes UTF-8, and UTF-16/UTF-32 with or without a byte order mark; anything
else containing NUL bytes is treated as binary and returns None.
"""

import codecs
import os
import shutil
import subprocess

WALK_SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
                  ".ruff_cache", ".tox", ".nox"}


def _git_files(root: str, git: str):
    try:
        p = subprocess.run([git, "-C", root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    names = p.stdout.decode("utf-8", "surrogateescape").split("\0")
    return sorted({n for n in names if n})


def _walk_files(root: str):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        keep = []
        for d in sorted(dirnames):
            full = os.path.join(dirpath, d)
            if d in WALK_SKIP_DIRS or os.path.isfile(os.path.join(full, "pyvenv.cfg")):
                continue
            keep.append(d)
        dirnames[:] = keep
        for fn in sorted(filenames):
            rel = os.path.relpath(os.path.join(dirpath, fn), root)
            out.append(rel.replace(os.sep, "/"))
    return out


def list_files(root: str, git: str = None, use_git: bool = None):
    """Repository-relative paths (forward slashes) of every file worth checking under root.

    An explicit `git` executable is always tried (tests pass a fake one, or a missing path to
    force the walk); otherwise PULSAR_SCAN_NO_GIT=1 selects the walk.
    """
    if use_git is None:
        use_git = bool(git) or os.environ.get("PULSAR_SCAN_NO_GIT", "") != "1"
    if use_git and os.path.exists(os.path.join(root, ".git")):
        git = git or shutil.which("git")
        if git:
            files = _git_files(root, git)
            if files is not None:
                return files
    return _walk_files(root)


def _looks_utf16(sample: bytes):
    """'utf-16-le' / 'utf-16-be' for BOM-less UTF-16 text (mostly ASCII), else None."""
    n = len(sample) // 2
    if n < 2:
        return None
    even = sample[0:2 * n:2].count(0)
    odd = sample[1:2 * n:2].count(0)
    if odd >= 0.6 * n and even <= 0.1 * n:
        return "utf-16-le"
    if even >= 0.6 * n and odd <= 0.1 * n:
        return "utf-16-be"
    return None


def decode(data: bytes):
    """Text of a file's bytes, or None when they look binary."""
    if data.startswith(codecs.BOM_UTF8):
        return data[len(codecs.BOM_UTF8):].decode("utf-8", "replace")
    if data.startswith(codecs.BOM_UTF32_LE) or data.startswith(codecs.BOM_UTF32_BE):
        return data.decode("utf-32", "replace")
    if data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
        return data.decode("utf-16", "replace")
    if b"\0" in data[:8192]:
        enc = _looks_utf16(data[:8192])
        return data.decode(enc, "replace") if enc else None
    return data.decode("utf-8", "replace")


def read_text(path: str, max_bytes: int):
    """(text, None) for a text file, (None, reason) for one that is skipped.

    Raises OSError when the file cannot be read; callers decide whether that is fatal.
    """
    if os.path.islink(path):
        return None, "symlink"
    if os.path.getsize(path) > max_bytes:
        return None, "larger than %d bytes" % max_bytes
    with open(path, "rb") as f:
        data = f.read()
    text = decode(data)
    return (text, None) if text is not None else (None, "binary")
