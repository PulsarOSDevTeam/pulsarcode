# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""`pulsarcode top`: Pulsar Observatory, live mission control for Pulsar Core.

  pulsarcode top              full-screen live dashboard
  pulsarcode top --once       print one text summary and exit
  pulsarcode top --json       print one JSON snapshot and exit (implies --once)
  pulsarcode top --sid SID    start focused on one session

Two background threads talk to Core with the admin token (CoreClient.admin(),
which makes Core prove its identity before the token ever leaves this
process): one follows the /pulsar/events stream, the other polls /pulsar/stats
for the live session list. Both reconnect with backoff; with no Core running
the screen idles on a beacon and links up the moment one starts.

Everything they receive goes through a queue into Reducer, a pure state
machine (no I/O, no clock of its own, unit tested with synthetic streams)
that keeps throughput history, latency, provider health, totals and the
event feed. The UI thread renders a View of it at 10 to 20 fps through the
diff renderer, and degrades to ASCII, 256 and 16 colors, reduced motion and
tiny terminals; pipes, NO_COLOR and PULSAR_FX=off/plain get a one-shot text
summary. Every string from Core is cleaned of control characters on the way
in, and nothing here ever prints a token or a provider base URL.
"""

from __future__ import annotations

import json
import math
import os
import queue
import random
import re
import sys
import threading
import time
import unicodedata
import zlib
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence, Tuple

from pulsar import fx
from pulsar.fx import ASH, BAD, BEAM, DUST, EMBER, FLARE, ICE, NIGHT, OK, PLASMA, SPECTRUM, WARN, Canvas, Color
from pulsar.redact import redact

USAGE = """usage: pulsarcode top [--once] [--json] [--sid SID]

  (no flags)   live, animated mission control for Pulsar Core
  --once       print one text summary and exit
  --json       print one JSON snapshot and exit (implies --once)
  --sid SID    start focused on one session

keys  q quit   p pause   c clear feed   s / S next / previous session
      v every request in the feed   ? help
"""

HISTORY = 120            # seconds of throughput history
FEED_KEEP = 400
TTFT_KEEP = 240
RESULTS_KEEP = 24        # recent outcomes per provider (the health strip)
POLL_EVERY = 2.0         # /pulsar/stats cadence (the session list is not in the tick)
LIVE_WINDOW = 3.5        # seconds without data before the link counts as lost
LOST_HOLD = 10.0         # how long a lost link keeps the last picture before idling
FPS_FULL, FPS_LITE, FPS_CALM = 20, 15, 4
QUIET_KINDS = ("done", "cancel")          # feed kinds shown only with `v`
CHART: Tuple[Color, ...] = (BEAM, PLASMA, FLARE, EMBER)   # throughput area, bottom to top

_GU = {"mark": "◉", "dot": "●", "odot": "○", "ring": "◌", "sep": " · ", "chain": " › ", "to": " → ",
       "ell": "…", "warn": "▲", "relay": "↻", "pause": "❚❚", "ok": "✓", "bad": "✗", "bar": "▌",
       "diamond": "◆", "elbow": "╰", "cell": "▪", "nocell": "·", "vbar": "│", "spin": fx.SPINNER}
_GA = {"mark": "@", "dot": "*", "odot": "o", "ring": "o", "sep": " | ", "chain": " > ", "to": " -> ",
       "ell": "..", "warn": "!", "relay": "~", "pause": "||", "ok": "v", "bad": "x", "bar": "|",
       "diamond": "*", "elbow": "`", "cell": "#", "nocell": ".", "vbar": "|", "spin": "|/-\\"}

# Feed kinds: (unicode glyph, ascii glyph, label, color)
KINDS: Dict[str, Tuple[str, str, str, Color]] = {
    "relay": ("↻", "~", "RELAY", WARN),
    "heal": ("✚", "+", "HEAL", BEAM),
    "loop": ("⟲", "@", "LOOP", FLARE),
    "stage": ("▣", "#", "STAGE", PLASMA),
    "council": ("◈", "*", "COUNCIL", EMBER),
    "error": ("✗", "x", "ERROR", BAD),
    "breaker": ("⊘", "!", "BREAKER", BAD),
    "overflow": ("◐", "%", "CONTEXT", WARN),
    "link": ("◉", "@", "LINK", OK),
    "lost": ("○", "o", "LOST", BAD),
    "join": ("+", "+", "JOIN", OK),
    "left": ("-", "-", "LEFT", ASH),
    "done": ("✓", "v", "DONE", OK),
    "cancel": ("◌", "-", "CANCEL", DUST),
}


# ---------------------------------------------------------------------------
# Text and numbers
# ---------------------------------------------------------------------------

_INVISIBLE = frozenset("\u00ad\u061c\u180e\u200b\u200c\u200d\u200e\u200f\u2028\u2029\u202a\u202b\u202c"
                       "\u202d\u202e\u2060\u2061\u2062\u2063\u2064\u2066\u2067\u2068\u2069\ufeff")


def clean(text: Any, limit: int = 200) -> str:
    """Printable text that takes exactly one terminal cell per character.

    Strips C0 and C1 controls (no escape sequence can ride along in a model id or an upstream
    error), zero-width, bidi and other format characters and combining marks; wide glyphs (CJK,
    emoji) become '?' so a panel never shifts.
    """
    if text is None:
        return ""
    s = text if isinstance(text, str) else str(text)
    out: List[str] = []
    for ch in s[:limit * 4 + 16]:
        o = ord(ch)
        if o < 0x20 or 0x7F <= o <= 0x9F or ch in _INVISIBLE:
            continue
        if o >= 0xA0:
            cat = unicodedata.category(ch)
            if cat[0] in "CM" or cat in ("Zl", "Zp"):
                continue
            if unicodedata.east_asian_width(ch) in ("W", "F"):
                ch = "?"
        out.append(ch)
        if len(out) >= limit:
            break
    return "".join(out)


_ASCII_MAP = {0x00B7: ".", 0x2026: "..", 0x2192: "->", 0x203A: ">", 0x2039: "<", 0x2022: "*", 0x2013: "-",
              0x2014: "-", 0x00D7: "x", 0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"', 0x2500: "-",
              0x2502: "|", 0x25C9: "@", 0x25CF: "*", 0x25CB: "o"}


def ascii_only(text: str) -> str:
    """text for terminals without Unicode: typographic glyphs mapped, anything else becomes '?'."""
    return text.translate(_ASCII_MAP).encode("ascii", "replace").decode("ascii")


def _num(v: Any, default: float = 0.0) -> float:
    if isinstance(v, bool):
        return float(v)
    try:
        x = float(v)
    except (TypeError, ValueError, OverflowError):
        return default
    if x != x or x in (float("inf"), float("-inf")):
        return default
    return max(-1e15, min(1e15, x))


def _int(v: Any, default: int = 0) -> int:
    return int(_num(v, float(default)))


def human(n: Any) -> str:
    """Compact count: 999, 3.4k, 342k, 1.25M, 12.3M, 1.2B."""
    x = _num(n)
    a = abs(x)
    if a < 999.5:
        return "%d" % int(round(x))
    if a < 9950:
        return "%.1fk" % (x / 1e3)
    if a < 999500:
        return "%dk" % int(round(x / 1e3))
    if a < 9.995e6:
        return "%.2fM" % (x / 1e6)
    if a < 999.5e6:
        return "%.1fM" % (x / 1e6)
    return "%.1fB" % (x / 1e9)


def rate(x: Any) -> str:
    v = max(0.0, _num(x))
    if v < 9.95:
        return "%.1f" % v
    if v < 999.5:
        return "%d" % int(round(v))
    return human(v)


def dur(s: Any) -> str:
    s = max(0.0, _num(s))
    if s < 1:
        return "%dms" % int(s * 1000)
    if s < 9.95:
        return "%.1fs" % s
    if s < 60:
        return "%ds" % int(s)
    if s < 3600:
        return "%dm%02ds" % (s // 60, s % 60)
    if s < 86400:
        return "%dh%02dm" % (s // 3600, (s % 3600) // 60)
    return "%dd%02dh" % (s // 86400, (s % 86400) // 3600)


def ago(s: Any) -> str:
    s = max(0.0, _num(s))
    return "just now" if s < 2 else dur(s if s < 60 else int(s)) + " ago"


def money(c: Any) -> str:
    c = max(0.0, _num(c))
    if c == 0:
        return "$0"
    return "$%.4f" % c if c < 1 else "$%.2f" % c


def fails_text(n: int) -> str:
    return "%d fail%s" % (n, "" if n == 1 else "s")


def clock(t: float) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(t))
    except (ValueError, OverflowError, OSError):
        return "--:--:--"


def fit(s: str, n: int, ell: str = "…") -> str:
    """s cut to n cells, ending in ell when cut."""
    if n <= 0:
        return ""
    if len(s) <= n:
        return s
    if n <= len(ell):
        return s[:n]
    return s[:n - len(ell)] + ell


def fit_left(s: str, n: int, ell: str = "…") -> str:
    """s cut to n cells keeping the tail (paths)."""
    if n <= 0:
        return ""
    if len(s) <= n:
        return s
    if n <= len(ell):
        return s[-n:]
    return ell + s[-(n - len(ell)):]


def short_path(p: str) -> str:
    home = os.path.expanduser("~")
    if home and home not in ("/", "~") and (p == home or p.startswith(home.rstrip("/\\") + os.sep)):
        return "~" + p[len(home.rstrip("/\\")):]
    return p


def short_model(m: str, n: int = 28) -> str:
    """A model id that fits n cells: the vendor prefix goes first."""
    m = m or "default"
    if len(m) > n and "/" in m:
        m = m.rsplit("/", 1)[1]
    return m


def hue(sid: str) -> Color:
    """A stable spectrum color per session."""
    return fx.grad(SPECTRUM, (zlib.crc32(sid.encode("utf-8", "replace")) % 997) / 996.0)


def _host(url: Any) -> str:
    """Host[:port] of an endpoint URL: never the path, query or credentials."""
    if not isinstance(url, str) or not url.strip():
        return ""
    try:
        import urllib.parse
        u = urllib.parse.urlsplit(url.strip())
        host, port = u.hostname or "", u.port
    except ValueError:
        return ""
    return clean(host + (":%d" % port if port else ""), 80)


def _lift(c: Color) -> Color:
    """A brand color readable as text on a dark terminal."""
    m = max(c)
    return fx.lerp(c, ICE, min(0.85, (170 - m) / 255.0 + 0.15)) if m < 170 else c


_PROV: Dict[str, Tuple[str, Color]] = {}


def provider_meta(pid: str) -> Tuple[str, Color]:
    """(display name, brand color) for a provider id."""
    hit = _PROV.get(pid)
    if hit is not None:
        return hit
    name, color = pid or "unknown", BEAM
    try:
        from pulsar import providers as P
        p = P.get(pid)
        if p is not None:
            name = clean(p.name, 40) or name
            color = _lift(fx.hex_rgb(p.color))
    except Exception:
        pass
    if pid == "custom":
        name = "Custom"
    _PROV[pid] = (name, color)
    return name, color


def pct(vals: Sequence[float], q: float) -> float:
    """Nearest-rank percentile."""
    if not vals:
        return 0.0
    s = sorted(vals)
    i = min(len(s) - 1, max(0, int(math.ceil(q * len(s))) - 1))
    return s[i]


def speed_color(ttft: float) -> Color:
    if ttft <= 0:
        return DUST
    if ttft < 1.0:
        return OK
    if ttft < 2.5:
        return BEAM
    if ttft < 6.0:
        return WARN
    return BAD


# ---------------------------------------------------------------------------
# Reducer: events in, state out
# ---------------------------------------------------------------------------

class Series:
    """Per-second buckets over a sliding window (bucket keys are integer unix seconds)."""

    def __init__(self, span: int = HISTORY) -> None:
        self.span = span
        self.b: Dict[int, float] = {}

    def add(self, t: float, v: float = 1.0) -> None:
        if v <= 0:
            return
        s = int(t)
        self.b[s] = self.b.get(s, 0.0) + v
        self._prune(s)

    def spread(self, t0: float, t1: float, v: float) -> None:
        """Add v spread evenly over [t0, t1], by overlap with each one-second bucket."""
        if v <= 0:
            return
        if t1 - t0 > 5.0:
            t0 = t1 - 1.0
        if t1 - t0 <= 0.05:
            self.add(t1, v)
            return
        span = t1 - t0
        s = int(math.floor(t0))
        while s < t1:
            a, b = max(t0, float(s)), min(t1, float(s + 1))
            if b > a:
                self.b[s] = self.b.get(s, 0.0) + v * (b - a) / span
            s += 1
        self._prune(int(t1))

    def _prune(self, s: int) -> None:
        if len(self.b) > self.span + 16:
            cut = s - self.span - 8
            for k in [k for k in self.b if k < cut or k > s + 60]:
                del self.b[k]

    def values(self, now: float, n: Optional[int] = None) -> List[float]:
        """n per-second values ending with the last complete second, oldest first."""
        n = self.span if n is None else n
        end = int(now) - 1
        return [self.b.get(s, 0.0) for s in range(end - n + 1, end + 1)]

    def rate(self, now: float, secs: int = 3) -> float:
        return sum(self.values(now, secs)) / float(secs) if secs > 0 else 0.0

    def last(self) -> int:
        return max(self.b) if self.b else 0


class View:
    """Everything one frame shows, computed from a Reducer at one instant (plain, cleaned data)."""

    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)

    state: str
    now: float


def _agg(d: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {k: max(0, _int(d.get(k))) for k in
                           ("requests", "errors", "relays", "tokens_in", "tokens_out", "cache_read")}
    out["cost"] = max(0.0, _num(d.get("cost")))
    out["ttft_ewma"] = max(0.0, _num(d.get("ttft_ewma")))
    out["last_at"] = max(0.0, _num(d.get("last_at")))
    out["last_model"] = clean(d.get("last_model"), 160)
    out["last_provider"] = clean(d.get("last_provider"), 64)
    out["last_error"] = clean(scrub_urls(redact(str(d.get("last_error") or ""))), 200)
    return out


_URL = re.compile(r"(?i)\b(?:https?|wss?)://[^\s\"'<>`]+")


def scrub_urls(text: str) -> str:
    """`text` with every URL replaced by <url>: provider endpoints never reach the screen or --json."""
    return _URL.sub("<url>", str(text or ""))


def _error_kind(err: str) -> str:
    e = err.lower()
    if "client went away" in e:
        return "cancel"
    if "loop guard" in e:
        return "loop"
    if "context overflow" in e:
        return "overflow"
    return "error"


Part = Tuple[str, str]    # (text, role): role in txt, hi, dim, to, bad, warn, ok


class Reducer:
    """Pure state machine over Core's event stream and stats snapshots.

    Feed it with link(), event(), stats() and down(), always passing the clock (`now`, seconds);
    read it with view() or to_json(). It does no I/O and never reads the clock itself, so a
    synthetic stream replays deterministically. Every string is cleaned on the way in.
    """

    def __init__(self, history: int = HISTORY) -> None:
        self.history = max(10, int(history))
        self.feed: Deque[Dict[str, Any]] = deque(maxlen=FEED_KEEP)
        self.recent: Deque[Tuple[int, float, str, str]] = deque(maxlen=64)   # (n, at, kind, sid) for UI pulses
        self.seq = 0
        self.pulses = 0
        self.core: Dict[str, Any] = {}
        self.ever_linked = False
        self.last_rx = 0.0
        self.linked_at = 0.0
        self.down_reason = ""
        self.down_at = 0.0
        self.no_core = True
        self.hint_port = 0
        self._live_flag = False
        self._reset()

    def _reset(self) -> None:
        self.uptime = 0.0
        self.uptime_at = 0.0
        self.total: Dict[str, Any] = {}
        self.sess_agg: Dict[str, Dict[str, Any]] = {}
        self.prov_agg: Dict[str, Dict[str, Any]] = {}
        self.breakers: Dict[str, Dict[str, float]] = {}
        self.breakers_at = 0.0
        self.breaker_noted: Dict[str, float] = {}
        self.flights: Dict[str, Dict[str, Any]] = {}
        self.rid_sid: Dict[str, str] = {}
        self.seen: Dict[str, List[float]] = {}          # rid -> [tokens counted, last seen]
        self.live: Dict[str, Dict[str, Any]] = {}
        self.live_known = False
        self.tps = Series(self.history)
        self.rps = Series(self.history)
        self.eps = Series(self.history)
        self.tps_sid: Dict[str, Series] = {}
        self.ttft: Deque[Dict[str, Any]] = deque(maxlen=TTFT_KEEP)
        self.results: Dict[str, Deque[Tuple[float, bool]]] = {}
        self.counts = {"heals": 0, "loops": 0, "stage": 0, "council": 0, "relays": 0}
        self.last_relay: Dict[str, Dict[str, Any]] = {}
        self.last_end: Dict[str, Dict[str, Any]] = {}
        self.ends: Dict[str, Deque[Dict[str, Any]]] = {}
        self.profile_provider: Dict[str, str] = {}
        self.hosts: Dict[str, str] = {}
        self.snap_at = 0.0
        self.snaps = 0

    # -- link ------------------------------------------------------------------
    def linked(self, now: float) -> bool:
        return not self.no_core and self.last_rx > 0 and now - self.last_rx < LIVE_WINDOW

    def state(self, now: float) -> str:
        """live, lost (showing the last picture while relinking) or idle."""
        if self.linked(now):
            return "live"
        if self.ever_linked and not self.no_core and now - self.last_rx < LOST_HOLD:
            return "lost"
        return "idle"

    def link(self, info: Optional[Dict[str, Any]], now: float) -> None:
        """Data arrived from the Core described by info ({port, pid, version})."""
        info = info if isinstance(info, dict) else {}
        pid, port = max(0, _int(info.get("pid"))), max(0, _int(info.get("port")))
        if pid and self.core.get("pid") and pid != self.core.get("pid"):
            self._reset()               # a different Core: its numbers start over
            self._live_flag = False
        if pid or port:
            self.core = {"port": port, "pid": pid, "version": clean(info.get("version"), 24)}
            self.hint_port = port or self.hint_port
        self.last_rx = max(self.last_rx, now)
        self.no_core = False
        self.ever_linked = True
        if not self._live_flag:
            self._live_flag = True
            self.linked_at = now
            parts: List[Part] = [("Pulsar Core", "txt")]
            if self.core.get("port"):
                parts.append((" :%d" % self.core["port"], "hi"))
            if self.core.get("version"):
                parts.append(("  v" + self.core["version"], "dim"))
            if self.core.get("pid"):
                parts.append(("  pid %d" % self.core["pid"], "dim"))
            self._note(now, "link", "", parts)

    def down(self, reason: str, now: float, no_core: bool = False, info: Optional[Dict[str, Any]] = None) -> None:
        """A connection attempt failed (no_core: there is no run/core.json at all)."""
        self.down_reason = clean(redact(str(reason or "")), 160)
        self.down_at = now
        self.no_core = bool(no_core)
        if not no_core and isinstance(info, dict):
            self.hint_port = max(0, _int(info.get("port"))) or self.hint_port

    def step(self, now: float) -> None:
        """Advance the clock: notices a link that went quiet."""
        if self._live_flag and not self.linked(now):
            self._live_flag = False
            why = "Pulsar Core stopped" if self.no_core else (self.down_reason or "no data from Pulsar Core")
            self._note(now, "lost", "", [(why, "txt")])
            self.flights.clear()

    # -- input -----------------------------------------------------------------
    def event(self, name: str, data: Any, now: float) -> None:
        """One /pulsar/events message."""
        if not isinstance(data, dict):
            return
        fn: Optional[Callable[[Dict[str, Any], float], None]] = {
            "tick": self._snapshot, "begin": self._begin, "end": self._end, "relay": self._relay,
            "heal": self._heal, "loop": self._loop, "stage": self._stage, "council": self._council,
        }.get(str(name))
        if fn is not None:
            fn(data, now)

    def stats(self, snap: Any, now: float) -> None:
        """A GET /pulsar/stats reply (a tick snapshot plus sessions_live)."""
        if not isinstance(snap, dict):
            return
        self._snapshot(snap, now)
        if isinstance(snap.get("sessions_live"), list):
            self._sessions(snap["sessions_live"], now)

    def clear_feed(self) -> None:
        self.feed.clear()

    # -- internals -------------------------------------------------------------
    def _note(self, now: float, kind: str, sid: str, parts: List[Part], t: float = 0.0) -> None:
        self.seq += 1
        parts = [(clean(s, 240), role) for s, role in parts if s or role == "to"]
        self.feed.append({"id": self.seq, "t": t or now, "at": now, "kind": kind, "sid": sid, "parts": parts})
        self._pulse(now, kind, sid)

    def _pulse(self, now: float, kind: str, sid: str) -> None:
        self.pulses += 1
        self.recent.append((self.pulses, now, kind, sid))

    def _series(self, sid: str) -> Series:
        s = self.tps_sid.get(sid)
        if s is None:
            s = self.tps_sid[sid] = Series(self.history)
        return s

    def _prov_of(self, profile: str) -> str:
        if not profile:
            return ""
        return self.profile_provider.get(profile) or ("custom" if profile.startswith("custom") else profile)

    def _result(self, provider: str, now: float, ok: bool) -> None:
        if provider:
            dq = self.results.get(provider)
            if dq is None:
                if len(self.results) > 64:
                    return
                dq = self.results[provider] = deque(maxlen=RESULTS_KEEP)
            dq.append((now, ok))

    def _snapshot(self, snap: Dict[str, Any], now: float) -> None:
        up = _num(snap.get("uptime"), -1.0)
        if up >= 0:
            self.uptime, self.uptime_at = up, now
        if isinstance(snap.get("total"), dict):
            self.total = _agg(snap["total"])
        if isinstance(snap.get("sessions"), dict):
            self.sess_agg = {clean(k, 64): _agg(v) for k, v in list(snap["sessions"].items())[:512]
                             if isinstance(v, dict)}
        if isinstance(snap.get("providers"), dict):
            self.prov_agg = {clean(k, 64): _agg(v) for k, v in list(snap["providers"].items())[:128]
                             if isinstance(v, dict)}
        if isinstance(snap.get("breakers"), dict):
            self._breakers(snap["breakers"], now)
        if isinstance(snap.get("flights"), list):
            self._flights(snap["flights"], now)
        self.snaps += 1
        self.snap_at = now

    def _breakers(self, raw: Dict[str, Any], now: float) -> None:
        fresh: Dict[str, Dict[str, float]] = {}
        for k, v in list(raw.items())[:128]:
            if not isinstance(v, dict):
                continue
            key = clean(k, 240)
            left = max(0.0, _num(v.get("open_for")))
            if not key or left <= 0:
                continue
            old = self.breakers.get(key)
            expected = old["left"] - (now - self.breakers_at) if old else -1.0
            rearmed = old is None or left > expected + 1.5
            span = left if rearmed or old is None else max(old["span"], left)
            fails = max(0, _int(v.get("fails")))
            fresh[key] = {"left": left, "fails": fails, "span": max(0.1, span)}
            if rearmed and now - self.breaker_noted.get(key, -1e9) >= 60.0:   # a dead target re-arms often
                self.breaker_noted[key] = now
                profile, _, model = key.partition("|")
                prov = self._prov_of(profile)
                name = provider_meta(prov)[0] if prov else profile
                self._note(now, "breaker", "", [(short_model(model or profile, 40), "hi"), ("  on " + name, "txt"),
                                                ("  cooling %s after %d fail%s" % (dur(left), fails,
                                                                                    "" if fails == 1 else "s"), "bad")])
        self.breakers = fresh
        self.breakers_at = now
        if len(self.breaker_noted) > 256:
            self.breaker_noted = {k: t for k, t in self.breaker_noted.items() if now - t < 60.0}

    def _flights(self, raw: List[Any], now: float) -> None:
        start = self.snap_at if self.snap_at and 0 < now - self.snap_at < 5 else now - 1.0
        baseline = self.snaps == 0          # tokens generated before we watched are not throughput
        fresh: Dict[str, Dict[str, Any]] = {}
        gained = 0.0
        per: Dict[str, float] = {}
        for f in raw[:256]:
            if not isinstance(f, dict):
                continue
            rid = clean(f.get("rid"), 40)
            if not rid:
                continue
            sid = clean(f.get("sid"), 64)
            tokens = max(0, _int(f.get("tokens")))
            elapsed = max(0.0, _num(f.get("elapsed")))
            old = self.flights.get(rid)
            fresh[rid] = {"rid": rid, "sid": sid, "model": clean(f.get("model"), 160),
                          "provider": clean(f.get("provider"), 64), "elapsed": elapsed,
                          "waiting": bool(f.get("waiting")), "tokens": tokens, "tps": max(0.0, _num(f.get("tps"))),
                          "relays": max(0, _int(f.get("relays"))), "at": now,
                          "born": old["born"] if old else now - elapsed}
            self.rid_sid[rid] = sid
            seen = self.seen.get(rid)
            if seen is None:
                seen = self.seen[rid] = [float(tokens) if baseline else 0.0, now]
            d = tokens - seen[0]
            if d > 0:
                seen[0] = float(tokens)
                gained += d
                per[sid] = per.get(sid, 0.0) + d
            seen[1] = now
        self.flights = fresh
        if gained:
            self.tps.spread(start, now, gained)
            for sid, d in per.items():
                self._series(sid).spread(start, now, d)
        for rid in [r for r, s in self.seen.items() if r not in fresh and now - s[1] > 30]:
            self.seen.pop(rid, None)
            self.rid_sid.pop(rid, None)

    def _begin(self, d: Dict[str, Any], now: float) -> None:
        rid = clean(d.get("rid"), 40)
        if not rid:
            return
        sid = clean(d.get("sid"), 64)
        self.rps.add(now)
        self.rid_sid[rid] = sid
        self.seen.setdefault(rid, [0.0, now])
        if rid not in self.flights and len(self.flights) < 256:
            self.flights[rid] = {"rid": rid, "sid": sid, "model": clean(d.get("model"), 160),
                                 "provider": clean(d.get("provider"), 64), "elapsed": 0.0, "waiting": True,
                                 "tokens": 0, "tps": 0.0, "relays": 0, "at": now, "born": now}
        self._pulse(now, "begin", sid)

    def _end(self, d: Dict[str, Any], now: float) -> None:
        rid = clean(d.get("rid"), 40)
        sid = clean(d.get("sid"), 64) or self.rid_sid.get(rid, "")
        ok = bool(d.get("ok"))
        out = max(0, _int(d.get("tokens_out")))
        seen = self.seen.pop(rid, None)
        gain = out - (seen[0] if seen else 0.0)
        if gain > 0:
            self.tps.add(now, gain)
            self._series(sid).add(now, gain)
        self.flights.pop(rid, None)
        self.rid_sid.pop(rid, None)
        model, prov = clean(d.get("model"), 160), clean(d.get("provider"), 64)
        ttft, took = max(0.0, _num(d.get("ttft"))), max(0.0, _num(d.get("dur")))
        err = clean(scrub_urls(redact(str(d.get("error") or ""))), 160)
        kind = "done" if ok else _error_kind(err)
        if ttft > 0:
            self.ttft.append({"t": now, "v": ttft, "provider": prov, "sid": sid})
        if kind in ("done", "error"):
            self._result(prov, now, ok)
        if kind == "error":
            self.eps.add(now)
        self.last_end[sid] = {"at": now, "ok": ok, "kind": kind, "model": model, "provider": prov,
                              "tokens_out": out, "dur": took, "ttft": ttft, "error": err}
        if kind != "loop":
            dq = self.ends.get(sid)
            if dq is None:
                dq = self.ends[sid] = deque(maxlen=4)
            dq.append(self.last_end[sid])
            if len(self.ends) > 256:
                for k in list(self.ends)[:64]:
                    self.ends.pop(k, None)
        if len(self.last_end) > 512:
            for k in list(self.last_end)[:128]:
                self.last_end.pop(k, None)
        when = _num(d.get("t")) or now
        name = short_model(model, 40)
        if kind == "done":
            parts: List[Part] = [(name, "hi"), ("  %s tok" % human(out), "txt"), ("  %s" % dur(took), "dim")]
            if ttft:
                parts.append(("  ttft %s" % dur(ttft), "dim"))
            self._note(now, "done", sid, parts, when)
        elif kind == "error":
            self._note(now, "error", sid, [(name, "hi"), ("  " + (err or "request failed"), "bad")], when)
        elif kind == "overflow":
            self._note(now, "overflow", sid, [(name, "hi"), ("  context is full; Claude Code compacts", "warn")], when)
        elif kind == "cancel":
            self._note(now, "cancel", sid, [(name, "hi"), ("  cancelled by Claude Code", "dim")], when)
        else:
            self._pulse(now, "end", sid)

    def _relay(self, d: Dict[str, Any], now: float) -> None:
        rid = clean(d.get("rid"), 40)
        sid = clean(d.get("sid"), 64) or self.rid_sid.get(rid, "")
        frm, to = clean(d.get("frm"), 240), clean(d.get("to"), 240)
        why = clean(scrub_urls(redact(str(d.get("reason") or ""))), 120)
        fprof, _, fmodel = frm.partition("|")
        tprov, _, tmodel = to.partition("|")
        self.counts["relays"] += 1
        self.last_relay[sid] = {"at": now, "from": fmodel or fprof, "to": tmodel or tprov, "reason": why,
                                "from_provider": self._prov_of(fprof), "to_provider": tprov}
        if len(self.last_relay) > 512:
            for k in list(self.last_relay)[:128]:
                self.last_relay.pop(k, None)
        f = self.flights.get(rid)
        if f is not None:
            f["model"], f["provider"] = tmodel, tprov
            f["relays"] += 1
        self._result(self._prov_of(fprof), now, False)
        parts: List[Part] = [(short_model(fmodel or fprof, 40), "hi"), ("", "to"), (short_model(tmodel or tprov, 40), "hi")]
        if why:
            parts.append(("  " + why, "warn"))
        self._note(now, "relay", sid, parts, _num(d.get("t")) or now)

    def _heal(self, d: Dict[str, Any], now: float) -> None:
        prof, model = clean(d.get("profile"), 80), clean(d.get("model"), 160)
        fix = d.get("fix")
        fixes = [clean(x, 40) for x in fix[:8]] if isinstance(fix, list) else []
        self.counts["heals"] += 1
        self._note(now, "heal", clean(d.get("sid"), 64),
                   [(short_model(model or prof, 40), "hi"),
                    ("  self-healed: " + (", ".join(x for x in fixes if x) or "request adjusted"), "txt")],
                   _num(d.get("t")) or now)

    def _loop(self, d: Dict[str, Any], now: float) -> None:
        self.counts["loops"] += 1
        detail = clean(scrub_urls(redact(str(d.get("detail") or ""))), 120)
        self._note(now, "loop", clean(d.get("sid"), 64),
                   [("stopped a runaway loop", "txt")] + ([("  " + detail, "dim")] if detail else []),
                   _num(d.get("t")) or now)

    def _stage(self, d: Dict[str, Any], now: float) -> None:
        self.counts["stage"] += 1
        kind, title = clean(d.get("kind"), 24) or "scene", clean(d.get("title"), 120)
        self._note(now, "stage", clean(d.get("sid"), 64),
                   [(kind, "hi")] + ([("  " + title, "txt")] if title else []), _num(d.get("t")) or now)

    def _council(self, d: Dict[str, Any], now: float) -> None:
        self.counts["council"] += 1
        models = d.get("models")
        names = [short_model(clean(m, 120), 24) for m in models[:6]] if isinstance(models, list) else []
        self._note(now, "council", clean(d.get("sid"), 64),
                   [("%d model%s" % (len(names), "" if len(names) == 1 else "s"), "hi")] +
                   ([("  " + ", ".join(names), "txt")] if names else []), _num(d.get("t")) or now)

    def _sessions(self, lst: List[Any], now: float) -> None:
        fresh: Dict[str, Dict[str, Any]] = {}
        for s in lst[:64]:
            if not isinstance(s, dict):
                continue
            sid = clean(s.get("sid"), 64)
            if not sid:
                continue
            plan: List[Dict[str, str]] = []
            raw_plan = s.get("plan") if isinstance(s.get("plan"), list) else []
            for t in raw_plan[:8]:
                if not isinstance(t, dict):
                    continue
                tgt = {"profile": clean(t.get("profile"), 80), "provider": clean(t.get("provider"), 64),
                       "model": clean(t.get("model"), 160)}
                plan.append(tgt)
                if tgt["profile"] and tgt["provider"]:
                    self.profile_provider[tgt["profile"]] = tgt["provider"]
                host = _host(t.get("base_url"))
                if host and tgt["provider"] == "custom":
                    self.hosts[tgt["profile"]] = host
            old = self.live.get(sid)
            fresh[sid] = {"sid": sid, "label": clean(s.get("label"), 60), "cwd": clean(s.get("cwd"), 300),
                          "pid": max(0, _int(s.get("pid"))), "plan": plan, "since": old["since"] if old else now}
        if self.live_known:
            for k in fresh:
                if k not in self.live:
                    self._note(now, "join", k, [(short_path(fresh[k]["cwd"]) or "new session", "txt")])
            for k in self.live:
                if k not in fresh:
                    self._note(now, "left", k, [("session ended", "dim")])
        elif fresh:
            self._note(now, "join", "", [("%d session%s live" % (len(fresh), "" if len(fresh) == 1 else "s"), "txt")])
        ordered = {k: fresh[k] for k in self.live if k in fresh}
        ordered.update({k: v for k, v in fresh.items() if k not in ordered})
        self.live = ordered
        self.live_known = True
        for k in [k for k, ser in self.tps_sid.items() if k not in fresh and ser.last() < now - self.history - 5]:
            del self.tps_sid[k]

    # -- output ----------------------------------------------------------------
    def _open(self, now: float) -> Dict[str, Dict[str, float]]:
        out = {}
        for key, b in self.breakers.items():
            left = b["left"] - (now - self.breakers_at)
            if left > 0:
                out[key] = {"left": left, "fails": b["fails"], "span": b["span"]}
        return out

    def _session_views(self, now: float, opened: Dict[str, Dict[str, float]]) -> List[Dict[str, Any]]:
        order = list(self.live)
        if not self.live_known:
            for sid, agg in self.sess_agg.items():
                if sid not in order and now - agg.get("last_at", 0.0) < 600:
                    order.append(sid)
        for f in self.flights.values():
            if f["sid"] and f["sid"] not in order:
                order.append(f["sid"])
        out = []
        for sid in order[:64]:
            info = self.live.get(sid, {})
            fl = [dict(f, elapsed=f["elapsed"] + max(0.0, now - f["at"]))
                  for f in sorted((f for f in self.flights.values() if f["sid"] == sid), key=lambda f: f["born"])]
            answering = {(f["provider"], f["model"]) for f in fl}
            targets = []
            for t in info.get("plan") or []:
                key = t["profile"] + "|" + t["model"]
                state = "live" if (t["provider"], t["model"]) in answering else ("open" if key in opened else "idle")
                targets.append(dict(t, state=state))
            relay = self.last_relay.get(sid)
            ser = self.tps_sid.get(sid)
            out.append({"sid": sid, "label": info.get("label", ""), "cwd": info.get("cwd", ""),
                        "pid": info.get("pid", 0), "plan": targets, "agg": self.sess_agg.get(sid, {}),
                        "flights": fl, "relay": relay if relay and now - relay["at"] < 90 else None,
                        "last": self.last_end.get(sid), "tps": ser.rate(now) if ser else 0.0,
                        "hist": ser.values(now, 60) if ser else [0.0] * 60, "hue": hue(sid),
                        "recent": list(self.ends.get(sid, ()))[::-1],
                        "live": sid in self.live, "since": info.get("since", now)})
        return out

    def _provider_views(self, now: float, opened: Dict[str, Dict[str, float]]) -> List[Dict[str, Any]]:
        ids: List[str] = []

        def add(p: str) -> None:
            if p and p not in ids:
                ids.append(p)
        for p in self.prov_agg:
            add(p)
        for s in self.live.values():
            for t in s["plan"]:
                add(t["provider"])
        for f in self.flights.values():
            add(f["provider"])
        opens: Dict[str, List[Dict[str, Any]]] = {}
        for key, b in opened.items():
            profile, _, model = key.partition("|")
            prov = self._prov_of(profile)
            add(prov)
            opens.setdefault(prov, []).append({"profile": profile, "model": model, "left": b["left"],
                                               "fails": int(b["fails"]), "span": b["span"]})
        out = []
        for pid in ids[:32]:
            agg = self.prov_agg.get(pid, {})
            res = list(self.results.get(pid, ()))
            recent_bad = any(not ok for t, ok in res[-5:] if now - t < 120)
            busy = sum(1 for f in self.flights.values() if f["provider"] == pid)
            req, err = agg.get("requests", 0), agg.get("errors", 0)
            br = sorted(opens.get(pid, []), key=lambda b: -b["left"])
            # Red OPEN only when every known target of this provider has an open breaker (and nothing of it
            # answered lately); one open profile or model among working ones is degraded, stats kept.
            known = {t["profile"] + "|" + t["model"] for s in self.live.values() for t in s["plan"]
                     if t["provider"] == pid}
            open_keys = {b["profile"] + "|" + b["model"] for b in br}
            recent_ok = any(ok for t, ok in res[-5:] if now - t < 60)
            all_open = bool(br) and known <= open_keys and not recent_ok
            health = ("open" if all_open else "degraded" if br or recent_bad else "busy" if busy
                      else "ok" if req else "idle")
            name, color = provider_meta(pid)
            hosts = sorted({h for prof, h in self.hosts.items() if self._prov_of(prof) == pid})
            if pid == "custom" and len(hosts) == 1:
                name = "Custom " + hosts[0]
            out.append({"id": pid, "name": name, "color": color, "health": health, "requests": req, "errors": err,
                        "err_rate": (err / float(req)) if req else 0.0, "ttft": agg.get("ttft_ewma", 0.0),
                        "tokens_in": agg.get("tokens_in", 0), "tokens_out": agg.get("tokens_out", 0),
                        "results": [ok for _, ok in res], "breakers": br, "busy": busy,
                        "last_error": agg.get("last_error", "")})
        rank = {"open": 0, "degraded": 1, "busy": 2, "ok": 3, "idle": 4}
        out.sort(key=lambda p: (rank.get(p["health"], 5), -p["requests"], p["name"].lower()))
        return out

    def view(self, now: float, focus: Optional[str] = None, verbose: bool = False) -> View:
        state = self.state(now)
        opened = self._open(now)
        sessions = self._session_views(now, opened)
        if focus not in [s["sid"] for s in sessions]:
            focus = None
        providers = self._provider_views(now, opened)
        series = self.tps_sid.get(focus) if focus else self.tps
        tps_hist = series.values(now) if series is not None else [0.0] * self.history
        flights = [f for f in self.flights.values() if not focus or f["sid"] == focus]
        if self.snaps >= 3:
            tps_now = (series.rate(now, 3) if series is not None else 0.0)
        else:
            tps_now = sum(f["tps"] for f in flights if not f["waiting"])
        ttfts = [x["v"] for x in self.ttft if not focus or x["sid"] == focus]
        feed = [e for e in self.feed if (verbose or e["kind"] not in QUIET_KINDS) and
                (not focus or not e["sid"] or e["sid"] == focus)]
        t = self.total
        totals = {"requests": t.get("requests", 0), "errors": t.get("errors", 0),
                  "tokens_in": t.get("tokens_in", 0), "tokens_out": t.get("tokens_out", 0),
                  "cache_read": t.get("cache_read", 0), "cost": t.get("cost", 0.0),
                  "relays": max(t.get("relays", 0), self.counts["relays"]), "heals": self.counts["heals"],
                  "loops": self.counts["loops"], "stage": self.counts["stage"], "council": self.counts["council"],
                  "in_flight": len(self.flights), "sessions": len([s for s in sessions if s["live"]]) or len(sessions),
                  "ttft_ewma": t.get("ttft_ewma", 0.0)}
        live = state == "live"
        return View(state=state, now=now, core=dict(self.core), port=self.core.get("port") or self.hint_port,
                    uptime=self.uptime + (max(0.0, now - self.uptime_at) if live and self.uptime_at else 0.0),
                    since_rx=(now - self.last_rx) if self.last_rx else -1.0, down_reason=self.down_reason,
                    no_core=self.no_core, ever_linked=self.ever_linked, linked_at=self.linked_at,
                    totals=totals, tps_now=tps_now, rps_now=self.rps.rate(now, 10), tps_hist=tps_hist,
                    tps_peak=max(tps_hist) if tps_hist else 0.0, rps_hist=self.rps.values(now),
                    eps_hist=self.eps.values(now), sessions=sessions, providers=providers, ttft=ttfts,
                    p50=pct(ttfts, 0.5), p95=pct(ttfts, 0.95), ewma=t.get("ttft_ewma", 0.0),
                    feed=feed[-200:], focus=focus, verbose=verbose, seq=self.seq, history=self.history,
                    live_known=self.live_known)

    def to_json(self, now: float) -> Dict[str, Any]:
        """A JSON-safe snapshot for scripts: no tokens, no endpoint URLs."""
        v = self.view(now, verbose=True)
        return {
            "generated": round(now, 3),
            "core": {"running": v.state != "idle", "state": v.state, "port": v.port or None,
                     "pid": v.core.get("pid") or None, "version": v.core.get("version") or None,
                     "uptime": round(v.uptime, 1)},
            "totals": {k: (round(x, 6) if isinstance(x, float) else x) for k, x in v.totals.items()},
            "throughput": {"tokens_per_s": round(v.tps_now, 2), "requests_per_s": round(v.rps_now, 3),
                           "window_s": self.history, "tokens_per_s_history": [round(x, 2) for x in v.tps_hist],
                           "requests_history": [int(x) for x in v.rps_hist]},
            "latency": {"p50": round(v.p50, 3), "p95": round(v.p95, 3), "ewma": round(v.ewma, 3),
                        "samples": len(v.ttft)},
            "sessions": [{
                "sid": s["sid"], "label": s["label"], "cwd": s["cwd"], "pid": s["pid"],
                "plan": [{"profile": t["profile"], "provider": t["provider"], "model": t["model"],
                          "state": t["state"]} for t in s["plan"]],
                "requests": s["agg"].get("requests", 0), "errors": s["agg"].get("errors", 0),
                "relays": s["agg"].get("relays", 0), "tokens_in": s["agg"].get("tokens_in", 0),
                "tokens_out": s["agg"].get("tokens_out", 0), "cache_read": s["agg"].get("cache_read", 0),
                "cost": round(s["agg"].get("cost", 0.0), 6), "ttft_ewma": round(s["agg"].get("ttft_ewma", 0.0), 3),
                "last_model": s["agg"].get("last_model", ""), "last_provider": s["agg"].get("last_provider", ""),
                "flights": [{"rid": f["rid"], "model": f["model"], "provider": f["provider"],
                             "elapsed": round(f["elapsed"], 2), "waiting": f["waiting"], "tokens": f["tokens"],
                             "tps": f["tps"], "relays": f["relays"]} for f in s["flights"]],
            } for s in v.sessions],
            "providers": [{
                "id": p["id"], "name": p["name"], "health": p["health"], "requests": p["requests"],
                "errors": p["errors"], "error_rate": round(p["err_rate"], 4), "ttft_ewma": round(p["ttft"], 3),
                "tokens_in": p["tokens_in"], "tokens_out": p["tokens_out"], "in_flight": p["busy"],
                "last_error": scrub_urls(p["last_error"]),
                "breakers": [{"target": b["profile"] + "|" + b["model"], "open_for": round(b["left"], 1),
                              "fails": b["fails"]} for b in p["breakers"]],
            } for p in v.providers],
            "feed": [{"t": round(e["t"], 3), "kind": e["kind"], "sid": e["sid"], "text": entry_text(e)}
                     for e in v.feed[-50:]],
        }


def entry_text(e: Dict[str, Any], to: str = " -> ") -> str:
    return "".join(to if role == "to" else s for s, role in e.get("parts") or [])


# ---------------------------------------------------------------------------
# Link: the two background threads
# ---------------------------------------------------------------------------

def _admin_client() -> Tuple[Any, Optional[Dict[str, Any]]]:
    """(CoreClient with the admin token, {port, pid, version, started}) or (None, None)."""
    from pulsar.core import state as ST
    from pulsar.core.client import CoreClient
    info = ST.read_core()
    if info is None:
        return None, None
    client = CoreClient.admin()
    if client is None:
        return None, None
    return client, {"port": info.port, "pid": info.pid, "version": info.version, "started": info.started}


class Link:
    """Follows /pulsar/events and polls /pulsar/stats on daemon threads; the UI thread drains the queue.

    The admin token stays inside the CoreClient; only parsed events and snapshots cross the queue.
    """

    def __init__(self, factory: Optional[Callable[[], Tuple[Any, Optional[Dict[str, Any]]]]] = None,
                 poll: float = POLL_EVERY, sse_timeout: float = 6.0) -> None:
        self.factory = factory or _admin_client
        self.poll = poll
        self.sse_timeout = sse_timeout
        self.q: "queue.Queue[Tuple[Any, ...]]" = queue.Queue(maxsize=20000)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._cached: Optional[Tuple[Tuple[Any, ...], Any]] = None
        self.threads: List[threading.Thread] = []
        self.checked_at = 0.0

    def start(self) -> "Link":
        for target, name in ((self._events_loop, "pulsar-top-events"), (self._poll_loop, "pulsar-top-stats")):
            th = threading.Thread(target=target, name=name, daemon=True)
            th.start()
            self.threads.append(th)
        return self

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def _put(self, item: Tuple[Any, ...]) -> None:
        try:
            self.q.put_nowait(item)
        except queue.Full:
            pass                    # a stalled UI drops data rather than growing without bound

    def _client(self) -> Tuple[Any, Optional[Dict[str, Any]]]:
        """The client for the Core run/core.json names now, reusing one that already proved itself."""
        try:
            client, info = self.factory()
        except Exception:
            return None, None
        if client is None:
            return None, None
        key = tuple(sorted((info or {}).items()))
        with self._lock:
            if self._cached is not None and self._cached[0] == key:
                return self._cached[1], info
            self._cached = (key, client)
        return client, info

    def _events_loop(self) -> None:
        from pulsar.core.client import CoreUnavailable
        delay = 0.5
        while not self._stop.is_set():
            client, info = self._client()
            if client is None:
                self._put(("down", "no Pulsar Core is running", True, None))
                self._stop.wait(1.0)
                continue
            reason = "Pulsar Core closed the event stream"
            try:
                for name, data in client.events(timeout=self.sse_timeout):
                    if self._stop.is_set():
                        return
                    self._put(("ev", info, name, data))
                    delay = 0.5
            except CoreUnavailable as exc:
                reason = str(exc) or "Pulsar Core is not answering"
            except Exception as exc:          # timeouts, resets, a truncated stream
                reason = "event stream: %s" % type(exc).__name__
            self._put(("down", reason, False, info))
            self._stop.wait(delay)
            delay = min(5.0, delay * 2)

    def _poll_loop(self) -> None:
        from pulsar.core.client import CoreUnavailable
        while not self._stop.is_set():
            client, info = self._client()
            self.checked_at = time.time()
            if client is None:
                self._put(("down", "no Pulsar Core is running", True, None))
                self._stop.wait(1.0)
                continue
            try:
                snap = client.get("/pulsar/stats", timeout=2.5)
                if isinstance(snap, dict):
                    self._put(("stats", info, snap))
            except CoreUnavailable as exc:
                self._put(("down", str(exc) or "Pulsar Core is not answering", False, info))
            except Exception as exc:
                self._put(("down", "stats: %s" % type(exc).__name__, False, info))
            self._stop.wait(self.poll)

    def drain(self, red: Reducer, now: float, limit: int = 5000) -> int:
        n = 0
        while n < limit:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                break
            n += 1
            kind = item[0]
            if kind == "ev":
                red.link(item[1], now)
                red.event(item[2], item[3], now)
            elif kind == "stats":
                red.link(item[1], now)
                red.stats(item[2], now)
            elif kind == "down":
                red.down(item[1], now, no_core=item[2], info=item[3])
        return n


# ---------------------------------------------------------------------------
# Drawing primitives
# ---------------------------------------------------------------------------

_BL = (0, 0x40, 0x44, 0x46, 0x47)      # braille left column, k dots filled from the bottom
_BR = (0, 0x80, 0xA0, 0xB0, 0xB8)      # braille right column
_EIGHTHS = " ▁▂▃▄▅▆▇█"
_EIGHTHS_A = " ..::=+##"
_HEIGHTHS = " ▏▎▍▌▋▊▉█"

_BIG: Dict[str, Tuple[str, str, str]] = {
    "0": ("█▀█", "█ █", "▀▀▀"), "1": ("▀█ ", " █ ", "▀▀▀"), "2": ("▀▀█", "█▀▀", "▀▀▀"),
    "3": ("▀▀█", " ▀█", "▀▀▀"), "4": ("█ █", "▀▀█", "  ▀"), "5": ("█▀▀", "▀▀█", "▀▀▀"),
    "6": ("█▀▀", "█▀█", "▀▀▀"), "7": ("▀▀█", "  █", "  ▀"), "8": ("█▀█", "█▀█", "▀▀▀"),
    "9": ("█▀█", "▀▀█", "▀▀▀"), ".": (" ", " ", "▀"), "k": ("█ ▄", "█▀▄", "▀ ▀"),
    "M": ("█▄ ▄█", "█ ▀ █", "▀   ▀"), "B": ("█▀▄", "█▀▄", "▀▀ "),
}


def big_width(text: str) -> int:
    return sum(len(_BIG.get(ch, _BIG["0"])[0]) + 1 for ch in text) - (1 if text else 0)


def big_text(cv: Canvas, x: int, y: int, text: str, t: float = 0.0, stops: Sequence[Color] = SPECTRUM,
             glow: float = 0.0) -> int:
    """Three-row block digits in a drifting gradient. Returns the width drawn."""
    total = max(1, big_width(text) - 1)
    cx = x
    for ch in text:
        rows = _BIG.get(ch, _BIG["0"])
        for r, row in enumerate(rows):
            for i, g in enumerate(row):
                if g != " ":
                    col = fx.grad(stops, fx.tri((cx + i - x) / float(total) + t))
                    if glow > 0:
                        col = fx.lerp(col, ICE, glow)
                    cv.put(cx + i, y + r, g, col, True)
        cx += len(rows[0]) + 1
    return cx - x - 1


def area_chart(cv: Canvas, x: int, y: int, w: int, h: int, samples: Sequence[float], u: bool = True,
               sweep: Optional[float] = None, colors: Optional[Sequence[Color]] = None,
               fade: bool = True) -> List[int]:
    """Braille area chart of samples in [0, 1] (two per cell, oldest first; one per cell in ASCII).

    Colored by height (CHART, cool at the floor, hot at the top) unless `colors` gives one color
    per cell column. Returns the crest row of each column (-1 where empty) for the live head.
    """
    crest_rows: List[int] = []
    if w <= 0 or h <= 0:
        return crest_rows
    dots = h * (4 if u else 3)
    for cx in range(w):
        if u:
            a = samples[2 * cx] if 2 * cx < len(samples) else 0.0
            b = samples[2 * cx + 1] if 2 * cx + 1 < len(samples) else 0.0
        else:
            a = b = samples[cx] if cx < len(samples) else 0.0
        na = int(round(fx.clamp01(a) * dots))
        nb = int(round(fx.clamp01(b) * dots))
        if a > 0.004 and na == 0:
            na = 1                      # activity is never invisible
        if b > 0.004 and nb == 0:
            nb = 1
        depth = (0.42 + 0.58 * (cx / float(max(1, w - 1))) ** 0.8) if fade else 1.0
        lit = sweep is not None and 0 <= sweep - cx < 3
        crest = -1
        for cy in range(h):
            per = 4 if u else 3
            base = (h - 1 - cy) * per
            ka, kb = max(0, min(per, na - base)), max(0, min(per, nb - base))
            if ka == 0 and kb == 0:
                continue
            if crest < 0:
                crest = cy
            if u:
                ch = chr(0x2800 + _BL[ka] + _BR[kb])
            else:
                k = max(ka, kb)
                ch = "#" if k >= 3 else ":" if k == 2 else "."
            if colors is not None:
                col = colors[cx] if cx < len(colors) else BEAM
            else:
                col = fx.grad(CHART, (h - 1 - cy + 0.5) / float(h))
            top = cy == crest
            col = fx.lerp(col, ICE, 0.32) if top else col
            bright = (1.0 if top else 0.58) * depth
            if lit:
                col = fx.lerp(col, ICE, 0.55 * (1 - (sweep - cx) / 3.0))
                bright = max(bright, 0.85)
            cv.put(x + cx, y + cy, ch, fx.scale(col, bright), top)
        crest_rows.append(crest)
    return crest_rows


def vbars(cv: Canvas, x: int, y: int, w: int, h: int, vals: Sequence[float], colors: Sequence[Color],
          u: bool = True) -> None:
    """Right-aligned vertical bars (one column each) of vals in [0, 1], eighth-cell precision."""
    vals = list(vals)[-w:] if w > 0 else []
    colors = list(colors)[-len(vals):] if vals else []
    off = x + w - len(vals)
    ramp = _EIGHTHS if u else _EIGHTHS_A
    for i, v in enumerate(vals):
        total = fx.clamp01(v) * h * 8
        if v > 0:
            total = max(1.0, total)
        for r in range(h):
            fill = int(max(0.0, min(8.0, total - r * 8)))
            if fill <= 0:
                break
            cv.put(off + i, y + h - 1 - r, ramp[fill], colors[i] if i < len(colors) else BEAM)


def hbar(cv: Canvas, x: int, y: int, w: int, frac: float, color: Color, u: bool = True,
         track: Optional[Color] = NIGHT) -> None:
    """Horizontal bar with eighth-cell precision on a faint track."""
    if w <= 0:
        return
    total = fx.clamp01(frac) * w * 8
    for i in range(w):
        fill = int(max(0.0, min(8.0, total - i * 8)))
        if fill > 0:
            cv.put(x + i, y, _HEIGHTHS[fill] if u else ("#" if fill >= 4 else "-"), color)
        elif track is not None:
            cv.put(x + i, y, "·" if u else ".", track)


def resample(vals: Sequence[float], n: int, frac: float) -> List[float]:
    """n samples across per-second vals (oldest first), shifted by frac of a second for smooth scroll."""
    m = len(vals)
    if n <= 0 or m == 0:
        return [0.0] * max(0, n)
    sm = [(vals[max(0, i - 1)] + 2 * vals[i] + vals[min(m - 1, i + 1)]) / 4.0 for i in range(m)]
    step = m / float(n)
    out = []
    for k in range(n):
        a = k * step + frac
        b = a + step
        if step >= 1.0:
            acc = wsum = 0.0
            i = int(a)
            while i < b and i < m:
                ov = min(b, i + 1) - max(a, i)
                if ov > 0:
                    acc += sm[i] * ov
                    wsum += ov
                i += 1
            out.append(acc / wsum if wsum else (sm[-1] if a >= m - 1 else 0.0))
        else:
            c = (a + b) / 2.0 - 0.5
            i0 = int(math.floor(c))
            f = c - i0
            v0 = sm[min(m - 1, max(0, i0))]
            v1 = sm[min(m - 1, max(0, i0 + 1))]
            out.append(v0 + (v1 - v0) * f)
    return out


def _left_column(rects: Dict[str, Tuple[int, int, int, int]], y: int, H: int, lw: int, n_sessions: int,
                 n_providers: int, prov_h: int, sessions_rows: Optional[int]) -> None:
    """SESSIONS on top, PROVIDERS at the bottom, and ROUTES between them when there is room for it."""
    rest = H - prov_h
    sess_h = rest
    if sessions_rows is not None:
        want = max(7, sessions_rows)
        routes_cap = max(11, 3 * max(n_sessions, n_providers, 1) + 5)
        room = rest - want
        if room >= 8:
            routes_h = min(room, routes_cap)
            sess_h = rest - routes_h
            rects["routes"] = (0, y + sess_h, lw, routes_h)
    rects["sessions"] = (0, y, lw, sess_h)
    rects["providers"] = (0, y + rest, lw, prov_h)


def layout(w: int, h: int, n_sessions: int = 1, n_providers: int = 1,
           sessions_rows: Optional[int] = None) -> Dict[str, Tuple[int, int, int, int]]:
    """Panel rectangles (x, y, w, h) for a w x h screen: header rows 0-1, footer the last row.

    sessions_rows is how many rows the SESSIONS panel would like (borders included); when the
    left column has room to spare, a ROUTES panel takes it.
    """
    top, bottom = 2, h - 1
    rects: Dict[str, Tuple[int, int, int, int]] = {}
    if w >= 130 and h >= 32:
        rects["totals"] = (0, top, w, 4)
        y = top + 4
        H = bottom - y
        lw = max(50, min(78, int(w * 0.40)))
        rw = w - lw
        prov_h = max(5, min(n_providers + 3, H // 2))
        _left_column(rects, y, H, lw, n_sessions, n_providers, prov_h, sessions_rows)
        tp_h = max(9, int(H * 0.46))
        lat_w = max(34, int(rw * 0.42))
        rects["throughput"] = (lw, y, rw, tp_h)
        rects["latency"] = (lw, y + tp_h, lat_w, H - tp_h)
        rects["feed"] = (lw + lat_w, y + tp_h, rw - lat_w, H - tp_h)
        return rects
    if w >= 80 and h >= 22:
        rects["totals"] = (0, top, w, 3)
        y = top + 3
        H = bottom - y
        lw = w // 2
        rw = w - lw
        prov_h = max(4, min(n_providers + 2, H // 3))
        _left_column(rects, y, H, lw, n_sessions, n_providers, prov_h, sessions_rows if h >= 34 else None)
        tp_h = max(6, int(H * 0.42))
        rem = H - tp_h
        rects["throughput"] = (lw, y, rw, tp_h)
        if rem >= 9:
            lat_h = min(7, max(4, rem // 2))
            rects["latency"] = (lw, y + tp_h, rw, lat_h)
            rects["feed"] = (lw, y + tp_h + lat_h, rw, rem - lat_h)
        elif rem >= 3:
            rects["feed"] = (lw, y + tp_h, rw, rem)
        return rects
    # Narrow or short: one column, panels in priority order, each at least its minimum height.
    want = [("sessions", 4, 3 + 3 * max(1, n_sessions)), ("throughput", 5, 9), ("totals", 3, 3),
            ("providers", 3, n_providers + 2), ("feed", 3, 8), ("latency", 4, 6)]
    H = bottom - top
    chosen: List[List[Any]] = []
    used = 0
    for name, lo, hi in want:
        if used + lo <= H:
            chosen.append([name, lo, max(lo, hi)])
            used += lo
    spare = H - used
    for item in chosen:
        if spare <= 0:
            break
        add = min(spare, item[2] - item[1])
        item[1] += add
        spare -= add
    if spare > 0 and chosen:
        chosen[0][1] += spare
    y = top
    for name in ("totals", "sessions", "throughput", "providers", "latency", "feed"):
        for item in chosen:
            if item[0] == name:
                rects[name] = (0, y, w, item[1])
                y += item[1]
    return rects


# ---------------------------------------------------------------------------
# The Observatory screen
# ---------------------------------------------------------------------------

PANELS = ("totals", "sessions", "throughput", "routes", "providers", "latency", "feed")
TITLES = {"totals": ("TOTALS", ICE), "sessions": ("SESSIONS", BEAM), "throughput": ("THROUGHPUT", EMBER),
          "routes": ("ROUTES", PLASMA), "providers": ("PROVIDERS", OK), "latency": ("LATENCY", PLASMA),
          "feed": ("FEED", FLARE)}

# Wire directions for the routes panel, and the box-drawing glyph for each combination.
_U, _D, _L, _R = 1, 2, 4, 8
_WIRE = {_L | _R: "─", _U | _D: "│", _D | _R: "╭", _D | _L: "╮", _U | _R: "╰", _U | _L: "╯",
         _U | _D | _R: "├", _U | _D | _L: "┤", _L | _R | _D: "┬", _L | _R | _U: "┴", 15: "┼",
         _L: "─", _R: "─", _U: "│", _D: "│"}


class Observatory:
    """Renders Views of a Reducer to Canvases and handles keys. No I/O: frame() is pure drawing."""

    def __init__(self, red: Reducer, unicode: bool = True, motion: bool = True,
                 haptics: Optional[fx.Haptics] = None, focus: Optional[str] = None, seed: Optional[int] = None) -> None:
        self.red = red
        self.u = unicode
        self.motion = motion
        self.haptics = haptics if haptics is not None else fx.Haptics(mode="off")
        self.rng = random.Random(7 if seed is None else seed)
        self.g = _GU if unicode else _GA
        self.focus: Optional[str] = focus or None
        self.verbose = False
        self.paused = False
        self.paused_at = 0.0
        self.frozen: Optional[View] = None
        self.help = False
        self.quality = 1.0
        self.lite = False               # a slow link or machine: fewer cells change per frame
        self.repaint = False
        self.t0: Optional[float] = None
        self.last_frame = 0.0
        self.disp: Dict[str, Tuple[float, float]] = {}
        self.bumped: Dict[str, float] = {}
        self.prev_vals: Dict[str, float] = {}
        self.scale = 0.0
        self.last_pulse = 0
        self.link_t = -99.0
        self.lost_t = -99.0
        self.prev_state = ""
        self.glitch_until = 0.0
        self.last_glitch = -99.0
        self.glitch_box: Optional[Tuple[int, int, int, int]] = None
        self.card_flash: Dict[str, Tuple[float, Color]] = {}
        self.panel_flash: Dict[str, Tuple[float, Color]] = {}
        self.rects: Dict[str, Tuple[int, int, int, int]] = {}
        self.card_rows: Dict[str, Tuple[int, int, int, int]] = {}
        self.stars: Optional[fx.Stars] = None
        self._flying = False
        self._fresh = False

    # -- text ------------------------------------------------------------------
    def s(self, text: str) -> str:
        return text if self.u else ascii_only(text)

    def put_parts(self, cv: Canvas, x: int, y: int, parts: Sequence[Tuple[str, Color, bool]], limit: int) -> int:
        """Colored runs left to right, cut at x + limit. Returns the next x."""
        end = x + max(0, limit)
        ell = self.g["ell"]
        for text, color, bold in parts:
            if x >= end:
                break
            text = self.s(text)
            room = end - x
            if len(text) > room:
                text = fit(text, room, ell)
            cv.text(x, y, text, color, bold)
            x += len(text)
        return x

    # -- animation helpers -----------------------------------------------------
    def ease(self, key: str, target: float, now: float, speed: float = 9.0) -> float:
        if key in self.prev_vals and abs(self.prev_vals[key] - target) > 1e-9:
            self.bumped[key] = now
        self.prev_vals[key] = target
        if not self.motion or self.paused:
            self.disp[key] = (target, now)
            return target
        cur, at = self.disp.get(key, (target, now))
        dt = max(0.0, min(0.5, now - at))
        cur = cur + (target - cur) * min(1.0, dt * speed)
        if abs(target - cur) <= max(0.5, abs(target) * 0.002):
            cur = target
        self.disp[key] = (cur, now)
        return cur

    def bump(self, key: str, now: float, span: float = 0.7) -> float:
        if not self.motion:
            return 0.0
        at = self.bumped.get(key)
        return max(0.0, 1.0 - (now - at) / span) if at is not None else 0.0

    def power(self, now: float, idx: int) -> float:
        if not self.motion:
            return 1.0
        return fx.clamp01((now - self.link_t - 0.10 - idx * 0.07) / 0.42)

    def spinner(self, now: float, speed: float = 12.0) -> str:
        spin = self.g["spin"]
        return spin[int(now * speed) % len(spin)] if self.motion else self.g["dot"]

    def breathe(self, now: float, rate: float = 2.0, lo: float = 0.55) -> float:
        if not self.motion:
            return 1.0
        return lo + (1 - lo) * (0.5 + 0.5 * math.sin(now * rate))

    # -- keys -------------------------------------------------------------------
    def key(self, k: str, now: float) -> bool:
        """Handle one key token. Returns False to quit."""
        if k == "esc" and self.help:
            self.help = False
            return True
        if k in ("q", "Q", "ctrl-c", "ctrl-d", "esc"):
            return False
        self.haptics.tap()
        if k in ("p", "P", " "):
            self.set_paused(not self.paused, now)
        elif k in ("c", "C"):
            self.red.clear_feed()
            if self.frozen is not None:
                self.frozen.feed = []
        elif k in ("s", "tab", "right", "down"):
            self.cycle(1, now)
        elif k in ("S", "shift-tab", "left", "up"):
            self.cycle(-1, now)
        elif k in ("v", "V"):
            self.verbose = not self.verbose
            self._refreeze(now)
        elif k in ("?", "h", "H"):
            self.help = not self.help
        elif k == "ctrl-l":
            self.repaint = True
        return True

    def set_paused(self, on: bool, now: float) -> None:
        self.paused = on
        if on:
            self.paused_at = now
            self.frozen = self.red.view(now, self.focus, self.verbose)
        else:
            self.frozen = None
            self.last_pulse = self.red.pulses       # do not replay what happened while paused

    def _refreeze(self, now: float) -> None:
        if self.paused:
            self.frozen = self.red.view(self.paused_at, self.focus, self.verbose)

    def cycle(self, step: int, now: float) -> None:
        v = self.frozen if self.paused and self.frozen is not None else self.red.view(now)
        sids = [s["sid"] for s in v.sessions]
        if not sids:
            self.focus = None
            return
        order: List[Optional[str]] = [None] + sids
        try:
            i = order.index(self.focus)
        except ValueError:
            i = 0
        self.focus = order[(i + step) % len(order)]
        self._refreeze(now)

    # -- frame --------------------------------------------------------------------
    def frame(self, w: int, h: int, now: float) -> Canvas:
        w, h = max(1, int(w)), max(1, int(h))
        if self.t0 is None:
            self.t0 = now
        dt = max(0.0, min(0.25, now - self.last_frame)) if self.last_frame else 0.0
        self.last_frame = now
        self.haptics.update(dt)
        self.haptics.offset()               # a dashboard never shakes; drain the queue
        if self.paused and self.frozen is not None:
            v = self.frozen
        else:
            v = self.red.view(now, self.focus, self.verbose)
            self.focus = v.focus
        self._react(v, now)
        self._prune(v, now)
        self._flying = any(s["flights"] for s in v.sessions)
        self._fresh = bool(v.feed) and now - v.feed[-1]["at"] < 1.0
        cv = Canvas(w, h)
        if v.state == "idle":
            self._idle(cv, v, now)
        elif w < 34 or h < 10:
            self._tiny(cv, v, now)
        else:
            self._dashboard(cv, v, now)
        return cv

    def busy(self, now: float) -> bool:
        """True while something on screen moves with the data (worth the full frame rate)."""
        if not self.motion or self.paused or self.prev_state != "live":
            return False
        return (self._flying or self._fresh or now - self.link_t < 1.5 or now < self.glitch_until + 0.2
                or self.haptics.flash > 0.05)

    def _prune(self, v: View, now: float) -> None:
        """Forget per-flight and per-session animation state that no longer has a subject."""
        if now - getattr(self, "_pruned", 0.0) < 5.0:
            return
        self._pruned = now
        live = {"f:" + f["rid"] for s in v.sessions for f in s["flights"]}
        for table in (self.disp, self.bumped, self.prev_vals):
            for k in [k for k in table if k.startswith("f:") and k not in live]:
                del table[k]
        for k in [k for k, (at, _) in self.card_flash.items() if now - at > 5.0]:
            del self.card_flash[k]

    def _react(self, v: View, now: float) -> None:
        if v.state == "live" and self.prev_state != "live":
            self.link_t = now
            if self.prev_state:
                self.haptics.confirm()
        elif v.state != "live" and self.prev_state == "live":
            self.lost_t = now
            self.haptics.pulse(BAD, 0.9)
            self._glitch(None, now, 0.28, force=True)
        self.prev_state = v.state
        if self.paused:
            return
        for n, at, kind, sid in list(self.red.recent):
            if n <= self.last_pulse:
                continue
            self.last_pulse = n
            if now - at > 2.0:
                continue
            if kind == "relay":
                self.haptics.pulse(WARN, 0.65)
                self.card_flash[sid] = (now, WARN)
                self._glitch(self.card_rows.get(sid) or self.rects.get("sessions"), now, 0.16)
            elif kind in ("error", "breaker"):
                self.haptics.pulse(BAD, 0.7)
                self.card_flash[sid] = (now, BAD)
                self.panel_flash["providers"] = (now, BAD)
            elif kind == "loop":
                self.haptics.pulse(FLARE, 0.75)
                self.card_flash[sid] = (now, FLARE)
                self._glitch(self.card_rows.get(sid), now, 0.14)
            elif kind == "heal":
                self.haptics.pulse(BEAM, 0.45)
            elif kind in ("stage", "council"):
                self.haptics.pulse(PLASMA, 0.45)
                self.card_flash[sid] = (now, PLASMA)
            elif kind == "begin":
                self.card_flash[sid] = (now, BEAM)
            elif kind in ("done", "end"):
                self.card_flash[sid] = (now, OK)
            if kind not in ("begin", "end"):
                self.panel_flash["feed"] = (now, KINDS.get(kind, ("", "", "", BEAM))[3])

    def _glitch(self, box: Optional[Tuple[int, int, int, int]], now: float, span: float, force: bool = False) -> None:
        """A short slice-and-split burst. At most one every few seconds: a session that relays on every
        request (a dead first target) must not turn the screen into noise."""
        if self.motion and (force or now - self.last_glitch > 4.0):
            self.glitch_until = now + span
            self.glitch_box = box
            self.last_glitch = now

    def _flash(self, table: Dict[str, Tuple[float, Color]], key: str, now: float, span: float = 0.8
               ) -> Tuple[float, Color]:
        hit = table.get(key)
        if hit is None or not self.motion:
            return 0.0, BEAM
        k = 1.0 - (now - hit[0]) / span
        return (k, hit[1]) if k > 0 else (0.0, hit[1])

    # -- dashboard ------------------------------------------------------------------
    def _dashboard(self, cv: Canvas, v: View, now: float) -> None:
        lw = max(50, min(78, int(cv.w * 0.40))) if cv.w >= 130 else cv.w // 2
        rects = layout(cv.w, cv.h, len(v.sessions), len(v.providers), self._sessions_rows(v, lw - 4))
        self.rects = rects
        self._header(cv, v, now)
        for i, name in enumerate(PANELS):
            r = rects.get(name)
            if r is None:
                continue
            pw = self.power(now, i)
            inner = self._panel(cv, r, name, v, now, pw, i)
            if inner is None or pw < 0.55 or inner[2] < 2 or inner[3] < 1:
                continue
            getattr(self, "_draw_" + name)(cv, inner, v, now)
            if pw < 1.0 and self.u:
                x, y, w, h = r
                fx.scanline(cv, y + pw * h, (x + 1, y + 1, x + w - 1, y + h - 1), BEAM, 1.0 - pw, self.u)
        self._footer(cv, v, now)
        if v.state == "lost":
            self._lost(cv, v, now)
        if self.help:
            self._help(cv)
        k = 1.0 - (now - self.link_t) / 0.7
        if self.motion and k > 0 and self.quality >= 0.15:
            self._rule_flare(cv, k)
        if self.motion and now < self.glitch_until and self.quality >= 0.25:
            box = self.glitch_box or (0, 2, cv.w, cv.h - 1)
            fx.glitch(cv, (box[0], box[1], box[0] + box[2], box[1] + box[3]) if self.glitch_box else box,
                      0.55, self.rng, self.u)

    def _rule_flare(self, cv: Canvas, k: float) -> None:
        """Link established: an anamorphic streak ignites along the header rule (and only there)."""
        cx = cv.w // 2
        span = max(4, cv.w // 2)
        for dx in range(-span, span + 1):
            f = (1 - abs(dx) / (span + 1.0)) ** 2.2 * k
            if f < 0.05:
                continue
            ch = ("━" if f > 0.55 else "─") if self.u else ("=" if f > 0.55 else "-")
            cv.put(cx + dx, 1, ch, fx.lerp(fx.scale(BEAM, 0.35 + 0.65 * f), ICE, f * f), f > 0.6)
        cv.put(cx, 1, self.g["mark"], fx.lerp(BEAM, ICE, min(1.0, k * 1.4)), True)

    def _header(self, cv: Canvas, v: View, now: float) -> None:
        w = cv.w
        g = self.g
        t = now - (self.t0 or now)
        beat = 0.0
        if self.motion and v.state == "live" and self.red.snap_at:
            beat = max(0.0, 1.0 - (now - self.red.snap_at) / 0.6)
        mark_c = fx.lerp(BEAM, ICE, beat) if v.state == "live" else (WARN if v.state == "lost" else DUST)
        cv.put(1, 0, g["mark"], mark_c, True)
        title = "PULSAR OBSERVATORY" if w >= 64 else "PULSAR"
        x = cv.gtext(3, 0, title, SPECTRUM, bold=True,
                     offset=(math.floor(t * 8) / 8.0 * 0.06) if self.motion and not self.lite else 0.0)
        # right: status pill + clock
        if self.paused:
            pill, pc = g["pause"] + " PAUSED", PLASMA
        elif v.state == "live":
            pill, pc = g["dot"] + " LIVE", OK
        else:
            pill, pc = g["ring"] + " RELINKING", WARN
        right = [(pill, pc, True)]
        if w >= 72:
            right.append(("   " + clock(v.now if self.paused else now), DUST, False))
        rw = sum(len(p[0]) for p in right)
        rx = w - 1 - rw
        if rx > x + 2:
            xx = rx
            for i, (text, col, bold) in enumerate(right):
                if i == 0:
                    if v.state == "live" and not self.paused:
                        cv.put(xx, 0, text[0], fx.scale(OK, self.breathe(now, 4.0, 0.45)), True)
                    elif self.motion and int(now * 2.5) % 2:
                        cv.put(xx, 0, text[0], fx.scale(col, 0.4), True)
                    else:
                        cv.put(xx, 0, text[0], col, True)
                    cv.text(xx + 1, 0, text[1:], col, bold)
                else:
                    cv.text(xx, 0, text, col, bold)
                xx += len(text)
        # middle: Core identity
        bits = []
        if v.port:
            bits.append("core :%d" % v.port)
        if v.uptime:
            bits.append("up " + dur(v.uptime))
        if v.core.get("version"):
            bits.append("v" + v.core["version"])
        n_live = v.totals["sessions"]
        bits.append("%d session%s" % (n_live, "" if n_live == 1 else "s"))
        room = rx - x - 6
        while bits and len(self.s(g["sep"].join(bits))) > room:
            bits.pop()
        if bits:
            cv.text(x + 4, 0, self.s(g["sep"].join(bits)), ASH)
        # rule with a traveling beam
        ch = "─" if self.u else "-"
        fk, fc = (self.haptics.flash, self.haptics.flash_color) if self.motion else (0.0, BEAM)
        for i in range(w):
            col = NIGHT
            if fk > 0:
                col = fx.lerp(NIGHT, fc, fk * 0.85)
            cv.put(i, 1, ch, col)
        if self.motion and self.quality >= 0.2:
            span = 18
            p = ((now % 3.6) / 3.6) * (w + span * 2) - span
            for i in range(max(0, int(p) - span), min(w, int(p) + 1)):
                k = 1.0 - (p - i) / span
                if k <= 0:
                    continue
                c = fx.lerp(NIGHT, fx.grad(SPECTRUM, i / float(max(1, w - 1))), k)
                cv.put(i, 1, ("━" if k > 0.55 else "─") if self.u else "=", fx.lerp(c, ICE, k * k * 0.5), k > 0.8)

    def _footer(self, cv: Canvas, v: View, now: float) -> None:
        y = cv.h - 1
        hints = [("q", "quit"), ("p", "resume" if self.paused else "pause"), ("c", "clear"), ("s", "focus"),
                 ("v", "events only" if self.verbose else "all requests"), ("?", "help")]
        right_parts: List[Tuple[str, Color, bool]] = []
        if self.focus:
            right_parts = [("focus ", DUST, False), (self._focus_name(v), hue(self.focus), True)]
        elif v.sessions:
            right_parts = [("all sessions", DUST, False)]
        rw = sum(len(p[0]) for p in right_parts)
        x = 1
        limit = cv.w - (rw + 3 if rw else 1)
        for i, (k, label) in enumerate(hints):
            need = (3 if i else 0) + len(k) + 1 + len(label)
            if x + need > limit:
                break
            if i:
                x = cv.text(x, y, self.s(" · ") if self.u else " | ", NIGHT)
            x = cv.text(x, y, k, BEAM, True)
            x = cv.text(x + 1, y, label, DUST)
        if rw and cv.w - rw - 1 > x + 2:
            self.put_parts(cv, cv.w - rw - 1, y, right_parts, rw)

    def _panel(self, cv: Canvas, r: Tuple[int, int, int, int], name: str, v: View, now: float, pw: float,
               idx: int) -> Optional[Tuple[int, int, int, int]]:
        x, y, w, h = r
        if w < 6 or h < 3 or pw <= 0:
            return None
        title, accent = TITLES[name]
        fk, fc = self._flash(self.panel_flash, name, now, 0.9)
        focus_panel = name == "sessions" and bool(self.focus)
        if self.motion:
            # The gradient drifts around the frame in quarter-second steps (every step repaints the border,
            # so a smooth per-frame drift would cost bandwidth over SSH for no visible gain); lite links hold it.
            tq = 0.0 if self.lite else math.floor(now * 4.0) / 4.0
            phase = (tq * 0.035 + idx * 0.17) % 1.0
            bright = 0.30 + (0.18 if focus_panel else 0.0) + 0.5 * fk
            cv.gbox(x, y, w, h, SPECTRUM, phase, self.u, bright=min(1.0, bright), draw=pw)
            if fk > 0:
                for i in range(1, w - 1):
                    ch, col, b = cv.get(x + i, y)
                    cv.put(x + i, y, ch, fx.lerp(col or NIGHT, fc, fk * 0.6), b)
        else:
            cv.box(x, y, w, h, fx.lerp(NIGHT, DUST, 0.7), unicode=self.u)
        if pw < 0.3:
            return None
        label = title
        if w - 6 >= len(label) + 2:
            cv.put(x + 2, y, " ", None)
            cv.put(x + 3, y, self.g["diamond"], accent, True)
            cv.text(x + 4, y, " " + label + " ", ICE, True)
            after = x + 4 + len(label) + 2
            meta = self._meta(name, v)
            room = x + w - 3 - after - 1
            if meta and room >= 6:
                m = fit(self.s(meta), room - 2, self.g["ell"])
                cv.text(x + w - 3 - len(m) - 1, y, " " + m + " ", DUST)
        return (x + 2, y + 1, w - 4, h - 2)

    def _focus_name(self, v: View) -> str:
        """The focused session as people know it: its label (the project folder), else its sid."""
        sid = v.focus or self.focus or ""
        for s in v.sessions:
            if s["sid"] == sid:
                return clean(s.get("label") or "", 40) or sid
        return sid

    def _meta(self, name: str, v: View) -> str:
        if name == "sessions":
            flying = sum(len(s["flights"]) for s in v.sessions)
            return "%d live%s" % (len(v.sessions), (" · %d in flight" % flying) if flying else "")
        if name == "providers":
            # Every open breaker counts, a degraded provider's (one target down among working ones) too.
            opened = sum(len(p["breakers"]) for p in v.providers)
            return "%d%s" % (len(v.providers), (" · %d breaker%s open" % (opened, "" if opened == 1 else "s"))
                             if opened else "")
        if name == "throughput":
            who = self._focus_name(v) if v.focus else "all sessions"
            return "tok/s · %s · last %dm" % (who, max(1, v.history // 60))
        if name == "latency":
            return "time to first token"
        if name == "routes":
            flying = sum(len(s["flights"]) for s in v.sessions)
            return ("%d packet%s" % (flying, "" if flying == 1 else "s")) if flying else "sessions · core · providers"
        if name == "feed":
            if self.paused:
                return "paused"
            return ("every request" if self.verbose else "events") + (" · " + self._focus_name(v) if v.focus else "")
        if name == "totals":
            return "core-wide"
        return ""

    # -- panels -----------------------------------------------------------------------
    def _draw_totals(self, cv: Canvas, r: Tuple[int, int, int, int], v: View, now: float) -> None:
        x, y, w, h = r
        T = v.totals
        g = self.g
        tiles = [
            ("TOK/S", rate(self.ease("tps", v.tps_now, now, 5.0)), OK if v.tps_now > 0.05 else DUST, "tps"),
            ("REQUESTS", human(self.ease("req", T["requests"], now)), ICE, "req"),
            ("IN FLIGHT", "%d" % T["in_flight"], PLASMA if T["in_flight"] else DUST, "inflight"),
            ("TOKENS IN", human(self.ease("tin", T["tokens_in"], now)), ICE, "tin"),
            ("TOKENS OUT", human(self.ease("tout", T["tokens_out"], now)), ICE, "tout"),
            ("CACHE READ", human(self.ease("cache", T["cache_read"], now)), BEAM if T["cache_read"] else DUST, "cache"),
            ("EST. COST", money(T["cost"]), EMBER if T["cost"] else DUST, "cost"),
            ("RELAYS", "%d" % T["relays"], WARN if T["relays"] else DUST, "relays"),
            ("HEALS", "%d" % T["heals"], BEAM if T["heals"] else DUST, "heals"),
            ("LOOPS", "%d" % T["loops"], FLARE if T["loops"] else DUST, "loops"),
            ("ERRORS", "%d" % T["errors"], BAD if T["errors"] else DUST, "errors"),
            ("UPTIME", dur(v.uptime), ASH, ""),
        ]
        for key, val in (("inflight", T["in_flight"]), ("cost", T["cost"]), ("relays", T["relays"]),
                         ("heals", T["heals"]), ("loops", T["loops"]), ("errors", T["errors"])):
            self.ease(key, float(val), now)         # registers a bump when the value changes
        if h >= 2:
            widths = [max(len(t[0]), 7) for t in tiles]
            n = 0
            used = 0
            for wd in widths:
                if used + wd + (3 if n else 0) > w:
                    break
                used += wd + (3 if n else 0)
                n += 1
            if n == 0:
                return
            extra = (w - used) // max(1, n) if n > 1 else 0
            xx = x
            for i in range(n):
                label, value, col, key = tiles[i]
                b = self.bump(key, now) if key else 0.0
                if i:
                    cv.put(xx + 1 + extra // 2, y, g["vbar"], NIGHT)
                    cv.put(xx + 1 + extra // 2, y + 1, g["vbar"], NIGHT)
                    xx += 3 + extra
                cv.text(xx, y, label, DUST)
                cv.text(xx, y + 1, fit(value, widths[i], g["ell"]), fx.lerp(col, ICE, b * 0.8), True)
                xx += widths[i]
            return
        # one row: LABEL value pairs
        xx = x
        for i, (label, value, col, key) in enumerate(tiles):
            piece = len(label) + 1 + len(value) + (3 if i else 0)
            if xx + piece > x + w:
                break
            if i:
                xx = cv.text(xx, y, " " + g["vbar"] + " ", NIGHT)
            xx = cv.text(xx, y, label + " ", DUST)
            b = self.bump(key, now) if key else 0.0
            xx = cv.text(xx, y, value, fx.lerp(col, ICE, b * 0.8), True)

    def _card_rows(self, s: Dict[str, Any], level: int, w: int) -> List[str]:
        """Row kinds of a full card at a detail level (0 = essentials only)."""
        rows = ["head", "chain"]
        if s["flights"]:
            rows += ["flight"] * min(3, len(s["flights"]))
            if len(s["flights"]) > 3:
                rows.append("more")
        else:
            rows.append("idle")
        if level >= 1 and s["relay"] and w >= 30:
            rows.append("relay")
        if level >= 2 and w >= 30:
            rows.append("spark")
        if level >= 3 and w >= 30 and s["agg"].get("requests"):
            rows.append("stats")
        if level >= 4 and w >= 44 and s["recent"]:
            rows += ["recent"] * min(3, len(s["recent"]))
        return rows

    def _draw_sessions(self, cv: Canvas, r: Tuple[int, int, int, int], v: View, now: float) -> None:
        x, y, w, h = r
        self.card_rows = {}
        S = v.sessions
        if not S:
            self._no_sessions(cv, r, now)
            return
        order = sorted(S, key=lambda s: s["sid"] != self.focus) if self.focus else list(S)
        plan: Optional[List[Tuple[Dict[str, Any], List[str]]]] = None
        gap = 1
        # Most detail first; separating blank rows go before the essentials do.
        for level, gap in ((4, 1), (3, 1), (2, 1), (1, 1), (1, 0), (0, 1), (0, 0)):
            cards = [(s, self._card_rows(s, level, w)) for s in order]
            if sum(len(rows) for _, rows in cards) + gap * (len(cards) - 1) <= h:
                plan = cards
                break
        if plan is None:
            # the focused (or first) card in full, the rest one row each
            first = self._card_rows(order[0], 0, w)
            if len(first) + len(order) - 1 <= h:
                plan = [(order[0], first)] + [(s, ["compact"]) for s in order[1:]]
            else:
                plan = [(s, ["compact"]) for s in order]
            gap = 0
        yy = y
        end = y + h
        for i, (s, rows) in enumerate(plan):
            left = end - yy
            if left <= 0:
                break
            if rows == ["compact"]:
                if i < len(plan) - 1 and left == 1:
                    cv.text(x, yy, fit("+%d more" % (len(plan) - i), w, self.g["ell"]), DUST)
                    break
                self._card_compact(cv, x, yy, w, s, now)
                self.card_rows[s["sid"]] = (x, yy, w, 1)
                yy += 1
                continue
            rows = rows[:left]
            self._card(cv, x, yy, w, rows, s, now)
            self.card_rows[s["sid"]] = (x, yy, w, len(rows))
            yy += len(rows) + gap

    def _accent(self, cv: Canvas, x: int, y: int, rows: int, s: Dict[str, Any], now: float) -> None:
        focused = s["sid"] == self.focus
        fk, fc = self._flash(self.card_flash, s["sid"], now, 0.9)
        for j in range(rows):
            if focused and self.motion:
                col = fx.grad(SPECTRUM, fx.tri(now * 0.5 + j * 0.25))
            else:
                col = fx.scale(s["hue"], 0.85 if focused else 0.5)
            if fk > 0:
                col = fx.lerp(col, fc, fk)
            cv.put(x, y + j, self.g["bar"], col, focused)

    def _card(self, cv: Canvas, x: int, y: int, w: int, rows: List[str], s: Dict[str, Any], now: float) -> None:
        g = self.g
        self._accent(cv, x, y, len(rows), s, now)
        cx, cw = x + 2, w - 2
        agg = s["agg"]
        flights = list(s["flights"][:3])
        recent = list(s["recent"])
        for j, kind in enumerate(rows):
            yy = y + j
            if kind == "head":
                self._card_head(cv, cx, yy, cw, s, now)
            elif kind == "chain":
                self._chain(cv, cx, yy, cw, s, now)
            elif kind == "flight" and flights:
                self._flight(cv, cx, yy, cw, flights.pop(0), now)
            elif kind == "more":
                cv.text(cx + 2, yy, "+%d more in flight" % (len(s["flights"]) - 3), DUST)
            elif kind == "idle":
                self._idle_line(cv, cx, yy, cw, s, now)
            elif kind == "relay" and s["relay"]:
                rl = s["relay"]
                age = now - rl["at"]
                col = fx.lerp(WARN, DUST, fx.clamp01(age / 90.0))
                parts = [(g["relay"] + " relayed " + ago(age) + "  ", col, False),
                         (short_model(rl["from"], 24), ASH, False), (g["to"], DUST, False),
                         (short_model(rl["to"], 24), ICE, False)]
                if rl["reason"]:
                    parts.append(("  " + rl["reason"], col, False))
                self.put_parts(cv, cx + 2, yy, parts, cw - 2)
            elif kind == "spark":
                self._spark(cv, cx + 2, yy, cw - 2, s, now)
            elif kind == "recent" and recent:
                self._recent_line(cv, cx + 2, yy, cw - 2, recent.pop(0), now)
            elif kind == "stats":
                parts = [("in ", DUST, False), (human(agg.get("tokens_in", 0)), ASH, False),
                         ("  out ", DUST, False), (human(agg.get("tokens_out", 0)), ASH, False)]
                if agg.get("cache_read"):
                    parts += [("  cache ", DUST, False), (human(agg["cache_read"]), BEAM, False)]
                if agg.get("cost"):
                    parts += [("  ", DUST, False), (money(agg["cost"]), EMBER, False)]
                if agg.get("ttft_ewma"):
                    parts += [("  ttft ", DUST, False), (dur(agg["ttft_ewma"]), speed_color(agg["ttft_ewma"]), False)]
                self.put_parts(cv, cx + 2, yy, parts, cw - 2)

    def _recent_line(self, cv: Canvas, x: int, y: int, w: int, e: Dict[str, Any], now: float) -> None:
        g = self.g
        if e["ok"]:
            parts = [(g["ok"], fx.scale(OK, 0.8), False), ("  " + clock(e["at"]), NIGHT, False),
                     ("  " + short_model(e["model"], 22), DUST, False), ("  %s tok" % human(e["tokens_out"]), ASH, False),
                     ("  " + dur(e["dur"]), DUST, False)]
            if e["ttft"]:
                parts.append(("  ttft " + dur(e["ttft"]), fx.scale(speed_color(e["ttft"]), 0.8), False))
        else:
            col = DUST if e["kind"] == "cancel" else fx.scale(BAD, 0.85)
            parts = [(g["bad"] if e["kind"] != "cancel" else g["odot"], col, False), ("  " + clock(e["at"]), NIGHT, False),
                     ("  " + short_model(e["model"], 22), DUST, False),
                     ("  " + (e["error"] or e["kind"]), col, False)]
        self.put_parts(cv, x, y, parts, w)

    def _card_head(self, cv: Canvas, cx: int, y: int, cw: int, s: Dict[str, Any], now: float) -> None:
        g = self.g
        agg = s["agg"]
        flying = bool(s["flights"])
        dot_c = fx.lerp(s["hue"], ICE, 0.5 * self.breathe(now, 7.0, 0.0)) if flying else DUST
        cv.put(cx, y, g["dot"] if flying else g["odot"], dot_c, True)
        name = s["label"] or s["sid"]
        # Counters on the right, dropped least-important first so the name always reads in full.
        req = [("%d req" % agg["requests"], ASH, False)] if agg.get("requests") else []
        tok = [(g["sep"] + human(agg.get("tokens_in", 0) + agg.get("tokens_out", 0)) + " tok", DUST, False)] \
            if agg.get("requests") else []
        rel = [("  " + g["relay"] + "%d" % agg["relays"], WARN, True)] if agg.get("relays") else []
        err = [("  " + g["bad"] + "%d" % agg["errors"], BAD, True)] if agg.get("errors") else []
        nlen = len(self.s(name))
        right: List[Tuple[str, Color, bool]] = []
        for option in (req + tok + rel + err, req + rel + err, rel + err, err, []):
            rw = sum(len(self.s(p[0])) for p in option)
            if 2 + min(nlen, 24) + 2 + rw <= cw:
                right = option
                break
        rw = sum(len(self.s(p[0])) for p in right)
        name_w = min(nlen, max(6, cw - 2 - rw - 2))
        nx = cv.text(cx + 2, y, fit(self.s(name), name_w, g["ell"]), ICE, True)
        cwd = short_path(s["cwd"])
        room = cx + cw - rw - 2 - (nx + 2)
        if cwd and room >= 6:
            cv.text(nx + 2, y, fit_left(self.s(cwd), room, g["ell"]), DUST)
        if rw:
            self.put_parts(cv, cx + cw - rw, y, right, rw)

    def _spark(self, cv: Canvas, x: int, y: int, w: int, s: Dict[str, Any], now: float) -> None:
        """One-row braille sparkline of this session's tokens/s over the last minute."""
        hist = s["hist"]
        label = rate(s["tps"]) + " tok/s"
        lw = len(label) + 6
        sw = max(0, min(30, w - lw))
        if sw < 6:
            return
        peak = max(hist) if hist else 0.0
        if peak <= 0:
            for i in range(sw):
                cv.put(x + i, y, "⣀" if self.u else "_", fx.scale(s["hue"], 0.3))
        else:
            n = sw * (2 if self.u else 1)
            frac = (now - int(now)) if self.motion and not self.paused else 0.0
            samples = [min(1.0, val / (peak * 1.05)) for val in resample(hist, n, frac)]
            cols = [fx.lerp(fx.scale(s["hue"], 0.55), s["hue"], i / float(max(1, sw - 1))) for i in range(sw)]
            area_chart(cv, x, y, sw, 1, samples, self.u, colors=cols, fade=False)
        cv.text(x + sw + 1, y, "60s", NIGHT)
        cv.text(x + sw + 5, y, label, OK if s["tps"] > 0.05 else DUST, s["tps"] > 0.05)

    def _chain(self, cv: Canvas, x: int, y: int, w: int, s: Dict[str, Any], now: float) -> None:
        g = self.g
        plan = s["plan"]
        if not plan:
            model = s["agg"].get("last_model") or (s["flights"][0]["model"] if s["flights"] else "")
            self.put_parts(cv, x + 2, y, [("model  ", DUST, False), (short_model(model, 40) or "unknown", ASH, False)],
                           w - 2)
            return
        parts: List[Tuple[str, Color, bool]] = [("route  " if w >= 40 else "", DUST, False)]
        budget = w - 2 - len(parts[0][0])
        names = [short_model(t["model"], 26 if len(plan) <= 2 else 18) for t in plan]
        # drop fallbacks from the tail until the chain fits
        shown = len(plan)
        while shown > 1 and sum(len(n) + 3 for n in names[:shown]) + (6 if shown < len(plan) else 0) > budget:
            shown -= 1
        for i, t in enumerate(plan[:shown]):
            if i:
                parts.append((g["chain"], DUST, False))
            n = names[i]
            if t["state"] == "live":
                k = self.breathe(now, 6.0, 0.6)
                parts.append(((">" if not self.u else "▸") + n, fx.lerp(OK, ICE, 1 - k), True))
            elif t["state"] == "open":
                parts.append((g["bad"] + n, BAD, False))
            else:
                parts.append((n, ASH if i == 0 else DUST, i == 0))
        if shown < len(plan):
            parts.append((g["chain"] + "+%d" % (len(plan) - shown), DUST, False))
        self.put_parts(cv, x + 2, y, parts, w - 2)

    def _flight(self, cv: Canvas, x: int, y: int, w: int, f: Dict[str, Any], now: float) -> None:
        g = self.g
        el = f["elapsed"]
        if f["waiting"]:
            label, lc = "WAIT  ", PLASMA
            text = [("first token " + dur(el), PLASMA, False)]
        else:
            label, lc = "STREAM", OK
            tps = self.ease("f:" + f["rid"], f["tps"], now, 6.0)
            text = [(rate(tps) + " tok/s", OK, True), (g["sep"] + human(f["tokens"]) + " tok", ASH, False),
                    (g["sep"] + dur(el), DUST, False)]
        if f["relays"]:
            text.append(("  " + g["relay"] + "%d" % f["relays"], WARN, True))
        tw = sum(len(self.s(p[0])) for p in text)
        cv.put(x, y, self.spinner(now + hash(f["rid"]) % 7) if self.motion else g["dot"], lc, True)
        lx = x + 2
        if w >= 34:
            cv.text(lx, y, label, lc, True)
            lx += len(label) + 1
        bar_w = min(32, w - (lx - x) - tw - 2)
        if bar_w >= 5:
            if f["waiting"]:
                self._scanner(cv, lx, y, bar_w, now - f["born"])
            else:
                frac = math.log1p(f["tokens"]) / math.log1p(8192.0)
                fx.bar(cv, lx, y, bar_w, frac, t=now - f["born"], unicode=self.u, shimmer=self.motion)
            lx += bar_w + 2
        self.put_parts(cv, lx, y, text, x + w - lx)

    def _scanner(self, cv: Canvas, x: int, y: int, w: int, t: float) -> None:
        """A light bouncing along a track: the model is thinking."""
        track = "─" if self.u else "-"
        for i in range(w):
            cv.put(x + i, y, track, NIGHT)
        if not self.motion:
            cv.put(x, y, "━" if self.u else "=", PLASMA)
            return
        p = (math.sin(t * 2.6) * 0.5 + 0.5) * (w - 1)
        for i in range(w):
            d = abs(i - p)
            if d < 4:
                k = 1 - d / 4.0
                cv.put(x + i, y, ("━" if k > 0.4 else "─") if self.u else "=", fx.lerp(fx.scale(PLASMA, 0.5), ICE, k * k),
                       k > 0.75)

    def _idle_line(self, cv: Canvas, x: int, y: int, w: int, s: Dict[str, Any], now: float) -> None:
        g = self.g
        last = s["last"]
        if last is None:
            agg = s["agg"]
            if agg.get("requests") and agg.get("last_at"):     # answered before we started watching
                parts = [(g["odot"] + " idle", DUST, False), (g["sep"] + "last answer " + ago(now - agg["last_at"]), DUST,
                                                               False)]
                if agg.get("last_model"):
                    parts.append((g["sep"] + short_model(agg["last_model"], 22), ASH, False))
                self.put_parts(cv, x, y, parts, w)
                return
            self.put_parts(cv, x, y, [(g["odot"] + " idle", DUST, False), (g["sep"] + "no requests yet", NIGHT, False)],
                           w)
            return
        age = ago(now - last["at"])
        if last["ok"]:
            parts = [(g["ok"] + " idle", DUST, False), (g["sep"] + "last answer " + age, DUST, False),
                     (g["sep"] + short_model(last["model"], 22), ASH, False)]
            if last["dur"]:
                parts.append((g["sep"] + dur(last["dur"]), DUST, False))
        elif last["kind"] == "cancel":
            parts = [(g["odot"] + " idle", DUST, False), (g["sep"] + "last request cancelled " + age, DUST, False)]
        else:
            parts = [(g["bad"] + " failed " + age, BAD, True), ("  " + (last["error"] or last["kind"]), fx.scale(BAD, 0.8),
                                                                False)]
        self.put_parts(cv, x, y, parts, w)

    def _card_compact(self, cv: Canvas, x: int, y: int, w: int, s: Dict[str, Any], now: float) -> None:
        g = self.g
        self._accent(cv, x, y, 1, s, now)
        flying = s["flights"]
        dot_c = fx.lerp(s["hue"], ICE, 0.5 * self.breathe(now, 7.0, 0.0)) if flying else DUST
        parts: List[Tuple[str, Color, bool]] = [(g["dot"] if flying else g["odot"], dot_c, True),
                                                (" " + (s["label"] or s["sid"]), ICE, True)]
        model = flying[0]["model"] if flying else (s["plan"][0]["model"] if s["plan"] else s["agg"].get("last_model", ""))
        parts.append(("  " + short_model(model, 22), ASH, False))
        if flying:
            f = flying[0]
            if f["waiting"]:
                parts.append(("  " + self.spinner(now) + " first token " + dur(f["elapsed"]), PLASMA, False))
            else:
                parts.append(("  " + self.spinner(now) + " " + rate(f["tps"]) + " tok/s", OK, True))
        elif s["last"] or s["agg"].get("last_at"):
            at = s["last"]["at"] if s["last"] else s["agg"]["last_at"]
            parts.append(("  idle " + ago(now - at), DUST, False))
        self.put_parts(cv, x + 2, y, parts, w - 2)

    def _no_sessions(self, cv: Canvas, r: Tuple[int, int, int, int], now: float) -> None:
        x, y, w, h = r
        lines = [("no live sessions", ASH, True), ("run pulsarcode in any project", DUST, False)]
        rad = min((h - 4) // 2, (w - 4) // 4, 4)
        cy = y + h // 2 - (1 if rad >= 2 else 0)
        if rad >= 2 and self.u:
            cx = x + w // 2
            cy = y + rad
            if self.motion:
                fx.sweep(cv, cx, cy, rad, now - (self.t0 or now), True, PLASMA)
            else:
                cv.put(cx, cy, self.g["mark"], PLASMA, True)
            cy = y + 2 * rad + 2
        for i, (text, col, bold) in enumerate(lines):
            yy = cy + i
            if yy < y + h:
                t = fit(text, w, self.g["ell"])
                cv.text(x + (w - len(t)) // 2, yy, t, col, bold)

    def _sessions_rows(self, v: View, w: int) -> int:
        """Rows the SESSIONS panel wants at full detail (borders included)."""
        if not v.sessions:
            return 9
        return sum(len(self._card_rows(s, 4, w)) for s in v.sessions) + len(v.sessions) - 1 + 2

    def _draw_routes(self, cv: Canvas, r: Tuple[int, int, int, int], v: View, now: float) -> None:
        """Topology: sessions on the left, Core in the middle, providers on the right, packets on the wires.

        A request waiting for its first token travels out (session to provider); a streaming answer
        flows back, faster the more tokens per second. A relay bounces a packet off the failing provider.
        """
        x, y, w, h = r
        g = self.g
        S = v.sessions[:h]
        P = v.providers[:h]
        if not S and not P:
            msg = fit("no traffic yet", w, self.g["ell"])
            cv.text(x + (w - len(msg)) // 2, y + h // 2, msg, DUST)
            return
        names_s = [fit(self.s(s["label"] or s["sid"][:7]), 14, g["ell"]) for s in S]
        names_p = [fit(self.s(p["name"]), 16, g["ell"]) for p in P]
        lw = max([len(n) for n in names_s] + [4]) + 2
        rw = max([len(n) for n in names_p] + [4]) + 2
        if w - lw - rw < 18:
            return
        cx, cy = x + lw + (w - lw - rw) // 2, y + h // 2
        left0, right1 = x + lw + 1, x + w - rw - 2
        bl = left0 + max(1, (cx - 2 - left0) // 2)
        br = cx + 2 + max(1, (right1 - cx - 2) // 2)

        def spread(n: int) -> List[int]:
            return [y + min(h - 1, int((i + 0.5) * h / float(n))) for i in range(n)]
        ys, yp = spread(len(S)) if S else [], spread(len(P)) if P else []
        grid: Dict[Tuple[int, int], int] = {}

        def hseg(x0: int, x1: int, yy: int) -> None:
            for xx in range(x0, x1 + 1):
                grid[(xx, yy)] = grid.get((xx, yy), 0) | (_L if xx > x0 else 0) | (_R if xx < x1 else 0)

        def vseg(xx: int, y0: int, y1: int) -> None:
            for yy in range(y0, y1 + 1):
                grid[(xx, yy)] = grid.get((xx, yy), 0) | (_U if yy > y0 else 0) | (_D if yy < y1 else 0)
        for yy in ys:
            hseg(left0, bl, yy)
        if ys:
            vseg(bl, min(ys + [cy]), max(ys + [cy]))
            hseg(bl, cx - 2, cy)
        if yp:
            hseg(cx + 2, br, cy)
            vseg(br, min(yp + [cy]), max(yp + [cy]))
        for yy in yp:
            hseg(br, right1, yy)
        open_rows = {yp[j] for j, p in enumerate(P) if p["health"] == "open"}
        for (xx, yy), bits in grid.items():
            col = NIGHT
            if yy in open_rows and xx > br:
                col = fx.scale(BAD, 0.45)
            cv.put(xx, yy, _WIRE.get(bits, "─") if self.u else ("+" if bits & (_U | _D) and bits & (_L | _R)
                                                                  else "|" if bits & (_U | _D) else "-"), col)

        def path(i: int, j: int) -> List[Tuple[int, int]]:
            pts = [(xx, ys[i]) for xx in range(left0, bl + 1)]
            st = 1 if cy >= ys[i] else -1
            pts += [(bl, yy) for yy in range(ys[i] + st, cy + st, st)] if ys[i] != cy else []
            pts += [(xx, cy) for xx in range(bl + 1, cx - 1)] + [(cx, cy)] + [(xx, cy) for xx in range(cx + 2, br + 1)]
            st = 1 if yp[j] >= cy else -1
            pts += [(br, yy) for yy in range(cy + st, yp[j] + st, st)] if yp[j] != cy else []
            pts += [(xx, yp[j]) for xx in range(br + 1, right1 + 1)]
            return pts

        pidx = {p["id"]: j for j, p in enumerate(P)}
        head = g["dot"] if self.u else "o"
        for i, s in enumerate(S):
            for f in s["flights"]:
                j = pidx.get(f["provider"])
                if j is None:
                    continue
                pts = path(i, j)
                n = len(pts)
                if n < 4:
                    continue
                tint = fx.scale(s["hue"], 0.42)
                for (xx, yy) in pts:
                    if (xx, yy) != (cx, cy):
                        ch, _, b = cv.get(xx, yy)
                        cv.put(xx, yy, ch, tint, b)
                if f["waiting"]:
                    color, speed, count, back = PLASMA, 0.45, 2, False
                else:
                    color, speed, count, back = fx.lerp(s["hue"], ICE, 0.35), 0.55 + min(2.0, f["tps"] / 60.0), 3, True
                base = (now - f["born"]) * speed if self.motion else 0.35
                for k in range(count):
                    ph = (base + k / float(count)) % 1.0
                    pos = int(ph * (n - 1))
                    if back:
                        pos = n - 1 - pos
                    for tlen in range(1, 4):
                        tp = pos - tlen if not back else pos + tlen
                        if 0 <= tp < n and pts[tp] != (cx, cy):
                            ch, _, b = cv.get(*pts[tp])
                            cv.put(pts[tp][0], pts[tp][1], ch, fx.lerp(color, tint, tlen / 4.0), False)
                    if pts[pos] != (cx, cy):
                        cv.put(pts[pos][0], pts[pos][1], head, color, True)
            rl = s["relay"]
            if rl and self.motion and S and P and now - rl["at"] < 1.4:
                j = pidx.get(rl.get("from_provider", ""))
                if j is not None:
                    pts = path(i, j)
                    age = now - rl["at"]
                    core_at = pts.index((cx, cy)) if (cx, cy) in pts else 0
                    seg = pts[core_at + 2:]
                    if age < 0.7 and seg:
                        pos = int(age / 0.7 * (len(seg) - 1))
                        cv.put(seg[pos][0], seg[pos][1], head, WARN, True)
                    elif seg:
                        on = int(now * 10) % 2 == 0
                        cv.put(seg[-1][0], seg[-1][1], g["bad"], BAD if on else WARN, True)
        # nodes
        for i, s in enumerate(S):
            flying = bool(s["flights"])
            dot_c = fx.lerp(s["hue"], ICE, 0.5 * self.breathe(now, 7.0, 0.0)) if flying else fx.scale(s["hue"], 0.55)
            nx = x + lw - 1 - len(names_s[i]) - 2
            cv.put(nx, ys[i], g["dot"], dot_c, True)
            cv.text(nx + 2, ys[i], names_s[i], ICE if flying or s["sid"] == self.focus else ASH,
                    s["sid"] == self.focus)
        for j, p in enumerate(P):
            px = right1 + 2
            health = p["health"]
            cv.put(px, yp[j], g["dot"], self._health_color(health, now), True)
            cv.text(px + 2, yp[j], names_p[j], p["color"] if health != "idle" else DUST, health == "busy")
        # the core
        beat = max(0.0, 1.0 - (now - self.red.snap_at) / 0.6) if self.motion and self.red.snap_at else 0.0
        busy = any(s["flights"] for s in S)
        core_c = fx.lerp(BEAM if busy else fx.scale(BEAM, 0.7), ICE, beat)
        cv.put(cx - 1, cy, " ")
        cv.put(cx + 1, cy, " ")
        cv.put(cx, cy, g["mark"], core_c, True)
        if h >= 3 and cy + 1 < y + h:
            label = "CORE" + (" :%d" % v.port if v.port and w >= 60 else "")
            cv.text(cx - len(label) // 2, cy + 1, label, DUST)

    def _health_color(self, health: str, now: float) -> Color:
        if health == "open":
            return BAD if not self.motion or int(now * 2.5) % 2 == 0 else fx.scale(BAD, 0.35)
        if health == "degraded":
            return fx.scale(WARN, self.breathe(now, 3.0, 0.5))
        if health == "busy":
            return fx.lerp(OK, ICE, 0.4 * self.breathe(now, 9.0, 0.0))
        if health == "ok":
            return fx.scale(OK, self.breathe(now, 1.6, 0.6))
        return DUST

    def _providers_inline(self, cv: Canvas, r: Tuple[int, int, int, int], P: List[Dict[str, Any]], now: float) -> None:
        """Too short for a row each: health dots and names flowed across the rows there are."""
        x, y, w, h = r
        xx, yy = x, y
        for i, p in enumerate(P):
            label = self.s(p["name"]) + (" " + dur(p["breakers"][0]["left"]) if p["breakers"] else "")
            need = 2 + len(label) + 2
            if xx + need > x + w:
                xx, yy = x, yy + 1
            if yy >= y + h or (yy == y + h - 1 and i < len(P) - 1 and xx + need + 8 > x + w):
                if yy < y + h:
                    cv.text(xx, yy, "+%d" % (len(P) - i), DUST)
                return
            cv.put(xx, yy, self.g["dot"], self._health_color(p["health"], now), True)
            cv.text(xx + 2, yy, fit(label, x + w - xx - 2, self.g["ell"]),
                    BAD if p["health"] == "open" else WARN if p["breakers"] else
                    (p["color"] if p["health"] != "idle" else DUST))
            xx += need

    @staticmethod
    def _provider_rows(P: List[Dict[str, Any]], extra: int
                       ) -> List[Tuple[str, Dict[str, Any], Optional[Dict[str, Any]], int]]:
        """The Providers panel rows: ("p", provider, None, more) and ("b", provider, breaker, more).

        An open provider names its first breaker on its own row, so a spare row would only repeat it: spare
        rows go to the breakers no main row names (an open provider's second and later ones, every breaker of
        a degraded provider, whose main row keeps its stats). They are dealt one round at a time in display
        order, so one provider cannot take them all. more counts the breakers left unnamed, shown as +N on
        the last row that names one.
        """
        want = [p["breakers"][1:] if p["health"] == "open" else p["breakers"] for p in P]
        subs: List[List[Dict[str, Any]]] = [[] for _ in P]
        depth = 0
        while extra > 0 and any(len(wb) > depth for wb in want):
            for i, wb in enumerate(want):
                if extra > 0 and len(wb) > depth:
                    subs[i].append(wb[depth])
                    extra -= 1
            depth += 1
        rows: List[Tuple[str, Dict[str, Any], Optional[Dict[str, Any]], int]] = []
        for p, shown in zip(P, subs):
            on_main = 1 if p["breakers"] and (p["health"] == "open" or not shown) else 0
            more = max(0, len(p["breakers"]) - on_main - len(shown))
            rows.append(("p", p, None, 0 if shown else more))
            rows.extend(("b", p, b, more if j == len(shown) - 1 else 0) for j, b in enumerate(shown))
        return rows

    def _model_label(self, model: str, n: int, cut: bool = False) -> str:
        """The model id in n cells: whole, else without its vendor prefix, else "" (or, when cut, cut to n
        cells if that still leaves a few of its own)."""
        whole = self.s(model or "default")
        if len(whole) <= n:
            return whole
        bare = self.s(short_model(model, 0))
        if len(bare) <= n:
            return bare
        return fit(bare, n, self.g["ell"]) if cut and n >= 4 else ""

    def _breaker_subrow(self, cv: Canvas, x: int, y: int, w: int, b: Dict[str, Any], more: int) -> None:
        """One open breaker under its provider: model (+N more), OPEN countdown, then its fails if they fit."""
        g = self.g
        elbow = g["elbow"] + " "
        when = "  OPEN " + dur(b["left"])
        plus = " +%d" % more if more else ""
        tail = self.s(g["sep"]) + fails_text(b["fails"])
        model = b["model"] or b["profile"]
        avail = w - len(elbow) - len(when) - len(plus)
        label = self._model_label(model, avail - len(tail))
        if not label:
            tail = ""
            label = self._model_label(model, avail, cut=True)
        self.put_parts(cv, x, y, [(elbow, NIGHT, False), (label, ASH, False),
                                  (plus if label else plus[1:], DUST, False), (when, BAD, False), (tail, DUST, False)], w)

    def _open_main(self, cv: Canvas, x: int, y: int, room: int, b: Dict[str, Any], more: int, now: float) -> None:
        """An open provider's row: OPEN countdown, cooldown bar, the breaker's model (+N more) and its fails.

        When room is short the bar goes first, then the model's vendor prefix, then the fails; the model
        itself is cut last.
        """
        head = "OPEN " + dur(b["left"])
        plus = " +%d" % more if more else ""
        tail = self.s(self.g["sep"]) + fails_text(b["fails"])
        model = b["model"] or b["profile"]
        avail = room - len(head) - 1 - len(plus)
        label = self._model_label(model, avail - len(tail))
        if not label:
            tail = ""
            label = self._model_label(model, avail, cut=True)
        xx = self.put_parts(cv, x, y, [(head, BAD, True)], room)
        bw = min(10, room - (len(head) + 1 + len(label) + len(plus) + len(tail)) - 1)
        if bw >= 4:
            fx.bar(cv, xx + 1, y, bw, b["left"] / max(0.1, b["span"]), t=now, unicode=self.u,
                   stops=(fx.scale(BAD, 0.6), BAD), shimmer=False, head=False)
            xx += 1 + bw
        self.put_parts(cv, xx + 1, y, [(label, ASH, False), (plus if label else plus[1:], DUST, False),
                                       (tail, DUST, False)], x + room - xx - 1)

    def _draw_providers(self, cv: Canvas, r: Tuple[int, int, int, int], v: View, now: float) -> None:
        x, y, w, h = r
        g = self.g
        P = v.providers
        if not P:
            cv.text(x, y, fit("no providers yet", w, self.g["ell"]), DUST)
            return
        if len(P) > h:
            self._providers_inline(cv, r, P, now)
            return
        rows = self._provider_rows(P, h - len(P))
        name_w = max(6, min(max(len(p["name"]) for p in P), max(8, w // 3), 26))
        strip_w = 12 if w >= 62 else (8 if w >= 50 else 0)
        for i, (kind, p, b, more) in enumerate(rows):
            yy = y + i
            if kind == "b" and b is not None:
                self._breaker_subrow(cv, x + 2, yy, w - 2, b, more)
                continue
            health = p["health"]
            cv.put(x, yy, g["dot"], self._health_color(health, now), True)
            cv.text(x + 2, yy, fit(self.s(p["name"]), name_w, g["ell"]), p["color"] if health != "idle" else ASH,
                    health == "busy")
            sx = x + 2 + name_w + 2
            if strip_w and sx + strip_w < x + w:
                res = p["results"][-strip_w:]
                pad = strip_w - len(res)
                for j in range(strip_w):
                    if j < pad:
                        cv.put(sx + j, yy, g["nocell"], NIGHT)
                    else:
                        ok = res[j - pad]
                        cv.put(sx + j, yy, g["cell"], fx.scale(OK, 0.8) if ok else BAD)
                sx += strip_w + 2
            room = x + w - sx
            if health == "open" and p["breakers"]:
                self._open_main(cv, sx, yy, room, p["breakers"][0], more, now)
                continue
            stats: List[Tuple[str, Color, bool]] = [("%d req" % p["requests"], ICE if p["requests"] else DUST, False)]
            if p["requests"]:
                er = p["err_rate"] * 100
                stats.append(("%s%% err" % ("%.0f" % er if er >= 10 or er == 0 else "%.1f" % er),
                              BAD if er >= 20 else WARN if er > 0 else DUST, False))
            if p["ttft"]:
                stats.append(("ttft " + dur(p["ttft"]), speed_color(p["ttft"]), False))
            if p["tokens_out"]:
                stats.append((human(p["tokens_in"] + p["tokens_out"]) + " tok", DUST, False))
            if p["busy"]:
                stats.append((self.spinner(now) + " %d" % p["busy"], OK, True))
            xx = sx
            lead = False
            if p["breakers"] and not (i + 1 < len(rows) and rows[i + 1][0] == "b"):
                # Degraded, and no spare row below says which breaker is open: say it here, first, with the
                # model cut short enough that its request count (and error rate when it fits) still shows.
                b0 = p["breakers"][0]
                model = b0["model"] or b0["profile"]
                head = "OPEN " + dur(b0["left"]) + " "
                plus = " +%d" % more if more else ""
                avail = room - len(head) - len(plus)
                keep = [len(self.s("  " + t)) for t, _, _ in stats[:2]]
                label = (self._model_label(model, avail - sum(keep)) or self._model_label(model, avail - keep[0])
                         or self._model_label(model, avail, cut=True))
                xx = self.put_parts(cv, xx, yy, [(head, BAD, False), (label, WARN, False),
                                                 (plus if label else plus[1:], DUST, False)], room)
                lead = True
            for j, (text, col, bold) in enumerate(stats):
                piece = ("  " if j or lead else "") + text
                if xx + len(self.s(piece)) > x + w:
                    break
                xx = cv.text(xx, yy, self.s(piece), col, bold)

    def _draw_throughput(self, cv: Canvas, r: Tuple[int, int, int, int], v: View, now: float) -> None:
        x, y, w, h = r
        g = self.g
        cur = self.ease("tps_big", v.tps_now, now, 5.0)
        gutter = 0
        if w >= 54 and h >= 4:
            gutter = 19
            cv.text(x, y, "TOKENS / S", DUST)
            if self.u and h >= 5:
                txt = rate(cur)
                k = self.bump("tps", now, 0.5) * 0.3
                big_text(cv, x, y + 1, txt, (math.floor(now * 8) / 8.0 * 0.07) if self.motion and not self.lite else 0.0,
                         glow=k)
                info_y = y + 5
            else:
                cv.text(x, y + 1, rate(cur), OK if cur > 0 else DUST, True)
                info_y = y + 3
            lines = [("peak  ", rate(v.tps_peak), ASH), ("req/s ", "%.2f" % v.rps_now, BEAM),
                     ("ttft  ", dur(v.p50) if v.ttft else "-", speed_color(v.p50))]
            if h >= 12:
                flying = [f for s_ in v.sessions for f in s_["flights"] if not v.focus or s_["sid"] == v.focus]
                busiest = max(v.sessions, key=lambda s_: s_["tps"]) if v.sessions else None
                lines += [("", "", DUST), ("2m    ", human(sum(v.tps_hist)) + " tok", ASH),
                          ("fly   ", "%d" % len(flying), PLASMA if flying else DUST)]
                if busiest is not None and busiest["tps"] > 0.05 and not v.focus:
                    lines.append(("top   ", fit(busiest["label"] or busiest["sid"], 12, g["ell"]), busiest["hue"]))
            for i, (lab, val, col) in enumerate(lines):
                if info_y + i < y + h:
                    cv.text(x, info_y + i, lab, DUST)
                    cv.text(x + len(lab), info_y + i, val, col, True)
        cx, cw = x + gutter, w - gutter
        ch = h - 1 if h >= 4 else h
        if cw < 8 or ch < 1:
            return
        hist = v.tps_hist
        target = max(10.0, max(hist) * 1.15 if hist else 10.0)
        if self.motion and not self.paused and self.scale > 0:
            self.scale += (target - self.scale) * 0.12
        else:
            self.scale = target
        scale = max(1.0, self.scale)
        chart_w = cw - 1                   # last column holds the live head
        n = chart_w * (2 if self.u else 1)
        frac = (v.now - int(v.now)) if self.motion and not self.lite else 0.0     # smooth scroll
        samples = [min(1.0, s / scale) for s in resample(hist, n, frac)]
        # grid
        for gy in sorted({ch // 2, ch // 4, (3 * ch) // 4} if ch >= 8 else {ch // 2}):
            if 0 < gy < ch:
                for i in range(0, chart_w, 2):
                    cv.put(cx + i, y + gy, "┈" if self.u else ".", fx.scale(NIGHT, 0.9))
        sweep = None
        if self.motion and self.quality >= 0.3 and not self.paused:
            p = (now % 7.0) / 1.6
            if p <= 1.0:
                sweep = p * (chart_w + 3)
        crests = area_chart(cv, cx, y, chart_w, ch, samples, self.u, sweep)
        # live head
        last = samples[-1] if samples else 0.0
        hy = y + ch - 1 - min(ch - 1, int(last * ch))
        if crests and crests[-1] >= 0:
            hy = y + crests[-1]
        hk = self.breathe(now, 6.0, 0.35)
        cv.put(cx + chart_w, hy, g["dot"] if last > 0 else (g["nocell"]), fx.lerp(EMBER, ICE, hk) if last > 0 else DUST,
               True)
        # scale label
        cv.text(cx, y, "max " + human(scale), fx.scale(ASH, 0.75))
        if h >= 4:
            ay = y + ch
            rps = v.rps_hist
            eps = v.eps_hist
            span = len(rps)
            for i in range(chart_w):
                a0 = int(i * span / float(chart_w))
                a1 = max(a0 + 1, int((i + 1) * span / float(chart_w)))
                req = sum(rps[a0:a1])
                err = sum(eps[a0:a1])
                if err:
                    cv.put(cx + i, ay, "━" if self.u else "x", BAD, True)
                elif req:
                    cv.put(cx + i, ay, "━" if self.u else "=", fx.scale(BEAM, min(1.0, 0.45 + 0.2 * req)))
                else:
                    cv.put(cx + i, ay, "─" if self.u else "-", NIGHT)
            left = "-%dm" % max(1, v.history // 60)
            cv.text(cx, ay, left, DUST)
            if chart_w >= 24:
                mid = "-%ds" % (v.history // 2)
                cv.text(cx + chart_w // 2 - len(mid) // 2, ay, mid, DUST)
            cv.text(cx + chart_w + 1 - 3, ay, "now", DUST)

    def _draw_latency(self, cv: Canvas, r: Tuple[int, int, int, int], v: View, now: float) -> None:
        x, y, w, h = r
        vals = v.ttft
        if not vals:
            msg = fit("no samples since top started", w, self.g["ell"])
            cv.text(x + max(0, (w - len(msg)) // 2), y + h // 2, msg, DUST)
            return
        head: List[Tuple[str, Color, bool]] = [("p50 ", DUST, False), (dur(v.p50), speed_color(v.p50), True),
                                               ("   p95 ", DUST, False), (dur(v.p95), speed_color(v.p95), True)]
        if w >= 40:
            head += [("   ewma ", DUST, False), (dur(v.ewma), speed_color(v.ewma), True)]
        if w >= 52:
            head += [("   n ", DUST, False), ("%d" % len(vals), ASH, False)]
        self.put_parts(cv, x, y, head, w)
        bh = h - 1
        if bh < 1:
            return
        hist_w = 26 if (w >= 60 and bh >= 6) else 0
        stacked = not hist_w and bh >= 14 and w >= 30
        hist_rows = 0
        if stacked:
            hist_rows = 6
            bh -= hist_rows + 2
        bw = w - hist_w - (3 if hist_w else 0)
        if bw < 4:
            return
        per = 2 if self.u else 1               # requests per cell: thin braille bars
        shown = vals[-bw * per:]
        top = max(2.0, v.p95 * 1.25, max(shown) if shown else 0.0)
        lt = math.log1p(top * 4)

        def norm(t: float) -> float:
            return math.log1p(max(0.0, t) * 4) / lt
        samples = [norm(t) for t in shown]
        pad = bw * per - len(samples)          # right-align: the newest request sits at the right edge
        samples = [0.0] * pad + samples
        cells = (len(samples) + per - 1) // per
        worst = [max([0.0] + [shown[i - pad] for i in range(c * per, c * per + per) if 0 <= i - pad < len(shown)])
                 for c in range(cells)]
        cols = [speed_color(t) for t in worst]
        self.ease("ttft_n", float(len(vals)), now)
        k = self.bump("ttft_n", now, 0.6)
        if k > 0 and cols:
            cols[-1] = fx.lerp(cols[-1], ICE, k)
        gy = y + 1 + bh - 1 - min(bh - 1, int(norm(v.p95) * bh))
        if bh >= 3:
            for i in range(0, bw, 2):
                cv.put(x + i, gy, "┄" if self.u else ".", fx.scale(WARN, 0.35))
        area_chart(cv, x, y + 1, bw, bh, samples, self.u, colors=cols, fade=False)
        if bh >= 3:
            cv.text(x + bw - 3, gy, "p95", fx.scale(WARN, 0.85))
        if hist_w or stacked:
            if hist_w:
                hx, hy, hw = x + bw + 3, y + 1, hist_w
                for j in range(bh):
                    cv.put(hx - 2, y + 1 + j, self.g["vbar"], NIGHT)
            else:
                hx, hy, hw = x, y + 1 + bh + 1, min(w, 48)
                cv.text(hx, hy - 1, "distribution", NIGHT)
            bins = [(0.5, "<0.5s", OK), (1.0, "<1s", OK), (2.0, "<2s", BEAM), (4.0, "<4s", WARN), (8.0, "<8s", FLARE),
                    (float("inf"), "8s+", BAD)]
            counts = [0] * len(bins)
            for t in vals:
                for j, (lim, _, _) in enumerate(bins):
                    if t < lim:
                        counts[j] += 1
                        break
            mx = max(1, max(counts))
            for j, (_, lab, col) in enumerate(bins):
                yy = hy + j
                if yy >= y + h:
                    break
                cv.text(hx, yy, "%-5s" % lab, DUST)
                hbar(cv, hx + 6, yy, hw - 11, counts[j] / float(mx), col if counts[j] else NIGHT, self.u)
                cv.text(hx + hw - 4, yy, "%4d" % counts[j], ASH if counts[j] else NIGHT)

    def _draw_feed(self, cv: Canvas, r: Tuple[int, int, int, int], v: View, now: float) -> None:
        x, y, w, h = r
        items = v.feed[-h:]
        if not items:
            msg = ("quiet. requests, relays, heals, loops, stage and council events land here" if v.verbose
                   else "quiet. relays, heals, loops, stage and council events land here (v shows every request)")
            lines = _wrap(msg, w, self.g["ell"])
            for i, line in enumerate(lines[:h]):
                cv.text(x, y + i, line, DUST)
            return
        show_time = w >= 46
        show_sid = w >= 36
        yy = y + h - len(items)
        for e in items:
            glyph_u, glyph_a, label, col = KINDS.get(e["kind"], ("·", ".", e["kind"].upper()[:7], ASH))
            age = now - e["at"]
            fresh = self.motion and not self.paused and age < 0.5
            xx = x
            if show_time:
                xx = cv.text(xx, yy, clock(e["t"]), DUST if age > 5 else ASH)
                xx += 1
            gk = max(0.0, 1.0 - age / 0.6) if self.motion and not self.paused else 0.0
            cv.put(xx, yy, glyph_u if self.u else glyph_a, fx.lerp(col, ICE, gk), True)
            xx += 2
            lab = "%-7s" % label
            cv.text(xx, yy, lab, fx.lerp(col, ICE, gk * 0.7), True)
            xx += len(lab) + 1
            if show_sid:
                sid = e["sid"][:7] if e["sid"] else ""
                if sid:
                    cv.text(xx, yy, "%-7s" % sid, fx.scale(hue(e["sid"]), 0.9))
                elif e["kind"] in ("link", "lost"):
                    cv.text(xx, yy, "core", NIGHT)
                xx += 8
            room = x + w - xx
            if room <= 0:
                yy += 1
                continue
            if fresh:
                text = fit(self.s(entry_text(e, self.g["to"])), room, self.g["ell"])
                fx.decode_text(cv, xx, yy, text, now, e["at"], 0.3, self.rng, stops=(ICE, ASH), bold=False,
                               unicode=self.u)
            else:
                parts = []
                for s_, role in e["parts"]:
                    if role == "to":
                        parts.append((self.g["to"], DUST, False))
                    else:
                        parts.append((s_, {"hi": ICE, "dim": DUST, "bad": fx.scale(BAD, 0.9), "warn": WARN,
                                           "ok": OK}.get(role, ASH), role == "hi"))
                self.put_parts(cv, xx, yy, parts, room)
            yy += 1

    # -- overlays -----------------------------------------------------------------------
    def _lost(self, cv: Canvas, v: View, now: float) -> None:
        g = self.g
        msg = "CORE LINK LOST"
        sub = "showing the last picture" + g["sep"] + "relinking"
        if v.down_reason:
            sub2 = fit(self.s(v.down_reason), max(10, cv.w - 12), g["ell"])
        else:
            sub2 = ""
        bw = min(cv.w - 4, max(len(sub), len(sub2)) + 8)
        bh = 5 if sub2 else 4
        bx = (cv.w - bw) // 2
        by = max(2, (cv.h - bh) // 2)
        on = not self.motion or int(now * 2) % 2 == 0
        cv.box(bx, by, bw, bh, WARN if on else fx.scale(WARN, 0.5), unicode=self.u, fill=True)
        cv.text(bx + 3, by + 1, g["warn"] + " " + msg, WARN, True)
        cv.text(bx + 3, by + 2, fit(self.s(sub), bw - 6, g["ell"]), ASH)
        if sub2:
            cv.text(bx + 3, by + 3, fit(sub2, bw - 6, g["ell"]), DUST)

    def _help(self, cv: Canvas) -> None:
        rows = [("q  esc", "quit"), ("p  space", "pause / resume the picture"), ("c", "clear the feed"),
                ("s  tab", "focus the next session"), ("S  shift-tab", "previous session"),
                ("v", "every request in the feed"), ("ctrl-l", "repaint"), ("?", "close this help")]
        bw = min(cv.w - 2, 50)
        bh = min(cv.h - 2, len(rows) + 4)
        bx, by = (cv.w - bw) // 2, max(1, (cv.h - bh) // 2)
        cv.box(bx, by, bw, bh, BEAM, unicode=self.u, fill=True)
        cv.text(bx + 2, by, " KEYS ", ICE, True)
        for i, (k, d) in enumerate(rows[:bh - 3]):
            cv.text(bx + 3, by + 2 + i, "%-13s" % k, BEAM, True)
            cv.text(bx + 17, by + 2 + i, fit(d, bw - 20, self.g["ell"]), ASH)

    # -- idle and tiny ------------------------------------------------------------------------
    def _idle(self, cv: Canvas, v: View, now: float) -> None:
        w, h = cv.w, cv.h
        g = self.g
        t = now - (self.t0 or now)
        rich = self.motion and self.quality >= 0.2
        if rich and w >= 30 and h >= 10:
            if self.stars is None or (self.stars.w, self.stars.h) != (w, h):
                self.stars = fx.Stars(w, h, random.Random(3), max(6, w * h // 90))
            self.stars.draw(cv, t, self.u)
        big = h >= 16 and w >= 44
        cx = w // 2
        if big:
            cy = max(3, int(h * 0.33))
            text_y = max(cy + 5, min(h - 7, int(h * 0.64)))
            max_r = max(6.0, min(w * 0.26, (text_y - 2 - cy) * 2.0))
            if rich and self.u and self.quality >= 0.3 and not self.lite:
                key = (w, h, cx, cy, text_y)
                beams = getattr(self, "_beams", None)
                if beams is None or getattr(self, "_beams_key", None) != key:
                    beams = fx.Beams(w, h, cx, cy, radius=max(max_r * 1.6, w * 0.42), region=(0, 0, w, text_y - 1))
                    self._beams, self._beams_key = beams, key
                beams.draw(cv, t, amount=0.5, unicode=True, speed=0.55)
            if self.motion:
                fx.rings(cv, cx, cy, t, period=2.6, count=3, unicode=self.u, max_r=max_r)
            else:
                cv.put(cx, cy, g["mark"], BEAM, True)
        else:
            text_y = max(0, h // 2 - 2)
        title = "P U L S A R   O B S E R V A T O R Y" if w >= 48 else ("PULSAR OBSERVATORY" if w >= 22 else "PULSAR")
        if v.no_core:
            lead = ("Pulsar Core stopped: start a session with " if v.ever_linked
                    else "no Core running: start a session with ")
            cmd = "pulsarcode"
            note, note_c = "scanning for Pulsar Core" + "." * (int(now * 2) % 4 if self.motion else 3), DUST
        else:
            lead = ("Pulsar Core on :%d is not answering yet" % v.port) if v.port else "Pulsar Core is not answering"
            cmd = ""
            note, note_c = "retrying" + (": " + v.down_reason if v.down_reason else ""), WARN
        rows: List[List[Tuple[str, Color, bool]]] = [
            [(title, ICE, True)], [], [(lead, ASH, False), (cmd, BEAM, True)],
            [(self.spinner(now, 10.0) + "  ", BEAM, True), (note, note_c, False)]]
        if h >= 12 and w >= 50:
            rows += [[], [("pulsarcode top --once", DUST, False), ("  prints a summary   ", NIGHT, False),
                          ("--json", DUST, False), ("  for scripts", NIGHT, False)]]
        for i, parts in enumerate(rows):
            yy = text_y + i
            if yy >= h - 1 or not parts:
                continue
            total = sum(len(self.s(p[0])) for p in parts)
            if total > w - 2 and i == 2 and cmd:          # narrow: drop the command, keep the sentence
                parts = [(lead.rstrip(" ").rstrip(":") , ASH, False)]
                total = len(parts[0][0])
            shown = min(total, w - 2)
            xx = max(1, (w - shown) // 2)
            cv.clear(xx - 2, yy, shown + 4, 1)
            if i == 0:
                cv.gtext(xx, yy, fit(self.s(title), w - 2, g["ell"]), SPECTRUM, True,
                         offset=t * 0.08 if self.motion else 0.0)
            else:
                self.put_parts(cv, xx, yy, parts, w - 2)
        if h >= 6:
            cv.text(1, h - 1, "q", BEAM, True)
            cv.text(3, h - 1, "quit", DUST)
            ver = "pulsarcode top"
            if w > len(ver) + 10:
                cv.text(w - len(ver) - 1, h - 1, ver, NIGHT)

    def _tiny(self, cv: Canvas, v: View, now: float) -> None:
        g = self.g
        T = v.totals
        status = ("PAUSED", PLASMA) if self.paused else (("LIVE", OK) if v.state == "live" else ("RELINK", WARN))
        lines: List[List[Tuple[str, Color, bool]]] = [
            [(g["mark"] + " ", BEAM, True), ("PULSAR", EMBER, True), ("  " + status[0], status[1], True)],
            [("%d sess" % T["sessions"], ICE, False), (g["sep"] + "%d flying" % T["in_flight"], PLASMA, False)],
            [(rate(v.tps_now) + " tok/s", OK if v.tps_now > 0 else DUST, True)],
            [("%s req" % human(T["requests"]), ASH, False), (g["sep"] + "%d err" % T["errors"], BAD if T["errors"] else DUST,
                                                             False)],
            [(g["relay"] + " %d relays" % T["relays"], WARN if T["relays"] else DUST, False)],
        ]
        if v.feed:
            e = v.feed[-1]
            lines.append([(KINDS.get(e["kind"], ("", "", e["kind"], ASH))[2] + " ", KINDS.get(e["kind"], ("", "", "", ASH))[3],
                           True), (entry_text(e, g["to"]), ASH, False)])
        for i, parts in enumerate(lines[:max(1, cv.h - 1)]):
            self.put_parts(cv, 1, i, parts, cv.w - 2)
        if cv.h >= 3:
            self.put_parts(cv, 1, cv.h - 1, [("q", BEAM, True), (" quit", DUST, False)], cv.w - 2)


def _wrap(text: str, width: int, ell: str = "…") -> List[str]:
    lines: List[str] = []
    cur = ""
    for word in text.split(" "):
        if cur and len(cur) + 1 + len(word) > width:
            lines.append(cur)
            cur = word
        else:
            cur = (cur + " " + word) if cur else word
    if cur:
        lines.append(cur)
    return [fit(line, width, ell) for line in lines]


# ---------------------------------------------------------------------------
# One-shot text summary
# ---------------------------------------------------------------------------

def _breaker_line(b: Dict[str, Any], p: Callable[..., str]) -> str:
    return (p("OPEN %s" % dur(b["left"]), BAD, True) +
            p("  %s  %s" % (fails_text(b["fails"]), short_model(b["model"] or b["profile"], 40)), DUST))


def summary(v: View, ink: fx.Ink, u: bool = True) -> str:
    g = _GU if u else _GA

    def p(text: str, color: Optional[Color] = None, bold: bool = False) -> str:
        text = text if u else ascii_only(text)
        return ink.paint(text, color, bold) if (color is not None or bold) and ink.depth else text
    lines: List[str] = []
    brand = ink.gradient(g["mark"] + " PULSAR OBSERVATORY", bold=True) if ink.depth else g["mark"] + " PULSAR OBSERVATORY"
    if v.state == "idle":
        lines.append(brand)
        if v.no_core:
            lines.append("  no Pulsar Core is running: start a session with " + p("pulsarcode", BEAM, True))
        else:
            lines.append("  " + p("Pulsar Core is not answering", WARN) + (": " + p(v.down_reason, DUST) if v.down_reason else ""))
        return "\n".join(lines) + "\n"
    T = v.totals
    head = [brand]
    if v.port:
        head.append(p("core :%d" % v.port, ASH))
    if v.uptime:
        head.append(p("up " + dur(v.uptime), DUST))
    if v.core.get("version"):
        head.append(p("v" + v.core["version"], DUST))
    lines.append("  ".join(head))
    stats = ["%d session%s" % (T["sessions"], "" if T["sessions"] == 1 else "s"), "%d in flight" % T["in_flight"],
             "%s requests" % human(T["requests"]), "%d errors" % T["errors"], "in %s" % human(T["tokens_in"]),
             "out %s" % human(T["tokens_out"]), "cache %s" % human(T["cache_read"]), money(T["cost"]),
             "relays %d" % T["relays"]]
    lines.append("  " + p(g["sep"].join(stats), ASH))
    lines.append("  " + p("throughput ", DUST) + p(rate(v.tps_now) + " tok/s", OK, True) +
                 (p("   latency p50 %s  p95 %s  (%d samples)" % (dur(v.p50), dur(v.p95), len(v.ttft)), DUST)
                  if v.ttft else ""))
    lines.append("")
    lines.append(p("SESSIONS", BEAM, True))
    if not v.sessions:
        lines.append("  " + p("no live sessions", DUST))
    for s in v.sessions:
        agg = s["agg"]
        lines.append("  " + p(s["label"] or s["sid"], ICE, True) + "  " + p(short_path(s["cwd"]), DUST))
        chain = g["chain"].join(short_model(t["model"], 40) + (" (breaker open)" if t["state"] == "open" else "")
                                for t in s["plan"]) or short_model(agg.get("last_model", ""), 40)
        lines.append("    " + p("route  ", DUST) + p(chain, ASH) + "   " +
                     p("%d req%s%s tok%s" % (agg.get("requests", 0), g["sep"],
                                             human(agg.get("tokens_in", 0) + agg.get("tokens_out", 0)),
                                             (g["sep"] + "relays %d" % agg["relays"]) if agg.get("relays") else ""), DUST))
        for f in s["flights"]:
            if f["waiting"]:
                lines.append("    " + p("now    ", DUST) + p("awaiting first token %s" % dur(f["elapsed"]), PLASMA) +
                             "  " + p(short_model(f["model"], 40), DUST))
            else:
                lines.append("    " + p("now    ", DUST) + p("streaming %s tok/s" % rate(f["tps"]), OK) +
                             p("%s%s tok%s%s" % (g["sep"], human(f["tokens"]), g["sep"], dur(f["elapsed"])), DUST) +
                             "  " + p(short_model(f["model"], 40), DUST))
    lines.append("")
    lines.append(p("PROVIDERS", OK, True))
    if not v.providers:
        lines.append("  " + p("no providers yet", DUST))
    for pr in v.providers:
        col = {"open": BAD, "degraded": WARN, "busy": OK, "ok": OK}.get(pr["health"], DUST)
        row = "  " + p(g["dot"], col, True) + " " + p("%-24s" % fit(pr["name"], 24, g["ell"]), ICE) + " "
        # An open provider leads with its first breaker; any other provider (a degraded one too) keeps its
        # stats on its own line. Every open breaker gets a line of its own below.
        rest = pr["breakers"]
        if pr["health"] == "open" and rest:
            row += _breaker_line(rest[0], p)
            rest = rest[1:]
        else:
            row += p("%-9s" % pr["health"], col) + p("%5d req  %5.1f%% err" % (pr["requests"], pr["err_rate"] * 100), ASH)
            if pr["ttft"]:
                row += p("  ttft %s" % dur(pr["ttft"]), DUST)
        lines.append(row)
        for b in rest:
            lines.append(" " * 29 + p(g["elbow"] + " ", DUST) + _breaker_line(b, p))
    if v.feed:
        lines.append("")
        lines.append(p("FEED", FLARE, True))
        for e in v.feed[-8:]:
            glyph_u, glyph_a, label, col = KINDS.get(e["kind"], ("·", ".", e["kind"].upper(), ASH))
            lines.append("  " + p(clock(e["t"]), DUST) + " " + p((glyph_u if u else glyph_a) + " %-7s" % label, col, True) +
                         " " + p(entry_text(e, g["to"]), ASH))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------

def parse_args(args: List[str]) -> Dict[str, Any]:
    opts: Dict[str, Any] = {"once": False, "json": False, "sid": None, "help": False}
    i = 0
    while i < len(args):
        a = str(args[i])
        if a in ("-h", "--help", "help"):
            opts["help"] = True
        elif a in ("--once", "--plain"):
            opts["once"] = True
        elif a == "--json":
            opts["json"] = True
        elif a == "--sid":
            if i + 1 >= len(args):
                raise ValueError("--sid needs a session id")
            opts["sid"] = clean(args[i + 1], 64) or None
            i += 1
        elif a.startswith("--sid="):
            opts["sid"] = clean(a[6:], 64) or None
        else:
            raise ValueError("unknown option %s" % clean(a, 40))
        i += 1
    return opts


def _fx_level() -> str:
    try:
        from pulsar import hardware
        return hardware.fx_budget(hardware.peek())
    except Exception:
        return "lite"


def _isatty(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def live_mode(caps: Any) -> str:
    """anim, calm (reduced motion: live numbers, no animation) or plain (a one-shot summary)."""
    from pulsar.term import FX_OFF
    if not caps.tty or not _isatty(sys.stdout) or caps.color <= 0 or caps.fx in FX_OFF or caps.fx == "plain":
        return "plain"
    return "anim" if caps.motion else "calm"


def once(as_json: bool = False, focus: Optional[str] = None, out: Any = None,
         factory: Optional[Callable[[], Tuple[Any, Optional[Dict[str, Any]]]]] = None) -> int:
    """Print one summary (or JSON snapshot). Exit status 0 when a Core answered, 1 otherwise."""
    out = out or sys.stdout
    red = Reducer()
    now = time.time()
    try:
        client, info = (factory or _admin_client)()
    except Exception:
        client, info = None, None
    snap: Any = None
    err = ""
    if client is not None:
        from pulsar.core.client import CoreUnavailable
        try:
            snap = client.get("/pulsar/stats", timeout=3.0)
        except CoreUnavailable as exc:
            err = str(exc)
        except Exception as exc:
            err = type(exc).__name__
    if isinstance(snap, dict):
        red.link(info, now)
        red.stats(snap, now)
    else:
        red.down(err or "no Pulsar Core is running", now, no_core=client is None, info=info)
    if as_json:
        out.write(json.dumps(red.to_json(now), indent=2, sort_keys=True) + "\n")
    else:
        from pulsar.term import detect_color, detect_unicode
        depth = detect_color(os.environ, _isatty(out))
        u = detect_unicode(os.environ, getattr(out, "encoding", "") or "")
        out.write(summary(red.view(now, focus, verbose=False), fx.Ink(depth), u))
    try:
        out.flush()
    except (OSError, ValueError):
        pass
    return 0 if isinstance(snap, dict) else 1


def _live(caps: Any, opts: Dict[str, Any], calm: bool) -> int:
    from pulsar.term import Keyboard
    level = "minimal" if calm else _fx_level()
    fps = FPS_CALM if calm else (FPS_FULL if level == "full" else FPS_LITE)
    budget = fx.FrameBudget(level, fps=fps)
    red = Reducer()
    link = Link()
    seed = int(os.environ.get("PULSAR_SEED", "0") or 0) or None
    obs = Observatory(red, unicode=caps.unicode, motion=not calm,
                      haptics=fx.Haptics(caps.out, os.environ.get("PULSAR_HAPTICS", "on").lower()),
                      focus=opts.get("sid"), seed=seed)
    obs.lite = level != "full"
    failure: Optional[BaseException] = None
    link.start()
    try:
        calm_period = max(budget.period, 0.1)       # 10 fps when nothing moves with the data
        with fx.Screen(caps.out, caps.color) as scr, Keyboard(caps.out) as kb:
            while True:
                started = budget.begin()
                now = time.time()
                link.drain(red, now)
                red.step(now)
                obs.quality = budget.quality
                w, h = caps.size
                try:
                    cv = obs.frame(w, h, now)
                except Exception as exc:          # a drawing bug must never strand the terminal
                    failure = exc
                    break
                if obs.repaint:
                    scr.invalidate()
                    obs.repaint = False
                scr.present(cv)
                budget.end()
                period = budget.period if obs.busy(now) else (calm_period if obs.prev_state == "live" else
                                                              max(calm_period, 0.125))
                key = kb.read(max(0.004, period - (time.perf_counter() - started)))
                go = True
                while key is not None:
                    go = obs.key(key, time.time())
                    if not go:
                        break
                    key = kb.read(0) if kb.pending() else None
                if not go:
                    break
    finally:
        link.stop()
    if failure is not None:
        if os.environ.get("PULSAR_DEBUG") == "1":
            import traceback
            traceback.print_exception(type(failure), failure, failure.__traceback__)
        sys.stderr.write("pulsarcode top: the live view stopped (%s); a summary instead:\n" % type(failure).__name__)
        sys.stdout.write(summary(red.view(time.time()), fx.Ink(0), caps.unicode))
        return 1
    return 0


def cmd_top(args: list) -> int:
    """`pulsarcode top`: the Observatory."""
    try:
        opts = parse_args([str(a) for a in (args or [])])
    except ValueError as exc:
        sys.stderr.write("pulsarcode top: %s\n\n%s" % (exc, USAGE))
        return 2
    if opts["help"]:
        sys.stdout.write(USAGE)
        return 0
    if opts["json"] or opts["once"]:
        return once(as_json=opts["json"], focus=opts["sid"])
    from pulsar.term import Caps
    caps = Caps()
    mode = live_mode(caps)
    if mode == "plain":
        return once(focus=opts["sid"])
    try:
        return _live(caps, opts, calm=mode == "calm")
    except KeyboardInterrupt:
        return 0
