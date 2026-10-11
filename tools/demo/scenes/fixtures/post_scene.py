#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Post a Stage scene to the demo Core as one of its sessions (what the pulsar MCP server does).

    post_scene.py STATE.json NAME [SESSION_INDEX]

STATE.json is written by `seed.py core`; NAME is a key of stage_scenes.json. Prints nothing (it
runs in the background of a recorded terminal); the exit status says whether Core took it.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def post(state: dict, scene: dict, index: int = 0) -> bool:
    sess = state["sessions"][index]
    req = urllib.request.Request(state["url"] + "/pulsar/stage", data=json.dumps({"scene": scene}).encode(),
                                 headers={"Authorization": "Bearer " + sess["token"],
                                          "Content-Type": "application/json"}, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))      # loopback: never a proxy
    try:
        with opener.open(req, timeout=10) as resp:
            return resp.status == 200
    except OSError:
        return False


def main(argv) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    with open(argv[0]) as f:
        state = json.load(f)
    with open(os.path.join(HERE, "stage_scenes.json")) as f:
        scenes = json.load(f)
    return 0 if post(state, scenes[argv[1]], int(argv[2]) if len(argv) > 2 else 0) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
