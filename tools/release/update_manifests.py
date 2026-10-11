#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Point the Homebrew formula, the Scoop manifest and the npm package at a release.

    python tools/release/update_manifests.py --dist dist            # hash local release files
    python tools/release/update_manifests.py --sums dist/SHA256SUMS # trust a checksum file
    python tools/release/update_manifests.py --download              # fetch the published assets
    python tools/release/update_manifests.py --check-tag v3.0.0      # tag, __version__, npm agree?
    python tools/release/update_manifests.py --npm-version 3.1.0rc1  # prints 3.1.0-rc.1

The version defaults to __version__ in pulsar/__init__.py. The release workflow runs the
--dist form after the GitHub release exists and opens a pull request with the result:

  packaging/homebrew/pulsarcode.rb   url + sha256 of pulsarcode-app-<v>.tar.gz
  packaging/scoop/pulsarcode.json    version + url + hash + extract_dir of pulsarcode-app-<v>.zip
  packaging/npm/package.json         version (npm spelling, see below)

Versions are PEP 440 (X.Y.Z, X.Y.ZaN, X.Y.ZbN, X.Y.ZrcN, .postN, .devN); tags are v + that.
npm only takes semver, so the npm package spells pre-releases the semver way:
3.1.0a1 -> 3.1.0-alpha.1, 3.1.0b2 -> 3.1.0-beta.2, 3.1.0rc1 -> 3.1.0-rc.1,
3.1.0.dev3 -> 3.1.0-dev.3, 3.1.0rc1.dev2 -> 3.1.0-rc.1.dev.2. Post-releases have no semver
equivalent: npm keeps the X.Y.Z package (which installs vX.Y.Z) and the release skips npm.
Pre-releases and dev releases are published to npm under the `next` dist-tag and never
touch the Homebrew and Scoop manifests.

--check-tag also writes version, npm_version, npm_tag and prerelease to $GITHUB_OUTPUT when
that variable is set (the release workflow's verify job).

Python 3.9+, standard library only.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SLUG = "PulsarOSDevTeam/pulsarcode"
FORMULA = os.path.join(REPO, "packaging", "homebrew", "pulsarcode.rb")
SCOOP = os.path.join(REPO, "packaging", "scoop", "pulsarcode.json")
NPM = os.path.join(REPO, "packaging", "npm", "package.json")
# The PEP 440 subset Pulsar Code uses for releases (no epochs, no local versions).
# Canonical spelling only (no leading zeros), so tag <-> npm version maps both ways exactly.
_N = r"(?:0|[1-9][0-9]*)"
VERSION_RE = re.compile(r"^(?P<release>{n}\.{n}\.{n})(?:(?P<pre>a|b|rc)(?P<pre_n>{n}))?"
                        r"(?:\.post(?P<post>{n}))?(?:\.dev(?P<dev>{n}))?$".format(n=_N))
SEMVER_PRE = {"a": "alpha", "b": "beta", "rc": "rc"}
# semver.org's reference expression
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$")


def parse_version(version: str):
    m = VERSION_RE.match(version or "")
    if not m:
        raise ValueError("not a release version (X.Y.Z[a|b|rcN][.postN][.devN]): %r" % (version,))
    return m


def is_prerelease(version: str) -> bool:
    m = parse_version(version)
    return bool(m.group("pre") or m.group("dev") is not None)


def npm_version(version: str):
    """The npm (semver) spelling of a PEP 440 release version, or None for post-releases."""
    m = parse_version(version)
    if m.group("post") is not None:
        return None
    parts = []
    if m.group("pre"):
        parts += [SEMVER_PRE[m.group("pre")], m.group("pre_n")]
    if m.group("dev") is not None:
        parts += ["dev", m.group("dev")]
    return m.group("release") + ("-" + ".".join(parts) if parts else "")


def npm_package_version(version: str) -> str:
    """What packaging/npm/package.json must say for this release (the base release for .postN)."""
    mapped = npm_version(version)
    if mapped is not None:
        return mapped
    m = parse_version(version)
    base = m.group("release") + ((m.group("pre") + m.group("pre_n")) if m.group("pre") else "")
    return npm_version(base)


def read_version(root: str = REPO) -> str:
    with open(os.path.join(root, "pulsar", "__init__.py"), encoding="utf-8") as f:
        m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', f.read(), re.M)
    if not m:
        raise SystemExit("could not read __version__ from pulsar/__init__.py")
    return m.group(1)


def asset_names(version: str) -> dict:
    return {"tar": "pulsarcode-app-%s.tar.gz" % version, "zip": "pulsarcode-app-%s.zip" % version}


def asset_url(version: str, name: str) -> str:
    return "https://github.com/%s/releases/download/v%s/%s" % (SLUG, version, name)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_url(url: str) -> str:
    h = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=60) as r:  # noqa: S310 (fixed https URL)
        for chunk in iter(lambda: r.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_sums(path: str) -> dict:
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(\S.*?)\s*$", line)
            if m:
                out[os.path.basename(m.group(2))] = m.group(1).lower()
    return out


def update_formula(text: str, url: str, digest: str) -> str:
    new, n_url = re.subn(r'(?m)^(  url ")[^"]*(")', lambda m: m.group(1) + url + m.group(2), text, count=1)
    new, n_sha = re.subn(r'(?m)^(  sha256 ")[0-9a-fA-F]*(")', lambda m: m.group(1) + digest + m.group(2), new, count=1)
    if n_url != 1 or n_sha != 1:
        raise SystemExit("pulsarcode.rb: could not find the top-level url/sha256 lines")
    return new


def update_scoop(data: dict, version: str, url: str, digest: str) -> dict:
    data["version"] = version
    data["url"] = url
    data["hash"] = digest
    data["extract_dir"] = "pulsarcode-" + version
    return data


def write_json(path: str, data: dict, indent: int) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)
        f.write("\n")


def check_tag(tag: str, version: str, npm_file: str = NPM, output: str = None) -> int:
    problems = []
    if tag.startswith("refs/tags/"):
        tag = tag[len("refs/tags/"):]
    if tag != "v" + version:
        problems.append("tag %s does not match pulsar.__version__ %s (expected v%s)" % (tag, version, version))
    try:
        parse_version(version)
    except ValueError as exc:
        problems.append(str(exc))
    with open(npm_file, encoding="utf-8") as f:
        have = json.load(f).get("version")
    want = None if problems else npm_package_version(version)
    if want is not None and have != want:
        problems.append("packaging/npm/package.json version %s != %s (npm spelling of %s)" % (have, want, version))
    if want is not None and not SEMVER_RE.match(want):
        problems.append("npm version %s is not valid semver" % want)
    for p in problems:
        print("::error::" + p if os.environ.get("GITHUB_ACTIONS") else "error: " + p, file=sys.stderr)
    if problems:
        return 1
    publish = npm_version(version) or ""
    pre = is_prerelease(version)
    print("versions agree: %s (npm %s%s)" % (tag, want, "" if publish else ", not published to npm"))
    output = output if output is not None else os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as f:
            f.write("version=%s\n" % version)
            f.write("npm_version=%s\n" % publish)
            f.write("npm_tag=%s\n" % ("next" if pre else "latest"))
            f.write("prerelease=%s\n" % ("true" if pre else "false"))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", default=None, help="default: pulsar.__version__")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--dist", help="folder holding the release archives")
    src.add_argument("--sums", help="a SHA256SUMS file from the release")
    src.add_argument("--download", action="store_true", help="hash the assets published on GitHub")
    src.add_argument("--check-tag", metavar="TAG", help="only check that TAG, __version__ and npm agree")
    src.add_argument("--npm-version", metavar="VERSION", help="print the npm spelling of VERSION")
    ap.add_argument("--dry-run", action="store_true", help="print the changes, write nothing")
    args = ap.parse_args(argv)

    if args.npm_version:
        try:
            print(npm_version(args.npm_version) or "")
        except ValueError as exc:
            raise SystemExit(str(exc))
        return 0
    version = args.version or read_version()
    if args.check_tag:
        return check_tag(args.check_tag, version)
    if not VERSION_RE.match(version):
        raise SystemExit("unexpected version %r" % version)
    if is_prerelease(version):
        print("%s is a pre-release: the Homebrew and Scoop manifests keep pointing at the "
              "latest stable release." % version)
        return 0

    names = asset_names(version)
    if args.dist:
        digests = {k: sha256_file(os.path.join(args.dist, n)) for k, n in names.items()}
    elif args.sums:
        sums = parse_sums(args.sums)
        missing = [n for n in names.values() if n not in sums]
        if missing:
            raise SystemExit("%s has no entry for %s" % (args.sums, ", ".join(missing)))
        digests = {k: sums[n] for k, n in names.items()}
    elif args.download:
        digests = {k: sha256_url(asset_url(version, n)) for k, n in names.items()}
    else:
        ap.error("pass --dist, --sums, --download or --check-tag")

    tar_url, zip_url = asset_url(version, names["tar"]), asset_url(version, names["zip"])
    with open(FORMULA, encoding="utf-8") as f:
        formula = update_formula(f.read(), tar_url, digests["tar"])
    with open(SCOOP, encoding="utf-8") as f:
        scoop = update_scoop(json.load(f), version, zip_url, digests["zip"])
    with open(NPM, encoding="utf-8") as f:
        npm = json.load(f)
    npm["version"] = npm_package_version(version)

    print("homebrew  %s\n          sha256 %s" % (tar_url, digests["tar"]))
    print("scoop     %s\n          hash   %s" % (zip_url, digests["zip"]))
    print("npm       version %s" % npm["version"])
    if args.dry_run:
        return 0
    with open(FORMULA, "w", encoding="utf-8", newline="\n") as f:
        f.write(formula)
    write_json(SCOOP, scoop, 4)
    write_json(NPM, npm, 2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
