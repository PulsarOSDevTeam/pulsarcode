# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
from pulsar import catalog as C
from pulsar import providers as P


def M(i, profile="p", **kw):
    return C.Model(i, profile, **kw)


def test_parse_filters_non_chat_and_tags():
    prov = P.get("openrouter")
    payload = {"data": [
        {"id": "moonshotai/kimi-k2", "context_length": 131072, "pricing": {"prompt": "0", "completion": "0"},
         "supported_parameters": ["tools", "reasoning"]},
        {"id": "openai/text-embedding-3-large"},
        {"id": "qwen/qwen3-coder:free", "pricing": {"prompt": "0.1", "completion": "0.1"},
         "supported_parameters": ["temperature"]},
        {"id": "deepseek/deepseek-r1"},
    ]}
    ms = {m.id: m for m in C.parse_models(prov, "openrouter", payload)}
    assert "openai/text-embedding-3-large" not in ms
    assert ms["moonshotai/kimi-k2"].context == 131072 and ms["moonshotai/kimi-k2"].free
    assert "no-tools" in ms["qwen/qwen3-coder:free"].tags and ms["qwen/qwen3-coder:free"].free
    assert ms["deepseek/deepseek-r1"].reasoning


def test_gemini_prefix_stripped_and_anthropic_shape():
    ms = C.parse_models(P.get("gemini"), "gemini", {"data": [{"id": "models/gemini-3.8-flash"}]})
    assert ms[0].id == "gemini-3.8-flash" and ms[0].context == 1_048_576
    ms = C.parse_models(P.get("anthropic"), "anthropic",
                        {"data": [{"id": "claude-sonnet-4-5", "display_name": "Claude Sonnet 4.5"}]})
    assert ms[0].name == "Claude Sonnet 4.5"


def test_picks_follow_provider_preference_on_current_catalogs():
    def ids(*names):
        return [M(n) for n in names]
    assert C.recommended(ids("deepseek-v4-pro", "deepseek-flash"), P.get("deepseek")) == "deepseek-v4-pro"
    assert C.recommended([], P.get("deepseek")) == "deepseek-flash"
    oa = ids("gpt-5.3-codex", "gpt-5.5", "gpt-6.1-sol", "gpt-6-luna", "gpt-4o")
    assert C.recommended(oa, P.get("openai")) == "gpt-6.1-sol"
    gem = ids("gemini-2.5-pro", "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.1-pro-preview")
    assert C.recommended(gem, P.get("gemini")) == "gemini-3.8-flash"
    xai = ids("grok-4.7", "grok-4.6", "grok-4.3", "grok-build-0.1", "grok-4.20-0309-reasoning")
    assert C.recommended(xai, P.get("xai")) == "grok-4.7"
    orr = ids("x-ai/grok-4.20", "x-ai/grok-4.7", "moonshotai/kimi-k3", "anthropic/claude-sonnet-5.5")
    top = [m.id for m in C.picks(orr, P.get("openrouter"))]
    assert top[0] == "moonshotai/kimi-k3" and "x-ai/grok-4.7" in top and "x-ai/grok-4.20" not in top
    assert C.recommended(ids("MiniMax-M2.7", "MiniMax-M3"), P.get("minimax")) == "MiniMax-M3"
    assert C.recommended(ids("openai/gpt-oss-120b", "llama-3.3-70b-versatile"), P.get("groq")) == "openai/gpt-oss-120b"
    assert C.recommended(ids("gpt-oss-120b", "qwen-3.8-27b"), P.get("cerebras")) == "gpt-oss-120b"


def test_reasoning_heuristics():
    assert C.is_reasoning("deepseek-reasoner")
    assert C.is_reasoning("o3-mini") and C.is_reasoning("openai/o4-mini")
    assert C.is_reasoning("qwen/qwq-thinking")
    assert not C.is_reasoning("moonshotai/kimi-k2.6")
    assert not C.is_reasoning("gpt-4o")


def test_version_key_prefers_newest_undated():
    ids = ["gpt-5", "gpt-5.1", "gpt-4.1", "gpt-5-2025-08-07"]
    assert sorted(ids, key=C.version_key)[0] == "gpt-5.1"
    assert sorted(["kimi-k2-instruct", "kimi-k2.6"], key=C.version_key)[0] == "kimi-k2.6"


def test_picks_follow_provider_preference():
    nv = P.get("nvidia")
    models = [M(i, "nvidia") for i in ("meta/llama-3.3-70b-instruct", "qwen/qwen3-coder-480b-a35b-instruct",
                                       "moonshotai/kimi-k2.6", "moonshotai/kimi-k2-instruct")]
    top = C.picks(models, nv)
    assert top[0].id == "moonshotai/kimi-k2.6"
    assert C.recommended(models, nv) == "moonshotai/kimi-k2.6"
    assert C.recommended([], nv) == "moonshotai/kimi-k2.6"


def test_tier_letter_boundaries():
    assert C.tier_of(M("minimaxai/minimax-m2.7")) == C.TIER_GENERAL  # 'mini' must not match 'minimax'
    assert C.tier_of(M("microsoft/phi-4-mini-instruct")) == C.TIER_FAST
    assert C.tier_of(M("qwen/qwen3-coder-480b")) == C.TIER_CODING
    assert C.tier_of(M("x/thing", tags=("reasoning",))) == C.TIER_REASONING
    assert C.tier_of(M("zzz/unknown-thing")) == C.TIER_OTHER


def test_group_recent_first_and_unique():
    nv = P.get("nvidia")
    models = [M(i, "nvidia") for i in ("moonshotai/kimi-k2.6", "meta/llama-3.1-8b-instruct", "z/zz")]
    groups = C.group(models, {"nvidia": nv}, recent=[("nvidia", "z/zz"), ("nvidia", "ghost")])
    assert groups[0][0] == C.TIER_RECENT and [m.id for m in groups[0][1]] == ["z/zz"]
    flat = [m.id for _, items in groups for m in items]
    assert len(flat) == len(set(flat)) == 3


def test_fuzzy_search():
    models = [M("moonshotai/kimi-k2.6"), M("qwen/qwen3-coder-480b"), M("meta/llama-3.3-70b")]
    assert C.search(models, "kimi", {})[0].id == "moonshotai/kimi-k2.6"
    assert C.search(models, "qc480", {})[0].id == "qwen/qwen3-coder-480b"
    assert C.search(models, "zzzz", {}) == []
    assert len(C.search(models, "", {})) == 3


def test_cache_roundtrip_and_offline_fallback(pulsar_home):
    prov = P.get("nvidia")
    res = C.load_catalog(prov, "nvidia", "")
    assert res.source == "seed" and any(m.id == "moonshotai/kimi-k2.6" for m in res.models)
    C.write_cache("nvidia", [M("a/b", "nvidia", tags=("free",))])
    res = C.load_catalog(prov, "nvidia", "")
    assert res.source == "cache" and res.models[0].tags == ("free",)
    import os
    mode = os.stat(C.cache_file("nvidia")).st_mode & 0o777
    assert mode == 0o600


def test_size_parsing_and_fast_tier():
    assert C.param_billions("nvidia/nemotron-3-super-120b-a12b") == 120
    assert C.param_billions("qwen/qwen3-coder-480b-a35b-instruct") == 480
    assert C.param_billions("google/gemma-4-31b-it") == 31
    assert C.param_billions("moonshotai/kimi-k2.6") == 0
    assert C.tier_of(M("nvidia/nemotron-3-super-120b-a12b")) == C.TIER_GENERAL
    assert C.tier_of(M("google/gemma-4-31b-it")) == C.TIER_GENERAL
    assert C.tier_of(M("mistralai/mistral-7b-instruct-v0.3")) == C.TIER_FAST
    assert C.tier_of(M("google/gemini-2.5-flash")) == C.TIER_FAST
    assert C.tier_of(M("z-ai/glm-5.3-flash")) == C.TIER_CODING
    assert C.tier_of(M("moonshotai/kimi-k3")) == C.TIER_CODING
    assert C.tier_of(M("deepseek-flash")) == C.TIER_CODING


def test_next_generation_models_win_picks():
    nv = P.get("nvidia")
    models = [M(i, "nvidia") for i in ("moonshotai/kimi-k2.6", "moonshotai/kimi-k3")]
    assert C.recommended(models, nv) == "moonshotai/kimi-k3"
    orr = P.get("openrouter")
    models = [M(i, "openrouter") for i in ("anthropic/claude-sonnet-5.5:batch", "anthropic/claude-sonnet-5.5")]
    assert C.picks(models, orr)[0].id == "anthropic/claude-sonnet-5.5"


def test_non_chat_models_dropped():
    ms = C.parse_models(P.get("nvidia"), "nvidia", {"data": [{"id": "nvidia/nemotron-4-340b-reward"},
                                                             {"id": "nvidia/riva-translate-4b-instruct"},
                                                             {"id": "meta/llama-3.3-70b-instruct"}]})
    assert [m.id for m in ms] == ["meta/llama-3.3-70b-instruct"]


def test_nvidia_live_shape_filters_every_non_chat_family():
    junk = ["nvidia/embed-qa-4", "nvidia/llama-3.1-nemoguard-8b-content-safety", "nvidia/nemotron-3.5-content-safety",
            "nvidia/nemotron-4-340b-reward", "nvidia/nemotron-parse", "nvidia/nemotron-parse-2.0", "nvidia/nvclip",
            "nvidia/riva-translate-4b-instruct-v2", "nvidia/llama-3.2-nemoretriever-1b-vlm-embed-v1",
            "nvidia/ising-calibration-1.5-31b", "nvidia/ai-synthetic-video-detector", "meta/llama-guard-4-12b",
            "snowflake/arctic-embed-l", "google/deplot", "nvidia/vila", "adept/fuyu-8b"]
    keep = ["moonshotai/kimi-k3", "z-ai/glm-5.3", "nvidia/nemotron-3-super-120b-a12b", "deepseek-ai/deepseek-v4.1-flash"]
    payload = {"object": "list", "data": [{"id": i, "object": "model", "owned_by": "x"} for i in junk + keep]}
    ms = C.parse_models(P.get("nvidia"), "nvidia", payload)
    assert [m.id for m in ms] == keep
    assert {m.id: m.context for m in ms}["moonshotai/kimi-k3"] == 1_048_576


def test_together_top_level_list_and_type_filter():
    payload = [
        {"id": "moonshotai/Kimi-K3", "type": "chat", "context_length": 262144},
        {"id": "meta-llama/Llama-3-70b-hf", "type": "language", "context_length": 8192},
        {"id": "Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8", "type": "chat"},
        {"id": "togethercomputer/m2-bert-80M-8k-retrieval", "type": "embedding"},
        {"id": "black-forest-labs/FLUX.1-schnell", "type": "image"},
        {"id": "Salesforce/Llama-Rank-V1", "type": "rerank"},
    ]
    ms = C.parse_models(P.get("together"), "together", payload)
    assert [m.id for m in ms] == ["moonshotai/Kimi-K3", "Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8"]
    assert ms[0].context == 262144 and ms[1].context == 262_144


def test_openrouter_batch_variants_dropped_and_no_tools_tagged():
    payload = {"data": [
        {"id": "anthropic/claude-sonnet-5.5", "context_length": 1000000, "supported_parameters": ["tools", "reasoning"],
         "top_provider": {"context_length": 1000000, "max_completion_tokens": 128000}},
        {"id": "anthropic/claude-sonnet-5.5:batch", "supported_parameters": ["tools"]},
        {"id": "openrouter/fusion", "supported_parameters": ["temperature"]},
        {"id": "google/gemini-3.1-flash-image", "architecture": {"output_modalities": ["image"]},
         "supported_parameters": ["temperature"]},
    ]}
    ms = {m.id: m for m in C.parse_models(P.get("openrouter"), "openrouter", payload)}
    assert set(ms) == {"anthropic/claude-sonnet-5.5", "openrouter/fusion"}
    assert ms["anthropic/claude-sonnet-5.5"].max_output == 128000 and "thinks" in ms["anthropic/claude-sonnet-5.5"].tags
    assert "no-tools" in ms["openrouter/fusion"].tags


def test_huggingface_entries_without_tool_support_dropped():
    payload = {"data": [
        {"id": "moonshotai/Kimi-K3", "providers": [{"provider": "a", "context_length": 1048576, "supports_tools": True},
                                                   {"provider": "b", "is_free": False}]},
        {"id": "deepseek-ai/DeepSeek-R1", "providers": [{"provider": "a", "supports_tools": False, "context_length": 64000}]},
        {"id": "Qwen/Qwen3.8-27B", "providers": [{"provider": "a", "supports_tools": True, "is_free": True}]},
    ]}
    ms = {m.id: m for m in C.parse_models(P.get("huggingface"), "huggingface", payload)}
    assert set(ms) == {"moonshotai/Kimi-K3", "Qwen/Qwen3.8-27B"}
    assert ms["moonshotai/Kimi-K3"].context == 1048576 and ms["Qwen/Qwen3.8-27B"].free


def test_gateway_catalog_shapes():
    vercel = {"object": "list", "data": [
        {"id": "moonshotai/kimi-k3", "type": "language", "context_window": 1000000, "max_tokens": 131072,
         "supported_parameters": ["tools", "reasoning"]},
        {"id": "openai/text-embedding-3-small", "type": "embedding"},
        {"id": "google/veo-3.1-generate-001", "type": "video"},
        {"id": "morph/morph-v3-fast", "type": "language", "supported_parameters": ["max_tokens"]},
    ]}
    ms = {m.id: m for m in C.parse_models(P.get("vercel"), "vercel", vercel)}
    assert set(ms) == {"moonshotai/kimi-k3", "morph/morph-v3-fast"} and "no-tools" in ms["morph/morph-v3-fast"].tags
    assert ms["moonshotai/kimi-k3"].context == 1000000 and ms["moonshotai/kimi-k3"].max_output == 131072
    deepinfra = {"data": [
        {"id": "moonshotai/Kimi-K3", "metadata": {"context_length": 1048576, "tags": ["chat", "reasoning"]}},
        {"id": "Qwen/Qwen3-TTS", "metadata": {"tags": ["tts"]}},
        {"id": "BAAI/bge-m3-multi-8k", "metadata": {"tags": ["embed"]}},
    ]}
    assert [m.id for m in C.parse_models(P.get("deepinfra"), "deepinfra", deepinfra)] == ["moonshotai/Kimi-K3"]
    requesty = {"data": [
        {"id": "moonshot/kimi-k3", "api": "chat", "context_window": 1048576, "max_output_tokens": 131072,
         "supports_tool_calling": True, "supports_reasoning": True},
        {"id": "x/no-tools", "api": "chat", "supports_tool_calling": False},
        {"id": "x/embedder", "api": "embeddings"},
    ]}
    ms = {m.id: m for m in C.parse_models(P.get("requesty"), "requesty", requesty)}
    assert set(ms) == {"moonshot/kimi-k3", "x/no-tools"} and "no-tools" in ms["x/no-tools"].tags
    assert "thinks" in ms["moonshot/kimi-k3"].tags
    novita = {"data": [
        {"id": "zai-org/glm-5.3", "model_type": "chat", "context_size": 1048576, "max_output_tokens": 131072,
         "features": ["function-calling", "reasoning"]},
        {"id": "x/plain", "model_type": "chat", "features": ["serverless"]},
    ]}
    ms = {m.id: m for m in C.parse_models(P.get("novita"), "novita", novita)}
    assert ms["zai-org/glm-5.3"].context == 1048576 and "no-tools" in ms["x/plain"].tags
    venice = {"data": [{"id": "kimi-k3", "type": "text", "model_spec": {
        "availableContextTokens": 1000000, "maxCompletionTokens": 131072,
        "capabilities": {"supportsFunctionCalling": True, "supportsReasoning": True}}}]}
    ms = C.parse_models(P.get("venice"), "venice", venice)
    assert ms[0].context == 1000000 and ms[0].max_output == 131072 and "thinks" in ms[0].tags
    chutes = {"data": [{"id": "moonshotai/Kimi-K3-TEE", "context_length": 1048576, "max_output_length": 65535,
                        "supported_features": ["json_mode", "tools"], "output_modalities": ["text"]}]}
    ms = C.parse_models(P.get("chutes"), "chutes", chutes)
    assert ms[0].context == 1048576 and ms[0].max_output == 65535 and "no-tools" not in ms[0].tags
    cerebras = {"data": [{"id": "gpt-oss-120b", "capabilities": {"tools": True, "reasoning": True},
                          "limits": {"max_context_length": 131072, "max_completion_tokens": 40960}}]}
    ms = C.parse_models(P.get("cerebras"), "cerebras", cerebras)
    assert ms[0].context == 131072 and ms[0].max_output == 40960 and "free" in ms[0].tags


def test_known_context_for_current_families():
    cases = {
        "moonshotai/kimi-k3": 1_048_576, "kimi-k2.7-code": 262_144, "z-ai/glm-5.3": 1_048_576, "glm-5.1": 204_800,
        "deepseek-v4-pro": 1_048_576, "deepseek-flash": 1_048_576, "gpt-6.1-sol": 1_050_000, "openai/gpt-5.5": 1_050_000,
        "gpt-5.4-mini": 400_000, "gemini-3.8-flash": 1_048_576, "grok-4.7": 500_000, "grok-4.3": 1_000_000,
        "x-ai/grok-4.20": 2_000_000, "grok-build-0.1": 256_000, "MiniMax-M3": 1_048_576, "MiniMax-M2.7": 204_800,
        "anthropic/claude-opus-5.5": 1_000_000, "claude-sonnet-4-6": 1_000_000, "claude-haiku-4-5": 200_000,
        "gpt-oss-120b": 131_072,
    }
    for mid, ctx in cases.items():
        assert C.guess_context(mid) == ctx, mid


def test_seeds_for_offline_providers():
    assert [m.id for m in C.seed_models(P.get("deepseek"), "deepseek")] == ["deepseek-v4-pro", "deepseek-flash"]
    assert C.seed_models(P.get("minimax"), "minimax")[0].id == "MiniMax-M3"
    assert [m.id for m in C.seed_models(P.get("bedrock"), "bedrock")] == ["sonnet", "opus", "haiku"]
    assert C.seed_models(P.get("together"), "together") == []


def test_public_catalog_never_gets_a_key(monkeypatch):
    seen = []

    def fake_get(url, headers, timeout):
        seen.append((url, dict(headers)))
        if "public" in url:
            return {"data": [{"id": "gpt-oss-120b", "limits": {"max_context_length": 131072,
                                                                "max_completion_tokens": 40960}}]}
        return {"data": [{"id": "gpt-oss-120b"}]}
    monkeypatch.setenv("PULSAR_OFFLINE", "0")
    monkeypatch.setattr(C, "_get_json", fake_get)
    cb = P.get("cerebras")
    ms = C.fetch_live(cb, "cerebras", "csk-secret")
    assert ms[0].max_output == 40960                     # enriched from the public listing
    assert seen[0] == ("https://api.cerebras.ai/v1/models", {"Authorization": "Bearer csk-secret"})
    assert seen[1] == ("https://api.cerebras.ai/public/v1/models", {})
    seen.clear()
    C.fetch_live(cb, "cerebras", "")                     # no key yet: public list only
    assert seen == [("https://api.cerebras.ai/public/v1/models", {})]


def test_catalog_auth_styles(monkeypatch):
    seen = []
    monkeypatch.setenv("PULSAR_OFFLINE", "0")
    monkeypatch.setattr(C, "_get_json", lambda url, headers, timeout: seen.append((url, headers)) or {"data": []})
    azure = P.detect("https://res.openai.azure.com k").best
    C.fetch_live(azure, "azure", "azkey")
    assert seen[-1] == ("https://res.openai.azure.com/openai/v1/models", {"api-key": "azkey"})
    C.fetch_live(P.get("anthropic"), "anthropic", "sk-ant-api03-x")
    assert seen[-1][1]["x-api-key"] == "sk-ant-api03-x"
    n = len(seen)
    # setup tokens and cloud routes never hit a models endpoint
    C.fetch_live(P.get("anthropic"), "anthropic", "sk-ant-oat01-x")
    C.fetch_live(P.get("claude"), "claude", "sk-ant-oat01-x")
    C.fetch_live(P.get("bedrock"), "bedrock", "ABSKx")
    assert len(seen) == n


def test_native_custom_endpoint_lists_its_models_or_stays_quiet(monkeypatch):
    seen = []

    def listing(url, headers, timeout):
        seen.append((url, dict(headers)))
        return {"data": [{"id": "claude-sonnet-5", "type": "model", "display_name": "Sonnet"}], "has_more": False}
    monkeypatch.setenv("PULSAR_OFFLINE", "0")
    monkeypatch.setattr(C, "_get_json", listing)
    prov = P.detect("https://gw.example.com/anthropic k").best
    assert prov.route == "native" and prov.catalog == "anthropic"
    assert [m.id for m in C.fetch_live(prov, "custom-gw", "zz-gw-key-0123456789")] == ["claude-sonnet-5"]
    url, headers = seen[-1]
    assert url == "https://gw.example.com/anthropic/v1/models?limit=1000"
    assert headers["anthropic-version"] == "2023-06-01" and headers["Authorization"] == "Bearer zz-gw-key-0123456789"

    def missing(url, headers, timeout):
        raise C.CatalogError("not found", 404)
    monkeypatch.setattr(C, "_get_json", missing)               # many such endpoints list no models
    assert C.fetch_live(prov, "custom-gw", "zz-gw-key-0123456789") == []
    assert P.detect("https://gw.example.com/v1 k").best.catalog == "openai"     # the OpenAI route is unchanged


def test_invalid_key_400_reads_as_auth_failure(monkeypatch):
    import io
    import urllib.error

    def raise_400(req, timeout):
        body = b'[{"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.", "status": "INVALID_ARGUMENT"}}]'
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(body))
    monkeypatch.setattr(C.OPENER, "open", raise_400)
    try:
        C._get_json("https://generativelanguage.googleapis.com/v1beta/openai/models", {}, 1)
    except C.CatalogError as exc:
        assert exc.status == 401 and "API key not valid" in str(exc)
    else:
        raise AssertionError("expected CatalogError")


def test_offline_nim_seed_lists_only_live_models():
    ids = [m.id for m in C.seed_models(P.get("nvidia"), "nvidia")]
    assert ids[0] == "moonshotai/kimi-k3" and "z-ai/glm-5.3" in ids
    assert "qwen/qwen3-coder-480b-a35b-instruct" not in ids            # retired from NIM
    assert "qwen/qwen3-coder-480b-a35b-instruct" in C.NIM_SEED         # still known to v1 alias migration


# ---------------------------------------------------------------------------
# security regressions (redirects, plain http, escape sequences, redaction)
# ---------------------------------------------------------------------------

def _servers(redirect_code=307):
    """A provider that answers every request with a redirect, and the host it points at (which logs headers)."""
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    seen = []

    class Steal(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append({k.lower(): v for k, v in self.headers.items()})
            body = _json.dumps({"data": [{"id": "stolen-model"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    steal = ThreadingHTTPServer(("127.0.0.1", 0), Steal)

    class Redir(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(redirect_code)
            self.send_header("Location", "http://127.0.0.1:%d/steal?token=sk-or-v1-abcdefghijklmnop" % steal.server_port)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    redir = ThreadingHTTPServer(("127.0.0.1", 0), Redir)
    for srv in (steal, redir):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    return redir, steal, seen


def test_redirects_never_carry_a_key_anywhere(monkeypatch):
    from dataclasses import replace
    monkeypatch.setenv("PULSAR_OFFLINE", "0")
    for code in (301, 302, 303, 307, 308):
        redir, steal, seen = _servers(code)
        try:
            base = "http://127.0.0.1:%d/v1" % redir.server_port
            for prov, key in ((replace(P.get("openai"), base_url=base), "sk-proj-SECRETSECRET1111"),
                              (replace(P.get("anthropic"), base_url=base), "sk-ant-api03-SECRETSECRET2222"),
                              (P.custom_provider(base), "sk-custom-SECRET-3333")):
                try:
                    C.fetch_live(prov, "x", key, timeout=5)
                except C.CatalogError as exc:
                    assert exc.status == code
                    assert "not followed" in str(exc) and "key stays put" in str(exc)
                    assert "sk-or-v1-abcdefghijklmnop" not in str(exc) and "SECRET" not in str(exc)
                else:
                    raise AssertionError("a %d must not be followed" % code)
            assert seen == []          # the second host never heard from us, let alone saw a key
        finally:
            redir.shutdown()
            steal.shutdown()
            redir.server_close()
            steal.server_close()


def test_openers_have_no_following_redirect_handler():
    import urllib.request
    for opener in (C.OPENER, C.LOCAL_OPENER, C.opener_for("https://api.openai.com/v1"), C.opener_for("http://127.0.0.1:1")):
        handlers = [h for h in opener.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
        assert handlers and all(isinstance(h, C._NoRedirect) for h in handlers)
    assert C.opener_for("http://192.168.1.20:11434/v1") is C.LOCAL_OPENER
    assert C.opener_for("https://api.groq.com/openai/v1") is C.OPENER


def test_key_never_sent_over_plain_http_to_public_hosts(monkeypatch):
    calls = []
    monkeypatch.setattr(C.OPENER, "open", lambda req, timeout: calls.append(req.full_url))
    monkeypatch.setattr(C.LOCAL_OPENER, "open", lambda req, timeout: calls.append(req.full_url))
    monkeypatch.delenv("PULSAR_ALLOW_HTTP", raising=False)
    try:
        C._get_json("http://203.0.113.9:8080/v1/models", {"Authorization": "Bearer sk-live-SECRET-1234567890"}, 1)
    except C.CatalogError as exc:
        assert "plain http" in str(exc) and "SECRET" not in str(exc)
    else:
        raise AssertionError("expected a refusal")
    assert calls == []
    # keyless public catalogs, LAN hosts and the explicit opt-in still go out
    for url, headers in (("http://203.0.113.9/v1/models", {}),
                         ("http://192.168.1.20:8000/v1/models", {"Authorization": "Bearer k-123456789"}),
                         ("http://ollama.local:11434/v1/models", {"Authorization": "Bearer k-123456789"})):
        try:
            C._get_json(url, headers, 1)
        except (C.CatalogError, TypeError, AttributeError):
            pass
    assert len(calls) == 3
    monkeypatch.setenv("PULSAR_ALLOW_HTTP", "1")
    try:
        C._get_json("http://203.0.113.9:8080/v1/models", {"Authorization": "Bearer sk-live-SECRET-1234567890"}, 1)
    except (C.CatalogError, TypeError, AttributeError):
        pass
    assert len(calls) == 4


def test_catalog_error_text_is_redacted(monkeypatch):
    import io
    import urllib.error
    key = "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWX"

    def echo(req, timeout):
        body = ('{"error": {"message": "Invalid API key provided: %s \\u001b]52;c;Y3VybA==\\u0007"}}' % key).encode()
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(body))
    monkeypatch.setattr(C.OPENER, "open", echo)
    try:
        C._get_json("https://api.openai.com/v1/models", {"Authorization": "Bearer " + key}, 1)
    except C.CatalogError as exc:
        text = str(exc)
        assert exc.status == 401 and key not in text and "Invalid API key" in text
        assert "\x1b" not in text and "\x07" not in text
    else:
        raise AssertionError("expected CatalogError")


ESCAPES = ["good/coder-1",
           "evil-osc52\x1b]52;c;Y3VybCBldmlsLnNoIHwgc2gK\x07",
           "evil-title\x1b]0;sudo password expired\x07",
           "evil-clear\x1b[2J\x1b[Hfake-prompt$ ",
           "evil\u202eexe.txt", "spaced id", "x" * 201, "\x9b31m"]


def test_model_ids_with_control_sequences_are_dropped(pulsar_home):
    ms = C.parse_models(P.get("openai"), "openai",
                        {"data": [{"id": i, "display_name": "Nice\x1b]0;pwn\x07 \u202eName"} for i in ESCAPES]})
    assert [m.id for m in ms] == ["good/coder-1"]
    assert ms[0].name == "Nice]0;pwn Name"
    # a cache written before the fix (or edited by hand) is filtered the same way
    import json
    import time
    C.cache_file("openai").parent.mkdir(parents=True, exist_ok=True)
    C.cache_file("openai").write_text(json.dumps({"schema": C.CACHE_SCHEMA, "at": int(time.time()), "models": [
        {"id": i, "profile": "openai", "name": "n\x1b[2J", "tags": ["free\x07"]} for i in ESCAPES]}))
    cached = C.read_cache("openai")
    assert [m.id for m in cached] == ["good/coder-1"] and cached[0].name == "n[2J" and cached[0].tags == ("free",)


def test_ansi_endpoint_end_to_end(monkeypatch, pulsar_home, capsys):
    """A models endpoint that lists terminal escape sequences: nothing it lists reaches the terminal raw."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            b = json.dumps({"data": [{"id": i} for i in ESCAPES]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        from pulsar import cli
        from pulsar import config as K
        monkeypatch.setenv("PULSAR_OFFLINE", "0")
        st = K.State()
        st.upsert(K.Profile(id="custom-local", provider="custom", base_url="http://127.0.0.1:%d/v1" % srv.server_port))
        K.save(st)
        assert cli.main(["models", "--refresh"]) == 0
        out = capsys.readouterr()
        assert "good/coder-1" in out.out
        assert "\x1b]" not in out.out + out.err and "\x07" not in out.out + out.err and "\x1b[2J" not in out.out
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------------------
# One TLS decision for Core and the CLI (verify/O): catalogs and key probes use netcfg.ssl_context()
# ---------------------------------------------------------------------------

def test_cli_openers_use_netcfgs_tls_context(monkeypatch):
    import http.client
    import ssl

    from pulsar import netcfg
    ctx = ssl.create_default_context()
    monkeypatch.setattr(netcfg, "ssl_context", lambda: ctx)
    seen = []

    class FakeConn:
        def __init__(self, host, timeout=None, context=None, **kw):
            seen.append((host, context))
            raise OSError("no network in tests")
    monkeypatch.setattr(http.client, "HTTPSConnection", FakeConn)
    for url in ("https://api.groq.com/openai/v1/models", "https://192.168.1.20:8443/v1/models"):
        try:
            C._get_json(url, {"Authorization": "Bearer gsk_0123456789abcdef0123"}, 1)
        except C.CatalogError as exc:
            assert "unreachable" in str(exc) and "0123456789abcdef" not in str(exc)
    assert [c for _, c in seen] == [ctx, ctx]


def test_private_ca_bundle_verifies_catalogs_end_to_end(tmp_path, monkeypatch):
    import http.server
    import shutil
    import ssl
    import subprocess
    import threading

    import pytest

    from pulsar import netcfg
    if shutil.which("openssl") is None:
        pytest.skip("needs openssl")

    def ossl(*args):
        subprocess.run(["openssl"] + list(args), cwd=str(tmp_path), check=True, capture_output=True, timeout=60)
    ossl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-days", "2",
         "-subj", "/CN=Corp Inspecting CA")
    ossl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "srv.key", "-out", "srv.csr", "-subj", "/CN=127.0.0.1")
    (tmp_path / "ext.cnf").write_text("subjectAltName=IP:127.0.0.1\nbasicConstraints=CA:FALSE\n")
    ossl("x509", "-req", "-in", "srv.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-out", "srv.pem",
         "-days", "2", "-extfile", "ext.cnf")

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"data": [{"id": "corp/model-1"}]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(str(tmp_path / "srv.pem"), str(tmp_path / "srv.key"))
    srv.socket = sctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "https://127.0.0.1:%d/v1/models" % srv.server_address[1]
    try:
        for var in ("PULSAR_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS"):
            monkeypatch.delenv(var, raising=False)
        netcfg.reset()
        with pytest.raises(C.CatalogError):
            C._get_json(url, {}, 5)                       # an unknown CA is still refused
        monkeypatch.setenv("PULSAR_CA_BUNDLE", str(tmp_path / "ca.pem"))
        netcfg.reset()
        assert C._get_json(url, {"Authorization": "Bearer sk-x-0123456789"}, 5)["data"][0]["id"] == "corp/model-1"
    finally:
        srv.shutdown()
        srv.server_close()
        netcfg.reset()


# ---------------------------------------------------------------------------
# A provider that needs your own resource URL and has none saved is never contacted
# ---------------------------------------------------------------------------

def _no_network(monkeypatch):
    """Any catalog request fails the test; PULSAR_OFFLINE is lifted so only the placeholder check can stop it."""
    import pytest
    monkeypatch.delenv("PULSAR_OFFLINE", raising=False)
    calls = []

    def boom(url, headers, timeout):
        calls.append(url)
        pytest.fail("a request was made to %s" % url)
    monkeypatch.setattr(C, "_get_json", boom)
    monkeypatch.setattr(C.OPENER, "open", lambda *a, **k: pytest.fail("OPENER used"))
    return calls


def _azure_placeholder():
    from pulsar import config as K
    return K.Profile(id="azure", provider="azure", model="gpt-4o", base_url="").resolve()


def test_placeholder_profile_is_never_fetched(monkeypatch, pulsar_home):
    _no_network(monkeypatch)
    prov = _azure_placeholder()
    assert "YOUR-RESOURCE" in prov.base_url
    problem = C.placeholder_problem(prov)
    assert "no resource URL" in problem and "https://<your-resource>.openai.azure.com/openai/v1" in problem
    for refresh in (False, True):
        res = C.load_catalog(prov, "azure", "fake-azure-key-not-real-0123456789", refresh=refresh)
        assert res.source == "seed" and "no resource URL" in res.error
    import pytest
    with pytest.raises(C.CatalogError):
        C.fetch_live(prov, "azure", "fake-azure-key-not-real-0123456789")
    # the same provider with its own resource URL is fine
    from dataclasses import replace
    assert C.placeholder_problem(replace(prov, base_url="https://fake-res.openai.azure.com/openai/v1")) == ""
    assert C.placeholder_problem(P.get("groq")) == ""
    assert C.placeholder_problem(P.get("foundry"))                    # the registry default is a placeholder too


def test_locate_skips_placeholder_profiles_and_reports_what_it_could_not_check(monkeypatch, pulsar_home):
    _no_network(monkeypatch)
    unchecked = []
    pid, known = C.locate("gpt-4o", [("azure", _azure_placeholder(), "fake-azure-key-not-real-0123456789"),
                                     ("groq", P.get("groq"), "")], unchecked=unchecked)
    assert pid == "" and not known
    assert unchecked == [("azure", "no resource URL"), ("groq", "no key")]


def test_locate_names_unreachable_catalogs(monkeypatch, pulsar_home):
    monkeypatch.setattr(C, "load_catalog", lambda *a, **k: C.CatalogResult([], "seed", "unreachable: refused"))
    unchecked = []
    pid, known = C.locate("grok-build-1", [("groq", P.get("groq"), "gsk_fake"), ("xai", P.get("xai"), "xai-fake")],
                          unchecked=unchecked)
    assert pid == "" and known
    assert unchecked == [("groq", "unreachable"), ("xai", "unreachable")]
    msg = C.unlisted_message("grok-build-1", ["groq", "xai"], unchecked)
    assert "could not check" in msg and "groq (unreachable)" in msg and "xai (unreachable)" in msg
    assert "<provider>:grok-build-1" in msg and "groq:grok-build-1" not in msg
    assert C.locate("x", [("groq", P.get("groq"), "gsk_fake")]) == ("", True)     # old callers: same shape
    msg = C.unlisted_message("grok-build-1", ["groq", "xai"], [("xai", "unreachable")])
    assert msg.startswith("no connected provider that could be checked lists") and "groq:" not in msg
    assert C.unlisted_message("m", ["groq"], []).startswith("no connected provider lists 'm'.")
