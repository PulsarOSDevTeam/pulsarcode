# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Pilot safety rails.

  off by default   enabled only by `pulsarcode pilot on` (config prefs) or
                   PULSAR_PILOT=1. `pulsarcode pilot off` also stops sessions
                   that are already running (checked before every action).
  failsafe         a human slamming the mouse into the top-left corner of
                   the main display stops Pilot for the rest of the session.
  rate limit       a token bucket (10 actions/s, burst 10) per session.
  audit log        ~/.pulsarcode/logs/pilot.log (0600): action, coordinates,
                   text LENGTH and log-safe key names. Never typed text,
                   never image data.
"""

from __future__ import annotations

import math
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Tuple

from pulsar.pilot.backends import FAILSAFE_ZONE, PilotError, in_failsafe

PROCESS_START = time.time()
RATE = 10.0
BURST = 10.0
LOG_MAX = 1 << 20

FAILSAFE_MSG = ("Pilot stopped: the user moved the mouse into the top-left corner (the failsafe). "
                "The user has taken control. Do not retry Pilot actions in this session; ask the user how to "
                "continue. They can restart the session to re-arm Pilot.")
OFF_MSG = ("Pilot (computer use) is off. The user can enable it with `pulsarcode pilot on`; until then do not "
           "try to control the computer.")


class SafetyError(PilotError):
    pass


def _prefs() -> Dict[str, object]:
    try:
        from pulsar import config as K
        return dict(K.load().prefs)
    except Exception:
        return {}


ARMED_AT = "PULSAR_PILOT_ARMED_AT"     # when the launcher armed this session (a Unix time)


def armed_at(env: Mapping[str, str]) -> Optional[float]:
    try:
        t = float((env.get(ARMED_AT) or "").strip())
    except ValueError:
        return None
    return t if math.isfinite(t) and t > 0 else None


def state(env: Optional[Mapping[str, str]] = None, prefs: Optional[Dict[str, object]] = None,
          started: float = PROCESS_START) -> Tuple[bool, str]:
    """(enabled, why) for this process.

    `pulsarcode pilot off` wins over anything armed before it: before this session was armed (ARMED_AT,
    which the launcher sets and pins) or, without that, before this process started. So an MCP server
    that Claude Code restarts later in the same session stays off.
    """
    env = os.environ if env is None else env
    flag = env.get("PULSAR_PILOT", "")
    if flag == "0":
        return False, "PULSAR_PILOT=0"
    prefs = _prefs() if prefs is None else prefs
    armed = armed_at(env)
    if armed is not None:
        started = min(started, armed)
    try:
        off_at = float(prefs.get("pilot_off_at") or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        off_at = 0.0
    if off_at and off_at >= started:
        return False, "turned off with `pulsarcode pilot off`"
    if prefs.get("pilot") is True:
        return True, "on (pulsarcode pilot on)"
    if flag == "1":
        return True, "on (PULSAR_PILOT=1)"
    return False, "off (enable with `pulsarcode pilot on`)"


def enabled() -> bool:
    return state()[0]


def set_enabled(on: bool) -> None:
    from pulsar import config as K
    st = K.load()
    st.prefs["pilot"] = bool(on)
    if on:
        st.prefs.pop("pilot_off_at", None)
    else:
        st.prefs["pilot_off_at"] = time.time()
    K.save(st)


def session_env() -> Dict[str, str]:
    """What a launcher should export so the MCP server registers the pilot tools."""
    return {"PULSAR_PILOT": "1"} if enabled() else {}


def log_path() -> Path:
    from pulsar import config as K
    return K.home() / "logs" / "pilot.log"


class Guard:
    """Per-session safety state (one MCP server process == one session)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, rate: float = RATE, burst: float = BURST,
                 state_fn: Callable[[], Tuple[bool, str]] = state) -> None:
        self.clock = clock
        self.rate = rate
        self.burst = burst
        self.tokens = burst
        self.stamp = clock()
        self.tripped = False
        self.state_fn = state_fn
        self.lock = threading.Lock()
        self._cache: Tuple[float, Tuple[bool, str]] = (-1e9, (False, ""))

    # -- gates -------------------------------------------------------------------------
    def require_enabled(self) -> None:
        if self.tripped:
            raise SafetyError(FAILSAFE_MSG)
        now = self.clock()
        if now - self._cache[0] > 0.5:
            self._cache = (now, self.state_fn())
        ok, why = self._cache[1]
        if not ok:
            raise SafetyError(OFF_MSG + " (" + why + ")")

    def failsafe(self, pos: Tuple[int, int], origin: Tuple[int, int] = (0, 0)) -> None:
        """Abort and disarm when the REAL pointer sits in the kill-switch corner."""
        if self.tripped:
            raise SafetyError(FAILSAFE_MSG)
        if in_failsafe(pos[0], pos[1], origin):
            self.tripped = True
            audit("failsafe", x=pos[0], y=pos[1], zone=FAILSAFE_ZONE)
            raise SafetyError(FAILSAFE_MSG)

    def throttle(self, max_wait: float = 1.0, sleep: Callable[[float], None] = time.sleep) -> None:
        with self.lock:
            now = self.clock()
            self.tokens = min(self.burst, self.tokens + (now - self.stamp) * self.rate)
            self.stamp = now
            if self.tokens >= 1:
                self.tokens -= 1
                return
            wait = (1 - self.tokens) / self.rate
            if wait > max_wait:
                raise SafetyError("Pilot rate limit: at most %d actions per second." % int(self.rate))
            sleep(wait)
            self.tokens = 0.0
            self.stamp = self.clock()


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

_LOG_LOCK = threading.Lock()
_ALLOWED = ("x", "y", "x1", "y1", "x2", "y2", "dx", "dy", "button", "count", "chars", "keys", "w", "h", "iw", "ih",
            "zone", "ok", "error", "label_chars", "region")
# The first audit failure of this process ("" while the log works) and whether the model was told.
_AUDIT_PROBLEM = {"error": "", "told": False}


def _fmt(v: object) -> str:
    s = str(v)
    s = "".join(ch if ch.isprintable() and ch not in " =\n\r" else "_" for ch in s)
    return s[:120]


def audit(action: str, **fields: object) -> None:
    """Append one line. Unknown fields are dropped so nothing sensitive slips in by accident."""
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        parts = [time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "pid=%d" % os.getpid(), "action=" + _fmt(action)]
        for k in _ALLOWED:
            if k in fields and fields[k] is not None:
                parts.append("%s=%s" % (k, _fmt(fields[k])))
        line = " ".join(parts) + "\n"
        with _LOG_LOCK:
            try:
                if path.stat().st_size > LOG_MAX:
                    os.replace(str(path), str(path) + ".1")
            except OSError:
                pass
            fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                try:
                    os.fchmod(fd, 0o600)
                except (AttributeError, OSError):
                    pass
                os.write(fd, line.encode("utf-8", "replace"))
            finally:
                os.close(fd)
    except Exception as exc:
        # Auditing must never break an action, but a missing trail is said once (stderr, then the model).
        if not _AUDIT_PROBLEM["error"]:
            _AUDIT_PROBLEM["error"] = type(exc).__name__
            try:
                sys.stderr.write("pulsar pilot: the audit log could not be written (%s); actions are not being "
                                 "recorded.\n" % type(exc).__name__)
                sys.stderr.flush()
            except (OSError, ValueError):
                pass


def audit_notice() -> str:
    """Once per process, after the audit log failed: a note for the model to pass on. Else ""."""
    if not _AUDIT_PROBLEM["error"] or _AUDIT_PROBLEM["told"]:
        return ""
    _AUDIT_PROBLEM["told"] = True
    return ("Note: Pilot's audit log could not be written (%s), so this session's actions are not being recorded. "
            "Tell the user." % _AUDIT_PROBLEM["error"])
