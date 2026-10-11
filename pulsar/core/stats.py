# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""In-memory metrics and an event bus for observers. Never holds prompt text or keys."""

from __future__ import annotations

import asyncio
import collections
import time
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Set

from pulsar.redact import redact

# Event fields that can carry upstream/provider text (which sometimes echoes a key back).
_FREE_TEXT = ("error", "reason", "detail", "message", "notice")


@dataclass
class Flight:
    """A request in progress."""
    rid: str
    sid: str
    model: str
    provider: str
    started: float
    first_token: float = 0.0
    out_chars: int = 0
    relays: int = 0
    profile: str = ""               # the plan target's profile (two custom endpoints can serve one model id)


@dataclass
class Agg:
    requests: int = 0
    errors: int = 0
    relays: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0
    cost: float = 0.0
    ttft_ewma: float = 0.0
    last_model: str = ""
    last_provider: str = ""
    last_profile: str = ""
    last_error: str = ""
    last_at: float = 0.0

    def to_json(self) -> Dict[str, Any]:
        return dict(vars(self))


class Stats:
    def __init__(self, ring: int = 600) -> None:
        self.started = time.time()
        self.total = Agg()
        self.by_session: Dict[str, Agg] = collections.defaultdict(Agg)
        self.by_provider: Dict[str, Agg] = collections.defaultdict(Agg)
        self.flights: Dict[str, Flight] = {}
        self.events: Deque[Dict[str, Any]] = collections.deque(maxlen=ring)
        self._subs: Set[asyncio.Queue] = set()
        self._seq = 0

    # -- bus ---------------------------------------------------------------
    def emit(self, _type: str, **data: Any) -> Dict[str, Any]:
        for k in _FREE_TEXT:
            v = data.get(k)
            if isinstance(v, str) and v:
                data[k] = redact(v)
        self._seq += 1
        ev = {"seq": self._seq, "t": time.time(), **data, "type": _type}
        self.events.append(ev)
        for q in list(self._subs):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass  # slow observer: drop rather than grow without bound
        return ev

    def subscribe(self) -> "asyncio.Queue[Dict[str, Any]]":
        q: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue(maxsize=2000)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    # -- flights -----------------------------------------------------------
    def begin(self, rid: str, sid: str, model: str, provider: str, profile: str = "") -> Flight:
        f = Flight(rid, sid, model, provider, time.time(), profile=profile)
        self.flights[rid] = f
        self.emit("begin", rid=rid, sid=sid, model=model, provider=provider)
        return f

    def token(self, rid: str, chars: int) -> None:
        f = self.flights.get(rid)
        if f is None:
            return
        if not f.first_token:
            f.first_token = time.time()
        f.out_chars += chars

    def end(self, rid: str, *, ok: bool, tokens_in: int = 0, tokens_out: int = 0, cache_read: int = 0,
            cost: float = 0.0, error: str = "", model: str = "", provider: str = "", profile: str = "") -> None:
        f = self.flights.pop(rid, None)
        if f is None:
            return
        error = redact(error)[:200] if error else ""
        model = model or f.model
        provider = provider or f.provider
        profile = profile or f.profile
        ttft = (f.first_token - f.started) if f.first_token else 0.0
        tokens_out = tokens_out or max(0, f.out_chars // 4)
        for agg in (self.total, self.by_session[f.sid], self.by_provider[provider]):
            agg.requests += 1
            agg.errors += 0 if ok else 1
            agg.relays += f.relays
            agg.tokens_in += tokens_in
            agg.tokens_out += tokens_out
            agg.cache_read += cache_read
            agg.cost += cost
            if ttft:
                agg.ttft_ewma = ttft if not agg.ttft_ewma else agg.ttft_ewma * 0.7 + ttft * 0.3
            agg.last_model, agg.last_provider, agg.last_profile, agg.last_at = model, provider, profile, time.time()
            if error:
                agg.last_error = error[:200]
        self.emit("end", rid=rid, sid=f.sid, model=model, provider=provider, ok=ok, ttft=round(ttft, 3),
                  dur=round(time.time() - f.started, 3), tokens_in=tokens_in, tokens_out=tokens_out,
                  cache_read=cache_read, cost=cost, error=error[:200], relays=f.relays)

    def relay(self, rid: str, frm: str, to: str, reason: str, profile: str = "") -> None:
        """`to` is "provider|model"; `profile` names the plan target taking over (its profile id)."""
        f = self.flights.get(rid)
        if f is not None:
            f.relays += 1
            f.model, f.provider = to.split("|", 1)[-1], to.split("|", 1)[0]
            f.profile = profile
        self.emit("relay", rid=rid, sid=f.sid if f else "", frm=frm, to=to, reason=reason)

    # -- snapshots ---------------------------------------------------------
    def snapshot(self, sid: Optional[str] = None) -> Dict[str, Any]:
        now = time.time()
        flights: List[Dict[str, Any]] = []
        for f in self.flights.values():
            if sid and f.sid != sid:
                continue
            elapsed = now - f.started
            gen = now - f.first_token if f.first_token else 0.0
            flights.append({"rid": f.rid, "sid": f.sid, "model": f.model, "provider": f.provider,
                            "elapsed": round(elapsed, 2), "waiting": not f.first_token,
                            "tokens": f.out_chars // 4, "tps": round((f.out_chars / 4) / gen, 1) if gen > 0.2 else 0.0,
                            "relays": f.relays})
        out: Dict[str, Any] = {"uptime": round(now - self.started, 1), "flights": flights}
        if sid:
            out["session"] = self.by_session[sid].to_json() if sid in self.by_session else Agg().to_json()
        else:
            out["total"] = self.total.to_json()
            out["sessions"] = {k: v.to_json() for k, v in self.by_session.items()}
            out["providers"] = {k: v.to_json() for k, v in self.by_provider.items()}
        return out
