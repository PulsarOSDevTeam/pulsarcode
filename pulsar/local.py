# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Local model servers: loopback-only discovery and hardware advice.

discover_local() asks the well-known local inference servers on this
machine which models they serve: Ollama (11434), LM Studio (1234),
llama.cpp (8080), vLLM (8000), Jan (1337) and LocalAI (8080, told apart
from llama.cpp by its answer). It is safe to run at every launch:

* it only ever connects to 127.0.0.1 (every URL is checked before use)
* it never uses a proxy, never follows redirects and sends no credentials
* all ports are probed concurrently with a short timeout, and any error
  simply means "nothing there"

recommend_local() turns a hardware summary into one line of advice.

configured_endpoint() reads the self-hosted endpoint PULSARCODE_LOCAL_ENDPOINT
names (any host the user chose; never read, never contacted when unset), and
endpoint_answers() tells whether it answers before a launch prefers it.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Mapping, Optional, Tuple

LOOPBACK = "127.0.0.1"
_ALLOWED_HOSTS = frozenset({"127.0.0.1"})
MAX_BYTES = 2_000_000
UA = "pulsarcode-local-discovery"

# (provider id, port). LocalAI shares 8080 with llama.cpp: one probe decides.
SERVERS: Tuple[Tuple[str, int], ...] = (
    ("ollama", 11434),
    ("lmstudio", 1234),
    ("llamacpp", 8080),
    ("vllm", 8000),
    ("jan", 1337),
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A local server must never bounce us to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


# No ProxyHandler entries: HTTP(S)_PROXY from the environment is ignored, so a
# loopback probe can never leave the machine through a proxy.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _get_json(port: int, path: str, timeout: float) -> Optional[Any]:
    url = "http://%s:%d%s" % (LOOPBACK, port, path)
    if urllib.parse.urlsplit(url).hostname not in _ALLOWED_HOSTS:
        raise ValueError("refusing non-loopback URL")  # defence in depth
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read(MAX_BYTES).decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _model_ids(payload: Any) -> List[str]:
    items: Any = None
    if isinstance(payload, dict):
        items = payload.get("data")
        if not isinstance(items, list):
            items = payload.get("models")
    elif isinstance(payload, list):
        items = payload
    out: List[str] = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, str):
            mid = item
        elif isinstance(item, dict):
            mid = str(item.get("id") or item.get("model") or item.get("name") or "")
        else:
            continue
        mid = mid.strip()
        if mid and mid not in out:
            out.append(mid)
    return out


def _owned_by(payload: Any) -> List[str]:
    items = payload.get("data") if isinstance(payload, dict) else None
    return [str(i.get("owned_by") or "") for i in items or [] if isinstance(i, dict)]


def _probe(provider_id: str, port: int, timeout: float) -> Optional[Dict[str, Any]]:
    listing = _get_json(port, "/v1/models", timeout)
    models = _model_ids(listing)
    if provider_id == "ollama":
        tags = _get_json(port, "/api/tags", timeout)
        for mid in _model_ids(tags):
            if mid not in models:
                models.append(mid)
        if listing is None and tags is None:
            return None
    elif listing is None:
        return None
    if provider_id in ("llamacpp", "localai"):
        if "llamacpp" in _owned_by(listing):
            provider_id = "llamacpp"
        else:
            props = _get_json(port, "/props", timeout)
            looks_llama = isinstance(props, dict) and any(
                k in props for k in ("default_generation_settings", "total_slots", "build_info"))
            provider_id = "llamacpp" if looks_llama else "localai"
    return {"provider_id": provider_id, "base_url": "http://%s:%d/v1" % (LOOPBACK, port), "port": port,
            "models": models}


def discover_local(timeout: float = 0.25, servers: Optional[Tuple[Tuple[str, int], ...]] = None) -> List[Dict[str, Any]]:
    """Running local servers: [{provider_id, base_url, port, models: [ids]}], in SERVERS order.

    Each probe is a handful of loopback GETs with `timeout` seconds each; all
    ports are probed at the same time, so the whole call takes roughly
    2 x timeout when nothing answers slowly.
    """
    wanted = servers if servers is not None else SERVERS
    seen_ports = set()
    plan: List[Tuple[str, int]] = []
    for pid, port in wanted:
        if port not in seen_ports:
            seen_ports.add(port)
            plan.append((pid, port))
    results: Dict[int, Dict[str, Any]] = {}
    lock = threading.Lock()

    def run(pid: str, port: int) -> None:
        try:
            found = _probe(pid, port, timeout)
        except Exception:
            found = None
        if found is not None:
            with lock:
                results[port] = found

    threads = [threading.Thread(target=run, args=item, name="pulsar-local-%d" % item[1], daemon=True)
               for item in plan]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout * 4 + 0.5)
    with lock:
        out = [results[port] for _, port in plan if port in results]
    seen_urls = set()
    unique = []
    for item in out:
        if item["base_url"] not in seen_urls:
            seen_urls.add(item["base_url"])
            unique.append(item)
    return unique


# ---------------------------------------------------------------------------
# A self-hosted endpoint named in the environment
# ---------------------------------------------------------------------------

ENDPOINT_VARS = ("PULSARCODE_LOCAL_ENDPOINT", "PULSAR_LOCAL_ENDPOINT")
_OFF_WORDS = frozenset(("", "0", "off", "none", "false"))
_CONTROL = re.compile("[\x00-\x20\x7f-\x9f]")


def configured_endpoint(environ: Optional[Mapping[str, str]] = None) -> str:
    """What PULSARCODE_LOCAL_ENDPOINT (or its alias PULSAR_LOCAL_ENDPOINT) names, stripped, or "".

    An OpenAI-compatible base URL, or an Anthropic Messages one when it ends in /anthropic. The first
    of the two names that is set decides, and 0, off, none, false or an empty value turn the endpoint
    off even when the other name is set. Reading it never touches the network.
    """
    env = os.environ if environ is None else environ
    for var in ENDPOINT_VARS:
        raw = env.get(var)
        if raw is None:
            continue
        raw = raw.strip()
        return "" if raw.lower() in _OFF_WORDS else raw
    return ""


def endpoint_base(raw: str) -> str:
    """`raw` as a base URL to save: http(s) with a host, no user:password, ?query or #fragment, no
    trailing slash. "" for anything else (no scheme, spaces, control characters)."""
    raw = (raw or "").strip()
    if not raw or _CONTROL.search(raw):
        return ""
    try:
        split = urllib.parse.urlsplit(raw)
        host = split.hostname
    except ValueError:
        return ""
    scheme = split.scheme.lower()
    if scheme not in ("http", "https") or not host:
        return ""
    netloc = split.netloc.rsplit("@", 1)[-1]
    return urllib.parse.urlunsplit((scheme, netloc, split.path, "", "")).rstrip("/")


def prefer_local(environ: Optional[Mapping[str, str]] = None) -> bool:
    """False with PULSARCODE_PREFER_LOCAL=0 (or off, false, no): the saved provider launches even while
    the configured endpoint answers."""
    env = os.environ if environ is None else environ
    return (env.get("PULSARCODE_PREFER_LOCAL") or "").strip().lower() not in ("0", "off", "false", "no")


def endpoint_answers(base_url: str, timeout: float = 1.5) -> bool:
    """True when an HTTP server answers at `base_url`, whatever the status (a 401 or 404 still answers).

    One GET of <base>/models (<base>/v1/models for an Anthropic Messages base). No key, no redirect
    followed, and no proxy for a loopback or LAN host. A refused connection, a timeout or an answer
    that is not HTTP means no.
    """
    base = endpoint_base(base_url)
    if not base:
        return False
    url = base + ("/v1/models" if base.endswith("/anthropic") else "/models")
    from pulsar import catalog as C
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with C.opener_for(url).open(req, timeout=timeout) as resp:
            resp.read(1)
        return True
    except urllib.error.HTTPError:
        return True                                   # an error status is still the server answering
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Hardware advice
# ---------------------------------------------------------------------------

def _num(hw: Mapping[str, Any], *keys: str) -> float:
    for k in keys:
        try:
            v = float(hw.get(k) or 0)
        except (TypeError, ValueError):
            continue
        if v > 0:
            return v
    return 0.0


_CLASSES: Tuple[Tuple[float, str], ...] = (
    (6, "a 3-4B model at 4-bit (Gemma 3 4B class): fine for quick edits, weak at agentic tool use"),
    (12, "a 7-9B model at 4-bit (Qwen3.5 9B class); coding agents work, slowly and with simple tasks"),
    (20, "gpt-oss-20b (about 13 GB) or a 14B model at 4-bit"),
    (32, "a 27-32B model at 4-bit (Qwen3.8 27B, Gemma 4 31B) or a 30B-A3B MoE such as Qwen3 Coder 30B-A3B"),
    (64, "a 30-35B MoE at 8-bit (Qwen3.6 35B-A3B, Nemotron 3 Nano 30B-A3B) or a 70B dense model at 4-bit"),
    (128, "a 120B-class MoE at 4-bit (gpt-oss-120b needs about 65 GB; Nemotron 3 Super 120B-A12B)"),
)


def recommend_local(hardware: Mapping[str, Any]) -> str:
    """One line of advice for local models from a hardware summary.

    Understands ram_gb, vram_gb, unified (Apple Silicon style shared memory),
    or os + arch (darwin + arm64 implies unified). Sizes assume 4-bit weights
    plus room for a coding-agent context.
    """
    hw = hardware or {}
    ram = _num(hw, "ram_gb", "ram", "memory_gb")
    vram = _num(hw, "vram_gb", "vram", "gpu_memory_gb")
    os_name = str(hw.get("os") or hw.get("platform") or "").lower()
    arch = str(hw.get("arch") or hw.get("machine") or "").lower()
    unified = bool(hw.get("unified")) or (os_name in ("darwin", "macos", "mac") and arch in ("arm64", "aarch64"))
    if unified and ram:
        budget, where = ram * 0.70, "%.0f GB unified memory" % ram
    elif vram >= 4:
        budget, where = vram * 0.90, "%.0f GB of GPU memory" % vram
    elif ram:
        budget, where = ram * 0.50, "%.0f GB RAM, CPU only" % ram
    else:
        return "Hardware unknown: start with a free cloud tier (NVIDIA NIM, Gemini, Groq) or a 7-9B local model."
    if budget < 3:
        return ("With %s, local coding models will struggle; use a free cloud tier (NVIDIA NIM, Gemini, Groq) "
                "instead." % where)
    pick = "a 200B+ MoE at 4-bit (Qwen3.5 397B-A17B class needs about 220 GB)"
    for limit, text in _CLASSES:
        if budget < limit:
            pick = text
            break
    tail = " Expect a few tokens per second on CPU; a GPU is much faster." if where.endswith("CPU only") else ""
    return "With %s, run %s. Leave headroom for context: 32k tokens of cache costs several GB.%s" % (where, pick, tail)
