#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Build the wheel, install it into a fresh virtualenv, and run the hostile-directory smoke test.

    python tools/release/wheel_smoke.py [--wheel path/to.whl] [--keep]

Without --wheel it runs `python -m build --wheel` (pip install build) into a temporary folder.
The fresh venv installs the wheel and its dependencies from PyPI, then tools/release/smoke.py
runs the venv's `pulsarcode` console script (version, providers, help, status) from a folder
full of booby-trapped modules. Works on Linux, macOS and Windows with Python 3.9+.
"""

import argparse
import glob
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

import smoke  # noqa: E402


def venv_paths(venv: str):
    if os.name == "nt":
        return os.path.join(venv, "Scripts", "python.exe"), os.path.join(venv, "Scripts", "pulsarcode.exe")
    return os.path.join(venv, "bin", "python"), os.path.join(venv, "bin", "pulsarcode")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--wheel", help="an existing wheel (default: build one from this checkout)")
    ap.add_argument("--keep", action="store_true", help="keep the temporary folder")
    args = ap.parse_args(argv)

    work = tempfile.mkdtemp(prefix="pulsar-wheel-")
    try:
        wheel = args.wheel
        if not wheel:
            subprocess.check_call([sys.executable, "-m", "build", "--wheel", "--outdir",
                                   os.path.join(work, "dist"), REPO])
            found = glob.glob(os.path.join(work, "dist", "pulsarcode-*.whl"))
            if len(found) != 1:
                print("wheel_smoke: expected one wheel, found %r" % found, file=sys.stderr)
                return 1
            wheel = found[0]
        venv = os.path.join(work, "venv")
        subprocess.check_call([sys.executable, "-m", "venv", venv])
        py, script = venv_paths(venv)
        subprocess.check_call([py, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", wheel])
        if not os.path.exists(script):
            print("wheel_smoke: console script missing: %s" % script, file=sys.stderr)
            return 1
        print("wheel_smoke: installed %s" % os.path.basename(wheel))
        # pip's console script honours PYTHONPATH by design (it cannot pass -E), so the PYTHONPATH run is
        # left to the launchers that do: the installers' shims, Homebrew, Scoop and npm.
        return smoke.main(["--status", "--no-pythonpath", script])
    finally:
        if args.keep:
            print("wheel_smoke: kept %s" % work)
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
