# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""orbit: a made-up project for the demo session recording (tools/demo/make_all.sh). Never run."""

import argparse


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="orbit")
    sub = ap.add_subparsers(dest="cmd", required=True)
    nxt = sub.add_parser("next", help="the next visible pass")
    nxt.add_argument("--city", default="Montreal")
    sub.add_parser("watch", help="live positions").add_argument("satellite")
    ap.parse_args(argv)
    return 0
