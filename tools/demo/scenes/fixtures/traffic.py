#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Synthetic Claude Code traffic into the demo Core, for the Observatory recording.

    traffic.py STATE.json SECONDS [STAGE_AT]

Streams Anthropic Messages requests into Core as each session of STATE.json (written by
`seed.py core`), the way Claude Code does, against the demo upstream in traffic mode: variable
latency, occasional rate limits (relays), an unhealthy first hop for the first session (relays),
and a max_tokens limit on the second session's provider that Core learns once (a heal). Midway it
posts a Stage scene (at STAGE_AT seconds, default half way, at most 14 s), so the feed shows every
kind of event. Stdlib only; prints one line per error.
"""

from __future__ import annotations

import http.client
import json
import os
import random
import sys
import threading
import time
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import post_scene  # noqa: E402

PROMPTS = ("refactor the pass search into a generator", "why does test_predict flake on CI?",
           "add --min-elevation to orbit next", "write a migration note for the rate limiter",
           "explain the token bucket in limiter.py", "summarize the open TODOs")


def request(port: int, token: str, max_tokens: int, rng: random.Random) -> None:
    body = json.dumps({"model": "claude-sonnet-4-5", "max_tokens": max_tokens, "stream": True,
                       "messages": [{"role": "user", "content": rng.choice(PROMPTS)}]})
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        conn.request("POST", "/v1/messages", body=body, headers={
            "Authorization": "Bearer " + token, "Content-Type": "application/json",
            "anthropic-version": "2023-06-01"})
        resp = conn.getresponse()
        while resp.read(4096):
            pass
    finally:
        conn.close()


def worker(port: int, sess: dict, stop: float, gap, max_tokens: int, seed: int) -> None:
    rng = random.Random(seed)
    time.sleep(rng.uniform(0, 1.5))
    while time.time() < stop:
        try:
            request(port, sess["token"], max_tokens, rng)
        except (OSError, http.client.HTTPException) as exc:
            print("request failed: %s" % type(exc).__name__, flush=True)
        time.sleep(rng.uniform(*gap))


def main(argv) -> int:
    if len(argv) not in (2, 3):
        print(__doc__, file=sys.stderr)
        return 2
    with open(argv[0]) as f:
        state = json.load(f)
    port = urlsplit(state["url"]).port
    stop = time.time() + float(argv[1])
    sessions = state["sessions"]
    plan = [(0, (0.4, 2.0), 8192), (0, (0.8, 3.0), 8192), (1, (0.6, 2.5), 32000), (2, (2.0, 5.0), 8192)]
    threads = []
    for n, (i, gap, max_tokens) in enumerate(plan):
        if i < len(sessions):
            th = threading.Thread(target=worker, args=(port, sessions[i], stop, gap, max_tokens, 100 + n),
                                  daemon=True)
            th.start()
            threads.append(th)
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "stage_scenes.json")) as f:
        scenes = json.load(f)
    time.sleep(float(argv[2]) if len(argv) > 2 else min(14.0, float(argv[1]) / 2))
    if len(sessions) > 1 and not post_scene.post(state, scenes["progress"], 1):
        print("stage post failed", flush=True)
    for th in threads:
        th.join()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
