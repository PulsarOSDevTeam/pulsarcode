# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
import re
from dataclasses import replace

import pytest

from pulsar import providers as P


@pytest.mark.parametrize("key,pid", [
    ("nvapi-AbC123def456ghi789", "nvidia"),
    ("sk-ant-api03-abcdefghijklmnop", "anthropic"),
    ("sk-or-v1-0123456789abcdef0123", "openrouter"),
    ("sk-proj-abcdefghijklmnopqrstuv", "openai"),
    ("sk-svcacct-abcdefghijklmnop", "openai"),
    ("AIzaSyA-abcdefghijklmnopqrstuvwxyz", "gemini"),
    ("AQ.Ab8RN6abcdefghijklmnopqrstuvwxyz", "gemini"),
    ("gsk_abcdefghijklmnopqrstuvwxyz", "groq"),
    ("xai-abcdefghijklmnopqrstuvwxyz", "xai"),
    ("csk-abcdefghijklmnop", "cerebras"),
    ("csk_abcdefghijklmnop", "cerebras"),
    ("fw_abcdefghijklmnop", "fireworks"),
    ("hf_abcdefghijklmnop", "huggingface"),
    ("tgp_v1_abcdefghijklmnop", "together"),
    ("tgp_abcdefghijklmnop", "together"),
    ("sk-cp-abcdefghijklmnop", "minimax"),
    ("sk-api-abcdefghijklmnop", "minimax"),
    ("vck_abcdefghijklmnop", "vercel"),
    ("vapi_abcdefghijklmnop", "venice"),
    ("cpk_abcdefghijklmnop", "chutes"),
    ("sk-ant-oat01-abcdefghijklmnop", "claude"),
    ("ABSK" + "QmVkcm9jaw" * 12 + "==", "bedrock"),
    ("bedrock-api-key-YmVkcm9jay5hbWF6b25hd3MuY29tabc", "bedrock"),
])
def test_prefix_detection_is_certain(key, pid):
    det = P.detect(key)
    assert det.kind == "key"
    assert det.certain
    assert det.best.id == pid
    assert det.key == key


def test_specific_prefixes_win_over_generic_ones():
    assert P.detect("sk-ant-xyz1234567890").best.id == "anthropic"
    assert P.detect("sk-ant-oat01-xyz1234567890").best.id == "claude"
    assert P.detect("sk-or-v1-xyz1234567890").best.id == "openrouter"
    assert P.detect("sk-cp-xyz1234567890").best.id == "minimax"  # not a bare sk- key


def test_anthropic_setup_token_routes_to_oauth_env():
    prov = P.detect("sk-ant-oat01-" + "a" * 80).best
    assert prov.route == "cloud" and prov.cloud == "anthropic"
    assert prov.key_env == "CLAUDE_CODE_OAUTH_TOKEN"
    assert P.cloud_env(prov, "sk-ant-oat01-tok") == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-tok"}


@pytest.mark.parametrize("key,needle", [
    ("sk-ant-admin01-" + "a" * 90, "Admin API key"),
    ("sk-admin-" + "a" * 40, "OpenAI Admin key"),
    ("pplx-" + "a" * 48, "Perplexity"),
    ("ghp_" + "a" * 36, "GitHub"),
    ("github_pat_" + "a" * 82, "GitHub"),
    ("sk_live_" + "a" * 24, "Stripe"),
    ("AKIAIOSFODNN7EXAMPLE", "AWS access key id"),
])
def test_unusable_credentials_are_rejected_and_dropped(key, needle):
    det = P.detect(key)
    assert det.kind == "rejected" and needle in det.note
    assert det.key == "" and det.candidates == [] and not det.certain
    # also when pasted as an env assignment or next to a URL
    assert P.detect("ANTHROPIC_API_KEY=" + key).kind == "rejected"
    assert P.detect("https://api.openai.com/v1 " + key).kind == "rejected"


def test_admin_keys_are_never_stored_but_still_masked():
    from pulsar.redact import redact
    admin = "sk-admin-" + "Q" * 40
    assert P.env_keys({"OPENAI_API_KEY": admin}) == []
    assert P.detect("OPENAI_API_KEY=" + admin).kind == "rejected"
    assert "sk-admin-" not in P.get("openai").prefixes
    assert P.mask(admin).startswith("sk-admin-") and "Q" * 8 not in P.mask(admin)
    assert "Q" * 20 not in redact("echoed " + admin)


def test_refusals_state_no_third_party_dates_or_events():
    # A refusal speaks for Pulsar Code only: it never asserts what another company did, or when.
    for prefix, note in P._REJECT:
        assert not re.search(r"\b(20\d\d|retired|ended|shut down|discontinued)\b", note), prefix
        assert "never sends it anywhere" in note or "Create" in note, prefix


def test_unadvertised_subscription_route_wears_a_neutral_pulsar_color():
    from pulsar import fx
    assert fx.hex_rgb(P.get("claude").color) == fx.ASH


def test_ambiguous_sk_key_offers_ranked_candidates():
    det = P.detect("sk-" + "0123456789abcdef" * 2)
    assert not det.certain
    assert [p.id for p in det.candidates] == ["deepseek", "dashscope"]
    # 48-char sk- keys without OpenAI's base64 marker are most likely Moonshot's
    det = P.detect("sk-" + "A" * 48)
    assert not det.certain and det.candidates[0].id == "moonshot"
    # every legacy OpenAI key carries "T3BlbkFJ" (base64 for "OpenAI")
    det = P.detect("sk-" + "a" * 20 + "T3BlbkFJ" + "b" * 20)
    assert det.certain and det.best.id == "openai"


def test_low_confidence_shape_always_asks():
    det = P.detect("sk_" + "a" * 30)
    assert det.kind == "key" and not det.certain
    assert det.candidates[0].id == "novita" and len(det.candidates) > 5


def test_zai_shape_and_no_jwt_guessing():
    assert P.detect("0123456789abcdef0123456789abcdef.AbCdEfGhIjKlMnOp").best.id == "zai"
    # A JWT is no longer filed under MiniMax: it could be anyone's token.
    det = P.detect("eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl")
    assert det.kind == "unknown" and not det.certain


def test_env_assignment_paste_uses_variable_name():
    det = P.detect("export MISTRAL_API_KEY=abcdefghijklmnopqrstuvwxyz123456")
    assert det.certain and det.best.id == "mistral"
    assert det.key == "abcdefghijklmnopqrstuvwxyz123456"
    det = P.detect('DEEPSEEK_API_KEY="sk-0123"')
    assert det.best.id == "deepseek" and det.key == "sk-0123"
    det = P.detect("$env:GROQ_API_KEY='gsk_abc123'")
    assert det.best.id == "groq" and det.key == "gsk_abc123"


def test_env_assignment_value_prefix_beats_variable_name():
    # OpenRouter keys are often exported as OPENAI_API_KEY: never send them to OpenAI.
    det = P.detect("OPENAI_API_KEY=sk-or-v1-abcdef0123456789")
    assert det.certain and det.best.id == "openrouter" and det.note == "from OPENAI_API_KEY"
    det = P.detect("ANTHROPIC_API_KEY=sk-ant-oat01-abcdef")
    assert det.best.id == "claude"


def test_base64_padding_is_not_an_env_assignment():
    key = "ABSK" + "Zm9vYmFy" * 14 + "=="
    det = P.detect(key)
    assert det.best.id == "bedrock" and det.key == key


def test_quotes_and_bearer_are_stripped():
    assert P.detect('  "nvapi-abc123"  ').key == "nvapi-abc123"
    assert P.detect("Bearer gsk_abc123").best.id == "groq"


def test_local_words_and_urls():
    assert P.detect("ollama").best.id == "ollama"
    assert P.detect("LOCAL").best.id == "ollama"
    assert P.detect("llama.cpp").best.id == "llamacpp"
    assert P.detect("vllm").best.id == "vllm"
    det = P.detect("http://localhost:11434")
    assert det.kind == "local" and det.best.id == "ollama" and det.base_url.endswith("/v1")
    det = P.detect("http://127.0.0.1:1234/v1")
    assert det.best.id == "lmstudio"
    det = P.detect("http://127.0.0.1:8080")
    assert det.kind == "local" and det.best.id == "llamacpp" and det.base_url == "http://127.0.0.1:8080/v1"
    assert P.detect("http://localhost:8000/v1").best.id == "vllm"
    assert P.detect("http://127.0.0.1:1337").best.id == "jan"
    det = P.detect("https://llm.example.com/v1  sk-secret")
    assert det.kind == "url" and det.best.id == "custom" and det.best.route == "adapter"
    assert det.key == "sk-secret" and det.base_url == "https://llm.example.com/v1"
    det = P.detect("https://gw.example.com/anthropic tok")
    assert det.best.route == "native"
    # remote 8080/8000 are just custom endpoints
    assert P.detect("https://example.com:8080/v1 k").best.id == "custom"


def test_registry_urls_map_to_providers_and_regions():
    det = P.detect("https://openrouter.ai/api/v1 sk-or-v1-abc")
    assert det.certain and det.best.id == "openrouter" and det.base_url == ""
    det = P.detect("https://api.moonshot.cn/v1 sk-abc")
    prov = det.best
    assert prov.id == "moonshot" and prov.region == "cn"
    assert prov.native_url == "https://api.moonshot.cn/anthropic" and det.base_url == "https://api.moonshot.cn/v1"
    det = P.detect("https://open.bigmodel.cn/api/anthropic 0123.abc")
    assert det.best.id == "zai" and det.best.native_url == "https://open.bigmodel.cn/api/anthropic"
    det = P.detect("https://api.z.ai/api/coding/paas/v4 k")
    assert det.best.region == "coding"
    det = P.detect("https://api.minimaxi.com/anthropic k")
    assert det.best.id == "minimax" and det.best.native_url == "https://api.minimaxi.com/anthropic"


def test_url_and_key_from_different_vendors_asks_first():
    det = P.detect("https://api.openai.com/v1 sk-or-v1-abcdef")
    assert not det.certain
    assert [p.id for p in det.candidates] == ["openrouter", "openai"]
    assert "OpenRouter" in det.note


def test_azure_and_foundry_urls():
    det = P.detect("https://my-res.openai.azure.com/openai/deployments/x/chat/completions?api-version=2024 0123abcd")
    prov = det.best
    assert det.certain and prov.id == "azure" and prov.base_url == "https://my-res.openai.azure.com/openai/v1"
    assert det.base_url == prov.base_url
    assert P.auth_headers(prov, "k") == {"api-key": "k"}
    assert prov.wire_for("gpt-6.1-sol") == "responses" and prov.wire_for("my-deployment") == "chat"
    det = P.detect("https://my-res.services.ai.azure.com/anthropic k")
    prov = det.best
    assert prov.id == "foundry" and prov.route == "native" and prov.auth == "x-api-key"
    assert prov.native_base() == "https://my-res.services.ai.azure.com/anthropic"
    # a persisted base_url override restores the native endpoint
    again = replace(P.get("foundry"), base_url="https://other.services.ai.azure.com/anthropic")
    assert again.native_url == "https://other.services.ai.azure.com/anthropic"
    assert P.get("azure") not in P.keyed_providers()


def test_cloud_keywords():
    det = P.detect("vertex my-project-42 us-east5")
    assert det.kind == "cloud" and det.certain and det.best.id == "vertex"
    assert det.options == {"project": "my-project-42", "region": "us-east5"}
    assert P.cloud_options(det.best, det.base_url) == det.options
    env = P.cloud_env(det.best, "", P.cloud_options(det.best, det.base_url), environ={})
    assert env == {"CLAUDE_CODE_USE_VERTEX": "1", "ANTHROPIC_VERTEX_PROJECT_ID": "my-project-42",
                   "CLOUD_ML_REGION": "us-east5"}
    det = P.detect("vertex my-project-42")
    assert det.options["region"] == "global" and "aiplatform.googleapis.com/v1/projects/my-project-42" in det.base_url
    det = P.detect("vertex")
    assert det.kind == "cloud" and "project" in det.note and det.options == {}
    assert P.cloud_missing(det.best, {}, environ={}) == ["project"]
    assert P.cloud_missing(det.best, {}, environ={"GOOGLE_CLOUD_PROJECT": "p"}) == []
    with pytest.raises(ValueError):
        P.cloud_env(det.best, "", {}, environ={})
    assert P.detect("vertex NOT_A_PROJECT").kind == "unknown"
    assert P.detect("vertex my-project-42 mars-1").kind == "unknown"


def test_bedrock_key_and_region():
    prov = P.detect("ABSK" + "a" * 120).best
    assert prov.route == "cloud" and prov.cloud == "bedrock" and prov.key_env == "AWS_BEARER_TOKEN_BEDROCK"
    assert P.cloud_missing(prov, {}, environ={}) == ["region"]
    assert P.cloud_missing(prov, {}, environ={"AWS_REGION": "eu-west-1"}) == []
    env = P.cloud_env(prov, "ABSKkey", {}, environ={"AWS_DEFAULT_REGION": "us-west-2"})
    assert env == {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_BEARER_TOKEN_BEDROCK": "ABSKkey", "AWS_REGION": "us-west-2"}
    det = P.detect("bedrock eu-central-1")
    assert det.kind == "cloud" and det.options == {"region": "eu-central-1"}
    assert P.cloud_options(det.best, det.base_url) == {"region": "eu-central-1"}
    assert P.cloud_env(det.best, "", det.options, environ={}) == {"CLAUDE_CODE_USE_BEDROCK": "1",
                                                                  "AWS_REGION": "eu-central-1"}
    assert P.cloud_options(prov, "") == {} and P.cloud_options(prov, prov.base_url) == {}
    assert P.detect("bedrock not-a-region").kind == "unknown"


def test_unknown_and_garbage():
    assert P.detect("").kind == "empty"
    assert P.detect("hello world").kind == "unknown"
    det = P.detect("zzzz")
    assert det.kind == "unknown" and det.candidates
    assert all(p.needs_key and not p.needs_url and p.route != "cloud" for p in det.candidates)


def test_mask_never_reveals_secret_body():
    key = "nvapi-SECRETSECRETSECRET1234"
    m = P.mask(key)
    assert m.startswith("nvapi-") and m.endswith("1234") and "SECRET" not in m
    assert "abc" not in P.mask("sk-abcdefghijklmnopqrstuvwxyz")
    m = P.mask("sk-ant-oat01-SECRETSECRETSECRET9876")
    assert m.startswith("sk-ant-oat") and "SECRET" not in m
    m = P.mask("AQ.SECRETSECRETSECRET5555")
    assert m.startswith("AQ.") and "SECRET" not in m
    assert "SECRET" not in P.mask("bedrock-api-key-SECRETSECRETSECRET")


def test_env_keys_one_per_provider():
    env = {"NVIDIA_API_KEY": "nvapi-x", "NVIDIA_NIM_API_KEY": "nvapi-y", "OPENAI_API_KEY": "sk-proj-z"}
    found = P.env_keys(env)
    ids = [p.id for p, _, _ in found]
    assert ids.count("nvidia") == 1 and "openai" in ids
    assert found[0][2] == "nvapi-y"  # first listed env var wins


def test_env_keys_trust_the_value_and_skip_unusable():
    found = {p.id: (var, value) for p, var, value in P.env_keys({
        "OPENAI_API_KEY": "sk-or-v1-abc", "ANTHROPIC_API_KEY": "sk-ant-oat01-abc"})}
    assert found["openrouter"] == ("OPENAI_API_KEY", "sk-or-v1-abc")
    assert found["claude"] == ("ANTHROPIC_API_KEY", "sk-ant-oat01-abc")
    assert "openai" not in found and "anthropic" not in found
    assert P.env_keys({"ANTHROPIC_API_KEY": "sk-ant-admin01-x"}) == []


def test_gemini_env_precedence():
    found = P.env_keys({"GEMINI_API_KEY": "AIza-gemini", "GOOGLE_API_KEY": "AIza-google"})
    assert found == [(P.get("gemini"), "GOOGLE_API_KEY", "AIza-google")]


def test_research_registry_fixes():
    ds = P.get("deepseek")
    assert ds.base_url == "https://api.deepseek.com" and ds.fallback_model == "deepseek-flash"
    assert any(re.search(x, "deepseek-v4-pro") for x in ds.prefer)
    assert not any(re.search(x, "deepseek-chat") for x in ds.prefer)
    assert P.get("gemini").fallback_model == "gemini-3.8-flash"
    assert P.get("xai").fallback_model == "grok-4.7"
    assert P.get("zai").fallback_model == "glm-5.3"
    assert P.get("moonshot").fallback_model == "kimi-k2.6"
    assert P.get("minimax").fallback_model == "MiniMax-M3" and P.get("minimax").catalog == "openai"
    assert re.search(P.get("minimax").prefer[0], "MiniMax-M3")
    assert P.get("nebius").base_url == "https://api.tokenfactory.nebius.com/v1"
    assert P.get("cerebras").models_url == "https://api.cerebras.ai/public/v1/models"
    assert "github" not in P.BY_ID and "perplexity" not in P.BY_ID


def test_openai_wire_per_model():
    oa = P.get("openai")
    for model in ("gpt-5.3-codex", "gpt-5-codex", "gpt-6.1-sol", "gpt-6-luna", "gpt-6", "o3-pro", "gpt-5.5-pro",
                  "codex-mini-latest"):
        assert oa.wire_for(model) == "responses", model
    for model in ("gpt-5.5", "gpt-5.4-mini", "gpt-4o", "o4-mini", "gpt-4.1"):
        assert oa.wire_for(model) == "chat", model
    assert not any(re.search(x, "gpt-5.3-codex") for x in oa.prefer)
    assert re.search(oa.prefer[0], "gpt-6.1-sol")
    # routers translate for us: OpenAI models stay on chat there
    assert P.get("openrouter").wire_for("openai/gpt-6.1-sol") == "chat"


def test_mistral_tool_ids():
    assert P.get("mistral").tool_id_style_for("anything") == "mistral9"
    assert P.get("openrouter").tool_id_style_for("mistralai/devstral-2512") == "mistral9"
    assert P.get("nvidia").tool_id_style_for("mistralai/codestral-22b-instruct-v0.1") == "mistral9"
    assert P.get("together").tool_id_style_for("moonshotai/Kimi-K3") == "default"


def test_native_selection_for_hybrid_providers():
    orr = P.get("openrouter")
    assert orr.route == "adapter"
    assert orr.native_for("anthropic/claude-sonnet-5.5") and orr.native_base() == "https://openrouter.ai/api"
    assert not orr.native_for("moonshotai/kimi-k3")
    assert P.get("vercel").native_for("anthropic/claude-opus-5")
    assert P.get("requesty").native_for("anthropic/claude-sonnet-5-5")
    assert P.get("deepseek").native_for("deepseek-flash")
    assert not P.get("together").native_for("moonshotai/Kimi-K3")    # endpoint known, opt-in only
    assert not P.get("bedrock").native_for("sonnet")


def test_auth_headers():
    assert P.auth_headers(P.get("openai"), "k") == {"Authorization": "Bearer k"}
    assert P.auth_headers(P.get("anthropic"), "k", native=True) == {"x-api-key": "k"}
    assert P.auth_headers(P.get("sambanova"), "k", native=True) == {"x-api-key": "k"}
    assert P.auth_headers(P.get("huggingface"), "k", native=True) == {"Authorization": "Bearer k"}
    assert P.auth_headers(P.get("openai"), "") == {}


def test_regions():
    cn = P.with_region(P.get("moonshot"), "cn")
    assert cn.base_url == "https://api.moonshot.cn/v1" and cn.native_url == "https://api.moonshot.cn/anthropic"
    assert cn.anthropic_native_url == "https://api.moonshot.cn/anthropic" and cn.region == "cn"
    # a profile override (dataclasses.replace) swaps the Anthropic endpoints too
    again = replace(P.get("minimax"), base_url="https://api.minimaxi.com/v1")
    assert again.native_url == "https://api.minimaxi.com/anthropic"
    us = P.with_region(P.get("dashscope"), "us")
    assert us.anthropic_native_url == ""          # not verified there: never guess a host for a key
    with pytest.raises(KeyError):
        P.with_region(P.get("moonshot"), "mars")


def test_registry_is_consistent():
    ids = [p.id for p in P.PROVIDERS]
    assert len(ids) == len(set(ids))
    assert all("-" not in i for i in ids)  # profile ids are split on "-"
    for p in P.PROVIDERS:
        assert p.route in ("adapter", "native", "cloud")
        assert p.base_url.startswith("http")
        if p.route == "native" and p.id != "anthropic":
            assert p.native_url.startswith("https://")
        if p.route == "cloud":
            assert p.cloud in ("bedrock", "vertex", "anthropic") and p.catalog == "none"
        assert p.wire in ("chat", "responses")
        assert p.tool_id_style in ("default", "mistral9")
        assert p.auth_header in ("bearer", "api-key", "x-api-key") and p.auth in ("bearer", "x-api-key")
        for url in (p.anthropic_native_url, p.models_url):
            assert not url or url.startswith("https://") or (p.local and url.startswith("http://127.0.0.1"))
        assert not p.anthropic_native_url.endswith("/v1")
        for rx in p.prefer + ((p.native_models,) if p.native_models else ()) + \
                ((p.responses_models,) if p.responses_models else ()):
            re.compile(rx)
        for name, base, native in p.regions:
            assert base.startswith("https://") and (not native or native.startswith("https://"))
            if p.route == "native":
                assert native, "native-route regions need an Anthropic endpoint"
        assert p.color.startswith("#") and len(p.color) == 7
        for prefix in p.prefixes:
            hits = [q.id for q in P.PROVIDERS if prefix in q.prefixes]
            assert hits == [p.id], prefix


def test_detection_never_needs_network(monkeypatch):
    import socket

    def boom(*a, **k):
        raise AssertionError("detection touched the network")
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    for raw in ("nvapi-x", "https://api.openai.com/v1 sk-proj-x", "vertex proj-123456", "bedrock us-east-1",
                "sk-" + "a" * 48, "http://127.0.0.1:8080"):
        P.detect(raw)


def test_unknown_regional_host_never_borrows_another_regions_endpoint():
    ws = replace(P.get("dashscope"), base_url="https://ws-123.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1")
    assert ws.anthropic_native_url == "" and not ws.native_for("qwen3-coder-plus")
    # A native-route provider behind a proxy or gateway: its native traffic follows the configured host, so
    # the key never bypasses the gateway for the vendor's public host (or another region's).
    proxied = replace(P.get("moonshot"), base_url="https://proxy.example/v1")
    # native traffic follows the configured host: the key never bypasses it for the vendor's public host
    assert proxied.native_url == proxied.anthropic_native_url == "https://proxy.example/anthropic"
    assert P.get("moonshot").native_url == "https://api.moonshot.ai/anthropic"   # the registry itself is untouched


def test_mismatched_url_and_key_carry_no_base_override():
    det = P.detect("https://api.moonshot.cn/v1 sk-or-v1-abcdef")
    assert det.base_url == "" and [p.id for p in det.candidates] == ["openrouter", "moonshot"]
