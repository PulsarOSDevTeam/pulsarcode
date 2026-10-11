# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""The launcher's bootstrap with the hardware probe pinned (see pinned_python).

    pinned_boot.py APP_DIR MODULE ARGS...

Same sys.path handling as the ./pulsarcode bootstrap, then pulsar.hardware's probes return a
fixed, plainly illustrative machine ("Demo machine · 8 cores · 16 GB · no GPU").
"""

import runpy
import sys

app = sys.argv.pop(1)
module = sys.argv.pop(1)
sys.path[:] = [app] + [p for p in sys.path if p not in ("", ".", app)]

from pulsar import hardware  # noqa: E402

DEMO_HW = {"os": "linux", "arch": "x86_64", "cpu": "Demo machine", "chip": "", "cores": 8, "physical_cores": 8,
           "ram_gb": 16.0, "gpus": [], "env": {}, "accel": "none", "partial": False, "gpu_cache": True,
           "probe_ms": 1.0}


def _pinned(*_a, **_k):
    out = {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v) for k, v in DEMO_HW.items()}
    out["terminal"] = hardware._terminal() if hasattr(hardware, "_terminal") else {}
    return out


hardware.probe = _pinned
hardware.basics = _pinned
hardware.peek = lambda: {"os": "linux", "cores": 8, "env": {"ssh": False, "termux": False}}

sys.argv[0] = module
runpy.run_module(module, run_name="__main__", alter_sys=True)
