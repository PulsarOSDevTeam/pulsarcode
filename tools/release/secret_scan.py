#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Fail when something that looks like a real credential is committed.

    python tools/release/secret_scan.py [paths...]     (default: the whole repository)

Looks for provider key shapes (Anthropic, OpenAI, OpenRouter, NVIDIA, Gemini, Groq, xAI,
Hugging Face, Cerebras, Together, Fireworks, DeepSeek/Moonshot-style sk-...), GitHub, npm,
PyPI, AWS and Slack tokens, and private key blocks. The patterns require full-length key
bodies, so documentation shapes such as `nvapi-...` or `sk-or-...` never match.

It also catches leaks that are not credentials: the home folder of a real user (/Users/<name>,
/home/<name>, C:\\Users\\<name>; placeholder names such as `you`, `dev` or `runner` pass), a
private network address (10/8, 172.16/12, 192.168/16) outside tests (CIDR ranges pass; examples
use the documentation range 192.0.2.0/24), and files that hold secrets by their nature (.env,
*.pem, *.key, SSH private keys, .netrc, .pypirc and the like; .env.example passes).

Every file git tracks (plus untracked files that are not ignored) is scanned, tests and
fixtures included: tests are exactly where a key pasted while debugging a provider ends up.
Outside a git checkout the folder is walked instead (see repofiles.py). Text is decoded as
UTF-8, or UTF-16/UTF-32 when the file has a byte order mark or looks like BOM-less UTF-16.

A deliberate fake is accepted in two ways: the marker `secret-scan: allow` on the same line,
or an entry in secret_scan_allow.txt (path, kind and a fingerprint of that exact value, for a
line that does not carry the marker; for a secret-file finding the value is the file's path).
Findings are printed redacted (prefix, length and fingerprint), never in full.
Exit status 1 when anything is found, 2 when a file could not be read.
"""

import hashlib
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

import repofiles  # noqa: E402

PATTERNS = [
    ("anthropic", r"sk-ant-(?:api|admin|oat|sid)[0-9]{2}-[A-Za-z0-9_-]{32,}"),
    ("openrouter", r"sk-or-v1-[0-9a-f]{40,}"),
    ("openai", r"sk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{32,}"),
    ("sk-generic", r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9]{32,}(?![A-Za-z0-9_-])"),
    ("nvidia", r"nvapi-[A-Za-z0-9_-]{40,}"),
    ("google", r"AIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])"),
    ("groq", r"gsk_[A-Za-z0-9]{40,}"),
    ("xai", r"xai-[A-Za-z0-9]{40,}"),
    ("huggingface", r"hf_[A-Za-z0-9]{30,}"),
    ("cerebras", r"csk-[a-z0-9]{40,}"),
    ("together", r"tgp_v1_[A-Za-z0-9_-]{30,}"),
    ("fireworks", r"fw_[A-Za-z0-9]{24,}"),
    ("github", r"(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})"),
    ("npm", r"npm_[A-Za-z0-9]{36}(?![A-Za-z0-9])"),
    ("pypi", r"pypi-AgE[A-Za-z0-9_-]{50,}"),
    ("aws", r"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])"),
    ("slack", r"xox[baprs]-[A-Za-z0-9-]{20,}"),
    ("private-key", r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----"),
]
COMPILED = [(name, re.compile(rx)) for name, rx in PATTERNS]

# Generic leaks that are not credentials. Placeholder user names are what examples, fixtures, CI
# runners and system folders use; a name of one letter or starting with a dot is never a person's.
PLACEHOLDER_USERS = frozenset((
    "u", "user", "username", "your-name", "yourname", "name", "me", "you", "someone", "dev", "developer",
    "example", "alice", "bob", "jane", "john", "jdoe", "admin", "test", "runner", "ubuntu", "vagrant",
    "ec2-user", "pi", "linuxbrew", "shared", "public", "default"))
HOME_PATHS = [
    re.compile(r"(?<![\w.$}-])/(?:Users|home)/([A-Za-z0-9._-]+)"),
    re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:(?:\\\\|\\|/)+Users(?:\\\\|\\|/)+([A-Za-z0-9._-]+)", re.I),
]
PRIVATE_ADDRESS = re.compile(r"(?<![0-9.])(?:10(?:\.[0-9]{1,3}){3}|192\.168(?:\.[0-9]{1,3}){2}|"
                             r"172\.(?:1[6-9]|2[0-9]|3[01])(?:\.[0-9]{1,3}){2})(?![0-9.]|/[0-9])")
SECRET_FILE = re.compile(r"(?i)^(?:\.env(?:\.(?!example$|sample$|template$|dist$)[^/]+)?|.+\.(?:pem|key|p12|pfx|jks"
                         r"|keystore)|id_(?:rsa|dsa|ecdsa|ed25519)|\.netrc|\.pypirc|\.git-credentials|authorized_keys)$")
LABELS = {
    "home-path": "a home folder path (a real user name?)",
    "private-address": "a private network address",
    "secret-file": "a file that usually holds secrets",
}

ALLOW = "secret-scan: allow"
ALLOW_FILE = os.path.join(HERE, "secret_scan_allow.txt")
MAX_BYTES = 5 * 1024 * 1024


def fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def redact(token: str) -> str:
    return "%s... (%d chars, fp %s)" % (token[:6], len(token), fingerprint(token))


def load_allowlist(path: str = ALLOW_FILE) -> set:
    """{(path, kind, fingerprint)} from secret_scan_allow.txt (comments and blanks ignored)."""
    out = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line:
                    parts = line.split()
                    if len(parts) == 3:
                        out.add(tuple(parts))
    except OSError:
        pass
    return out


def is_test_path(rel: str) -> bool:
    return any(part in ("test", "tests") for part in rel.split("/")[:-1])


def leaks(line: str, in_tests: bool = False):
    """(kind, matched text) for every generic leak on one line."""
    for rx in HOME_PATHS:
        for m in rx.finditer(line):
            user = m.group(1)
            if not (len(user) == 1 or user.startswith(".") or user.lower() in PLACEHOLDER_USERS):
                yield "home-path", m.group(0)
    if not in_tests:
        for m in PRIVATE_ADDRESS.finditer(line):
            yield "private-address", m.group(0)


def scan_text(text: str, rel: str, allow=frozenset()) -> list:
    hits = []
    in_tests = is_test_path(rel)
    for lineno, line in enumerate(text.splitlines(), 1):
        if ALLOW in line:
            continue
        found = [(name, m.group(0)) for name, rx in COMPILED for m in rx.finditer(line)]
        for name, token in found + list(leaks(line, in_tests)):
            if (rel, name, fingerprint(token)) in allow:
                continue
            hits.append((rel, lineno, name, redact(token)))
    return hits


def scan_file(path: str, rel: str, allow=frozenset()) -> list:
    text, _why = repofiles.read_text(path, MAX_BYTES)
    return scan_text(text, rel, allow) if text is not None else []


def scan(paths=None, root: str = REPO, allow=None, errors=None, git=None) -> list:
    """Findings as (relpath, line, kind, redacted). Unreadable files go to `errors` if given."""
    allow = load_allowlist() if allow is None else allow
    targets = []
    if paths:
        for top in paths:
            if os.path.isdir(top):
                targets += [os.path.join(top, rel) for rel in repofiles.list_files(top, git=git)]
            else:
                targets.append(top)
    else:
        targets = [os.path.join(root, rel) for rel in repofiles.list_files(root, git=git)]
    findings = []
    for path in targets:
        if not os.path.isfile(path):
            continue  # deleted in the worktree, a submodule, a dangling link
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        if SECRET_FILE.match(os.path.basename(rel)) and (rel, "secret-file", fingerprint(rel)) not in allow:
            findings.append((rel, 0, "secret-file", rel))
        try:
            findings.extend(scan_file(path, rel, allow))
        except OSError as exc:
            if errors is None:
                raise
            errors.append((rel, str(exc)))
    return findings


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    errors = []
    findings = scan(argv or None, errors=errors)
    for rel, lineno, name, shown in findings:
        if name == "secret-file":
            print("%s: %s" % (rel, LABELS[name]))
        elif name in LABELS:
            print("%s:%d: %s: %s" % (rel, lineno, LABELS[name], shown))
        else:
            print("%s:%d: possible %s credential: %s" % (rel, lineno, name, shown))
    for rel, why in errors:
        print("%s: could not read: %s" % (rel, why), file=sys.stderr)
    if findings:
        print("secret scan: %d finding(s). Remove it (and rotate a credential), use a placeholder (a name "
              "such as `you`, an address from 192.0.2.0/24), or mark a deliberate case with '%s' on the "
              "same line or in secret_scan_allow.txt." % (len(findings), ALLOW), file=sys.stderr)
        return 1
    if errors:
        return 2
    print("secret scan clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
