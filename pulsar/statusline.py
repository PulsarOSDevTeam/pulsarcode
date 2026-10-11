# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""`pulsarcode statusline`: the branded status line Claude Code shows under the prompt.

Claude Code runs it after each update (and every refreshInterval seconds), passing session JSON on
stdin; it inherits the session environment, so it asks Pulsar Core (as this session) which model is
really answering. Must be fast and must never fail loudly.

Cheap by construction: the plugin starts it as `python_argv(module="pulsar.statusline")`, so the
CLI is never imported, and it talks to Core over one raw loopback socket (no urllib, no http.client,
no ssl) with a 0.35 s budget.

Colours follow the terminal: COLORTERM=truecolor|24bit gets 24-bit colour, everything else 256
colours, NO_COLOR (non-empty) or TERM=dumb none. PULSAR_COLOR=24|256|0 overrides.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple

PULSE = "·∙•●◉●•∙"
ASCII_PULSE = ".oOo"
CORE_TIMEOUT = 0.35

# Model ids, provider names and Claude Code's own display name reach the terminal here: strip every
# control character (C0, DEL, C1, zero-width, bidi) so no escape sequence can ride along.
_UNPRINTABLE = re.compile("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028\u2029\u202a-\u202e\u2060-\u2069\ufeff]")


def _safe(text: Any, limit: int = 80) -> str:
    return _UNPRINTABLE.sub("", str(text if text is not None else ""))[:limit]


SPECTRUM = ((255, 106, 0), (255, 46, 136), (157, 77, 255), (54, 226, 255))


def color_depth(environ: Optional[Mapping[str, str]] = None) -> int:
    """24 (truecolor), 8 (256 colours) or 0 (none) for this terminal."""
    env = os.environ if environ is None else environ
    if env.get("NO_COLOR"):                       # https://no-color.org: present and not empty
        return 0
    forced = (env.get("PULSAR_COLOR") or "").strip().lower()
    if forced in ("0", "none", "off", "no", "false"):
        return 0
    if forced in ("8", "256", "8bit"):
        return 8
    if forced in ("24", "24bit", "truecolor"):
        return 24
    if (env.get("TERM") or "").strip().lower() == "dumb":
        return 0
    if (env.get("COLORTERM") or "").strip().lower() in ("truecolor", "24bit"):
        return 24
    return 8


def _cube(v: int) -> int:
    return max(0, min(5, (int(v) * 5 + 127) // 255))


def _c(rgb, text: str, bold: bool = False, depth: int = 24) -> str:
    if depth == 0:
        return text
    r, g, b = rgb
    code = "38;2;%d;%d;%d" % (r, g, b) if depth >= 24 else "38;5;%d" % (16 + 36 * _cube(r) + 6 * _cube(g) + _cube(b))
    return "\x1b[%s%sm%s\x1b[0m" % ("1;" if bold else "", code, text)


def _strong(text: str, depth: int) -> str:
    """Bold in the terminal's own text colour: the names that must read on a light background as on a dark one
    (the brand's near-white would vanish on white)."""
    return text if depth == 0 else "\x1b[1m%s\x1b[0m" % text


def _grad(text: str, depth: int) -> str:
    if depth == 0:
        return text
    out = []
    n = max(1, len(text) - 1)
    for i, ch in enumerate(text):
        t = i / n * 3
        k = min(2, int(t))
        f = t - k
        a, b = SPECTRUM[k], SPECTRUM[k + 1]
        out.append(_c((int(a[0] + (b[0] - a[0]) * f), int(a[1] + (b[1] - a[1]) * f), int(a[2] + (b[2] - a[2]) * f)),
                      ch, True, depth))
    return "".join(out)


def _num(value: Any, default: float = 0.0) -> float:
    """A finite float from anything Core or Claude Code sent (strings, None, garbage), else `default`."""
    if isinstance(value, bool):
        return default
    try:
        f = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return f


def _int(value: Any, cap: float = 1e12) -> int:
    """int() of a Core/Claude Code number, clamped so a huge (or non-finite) float never overflows."""
    return int(max(-cap, min(cap, _num(value))))


def _k(n: Any) -> str:
    n = _num(n)
    if n >= 1_000_000:
        return "%.1fM" % (n / 1_000_000)
    if n >= 1000:
        return "%.1fk" % (n / 1000)
    return "%d" % n


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _gauge(pct: float, depth: int, unicode: bool, width: int = 6, hot: bool = False) -> str:
    """`ctx ▰▰▰▱▱▱ 48%`: how full the context window is (green, then amber, then red; red when `hot`)."""
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct / 100.0 * width))
    on, off = ("▰", "▱") if unicode else ("#", "-")
    color = (255, 70, 90) if hot else (80, 250, 170) if pct < 60 else (255, 196, 0) if pct < 85 else (255, 70, 90)
    bar = on * filled + off * (width - filled)
    return (_c((150, 146, 172), "ctx ", False, depth) + _c(color, bar if unicode else "[" + bar + "]", False, depth)
            + _c(color, " %d%%" % int(round(pct)), False, depth))


_WINDOW_VARS = ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_CODE_MAX_CONTEXT_TOKENS")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
# Providers whose models Claude Code's own price table really prices (its total_cost_usd).
CLAUDE_PRICED = ("anthropic", "claude", "bedrock", "vertex", "foundry")


# Each registry provider's display name (pulsar.providers, Provider.name), the name the picker, WARP,
# Profile.label and pulsar_switch show. Static so the status line never imports the registry on its hot
# path; tests/test_statusline.py checks it against pulsar.providers.PROVIDERS.
PROVIDER_NAMES: Dict[str, str] = {
    "nvidia": "NVIDIA NIM", "anthropic": "Anthropic", "claude": "Claude subscription", "openrouter": "OpenRouter",
    "openai": "OpenAI", "gemini": "Google Gemini", "groq": "Groq", "xai": "xAI", "deepseek": "DeepSeek",
    "moonshot": "Moonshot Kimi", "zai": "Z.ai GLM", "minimax": "MiniMax", "mistral": "Mistral",
    "cerebras": "Cerebras", "together": "Together AI", "fireworks": "Fireworks AI", "huggingface": "Hugging Face",
    "deepinfra": "DeepInfra", "sambanova": "SambaNova", "vercel": "Vercel AI Gateway", "requesty": "Requesty",
    "novita": "Novita AI", "dashscope": "Alibaba Qwen", "nebius": "Nebius Token Factory", "venice": "Venice",
    "chutes": "Chutes", "cohere": "Cohere", "azure": "Azure OpenAI", "foundry": "Azure AI Foundry (Claude)",
    "bedrock": "Amazon Bedrock", "vertex": "Google Vertex AI", "ollama": "Ollama", "lmstudio": "LM Studio",
    "llamacpp": "llama.cpp", "vllm": "vLLM", "jan": "Jan", "localai": "LocalAI",
}


def _env_int(env: Mapping[str, str], name: str) -> int:
    """A plain ASCII decimal from the environment, else 0 (unset).

    str.isdigit() alone also accepts digits int() refuses ('²') and other scripts' digits ('１２３') that
    Claude Code itself does not read as a number, so both count as unset, like any other garbage.
    """
    raw = str(env.get(name) or "").strip()
    if not (raw.isascii() and raw.isdigit()) or len(raw) >= 13:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def real_window(nominal: float, environ: Optional[Mapping[str, str]] = None) -> int:
    """The window the launcher set for this model when it is smaller than Claude Code's nominal one, else 0.

    Claude Code reports used_percentage against the window of the model behavesAs names (200k); for a
    smaller model the launcher sets CLAUDE_CODE_MAX_CONTEXT_TOKENS (and CLAUDE_CODE_AUTO_COMPACT_WINDOW,
    floored at 100k) to the real one (pulsar.launch.compact_env).
    """
    env = {} if environ is None else environ
    best = 0
    for var in _WINDOW_VARS:
        n = _env_int(env, var)
        if 0 < n < nominal:
            best = n if not best else min(best, n)
    return best


def _context(ctx: Dict[str, Any], env: Mapping[str, str]) -> Optional[Tuple[float, bool]]:
    """(percent used, past the blocking limit) for the gauge, or None when Claude Code sent nothing usable."""
    if ctx.get("used_percentage") is None:
        return None
    pct = _num(ctx.get("used_percentage"), -1.0)
    if pct < 0:
        return None
    nominal = _num(ctx.get("context_window_size"), 200_000.0)
    window = real_window(nominal, env) if nominal > 0 else 0
    if not window:
        return pct, False
    usage = _dict(ctx.get("current_usage"))
    if usage:
        used = sum(max(0.0, _num(usage.get(k))) for k in
                   ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    else:
        used = pct / 100.0 * nominal
    blocking = _env_int(env, "CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE")
    return used * 100.0 / window, bool(blocking and used >= blocking * 0.95)


def provider_label(target: Dict[str, Any]) -> str:
    """What to call a plan target's provider, as the picker and WARP do (config.Profile.label).

    A registry provider's display name ("groq" -> "Groq"), the endpoint's host for a custom one, and an
    id the registry does not know as it is (sanitized).
    """
    prov = str(target.get("provider") or "") if isinstance(target, dict) else ""
    if prov == "custom":
        host = _SCHEME.sub("", str(target.get("base_url") or "")).split("/")[0].rsplit("@", 1)[-1]
        return _safe(host, 40) or "custom"
    return PROVIDER_NAMES.get(prov) or _safe(prov, 40)


def _open_breaker(target: Dict[str, Any], breakers: Dict[str, Any]) -> bool:
    """True when Core reports an open breaker for this target's profile (keys are "profile|model")."""
    prefix = str(target.get("profile") or "") + "|"
    return prefix != "|" and any(str(k).startswith(prefix) for k in breakers)


def _answering_target(plan: List[Dict[str, Any]], provider_id: str, live: str, profile: str,
                      breakers: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The plan target that answered last, for its label (two custom endpoints can serve the same model id).

    Core names the profile that answered (last_profile). A Core that does not (older builds) names only the
    provider and model: among the targets that match, one whose breaker is closed is the likelier answer
    (Core relays away from a failing target), then the first in plan order.
    """
    same = [t for t in plan if str(t.get("provider") or "") == provider_id]
    if profile:
        mine = [t for t in same if str(t.get("profile") or "") == profile]
        hit = next((t for t in mine if live and str(t.get("model") or "") == live), mine[0] if mine else None)
        if hit is not None:
            return hit
    live_ones = [t for t in same if live and str(t.get("model") or "") == live] or same
    return next((t for t in live_ones if not _open_breaker(t, breakers)), live_ones[0] if live_ones else None)


def _claude_priced(provider: str, env: Mapping[str, str]) -> bool:
    """True when Claude Code's own cost figure applies: a Claude provider, or (unknown) no foreign endpoint."""
    if provider:
        return provider.lower() in CLAUDE_PRICED
    base = str(env.get("ANTHROPIC_BASE_URL") or "")
    return not base or _SCHEME.sub("", base).split("/")[0].lower() == "api.anthropic.com"


def render(info: Dict[str, Any], stats: Optional[Dict[str, Any]], depth: int = 24, now: Optional[float] = None,
           unicode: bool = True, environ: Optional[Mapping[str, str]] = None) -> str:
    """The status line. `info` is Claude Code's stdin JSON, `stats` Core's /pulsar/stats (None: offline).

    environ: the session's environment (main() passes os.environ); None means none is known.
    """
    now = time.time() if now is None else now
    env: Mapping[str, str] = {} if environ is None else environ
    info = _dict(info)
    dim = (150, 146, 172)
    sep = _c((88, 84, 108), "  │  " if unicode else "  |  ", False, depth)
    beads = PULSE if unicode else ASCII_PULSE
    pulse = beads[int(now * 4) % len(beads)]
    parts: List[str] = [_c((54, 226, 255), pulse, True, depth) + " " + _grad("PulsarCode", depth)]
    stats_d = _dict(stats)
    plan = [t for t in (stats_d.get("plan") or []) if isinstance(t, dict)] if isinstance(stats_d.get("plan"), list) else []
    agg = _dict(stats_d.get("session"))
    flights = [f for f in stats_d.get("flights") or [] if isinstance(f, dict)] \
        if isinstance(stats_d.get("flights"), list) else []
    cc_model = _dict(info.get("model"))
    live_model = agg.get("last_model") or (plan[0].get("model") if plan else "") or \
        cc_model.get("display_name") or cc_model.get("id") or ""
    provider_id = str(agg.get("last_provider") or (plan[0].get("provider") if plan else "") or "")
    provider = ""
    if provider_id:
        target = _answering_target(plan, provider_id, str(agg.get("last_model") or ""),
                                   str(agg.get("last_profile") or ""), _dict(stats_d.get("breakers")))
        provider = provider_label(target if target is not None else {"provider": provider_id})
    live_model, provider = _safe(live_model), _safe(provider, 40)
    if live_model:
        parts.append(_strong(live_model, depth) +
                     (_c(dim, (" · " if unicode else " - ") + provider, False, depth) if provider else ""))
    gauge = _context(_dict(info.get("context_window")), env)
    if gauge is not None:
        parts.append(_gauge(gauge[0], depth, unicode, hot=gauge[1]))
    core_cost = 0.0
    if stats is None:
        parts.append(_c((255, 196, 0), "core offline", False, depth))
    else:
        if flights:
            f = flights[0]
            if f.get("waiting"):
                parts.append(_c((157, 77, 255), "awaiting first token %ds" % _int(f.get("elapsed")), False, depth))
            else:
                tps = _num(f.get("tps"))
                parts.append(_c((80, 250, 170), ("⇣ %s tok/s" if unicode else "%s tok/s") % ("%g" % round(tps, 1)),
                                False, depth))
        tin, tout = _num(agg.get("tokens_in")), _num(agg.get("tokens_out"))
        if tin or tout:
            parts.append(_c(dim, "in %s  out %s" % (_k(tin), _k(tout)), False, depth))
        ttft = _num(agg.get("ttft_ewma"))
        if ttft > 0:
            parts.append(_c(dim, "ttft %dms" % _int(min(ttft, 1e9) * 1000), False, depth))
        if len(plan) > 1:
            breakers = _dict(stats_d.get("breakers"))
            dots = "".join(_c((255, 70, 90) if any(str(k).startswith(str(t.get("profile", "")) + "|") for k in breakers)
                              else (80, 250, 170), "●" if unicode else "*", False, depth) for t in plan[:5])
            parts.append(_c(dim, "relay ", False, depth) + dots)
        relays = _int(agg.get("relays"))
        if relays > 0:
            parts.append(_c((255, 196, 0), ("↻ %d" if unicode else "relays %d") % relays, False, depth))
        core_cost = _num(agg.get("cost"))
    # Core's own cost when it priced the session. Else Claude Code's figure, from its own price table, but
    # only for a model that table really prices (a Claude provider): for any other model it would price
    # the behavesAs Claude model, so a free or local model would show a made-up cost.
    if core_cost > 0:
        cost, approx = core_cost, ""
    elif _claude_priced(provider_id, env):
        cost, approx = _num(_dict(info.get("cost")).get("total_cost_usd")), "~"
    else:
        cost, approx = 0.0, ""
    if cost > 0:
        parts.append(_c(dim, approx + ("$%.4f" % cost if cost < 10 else "$%.2f" % cost), False, depth))
    return sep.join(parts)


# ---------------------------------------------------------------------------
# Core, as this session, over one loopback socket
# ---------------------------------------------------------------------------

_CORE_URL = re.compile(r"^http://(127\.0\.0\.1|localhost|\[::1\]):([0-9]{1,5})/?$")
_SESSION_TOKEN = re.compile(r"^pls_[A-Za-z0-9_\-]{8,256}$")
_PATH = re.compile(r"^/[A-Za-z0-9/_\-.?=&]{0,200}$")


def session_core(environ: Optional[Mapping[str, str]] = None) -> Optional[Tuple[str, int, str]]:
    """(host, port, session token) when this process runs inside a Pulsar session, else None.

    Only plain http on loopback and a well-formed session token: the token never goes anywhere else
    and can never smuggle a header line.
    """
    env = os.environ if environ is None else environ
    m = _CORE_URL.match(env.get("PULSAR_CORE", "") or "")
    token = env.get("PULSAR_SESSION_TOKEN", "") or env.get("ANTHROPIC_AUTH_TOKEN", "") or ""
    if not m or not _SESSION_TOKEN.match(token):
        return None
    port = int(m.group(2))
    if not 0 < port < 65536:
        return None
    return m.group(1), port, token


_HEX = re.compile(rb"^[0-9A-Fa-f]{1,8}$")
_MAX_CHUNKS = 10000


def _dechunk(body: bytes) -> Optional[bytes]:
    """Undo chunked transfer coding, or None for anything malformed.

    Sizes are plain hex digits only (no sign, no 0x, no underscores that int() would accept), so
    every chunk moves forward, and the chunk count is capped: hostile input can never loop.
    """
    out, pos = [], 0
    for _ in range(_MAX_CHUNKS):
        end = body.find(b"\r\n", pos)
        if end < 0:
            return None
        field = body[pos:end].split(b";", 1)[0].strip()
        if not _HEX.match(field):
            return None
        size = int(field, 16)
        if size == 0:
            return b"".join(out)
        start = end + 2
        if start + size > len(body):
            return None
        out.append(body[start:start + size])
        pos = start + size + 2
    return None


def core_get(path: str, environ: Optional[Mapping[str, str]] = None, timeout: float = CORE_TIMEOUT,
             limit: int = 4_000_000) -> Optional[Dict[str, Any]]:
    """GET `path` from this session's Core within `timeout` seconds in total; the JSON object, or None.

    No proxy, no redirect, no TLS: Core is plain http on loopback, and anything else is refused.
    """
    target = session_core(environ)
    if target is None or not _PATH.match(path):
        return None
    host, port, token = target
    import socket
    addr = "::1" if host == "[::1]" else "127.0.0.1"
    deadline = time.monotonic() + max(0.01, timeout)
    chunks: List[bytes] = []
    total = 0
    try:
        with socket.create_connection((addr, port), timeout=max(0.01, timeout)) as sock:
            sock.sendall(("GET %s HTTP/1.1\r\nHost: %s:%d\r\nAuthorization: Bearer %s\r\nAccept: application/json\r\n"
                          "User-Agent: pulsar-statusline\r\nConnection: close\r\n\r\n"
                          % (path, host, port, token)).encode("ascii"))
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                sock.settimeout(left)
                data = sock.recv(65536)
                if not data:
                    break
                chunks.append(data)
                total += len(data)
                if total > limit:
                    return None
    except (OSError, ValueError, UnicodeError):
        return None
    if time.monotonic() > deadline:
        return None
    raw = b"".join(chunks)
    head, sep, body = raw.partition(b"\r\n\r\n")
    if not sep:
        return None
    lines = head.split(b"\r\n")
    status = lines[0].split()
    if len(status) < 2 or not status[0].startswith(b"HTTP/") or status[1] != b"200":
        return None                                   # an error, or a redirect Core never sends
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(b":")
        headers[name.strip().lower()] = value.strip().lower()
    if headers.get(b"transfer-encoding") == b"chunked":
        body = _dechunk(body)
        if body is None:
            return None
    elif headers.get(b"content-length", b"").isdigit():
        body = body[:int(headers[b"content-length"])]
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_stdin(limit: int = 1_000_000) -> Dict[str, Any]:
    try:
        stream = sys.stdin
        if stream is None or stream.isatty():
            return {}
        raw = stream.read(limit)
        info = json.loads(raw) if raw and raw.strip() else {}
    except (ValueError, OSError, AttributeError, UnicodeDecodeError, RecursionError):
        return {}
    return info if isinstance(info, dict) else {}


def _unicode_ok() -> bool:
    if os.environ.get("PULSAR_ASCII") == "1":
        return False
    enc = getattr(sys.stdout, "encoding", "") or "ascii"
    try:
        "◉│●⇣↻·▰▱".encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def main(argv: Optional[List[str]] = None) -> int:
    info = _read_stdin()
    depth = color_depth()
    stats = None
    try:
        stats = core_get("/pulsar/stats", timeout=CORE_TIMEOUT)
    except Exception:
        stats = None
    uni = _unicode_ok()
    try:
        line = render(info, stats, depth, unicode=uni, environ=os.environ)
    except Exception:
        line = "◉ PulsarCode" if uni else "PulsarCode"
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except (OSError, ValueError, UnicodeEncodeError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
