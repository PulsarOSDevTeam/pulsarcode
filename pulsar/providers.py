# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Provider registry and API-key fingerprinting.

Paste any key and pulsarcode works out where it belongs from its shape
alone. Detection is purely local: a key is only ever sent to the one
provider it was matched to (or the one the operator picked when the
shape is ambiguous). We never "spray" a key across vendors to see which
one accepts it, because that would hand your secret to strangers. Keys
that can never drive a model (admin keys, GitHub or Stripe tokens, ...)
are refused outright and never sent anywhere.

Three routes exist:

  adapter  the provider speaks an OpenAI dialect (chat-completions, or the
           Responses API for models that need it, see Provider.wire_for).
           Pulsar Core translates Anthropic Messages <-> OpenAI on the fly.
           Hybrid providers can still send selected models to their own
           Anthropic-compatible endpoint (Provider.native_for).
  native   the provider speaks the Anthropic Messages dialect itself
           (Anthropic, plus vendors that ship an Anthropic-compatible
           endpoint). Core forwards requests as they are.
  cloud    Claude Code authenticates by itself through one of its built-in
           integrations (Amazon Bedrock, Google Vertex AI, or a Claude
           subscription token). Pulsar only prepares the environment, see
           cloud_env(); no traffic passes through Core.

v3 fields and how Core should use them are documented on Provider.
"""

from __future__ import annotations

import ipaddress
import os
import re
import urllib.parse
from dataclasses import dataclass, field, replace
from typing import Dict, List, Mapping, Optional, Tuple

# Model families that require 9-character [A-Za-z0-9] tool-call ids wherever
# they are served (Mistral's API, and vLLM/NIM builds using Mistral's tokenizer).
MISTRAL_FAMILY = re.compile(r"(?i)(^|[/_.-])(mistral|devstral|codestral|magistral|ministral|pixtral|voxtral|mixtral)")

# OpenAI models whose chat-completions route lacks tool calling (or the route
# itself): gpt-5.x-codex is Responses-only; gpt-6.x returns tools only through
# the Responses API; the -pro reasoning models are Responses-only.
OPENAI_RESPONSES_MODELS = (r"(?i)(^|/)(gpt-\d+(\.\d+)?-codex\b|codex-|gpt-([6-9]|\d{2,})(\b|[.-])|"
                           r"o\d+-pro\b|gpt-5(\.\d+)?-pro\b)")


@dataclass(frozen=True)
class Provider:
    """One model vendor.

    v2 fields keep their meaning. v3 adds (all optional):

    wire / responses_models
        OpenAI wire protocol on the adapter route. Core calls
        ``wire_for(model)``: "chat" -> POST {base_url}/chat/completions via
        pulsar.translate, "responses" -> POST {base_url}/responses via
        pulsar.responses.
    tool_id_style
        "mistral9" when the upstream requires tool-call ids of exactly nine
        [A-Za-z0-9] characters. Core calls ``tool_id_style_for(model)``,
        which also answers "mistral9" for Mistral-family models on any host.
    auth_header
        How the adapter route sends the key: "bearer" (Authorization:
        Bearer), "api-key" (Azure OpenAI) or "x-api-key". Use
        ``auth_headers(provider, key, native=False)``.
    anthropic_native_url / native_models
        A verified Anthropic Messages base (no /v1; Core appends
        /v1/messages). For adapter-route providers, models matching the
        ``native_models`` regex go there instead of the adapter. Core calls
        ``native_for(model)`` and ``native_base()``; the native header comes
        from ``auth`` as before.
    models_url
        A public model list that needs no key. Pulsar never sends a key to it.
    regions
        (name, base_url, anthropic base or "") variants such as China
        endpoints. ``replace(p, base_url=<a region base>)`` swaps the
        Anthropic URLs too, so persisted base_url overrides keep working.
    cloud / key_env
        For route "cloud": which Claude Code integration to switch on
        ("bedrock", "vertex" or "anthropic" for a subscription token) and the
        variable Claude Code reads the credential from. Use ``cloud_env()``.
    needs_url
        The provider has no fixed host (Azure, Foundry): it can only be set up
        by pasting its URL, never picked for a bare key.
    native_is_base
        The pasted base URL is itself the Anthropic endpoint (Foundry).
    """

    id: str
    name: str
    route: str                      # "adapter" | "native" | "cloud"
    base_url: str                   # OpenAI-compatible base (adapter route + catalog)
    color: str                      # brand accent, hex
    native_url: str = ""            # Anthropic-compatible base (native route)
    auth: str = "bearer"            # native header: "bearer" | "x-api-key"
    catalog: str = "openai"         # "openai" | "anthropic" | "none"
    prefixes: Tuple[str, ...] = ()
    env: Tuple[str, ...] = ()
    key_url: str = ""
    free: str = ""                  # short free-tier note, empty when paid only
    needs_key: bool = True
    local: bool = False
    prefer: Tuple[str, ...] = ()    # regexes, best first, for the recommended tier
    fallback_model: str = ""        # used when the catalog cannot be reached
    usage_in_stream: bool = False   # accepts stream_options.include_usage
    strict_schema: bool = False     # rejects JSON-schema keywords outside OpenAPI 3
    max_output: int = 0             # default cap on max_tokens, 0 = pass through
    blurb: str = ""
    # -- v3 ------------------------------------------------------------------
    wire: str = "chat"              # adapter wire: "chat" | "responses"
    responses_models: str = ""      # regex: models that need the Responses wire here
    tool_id_style: str = "default"  # "default" | "mistral9"
    auth_header: str = "bearer"     # adapter header: "bearer" | "api-key" | "x-api-key"
    native_models: str = ""         # regex: models routed to anthropic_native_url
    anthropic_native_url: str = ""  # verified Anthropic Messages base, no /v1
    models_url: str = ""            # public model list, fetched without a key
    regions: Tuple[Tuple[str, str, str], ...] = ()
    cloud: str = ""                 # route "cloud": "bedrock" | "vertex" | "anthropic"
    key_env: str = ""               # route "cloud": env var that carries the credential
    needs_url: bool = False
    native_is_base: bool = False

    def __post_init__(self) -> None:
        if self.native_is_base and self.base_url:
            object.__setattr__(self, "native_url", self.base_url.rstrip("/"))
            object.__setattr__(self, "anthropic_native_url", self.base_url.rstrip("/"))
            return
        region = _region_for(self.regions, self.base_url) if self.regions else None
        if region is None:
            self._follow_base()
        if not self.regions:
            return
        natives = {r[2] for r in self.regions if r[2]}
        if region is None:
            # An unknown regional host (a workspace URL, a proxy): keys are region-bound, so
            # never pair it with another region's Anthropic endpoint on the adapter route.
            if self.route != "native" and self.anthropic_native_url in natives:
                object.__setattr__(self, "anthropic_native_url", "")
            return
        native = region[2]
        if self.route == "native" and native and (not self.native_url or self.native_url in natives):
            object.__setattr__(self, "native_url", native)
        if not self.anthropic_native_url or self.anthropic_native_url in natives:
            object.__setattr__(self, "anthropic_native_url", native)

    def _follow_base(self) -> None:
        """A base_url override (a gateway, a proxy, another host) moves native traffic along with it.

        The registry's Anthropic Messages bases (native_url, anthropic_native_url) belong to the vendor's
        default host. With another base_url they are derived from it the way they sit beside the default
        (same path on another host; or the vendor's /anthropic next to a stripped /v1); when that cannot be
        derived, an adapter-route provider drops native routing (the chat wire on the configured host) and
        a native-route provider uses the configured host itself. Either way the key never bypasses the
        configured gateway for the vendor's public host.
        """
        default = _DEFAULTS.get(self.id)
        if default is None or _norm(self.base_url) == _norm(default[0]):
            return
        if _netloc(self.base_url) == _netloc(default[0]):
            return      # the vendor's own host on another path (DeepSeek's /beta): its native bases stand
        if self.regions and self.route != "native":
            return      # keys are region-bound: __post_init__ drops native routing for an unknown regional host
        base, d_native, d_anthropic = default
        for attr, value in (("native_url", d_native), ("anthropic_native_url", d_anthropic)):
            current = getattr(self, attr)
            if not current or _norm(current) != _norm(value):
                continue                 # empty, or set explicitly for this instance: leave it
            moved = _derive_native(base, value, self.base_url)
            if not moved and self.route == "native":
                moved = _base_root(self.base_url)
            object.__setattr__(self, attr, moved)

    @property
    def short(self) -> str:
        return self.name.split(" ")[0]

    @property
    def region(self) -> str:
        found = _region_for(self.regions, self.base_url)
        return found[0] if found else ""

    def wire_for(self, model_id: str = "") -> str:
        """OpenAI wire for this model on the adapter route: "chat" or "responses"."""
        if self.responses_models and re.search(self.responses_models, model_id or ""):
            return "responses"
        return self.wire

    def tool_id_style_for(self, model_id: str = "") -> str:
        if self.tool_id_style != "default":
            return self.tool_id_style
        return "mistral9" if MISTRAL_FAMILY.search(model_id or "") else "default"

    def native_for(self, model_id: str = "") -> bool:
        """True when this model should be sent as Anthropic Messages (native)."""
        if self.route == "native":
            return True
        if self.route != "adapter" or not self.native_models or not self.anthropic_native_url:
            return False
        return re.search(self.native_models, model_id or "") is not None

    def native_base(self) -> str:
        """Anthropic Messages base for native traffic ("" means api.anthropic.com)."""
        return (self.native_url or self.anthropic_native_url).rstrip("/")


def _norm(url: str) -> str:
    return (url or "").strip().rstrip("/").lower()


def _netloc(url: str) -> str:
    """host[:port] of `url`, lower case ("" when it has none)."""
    try:
        return urllib.parse.urlsplit((url or "").strip()).netloc.lower()
    except ValueError:
        return ""


def _base_root(url: str) -> str:
    """A base URL without its trailing /v<N> (where Anthropic-compatible paths usually start)."""
    return re.sub(r"/v\d+$", "", (url or "").strip().rstrip("/"))


def _derive_native(default_base: str, default_native: str, base: str) -> str:
    """`default_native` moved onto `base` the way it sits beside `default_base`, or "" when that is unclear."""
    db = urllib.parse.urlsplit(default_base.strip().rstrip("/"))
    dn = urllib.parse.urlsplit(default_native.strip().rstrip("/"))
    b = urllib.parse.urlsplit((base or "").strip().rstrip("/"))
    if not b.scheme or not b.netloc or db.netloc.lower() != dn.netloc.lower():
        return ""
    origin = b.scheme + "://" + b.netloc
    if b.path.rstrip("/") == db.path.rstrip("/"):
        return origin + dn.path.rstrip("/")             # the same layout on another host
    d_root = _base_root(db.path)
    if dn.path.startswith(d_root) and (dn.path == d_root or dn.path[len(d_root):].startswith("/") or not d_root):
        return origin + _base_root(b.path) + dn.path[len(d_root):].rstrip("/")
    return ""


def _region_for(regions: Tuple[Tuple[str, str, str], ...], base_url: str) -> Optional[Tuple[str, str, str]]:
    want = _norm(base_url)
    for r in regions:
        if _norm(r[1]) == want:
            return r
    return None


_P = Provider
_DEFAULTS: Dict[str, Tuple[str, str, str]] = {}

PROVIDERS: Tuple[Provider, ...] = (
    _P("nvidia", "NVIDIA NIM", "adapter", "https://integrate.api.nvidia.com/v1", "#76B900",
       prefixes=("nvapi-",), env=("NVIDIA_NIM_API_KEY", "NVIDIA_API_KEY", "NVIDIA_NIM_KEY"),
       key_url="https://build.nvidia.com/moonshotai/kimi-k2.6",
       free="free tier, no card",
       prefer=(r"^moonshotai/kimi-k3", r"^moonshotai/kimi-k\d", r"^z-ai/glm-5", r"^deepseek-ai/deepseek-v4",
               r"^nvidia/nemotron-3-(ultra|super)", r"^qwen/qwen\d[\w.]*-coder"),
       fallback_model="moonshotai/kimi-k2.6", max_output=16384,
       models_url="https://integrate.api.nvidia.com/v1/models",
       blurb="Kimi, GLM, DeepSeek, Nemotron and dozens more on NVIDIA's free developer tier"),
    _P("anthropic", "Anthropic", "native", "https://api.anthropic.com/v1", "#D97757",
       native_url="", auth="x-api-key", catalog="anthropic",
       prefixes=("sk-ant-api", "sk-ant-"), env=("ANTHROPIC_API_KEY",),
       key_url="https://console.anthropic.com/settings/keys",
       prefer=(r"^claude-sonnet", r"^claude-opus", r"^claude-haiku"),
       anthropic_native_url="https://api.anthropic.com",
       blurb="Claude, straight from the source"),
    _P("claude", "Claude subscription", "cloud", "https://api.anthropic.com/v1", "#9692AC",
       catalog="none", prefixes=("sk-ant-oat",), env=("CLAUDE_CODE_OAUTH_TOKEN",),
       key_url="https://code.claude.com/docs/en/authentication",
       prefer=(r"^sonnet$", r"^opus$", r"^haiku$"),
       cloud="anthropic", key_env="CLAUDE_CODE_OAUTH_TOKEN",
       blurb="your Pro or Max plan via a `claude setup-token` token"),
    _P("openrouter", "OpenRouter", "adapter", "https://openrouter.ai/api/v1", "#6467F2",
       prefixes=("sk-or-",), env=("OPENROUTER_API_KEY",),
       key_url="https://openrouter.ai/keys", free="free models available",
       prefer=(r"^moonshotai/kimi-k3", r"^anthropic/claude-sonnet", r"^openai/gpt-\d", r"^z-ai/glm-5",
               r"^x-ai/grok-4\.[1-9]$", r"^deepseek/deepseek-v\d", r"^qwen/qwen\d[\w.]*-coder", r"^moonshotai/kimi-k\d"),
       fallback_model="moonshotai/kimi-k3", usage_in_stream=True,
       anthropic_native_url="https://openrouter.ai/api", native_models=r"^anthropic/",
       models_url="https://openrouter.ai/api/v1/models",
       blurb="hundreds of models behind one key"),
    _P("openai", "OpenAI", "adapter", "https://api.openai.com/v1", "#10A37F",
       prefixes=("sk-proj-", "sk-svcacct-"), env=("OPENAI_API_KEY",),
       key_url="https://platform.openai.com/api-keys",
       prefer=(r"^gpt-6(\.\d+)?-(sol|luna)$", r"^gpt-\d+(\.\d+)?$", r"^o\d-mini$", r"^gpt-4o$"),
       fallback_model="gpt-5.5", usage_in_stream=True,
       responses_models=OPENAI_RESPONSES_MODELS,
       blurb="GPT and o-series"),
    _P("gemini", "Google Gemini", "adapter", "https://generativelanguage.googleapis.com/v1beta/openai", "#4285F4",
       prefixes=("AIza", "AQ."), env=("GOOGLE_API_KEY", "GEMINI_API_KEY"),
       key_url="https://aistudio.google.com/apikey", free="free tier",
       prefer=(r"^gemini-[3-9][\d.]*-flash$", r"^gemini-[3-9][\d.]*-pro", r"^gemini-[3-9][\d.]*-flash", r"^gemini"),
       fallback_model="gemini-3.8-flash", strict_schema=True,
       blurb="Gemini Flash and Pro"),
    _P("groq", "Groq", "adapter", "https://api.groq.com/openai/v1", "#F55036",
       prefixes=("gsk_",), env=("GROQ_API_KEY",),
       key_url="https://console.groq.com/keys", free="free tier",
       prefer=(r"^openai/gpt-oss-120b", r"^qwen/qwen\d", r"^llama-\d[\d.]*-70b", r"^openai/gpt-oss"),
       fallback_model="openai/gpt-oss-120b", max_output=16384,
       blurb="very fast inference"),
    _P("xai", "xAI", "adapter", "https://api.x.ai/v1", "#E5E5E5",
       prefixes=("xai-",), env=("XAI_API_KEY",),
       key_url="https://console.x.ai",
       prefer=(r"^grok-4\.\d+$", r"^grok-build", r"^grok"),
       fallback_model="grok-4.7",
       blurb="Grok"),
    _P("deepseek", "DeepSeek", "native", "https://api.deepseek.com", "#4D6BFE",
       native_url="https://api.deepseek.com/anthropic",
       env=("DEEPSEEK_API_KEY",), key_url="https://platform.deepseek.com/api_keys",
       prefer=(r"^deepseek-v4-pro", r"^deepseek-flash"),
       fallback_model="deepseek-flash",
       anthropic_native_url="https://api.deepseek.com/anthropic",
       blurb="DeepSeek V4 via its Anthropic-compatible endpoint"),
    _P("moonshot", "Moonshot Kimi", "native", "https://api.moonshot.ai/v1", "#7C5CFF",
       native_url="https://api.moonshot.ai/anthropic",
       env=("MOONSHOT_API_KEY", "KIMI_API_KEY"), key_url="https://platform.moonshot.ai/console/api-keys",
       prefer=(r"^kimi-k3", r"^kimi-k\d", r"^kimi"),
       fallback_model="kimi-k2.6",
       anthropic_native_url="https://api.moonshot.ai/anthropic",
       regions=(("global", "https://api.moonshot.ai/v1", "https://api.moonshot.ai/anthropic"),
                ("cn", "https://api.moonshot.cn/v1", "https://api.moonshot.cn/anthropic")),
       blurb="Kimi via Moonshot's Anthropic-compatible endpoint"),
    _P("zai", "Z.ai GLM", "native", "https://api.z.ai/api/paas/v4", "#2D6BFF",
       native_url="https://api.z.ai/api/anthropic",
       env=("ZAI_API_KEY", "ZHIPU_API_KEY"), key_url="https://z.ai/manage-apikey/apikey-list",
       prefer=(r"^glm-5", r"^glm-\d"),
       fallback_model="glm-5.3",
       anthropic_native_url="https://api.z.ai/api/anthropic",
       regions=(("global", "https://api.z.ai/api/paas/v4", "https://api.z.ai/api/anthropic"),
                ("coding", "https://api.z.ai/api/coding/paas/v4", "https://api.z.ai/api/anthropic"),
                ("cn", "https://open.bigmodel.cn/api/paas/v4", "https://open.bigmodel.cn/api/anthropic")),
       blurb="GLM via Z.ai's Anthropic-compatible endpoint"),
    _P("minimax", "MiniMax", "native", "https://api.minimax.io/v1", "#FF4F6D",
       native_url="https://api.minimax.io/anthropic",
       prefixes=("sk-cp-", "sk-api-"), env=("MINIMAX_API_KEY",), key_url="https://platform.minimax.io",
       prefer=(r"(?i)^minimax-m\d",),
       fallback_model="MiniMax-M3",
       anthropic_native_url="https://api.minimax.io/anthropic",
       regions=(("global", "https://api.minimax.io/v1", "https://api.minimax.io/anthropic"),
                ("cn", "https://api.minimaxi.com/v1", "https://api.minimaxi.com/anthropic")),
       blurb="MiniMax via its Anthropic-compatible endpoint"),
    _P("mistral", "Mistral", "adapter", "https://api.mistral.ai/v1", "#FA520F",
       env=("MISTRAL_API_KEY",), key_url="https://console.mistral.ai/api-keys",
       free="free tier",
       prefer=(r"^mistral-large", r"^mistral-medium", r"^codestral", r"^devstral"),
       fallback_model="mistral-medium-latest", tool_id_style="mistral9",
       blurb="Mistral Large, Medium and Codestral"),
    _P("cerebras", "Cerebras", "adapter", "https://api.cerebras.ai/v1", "#F15A29",
       prefixes=("csk-", "csk_"), env=("CEREBRAS_API_KEY",),
       key_url="https://cloud.cerebras.ai", free="free tier",
       prefer=(r"gpt-oss-120b", r"qwen-\d", r"llama"),
       fallback_model="gpt-oss-120b", max_output=16384,
       models_url="https://api.cerebras.ai/public/v1/models",
       blurb="wafer-scale speed"),
    _P("together", "Together AI", "adapter", "https://api.together.xyz/v1", "#0F6FFF",
       prefixes=("tgp_v1_", "tgp_"), env=("TOGETHER_API_KEY",),
       key_url="https://api.together.ai/settings/api-keys",
       prefer=(r"(?i)kimi-k\d", r"(?i)qwen\d[\w.]*-coder", r"(?i)glm-5", r"(?i)deepseek-v\d", r"(?i)gpt-oss-120b"),
       anthropic_native_url="https://api.together.xyz",
       blurb="open models at scale"),
    _P("fireworks", "Fireworks AI", "adapter", "https://api.fireworks.ai/inference/v1", "#6720FF",
       prefixes=("fw_",), env=("FIREWORKS_API_KEY",),
       key_url="https://fireworks.ai/account/api-keys",
       prefer=(r"(?i)kimi-k\d", r"(?i)qwen\d[\w.]*-coder", r"(?i)glm-5", r"(?i)deepseek-v\d", r"(?i)gpt-oss-120b"),
       blurb="fast open-model serving"),
    _P("huggingface", "Hugging Face", "adapter", "https://router.huggingface.co/v1", "#FFD21E",
       prefixes=("hf_",), env=("HF_TOKEN", "HUGGINGFACE_API_KEY"),
       key_url="https://huggingface.co/settings/tokens", free="free monthly credits",
       prefer=(r"(?i)kimi-k\d", r"(?i)qwen\d[\w.]*-coder", r"(?i)glm-5", r"(?i)deepseek-v\d", r"(?i)gpt-oss-120b"),
       anthropic_native_url="https://router.huggingface.co",
       models_url="https://router.huggingface.co/v1/models",
       blurb="inference providers router"),
    _P("deepinfra", "DeepInfra", "adapter", "https://api.deepinfra.com/v1/openai", "#3E63DD",
       env=("DEEPINFRA_API_KEY", "DEEPINFRA_TOKEN"), key_url="https://deepinfra.com/dash/api_keys",
       prefer=(r"(?i)kimi-k\d", r"(?i)deepseek-v4", r"(?i)glm-5", r"(?i)minimax-m\d", r"(?i)qwen\d[\w.]*-coder",
               r"(?i)gpt-oss-120b"),
       fallback_model="moonshotai/Kimi-K3",
       anthropic_native_url="https://api.deepinfra.com/anthropic",
       models_url="https://api.deepinfra.com/v1/openai/models",
       blurb="open models, OpenAI and Anthropic endpoints"),
    _P("sambanova", "SambaNova", "adapter", "https://api.sambanova.ai/v1", "#EE7624",
       auth="x-api-key", env=("SAMBANOVA_API_KEY",), key_url="https://cloud.sambanova.ai/apis",
       prefer=(r"(?i)minimax-m\d", r"(?i)deepseek-v\d", r"(?i)gpt-oss-120b", r"(?i)llama"),
       fallback_model="MiniMax-M3",
       anthropic_native_url="https://api.sambanova.ai",
       models_url="https://api.sambanova.ai/v1/models",
       blurb="fast inference on RDUs"),
    _P("vercel", "Vercel AI Gateway", "adapter", "https://ai-gateway.vercel.sh/v1", "#EDEDED",
       prefixes=("vck_",), env=("AI_GATEWAY_API_KEY",), key_url="https://vercel.com/ai-gateway",
       prefer=(r"^moonshotai/kimi-k3", r"^anthropic/claude-sonnet", r"^openai/gpt-\d", r"^zai/glm-5",
               r"^(spacexai|xai)/grok-4\.[1-9]$", r"^deepseek/deepseek-v\d", r"^alibaba/qwen3[\w.]*-coder"),
       anthropic_native_url="https://ai-gateway.vercel.sh", native_models=r"^anthropic/",
       models_url="https://ai-gateway.vercel.sh/v1/models",
       blurb="every major model behind one Vercel key"),
    _P("requesty", "Requesty", "adapter", "https://router.requesty.ai/v1", "#7B61FF",
       env=("REQUESTY_API_KEY",), key_url="https://app.requesty.ai/api-keys",
       prefer=(r"^moonshot/kimi-k3", r"^anthropic/claude-sonnet-\d", r"^openai/gpt-\d", r"^zai/glm-5",
               r"^xai/grok-4\.[1-9]$", r"^deepseek/deepseek-v\d"),
       anthropic_native_url="https://router.requesty.ai", native_models=r"^anthropic/",
       models_url="https://router.requesty.ai/v1/models",
       blurb="LLM router with fallbacks"),
    _P("novita", "Novita AI", "adapter", "https://api.novita.ai/openai/v1", "#23D57C",
       env=("NOVITA_API_KEY",), key_url="https://novita.ai/settings/key-management",
       prefer=(r"^moonshotai/kimi-k3", r"^zai-org/glm-5", r"^deepseek/deepseek-v4", r"^minimax/minimax-m\d",
               r"^qwen/qwen3[\w.]*-coder", r"(?i)kimi-k\d"),
       anthropic_native_url="https://api.novita.ai/anthropic",
       models_url="https://api.novita.ai/openai/v1/models",
       blurb="open models, OpenAI and Anthropic endpoints"),
    _P("dashscope", "Alibaba Qwen", "adapter", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "#FF6A00",
       env=("DASHSCOPE_API_KEY",), key_url="https://www.alibabacloud.com/help/en/model-studio/get-api-key",
       prefer=(r"(?i)^qwen3[\w.]*-coder-plus", r"(?i)^qwen3[\w.]*-max", r"(?i)^qwen3[\w.]*-coder", r"(?i)^qwen"),
       fallback_model="qwen3-coder-plus", usage_in_stream=True,
       anthropic_native_url="https://dashscope-intl.aliyuncs.com/apps/anthropic",
       regions=(("intl", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                 "https://dashscope-intl.aliyuncs.com/apps/anthropic"),
                ("us", "https://dashscope-us.aliyuncs.com/compatible-mode/v1", "")),
       blurb="Qwen via Alibaba Cloud Model Studio (DashScope)"),
    _P("nebius", "Nebius Token Factory", "adapter", "https://api.tokenfactory.nebius.com/v1", "#E0FF4F",
       env=("NEBIUS_API_KEY",), key_url="https://tokenfactory.nebius.com",
       prefer=(r"(?i)kimi-k\d", r"(?i)qwen\d[\w.]*-coder", r"(?i)glm-5", r"(?i)deepseek-v\d", r"(?i)gpt-oss-120b"),
       blurb="open models on Nebius"),
    _P("venice", "Venice", "adapter", "https://api.venice.ai/api/v1", "#DD3300",
       prefixes=("vapi_",), env=("VENICE_API_KEY",), key_url="https://venice.ai/settings/api",
       prefer=(r"^kimi-k3", r"^z-ai-glm-5", r"^deepseek-v4", r"^qwen3-coder", r"(?i)kimi-k\d"),
       models_url="https://api.venice.ai/api/v1/models",
       blurb="private, uncensored inference"),
    _P("chutes", "Chutes", "adapter", "https://llm.chutes.ai/v1", "#00C2A8",
       prefixes=("cpk_",), env=("CHUTES_API_KEY", "CHUTES_API_TOKEN"), key_url="https://chutes.ai/app/api",
       prefer=(r"(?i)kimi-k\d", r"(?i)glm-5", r"(?i)deepseek-v\d", r"(?i)qwen3"),
       models_url="https://llm.chutes.ai/v1/models",
       blurb="decentralised open-model serving"),
    _P("cohere", "Cohere", "adapter", "https://api.cohere.ai/compatibility/v1", "#FF7759",
       catalog="none", env=("COHERE_API_KEY", "CO_API_KEY"), key_url="https://dashboard.cohere.com/api-keys",
       prefer=(r"^command-a", r"^command"),
       fallback_model="command-a-03-2025",
       blurb="Command A via Cohere's compatibility API"),
    _P("azure", "Azure OpenAI", "adapter", "https://YOUR-RESOURCE.openai.azure.com/openai/v1", "#0078D4",
       auth_header="api-key", needs_url=True, usage_in_stream=True,
       key_url="https://ai.azure.com",
       prefer=(r"^gpt-6(\.\d+)?-(sol|luna)$", r"^gpt-\d+(\.\d+)?$", r"^gpt"),
       responses_models=OPENAI_RESPONSES_MODELS,
       blurb="your Azure deployments (model = deployment name)"),
    _P("foundry", "Azure AI Foundry (Claude)", "native", "https://YOUR-RESOURCE.services.ai.azure.com/anthropic",
       "#50E6FF", auth="x-api-key", catalog="none", needs_url=True, native_is_base=True,
       key_url="https://ai.azure.com",
       prefer=(r"^claude-sonnet", r"^claude-opus", r"^claude"),
       blurb="Claude deployments on Microsoft Foundry"),
    _P("bedrock", "Amazon Bedrock", "cloud", "https://bedrock-runtime.us-east-1.amazonaws.com", "#FF9900",
       catalog="none", prefixes=("ABSK", "bedrock-api-key-"), env=("AWS_BEARER_TOKEN_BEDROCK",),
       needs_key=False, key_url="https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys.html",
       prefer=(r"^sonnet$", r"^opus$", r"^haiku$"),
       cloud="bedrock", key_env="AWS_BEARER_TOKEN_BEDROCK",
       blurb="Claude on Bedrock (API key or your AWS credentials)"),
    _P("vertex", "Google Vertex AI", "cloud", "https://aiplatform.googleapis.com/v1", "#34A853",
       catalog="none", needs_key=False, key_url="https://code.claude.com/docs/en/google-vertex-ai",
       prefer=(r"^sonnet$", r"^opus$", r"^haiku$"),
       cloud="vertex",
       blurb="Claude on Vertex AI with your gcloud credentials"),
    _P("ollama", "Ollama", "adapter", "http://127.0.0.1:11434/v1", "#FFFFFF",
       needs_key=False, local=True, free="runs on your machine",
       key_url="https://ollama.com/download",
       prefer=(r"coder", r"qwen", r"gpt-oss", r"llama", r"."),
       anthropic_native_url="http://127.0.0.1:11434",
       blurb="local models, fully offline"),
    _P("lmstudio", "LM Studio", "adapter", "http://127.0.0.1:1234/v1", "#B794F6",
       needs_key=False, local=True, free="runs on your machine",
       key_url="https://lmstudio.ai",
       prefer=(r"coder", r"qwen", r"gpt-oss", r"llama", r"."),
       anthropic_native_url="http://127.0.0.1:1234",
       blurb="local models via LM Studio's server"),
    _P("llamacpp", "llama.cpp", "adapter", "http://127.0.0.1:8080/v1", "#FF8236",
       needs_key=False, local=True,
       key_url="https://github.com/ggml-org/llama.cpp",
       prefer=(r"coder", r"qwen", r"gpt-oss", r"."),
       anthropic_native_url="http://127.0.0.1:8080",
       blurb="llama-server on your machine"),
    _P("vllm", "vLLM", "adapter", "http://127.0.0.1:8000/v1", "#FDB515",
       needs_key=False, local=True,
       key_url="https://docs.vllm.ai",
       prefer=(r"coder", r"qwen", r"gpt-oss", r"."),
       blurb="a local vLLM server"),
    _P("jan", "Jan", "adapter", "http://127.0.0.1:1337/v1", "#2B6CF6",
       needs_key=False, local=True,
       key_url="https://jan.ai",
       prefer=(r"coder", r"qwen", r"gpt-oss", r"."),
       blurb="Jan's local API server"),
    _P("localai", "LocalAI", "adapter", "http://127.0.0.1:8080/v1", "#7C3AED",
       needs_key=False, local=True,
       key_url="https://localai.io",
       prefer=(r"coder", r"qwen", r"gpt-oss", r"."),
       blurb="a local LocalAI server"),
)

BY_ID: Dict[str, Provider] = {p.id: p for p in PROVIDERS}
# Each registry provider's default (base_url, native_url, anthropic_native_url): a base_url override is
# measured against these (Provider._follow_base). Filled only now, so the registry itself is never moved.
_DEFAULTS.update({p.id: (p.base_url, p.native_url, p.anthropic_native_url) for p in PROVIDERS})


def get(provider_id: str) -> Optional[Provider]:
    return BY_ID.get(provider_id)


def keyed_providers() -> List[Provider]:
    """Providers an operator may pick for a bare key of unknown shape."""
    return [p for p in PROVIDERS if p.needs_key and not p.needs_url and p.route != "cloud"]


# ---------------------------------------------------------------------------
# Hosts, transport and printable text
# ---------------------------------------------------------------------------

_RFC1918 = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
_ULA = ipaddress.ip_network("fc00::/7")


def is_local_host(host: str) -> bool:
    """True for hosts whose traffic stays on this machine or the LAN.

    Loopback, RFC 1918, link-local (IPv4 and IPv6), IPv6 unique-local, the
    unspecified address, `localhost` and mDNS `.local` names. A port number
    (11434, ...) proves nothing: anyone can run a server on any port.
    """
    h = (host or "").strip().lower()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    h = h.split("%", 1)[0].rstrip(".")
    if not h:
        return False
    if h == "localhost" or h.endswith((".localhost", ".local")):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    if ip.version == 6 and getattr(ip, "ipv4_mapped", None) is not None:
        ip = ip.ipv4_mapped  # type: ignore[assignment]
    if ip.is_loopback or ip.is_link_local or ip.is_unspecified:
        return True
    if ip.version == 4:
        return any(ip in net for net in _RFC1918)
    return ip in _ULA


def _host_of(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""


def is_local_url(url: str) -> bool:
    return is_local_host(_host_of(url))


def insecure_http(url: str) -> bool:
    """Plain http:// to a host outside this machine and LAN: whatever is sent travels in clear text."""
    try:
        scheme = urllib.parse.urlsplit((url or "").strip()).scheme.lower()
    except ValueError:
        return False
    return scheme == "http" and not is_local_url(url)


def http_allowed(environ: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if environ is None else environ
    return env.get("PULSAR_ALLOW_HTTP") == "1"


def plain_http_refusal(url: str, key: str = "", environ: Optional[Mapping[str, str]] = None) -> str:
    """Why a key must not be sent to `url` (empty when it may)."""
    if not key or not insecure_http(url) or http_allowed(environ):
        return ""
    return ("refusing to send your key over plain http to %s (not a local or LAN host): it would travel "
            "unencrypted. Use https, or set PULSAR_ALLOW_HTTP=1 if you accept that risk." % (_host_of(url) or url))


# What real providers use (vendor/model, model:tag, Vertex's model@version, Bedrock ARNs, Cloudflare's
# @cf/..., Claude Code's `sonnet[1m]` suffix) and nothing a shell, a terminal or a settings file could
# reinterpret: no spaces, quotes, `$`, backticks, `;`, `&`, `|`, `<`, `>`, `*`, `?`, `~`, `#` or backslashes.
_MODEL_ID = re.compile(r"[A-Za-z0-9@][A-Za-z0-9._/:@+=,-]{0,199}(?:\[[A-Za-z0-9]{1,10}\])?")
MODEL_ID_RULE = "letters, digits and . _ / : @ + = , - (an optional [1m]-style suffix), at most 200 characters"
# C0, DEL, C1, zero-width and bidi controls: they can rewrite a terminal (OSC 52 clipboard, titles,
# screen clears) or make one string display as another.
_UNPRINTABLE = re.compile("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028\u2029\u202a-\u202e\u2060-\u2069\ufeff]")


def valid_model_id(model_id: object) -> bool:
    """Model ids are 1 to 200 characters of MODEL_ID_RULE (no spaces, shell or control characters)."""
    return isinstance(model_id, str) and len(model_id) <= 200 and _MODEL_ID.fullmatch(model_id) is not None


def clean_text(text: object, limit: int = 200) -> str:
    """`text` without control characters, safe to print, store or announce."""
    return _UNPRINTABLE.sub("", str(text if text is not None else ""))[:limit]


def custom_provider(base_url: str, native: bool = False, needs_key: bool = True) -> Provider:
    """A provider for an arbitrary endpoint the operator pasted."""
    url = base_url.rstrip("/")
    local = is_local_url(url)
    if native:
        # An Anthropic Messages endpoint lists its models beside /v1/messages; catalog.fetch_live falls back
        # quietly to no list when it does not.
        return Provider("custom", "Custom endpoint", "native", url, "#36E2FF", native_url=url,
                        catalog="anthropic", needs_key=needs_key, local=local, auth="bearer",
                        prefer=(r".",), blurb=url)
    return Provider("custom", "Custom endpoint", "adapter", url, "#36E2FF",
                    needs_key=needs_key, local=local, prefer=(r".",), blurb=url)


def with_region(provider: Provider, region: str) -> Provider:
    """The provider pointed at one of its named regions (KeyError if unknown)."""
    for name, base, _native in provider.regions:
        if name == region:
            return replace(provider, base_url=base)
    raise KeyError("%s has no region %r" % (provider.name, region))


def auth_headers(provider: Provider, key: str, native: bool = False) -> Dict[str, str]:
    """The header that carries `key` for this provider (empty when there is no key)."""
    if not key:
        return {}
    style = provider.auth if native else provider.auth_header
    if style == "x-api-key":
        return {"x-api-key": key}
    if style == "api-key":
        return {"api-key": key}
    return {"Authorization": "Bearer " + key}


# ---------------------------------------------------------------------------
# Cloud routes (Claude Code authenticates by itself)
# ---------------------------------------------------------------------------

BEDROCK_REGION = re.compile(r"^[a-z]{2}(-gov|-iso[a-z]?)?-[a-z]+-\d{1,2}$")
VERTEX_REGION = re.compile(r"^(global|us|eu|[a-z]+-[a-z]+\d{1,2})$")
GCP_PROJECT = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_BEDROCK_HOST = re.compile(r"^https://bedrock-runtime\.([a-z0-9-]+)\.amazonaws\.com/?$")
_VERTEX_PATH = re.compile(r"/v1/projects/([a-z][a-z0-9-]{4,28}[a-z0-9])/locations/([a-z0-9-]+)/?$")


def cloud_base_url(provider: Provider, options: Mapping[str, str]) -> str:
    """Encode cloud options in a base URL, so a profile's base_url persists them.

    Empty when there is nothing to persist (the profile then keeps no override).
    """
    region = (options.get("region") or "").strip()
    if provider.cloud == "bedrock":
        return "https://bedrock-runtime.%s.amazonaws.com" % region if region else ""
    if provider.cloud == "vertex":
        project = (options.get("project") or "").strip()
        if not project:
            return ""
        region = region or "global"
        host = "aiplatform.googleapis.com" if region in ("global", "us", "eu") else region + "-aiplatform.googleapis.com"
        return "https://%s/v1/projects/%s/locations/%s" % (host, project, region)
    return ""


def cloud_options(provider: Provider, base_url: str = "") -> Dict[str, str]:
    """Read back the options cloud_base_url() stored (pass the profile's base_url)."""
    url = (base_url or "").strip()
    if not url or _norm(url) == _norm(BY_ID.get(provider.id, provider).base_url):
        return {}
    if provider.cloud == "bedrock":
        m = _BEDROCK_HOST.match(url)
        return {"region": m.group(1)} if m else {}
    if provider.cloud == "vertex":
        m = _VERTEX_PATH.search(url)
        return {"project": m.group(1), "region": m.group(2)} if m else {}
    return {}


def cloud_missing(provider: Provider, options: Optional[Mapping[str, str]] = None,
                  environ: Optional[Mapping[str, str]] = None) -> List[str]:
    """Settings the launcher still has to ask for: "region" (Bedrock), "project" (Vertex)."""
    options = options or {}
    environ = os.environ if environ is None else environ
    if provider.cloud == "bedrock":
        if not (options.get("region") or environ.get("AWS_REGION") or environ.get("AWS_DEFAULT_REGION")):
            return ["region"]
    if provider.cloud == "vertex":
        if not (options.get("project") or environ.get("ANTHROPIC_VERTEX_PROJECT_ID")
                or environ.get("GOOGLE_CLOUD_PROJECT") or environ.get("GCLOUD_PROJECT")):
            return ["project"]
    return []


def cloud_env(provider: Provider, key: str = "", options: Optional[Mapping[str, str]] = None,
              environ: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Exact variables Claude Code needs for a cloud route.

    The launcher scrubs every routing variable first (launch.SCRUB), then
    applies these. It must NOT set ANTHROPIC_BASE_URL or ANTHROPIC_AUTH_TOKEN
    for a cloud route, and should pin only ANTHROPIC_MODEL (aliases such as
    "sonnet" are fine) when the operator chose a model.

      bedrock   CLAUDE_CODE_USE_BEDROCK=1, AWS_BEARER_TOKEN_BEDROCK=<key> when a
                Bedrock API key was given (else the AWS credential chain),
                AWS_REGION from options, $AWS_REGION or $AWS_DEFAULT_REGION
                (when none is known Claude Code uses the profile region, then
                us-east-1; cloud_missing() reports it so the launcher can ask).
      vertex    CLAUDE_CODE_USE_VERTEX=1, ANTHROPIC_VERTEX_PROJECT_ID, and
                CLOUD_ML_REGION (options, $CLOUD_ML_REGION, else "global").
                Credentials come from gcloud ADC.
      anthropic CLAUDE_CODE_OAUTH_TOKEN=<setup token>.

    Raises ValueError when a required setting is missing.
    """
    options = dict(options or {})
    environ = os.environ if environ is None else environ
    if provider.cloud == "bedrock":
        env = {"CLAUDE_CODE_USE_BEDROCK": "1"}
        if key:
            env["AWS_BEARER_TOKEN_BEDROCK"] = key
        region = options.get("region") or environ.get("AWS_REGION") or environ.get("AWS_DEFAULT_REGION") or ""
        if region:
            env["AWS_REGION"] = region
        return env
    if provider.cloud == "vertex":
        project = (options.get("project") or environ.get("ANTHROPIC_VERTEX_PROJECT_ID")
                   or environ.get("GOOGLE_CLOUD_PROJECT") or environ.get("GCLOUD_PROJECT") or "")
        if not project:
            raise ValueError("Vertex AI needs a Google Cloud project id: type `vertex <project-id> [region]`")
        region = options.get("region") or environ.get("CLOUD_ML_REGION") or "global"
        return {"CLAUDE_CODE_USE_VERTEX": "1", "ANTHROPIC_VERTEX_PROJECT_ID": project, "CLOUD_ML_REGION": region}
    if provider.cloud == "anthropic":
        if not key:
            raise ValueError("a Claude subscription needs a token from `claude setup-token`")
        return {provider.key_env or "CLAUDE_CODE_OAUTH_TOKEN": key}
    raise ValueError("%s is not a cloud route" % provider.name)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

@dataclass
class Detection:
    # "key" | "url" | "local" | "cloud" | "rejected" | "empty" | "unknown"
    #   cloud     keyword setup ("vertex <project> [region]", "bedrock [region]");
    #             options holds project/region, base_url their encoding.
    #   rejected  a credential that can never run a model; key is cleared, note
    #             explains why. It must never be sent anywhere.
    kind: str
    key: str = ""
    candidates: List[Provider] = field(default_factory=list)
    base_url: str = ""
    note: str = ""
    options: Dict[str, str] = field(default_factory=dict)
    # A risk the UI should show in red before anything is sent (plain http to a public host,
    # credentials dropped from a URL). Empty when there is nothing to warn about.
    warning: str = ""

    @property
    def certain(self) -> bool:
        return len(self.candidates) == 1

    @property
    def best(self) -> Optional[Provider]:
        return self.candidates[0] if self.candidates else None


_ENV_ASSIGN = re.compile(r"^(?:export\s+|set\s+|\$env:)?([A-Z][A-Z0-9_]*)\s*=(?!=)\s*(.+)$")
_LOCAL_WORDS = {"ollama": "ollama", "local": "ollama", "lmstudio": "lmstudio", "lm-studio": "lmstudio",
                "lm studio": "lmstudio", "llama.cpp": "llamacpp", "llamacpp": "llamacpp",
                "llama-server": "llamacpp", "vllm": "vllm", "jan": "jan", "localai": "localai"}
_LOOPBACK = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
_LOCAL_PORTS = {1234: "lmstudio", 8080: "llamacpp", 8000: "vllm", 1337: "jan"}

_GH = "This is a GitHub token. Pulsar Code does not run models with GitHub tokens and never sends it anywhere."
_STRIPE = "This is a Stripe key, not an AI provider key. Pulsar Code never sends it anywhere."
_REJECT: Tuple[Tuple[str, str], ...] = (
    ("sk-ant-admin", "This is an Anthropic Admin API key: it manages an organization but cannot run models. "
                     "Create a regular API key (sk-ant-api03-...) at https://console.anthropic.com/settings/keys."),
    ("sk-admin-", "This is an OpenAI Admin key: it manages an organization and is not meant to run models. "
                  "Create a project key (sk-proj-...) at https://platform.openai.com/api-keys."),
    ("pplx-", "This is a Perplexity key. Pulsar Code does not support Perplexity and never sends it anywhere."),
    ("ghp_", _GH), ("gho_", _GH), ("ghu_", _GH), ("ghs_", _GH), ("ghr_", _GH), ("github_pat_", _GH),
    ("sk_live_", _STRIPE), ("sk_test_", _STRIPE), ("rk_live_", _STRIPE), ("rk_test_", _STRIPE),
)
_REJECT_SHAPES: Tuple[Tuple[str, str], ...] = (
    (r"^(AKIA|ASIA)[A-Z2-7]{16}$", "This is an AWS access key id, not a secret. For Bedrock paste a Bedrock API key "
                                   "(ABSK...) or type `bedrock [region]` to use your AWS credentials."),
)

# Shape rules for keys without a unique prefix. Ordered; first match wins.
# "*" appends every other keyed provider, so the operator is always asked.
_SHAPES: Tuple[Tuple[str, Tuple[str, ...], str], ...] = (
    (r"^sk-[0-9a-f]{32}$", ("deepseek", "dashscope"), "32-hex sk- key"),
    (r"^sk-[A-Za-z0-9]{48}$", ("moonshot", "deepseek", "openai"), "48-char sk- key"),
    (r"^sk-[A-Za-z0-9_\-]{20,}$", ("openai", "moonshot", "deepseek", "dashscope"), "sk- key"),
    (r"^sk_[A-Za-z0-9_\-]{16,}$", ("novita", "*"), "sk_ key (Novita uses this shape)"),
    (r"^[0-9a-f]{32}\.[A-Za-z0-9]{10,24}$", ("zai",), "id.secret key"),
    (r"^[A-Za-z0-9]{32}$", ("mistral", "together"), "32-char key"),
    (r"^[0-9a-f]{64}$", ("together", "mistral"), "64-hex key"),
)


def _clean(text: str) -> str:
    text = (text or "").strip()
    # Strip surrounding quotes and a stray "Bearer " a user may have copied.
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'`":
        text = text[1:-1].strip()
    if text.lower().startswith("bearer "):
        text = text[7:].strip()
    return text


def provider_for_env_var(name: str) -> Optional[Provider]:
    for p in PROVIDERS:
        if name in p.env:
            return p
    return None


def _rejected(text: str) -> Optional[str]:
    for prefix, note in _REJECT:
        if text.startswith(prefix):
            return note
    for pattern, note in _REJECT_SHAPES:
        if re.match(pattern, text):
            return note
    return None


_BY_PREFIX: Tuple[Tuple[str, Provider], ...] = tuple(sorted(
    ((prefix, p) for p in PROVIDERS for prefix in p.prefixes), key=lambda item: -len(item[0])))


def _by_prefix(text: str) -> Optional[Provider]:
    for prefix, p in _BY_PREFIX:
        if text.startswith(prefix):
            return p
    if text.startswith("sk-") and "T3BlbkFJ" in text:
        return BY_ID["openai"]  # base64 "OpenAI": every legacy OpenAI key carries it
    return None


def looks_like_key(text: str, min_len: int = 12) -> bool:
    """`text` has a credential's shape: a known vendor prefix, or a credential Pulsar refuses to use."""
    t = _clean(text or "")
    if len(t) < min_len or re.search(r"\s", t):
        return False
    return _rejected(t) is not None or _by_prefix(t) is not None


def _detect_key(text: str) -> Detection:
    note = _rejected(text)
    if note:
        return Detection("rejected", note=note)
    p = _by_prefix(text)
    if p is not None:
        return Detection("key", key=text, candidates=[p])
    for pattern, ids, label in _SHAPES:
        if re.match(pattern, text):
            cands = [BY_ID[i] for i in ids if i != "*"]
            if "*" in ids:
                cands += [x for x in keyed_providers() if x not in cands]
            return Detection("key", key=text, candidates=cands, note=label)
    return Detection("unknown", key=text, candidates=keyed_providers(), note="unrecognized key shape")


def _detect_cloud(word: str, args: List[str]) -> Detection:
    if word == "vertex":
        prov = BY_ID["vertex"]
        if not args:
            return Detection("cloud", candidates=[prov],
                             note="add your Google Cloud project: vertex <project-id> [region]")
        project = args[0].lower()
        region = args[1].lower() if len(args) > 1 else ""
        if not GCP_PROJECT.match(project):
            return Detection("unknown", note="%r is not a Google Cloud project id" % args[0][:40])
        if region and not VERTEX_REGION.match(region):
            return Detection("unknown", note="%r is not a Vertex AI region (try global, us-east5, europe-west1)"
                             % args[1][:40])
        opts = {"project": project, "region": region or "global"}
        base = cloud_base_url(prov, opts)
        return Detection("cloud", candidates=[replace(prov, base_url=base)], base_url=base, options=opts)
    prov = BY_ID["bedrock"]
    if not args:
        return Detection("cloud", candidates=[prov], note="uses your AWS credentials; add a region: bedrock <region>")
    region = args[0].lower()
    if not BEDROCK_REGION.match(region):
        return Detection("unknown", note="%r is not an AWS region (try us-east-1, eu-west-1)" % args[0][:40])
    opts = {"region": region}
    base = cloud_base_url(prov, opts)
    return Detection("cloud", candidates=[replace(prov, base_url=base)], base_url=base, options=opts)


def _host_index() -> Dict[str, Tuple[Provider, str]]:
    """Registry host -> (provider, region name, or "" for the default) for pasted URLs."""
    index: Dict[str, Tuple[Provider, str]] = {}
    for p in PROVIDERS:
        if p.local or p.needs_url or p.route == "cloud":
            continue
        defaults = {urllib.parse.urlsplit(u).hostname for u in (p.base_url, p.native_url, p.anthropic_native_url,
                                                                 p.models_url) if u}
        by_host: Dict[str, List[str]] = {}
        for name, base, nat in p.regions:
            for u in (base, nat):
                host = urllib.parse.urlsplit(u).hostname if u else None
                if host and name not in by_host.setdefault(host, []):
                    by_host[host].append(name)
        for host in defaults:
            if host and host not in index:
                index[host] = (p, "")
        for host, names in by_host.items():
            if host not in index:
                index[host] = (p, names[0] if len(names) == 1 and host not in defaults else "")
    return index


def _registry_url(url: str, host: str) -> Optional[Tuple[Provider, str]]:
    """A pasted URL that belongs to a registry provider: (provider, region name or "")."""
    want = _norm(url)
    for p in PROVIDERS:
        for name, base, nat in p.regions:
            if want in (_norm(base), _norm(nat)):
                return p, name
    return _HOSTS.get(host)


_HOSTS = _host_index()


def _local_base(url: str) -> str:
    return url if re.search(r"/v\d+$", url) else url + "/v1"


HTTP_WARNING = ("plain http to a public host: anything sent there, including a key, travels unencrypted. "
                "Pulsar Code only sends a key to it with PULSAR_ALLOW_HTTP=1.")


def _detect_url(text: str) -> Detection:
    parts = text.split(None, 1)
    url = parts[0].rstrip("/")
    key = _clean(parts[1]) if len(parts) > 1 else ""
    if key and re.search(r"\s", key):
        return Detection("unknown", note="keys never contain spaces")
    if _UNPRINTABLE.search(url):
        return Detection("unknown", note="that URL contains control characters")
    try:
        split = urllib.parse.urlsplit(url)
        host = (split.hostname or "").lower()
        port = split.port
    except ValueError:
        return Detection("unknown", note="that URL could not be parsed")
    if not host:
        return Detection("unknown", note="that URL has no host")
    if key and _rejected(key):
        return Detection("rejected", note=_rejected(key) or "")
    warning = ""
    if "@" in split.netloc:
        # Never keep user:password (or a bare token@) in a saved base URL, a label or a log line.
        netloc = split.netloc.rsplit("@", 1)[1]
        split = split._replace(netloc=netloc)
        url = urllib.parse.urlunsplit(split).rstrip("/")
        warning = "dropped the user:password part of the URL; paste the key on its own instead"
    if split.query or split.fragment or url.endswith(("?", "#")):
        # `?api_key=...` would be saved in config.json and sent with every request: a base URL never
        # carries a query or fragment.
        split = split._replace(query="", fragment="")
        url = urllib.parse.urlunsplit(split).rstrip("/?#")
        warning = warning or "dropped the ?query / #fragment part of the URL; paste the key on its own instead"
    det = _detect_url_split(url, split, host, port, key)
    if not det.warning:
        det.warning = warning
    return det


def _detect_url_split(url: str, split: urllib.parse.SplitResult, host: str, port: Optional[int],
                      key: str) -> Detection:
    if port == 11434 and is_local_host(host):
        return Detection("local", key=key, candidates=[BY_ID["ollama"]], base_url=_local_base(url))
    if host in _LOOPBACK and port in _LOCAL_PORTS:
        return Detection("local", key=key, candidates=[BY_ID[_LOCAL_PORTS[port]]], base_url=_local_base(url))

    path = split.path.rstrip("/")
    if split.scheme == "https" and host.endswith(".services.ai.azure.com") and "/anthropic" in path:
        base = "https://%s/anthropic" % host
        return Detection("url", key=key, candidates=[replace(BY_ID["foundry"], base_url=base)], base_url=base)
    if split.scheme == "https" and host.endswith((".openai.azure.com", ".cognitiveservices.azure.com",
                                                  ".services.ai.azure.com")):
        base = "https://%s/openai/v1" % host
        return Detection("url", key=key, candidates=[replace(BY_ID["azure"], base_url=base)], base_url=base)

    hit = _registry_url(url, host)
    if hit is not None and split.scheme == "https":
        prov, region = hit
        if region:
            prov = with_region(prov, region)
        owner = _by_prefix(key) if key else None
        base = prov.base_url if prov.base_url != BY_ID[prov.id].base_url else ""
        if owner is not None and owner.id != prov.id:
            # Never apply one vendor's URL to another vendor's key: the operator decides.
            return Detection("url", key=key, candidates=[owner, BY_ID[prov.id]], base_url="",
                             note="the key looks like %s but the URL is %s" % (owner.name, prov.name))
        return Detection("url", key=key, candidates=[prov], base_url=base)

    native = bool(re.search(r"/anthropic/?$", url))
    prov = custom_provider(url, native=native, needs_key=bool(key))
    return Detection("url", key=key, candidates=[prov], base_url=url,
                     warning=HTTP_WARNING if insecure_http(url) else "")


def detect(raw: str) -> Detection:
    """Work out what the operator pasted. Never touches the network."""
    text = _clean(raw)
    if not text:
        return Detection("empty")

    assign = _ENV_ASSIGN.match(text)
    if assign and not re.match(r"^https?://", text, re.I):
        var = assign.group(1)
        value = _clean(assign.group(2))
        if re.search(r"\s", value):
            # A whole .env file or several assignments on one line: everything after the first `=`
            # would otherwise become "the key" (and other secrets would be sent as its Bearer value).
            return Detection("unknown", note="keys never contain spaces: paste one key, not a whole .env file")
        by_value = _detect_key(value) if value else None
        if by_value is not None and by_value.kind == "rejected":
            return by_value
        provider = provider_for_env_var(var)
        owner = _by_prefix(value) if by_value is not None and by_value.kind == "key" else None
        if owner is not None and (provider is None or owner.id != provider.id):
            # The value's own prefix outranks the variable name (OPENAI_API_KEY=sk-or-... is OpenRouter's).
            by_value.note = "from " + var
            return by_value
        if provider is not None:
            return Detection("key", key=value, candidates=[provider], note="from " + var)
        text = value

    lowered = text.lower()
    if lowered in _LOCAL_WORDS:
        return Detection("local", candidates=[BY_ID[_LOCAL_WORDS[lowered]]])

    words = lowered.split()
    if words and words[0] in ("vertex", "bedrock") and len(words) <= 3:
        return _detect_cloud(words[0], text.split()[1:])

    if re.match(r"^https?://", lowered):
        return _detect_url(text)

    if re.search(r"\s", text):
        return Detection("unknown", key=text, note="keys never contain spaces")

    return _detect_key(text)


def _all_prefixes() -> List[str]:
    out = [prefix for p in PROVIDERS for prefix in p.prefixes]
    out += [prefix for prefix, _ in _REJECT]
    return out


_MASK_PREFIXES = tuple(sorted(set(_all_prefixes()), key=len, reverse=True))


def mask(key: str, keep_prefix: Optional[int] = None) -> str:
    """Mask a secret for display. Reveals the vendor prefix and last 4."""
    if not key:
        return ""
    if keep_prefix is None:
        keep_prefix = 0
        for prefix in _MASK_PREFIXES:
            if key.startswith(prefix):
                keep_prefix = len(prefix)
                break
        if keep_prefix == 0 and key.startswith(("sk-", "sk_")):
            keep_prefix = 3
    if len(key) <= keep_prefix + 8:
        return key[:keep_prefix] + "•" * max(4, len(key) - keep_prefix)
    return key[:keep_prefix] + "•" * 8 + key[-4:]


def env_keys(environ: Mapping[str, str]) -> List[Tuple[Provider, str, str]]:
    """Keys already exported in the operator's shell: (provider, var, value).

    A value whose prefix proves it belongs to another provider is filed under
    that provider (OPENAI_API_KEY=sk-or-... is an OpenRouter key); values that
    can never run a model (admin keys, ...) are skipped.
    """
    found: List[Tuple[Provider, str, str]] = []
    seen = set()
    for p in PROVIDERS:
        for var in p.env:
            value = (environ.get(var) or "").strip()
            if not value:
                continue
            if _rejected(value):
                continue
            prov = _by_prefix(value) or p
            if prov.id not in seen:
                found.append((prov, var, value))
                seen.add(prov.id)
    return found
