#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Seed a scratch PULSAR_HOME for a demo recording, and optionally start its Core.

Run it with the Python that runs Pulsar Code (it imports the `pulsar` package from this checkout)
and with PULSAR_HOME pointing at a scratch folder. It refuses to touch ~/.pulsarcode.

    seed.py home [--profiles openrouter,groq] [--model PROFILE:MODEL] [--recent PROFILE:MODEL ...]
                 [--launches N] [--catalog] [--trust DIR]
    seed.py core --out FILE [--session LABEL:CWD:PROFILE/MODEL[>PROFILE/MODEL...]] ...

`home` writes profiles with obviously fake keys (FAKE_KEYS), optional catalog caches built by
Pulsar's own parser from the demo catalog in upstream.py, and an isolated Claude Code profile
whose folder trust is already answered for --trust (the demo project only).

`core` starts this home's Pulsar Core (PULSAR_PORT picks the port) and registers sessions, each
held alive by a `sleep` process, then writes FILE (0600): Core's pid and port and, per session,
its id, token and holder pid, for the scene scripts and for teardown.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
sys.path[:] = [REPO] + [p for p in sys.path if p not in ("", ".", REPO)]
sys.path.insert(1, HERE)

from pulsar import catalog as C  # noqa: E402
from pulsar import config as K  # noqa: E402
from pulsar import providers as P  # noqa: E402

import upstream  # noqa: E402

# Obviously fake keys: shaped so local detection recognises the provider, never valid anywhere,
# and accepted only by the demo upstream (it rejects any key without "fake" in it).
FAKE_KEYS = {
    "openrouter": "sk-or-v1-fake-key-not-real",
    "groq": "gsk_fake_demo_key_not_real_FAKE",
    "nvidia": "nvapi-fake-demo-key-not-real-FAKE",
    "cerebras": "csk-fake-demo-key-not-real-FAKE",
}


def guard() -> None:
    """Refuse the account's real Pulsar home (from the user database, not $HOME, which is scratch here)."""
    home = os.environ.get("PULSAR_HOME", "")
    try:
        import pwd
        account = pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError):
        account = os.path.expanduser("~")
    real = os.path.join(account, ".pulsarcode")
    if not home or os.path.realpath(home) == os.path.realpath(real):
        sys.exit("seed.py: set PULSAR_HOME to a scratch folder (never your ~/.pulsarcode)")


def catalog_for(pid: str):
    prov = P.get(pid)
    if pid == "openrouter":
        payload = {"data": [{"id": i, "name": n, "context_length": c, "created": 1790000000 - k * 86400,
                             "pricing": {"prompt": "0" if free else "0.0000006",
                                         "completion": "0" if free else "0.0000025"},
                             "supported_parameters": ["tools", "max_tokens"] if tools else ["max_tokens"]}
                            for k, (i, n, c, free, tools) in enumerate(upstream.OPENROUTER)]}
    else:
        host = {"groq": "api.groq.com", "nvidia": "integrate.api.nvidia.com", "cerebras": "api.cerebras.ai"}[pid]
        payload = {"data": [{"id": i} for i in upstream.SMALL[host]]}
    return C.parse_models(prov, pid, payload)


def cmd_home(ns) -> int:
    K.ensure_home()
    state = K.load()
    for pid in [p for p in ns.profiles.split(",") if p]:
        state.upsert(K.Profile(id=pid, provider=pid), activate=not state.active)
        K.set_key(pid, FAKE_KEYS[pid])
        if ns.catalog:
            C.write_cache(pid, catalog_for(pid))
    for spec in reversed(ns.recent or []):
        pid, model = spec.split(":", 1)
        state.push_recent(pid, model)
    if ns.model:
        pid, model = ns.model.split(":", 1)
        models = catalog_for(pid)
        m = next((x for x in models if x.id == model), None)
        state.set_model(pid, model, (m.context if m else 0) or C.guess_context(model))
    state.launches = ns.launches
    K.save(state)
    if ns.trust:
        cfg = K.home() / "claude_config"
        cfg.mkdir(parents=True, exist_ok=True)
        proj = os.path.realpath(ns.trust)
        data = {"hasCompletedOnboarding": True, "theme": "dark", "numStartups": 3,
                "projects": {proj: {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True,
                                    "allowedTools": []}}}
        K.write_private(cfg / ".claude.json", json.dumps(data, indent=2) + "\n")
    return 0


def cmd_core(ns) -> int:
    from pulsar.core import spawn
    from pulsar.core import state as ST
    K.ensure_home()
    info = spawn.ensure_core()
    out = {"core_pid": info.pid, "port": info.port, "url": info.url, "sessions": []}
    for spec in ns.session or []:
        label, cwd, chain = spec.split(":", 2)
        plan = []
        for hop in chain.split(">"):
            pid, model = hop.split("/", 1)
            plan.append(ST.Target(pid, pid, model, context=C.guess_context(model)))
        holder = subprocess.Popen(["sleep", "3600"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, start_new_session=True)
        flags = {"relay": True, "loopguard": True, "stage": True, "council": True, "pilot": False}
        sess, token = ST.new_session(plan, flags, label=label, cwd=cwd)
        sess.pid = holder.pid
        ST.save_session(sess)
        out["sessions"].append({"sid": sess.sid, "token": token, "holder": holder.pid, "label": label})
    K.write_private(Path(ns.out), json.dumps(out, indent=1) + "\n")
    return 0


def main(argv) -> int:
    guard()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("home")
    h.add_argument("--profiles", default="openrouter")
    h.add_argument("--model", default="")
    h.add_argument("--recent", action="append")
    h.add_argument("--launches", type=int, default=0)
    h.add_argument("--catalog", action="store_true")
    h.add_argument("--trust", default="")
    c = sub.add_parser("core")
    c.add_argument("--out", required=True)
    c.add_argument("--session", action="append")
    ns = ap.parse_args(argv)
    return cmd_home(ns) if ns.cmd == "home" else cmd_core(ns)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
