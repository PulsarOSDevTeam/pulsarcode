# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Pulsar Code: one command, any intelligence, inside Claude Code."""

import os as _os
import sys as _sys

__version__ = "3.0.0"
BRAND = "Pulsar Code"
TAGLINE = "any intelligence  ·  one terminal"
REPO = "PulsarOSDevTeam/pulsarcode"

# The directory that holds the `pulsar` package (the app dir or a checkout).
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))

# `python -m pulsar` puts the current directory first on sys.path, so a repo that ships its own
# `pulsar/` (or a file shadowing the standard library) would run as the user the moment Pulsar
# starts inside it, and again on every hook, status line refresh and MCP start. Every process
# Pulsar launches goes through this bootstrap instead: it drops the current directory from
# sys.path and pins the app dir before anything else is imported. Works on Python 3.9+.
BOOT = ("import sys;r=sys.argv.pop(1);m=sys.argv.pop(1);"
        "sys.path[:]=[r]+[p for p in sys.path if p not in ('','.',r)];"
        "import runpy;sys.argv[0]=m;runpy.run_module(m,run_name='__main__',alter_sys=True)")


def python_argv(*args: str, module: str = "pulsar", python: str = "") -> list:
    """argv that runs `python -m <module> <args>` without ever importing from the current directory."""
    root = ROOT.replace("\\", "/") if _os.name == "nt" else ROOT
    # -E: a PYTHONPATH naming "" or "." (or the project) must not reintroduce what BOOT removes
    # (sitecustomize.py, stdlib shadows). -X utf8 stands in for PYTHONUTF8, which -E ignores.
    flags = ["-E", "-B"] + (["-X", "utf8"] if _os.name == "nt" else [])
    return [python or _sys.executable] + flags + ["-c", BOOT, root, module] + [str(a) for a in args]
