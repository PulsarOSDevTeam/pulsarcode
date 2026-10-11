# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True, scope="session")
def session_home(tmp_path_factory):
    # A thread that outlives its test (a GPU probe finishing in the background) reads PULSAR_HOME after that
    # test restored it. Between tests it names this session folder, never the default home of whoever runs.
    before = os.environ.get("PULSAR_HOME")
    os.environ["PULSAR_HOME"] = str(tmp_path_factory.mktemp("session-home"))
    yield
    if before is None:
        os.environ.pop("PULSAR_HOME", None)
    else:
        os.environ["PULSAR_HOME"] = before


@pytest.fixture(autouse=True)
def pulsar_home(tmp_path, monkeypatch):
    home = tmp_path / "pulsarhome"
    monkeypatch.setenv("PULSAR_HOME", str(home))
    monkeypatch.setenv("PULSAR_OFFLINE", "1")
    monkeypatch.setenv("PULSAR_FX", "off")
    for var in list(os.environ):
        if var.endswith("_API_KEY") or var in ("HF_TOKEN", "NVIDIA_NIM_KEY"):
            monkeypatch.delenv(var, raising=False)
    return home
