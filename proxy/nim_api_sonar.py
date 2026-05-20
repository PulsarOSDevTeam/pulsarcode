#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""PulsarOS API Sonar for public NVIDIA NIM model discovery.

The scanner merges official NVIDIA reference pages, NVIDIA build pages,
optional operator-provided public URLs, and the authenticated /v1/models
endpoint when available. It never probes private networks or guesses
unauthorized endpoints.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


DEFAULT_MODEL = "moonshotai/kimi-k2.6"
DEFAULT_PUBLIC_MODEL = "nim-kimi"
DEFAULT_API_BASE = "https://integrate.api.nvidia.com/v1"
DEFAULT_CACHE_PATH = Path(os.path.expanduser("~/.pulsarcode/nim_sonar_catalog.json"))
DEFAULT_TIMEOUT_S = 2.5
DEFAULT_CACHE_TTL_S = 6 * 60 * 60
CACHE_SCHEMA_VERSION = 2
CLAUDE_CODE_SELECTOR_PREFIX = "claude-"

OFFICIAL_SOURCES: Tuple[Tuple[str, str], ...] = (
    ("nvidia-llm-reference", "https://docs.api.nvidia.com/nim/reference/llm-apis"),
    ("nvidia-build-models", "https://build.nvidia.com/models"),
    ("nvidia-build-moonshotai", "https://build.nvidia.com/moonshotai"),
    ("nvidia-kimi-k2-6-reference", "https://docs.api.nvidia.com/nim/reference/moonshotai-kimi-k2-6"),
)

STATIC_OFFICIAL_MODELS: Tuple[str, ...] = (
    "moonshotai/kimi-k2.6",
    "moonshotai/kimi-k2-thinking",
    "moonshotai/kimi-k2-instruct",
    "abacusai/dracarys-llama-3.1-70b-instruct",
    "bytedance/seed-oss-36b-instruct",
    "deepseek-ai/deepseek-v4-flash",
    "deepseek-ai/deepseek-v4-pro",
    "google/codegemma-7b",
    "google/gemma-2-2b-it",
    "google/gemma-7b",
    "meta/llama2-70b",
    "meta/llama-3.1-8b-instruct",
    "meta/llama-3.1-70b-instruct",
    "meta/llama-3.2-1b-instruct",
    "meta/llama-3.2-3b-instruct",
    "meta/llama-3.3-70b-instruct",
    "microsoft/phi-4-mini-instruct",
    "microsoft/phi-4-mini-flash-reasoning",
    "minimaxai/minimax-m2.5",
    "minimaxai/minimax-m2.7",
    "mistralai/magistral-small-2506",
    "mistralai/mistral-7b-instruct-v0.3",
    "mistralai/mistral-nemotron",
    "mistralai/mixtral-8x22b-instruct",
    "mistralai/mixtral-8x7b-instruct",
    "nvidia/gliner-pii",
    "nvidia/llama-3.1-nemoguard-8b-content-safety",
    "nvidia/llama-3.1-nemoguard-8b-topic-control",
    "nvidia/llama-3.1-nemotron-nano-8b-v1",
    "nvidia/llama-3.3-nemotron-super-49b-v1",
    "nvidia/llama-3.3-nemotron-super-49b-v1.5",
    "nvidia/llama-3.1-nemotron-ultra-253b-v1",
    "nvidia/nemoguard-jailbreak-detect",
    "nvidia/nemotron-3-nano-30b-a3b",
    "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/nemotron-content-safety-reasoning-4b",
    "nvidia/nemotron-mini-4b-instruct",
    "nvidia/nvidia-nemotron-nano-9b-v2",
    "nvidia/riva-translate-4b-instruct-v1_1",
    "nvidia/usdcode",
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
    "qwen/qwen2.5-coder-7b-instruct",
    "qwen/qwen2.5-coder-32b-instruct",
    "qwen/qwen3-5-122b-a10b",
    "qwen/qwen3-coder-480b-a35b-instruct",
    "qwen/qwen3-next-80b-a3b-instruct",
    "qwen/qwen3-next-80b-a3b-thinking",
    "qwen/qwq-32b",
    "sarvamai/sarvam-m",
    "stepfun-ai/step-3-5-flash",
    "stockmark/stockmark-2-100b-instruct",
    "upstage/solar-10.7b-instruct",
    "z-ai/glm4.7",
    "z-ai/glm5.1",
)

STATIC_OFFICIAL_MODEL_SET = set(STATIC_OFFICIAL_MODELS)

PROVIDER_RE = re.compile(r"https?://build\.nvidia\.com/([a-z0-9_.-]+)(?:[/#?]|$)", re.I)
PAIR_RE = re.compile(r"\b([a-z][a-z0-9_.-]{1,48})\s*/\s*([a-z0-9][a-z0-9_.-]{2,96})\b", re.I)
HEADING_RE = re.compile(r"^\s*#{2,4}\s+([a-z0-9][a-z0-9_.-]{2,96})\s*$", re.I | re.M)

BAD_PROVIDERS = {
    "api",
    "docs",
    "endpoint",
    "http",
    "https",
    "model",
    "models",
    "nim",
    "post",
    "reference",
    "v1",
}


@dataclass(frozen=True)
class NIMModelRecord:
    upstream_id: str
    alias: str
    provider: str
    model: str
    source: str
    source_url: str
    confidence: str = "official"
    context_tokens: Optional[int] = None
    tags: Tuple[str, ...] = ()

    def to_anthropic_model(
        self,
        model_id: Optional[str] = None,
        display_name: Optional[str] = None,
        created_at: str = "2026-05-19T00:00:00Z",
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "id": model_id or self.alias,
            "type": "model",
            "display_name": display_name or display_name_for_record(self),
            "created_at": created_at,
        }
        if self.context_tokens is not None:
            payload["max_input_tokens"] = self.context_tokens
        if self.upstream_id == DEFAULT_MODEL:
            payload["max_tokens"] = 16000
        return payload


def _split_upstream(upstream_id: str) -> Optional[Tuple[str, str]]:
    clean = upstream_id.strip().lower()
    if "/" not in clean:
        return None
    provider, model = clean.split("/", 1)
    provider = provider.strip()
    model = model.strip()
    if provider in BAD_PROVIDERS or not provider or not model:
        return None
    if not re.fullmatch(r"[a-z0-9_.-]+", provider):
        return None
    if not re.fullmatch(r"[a-z0-9_.-]+", model):
        return None
    return provider, model


def alias_for_model(upstream_id: str, public_default: str = DEFAULT_PUBLIC_MODEL) -> str:
    split = _split_upstream(upstream_id)
    if split is None:
        clean = re.sub(r"[^a-z0-9]+", "-", upstream_id.lower()).strip("-")
        return "nim-" + clean
    provider, model = split
    if upstream_id == DEFAULT_MODEL:
        return public_default
    compact = re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")
    if compact.startswith("kimi-k2"):
        return "nim-" + compact
    return "nim-" + provider.replace(".", "-") + "-" + compact


def claude_code_selector_alias_for_model(
    upstream_id: str,
    public_default: str = DEFAULT_PUBLIC_MODEL,
) -> str:
    return CLAUDE_CODE_SELECTOR_PREFIX + alias_for_model(upstream_id, public_default)


def display_name_for_record(record: NIMModelRecord, selector: bool = False) -> str:
    if record.upstream_id == DEFAULT_MODEL:
        return "Pulsar Kimi K2.6"
    if record.model == "kimi-k2-thinking":
        return "Pulsar Kimi K2 Thinking"
    words = re.split(r"[-_.]+", record.model)
    title = " ".join(part.upper() if len(part) <= 3 else part.capitalize() for part in words if part)
    provider = record.provider.replace("-", " ").replace("_", " ").title()
    prefix = "Pulsar NIM" if selector else "NIM"
    return f"{prefix} {provider} {title}".strip()


def normalize_requested_model_alias(requested: str) -> str:
    clean = re.sub(r"\s+", "", str(requested or "").strip().lower())
    for suffix in ("[1m]", "[long]"):
        if clean.endswith(suffix):
            clean = clean[: -len(suffix)]
            break
    if clean.startswith(CLAUDE_CODE_SELECTOR_PREFIX):
        candidate = clean[len(CLAUDE_CODE_SELECTOR_PREFIX):]
        if candidate.startswith("nim-"):
            return candidate
    return clean


def record_for_upstream(
    upstream_id: str,
    source: str,
    source_url: str,
    confidence: str = "official",
    tags: Sequence[str] = (),
    public_default: str = DEFAULT_PUBLIC_MODEL,
) -> Optional[NIMModelRecord]:
    split = _split_upstream(upstream_id)
    if split is None:
        return None
    provider, model = split
    context_tokens = 256000 if upstream_id in {"moonshotai/kimi-k2.6", "moonshotai/kimi-k2-thinking"} else None
    tag_set = tuple(dict.fromkeys(tags))
    if upstream_id == DEFAULT_MODEL:
        tag_set = tuple(dict.fromkeys(("default", "kimi-k2.6", "256k-context", *tag_set)))
    return NIMModelRecord(
        upstream_id=upstream_id,
        alias=alias_for_model(upstream_id, public_default),
        provider=provider,
        model=model,
        source=source,
        source_url=source_url,
        confidence=confidence,
        context_tokens=context_tokens,
        tags=tag_set,
    )


def static_catalog(public_default: str = DEFAULT_PUBLIC_MODEL) -> List[NIMModelRecord]:
    out: List[NIMModelRecord] = []
    for model in STATIC_OFFICIAL_MODELS:
        rec = record_for_upstream(
            model,
            source="static-official-seed",
            source_url="https://docs.api.nvidia.com/nim/reference/llm-apis",
            tags=("official-seed",),
            public_default=public_default,
        )
        if rec is not None:
            out.append(rec)
    return out


def extract_model_ids(text: str, source_url: str = "") -> List[str]:
    if source_url.endswith("/moonshotai-kimi-k2-6"):
        return ["moonshotai/kimi-k2.6"]
    if source_url.endswith("/llm-apis"):
        stop = text.find("## Retrieval")
        if stop != -1:
            text = text[:stop]
    found: List[str] = []
    for provider, model in PAIR_RE.findall(text):
        upstream = f"{provider.lower()}/{model.lower()}"
        if _split_upstream(upstream) is not None:
            found.append(upstream)
    match = PROVIDER_RE.search(source_url)
    if match:
        provider = match.group(1).lower()
        for model in HEADING_RE.findall(text):
            upstream = f"{provider}/{model.lower()}"
            if _split_upstream(upstream) is not None:
                found.append(upstream)
    return list(dict.fromkeys(found))


def fetch_text(url: str, timeout_s: float = DEFAULT_TIMEOUT_S) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("API Sonar accepts HTTPS public sources only")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "pulsarcode-api-sonar/0.1"},
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        data = response.read(2_000_000)
    return data.decode("utf-8", errors="replace")


def discover_from_public_sources(
    sources: Iterable[Tuple[str, str]],
    timeout_s: float = DEFAULT_TIMEOUT_S,
    public_default: str = DEFAULT_PUBLIC_MODEL,
) -> List[NIMModelRecord]:
    out: List[NIMModelRecord] = []
    official_urls = {url for _, url in OFFICIAL_SOURCES}
    for source, url in sources:
        try:
            text = fetch_text(url, timeout_s=timeout_s)
        except (OSError, urllib.error.URLError, ValueError):
            continue
        for upstream in extract_model_ids(text, url):
            if url in official_urls and upstream not in STATIC_OFFICIAL_MODEL_SET:
                continue
            rec = record_for_upstream(
                upstream,
                source=source,
                source_url=url,
                confidence="public",
                tags=("public-source",),
                public_default=public_default,
            )
            if rec is not None:
                out.append(rec)
    return out


def discover_from_authorized_api(
    api_base: str,
    api_key: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    public_default: str = DEFAULT_PUBLIC_MODEL,
) -> List[NIMModelRecord]:
    if not api_key:
        return []
    base = api_base.rstrip("/")
    url = base + "/models"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bearer " + api_key,
            "User-Agent": "pulsarcode-api-sonar/0.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            payload = json.loads(response.read(2_000_000).decode("utf-8", errors="replace"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return []
    out: List[NIMModelRecord] = []
    for item in payload.get("data", []) if isinstance(payload, dict) else []:
        raw_id = str(item.get("id", "")).strip().lower()
        if "/" not in raw_id:
            continue
        rec = record_for_upstream(
            raw_id,
            source="authorized-api",
            source_url=url,
            confidence="authorized",
            tags=("key-visible",),
            public_default=public_default,
        )
        if rec is not None:
            out.append(rec)
    return out


def custom_sources_from_env() -> List[Tuple[str, str]]:
    raw = os.environ.get("PULSAR_NIM_SONAR_SOURCES", "")
    sources: List[Tuple[str, str]] = []
    for index, item in enumerate(part.strip() for part in raw.split(",") if part.strip()):
        sources.append((f"operator-public-source-{index + 1}", item))
    return sources


def merge_records(records: Iterable[NIMModelRecord], public_default: str = DEFAULT_PUBLIC_MODEL) -> List[NIMModelRecord]:
    by_upstream: Dict[str, NIMModelRecord] = {}
    for rec in records:
        current = by_upstream.get(rec.upstream_id)
        if current is None:
            by_upstream[rec.upstream_id] = rec
            continue
        tags = tuple(dict.fromkeys((*current.tags, *rec.tags)))
        confidence = "authorized" if "authorized" in {current.confidence, rec.confidence} else current.confidence
        by_upstream[rec.upstream_id] = NIMModelRecord(
            upstream_id=current.upstream_id,
            alias=current.alias,
            provider=current.provider,
            model=current.model,
            source=current.source + "+" + rec.source,
            source_url=current.source_url,
            confidence=confidence,
            context_tokens=current.context_tokens or rec.context_tokens,
            tags=tags,
        )
    ordered = list(by_upstream.values())
    ordered.sort(key=lambda item: (0 if item.upstream_id == DEFAULT_MODEL else 1, item.provider, item.model))
    if DEFAULT_MODEL not in by_upstream:
        default_rec = record_for_upstream(
            DEFAULT_MODEL,
            source="default",
            source_url="https://docs.api.nvidia.com/nim/reference/moonshotai-kimi-k2-6",
            tags=("default",),
            public_default=public_default,
        )
        if default_rec is not None:
            ordered.insert(0, default_rec)
    return ordered


def read_cache(cache_path: Path, ttl_s: float = DEFAULT_CACHE_TTL_S) -> Optional[List[NIMModelRecord]]:
    try:
        stat = cache_path.stat()
    except OSError:
        return None
    if time.time() - stat.st_mtime > ttl_s:
        return None
    try:
        payload = json.loads(cache_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("schema_version") != CACHE_SCHEMA_VERSION:
        return None
    out: List[NIMModelRecord] = []
    for item in payload.get("models", []):
        try:
            out.append(NIMModelRecord(**item))
        except TypeError:
            continue
    return out or None


def write_cache(cache_path: Path, records: Sequence[NIMModelRecord]) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": int(time.time()),
            "schema_version": CACHE_SCHEMA_VERSION,
            "models": [asdict(record) for record in records],
        }
        cache_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        os.chmod(cache_path, 0o600)
    except OSError:
        return


def discover_catalog(
    api_base: str = DEFAULT_API_BASE,
    api_key: str = "",
    public_default: str = DEFAULT_PUBLIC_MODEL,
    use_network: bool = True,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    cache_path: Path = DEFAULT_CACHE_PATH,
    cache_ttl_s: float = DEFAULT_CACHE_TTL_S,
) -> List[NIMModelRecord]:
    cached = read_cache(cache_path, ttl_s=cache_ttl_s)
    if cached is not None:
        return merge_records(cached, public_default=public_default)

    records: List[NIMModelRecord] = static_catalog(public_default=public_default)
    if use_network:
        records.extend(discover_from_public_sources(OFFICIAL_SOURCES, timeout_s=timeout_s, public_default=public_default))
        records.extend(discover_from_public_sources(custom_sources_from_env(), timeout_s=timeout_s, public_default=public_default))
        records.extend(discover_from_authorized_api(api_base, api_key, timeout_s=timeout_s, public_default=public_default))
    merged = merge_records(records, public_default=public_default)
    write_cache(cache_path, merged)
    return merged


_CATALOG_CACHE: Tuple[float, List[NIMModelRecord]] = (0.0, [])


def runtime_catalog(
    api_base: str,
    api_key: str,
    public_default: str = DEFAULT_PUBLIC_MODEL,
) -> List[NIMModelRecord]:
    global _CATALOG_CACHE
    ttl_s = float(os.environ.get("PULSAR_NIM_SONAR_RUNTIME_TTL", "900"))
    now = time.time()
    if _CATALOG_CACHE[1] and now - _CATALOG_CACHE[0] < ttl_s:
        return _CATALOG_CACHE[1]
    use_network = os.environ.get("PULSAR_NIM_SONAR_NETWORK", "1") != "0"
    timeout_s = float(os.environ.get("PULSAR_NIM_SONAR_TIMEOUT", str(DEFAULT_TIMEOUT_S)))
    records = discover_catalog(
        api_base=api_base,
        api_key=api_key,
        public_default=public_default,
        use_network=use_network,
        timeout_s=timeout_s,
    )
    _CATALOG_CACHE = (now, records)
    return records


def resolve_model_alias(
    requested: str,
    api_base: str,
    api_key: str,
    default_model: str = DEFAULT_MODEL,
    public_default: str = DEFAULT_PUBLIC_MODEL,
) -> str:
    clean = normalize_requested_model_alias(requested)
    if clean in {public_default, default_model, ""}:
        return default_model
    for record in runtime_catalog(api_base=api_base, api_key=api_key, public_default=public_default):
        selector_alias = claude_code_selector_alias_for_model(record.upstream_id, public_default)
        if clean in {record.alias, record.upstream_id, selector_alias}:
            return record.upstream_id
    return default_model


def _selector_model_payload(record: NIMModelRecord, public_default: str) -> Dict[str, Any]:
    return record.to_anthropic_model(
        model_id=claude_code_selector_alias_for_model(record.upstream_id, public_default),
        display_name=display_name_for_record(record, selector=True),
    )


def openai_models_payload(
    records: Sequence[NIMModelRecord],
    include_selector_aliases: bool = True,
    include_primary_aliases: bool = False,
    public_default: str = DEFAULT_PUBLIC_MODEL,
) -> Dict[str, Any]:
    data = [record.to_anthropic_model() for record in records] if include_primary_aliases else []
    if include_selector_aliases:
        data.extend(_selector_model_payload(record, public_default) for record in records)
    return {
        "data": data,
        "first_id": data[0]["id"] if data else None,
        "has_more": False,
        "last_id": data[-1]["id"] if data else None,
    }


def claude_code_settings_payload(
    records: Sequence[NIMModelRecord],
    public_default: str = DEFAULT_PUBLIC_MODEL,
) -> Dict[str, Any]:
    available = [
        claude_code_selector_alias_for_model(record.upstream_id, public_default)
        for record in records
    ]
    return {"availableModels": list(dict.fromkeys(available))}


def _print_table(records: Sequence[NIMModelRecord]) -> None:
    print("pulsarcode API Sonar")
    print(f"models: {len(records)}")
    print()
    for record in records:
        context = f" context={record.context_tokens}" if record.context_tokens else ""
        tags = ",".join(record.tags)
        print(f"{record.alias:48} {record.upstream_id:52} {record.confidence:10} {tags}{context}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover public NVIDIA NIM model aliases for pulsarcode.")
    parser.add_argument("--api-base", default=os.environ.get("NVIDIA_NIM_API_BASE", DEFAULT_API_BASE))
    parser.add_argument("--public-model", default=os.environ.get("PULSAR_NIM_PUBLIC_MODEL", DEFAULT_PUBLIC_MODEL))
    parser.add_argument("--no-network", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--claude-settings", action="store_true")
    args = parser.parse_args()

    key = os.environ.get("NVIDIA_NIM_API_KEY") or os.environ.get("NVIDIA_API_KEY") or ""
    records = discover_catalog(
        api_base=args.api_base,
        api_key=key,
        public_default=args.public_model,
        use_network=not args.no_network,
    )
    if args.claude_settings:
        print(json.dumps(claude_code_settings_payload(records, public_default=args.public_model), indent=2, sort_keys=True))
    elif args.json:
        print(json.dumps([asdict(record) for record in records], indent=2, sort_keys=True))
    else:
        _print_table(records)


if __name__ == "__main__":
    main()
