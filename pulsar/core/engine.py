# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Routing engine: plan resolution, relay, circuit breakers, upstream I/O.

Every streamed reply follows one rule: hold the line with SSE pings until a
target commits (answers 200), then stream only from that target. Until the
commit, any target may fail and the next one in the session plan takes over
(relay). After the commit, a failure ends the reply with a readable notice.

Three wires per target (Resolved.wire):
  chat       OpenAI chat-completions; translated by pulsar.translate
  responses  OpenAI Responses API; translated by pulsar.responses
  native     Anthropic Messages; passed through untranslated (real usage, real thinking)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

import httpx

from pulsar import __version__
from pulsar import catalog as C
from pulsar import netcfg
from pulsar import providers as P
from pulsar import responses as R
from pulsar import translate as T
from pulsar.core.state import ModelPicks, Session, Target
from pulsar.redact import redact
from pulsar.core.stats import Stats

PING = "event: ping\ndata: {\"type\": \"ping\"}\n\n"
ANTHROPIC_DEFAULT = "https://api.anthropic.com"
_NATIVE_FIELD = re.compile(r"[\"'`]?([a-z_]{3,40})[\"'`]?")
_CTX_MAX = re.compile(r"(?i)maximum context length (?:is|of) (\d{3,8})")
_CTX_IN = re.compile(r"(?i)(?:(\d{2,8}) in the messages|(\d{2,8}) (?:input|prompt) tokens|input (?:tokens|length)[^0-9]{0,12}(\d{2,8}))")
# The context limit an upstream names in its overflow error: OpenAI and vLLM ("maximum context length is N",
# "maximum prompt length is N"), the Anthropic wording ("... tokens > N maximum"), llama.cpp ("N maximum
# context length"), TGI ("must be <= N"), Gemini ("maximum number of tokens allowed (N)").
_CTX_STATED = re.compile(r"(?i)(?:maximum (?:context|prompt) length (?:is|of)|must be <=|token limit:?|"
                         r"tokens allowed \()\s*(\d{3,8})|(\d{3,8}) maximum\b")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
HEAL_TTL = 7 * 86400
MAX_FALLBACK_BODY = 4 * 1024 * 1024
# No upstream body is read without a bound: a provider or custom endpoint that sends an endless error page,
# answer, line or event costs Core a bounded amount of memory and time.
MAX_ERROR_BODY = 64 * 1024                 # an error answer (a notice quotes a few hundred characters of it)
ERROR_BODY_SECONDS = 15.0
MAX_REPLY_BODY = 32 * 1024 * 1024          # a whole non-streamed answer
MAX_LINE = 8 * 1024 * 1024                 # one line of a stream
MAX_EVENT = 16 * 1024 * 1024               # one Anthropic SSE event, up to its blank line
QUEUE_CHARS = 16 * 1024 * 1024             # stream lines read ahead of the translator


def env_float(name: str, default: float, legacy: str = "", zero_ok: bool = False) -> float:
    """A numeric PULSAR_* setting, or its v1 name `legacy` when only that one is set. Anything unparsable,
    NaN, infinite, negative or (unless `zero_ok`) zero means the default: a typo must never make every
    request time out at once."""
    raw = os.environ.get(name, "").strip() or (os.environ.get(legacy, "").strip() if legacy else "")
    try:
        val = float(raw) if raw else default
    except ValueError:
        return default
    if math.isnan(val) or math.isinf(val) or val < 0 or (val == 0 and not zero_ok):
        return default
    return val


@dataclass
class Tuning:
    header_timeout: float = field(default_factory=lambda: env_float(
        "PULSAR_HEADER_TIMEOUT", 180, "PULSAR_NIM_STREAM_HEADER_TIMEOUT"))
    header_timeout_reasoning: float = field(default_factory=lambda: env_float(
        "PULSAR_HEADER_TIMEOUT_REASONING", 420, "PULSAR_NIM_STREAM_HEADER_TIMEOUT_REASONING"))
    first_token_timeout: float = field(default_factory=lambda: env_float(
        "PULSAR_FIRST_TOKEN_TIMEOUT", 120, "PULSAR_NIM_FIRST_TOKEN_TIMEOUT"))
    first_token_timeout_reasoning: float = field(default_factory=lambda: env_float(
        "PULSAR_FIRST_TOKEN_TIMEOUT_REASONING", 420, "PULSAR_NIM_FIRST_TOKEN_TIMEOUT_REASONING"))
    ping_interval: float = field(default_factory=lambda: env_float(
        "PULSAR_PING_INTERVAL", 5, "PULSAR_NIM_STREAM_PING_INTERVAL"))
    retries: int = field(default_factory=lambda: int(env_float("PULSAR_RETRIES", 2, zero_ok=True)))
    max_heals: int = 3


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------

@dataclass
class Resolved:
    target: Target
    provider: P.Provider
    key: str
    model: str
    native: bool
    allow_http: bool = False        # the session accepted sending its key over plain http (see http_opt_in)

    @property
    def key_id(self) -> str:
        return self.target.profile + "|" + self.model

    @property
    def label(self) -> str:
        return "%s · %s" % (self.provider.name if self.provider.id != "custom" else _host(self.provider.base_url),
                            self.model or "default")

    @property
    def wire(self) -> str:
        """"native" (Anthropic Messages), "responses" or "chat" (OpenAI)."""
        if self.native:
            return "native"
        return "responses" if self.provider.wire_for(self.model) == "responses" else "chat"

    @property
    def url(self) -> str:
        if self.native:
            return (self.provider.native_base() or ANTHROPIC_DEFAULT) + "/v1/messages"
        return self.provider.base_url.rstrip("/") + ("/responses" if self.wire == "responses" else "/chat/completions")


def _host(url: str) -> str:
    """host[:port] of a URL, never a user name or password it carries."""
    netloc = re.split(r"[/?#]", re.sub(r"(?i)^https?://", "", url or ""), maxsplit=1)[0]
    return netloc.rsplit("@", 1)[-1]


def http_opt_in(sess: Optional[Session]) -> bool:
    """Whether a session may send a key over plain http to a host outside this machine and LAN: what the launch
    recorded (flags["allow_http"], from its PULSAR_ALLOW_HTTP=1), else Core's own environment."""
    flag = sess.flags.get("allow_http") if sess is not None else None
    return flag if isinstance(flag, bool) else P.http_allowed()


def transport_refusal(r: Resolved) -> str:
    """Why Core must not send this request at all, or "". One check for every way a target is reached
    (the plan, a relay, a /model pick, a council call, a switch)."""
    try:
        split = urllib.parse.urlsplit(r.url)
        userinfo = split.username is not None or split.password is not None or "@" in split.netloc
    except ValueError:
        return "the endpoint URL of %s cannot be parsed" % _host(r.url)
    if userinfo:
        # httpx would send them as Basic auth in place of the key, and they would show in every notice.
        return ("the endpoint URL of %s carries a user name or password, and Pulsar Code never sends credentials "
                "inside a URL. Remove them from the URL and store the key with `pulsarcode key`." % _host(r.url))
    return "" if r.allow_http else P.plain_http_refusal(r.url, r.key, environ={})


_URL = re.compile(r"(?i)\b(?:https?|wss?)://[^\s\"'<>`]+")


def scrub_urls(text: str, *bases: str) -> str:
    """`text` without URLs: every http(s) URL, and each given base URL (a provider endpoint), becomes <url>.
    For what leaves Core as diagnostics (stats, the event bus, `pulsarcode top --json`)."""
    out = str(text or "")
    for b in sorted(((b or "").strip().rstrip("/") for b in bases), key=len, reverse=True):
        if len(b) >= 8:
            out = out.replace(b, "<url>")
    return _URL.sub("<url>", out)


def provider_for(t: Target) -> P.Provider:
    if t.provider == "custom":
        return P.custom_provider(t.base_url or "http://127.0.0.1:8000/v1", native=t.native)
    prov = P.get(t.provider)
    if prov is None:
        raise KeyError("unknown provider %s" % t.provider)
    if t.base_url and t.base_url != prov.base_url:
        prov = replace(prov, base_url=t.base_url)
    return prov


async def _read_body(resp: httpx.Response, cap: int, truncate: bool = False) -> bytes:
    """The response body, never more than `cap` bytes of it in memory: with `truncate` its first `cap` bytes
    (the rest is never read), otherwise ValueError once it grows past `cap`."""
    buf = bytearray()
    async for chunk in resp.aiter_bytes():
        buf += chunk
        if len(buf) > cap:
            if truncate:
                return bytes(buf[:cap])
            raise ValueError("the provider's answer is larger than %d MiB" % max(1, cap >> 20))
    return bytes(buf)


_EOL = re.compile(r"\r\n|\r|\n")


async def _lines(resp: httpx.Response, limit: Optional[int] = None) -> AsyncIterator[str]:
    """resp.aiter_lines() for an event stream (lines end in \\r\\n, \\n or \\r), with no line longer than
    `limit` characters (MAX_LINE): a longer one raises ValueError instead of growing in memory."""
    limit = MAX_LINE if limit is None else limit
    pending: List[str] = []         # the unfinished line, in pieces
    size = 0
    cr = False                      # the last piece ended in \r, which may be half of a \r\n
    async for text in resp.aiter_text():
        if not text:
            continue
        if cr:
            text, cr = "\r" + text, False
        if text.endswith("\r"):
            text, cr = text[:-1], True
        parts = _EOL.split(text)
        if len(parts) > 1:
            first = "".join(pending) + parts[0]
            pending, size = [], 0
            for line in [first] + parts[1:-1]:
                if len(line) > limit:
                    raise ValueError("the provider sent a line longer than %d MiB" % max(1, limit >> 20))
                yield line
        pending.append(parts[-1])
        size += len(parts[-1])
        if size > limit:
            raise ValueError("the provider sent a line longer than %d MiB" % max(1, limit >> 20))
    last = "".join(pending)
    if last or cr:
        yield last
def _placeholder(prov: P.Provider) -> bool:
    """A provider that needs your own URL (Azure OpenAI, Foundry) still pointing at the registry placeholder
    (https://YOUR-RESOURCE...), or at no URL at all: nothing may be sent there."""
    if not prov.needs_url:
        return False
    base = (prov.base_url or "").strip().rstrip("/")
    default = P.BY_ID.get(prov.id)
    return (not base or "YOUR-RESOURCE" in base.upper()
            or (default is not None and base.lower() == (default.base_url or "").strip().rstrip("/").lower()))


def clean_model(requested: str) -> str:
    req = (requested or "").strip()
    for suffix in ("[1m]", "[200k]", "[long]"):
        if req.lower().endswith(suffix):
            req = req[: -len(suffix)]
    return req


# What an upstream sends is read without trusting its types: json.loads accepts figures int() refuses (a
# superscript digit passes str.isdigit()), and gateways send numbers as strings, lists or objects.

def _obj(value: Any) -> Dict[str, Any]:
    """An upstream field that should hold an object: the object, or {} for anything else."""
    return value if isinstance(value, dict) else {}


def _count(value: Any) -> int:
    """A token count or block index from an upstream body; 0 when it is not a readable count."""
    return T._int(value) or 0


def _code_status(code: Any, default: int) -> int:
    """The HTTP status an upstream error body gives as its code, or `default` for anything but a plain
    three-digit status."""
    text = str(code).strip() if isinstance(code, (int, str)) and not isinstance(code, bool) else ""
    return int(text) if re.fullmatch(r"[1-5][0-9][0-9]", text) else default


class Failure(Exception):
    def __init__(self, kind: str, status: int = 0, detail: str = "", retry_after: float = 0.0,
                 overflow: Optional[bool] = None) -> None:
        super().__init__(kind)
        self.kind = kind            # network | header_timeout | first_token | interrupted | not_api | refused | http
        self.status = status
        self.detail = detail
        self.retry_after = retry_after
        self._overflow = overflow   # decided by Engine.open, which also weighs a smaller max_tokens

    @property
    def overflow(self) -> bool:
        """The prompt no longer fits the model's context: Claude Code compacts on "prompt is too long"."""
        if self._overflow is not None:
            return self._overflow
        return T.context_overflow(self.status, self.detail or "")

    @property
    def transient(self) -> bool:
        if self.kind == "not_api":
            return False            # a web page instead of the API: a wrong base URL does not heal by waiting
        return self.kind in ("network", "header_timeout") or self.status in (408, 409, 429, 500, 502, 503, 504, 529)


class Breakers:
    """Per-target circuit breakers: closed -> open (cooldown) -> half-open."""

    def __init__(self) -> None:
        self.until: Dict[str, float] = {}
        self.fails: Dict[str, int] = {}

    def allow(self, key: str) -> bool:
        return time.time() >= self.until.get(key, 0.0)

    def fail(self, key: str, f: Failure) -> float:
        n = self.fails.get(key, 0) + 1
        self.fails[key] = n
        if f.status in (401, 403):
            base = 300.0
        elif f.status == 402:
            base = 600.0
        elif f.status == 429:
            base = f.retry_after or 20.0
        elif f.kind == "network":
            base = 5.0
        else:
            base = f.retry_after or 10.0
        cool = min(600.0, base * (2 ** min(n - 1, 5)))
        self.until[key] = time.time() + cool
        return cool

    def ok(self, key: str) -> None:
        self.fails.pop(key, None)
        self.until.pop(key, None)

    def state(self) -> Dict[str, Any]:
        now = time.time()
        return {k: {"open_for": round(v - now, 1), "fails": self.fails.get(k, 0)} for k, v in self.until.items() if v > now}


# ---------------------------------------------------------------------------
# Notices (rendered inside Claude Code as assistant text)
# ---------------------------------------------------------------------------

def _short(detail: str, limit: int = 300, secrets: Any = ()) -> str:
    """The readable part of an upstream error, redacted after JSON decoding (an upstream that escapes the
    key as "\\/" or "\\uXXXX" shows it plainly once decoded)."""
    secrets = [s for s in (secrets or ()) if s]
    text = " ".join(str(detail or "").split())
    try:
        data = json.loads(detail)
        err = data.get("error", data) if isinstance(data, dict) else data
        if isinstance(err, dict):
            text = str(err.get("message") or err.get("detail") or err.get("title") or text)
        elif isinstance(err, str):
            text = err
    except (ValueError, TypeError):
        pass
    if "<html" in text.lower():
        text = re.sub(r"<[^>]+>", " ", text)
        text = " ".join(text.split())
    # Control characters first, bidi and zero-width ones included (they reorder or hide text): stripped after
    # redaction, a hidden character inside an echoed key would carry the key past it.
    text = _CONTROL.sub(" ", text)
    text = redact(P.clean_text(text, len(text)), secrets)
    return text[:limit] + ("..." if len(text) > limit else "")


def context_room(text: str, requested: int) -> int:
    """For "max_tokens too large for the context" errors: a max_tokens that fits, else 0.

    vLLM/OpenAI word it "maximum context length is N ... M input tokens" (or "M in the messages");
    when the prompt itself fits there is room for a smaller answer, so this is not an overflow.
    """
    m = _CTX_MAX.search(text or "")
    i = _CTX_IN.search(text or "")
    if not m or not i:
        return 0
    limit = int(m.group(1))
    used = int(next(g for g in i.groups() if g))
    room = limit - used - 64
    return room if 256 <= room < max(requested, 1) else 0


def notice(prov: P.Provider, model: str, f: Failure, tried: int = 1, secrets: Any = (), retried: int = 0) -> str:
    """The readable notice for a failure. `tried`: distinct targets tried; `retried`: extra attempts on the
    last one; `secrets`: the target's key, masked in the upstream text however the upstream escaped it."""
    name = prov.name if prov.id != "custom" else _host(prov.base_url)
    reasoning = C.is_reasoning(model or "")
    head = "◉ Pulsar Code  ·  %s  ·  %s" % (name, model or "default")
    swap = ("switch model with `/model <id>` or `/pulsar:switch <id>` here, or `pulsarcode switch <id>` from "
            "another terminal (takes effect on the next message)")
    status, kind = f.status, f.kind
    if kind == "network":
        if prov.local:
            body = "Could not reach %s at %s.\nStart it (for Ollama: `ollama serve`), then send your message again." % (
                name, prov.base_url)
        else:
            body = "Could not reach %s.\nCheck your connection or proxy, then send your message again." % name
    elif kind == "header_timeout":
        body = "%s did not start answering inside the wait window.%s\nRetry, or %s.\nRaise it with PULSAR_HEADER_TIMEOUT%s=<seconds>." % (
            name, " This is a reasoning model; long deliberation is normal." if reasoning else "", swap,
            "_REASONING" if reasoning else "")
    elif kind == "first_token":
        body = "%s opened the stream but sent nothing inside the window.%s\nRetry, or %s.\nRaise it with PULSAR_FIRST_TOKEN_TIMEOUT%s=<seconds>." % (
            name, " Reasoning models can think for minutes." if reasoning else "", swap, "_REASONING" if reasoning else "")
    elif kind == "not_api":
        body = ("%s answered with a web page instead of the API.\nCheck the base URL (does it end in /v1?) or your "
                "proxy, then send your message again; or %s." % (name, swap))
    elif kind == "refused":
        body = "Pulsar Code did not send this request: %s\nOr %s." % (f.detail, swap)
    elif kind == "interrupted":
        body = "The %s stream was cut off mid-answer. Send `continue` to resume." % name
    elif status in (401, 403):
        body = "%s rejected the API key (HTTP %d).\nPaste a fresh key with `pulsarcode key` in any terminal; it applies to the next message." % (name, status)
        if prov.key_url:
            body += "\nKeys: " + prov.key_url
    elif status == 402:
        body = "%s says the account is out of credits (HTTP 402).\nTop up%s, or add another provider with `pulsarcode key` and it becomes a relay target." % (
            name, " at " + prov.key_url if prov.key_url else "")
    elif status == 404:
        body = "%s does not serve `%s` for this key (HTTP 404).\nPlease %s." % (name, model, swap)
    elif status == 429:
        body = "%s is rate-limiting this key (HTTP 429). Wait a minute and retry." % name
        if prov.id == "nvidia":
            body += " If your free NIM credits are spent, check https://build.nvidia.com."
        body += "\nTip: connect a second provider (`pulsarcode key`) and Pulsar Code relays to it automatically."
    elif status in (400, 422):
        body = "%s refused the request (HTTP %d) even after Pulsar Code adapted it.\nPlease %s." % (name, status, swap)
    elif status >= 500:
        body = "%s is having trouble (HTTP %d). Retry shortly, or %s." % (name, status, swap)
    else:
        body = "%s returned HTTP %d. Please %s." % (name, status, swap)
    if tried > 1 and retried:
        body += "\n(%d relay targets were tried and the last one retried %s; all failed.)" % (
            tried, "once" if retried == 1 else "%d times" % retried)
    elif tried > 1:
        body += "\n(%d relay targets were tried; all failed.)" % tried
    elif retried:
        body += "\n(Retried %s; it kept failing.)" % ("once" if retried == 1 else "%d times" % retried)
    short = "" if kind == "refused" else _short(f.detail, secrets=secrets)   # a refusal is Pulsar's own text
    return head + "\n\n" + body + ("\n\nUpstream said: " + short if short else "")


def stated_limit(text: str) -> int:
    """The context limit an upstream overflow error names, or 0."""
    m = _CTX_STATED.search(text or "")
    return int(m.group(1) or m.group(2)) if m else 0


def overflow_message(body: Dict[str, Any], context: int, detail: str = "") -> str:
    """Claude Code's own wording for a prompt that no longer fits, which makes it compact. The maximum is the
    target's known window, else the limit the upstream error (`detail`) names, else just under the prompt's
    estimated size."""
    est = T.estimate_tokens(body)
    limit = context or stated_limit(detail) or max(est - 1, 1)
    return "prompt is too long: %d tokens > %d maximum" % (max(est, limit + 1), limit)


def _overflow_text(f: "Failure", body: Dict[str, Any], context: int, key: str) -> str:
    """The prompt-too-long message for an overflow Failure: the upstream's own words when it already speaks
    Anthropic's form ("prompt is too long: N tokens > M maximum", which Claude Code reads to size its
    compaction), else the same message built from the request (another vendor's wording)."""
    text = _short(f.detail, 500, secrets=[key])
    if text.lower().startswith("prompt is too long"):
        return text
    return overflow_message(body, context, f.detail)      # the limit the upstream states, when it states one


# ---------------------------------------------------------------------------
# Loop guard
# ---------------------------------------------------------------------------

def _sig(block: Any) -> str:
    return hashlib.sha1(json.dumps(block, sort_keys=True, default=str).encode()).hexdigest()


def _human_turn(m: Any) -> bool:
    """A user message that carries something a person typed (not only tool results or system reminders)."""
    if not isinstance(m, dict) or m.get("role") != "user":
        return False
    content = m.get("content")
    blocks = content if isinstance(content, list) else [{"type": "text", "text": str(content or "")}]
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "text":
            text = str(b.get("text") or "").strip()
            if text and not text.startswith("<system-reminder>"):
                return True
    return False


def _assistant_sig(m: Dict[str, Any]) -> str:
    """An assistant message's text and tool calls (tool ids left out: they are fresh every time)."""
    content = m.get("content")
    blocks = content if isinstance(content, list) else [{"type": "text", "text": str(content)}]
    return _sig([{"t": b.get("type"), "n": b.get("name"), "i": b.get("input"), "x": b.get("text")}
                 for b in blocks if isinstance(b, dict) and b.get("type") in ("text", "tool_use")])


def _results_sig(m: Dict[str, Any]) -> str:
    """A user message's tool results (content and error flag), or "" when it carries none."""
    content = m.get("content")
    res = [{"c": b.get("content"), "e": b.get("is_error")} for b in (content if isinstance(content, list) else ())
           if isinstance(b, dict) and b.get("type") == "tool_result"]
    return _sig(res) if res else ""


def _said_sig(m: Dict[str, Any]) -> str:
    """What a human turn says: its typed text, tool results and attachments, without system reminders."""
    content = m.get("content")
    blocks = content if isinstance(content, list) else [{"type": "text", "text": str(content or "")}]
    said: List[Dict[str, Any]] = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        kind = b.get("type")
        if kind == "text":
            text = str(b.get("text") or "").strip()
            if text and not text.startswith("<system-reminder>"):
                said.append({"t": "text", "x": text})
        elif kind == "tool_result":
            said.append({"t": kind, "c": b.get("content"), "e": b.get("is_error")})
        else:
            said.append({"t": kind, "s": b.get("source")})
    return _sig(said)


def detect_loop(body: Dict[str, Any], repeats: int = 4) -> Optional[str]:
    """A model stuck repeating the identical step. Returns a description or None.

    Two forms of the identical step, `repeats` times in a row:
    * within the current turn (the tail since the last human message): the same tool call with the same
      result, or the same text;
    * across turns: the same reply to the same message (a script, a Stop hook or a person sending the
      identical text back while the model answers it identically every time).
    The same short answer to different questions (several "OK." in a conversation) is neither.
    Only the user/assistant conversation counts: for a model id it does not know, Claude Code adds a role:"system"
    message after every human message ([user, system, assistant, user, system, ...]), which says nothing about
    what the model did.
    """
    assistant: List[str] = []
    results: List[str] = []
    messages = [m for m in body.get("messages") or []
                if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
    humans = [i for i, m in enumerate(messages) if _human_turn(m)]
    start = humans[-1] if humans else 0
    for m in messages[start:]:
        if m.get("role") == "assistant":
            assistant.append(_assistant_sig(m))
        elif m.get("role") == "user":
            res = _results_sig(m)
            if res:
                results.append(res)
    if len(assistant) >= repeats and len(set(assistant[-repeats:])) == 1:
        if len(results) < repeats - 1 or len(set(results[-(repeats - 1):])) == 1:
            return "the model repeated the identical step %d times in a row" % repeats
    # Across turns: the request answers a human message, and the last `repeats` exchanges (everything the model
    # did after one human message, then the next human message) are identical.
    if len(humans) > repeats and humans[-1] == len(messages) - 1:
        bounds = humans[-(repeats + 1):]
        exchanges = set()
        for a, b in zip(bounds, bounds[1:]):
            between = messages[a + 1:b]
            if not any(m.get("role") == "assistant" for m in between):
                return None
            steps = [_assistant_sig(m) if m.get("role") == "assistant" else _results_sig(m) for m in between]
            exchanges.add((_sig(steps), _said_sig(messages[b])))
        if len(exchanges) == 1:
            return "the model gave the identical reply to the same message %d times in a row" % repeats
    return None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class Engine:
    def __init__(self, stats: Stats, key_source: Callable[[str], str], home: Path,
                 transport: Optional[httpx.AsyncBaseTransport] = None, tuning: Optional[Tuning] = None) -> None:
        self.stats = stats
        self.key_source = key_source
        self.home = home
        self.transport = transport
        self.tuning = tuning or Tuning()
        self.breakers = Breakers()
        self._clients: Dict[str, httpx.AsyncClient] = {}
        self._heals: Dict[str, Dict[str, Dict[str, Any]]] = {}
        # The id Claude Code keeps sending after a switch is not a pick: plan[0] answers (see ModelPicks).
        self.picks = ModelPicks()

    # -- plumbing ------------------------------------------------------------
    def client(self, url: str) -> httpx.AsyncClient:
        host = _host(url)
        c = self._clients.get(host)
        if c is None:
            # A corporate proxy cannot reach a model server on this machine or LAN, so local hosts go direct.
            local = netcfg.is_local_host(host) and os.environ.get("PULSAR_PROXY_LOCAL") != "1"
            kw: Dict[str, Any] = dict(transport=self.transport, follow_redirects=False, verify=netcfg.ssl_context(),
                                      limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
                                      timeout=httpx.Timeout(connect=15.0, read=600.0, write=60.0, pool=30.0))
            try:
                c = httpx.AsyncClient(trust_env=not local, **kw)
            except (ImportError, ValueError):
                # ALL_PROXY=socks5://... without socksio, or a scheme httpx cannot use (socks4://):
                # keep any HTTP(S) proxy, skip the unusable one.
                proxy = "" if local else netcfg.env_http_proxy()
                c = httpx.AsyncClient(trust_env=False, proxy=proxy or None, **kw)
            self._clients[host] = c
        return c

    async def aclose(self) -> None:
        for c in self._clients.values():
            await c.aclose()

    @staticmethod
    def _diag(r: Resolved, f: Failure, limit: int = 120) -> str:
        """A failure as recorded in stats and on the bus: redacted, and without endpoint or other URLs."""
        text = _short(f.detail, limit * 2, secrets=[r.key]) or f.kind
        return scrub_urls(text, r.provider.base_url, r.provider.native_base(), r.url)[:limit]

    def _heal_file(self, profile: str) -> Path:
        safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", profile)
        return self.home / "cache" / ("heals-" + safe + ".json")

    def heal_get(self, profile: str, model: str) -> Dict[str, Any]:
        """Adjustments learned for this model; they expire after a week so provider fixes are picked up."""
        if profile not in self._heals:
            try:
                data = json.loads(self._heal_file(profile).read_text(encoding="utf-8"))
                self._heals[profile] = data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                self._heals[profile] = {}
        entry = self._heals[profile].get(model)
        if not isinstance(entry, dict) or time.time() - float(entry.get("_t") or 0) > HEAL_TTL:
            return {}
        return {k: v for k, v in entry.items() if k != "_t"}

    def heal_reset(self) -> int:
        """Forget every learned adjustment."""
        n = 0
        for f in (self.home / "cache").glob("heals-*.json"):
            try:
                f.unlink()
                n += 1
            except OSError:
                pass
        self._heals.clear()
        return n

    def heal_put(self, profile: str, model: str, heal: Dict[str, Any]) -> None:
        self.heal_get(profile, model)
        self._heals.setdefault(profile, {})[model] = dict(heal, _t=int(time.time()))
        try:
            from pulsar.config import write_private
            write_private(self._heal_file(profile), json.dumps(self._heals[profile], indent=1))
        except OSError:
            pass

    # -- plans ---------------------------------------------------------------
    def resolve(self, t: Target, requested: str, primary: bool, allow_http: bool = False) -> Resolved:
        prov = provider_for(t)
        if prov.route == "cloud":
            raise KeyError("%s is served by Claude Code itself, not Core" % prov.name)
        if _placeholder(prov):
            # A stale plan or a /model pick can still name an Azure profile saved without its resource URL:
            # the key never goes to the registry's YOUR-RESOURCE host (plan() skips the target).
            raise KeyError("%s has no resource URL saved" % prov.name)
        req = clean_model(requested)
        if prov.id == "anthropic":
            model = req if req.lower().startswith("claude-") else (t.model or req)
        elif primary and req and not req.lower().startswith("claude-"):
            model = req                                  # live switch from Claude Code's /model
        else:
            model = t.model or prov.fallback_model
        return Resolved(t, prov, self.key_source(t.profile), model, prov.native_for(model), allow_http)

    def owner_of(self, model: str, sess: Session) -> Optional[Target]:
        """Which connected profile serves `model` (a pick from Claude Code's /model list); the primary
        when no plan target and no cached catalog lists it."""
        found = self.find_owner(model, sess)
        if found is not None:
            return found
        return sess.plan[0] if sess.plan else None

    def find_owner(self, model: str, sess: Session) -> Optional[Target]:
        """The plan target, or else the connected profile whose cached catalog lists `model`; None if none does."""
        for t in sess.plan:
            if t.model == model:
                return t
        candidates: List[Target] = list(sess.plan)
        try:
            from pulsar import config as K
            st = K.load()
            for pid, prof in st.profiles.items():
                if not any(t.profile == pid for t in candidates):
                    candidates.append(Target(pid, prof.provider, prof.model, prof.base_url, prof.native, prof.context))
        except Exception:
            pass
        for t in candidates:
            cached = C.read_cache(t.profile, ttl=10 * 365 * 86400) or []
            m = next((x for x in cached if x.id == model), None)
            if m is not None:
                return Target(t.profile, t.provider, model, t.base_url, t.native, m.context or t.context)
        return None

    def plan(self, sess: Session, requested: str) -> List[Resolved]:
        out: List[Resolved] = []
        seen = set()
        # A session with Pilot armed never relays: its screenshots go only to the model the user chose.
        relay = sess.flags.get("relay", True) and not sess.flags.get("pilot")
        base_plan = list(sess.plan)
        requested = self.picks.explicit(sess, requested)   # "" unless this is a real /model pick
        req = clean_model(requested)
        if base_plan and req and not req.lower().startswith("claude-") and req != base_plan[0].model \
                and base_plan[0].provider != "anthropic":
            owner = self.owner_of(req, sess)
            if owner is not None and owner.profile != base_plan[0].profile:
                base_plan = [Target(owner.profile, owner.provider, req, owner.base_url, owner.native, owner.context)] + base_plan
        allow_http = http_opt_in(sess)
        for i, t in enumerate(base_plan if relay else base_plan[:1]):
            try:
                r = self.resolve(t, requested, primary=(i == 0), allow_http=allow_http)
            except KeyError:
                continue
            if r.key_id in seen:
                continue
            seen.add(r.key_id)
            out.append(r)
        return out

    def order(self, plan: List[Resolved]) -> List[Resolved]:
        """Healthy targets first; open breakers last (never dropped, so we always try something)."""
        healthy = [r for r in plan if self.breakers.allow(r.key_id)]
        cooling = [r for r in plan if not self.breakers.allow(r.key_id)]
        return healthy + cooling

    # -- upstream open ----------------------------------------------------------
    def _headers(self, r: Resolved, incoming: Dict[str, str]) -> Dict[str, str]:
        h = {"Content-Type": "application/json", "User-Agent": "pulsarcode/" + __version__,
             "Accept": "text/event-stream, application/json"}
        if r.native:
            h["anthropic-version"] = incoming.get("anthropic-version") or "2023-06-01"
            beta = incoming.get("anthropic-beta")
            if beta and r.provider.id == "anthropic":
                h["anthropic-beta"] = beta
        h.update(P.auth_headers(r.provider, r.key, native=r.native))
        if r.provider.id == "openrouter":
            h["HTTP-Referer"] = "https://github.com/PulsarOSDevTeam/pulsarcode"
            h["X-Title"] = "Pulsar Code"
        return h

    def _payload(self, r: Resolved, body: Dict[str, Any], stream: bool, heal: Dict[str, Any]) -> Dict[str, Any]:
        if r.native:
            # Our own carried-reasoning signatures mean nothing to an Anthropic upstream.
            out = {k: v for k, v in R.strip_carried(body).items() if k not in heal.get("drop_fields", [])}
            out["model"] = r.model or body.get("model")
            out["stream"] = stream
            return out
        fmt = ((body.get("output_config") or {}).get("format") or {}) if isinstance(body.get("output_config"), dict) else {}
        schema = fmt.get("schema") if isinstance(fmt, dict) and fmt.get("type") == "json_schema" else None
        kw: Dict[str, Any] = {"stream": stream, "max_output": r.provider.max_output,
                              "usage_in_stream": r.provider.usage_in_stream, "strict_schema": r.provider.strict_schema,
                              "heal": heal, "vision": not heal.get("no_vision")}
        if r.wire == "responses":
            req = R.build_request(body, r.model, r.provider.id, **kw)
            if isinstance(schema, dict) and not heal.get("drop_response_format"):
                req["text"] = {"format": {"type": "json_schema", "name": "response", "schema": schema, "strict": False}}
            return req
        style = r.provider.tool_id_style_for(r.model)
        if style != "default" and _accepts(T.build_request, "tool_id_style"):
            kw["tool_id_style"] = style
        req = T.build_request(body, r.model, r.provider.id, **kw)
        if isinstance(schema, dict) and not heal.get("drop_response_format"):
            # Claude Code asks for structured output (e.g. session titles) through output_config.format.
            req["response_format"] = {"type": "json_schema",
                                      "json_schema": {"name": "response", "schema": schema, "strict": False}}
        return req

    def _heal(self, r: Resolved, status: int, text: str, req: Dict[str, Any], heal: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not r.native:
            fmt_key = "text" if r.wire == "responses" else "response_format"
            if status in (400, 422) and fmt_key in req and not heal.get("drop_response_format") and \
                    re.search(r"response_format|json_schema|structured|text\.format", text or "", re.I):
                return dict(heal, drop_response_format=True)
            if r.wire == "responses":
                return R.heal_from_error(status, text, req, heal)
            return T.heal_from_error(status, text, req, heal)
        if status not in (400, 422):
            return None
        drop = list(heal.get("drop_fields", []))
        low = text.lower()
        optional = ("context_management", "output_config", "metadata", "thinking", "service_tier", "container",
                    "mcp_servers", "top_k", "betas", "speed", "inference_geo")
        for name in optional:
            if name in low and name in req and name not in drop:
                drop.append(name)
                return dict(heal, drop_fields=drop)
        return None

    async def open(self, r: Resolved, body: Dict[str, Any], incoming: Dict[str, str], stream: bool,
                   ) -> Tuple[httpx.Response, Dict[str, Any]]:
        """Open one target, healing 400s. Raises Failure. Returns (response, request_sent)."""
        refusal = transport_refusal(r)
        if refusal:
            raise Failure("refused", detail=refusal)
        reasoning = C.is_reasoning(r.model or "")
        timeout = self.tuning.header_timeout_reasoning if reasoning else self.tuning.header_timeout
        heal = self.heal_get(r.target.profile, r.model)
        once: Dict[str, Any] = {}              # per-request adjustments, never persisted
        for _ in range(self.tuning.max_heals + 1):
            req = self._payload(r, body, stream, dict(heal, **once))
            try:
                hr = self.client(r.url).build_request("POST", r.url, headers=self._headers(r, incoming), json=req)
                resp = await asyncio.wait_for(self.client(r.url).send(hr, stream=True), timeout=timeout)
            except asyncio.TimeoutError:
                raise Failure("header_timeout")
            except httpx.HTTPError as exc:
                raise Failure("network", detail=redact("%s: %s" % (type(exc).__name__, exc), [r.key]))
            if resp.status_code == 200:
                return resp, req
            try:
                head = await asyncio.wait_for(_read_body(resp, MAX_ERROR_BODY, truncate=True), ERROR_BODY_SECONDS)
            except (asyncio.TimeoutError, httpx.HTTPError):
                head = b""                  # the error body broke off or stalled: its status still says enough
            finally:
                await resp.aclose()
            raw = redact(head.decode("utf-8", errors="replace")[:20000], [r.key])
            if 300 <= resp.status_code < 400:
                where = _host(resp.headers.get("location", "")) or "?"
                raise Failure("http", resp.status_code, "upstream redirected to another location (%s; not followed: "
                              "your key stays put)" % scrub_urls(redact(where, [r.key])))
            # "maximum context length is N ... M input tokens": a smaller max_tokens may fit (context_room).
            sized = resp.status_code in (400, 413, 422) and bool(_CTX_MAX.search(raw) and _CTX_IN.search(raw))
            if sized:
                asked = int(req.get("max_tokens") or req.get("max_completion_tokens") or req.get("max_output_tokens")
                            or body.get("max_tokens") or 0)
                room = context_room(raw, asked)
                if room and once.get("max_tokens_cap") != room:
                    once["max_tokens_cap"] = room   # the prompt fits; only the answer budget was too big
                    continue
            if sized or T.context_overflow(resp.status_code, raw):
                raise Failure("http", resp.status_code, raw, overflow=True)   # a real overflow: Claude Code compacts
            fix = self._heal(r, resp.status_code, raw, req, heal)
            if fix is not None and fix != heal:
                heal = fix
                self.heal_put(r.target.profile, r.model, heal)
                self.stats.emit("heal", profile=r.target.profile, model=r.model, fix=sorted(heal))
                continue
            ra = 0.0
            try:
                ra = float(resp.headers.get("retry-after", "") or 0)
            except ValueError:
                pass
            raise Failure("http", resp.status_code, raw, ra)
        raise Failure("http", 400, "too many automatic adjustments")

    # -- streaming ------------------------------------------------------------
    async def stream(self, sess: Session, body: Dict[str, Any], incoming: Dict[str, str]) -> AsyncIterator[str]:
        rid = uuid.uuid4().hex[:10]
        plan = self.order(self.plan(sess, str(body.get("model") or "")))
        if not plan:
            yield _text_stream("◉ Pulsar Code: this session has no usable route. Run `pulsarcode key`.", "pulsar")
            return
        first = plan[0]
        self.stats.begin(rid, sess.sid, first.model, first.provider.id, profile=first.target.profile)
        loop = detect_loop(body) if sess.flags.get("loopguard", True) else None
        if loop:
            self.stats.end(rid, ok=False, error="loop guard")
            self.stats.emit("loop", sid=sess.sid, detail=loop)
            yield _text_stream("◉ Pulsar Code stopped a runaway loop: %s.\nThe same action kept producing the same "
                               "result. Tell the model what to do differently, or switch model with `/model <id>`." % loop,
                               first.model)
            return

        committed: Optional[Tuple[Resolved, httpx.Response, Dict[str, Any]]] = None
        last: Optional[Failure] = None
        last_r = first
        idx = 0
        attempts_on_last = 0
        try:
            while idx < len(plan):
                r = plan[idx]
                last_r = r
                task = asyncio.ensure_future(self.open(r, body, incoming, True))
                try:
                    while not task.done():
                        done, _ = await asyncio.wait({task}, timeout=self.tuning.ping_interval)
                        if not done:
                            yield PING
                finally:
                    if not task.done():
                        task.cancel()
                try:
                    resp, sent = task.result()
                    self.breakers.ok(r.key_id)
                    committed = (r, resp, sent)
                    break
                except Failure as f:
                    last = f
                    if f.overflow:
                        self.stats.end(rid, ok=False, error="context overflow")
                        yield overflow_event(overflow_message(body, r.target.context, f.detail))
                        return
                    cool = self.breakers.fail(r.key_id, f)
                    if idx + 1 < len(plan):
                        nxt = plan[idx + 1]
                        self.stats.relay(rid, r.key_id, nxt.provider.id + "|" + nxt.model,
                                         f.kind if f.kind != "http" else "HTTP %d" % f.status,
                                         profile=nxt.target.profile)
                        idx += 1
                        continue
                    if f.transient and attempts_on_last < self.tuning.retries:
                        attempts_on_last += 1
                        wait = min(8.0, f.retry_after or 1.5 * (2 ** (attempts_on_last - 1)), cool)
                        end = time.time() + wait
                        while time.time() < end:
                            await asyncio.sleep(min(self.tuning.ping_interval, max(0.05, end - time.time())))
                            yield PING
                        continue
                    break
            if committed is None:
                assert last is not None
                self.stats.end(rid, ok=False, error=self._diag(last_r, last),
                               model=last_r.model, provider=last_r.provider.id,
                               profile=last_r.target.profile)
                yield _text_stream(notice(last_r.provider, last_r.model, last, idx + 1, secrets=[last_r.key],
                                          retried=attempts_on_last), last_r.model)
                return
            r, resp, sent = committed
            if r.native:
                async for chunk in self._pump_native(rid, r, resp, body):
                    yield chunk
            else:
                async for chunk in self._pump_adapter(rid, r, resp, body, sent):
                    yield chunk
        finally:
            if rid in self.stats.flights:
                self.stats.end(rid, ok=False, error="client went away")

    async def _pump_adapter(self, rid: str, r: Resolved, resp: httpx.Response, body: Dict[str, Any],
                            req: Optional[Dict[str, Any]] = None) -> AsyncIterator[str]:
        """Translate an OpenAI stream (chat-completions or Responses) into Anthropic SSE. `req`: the request sent."""
        thinking = body.get("thinking") or {}
        emit_thinking = isinstance(thinking, dict) and thinking.get("type") in ("enabled", "adaptive") \
            and thinking.get("display") != "omitted" and os.environ.get("PULSAR_SHOW_REASONING", "1") != "0"
        responses = r.wire == "responses"
        tr: Any = (R.ResponsesStreamTranslator(r.model, T.estimate_tokens(body), emit_thinking, tools=body.get("tools"))
                   if responses else _translator(r.model, body, emit_thinking, *rescue_flags(r, body, req)))
        # On the chat wire message_start is held back (pings may go first) until there is something to send: a
        # context overflow that arrives before it is then answered as a real HTTP 400 (OverflowEvent), which
        # Claude Code compacts on at once. After any SSE error event it re-sends the whole request without
        # streaming first, and only compacts on the 400 that plain() answers. The Responses wire still sends
        # message_start at once, so its overflow takes that longer way.
        head = "".join(T.sse(ev) for ev in tr.start())
        held = [] if responses else [head]
        if responses:
            yield head

        def out(text: str) -> str:
            return (held.pop() if held else "") + text

        def overflow(message: str) -> str:
            return overflow_event(message) if held else _error_event("invalid_request_error", message)

        reasoning = C.is_reasoning(r.model or "")
        window = self.tuning.first_token_timeout_reasoning if reasoning else self.tuning.first_token_timeout
        queue: "asyncio.Queue[Optional[str]]" = asyncio.Queue(maxsize=4096)
        failure: Dict[str, str] = {}
        queued = [0]                     # characters read ahead and still queued
        room = asyncio.Event()

        async def pump() -> None:
            try:
                async for line in _lines(resp):
                    while queued[0] > QUEUE_CHARS:
                        room.clear()
                        await room.wait()
                    queued[0] += len(line)
                    await queue.put(line)
            except Exception as exc:
                failure["detail"] = "%s: %s" % (type(exc).__name__, exc)
            finally:
                await queue.put(None)

        pumper = asyncio.ensure_future(pump())
        started = time.time()
        end_note: Optional[str] = None
        before = 0
        data_seen = False
        stray: List[str] = []          # lines that are not SSE data: a provider that ignored stream=true
        stray_size = 0
        try:
            while True:
                if not tr.active:
                    remaining = window - (time.time() - started)
                    if remaining <= 0:
                        end_note = notice(r.provider, r.model, Failure("first_token"), secrets=[r.key])
                        break
                    wait = min(remaining, self.tuning.ping_interval)
                else:
                    wait = self.tuning.ping_interval * 6
                try:
                    line = await asyncio.wait_for(queue.get(), timeout=wait)
                except asyncio.TimeoutError:
                    yield PING
                    continue
                if line is not None:
                    queued[0] -= len(line)
                    if queued[0] <= QUEUE_CHARS:
                        room.set()
                if line is None:
                    if not data_seen:
                        fb = self._fallback(r, "\n".join(stray), body, failure.get("detail", ""), req)
                        if isinstance(fb, Failure) and fb.overflow and not tr.visible and not tr.tools_emitted:
                            # An overflow as a 200 JSON error body: Claude Code compacts, as for the SSE form.
                            yield overflow(overflow_message(body, r.target.context))
                            self.stats.end(rid, ok=False, error="context overflow", model=r.model,
                                           provider=r.provider.id, profile=r.target.profile)
                            return
                        if isinstance(fb, dict):
                            yield out("".join(_block_events(fb.get("content") or [], 0)))
                            u = fb.get("usage") or {}
                            yield T.sse(("message_delta", {"type": "message_delta", "delta": {
                                "stop_reason": fb.get("stop_reason") or "end_turn", "stop_sequence": None},
                                "usage": {"output_tokens": int(u.get("output_tokens") or 1)}}))
                            yield T.sse(("message_stop", {"type": "message_stop"}))
                            self.stats.end(rid, ok=True, tokens_in=int(u.get("input_tokens") or 0),
                                           tokens_out=int(u.get("output_tokens") or 0), model=r.model,
                                           provider=r.provider.id, profile=r.target.profile)
                            return
                        if fb.overflow:
                            yield _error_event("invalid_request_error", overflow_message(body, r.target.context,
                                                                                         fb.detail))
                            self.stats.end(rid, ok=False, error="context overflow")
                            return
                        end_note = notice(r.provider, r.model, fb, secrets=[r.key])
                    elif failure:
                        end_note = notice(r.provider, r.model, Failure("interrupted", detail=redact(failure["detail"], [r.key])),
                                          secrets=[r.key])
                    elif responses and not tr.done:
                        end_note = notice(r.provider, r.model, Failure("interrupted", detail="stream ended early"))
                    break
                line = line.strip()
                if not line.startswith("data:"):
                    if not data_seen and line and stray_size < MAX_FALLBACK_BODY:
                        stray.append(line)
                        stray_size += len(line)
                    continue
                data_seen = True
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                f: Optional[Failure] = None
                try:
                    if responses:
                        events = tr.feed(chunk)
                        if tr.failure is not None:
                            f = Failure("http", int(tr.failure.get("status") or 500),
                                        redact(str(tr.failure.get("detail") or ""), [r.key]))
                    elif isinstance(chunk, dict) and chunk.get("error"):
                        err = chunk["error"]
                        f = Failure("http", _error_status(err, 500), redact(json.dumps(err), [r.key]))
                        events = []
                    else:
                        events = tr.feed(chunk)
                except Exception as exc:
                    # An event nobody foresaw (or a translator bug) ends the reply with a notice; tr.finish
                    # below still closes the message.
                    end_note = notice(r.provider, r.model, Failure(
                        "interrupted", detail="an unreadable event (%s)" % type(exc).__name__), secrets=[r.key])
                    break
                for ev in events:
                    yield out(T.sse(ev))
                if tr.out_chars != before:
                    self.stats.token(rid, tr.out_chars - before)
                    before = tr.out_chars
                if f is not None:
                    if f.overflow and not tr.visible and not tr.tools_emitted:
                        yield overflow(overflow_message(body, r.target.context, f.detail))
                        self.stats.end(rid, ok=False, error="context overflow", model=r.model,
                                       provider=r.provider.id, profile=r.target.profile)
                        return
                    end_note = notice(r.provider, r.model, f, secrets=[r.key])
                    if responses:
                        tr.failure = None   # the notice already says it
                    break
                if responses and tr.done:
                    break
        finally:
            pumper.cancel()
            try:
                await resp.aclose()
            except Exception:
                pass
        for ev in tr.finish(end_note):
            yield out(T.sse(ev))
        usage = getattr(tr, "usage_in", None)
        self.stats.end(rid, ok=end_note is None, tokens_in=usage or T.estimate_tokens(body),
                       tokens_out=tr.usage_out or max(1, tr.out_chars // 4),
                       cache_read=int(getattr(tr, "cache_read", 0) or 0), cost=float(getattr(tr, "cost", 0.0) or 0.0),
                       error="" if end_note is None else "stream ended with notice", model=r.model,
                       provider=r.provider.id, profile=r.target.profile)

    def _fallback(self, r: Resolved, raw: str, body: Dict[str, Any], transport_error: str = "",
                  req: Optional[Dict[str, Any]] = None) -> Any:
        """A 200 that carried no SSE: a whole message (returned as an Anthropic message) or a Failure.
        `req`: the request sent (see rescue_flags)."""
        raw = raw.strip()
        if not raw:
            if transport_error:
                return Failure("interrupted", detail=redact(transport_error, [r.key]))
            return Failure("http", 502, "the provider answered 200 with an empty body")
        try:
            data = json.loads(raw)
        except ValueError:
            # A web page (a wrong base URL, a captive portal, a proxy's landing page) is not an outage.
            kind = "not_api" if re.search(r"(?i)<(!doctype html|html|head|body)\b", raw[:4096]) else "http"
            return Failure(kind, 502, "the provider answered 200 with something that is not a model stream: "
                           + redact(raw[:300], [r.key]))
        if isinstance(data, dict) and data.get("error") and not (data.get("choices") or data.get("output")
                                                                 or data.get("content")):
            return Failure("http", _error_status(data["error"], 502), redact(json.dumps(data), [r.key]))
        if r.wire == "responses" and isinstance(data, dict) and data.get("status") == "failed" \
                and not _answered(data):
            # A failed Responses body with nothing but reasoning: its error is upstream text (it can echo the
            # key or carry terminal escapes), so it becomes a redacted notice, never the model's reply.
            status, detail = R.error_of({"type": "response.failed", "response": data}) or (502, "")
            return Failure("http", status, redact(detail, [r.key]))
        try:
            if r.native and isinstance(data, dict) and (data.get("type") == "message" or (
                    isinstance(data.get("content"), list) and data.get("role") == "assistant")):
                return data
            if not r.native and isinstance(data, dict) and (data.get("choices") or data.get("output")):
                rescue, rescue_reasoning = rescue_flags(r, body, req)
                return _to_message(data, r.model, body, r.wire, secrets=[r.key], rescue=rescue,
                                   rescue_reasoning=rescue_reasoning)
        except Exception as exc:   # a malformed body must never kill the stream
            return Failure("http", 502, "unreadable provider answer (%s)" % type(exc).__name__)
        return Failure("http", 502, "the provider answered 200 with an unexpected body: " + redact(raw[:300], [r.key]))

    async def _pump_native(self, rid: str, r: Resolved, resp: httpx.Response,
                           body: Optional[Dict[str, Any]] = None) -> AsyncIterator[str]:
        """Forward Anthropic SSE verbatim; if the upstream dies mid-stream, close it out cleanly.
        `body` is the request Claude Code sent (it sizes a context-overflow error)."""
        open_block: Optional[int] = None
        max_index = -1
        started = False
        stopped = False
        tokens_in = tokens_out = cache_read = 0
        event = ""
        buf: List[str] = []
        held = 0                       # characters in buf (one event, up to its blank line)
        failure = ""
        mode = ""                      # "sse" once an SSE line arrives, "body" for a plain JSON answer
        body_lines: List[str] = []
        body_size = 0
        upstream_error: Optional[Failure] = None
        try:
            async for line in _lines(resp):
                if not mode and line.strip():
                    mode = "sse" if line.startswith(("event:", "data:", ":")) else "body"
                if mode != "sse":
                    if body_size >= MAX_FALLBACK_BODY:
                        break              # not a stream, and already larger than any answer: stop reading
                    body_lines.append(line)
                    body_size += len(line)
                    continue
                buf.append(line)
                held += len(line)
                if held > MAX_EVENT:
                    raise ValueError("the provider sent an event longer than %d MiB" % max(1, MAX_EVENT >> 20))
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    try:
                        data = json.loads(line[5:].strip())
                    except ValueError:
                        data = {}
                    et = data.get("type", event) if isinstance(data, dict) else event
                    if et == "error":
                        # Never forwarded as is: it can echo the key, and Claude Code would retry the whole
                        # request without streaming. It becomes a notice and a cleanly ended message.
                        err = data.get("error") if isinstance(data, dict) else None
                        etype = str(err.get("type") or "") if isinstance(err, dict) else ""
                        upstream_error = Failure("http", _ERROR_STATUS.get(etype, 500),
                                                 redact(json.dumps(err if err is not None else data), [r.key]))
                        buf = []
                        break
                    # Bookkeeping only (the event itself is forwarded as is): no field shape may cut the reply.
                    ev = _obj(data)
                    if et == "message_start":
                        started = True
                        u = _obj(_obj(ev.get("message")).get("usage"))
                        tokens_in = _count(u.get("input_tokens"))
                        cache_read = _count(u.get("cache_read_input_tokens"))
                    elif et == "content_block_start":
                        open_block = _count(ev.get("index"))
                        max_index = max(max_index, open_block)
                    elif et == "content_block_stop":
                        open_block = None
                    elif et == "content_block_delta":
                        d = _obj(ev.get("delta"))
                        n = len(str(d.get("text") or d.get("thinking") or d.get("partial_json") or ""))
                        if n:
                            self.stats.token(rid, n)
                    elif et == "message_delta":
                        tokens_out = _count(_obj(ev.get("usage")).get("output_tokens")) or tokens_out
                    elif et == "message_stop":
                        stopped = True
                if line == "":
                    yield "\n".join(buf) + "\n"
                    buf, held = [], 0
            if buf:
                yield "\n".join(buf) + "\n\n"
        except Exception as exc:
            failure = "%s: %s" % (type(exc).__name__, exc)
        finally:
            try:
                await resp.aclose()
            except Exception:
                pass
        if mode != "sse":
            fb = self._fallback(r, "\n".join(body_lines), {}, failure)
            if isinstance(fb, Failure) and fb.overflow:
                # A context overflow as a 200 JSON error body: nothing was sent yet, so it becomes Anthropic's
                # prompt-too-long error (a real HTTP 400 from Core's server), exactly as the SSE form does.
                yield overflow_event(_overflow_text(fb, body or {}, r.target.context, r.key))
                self.stats.end(rid, ok=False, error="context overflow", model=r.model, provider=r.provider.id,
                               profile=r.target.profile)
                return
            if isinstance(fb, dict):
                u = _obj(fb.get("usage"))
                yield _message_stream(fb)
                self.stats.end(rid, ok=True, tokens_in=_count(u.get("input_tokens")),
                               tokens_out=_count(u.get("output_tokens")), model=r.model, provider=r.provider.id,
                               profile=r.target.profile)
            else:
                yield _text_stream(notice(r.provider, r.model, fb, secrets=[r.key]), r.model)
                self.stats.end(rid, ok=False, error=self._diag(r, fb), model=r.model, provider=r.provider.id,
                               profile=r.target.profile)
            return
        if upstream_error is not None and not stopped:
            if upstream_error.overflow and not started:
                yield overflow_event(_overflow_text(upstream_error, body or {}, r.target.context, r.key))
                self.stats.end(rid, ok=False, error="context overflow", model=r.model, provider=r.provider.id,
                               profile=r.target.profile)
                return
            note = notice(r.provider, r.model, upstream_error, secrets=[r.key])
            failure = self._diag(r, upstream_error)
        else:
            note = notice(r.provider, r.model, Failure("interrupted", detail=redact(failure, [r.key]) or "stream ended early"),
                          secrets=[r.key])
        if not stopped:
            if not started:
                yield _text_stream(note, r.model)
            else:
                if open_block is not None:
                    yield T.sse(("content_block_stop", {"type": "content_block_stop", "index": open_block}))
                i = max_index + 1
                yield T.sse(("content_block_start", {"type": "content_block_start", "index": i,
                                                     "content_block": {"type": "text", "text": ""}}))
                yield T.sse(("content_block_delta", {"type": "content_block_delta", "index": i,
                                                     "delta": {"type": "text_delta", "text": "\n\n" + note}}))
                yield T.sse(("content_block_stop", {"type": "content_block_stop", "index": i}))
                yield T.sse(("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn",
                                                                                   "stop_sequence": None},
                                               "usage": {"output_tokens": tokens_out}}))
                yield T.sse(("message_stop", {"type": "message_stop"}))
        self.stats.end(rid, ok=stopped, tokens_in=tokens_in, tokens_out=tokens_out, cache_read=cache_read,
                       error="" if stopped else (scrub_urls(redact(failure, [r.key]), r.provider.base_url, r.url)
                                                 or "ended early"), model=r.model, provider=r.provider.id,
                       profile=r.target.profile)

    # -- non-streaming -----------------------------------------------------------
    async def plain(self, sess: Session, body: Dict[str, Any], incoming: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
        rid = uuid.uuid4().hex[:10]
        plan = self.order(self.plan(sess, str(body.get("model") or "")))
        if not plan:
            return 200, T.text_message("◉ Pulsar Code: this session has no usable route. Run `pulsarcode key`.", "pulsar")
        self.stats.begin(rid, sess.sid, plan[0].model, plan[0].provider.id, profile=plan[0].target.profile)
        loop = detect_loop(body) if sess.flags.get("loopguard", True) else None
        if loop:
            self.stats.end(rid, ok=False, error="loop guard")
            return 200, T.text_message("◉ Pulsar Code stopped a runaway loop: %s." % loop, plan[0].model)
        last: Optional[Failure] = None
        last_r = plan[0]
        tried = retried = 0
        for idx, r in enumerate(plan):
            last_r = r
            tried, retried = idx + 1, 0
            for attempt in range(self.tuning.retries + 1):
                retried = attempt
                try:
                    resp, req = await self.open(r, body, incoming, False)
                except Failure as f:
                    last = f
                    if f.overflow:
                        self.stats.end(rid, ok=False, error="context overflow")
                        return 400, {"type": "error", "error": {"type": "invalid_request_error",
                                                                "message": overflow_message(body, r.target.context,
                                                                                            f.detail)}}
                    self.breakers.fail(r.key_id, f)
                    if idx + 1 < len(plan) or not f.transient or attempt >= self.tuning.retries:
                        break
                    await asyncio.sleep(min(8.0, f.retry_after or 1.5 * (2 ** attempt)))
                    continue
                patience = self.tuning.header_timeout_reasoning if C.is_reasoning(r.model or "") \
                    else self.tuning.header_timeout
                try:
                    raw = await asyncio.wait_for(_read_body(resp, MAX_REPLY_BODY), timeout=patience)
                except asyncio.TimeoutError:
                    fb = Failure("interrupted", detail="the answer did not finish arriving within %d s" % patience)
                except ValueError as exc:
                    fb = Failure("http", 502, str(exc))
                except httpx.HTTPError as exc:
                    fb = Failure("interrupted", detail=redact("%s: %s" % (type(exc).__name__, exc), [r.key]))
                else:
                    self.breakers.ok(r.key_id)
                    fb = self._fallback(r, raw.decode("utf-8", errors="replace"), body, req=req)
                finally:
                    await resp.aclose()
                if not isinstance(fb, dict):
                    last = fb
                    if fb.overflow:
                        self.stats.end(rid, ok=False, error="context overflow", model=r.model,
                                       provider=r.provider.id, profile=r.target.profile)
                        return 400, {"type": "error", "error": {"type": "invalid_request_error",
                                                                "message": overflow_message(body, r.target.context,
                                                                                            fb.detail)}}
                    self.breakers.fail(r.key_id, fb)
                    break
                data = fb
                if r.native:
                    if is_classifier(body) and r.provider.id != "anthropic":
                        data = normalize_verdict(data)   # an Anthropic-compatible vendor's model, not Claude
                    usage = _obj(data.get("usage"))
                    self.stats.end(rid, ok=True, tokens_in=_count(usage.get("input_tokens")),
                                   tokens_out=_count(usage.get("output_tokens")), model=r.model, provider=r.provider.id,
                                   profile=r.target.profile)
                    return 200, data
                msg = data
                if is_classifier(body):
                    msg = normalize_verdict(msg)
                u = msg.get("usage") or {}
                self.stats.end(rid, ok=True, tokens_in=int(u.get("input_tokens") or 0),
                               tokens_out=int(u.get("output_tokens") or 0), model=r.model, provider=r.provider.id,
                               profile=r.target.profile)
                return 200, msg
            if idx + 1 < len(plan) and last is not None:
                nxt = plan[idx + 1]
                self.stats.relay(rid, r.key_id, nxt.provider.id + "|" + nxt.model,
                                 last.kind if last.kind != "http" else "HTTP %d" % last.status,
                                 profile=nxt.target.profile)
        assert last is not None
        self.stats.end(rid, ok=False, error=self._diag(last_r, last), model=last_r.model, provider=last_r.provider.id,
                       profile=last_r.target.profile)
        return 200, T.text_message(notice(last_r.provider, last_r.model, last, tried, secrets=[last_r.key],
                                          retried=retried), last_r.model)

    # -- council -------------------------------------------------------------------
    async def ask(self, t: Target, prompt: str, system: str = "", max_tokens: int = 2048,
                  timeout: float = 180.0, allow_http: bool = False) -> Dict[str, Any]:
        """One-shot question to any target (used by the council tool). `timeout` covers the whole answer;
        `allow_http`: the asking session's http_opt_in."""
        started = time.time()
        try:
            r = self.resolve(t, "", primary=False, allow_http=allow_http)
        except KeyError as exc:      # a profile from a newer release (or edited by hand): one error answer
            return {"model": P.clean_text(t.model, 120), "provider": P.clean_text(t.provider, 60), "ok": False,
                    "seconds": 0.0, "error": P.clean_text(str(exc.args[0] if exc.args else exc), 200)
                    or "not reachable from Core"}
        body = {"model": r.model, "max_tokens": max_tokens, "stream": False,
                "messages": [{"role": "user", "content": prompt}]}
        if system:
            body["system"] = system

        async def answer() -> Dict[str, Any]:
            resp, sent = await self.open(r, body, {}, False)
            try:
                raw = await _read_body(resp, MAX_REPLY_BODY)
            finally:
                await resp.aclose()
            msg = self._fallback(r, raw.decode("utf-8", errors="replace"), body, req=sent)
            if not isinstance(msg, dict):
                raise msg
            text = redact(_text_of(msg.get("content")), [r.key])
            return {"model": r.model, "provider": r.provider.id, "ok": True, "text": text,
                    "seconds": round(time.time() - started, 1)}
        try:
            return await asyncio.wait_for(answer(), timeout=timeout)
        except Failure as f:
            return {"model": r.model, "provider": r.provider.id, "ok": False,
                    "error": _short(f.detail, 200, secrets=[r.key]) or f.kind, "seconds": round(time.time() - started, 1)}
        except Exception as exc:    # timeouts, network, malformed answers: one failed answer, never a 500
            return {"model": r.model, "provider": r.provider.id, "ok": False, "error": type(exc).__name__,
                    "seconds": round(time.time() - started, 1)}


# Anthropic error types (an SSE `event: error`) as the HTTP status they stand for.
_ERROR_STATUS = {"invalid_request_error": 400, "authentication_error": 401, "billing_error": 402,
                 "permission_error": 403, "not_found_error": 404, "request_too_large": 413,
                 "rate_limit_error": 429, "api_error": 500, "timeout_error": 504, "overloaded_error": 529}
# OpenAI-style error codes that name a status more precisely than their error type does.
_ERROR_CODE_STATUS = {"invalid_api_key": 401, "context_length_exceeded": 400, "rate_limit_exceeded": 429,
                      "model_not_found": 404}


def _error_status(err: Any, default: int) -> int:
    """The HTTP status an error object carried in a 200 body stands for: its numeric code or status, else
    its error code or Anthropic error type (the same mapping as an SSE `event: error`), else `default`."""
    if not isinstance(err, dict):
        return default
    code = err.get("code") or err.get("status")
    if str(code or "").isdecimal():          # isdigit() also accepts superscripts, which int() refuses
        return int(code)
    if isinstance(code, str) and code in _ERROR_CODE_STATUS:
        return _ERROR_CODE_STATUS[code]
    etype = err.get("type")
    if isinstance(etype, str) and etype in _ERROR_STATUS:
        return _ERROR_STATUS[etype]
    return default

_VERDICT = re.compile(r"<block>\s*(yes|no)\s*</block>", re.I)
# A verdict counts only as the very first thing in the reply: whitespace, then an optional code fence.
_LEADING_VERDICT = re.compile(r"\s*(?:```[A-Za-z0-9_+.-]*[ \t]*\n?\s*)?<block>\s*(yes|no)\s*</block>", re.I)
_CLOSING_FENCE = re.compile(r"\s*```\s*$")


def is_classifier(body: Dict[str, Any]) -> bool:
    """Claude Code's auto-mode safety check: a non-streaming request whose instructions demand <block>yes|no</block>."""
    if body.get("stream"):
        return False
    sys_text = T.system_text(body.get("system"))
    return "<block>" in sys_text and ("<block>no</block>" in sys_text or "<block>yes</block>" in sys_text)


def normalize_verdict(msg: Dict[str, Any]) -> Dict[str, Any]:
    """Put the model's verdict in the exact form Claude Code's parser reads (the start of the text).

    Only a <block>yes|no</block> tag at the very start of the reply (after whitespace and an optional
    code fence) is a verdict. Anything else passes through untouched, so an answer Claude Code cannot
    parse still makes it block the tool (its safe default): a leading "No" or "Yes" word is never read
    as a verdict, and neither is a tag further on, which may be quoted from the transcript under review.
    In doubt (a later tag that disagrees with the first), the verdict is to block.
    """
    text = _text_of(msg.get("content"), "text")
    m = _LEADING_VERDICT.match(text)
    if m is None:
        return msg
    verdict = m.group(1).lower()
    rest = _CLOSING_FENCE.sub("", text[m.end():])
    if verdict == "no" and any(v.lower() == "yes" for v in _VERDICT.findall(rest)):
        verdict, rest = "yes", ""
    rest = _VERDICT.sub("", rest).strip()
    if verdict == "yes" and "<category>" not in rest:
        rest = "<category>unspecified</category>" + rest
    new_text = "<block>%s</block>%s" % (verdict, rest if verdict == "yes" else "")
    out = dict(msg)
    out["content"] = [{"type": "text", "text": new_text}]
    out["stop_reason"] = "end_turn"
    return out


def _text_of(content: Any, kind: Optional[str] = None) -> str:
    """The text of an Anthropic message's content blocks (only blocks of type `kind` when given), whatever
    shape an upstream sent them in."""
    if not isinstance(content, list):
        return ""
    return "".join(str(b.get("text") or "") for b in content
                   if isinstance(b, dict) and (kind is None or b.get("type") == kind))


def _answered(data: Dict[str, Any]) -> bool:
    """A Responses body that carries model output Pulsar Code shows (text or a tool call), not only reasoning,
    an error, or message parts responses.to_anthropic_message does not render."""
    output = data.get("output")
    for item in output if isinstance(output, list) else []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call":
            return True
        parts = item.get("content")
        if item.get("type") == "message" and any(
                isinstance(p, dict) and p.get("type") in R.RENDERED_PARTS and (p.get("text") or p.get("refusal"))
                for p in (parts if isinstance(parts, list) else [])):
            return True
    return False


def rescue_flags(r: Resolved, body: Dict[str, Any],
                 req: Optional[Dict[str, Any]] = None) -> Tuple[Optional[bool], bool]:
    """(rescue, rescue_reasoning) for a chat-wire reply (see translate.StreamTranslator): what only Core knows
    about the host.

    Local servers and custom endpoints often run without a tool-call parser, and a request whose tools went into
    the prompt (`req`, the request sent, has no "tools": heal prompt_tools or alternate) is answered in text by
    any model, so their replies are searched whatever the model id. Elsewhere the model decides
    (translate.may_send_text_calls). Reasoning is searched only on local servers and custom endpoints, whose
    reasoning parsers can swallow a whole reply.
    """
    own_host = bool(r.provider.local) or r.provider.id == "custom"
    in_prompt = req is not None and "tools" not in req and bool(T.tools_to_openai(body.get("tools")))
    return (True if own_host or in_prompt else None), own_host


def _translator(model: str, body: Dict[str, Any], emit_thinking: bool, rescue: Optional[bool] = None,
                rescue_reasoning: bool = False) -> T.StreamTranslator:
    kwargs: Dict[str, Any] = {"input_tokens": T.estimate_tokens(body), "emit_thinking": emit_thinking}
    try:
        return T.StreamTranslator(model, tools=body.get("tools"), rescue=rescue,  # type: ignore[call-arg]
                                  rescue_reasoning=rescue_reasoning, **kwargs)
    except TypeError:
        return T.StreamTranslator(model, **kwargs)


def _to_message(data: Dict[str, Any], model: str, body: Dict[str, Any], wire: str = "chat", *,
                secrets: Any = (), rescue: Optional[bool] = None, rescue_reasoning: bool = False) -> Dict[str, Any]:
    """A non-streamed answer as an Anthropic message. `secrets` (the target's key) are masked in any provider
    error text the message quotes, whatever the key's shape; rescue and rescue_reasoning: see rescue_flags."""
    if wire == "responses":
        return R.to_anthropic_message(data, model, T.estimate_tokens(body), tools=body.get("tools"),
                                      secrets=[s for s in secrets if s])
    try:
        return T.to_anthropic_message(data, model, T.estimate_tokens(body),  # type: ignore[call-arg]
                                      tools=body.get("tools"), rescue=rescue, rescue_reasoning=rescue_reasoning)
    except TypeError:
        return T.to_anthropic_message(data, model, T.estimate_tokens(body))


def _accepts(fn: Callable[..., Any], name: str) -> bool:
    try:
        import inspect
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind == p.VAR_KEYWORD for p in params.values())


def _text_stream(text: str, model: str) -> str:
    tr = T.StreamTranslator(model)
    return "".join(T.sse(ev) for ev in tr.start() + tr.finish(text))


def _error_event(kind: str, message: str) -> str:
    return T.sse(("error", {"type": "error", "error": {"type": kind, "message": message}}))


class OverflowEvent(str):
    """The SSE error event for a context overflow. When it is the first thing Engine.stream() yields
    (nothing sent yet), Core's server answers a real HTTP 400 invalid_request_error instead, so Claude Code
    compacts at once rather than re-sending the whole prompt without streaming first."""

    message = ""


def overflow_event(message: str) -> OverflowEvent:
    ev = OverflowEvent(_error_event("invalid_request_error", message))
    ev.message = message
    return ev


def _block_events(content: List[Dict[str, Any]], start: int) -> List[str]:
    """SSE events for finished Anthropic content blocks (used when an upstream did not stream)."""
    out: List[str] = []
    i = start
    for b in content:
        if not isinstance(b, dict):
            continue
        kind = b.get("type")
        if kind == "text":
            out.append(T.sse(("content_block_start", {"type": "content_block_start", "index": i,
                                                      "content_block": {"type": "text", "text": ""}})))
            if b.get("text"):
                out.append(T.sse(("content_block_delta", {"type": "content_block_delta", "index": i,
                                                          "delta": {"type": "text_delta", "text": b["text"]}})))
        elif kind == "tool_use":
            out.append(T.sse(("content_block_start", {"type": "content_block_start", "index": i, "content_block": {
                "type": "tool_use", "id": b.get("id") or T.new_id("toolu_"), "name": b.get("name", ""), "input": {}}})))
            out.append(T.sse(("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {
                "type": "input_json_delta", "partial_json": json.dumps(b.get("input") or {})}})))
        elif kind == "thinking":
            out.append(T.sse(("content_block_start", {"type": "content_block_start", "index": i,
                                                      "content_block": {"type": "thinking", "thinking": ""}})))
            out.append(T.sse(("content_block_delta", {"type": "content_block_delta", "index": i,
                                                      "delta": {"type": "thinking_delta", "thinking": b.get("thinking", "")}})))
            out.append(T.sse(("content_block_delta", {"type": "content_block_delta", "index": i,
                                                      "delta": {"type": "signature_delta", "signature": b.get("signature", "")}})))
        elif kind == "redacted_thinking":
            out.append(T.sse(("content_block_start", {"type": "content_block_start", "index": i,
                                                      "content_block": {"type": "redacted_thinking", "data": b.get("data", "")}})))
        else:
            continue
        out.append(T.sse(("content_block_stop", {"type": "content_block_stop", "index": i})))
        i += 1
    if i == start:
        out += _block_events([{"type": "text", "text": ""}], start)
    return out


def _message_stream(msg: Dict[str, Any]) -> str:
    """A complete Anthropic message replayed as a stream."""
    head = {k: v for k, v in msg.items() if k not in ("content", "stop_reason", "stop_sequence")}
    head.update({"content": [], "stop_reason": None, "stop_sequence": None})
    u = _obj(msg.get("usage"))
    content = msg.get("content")
    parts = [T.sse(("message_start", {"type": "message_start", "message": head}))]
    parts += _block_events(content if isinstance(content, list) else [], 0)
    parts.append(T.sse(("message_delta", {"type": "message_delta", "delta": {
        "stop_reason": msg.get("stop_reason") or "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": _count(u.get("output_tokens")) or 1}})))
    parts.append(T.sse(("message_stop", {"type": "message_stop"})))
    return "".join(parts)
