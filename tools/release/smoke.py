#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Smoke-test an installed `pulsarcode` command from a hostile working directory.

    python tools/release/smoke.py [--status] [--expect-version X.Y.Z] <launcher> [launcher args...]

<launcher> is whatever starts Pulsar Code: the installer's symlink, a wheel's console
script, a Homebrew bin, `pulsarcode.cmd` on Windows, `node packaging/npm/bin/pulsarcode.js`,
or `npx --yes --package ./pulsarcode-X.Y.Z.tgz pulsarcode` (an `npm pack` tarball). Launcher
arguments that name an existing file relative to the current directory (a script, a
tarball) are made absolute before the run moves into the hostile folder.

The working directory is a throwaway folder that plants a fake `pulsar/` package and files
shadowing modules Pulsar imports (json, runpy, httpx, fastapi, ...), plus a
`node_modules/.bin/` full of commands Pulsar or its launchers might look up on PATH (bash,
python3, claude, curl, ...): npm and npx put `<cwd>/node_modules/.bin` first on PATH, so
the npm wrapper must drop those entries before it starts anything. Each planted file prints
PWNED and appends to a marker file outside that folder. Pulsar must never import or run code
from the current directory (see pulsar.BOOT), so the run fails if a marker appears, PWNED is
printed, a command fails, or `version` reports the wrong version. The marker also catches
background processes (Pulsar Core, the MCP server) whose output is not visible here.

`node` and `sh` are not planted: npm runs a package's bin through `sh -c`, and the bin's
`#!/usr/bin/env node`, resolving both through that same PATH before any package code runs,
and a project `.npmrc` can likewise set NODE_OPTIONS or another registry. No package can
prevent that, which is why the docs say not to run `npx pulsarcode` inside a repository you
do not trust (install it once with `npm install -g pulsarcode` instead).

`version` also runs once with PYTHONPATH holding an empty element and the hostile folder (what
`export PYTHONPATH=$PYTHONPATH:/x` leaves behind when it was unset): every launcher that starts
Python with -E ignores it. pip's console scripts (pip, pipx, uv tool) honour PYTHONPATH by design,
so wheel_smoke.py passes --no-pythonpath.

Runs on Python 3.9+ on Linux, macOS and Windows. Uses a scratch PULSAR_HOME unless one is set.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))

# Module names Pulsar (or Python itself at startup) might import by name.
SHADOWED = ("json", "runpy", "httpx", "fastapi", "uvicorn", "asyncio", "subprocess", "typing",
            "sitecustomize", "usercustomize", "secrets", "hashlib", "signal", "socket")

PAYLOAD = (
    "import sys as _s\n"
    "try:\n"
    "    with open({mark!r}, 'a') as _f:\n"
    "        _f.write({name!r} + '\\n')\n"
    "except Exception:\n"
    "    pass\n"
    "_s.stdout.write('PWNED by hostile ' + {name!r} + '\\n')\n"
    "_s.stdout.flush()\n"
    "raise SystemExit(66)\n"
)

# Commands planted in <cwd>/node_modules/.bin (see the module docstring for why not `node`
# or `sh`).
PLANTED_BIN = ("bash", "env", "dirname", "readlink", "uname", "cat", "python", "python3",
               "claude", "curl", "wget", "uv", "git", "npm", "npx", "powershell", "pwsh")

SH_PAYLOAD = (
    "#!/bin/sh\n"
    "echo \"node_modules/.bin/{name}\" >> '{mark}'\n"
    "echo 'PWNED by hostile node_modules/.bin/{name}'\n"
    "echo 'PWNED by hostile node_modules/.bin/{name}' >&2\n"
    "exit 66\n"
)
CMD_PAYLOAD = (
    "@echo off\r\n"
    ">>\"{mark}\" echo node_modules/.bin/{name}\r\n"
    "echo PWNED by hostile node_modules/.bin/{name}\r\n"
    "exit /b 66\r\n"
)

# Launcher arguments with these endings are treated as file paths even without a separator.
PATH_SUFFIXES = (".js", ".cjs", ".mjs", ".py", ".pyz", ".cmd", ".bat", ".ps1", ".sh", ".tgz", ".whl")


def repo_version() -> str:
    try:
        with open(os.path.join(REPO, "pulsar", "__init__.py"), encoding="utf-8") as f:
            m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', f.read(), re.M)
        return m.group(1) if m else ""
    except OSError:
        return ""


def plant(cwd: str, mark: str) -> None:
    pkg = os.path.join(cwd, "pulsar")
    os.makedirs(pkg)
    for sub in ("__init__", "__main__", "cli", "core", "mcp", "statusline", "notify"):
        with open(os.path.join(pkg, sub + ".py"), "w", encoding="utf-8") as f:
            f.write(PAYLOAD.format(mark=mark, name="pulsar." + sub))
    for name in SHADOWED:
        with open(os.path.join(cwd, name + ".py"), "w", encoding="utf-8") as f:
            f.write(PAYLOAD.format(mark=mark, name=name))
    # A pyvenv.cfg / .pth in the cwd must not matter either.
    with open(os.path.join(cwd, "evil.pth"), "w", encoding="utf-8") as f:
        f.write("import os; open(%r, 'a').write('pth\\n')\n" % mark)
    # What npm/npx prepend to PATH: <cwd>/node_modules/.bin.
    nbin = os.path.join(cwd, "node_modules", ".bin")
    os.makedirs(nbin)
    for name in PLANTED_BIN:
        if os.name == "nt":
            with open(os.path.join(nbin, name + ".cmd"), "w", encoding="ascii", newline="") as f:
                f.write(CMD_PAYLOAD.format(name=name, mark=mark))
        else:
            path = os.path.join(nbin, name)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(SH_PAYLOAD.format(name=name, mark=mark.replace("'", "'\\''")))
            os.chmod(path, 0o755)


def _pathlike(arg: str) -> bool:
    return ("/" in arg or os.sep in arg or arg.lower().endswith(PATH_SUFFIXES)) and os.path.isfile(arg)


def absolutize(args):
    """Make launcher arguments that name an existing file (relative to the caller) absolute.

    The launcher runs from the hostile folder, so `node packaging/npm/bin/pulsarcode.js` would
    otherwise resolve the script inside it. Options are left alone, except `--opt=<file>`.
    """
    out = []
    for arg in args:
        if not os.path.isabs(arg):
            if arg.startswith("--") and "=" in arg:
                opt, _, value = arg.partition("=")
                if value and not os.path.isabs(value) and _pathlike(value):
                    arg = opt + "=" + os.path.abspath(value)
            elif not arg.startswith("-") and _pathlike(arg):
                arg = os.path.abspath(arg)
        out.append(arg)
    return out


def run(argv, cwd, env, timeout):
    try:
        p = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"").decode("utf-8", "replace") + (exc.stderr or b"").decode("utf-8", "replace")
        return 124, out
    return p.returncode, p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--status", action="store_true", help="also run `status` (PULSAR_OFFLINE=1)")
    ap.add_argument("--expect-version", default=None, help="default: __version__ from this checkout")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--no-pythonpath", action="store_true",
                    help="skip the run with PYTHONPATH naming the current folder (pip's console scripts honour it)")
    ap.add_argument("launcher", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)
    launcher = list(args.launcher)
    if launcher and launcher[0] == "--":
        launcher = launcher[1:]
    if not launcher:
        ap.error("missing launcher command")
    exe = launcher[0]
    resolved = shutil.which(exe) or (os.path.abspath(exe) if os.path.exists(exe) else "")
    if not resolved:
        print("smoke: launcher not found: %s" % exe, file=sys.stderr)
        return 2
    launcher = [resolved] + absolutize(launcher[1:])
    expect = repo_version() if args.expect_version is None else args.expect_version

    scratch = tempfile.mkdtemp(prefix="pulsar-smoke-")
    mark = os.path.join(scratch, "PWNED.marker")
    cwd = os.path.join(scratch, "hostile repo")  # a space in the path on purpose
    os.makedirs(cwd)
    plant(cwd, mark)

    env = dict(os.environ)
    for var in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONSAFEPATH"):
        env.pop(var, None)  # the check must not depend on interpreter flags that hide the bug
    env.setdefault("PULSAR_HOME", os.path.join(scratch, "pulsarhome"))
    env.update({"PULSAR_OFFLINE": "1", "PULSAR_FX": "off", "NO_COLOR": "1", "PULSAR_NO_ANIM": "1",
                "PYTHONIOENCODING": "utf-8"})

    commands = [["version"], ["--version"], ["providers"], ["help"]]
    if args.status:
        commands.append(["status"])
    runs = [(sub, env, " ".join(sub)) for sub in commands]
    if not args.no_pythonpath:
        # An empty element (the current folder) and the folder itself: ignored by every launcher that
        # starts Python with -E, run first (sitecustomize.py) by one that does not.
        hostile = dict(env, PYTHONPATH=os.pathsep.join(["", cwd]))
        runs.append((["version"], hostile, "version (PYTHONPATH=%s)" % hostile["PYTHONPATH"]))
    failures = []
    for sub, run_env, shown in runs:
        rc, out = run(launcher + sub, cwd, run_env, args.timeout)
        tail = "\n".join(out.strip().splitlines()[-6:])
        if "PWNED" in out:
            failures.append("%s: imported code from the current directory:\n%s" % (shown, tail))
        elif rc != 0 and sub != ["status"]:
            failures.append("%s: exit %d\n%s" % (shown, rc, tail))
        elif sub == ["version"] and expect and expect not in out:
            failures.append("version: expected %s, got: %s" % (expect, out.strip()[:200]))
        else:
            print("smoke: ok    pulsarcode %s (exit %d)" % (shown, rc))
            if sub == ["status"] and rc != 0:
                print("smoke: note  status exited %d (expected without a key or Claude Code)" % rc)

    # Give anything spawned in the background (Core, MCP) a moment to hit a planted module.
    time.sleep(2.0 if args.status else 0.3)
    run(launcher + ["stop"], cwd, env, 60)
    if os.path.exists(mark):
        with open(mark, encoding="utf-8", errors="replace") as f:
            hits = sorted(set(f.read().split()))
        failures.append("hostile modules executed: %s" % ", ".join(hits))

    shutil.rmtree(scratch, ignore_errors=True)
    if failures:
        print("smoke: FAILED", file=sys.stderr)
        for f in failures:
            print("  - " + f.replace("\n", "\n    "), file=sys.stderr)
        return 1
    print("smoke: passed; nothing from the working directory ran (%d commands)" % len(runs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
