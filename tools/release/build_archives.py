#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Build the release app archives and SHA256SUMS.

    python tools/release/build_archives.py [--version X.Y.Z] [--out dist] [--checksums]

Writes dist/pulsarcode-app-<version>.tar.gz and .zip: the engine (pulsar/), the `pulsarcode`
shim, both installers, requirements.txt, pyproject.toml, README, CHANGELOG and LICENSE under a
single `pulsarcode-<version>/` folder. Homebrew installs from the tarball, Scoop from the zip,
and anyone can unpack either one and run install.sh / install.ps1 from it offline.

The engine is taken the way the wheel takes it: every file under pulsar/ that git tracks or would
add (dot folders such as the Stage pane's .claude-plugin/ manifest included), without caches,
compiled files or the Stage pane plugin's own dev tests. Outside a git checkout the folder is
walked instead (see repofiles.py).

The archives are reproducible: a fixed file list in sorted order, normalised owners and
permissions, and timestamps from SOURCE_DATE_EPOCH (default 2026-01-01). With --checksums,
dist/SHA256SUMS covers every file in the output folder (sdist and wheel included) in the
format `sha256sum -c` understands.
"""

import argparse
import gzip
import hashlib
import io
import os
import re
import sys
import tarfile
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import repofiles  # noqa: E402

REPO = os.path.dirname(os.path.dirname(HERE))
TOP_FILES = ("pulsarcode", "install.sh", "install.ps1", "requirements.txt", "pyproject.toml",
             "README.md", "CHANGELOG.md", "LICENSE", "SECURITY.md")
EXECUTABLE = {"pulsarcode", "install.sh"}
DEFAULT_EPOCH = 1767225600  # 2026-01-01T00:00:00Z
# Never in an archive, like the wheel (pyproject.toml) and .gitignore: caches, compiled and editor
# files, installed node packages, and the Stage pane plugin's own tests.
SKIP_DIRS = {"__pycache__", "node_modules"}
SKIP_SUFFIXES = (".pyc", ".pyo", ".pyd", ".swp", "$py.class")
SKIP_NAMES = {".DS_Store"}
SKIP_PREFIXES = ("pulsar/fnplug/tests/",)


def read_version(root: str = REPO) -> str:
    with open(os.path.join(root, "pulsar", "__init__.py"), encoding="utf-8") as f:
        m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', f.read(), re.M)
    if not m:
        raise SystemExit("could not read __version__ from pulsar/__init__.py")
    return m.group(1)


def _engine_file(rel: str) -> bool:
    parts = rel.split("/")
    if parts[0] != "pulsar" or len(parts) < 2 or rel.startswith(SKIP_PREFIXES):
        return False
    if any(p in SKIP_DIRS for p in parts[:-1]):
        return False
    return parts[-1] not in SKIP_NAMES and not parts[-1].endswith(SKIP_SUFFIXES)


def collect(root: str = REPO) -> list:
    """Sorted relative paths that go into the archive."""
    files = []
    for name in TOP_FILES:
        if os.path.isfile(os.path.join(root, name)):
            files.append(name)
    for rel in repofiles.list_files(root):
        full = os.path.join(root, *rel.split("/"))
        if _engine_file(rel) and os.path.isfile(full) and not os.path.islink(full):
            files.append(rel)
    for required in ("pulsarcode", "install.sh", "install.ps1", "requirements.txt", "pulsar/__init__.py"):
        if required not in files:
            raise SystemExit("missing %s; run from a full checkout" % required)
    return sorted(files)


def _mode(rel: str) -> int:
    return 0o755 if rel in EXECUTABLE else 0o644


def build_tar(root: str, files: list, prefix: str, dest: str, epoch: int) -> None:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        dirs = {prefix}
        for rel in files:
            parts = rel.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                dirs.add(prefix + "/" + "/".join(parts[:i]))
        for d in sorted(dirs):
            info = tarfile.TarInfo(d)
            info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o755, epoch
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info)
        for rel in files:
            with open(os.path.join(root, rel), "rb") as f:
                data = f.read()
            info = tarfile.TarInfo(prefix + "/" + rel)
            info.size, info.mode, info.mtime = len(data), _mode(rel), epoch
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    with open(dest, "wb") as out:
        with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=epoch, compresslevel=9) as gz:
            gz.write(raw.getvalue())


def build_zip(root: str, files: list, prefix: str, dest: str, epoch: int) -> None:
    stamp = time.gmtime(max(epoch, 315532800))[:6]  # zip cannot store dates before 1980
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in files:
            info = zipfile.ZipInfo(prefix + "/" + rel, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # unix, so the permission bits below are honoured
            info.external_attr = (0o100000 | _mode(rel)) << 16
            with open(os.path.join(root, rel), "rb") as f:
                z.writestr(info, f.read())


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_checksums(out: str) -> str:
    names = sorted(n for n in os.listdir(out)
                   if os.path.isfile(os.path.join(out, n)) and n != "SHA256SUMS" and not n.startswith("."))
    dest = os.path.join(out, "SHA256SUMS")
    with open(dest, "w", encoding="ascii", newline="\n") as f:
        for n in names:
            f.write("%s  %s\n" % (sha256(os.path.join(out, n)), n))
    return dest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", default=None)
    ap.add_argument("--out", default=os.path.join(REPO, "dist"))
    ap.add_argument("--root", default=REPO, help="checkout to package (default: this repo)")
    ap.add_argument("--checksums", action="store_true", help="also write SHA256SUMS for the whole folder")
    ap.add_argument("--no-archives", action="store_true", help="only write SHA256SUMS")
    args = ap.parse_args(argv)
    root = os.path.abspath(args.root)
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    if not args.no_archives:
        version = args.version or read_version(root)
        if not re.match(r"^[0-9]+\.[0-9]+\.[0-9]+([.-]?[A-Za-z0-9.]+)?$", version):
            raise SystemExit("unexpected version %r" % version)
        epoch = int(os.environ.get("SOURCE_DATE_EPOCH") or DEFAULT_EPOCH)
        files = collect(root)
        prefix = "pulsarcode-" + version
        tgz = os.path.join(out, "pulsarcode-app-%s.tar.gz" % version)
        zp = os.path.join(out, "pulsarcode-app-%s.zip" % version)
        build_tar(root, files, prefix, tgz, epoch)
        build_zip(root, files, prefix, zp, epoch)
        for p in (tgz, zp):
            print("%s  %s  (%d files)" % (sha256(p), os.path.basename(p), len(files)))
    if args.checksums or args.no_archives:
        print("wrote " + write_checksums(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
