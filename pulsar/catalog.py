# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Model catalogs: live discovery, caching, ranking, tiers and fuzzy search.

Every OpenAI-compatible provider exposes GET /models; Anthropic exposes
GET /v1/models. We fetch with the operator's key (only to that provider),
drop non-chat models (embeddings, speech, image, moderation, batch-only
variants, tool-less router entries), tag what we can infer (free,
reasoning, thinks, no-tools, context window, max output) and cache for six
hours. Providers with a public catalog (Provider.models_url) can be listed
before any key exists; that URL never receives a key.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from pulsar import __version__
from pulsar import providers as P
from pulsar.redact import redact

CACHE_TTL_S = 6 * 3600
CACHE_SCHEMA = 4
UA = "pulsarcode/%s (+https://github.com/PulsarOSDevTeam/pulsarcode)" % __version__

# Offline seed for NVIDIA NIM so the picker renders even with no network
# (the chat models of the live public catalog, October 2026).
NIM_CURRENT: Tuple[str, ...] = (
    "moonshotai/kimi-k3",
    "moonshotai/kimi-k2.6",
    "z-ai/glm-5.3",
    "z-ai/glm-5.3-flash",
    "deepseek-ai/deepseek-v4.1-flash",
    "nvidia/nemotron-3-ultra-550b-a55b",
    "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/nemotron-3.5-lightning-30b-a3b",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
    "nvidia/nemotron-nano-3-30b-a3b",
    "nvidia/llama-3.1-nemotron-ultra-253b-v1",
    "nvidia/llama-3.1-nemotron-70b-instruct",
    "nvidia/llama-3.1-nemotron-51b-instruct",
    "nvidia/nemotron-4-340b-instruct",
    "nvidia/llama3-chatqa-1.5-70b",
    "nvidia/mistral-nemo-minitron-8b-8k-instruct",
    "openai/gpt-oss-20b",
    "google/gemma-4-31b-it",
    "google/gemma-3-12b-it",
    "google/gemma-3-4b-it",
    "google/codegemma-1.1-7b",
    "meta/llama-3.2-90b-vision-instruct",
    "meta/llama-3.2-11b-vision-instruct",
    "meta/codellama-70b",
    "meta/muse-glimmer-30b",
    "mistralai/mistral-large-2-instruct",
    "mistralai/mistral-large",
    "mistralai/codestral-22b-instruct-v0.1",
    "mistralai/mistral-7b-instruct-v0.3",
    "mistralai/mixtral-8x22b-v0.1",
    "nv-mistralai/mistral-nemo-12b-instruct",
    "microsoft/phi-3.5-moe-instruct",
    "microsoft/phi-3-vision-128k-instruct",
    "deepseek-ai/deepseek-coder-6.7b-instruct",
    "ibm/granite-34b-code-instruct",
    "ibm/granite-8b-code-instruct",
    "ibm/granite-3.0-8b-instruct",
    "bigcode/starcoder2-15b",
    "poolside/laguna-xs-2.1",
    "writer/palmyra-creative-122b",
    "writer/palmyra-med-70b-32k",
    "writer/palmyra-fin-70b-32k",
    "ai21labs/jamba-1.5-large-instruct",
    "databricks/dbrx-instruct",
    "01-ai/yi-large",
    "zyphra/zamba2-7b-instruct",
)

# v1-era NIM ids, kept so v1 model aliases still migrate (config._legacy_alias_map).
_NIM_V1: Tuple[str, ...] = (
    "abacusai/dracarys-llama-3.1-70b-instruct",
    "bytedance/seed-oss-36b-instruct",
    "deepseek-ai/deepseek-v4-flash",
    "deepseek-ai/deepseek-v4-pro",
    "google/codegemma-7b",
    "google/gemma-2-2b-it",
    "meta/llama2-70b",
    "meta/llama-3.1-8b-instruct",
    "meta/llama-3.1-70b-instruct",
    "meta/llama-3.2-1b-instruct",
    "meta/llama-3.2-3b-instruct",
    "meta/llama-3.3-70b-instruct",
    "meta/llama-4-maverick-17b-128e-instruct",
    "microsoft/phi-4-mini-instruct",
    "microsoft/phi-4-multimodal-instruct",
    "minimaxai/minimax-m2.7",
    "mistralai/mistral-large-3-675b-instruct-2512",
    "mistralai/mistral-medium-3.5-128b",
    "mistralai/mistral-nemotron",
    "mistralai/mistral-small-4-119b-2603",
    "mistralai/mixtral-8x7b-instruct-v0.1",
    "nvidia/llama-3.1-nemotron-nano-8b-v1",
    "nvidia/llama-3.3-nemotron-super-49b-v1",
    "nvidia/llama-3.3-nemotron-super-49b-v1.5",
    "nvidia/nemotron-3-nano-30b-a3b",
    "nvidia/nemotron-mini-4b-instruct",
    "nvidia/nvidia-nemotron-nano-9b-v2",
    "openai/gpt-oss-120b",
    "qwen/qwen3-coder-480b-a35b-instruct",
    "qwen/qwen3-next-80b-a3b-instruct",
    "qwen/qwen3.5-122b-a10b",
    "qwen/qwen3.5-397b-a17b",
    "sarvamai/sarvam-m",
    "stepfun-ai/step-3.5-flash",
    "stockmark/stockmark-2-100b-instruct",
    "upstage/solar-10.7b-instruct",
    "z-ai/glm-5.1",
)

NIM_SEED: Tuple[str, ...] = NIM_CURRENT + _NIM_V1

# Offline seeds for providers whose catalog may be unreachable or absent.
# Only ids verified against the provider's docs or a live catalog.
SEEDS: Dict[str, Tuple[str, ...]] = {
    "deepseek": ("deepseek-v4-pro", "deepseek-flash"),
    "moonshot": ("kimi-k3", "kimi-k2.7-code", "kimi-k2.7-code-highspeed", "kimi-k2.6"),
    "zai": ("glm-5.3", "glm-5.3-flash"),
    "minimax": ("MiniMax-M3", "MiniMax-M3.1-Flash-Preview", "MiniMax-M2.7", "MiniMax-M2.7-highspeed", "MiniMax-M2.5"),
    "gemini": ("gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite",
               "gemini-3.1-flash-lite"),
    "xai": ("grok-4.7", "grok-4.6", "grok-4.5", "grok-4.3", "grok-build-0.1"),
    "groq": ("openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.3-70b-versatile", "llama-3.1-8b-instant"),
    "cerebras": ("gpt-oss-120b", "qwen-3.8-27b"),
    "sambanova": ("MiniMax-M3", "gpt-oss-120b", "DeepSeek-V3.2", "DeepSeek-V3.1", "Meta-Llama-3.3-70B-Instruct"),
    "mistral": ("mistral-medium-latest", "mistral-large-latest", "codestral-latest"),
    "dashscope": ("qwen3-coder-plus", "qwen3-max", "qwen-plus"),
    "cohere": ("command-a-03-2025",),
    # Claude Code resolves these aliases itself on subscription, Bedrock and Vertex routes.
    "claude": ("sonnet", "opus", "haiku"),
    "bedrock": ("sonnet", "opus", "haiku"),
    "vertex": ("sonnet", "opus", "haiku"),
}

KNOWN_CONTEXT: Tuple[Tuple[str, int], ...] = (
    # first match wins: specific families before their generic parents
    (r"kimi-k3", 1_048_576),
    (r"kimi-k2\.[5-9]|kimi-k2-thinking|kimi-k2-0905", 262_144),
    (r"kimi-k2", 131_072),
    (r"qwen3-coder-(plus|flash)", 1_000_000),
    (r"qwen3-coder", 262_144),
    (r"qwen-?3[.-][5-9][\w.-]*-(plus|max|flash)", 1_000_000),
    (r"qwen-?3[.-][5-9]", 262_144),
    (r"deepseek-v4|deepseek-flash", 1_048_576),
    (r"deepseek-v3", 163_840),
    (r"deepseek-(chat|reasoner)", 128_000),
    (r"gpt-oss", 131_072),
    (r"glm-5[.-][2-9]", 1_048_576),
    (r"glm-5", 204_800),
    (r"glm-4[.-][67](?!v)", 204_800),
    (r"glm-4", 128_000),
    (r"(^|/)gemini", 1_048_576),
    (r"(^|/)gpt-([6-9]|\d{2,})", 1_050_000),
    (r"(^|/)gpt-5[.-][4-9](?![\w.-]*-(mini|nano))", 1_050_000),
    (r"(^|/)gpt-5", 400_000),
    (r"(^|/)gpt-4\.1", 1_000_000),
    (r"claude-(opus|sonnet|fable)-([5-9]|4[.-][6-9])", 1_000_000),
    (r"(^|[/-])claude", 200_000),
    (r"grok-4[.-]20", 2_000_000),
    (r"grok-4[.-]3", 1_000_000),
    (r"grok-4[.-][4-9]", 500_000),
    (r"grok-build|grok-code", 256_000),
    (r"grok-4", 256_000),
    (r"minimax-m3", 1_048_576),
    (r"minimax-m2", 204_800),
    (r"nemotron-3-(ultra|super|nano)", 262_144),
    (r"codestral|devstral", 256_000),
    (r"llama-3\.[13]-70b", 128_000),
)

REASONING_IDS = frozenset({
    "deepseek-ai/deepseek-v4-pro",
    "nvidia/llama-3.1-nemotron-ultra-253b-v1",
    "deepseek-reasoner",
})

_NOT_CHAT = re.compile(
    r"(embed|embedding|whisper|tts|speech|transcri|dall-?e|gpt-image|image-gen|imagen|"
    r"moderation|rerank|guard|safety|content-safety|topic-control|gliner|pii|"
    r"realtime|audio|clip|nv-embed|bge-|e5-|ocr|sdxl|flux|stable-diffusion|"
    r"babbage|davinci|^text-|/text-|computer-use|-parse|veo|lyria|aqa|"
    r"nemoretriever|vila|deplot|kosmos|paligemma|fuyu|neva|reward|translate|detector|calibration|"
    r"diffusion|cosmos|-tts|asr|parakeet|canary|/riva-|retriever|lyria|whisper)",
    re.I,
)

TIER_RECENT = "RECENT"
TIER_PICKS = "PICKS"
TIER_CODING = "CODING"
TIER_REASONING = "REASONING"
TIER_GENERAL = "GENERAL"
TIER_FAST = "FAST"
TIER_OTHER = "OTHER"

TIER_ORDER = (TIER_RECENT, TIER_PICKS, TIER_CODING, TIER_REASONING, TIER_GENERAL, TIER_FAST, TIER_OTHER)
TIER_TITLES = {
    TIER_RECENT: ("RECENT", "your last picks"),
    TIER_PICKS: ("PULSAR PICKS", "best for coding agents"),
    TIER_CODING: ("CODING", "code-tuned routes"),
    TIER_REASONING: ("REASONING", "deliberate, slower first token"),
    TIER_GENERAL: ("GENERAL", "large general models"),
    TIER_FAST: ("FAST", "small, quick, cheap"),
    TIER_OTHER: ("OTHER", "everything else"),
}

_CODING_HINTS = ("coder", "codestral", "devstral", "codellama", "codegemma", "kimi-k2", "kimi-k3", "deepseek-v4",
                 "deepseek-v3", "deepseek-flash", "gpt-oss-120b", "grok-code", "grok-build", "codex", "glm-5",
                 "glm-4.6", "glm-4.7", "qwen3-coder", "minimax-m3")
_FAST_HINTS = ("nano", "mini", "small", "flash", "lite", "instant", "haiku", "tiny", "phi")
_SIZE = re.compile(r"(?<![a-z0-9.])(\d+(?:\.\d+)?)b(?![a-z0-9])")


def param_billions(model_id: str) -> float:
    """Total parameter count from the id (480b, 7b), ignoring active-expert tags like a35b."""
    sizes = [float(x) for x in _SIZE.findall(model_id.lower().split("/")[-1])]
    return max(sizes) if sizes else 0.0
_GENERAL_HINTS = ("llama", "mistral", "mixtral", "gemma", "nemotron", "qwen", "glm", "minimax", "gpt",
                  "claude", "grok", "gemini", "deepseek", "kimi", "sonnet", "opus", "large", "medium", "pro")


@dataclass(frozen=True)
class Model:
    id: str
    profile: str
    name: str = ""
    context: int = 0
    tags: Tuple[str, ...] = ()
    created: int = 0
    max_output: int = 0          # provider-reported max output tokens, 0 = unknown

    @property
    def free(self) -> bool:
        return "free" in self.tags

    @property
    def reasoning(self) -> bool:
        return "reasoning" in self.tags

    @property
    def vision(self) -> Optional[bool]:
        """True when the model accepts images, False when it is known not to, None when unknown."""
        if "vision" in self.tags:
            return True
        if "text-only" in self.tags:
            return False
        return vision_hint(self.id)


# Model ids that accept image input (a hint only: catalog metadata and learned heals win).
_VISION_ID = re.compile(
    r"(?i)(vision|(^|[/_.:-])vl([/_.:-]|$)|-vl\d|omni|pixtral|llava|(^|[/_.:-])(gpt-)?4o|gpt-4\.1|gpt-4-turbo|"
    r"gpt-5|gpt-6|(^|/)o[34](-|$)|claude|gemini|gemma-3|gemma-4|qwen[\d.]*-?vl|internvl|kimi-k2\.6|kimi-k3|"
    r"llama-4|llama-3\.2-(11|90)b|grok-4|mistral-(medium|small)-3)")
# Ids known to be text-only even though they share a family name with a vision model.
_TEXT_ID = re.compile(r"(?i)(embed|whisper|tts|(^|[/_.:-])coder?([/_.:-]|$)|codestral|codellama|starcoder|"
                      r"gpt-oss|deepseek-(r1|v3|coder)|mixtral)")


def vision_hint(model_id: str) -> Optional[bool]:
    """A guess from the id alone: True (vision/VL/4o/omni families), False (known text-only), None."""
    mid = model_id or ""
    if _VISION_ID.search(mid):
        return True
    if _TEXT_ID.search(mid):
        return False
    return None


def is_reasoning(model_id: str) -> bool:
    low = model_id.lower()
    if low in REASONING_IDS:
        return True
    return bool(re.search(r"(reason|thinking|(^|[/-])o[134](-|$)|(^|[/-])r1([-:]|$)|-r1-)", low))


def guess_context(model_id: str) -> int:
    low = model_id.lower()
    for pattern, ctx in KNOWN_CONTEXT:
        if re.search(pattern, low):
            return ctx
    return 0


def _hint(text: str, hint: str) -> bool:
    return re.search(r"(?<![a-z])" + re.escape(hint) + r"(?![a-z])", text) is not None


def tier_of(model: Model) -> str:
    low = model.id.lower()
    if model.reasoning:
        return TIER_REASONING
    for h in _CODING_HINTS:
        if _hint(low, h):
            return TIER_CODING
    size = param_billions(low)
    small = 0 < size <= 14
    if small or (not size and any(_hint(low, h) for h in _FAST_HINTS)):
        return TIER_FAST
    for h in _GENERAL_HINTS:
        if _hint(low, h):
            return TIER_GENERAL
    return TIER_OTHER


_DATE = re.compile(r"(20\d{2}-?\d{2}-?\d{2}|-\d{4}$|-\d{6,8}$|preview|exp\b|-0\d{3}$)")


def version_key(model_id: str) -> Tuple:
    """Sort key that ranks newer, undated, larger releases first."""
    low = model_id.lower()
    tail = low.split("/")[-1]
    snapshot = 1 if (_DATE.search(tail) or ":" in tail) else 0
    core = re.sub(r"(?<![a-z0-9])a?\d+(\.\d+)?[bm](?![a-z0-9])", "", tail)  # drop sizes: 480b, a35b, 7b
    core = re.sub(r"(?<![a-z0-9])(bf16|fp\d+|nvfp4|mxfp4|int\d+|w\d+a\d+)(?![a-z0-9])", "", core)  # precisions
    nums = [int(n) for n in re.findall(r"\d+", core)[:4]]
    nums += [None] * (4 - len(nums))
    # Higher numbers first; a missing component ranks after any present one (5.1 before 5).
    return (snapshot, tuple(1 if n is None else -n for n in nums), len(low), low)


def picks(models: Sequence[Model], provider: P.Provider, limit: int = 6) -> List[Model]:
    chosen: List[Model] = []
    seen = set()
    for pattern in provider.prefer:
        rx = re.compile(pattern if pattern.startswith("(?i)") else "(?i)" + pattern)
        matches = sorted((m for m in models if rx.search(m.id) and m.id not in seen), key=lambda m: version_key(m.id))
        for m in matches[:2]:
            chosen.append(m)
            seen.add(m.id)
            if len(chosen) >= limit:
                return chosen
    return chosen


def recommended(models: Sequence[Model], provider: P.Provider) -> str:
    top = picks(models, provider, limit=1)
    if top:
        return top[0].id
    if provider.fallback_model:
        return provider.fallback_model
    return models[0].id if models else ""


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

class CatalogError(Exception):
    """A catalog or key-probe failure. The message is always redacted: it reaches the screen and logs."""

    def __init__(self, message: str, status: int = 0, secrets: Iterable[str] = ()) -> None:
        super().__init__(P.clean_text(redact(str(message), secrets), 400))
        self.status = status


_AUTH_400 = re.compile(r"(api[ _-]?key|auth(entication|orization)? key|API_KEY_INVALID|invalid (auth|credential|token))", re.I)
_CRED_HEADERS = frozenset({"authorization", "x-api-key", "api-key", "x-goog-api-key"})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect.

    urllib's default handler re-sends every header added to the Request
    (Authorization, x-api-key, ...) to whatever host a 30x points at, even from
    https to http. A key must only ever reach the provider it belongs to.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None   # the 30x then surfaces as an HTTPError


class _TrustHandler(urllib.request.HTTPSHandler):
    """HTTPS with Pulsar's one TLS decision (netcfg.ssl_context(): PULSAR_CA_BUNDLE, truststore,
    certifi, NODE_EXTRA_CA_CERTS), so a key that Core can use behind a TLS-inspecting proxy also
    verifies here. Resolved per request: netcfg.reset() (tests, a changed bundle) takes effect at once."""

    def https_open(self, req):  # type: ignore[override]
        from pulsar import netcfg
        return self.do_open(http.client.HTTPSConnection, req, context=netcfg.ssl_context())


# Every request this module (and anything that sends a key through it) makes goes through one of these.
OPENER = urllib.request.build_opener(_TrustHandler(), _NoRedirect())
# Loopback and LAN model servers are reached directly: a corporate proxy cannot reach them and must
# not see their traffic.
LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _TrustHandler(), _NoRedirect())
_OPENER = OPENER


def opener_for(url: str) -> urllib.request.OpenerDirector:
    return LOCAL_OPENER if P.is_local_url(url) else OPENER


def _secrets_in(headers: Dict[str, str]) -> List[str]:
    out: List[str] = []
    for k, v in headers.items():
        if k.lower() in _CRED_HEADERS and v:
            out.append(v)
            if v.lower().startswith("bearer "):
                out.append(v[7:].strip())
    return out


def _where(base: str, location: str) -> str:
    """scheme://host/path of a redirect target, without query or credentials."""
    try:
        target = urllib.parse.urlsplit(urllib.parse.urljoin(base, location or ""))
        host = target.hostname or "?"
        if target.port:
            host += ":%d" % target.port
        return "%s://%s%s" % (target.scheme or "?", host, target.path or "")
    except ValueError:
        return "another location"


def _get_json(url: str, headers: Dict[str, str], timeout: float) -> Any:
    secrets = _secrets_in(headers)
    if secrets:
        refusal = P.plain_http_refusal(url, secrets[0])
        if refusal:
            raise CatalogError(refusal, 0, secrets)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json", **headers})
    try:
        with opener_for(url).open(req, timeout=timeout) as resp:
            return json.loads(resp.read(16_000_000).decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            location = exc.headers.get("Location", "") if exc.headers is not None else ""
            raise CatalogError("redirect to %s not followed%s" % (
                _where(url, location), ": your key stays put" if secrets else ""), exc.code, secrets)
        detail = ""
        try:
            detail = exc.read(2000).decode("utf-8", errors="replace")
        except Exception:
            pass
        message = _short_error(detail) or exc.reason or "HTTP error"
        status = exc.code
        if status == 400 and _AUTH_400.search(detail or ""):
            status = 401  # Gemini answers a bad key with 400 "API key not valid"
        raise CatalogError(str(message), status, secrets)
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise CatalogError("unreachable: " + str(reason), 0, secrets)
    except ValueError:
        raise CatalogError("provider returned non-JSON", 0, secrets)


def _short_error(text: str) -> str:
    try:
        data = json.loads(text)
    except ValueError:
        return " ".join(text.split())[:160]
    err = data.get("error", data) if isinstance(data, dict) else data
    if isinstance(err, dict):
        return str(err.get("message") or err.get("detail") or err.get("title") or "")[:160]
    if isinstance(err, list) and err and isinstance(err[0], dict):
        inner = err[0].get("error", err[0])
        return str(inner.get("message", "") if isinstance(inner, dict) else inner)[:160]
    return str(err)[:160]


# Catalog "type" values that are never chat models (Together, Vercel, Venice, ...).
_NON_CHAT_TYPES = frozenset({"image", "images", "embedding", "embeddings", "video", "speech", "audio", "tts", "stt",
                             "transcription", "realtime", "reranking", "rerank", "moderation", "evaluation",
                             "upscale", "inpaint", "image-gen", "video-gen"})


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _dig(item: Dict[str, Any], *path: str) -> Any:
    cur: Any = item
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _is_chat(provider: P.Provider, item: Dict[str, Any], mid: str) -> bool:
    """False for entries a coding agent can never use (non-chat, batch-only, tool-less routers)."""
    if _NOT_CHAT.search(mid) or mid.endswith(":batch"):
        return False
    kind = item.get("type")
    if isinstance(kind, str) and kind:
        if provider.id == "together":
            if kind.lower() != "chat":
                return False
        elif kind.lower() in _NON_CHAT_TYPES:
            return False
    api = item.get("api")
    if isinstance(api, str) and api and api.lower() != "chat":
        return False
    mtype = item.get("model_type")
    if isinstance(mtype, str) and mtype and mtype.lower() != "chat":
        return False
    tags = _dig(item, "metadata", "tags")
    if isinstance(tags, list) and tags and "chat" not in tags:
        return False                       # DeepInfra: ["tts"], ["embed"], ["image-gen"], ...
    caps = item.get("capabilities")
    if isinstance(caps, dict) and caps.get("chat_completion") is False:
        return False                       # Azure OpenAI
    outputs = (_dig(item, "architecture", "output_modalities") or _dig(item, "modalities", "output")
               or item.get("output_modalities"))
    if isinstance(outputs, list) and outputs and "text" not in outputs:
        return False
    hosts = item.get("providers")
    if provider.id == "huggingface" and isinstance(hosts, list) and hosts:
        if not any(isinstance(h, dict) and h.get("supports_tools") for h in hosts):
            return False                   # no inference provider serves it with tool calling
    return True


def _supports_tools(item: Dict[str, Any]) -> Optional[bool]:
    params = item.get("supported_parameters")
    if isinstance(params, list) and params:
        return "tools" in params
    for value in (item.get("supports_tool_calling"), _dig(item, "capabilities", "tools"),
                  _dig(item, "capabilities", "function_calling"),
                  _dig(item, "model_spec", "capabilities", "supportsFunctionCalling")):
        if isinstance(value, bool):
            return value
    for key, flag in (("features", "function-calling"), ("supported_features", "tools")):
        feats = item.get(key)
        if isinstance(feats, list) and feats:
            return flag in feats
    return None


def _thinks(item: Dict[str, Any]) -> bool:
    params = item.get("supported_parameters")
    if isinstance(params, list) and "reasoning" in params:
        return True
    for value in (item.get("supports_reasoning"), _dig(item, "capabilities", "reasoning"),
                  _dig(item, "model_spec", "capabilities", "supportsReasoning")):
        if value is True:
            return True
    for key in ("features", "supported_features"):
        feats = item.get(key)
        if isinstance(feats, list) and "reasoning" in feats:
            return True
    return False


def _free(item: Dict[str, Any]) -> bool:
    pricing = item.get("pricing")
    if isinstance(pricing, dict) and "prompt" in pricing and "completion" in pricing:
        try:
            if float(pricing.get("prompt", 1)) == 0 and float(pricing.get("completion", 1)) == 0:
                return True
        except (TypeError, ValueError):
            pass
    hosts = item.get("providers")
    if isinstance(hosts, list) and any(isinstance(h, dict) and h.get("is_free") for h in hosts):
        return True
    return False


def _context(item: Dict[str, Any]) -> int:
    for value in (item.get("context_length"), item.get("context_window"), item.get("context_size"),
                  item.get("max_input_tokens"), item.get("max_context_length"),
                  _dig(item, "limits", "max_context_length"), _dig(item, "top_provider", "context_length"),
                  _dig(item, "metadata", "context_length"), _dig(item, "model_spec", "availableContextTokens"),
                  item.get("max_model_len")):
        n = _int(value)
        if n > 0:
            return n
    hosts = item.get("providers")
    if isinstance(hosts, list):
        return max([_int(h.get("context_length")) for h in hosts if isinstance(h, dict)] + [0])
    return 0


def _max_output(item: Dict[str, Any]) -> int:
    for value in (_dig(item, "top_provider", "max_completion_tokens"), item.get("max_completion_tokens"),
                  _dig(item, "limits", "max_completion_tokens"), item.get("max_output_tokens"),
                  _dig(item, "model_spec", "maxCompletionTokens"), item.get("max_output_length"),
                  item.get("max_tokens") if item.get("context_window") else None):
        n = _int(value)
        if n > 0:
            return n
    return 0


def _vision(item: Dict[str, Any]) -> Optional[bool]:
    """Image input from catalog metadata (OpenRouter `architecture.input_modalities`, a top-level
    `input_modalities` or `modalities.input`, a `capabilities.vision` flag); None when not reported."""
    for path in (("architecture", "input_modalities"), ("input_modalities",), ("modalities", "input")):
        mods = _dig(item, *path)
        if isinstance(mods, list) and mods:
            return any(str(m).lower() in ("image", "images", "vision") for m in mods)
    for path in (("capabilities", "vision"), ("supports_vision",), ("vision",)):
        flag = _dig(item, *path)
        if isinstance(flag, bool):
            return flag
    return None


def _openrouter_tags(item: Dict) -> Tuple[Tuple[str, ...], int]:
    """Kept for callers of v2: (tags, context) for one OpenRouter entry."""
    tags: List[str] = []
    if _free(item):
        tags.append("free")
    if _supports_tools(item) is False:
        tags.append("no-tools")
    if _thinks(item):
        tags.append("thinks")
    return tuple(tags), _int(item.get("context_length"))


def _items(payload: Any) -> Optional[List[Any]]:
    if isinstance(payload, list):
        return payload                     # Together returns a top-level array
    if isinstance(payload, dict):
        for key in ("data", "models"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return None


_FREE_PROVIDERS = ("nvidia", "gemini", "groq", "cerebras")

# Model ids come from providers and custom endpoints and end up on the terminal, in caches, in
# Claude Code's environment and settings. Only printable ASCII ids are kept.
valid_model_id = P.valid_model_id
clean_text = P.clean_text


def parse_models(provider: P.Provider, profile_id: str, payload: Any) -> List[Model]:
    items = _items(payload)
    if items is None:
        return []
    out: List[Model] = []
    for item in items:
        if isinstance(item, str):
            item = {"id": item}
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or item.get("name") or item.get("model") or "").strip()
        if mid.startswith("models/"):
            mid = mid[len("models/"):]
        if not valid_model_id(mid):
            continue   # control sequences, spaces or absurd lengths: never shown, cached or announced
        if not _is_chat(provider, item, mid):
            continue
        tags: List[str] = []
        if _free(item) or mid.endswith(":free"):
            tags.append("free")
        if _supports_tools(item) is False:
            tags.append("no-tools")
        if _thinks(item):
            tags.append("thinks")
        sees = _vision(item)
        if sees is not None:
            tags.append("vision" if sees else "text-only")
        ctx = _context(item) or guess_context(mid)
        if is_reasoning(mid):
            tags.insert(0, "reasoning")
        if provider.local or (provider.free and provider.id in _FREE_PROVIDERS):
            tags.append("free")
        if provider.local:
            tags.append("local")
        name = clean_text(item.get("display_name") or item.get("name") or "", 120).strip()
        if name == mid or name.startswith("models/"):
            name = ""
        created = item.get("created")
        out.append(Model(mid, profile_id, name=name, context=ctx, tags=tuple(dict.fromkeys(tags)),
                         created=created if isinstance(created, int) and created > 0 else 0,
                         max_output=_max_output(item)))
    dedup: Dict[str, Model] = {}
    for m in out:
        dedup.setdefault(m.id, m)
    return list(dedup.values())


def seed_models(provider: P.Provider, profile_id: str) -> List[Model]:
    if provider.id == "nvidia":
        ids: Iterable[str] = NIM_CURRENT
    elif provider.id in SEEDS:
        ids = SEEDS[provider.id]
    else:
        ids = (provider.fallback_model,) if provider.fallback_model else ()
    return parse_models(provider, profile_id, {"data": [{"id": i} for i in ids]})


def _native_custom(provider: P.Provider) -> bool:
    """An Anthropic Messages endpoint the operator pasted (its URL ends in /anthropic)."""
    return provider.id == "custom" and provider.route == "native"


def _authed(provider: P.Provider, key: str) -> Tuple[str, Dict[str, str]]:
    if _native_custom(provider):
        # Its model list sits beside /v1/messages, and it takes the key the way its messages do.
        return (provider.native_base() + "/v1/models?limit=1000",
                dict(P.auth_headers(provider, key, native=True), **{"anthropic-version": "2023-06-01"}))
    if provider.catalog == "anthropic":
        return (provider.base_url.rstrip("/") + "/models?limit=1000",
                {"x-api-key": key, "anthropic-version": "2023-06-01"} if key else {"anthropic-version": "2023-06-01"})
    return provider.base_url.rstrip("/") + "/models", P.auth_headers(provider, key)


def _merge(models: List[Model], extra: List[Model]) -> List[Model]:
    """Fill gaps (context, max output, tags) from a richer public listing."""
    by_id = {m.id: m for m in extra}
    out = []
    for m in models:
        e = by_id.get(m.id)
        if e is not None:
            m = replace(m, context=m.context or e.context, max_output=m.max_output or e.max_output,
                        name=m.name or e.name, tags=tuple(dict.fromkeys(m.tags + e.tags)))
        out.append(m)
    return out


def fetch_public(provider: P.Provider, profile_id: str, timeout: float = 8.0) -> List[Model]:
    """The provider's public catalog (Provider.models_url). Sends no key."""
    if os.environ.get("PULSAR_OFFLINE") == "1":
        raise CatalogError("offline mode (PULSAR_OFFLINE=1)")
    if not provider.models_url:
        raise CatalogError("%s has no public catalog" % provider.name)
    return parse_models(provider, profile_id, _get_json(provider.models_url, {}, timeout))


def url_example(provider: P.Provider) -> str:
    """The URL form a provider that needs your own resource URL expects, for messages:
    https://<your-resource>.openai.azure.com/openai/v1 for Azure OpenAI. "" for other providers."""
    if not provider.needs_url:
        return ""
    default = P.BY_ID.get(provider.id)
    base = (default.base_url if default is not None else provider.base_url) or ""
    return re.sub(r"(?i)YOUR-RESOURCE", "<your-resource>", base.rstrip("/")) or "https://<your-resource>/..."


def placeholder_problem(provider: P.Provider) -> str:
    """Why `provider` must not be contacted, or "" when it is fine.

    A provider that needs your own resource URL (Azure OpenAI, Azure AI Foundry) and has none saved (an
    empty base_url from an older save, the registry placeholder https://YOUR-RESOURCE... or the registry
    default) would send its key to a host that is not yours. Every path that could send that key (the
    catalog fetch here, the launch, `pulsarcode switch`, `pulsar_switch`, `status`) asks this first.
    """
    if not provider.needs_url:
        return ""
    default = P.BY_ID.get(provider.id)
    base = (provider.base_url or "").strip().rstrip("/")
    if (not base or "YOUR-RESOURCE" in base.upper()
            or (default is not None and base == (default.base_url or "").strip().rstrip("/"))):
        return ("%s has no resource URL saved (it still points at the placeholder). Reconnect with "
                "`pulsarcode key %s` and paste the key at the prompt. Nothing was sent."
                % (provider.name, url_example(provider) or "https://<your-resource>..."))
    return ""


def fetch_live(provider: P.Provider, profile_id: str, key: str, timeout: float = 8.0) -> List[Model]:
    if os.environ.get("PULSAR_OFFLINE") == "1":
        raise CatalogError("offline mode (PULSAR_OFFLINE=1)")
    problem = placeholder_problem(provider)
    if problem:
        raise CatalogError(problem)        # before any request: the key must not reach YOUR-RESOURCE...
    if provider.catalog == "none" or provider.route == "cloud":
        return seed_models(provider, profile_id)
    if provider.catalog == "anthropic" and key.startswith("sk-ant-oat"):
        return seed_models(provider, profile_id)   # subscription tokens are not API keys; never send as x-api-key
    if not key and provider.models_url:
        models = fetch_public(provider, profile_id, timeout)
    else:
        url, headers = _authed(provider, key)
        try:
            payload = _get_json(url, headers, timeout)
        except CatalogError:
            if _native_custom(provider):
                return seed_models(provider, profile_id)     # many such endpoints list no models: not an error
            raise
        models = parse_models(provider, profile_id, payload)
        if models and provider.models_url and provider.models_url.rstrip("/") != url.rstrip("/"):
            try:
                models = _merge(models, fetch_public(provider, profile_id, min(timeout, 5.0)))
            except CatalogError:
                pass
    if not models and provider.id == "nvidia":
        models = seed_models(provider, profile_id)
    return models


def cache_file(profile_id: str) -> Path:
    from pulsar.config import home
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", profile_id)
    return home() / "cache" / ("models-" + safe + ".json")


def read_cache(profile_id: str, ttl: float = CACHE_TTL_S) -> Optional[List[Model]]:
    path = cache_file(profile_id)
    try:
        if time.time() - path.stat().st_mtime > ttl:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != CACHE_SCHEMA:
        return None
    out = []
    for item in data.get("models", []):
        if not isinstance(item, dict) or not valid_model_id(item.get("id")):
            continue
        try:
            item["tags"] = tuple(clean_text(t, 40) for t in (item.get("tags") or ()))
            item["name"] = clean_text(item.get("name") or "", 120)
            out.append(Model(**item))
        except (TypeError, ValueError):
            continue
    return out or None


def write_cache(profile_id: str, models: Sequence[Model]) -> None:
    from pulsar.config import HomeError, write_private
    try:
        write_private(cache_file(profile_id), json.dumps(
            {"schema": CACHE_SCHEMA, "at": int(time.time()), "models": [asdict(m) for m in models]}))
    except (OSError, HomeError):
        pass          # best effort; a PULSAR_HOME that is someone else's folder is never written into


@dataclass
class CatalogResult:
    models: List[Model] = field(default_factory=list)
    source: str = "seed"          # "live" | "cache" | "seed"
    error: str = ""
    status: int = 0


def load_catalog(provider: P.Provider, profile_id: str, key: str, refresh: bool = False,
                 network: bool = True, timeout: float = 8.0) -> CatalogResult:
    if not refresh:
        cached = read_cache(profile_id)
        if cached:
            return CatalogResult(cached, "cache")
    if network and os.environ.get("PULSAR_OFFLINE") != "1":
        try:
            models = fetch_live(provider, profile_id, key, timeout=timeout)
            if models:
                write_cache(profile_id, models)
                return CatalogResult(models, "live")
            return CatalogResult(seed_models(provider, profile_id), "seed", "provider listed no chat models")
        except CatalogError as exc:
            stale = read_cache(profile_id, ttl=10 * 365 * 86400)
            if stale:
                return CatalogResult(stale, "cache", str(exc), exc.status)
            return CatalogResult(seed_models(provider, profile_id), "seed", str(exc), exc.status)
    stale = read_cache(profile_id, ttl=10 * 365 * 86400)
    if stale:
        return CatalogResult(stale, "cache")
    return CatalogResult(seed_models(provider, profile_id), "seed")


def learned_no_vision(profile_id: str, model_id: str) -> bool:
    """True when Core persisted a `no_vision` heal for this model (the provider rejected an image)."""
    from pulsar.config import home
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", profile_id)
    try:
        data = json.loads((home() / "cache" / ("heals-" + safe + ".json")).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    entry = data.get(model_id) if isinstance(data, dict) else None
    if not isinstance(entry, dict) or time.time() - float(entry.get("_t") or 0) > 7 * 86400:
        return False
    return bool(entry.get("no_vision"))


def model_vision(profile_id: str, model_id: str, provider: Optional[P.Provider] = None) -> Optional[bool]:
    """Whether `model_id` on `profile_id` can view images: a learned no_vision heal, then the cached
    catalog's metadata, then the id. None when nothing tells."""
    if not model_id:
        return None
    if learned_no_vision(profile_id, model_id):
        return False
    if provider is not None and (provider.id in ("anthropic", "claude", "foundry") or provider.route == "cloud"):
        return True                      # Claude models all take images
    m = next((x for x in read_cache(profile_id, ttl=10 * 365 * 86400) or [] if x.id == model_id), None)
    if m is not None:
        return m.vision
    return vision_hint(model_id)


def _unchecked_reason(res: "CatalogResult") -> str:
    """A few words for why a catalog could not be consulted (never the provider's raw text: it is shown
    next to a model id the user typed)."""
    err = (res.error or "").lower()
    if "offline" in err:
        return "offline"
    if res.status in (401, 403):
        return "key rejected (%d)" % res.status
    if res.status:
        return "HTTP %d" % res.status
    return "unreachable"


def locate(model_id: str, candidates: Sequence[Tuple[str, P.Provider, str]], fetch: bool = True,
           timeout: float = 6.0, unchecked: Optional[List[Tuple[str, str]]] = None) -> Tuple[str, bool]:
    """(profile id whose catalog lists `model_id` or "", whether any catalog was there to consult).

    `candidates` are (profile_id, provider, key) in preference order. Cached catalogs first; with
    `fetch`, profiles that have no cache are loaded (each key goes only to its own provider; a provider
    still on its placeholder URL (placeholder_problem) is never contacted).

    `unchecked`, when given, receives (profile id, reason) for every candidate whose catalog could not
    be consulted (no key, no resource URL, no model list, offline, unreachable), so a refusal can say
    which providers it could not check instead of claiming that none lists the id."""
    missing = []
    known = False
    out = unchecked if unchecked is not None else []
    for pid, prov, key in candidates:
        cached = read_cache(pid, ttl=10 * 365 * 86400)
        if cached is None:
            missing.append((pid, prov, key))
            continue
        known = True
        if any(m.id == model_id for m in cached):
            return pid, True
    for pid, prov, key in missing:
        if placeholder_problem(prov):
            out.append((pid, "no resource URL"))
            continue
        if prov.catalog == "none":
            out.append((pid, "no model list"))
            continue
        if prov.needs_key and not key:
            out.append((pid, "no key"))
            continue
        if not fetch:
            out.append((pid, "not fetched"))
            continue
        known = True                       # asked (even an offline answer does not vouch for the id)
        res = load_catalog(prov, pid, key, timeout=timeout)
        if res.source != "seed" and any(m.id == model_id for m in res.models):
            return pid, True
        if res.source == "seed" and res.error:
            out.append((pid, _unchecked_reason(res)))
    return "", known


def unlisted_message(model_id: str, profile_ids: Sequence[str], unchecked: Sequence[Tuple[str, str]]) -> str:
    """Why `model_id` was not bound to any connected profile, and how to name one. Neutral: the example
    never suggests the active profile, which would send one vendor's id to another vendor."""
    shown = P.clean_text(model_id, 80)
    names = ", ".join(P.clean_text(p, 40) for p in profile_ids) or "none"
    which = ", ".join("%s (%s)" % (P.clean_text(pid, 40), why) for pid, why in unchecked)
    if unchecked and {pid for pid, _ in unchecked} >= set(profile_ids):
        lead = "could not check whether any connected provider lists %r: %s." % (shown, which)
    elif unchecked:
        lead = "no connected provider that could be checked lists %r; could not check %s." % (shown, which)
    else:
        lead = "no connected provider lists %r." % shown
    return ("%s Name the provider as provider:model, for example <provider>:%s where <provider> is one of "
            "%s (see `pulsarcode models`)." % (lead, shown, names))


# ---------------------------------------------------------------------------
# Grouping and search
# ---------------------------------------------------------------------------

def group(models: Sequence[Model], provider_of: Dict[str, P.Provider],
          recent: Sequence[Tuple[str, str]] = ()) -> List[Tuple[str, List[Model]]]:
    """Tiered view. `recent` is a list of (profile, model id), newest first."""
    by_key = {(m.profile, m.id): m for m in models}
    placed = set()
    groups: Dict[str, List[Model]] = {t: [] for t in TIER_ORDER}
    for key in recent:
        m = by_key.get(tuple(key))
        if m is not None and key not in placed:
            groups[TIER_RECENT].append(m)
            placed.add(tuple(key))
    profiles = list(dict.fromkeys(m.profile for m in models))
    for pid in profiles:
        prov = provider_of.get(pid)
        if prov is None:
            continue
        for m in picks([x for x in models if x.profile == pid], prov):
            if (m.profile, m.id) not in placed:
                groups[TIER_PICKS].append(m)
                placed.add((m.profile, m.id))
    for m in models:
        if (m.profile, m.id) in placed:
            continue
        groups[tier_of(m)].append(m)
        placed.add((m.profile, m.id))
    for tier in TIER_ORDER[2:]:
        groups[tier].sort(key=lambda m: (0 if m.free else 1, version_key(m.id)))
    return [(t, groups[t]) for t in TIER_ORDER if groups[t]]


def fuzzy_score(query: str, text: str) -> Optional[int]:
    """Subsequence match score (higher is better), None when no match."""
    q = query.lower().strip()
    if not q:
        return 0
    t = text.lower()
    if q in t:
        return 1000 - t.index(q) * 2 - len(t) // 8 + (200 if t.startswith(q) or ("/" + q) in t else 0)
    score, pos, streak = 0, 0, 0
    for ch in q:
        if ch == " ":
            continue
        found = t.find(ch, pos)
        if found < 0:
            return None
        streak = streak + 1 if found == pos else 0
        score += 10 + streak * 6 - min(found - pos, 10)
        if found == 0 or t[found - 1] in "/-_.: ":
            score += 12
        pos = found + 1
    return score - len(t) // 8


def search(models: Sequence[Model], query: str, label_of: Dict[str, str]) -> List[Model]:
    scored = []
    for m in models:
        hay = m.id + " " + label_of.get(m.profile, m.profile) + " " + " ".join(m.tags)
        s = fuzzy_score(query, hay)
        if s is not None:
            scored.append((-s, version_key(m.id), m))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [m for _, _, m in scored]
