#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc. / Collapse Technologies Inc.
"""Small Anthropic Messages to NVIDIA NIM adapter for pulsarcode.

Claude Code sends Anthropic Messages API requests. NVIDIA NIM exposes an
OpenAI-compatible chat completions API. This proxy keeps the local hop tiny:
no LiteLLM router, no model groups, no provider fallback layer.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from proxy.nim_api_sonar import (
    normalize_requested_model_alias,
    openai_models_payload,
    resolve_model_alias,
    runtime_catalog,
)


PUBLIC_MODEL = "nim-kimi"
NIM_MODEL = "moonshotai/kimi-k2.6"
DEFAULT_API_BASE = "https://integrate.api.nvidia.com/v1"
NIM_SETUP_URL = "https://build.nvidia.com/moonshotai/kimi-k2.6"
NIM_QUICKSTART_URL = "https://docs.api.nvidia.com/nim/docs/api-quickstart"
ADAPTER_PROTOCOL = "nim-stream-ping-v2"


@dataclass(frozen=True)
class NIMSettings:
    api_key: str
    api_base: str = DEFAULT_API_BASE
    public_model: str = PUBLIC_MODEL
    nim_model: str = NIM_MODEL
    timeout_s: float = 3600.0
    preflight_enabled: bool = True
    preflight_ttl_s: float = 30.0
    preflight_timeout_s: float = 5.0
    stream_header_timeout_s: float = 180.0
    stream_ping_interval_s: float = 5.0

    @property
    def chat_url(self) -> str:
        return self.api_base.rstrip("/") + "/chat/completions"


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _system_to_text(system: Any) -> str:
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(part for part in parts if part)
    return ""


def _tool_result_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif block is not None:
                parts.append(_json_dumps(block))
        return "\n".join(part for part in parts if part)
    if content is None:
        return ""
    return _json_dumps(content)


def _flush_user_text(oai_messages: List[Dict[str, Any]], pending: List[str]) -> None:
    text = "".join(pending).strip()
    pending.clear()
    if text:
        oai_messages.append({"role": "user", "content": text})


def anthropic_messages_to_openai(body: Dict[str, Any], settings: NIMSettings) -> List[Dict[str, Any]]:
    oai_messages: List[Dict[str, Any]] = []
    system = _system_to_text(body.get("system", ""))
    if system:
        oai_messages.append({"role": "system", "content": system})

    for message in body.get("messages", []) or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role", "user")
        content = message.get("content", "")

        if isinstance(content, str):
            oai_messages.append({"role": role, "content": content})
            continue

        if not isinstance(content, list):
            oai_messages.append({"role": role, "content": _tool_result_to_text(content)})
            continue

        if role == "assistant":
            text_parts: List[str] = []
            tool_calls: List[Dict[str, Any]] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    text_parts.append(str(block.get("text", "")))
                elif block_type == "tool_use":
                    tool_calls.append({
                        "id": str(block.get("id") or "toolu_" + uuid.uuid4().hex[:24]),
                        "type": "function",
                        "function": {
                            "name": str(block.get("name", "")),
                            "arguments": _json_dumps(block.get("input", {})),
                        },
                    })
            out: Dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
            if tool_calls:
                out["tool_calls"] = tool_calls
            oai_messages.append(out)
            continue

        pending_text: List[str] = []
        for block in content:
            if not isinstance(block, dict):
                pending_text.append(str(block))
                continue
            block_type = block.get("type")
            if block_type == "text":
                pending_text.append(str(block.get("text", "")))
            elif block_type == "tool_result":
                _flush_user_text(oai_messages, pending_text)
                oai_messages.append({
                    "role": "tool",
                    "tool_call_id": str(block.get("tool_use_id", "")),
                    "content": _tool_result_to_text(block.get("content", "")),
                })
            else:
                pending_text.append(_json_dumps(block))
        _flush_user_text(oai_messages, pending_text)

    if not oai_messages:
        oai_messages.append({"role": "user", "content": ""})
    return oai_messages


def anthropic_tools_to_openai(tools: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not isinstance(tools, list):
        return out
    for tool in tools:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        out.append({
            "type": "function",
            "function": {
                "name": str(tool.get("name")),
                "description": str(tool.get("description", "")),
                "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
            },
        })
    return out


def _tool_choice_to_openai(tool_choice: Any) -> Any:
    if not isinstance(tool_choice, dict):
        return None
    choice_type = tool_choice.get("type")
    if choice_type == "auto":
        return "auto"
    if choice_type == "any":
        return "required"
    if choice_type == "none":
        return "none"
    if choice_type == "tool" and tool_choice.get("name"):
        return {"type": "function", "function": {"name": str(tool_choice["name"])}}
    return None


def build_openai_body(
    body: Dict[str, Any],
    settings: NIMSettings,
    stream: Optional[bool] = None,
) -> Dict[str, Any]:
    max_tokens = int(body.get("max_tokens", 4096))
    cap = os.environ.get("PULSAR_NIM_MAX_TOKENS")
    if cap:
        max_tokens = min(max_tokens, int(cap))
    requested_model = str(body.get("model") or settings.public_model)
    upstream_model = resolve_model_alias(
        requested_model,
        api_base=settings.api_base,
        api_key=settings.api_key,
        default_model=settings.nim_model,
        public_default=settings.public_model,
    )

    oai_body: Dict[str, Any] = {
        "model": upstream_model,
        "messages": anthropic_messages_to_openai(body, settings),
        "max_tokens": max_tokens,
        "temperature": float(body.get("temperature", 0.7)),
        "stream": bool(body.get("stream", False)) if stream is None else stream,
    }
    if "top_p" in body:
        oai_body["top_p"] = float(body["top_p"])
    if body.get("stop_sequences"):
        oai_body["stop"] = body["stop_sequences"]
    tools = anthropic_tools_to_openai(body.get("tools"))
    if tools:
        oai_body["tools"] = tools
        tool_choice = _tool_choice_to_openai(body.get("tool_choice"))
        if tool_choice is not None:
            oai_body["tool_choice"] = tool_choice
    return oai_body


def presentation_model_for_request(body: Dict[str, Any], settings: NIMSettings) -> str:
    requested = str(body.get("model") or "").strip()
    if not requested:
        return settings.public_model
    normalized = normalize_requested_model_alias(requested)
    return normalized or settings.public_model


def _parse_tool_arguments(raw: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"_raw": raw}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def openai_to_anthropic_message(
    data: Dict[str, Any],
    settings: NIMSettings,
    response_model: Optional[str] = None,
) -> Dict[str, Any]:
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content: List[Dict[str, Any]] = []
    text = message.get("content")
    if text:
        content.append({"type": "text", "text": str(text)})
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        content.append({
            "type": "tool_use",
            "id": str(call.get("id") or "toolu_" + uuid.uuid4().hex[:24]),
            "name": str(fn.get("name", "")),
            "input": _parse_tool_arguments(str(fn.get("arguments", "{}"))),
        })
    if not content:
        content.append({"type": "text", "text": ""})

    finish = choice.get("finish_reason")
    stop_reason = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
    }.get(finish, "end_turn")
    usage = data.get("usage") or {}
    return {
        "id": "msg_" + uuid.uuid4().hex[:24],
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": response_model or settings.public_model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
        },
    }


def _upstream_error(status_code: int, text: str) -> JSONResponse:
    message = text
    try:
        parsed = json.loads(text)
        message = str(parsed.get("message") or parsed.get("title") or parsed.get("error") or text)
    except Exception:
        pass
    error_type = "rate_limit_error" if status_code == 429 else "upstream_error"
    return JSONResponse(
        {"type": "error", "error": {"type": error_type, "message": message}},
        status_code=status_code,
    )


def should_return_rate_limit_as_message() -> bool:
    return os.environ.get("PULSAR_NIM_429_AS_MESSAGE", "1") != "0"


def should_return_api_issue_as_message(status_code: int) -> bool:
    if os.environ.get("PULSAR_NIM_API_ISSUE_AS_MESSAGE", "1") == "0":
        return False
    return status_code in {401, 403, 429, 500, 502, 503, 504}


def nim_issue_notice_text(settings: NIMSettings, status_code: int, detail: str = "") -> str:
    if status_code == 429:
        headline = "NVIDIA NIM returned 429 Too Many Requests."
        meaning = (
            "Your NVIDIA NIM key is rate-limited or has burned through its free 1000 credits. "
            "The local pulsarcode adapter is healthy; this is a per-account ceiling, not a local issue."
        )
        action = (
            "Rotate to a fresh NVIDIA account and paste its key:\n"
            "  pulsarcode /api\n\n"
            "Why a NEW account: the 1000 free credits belong to a single NVIDIA account and\n"
            "do not reset for about 30 days. Sign up at https://build.nvidia.com with a fresh\n"
            "email (Gmail aliases like you+nim2@gmail.com work), generate a new nvapi- key,\n"
            "paste it. Your model selection, Claude Code session, and chat history all carry over."
        )
    elif status_code in {401, 403}:
        headline = f"NVIDIA NIM returned HTTP {status_code} authentication failure."
        meaning = (
            "The local adapter is running, but the stored key was rejected or no "
            "longer has access to the selected model."
        )
        action = (
            "Paste a fresh key:\n"
            "  pulsarcode /api"
        )
    else:
        headline = f"NVIDIA NIM returned HTTP {status_code}."
        meaning = (
            "The local adapter is healthy, but the upstream NIM endpoint did not "
            "complete this request."
        )
        action = (
            "If this keeps happening, paste a fresh key or pick a different model:\n"
            "  pulsarcode /api          paste a fresh NVIDIA NIM key\n"
            "  pulsarcode pick          switch to a different model (fresh terminal tab)"
        )
    short_detail = " ".join(str(detail or "").split())
    if len(short_detail) > 420:
        short_detail = short_detail[:420] + "..."
    return (
        f"{headline}\n\n"
        f"{meaning}\n\n"
        f"{action}\n\n"
        "Panel flow when you paste:\n"
        f"  1. Open {NIM_SETUP_URL}\n"
        "  2. Sign in, or create an NVIDIA account on that page.\n"
        "  3. Click Get API Key in the model page right pane.\n"
        "  4. Click Generate Key, copy the key that starts with nvapi-, then paste it.\n\n"
        f"Official NVIDIA quickstart: {NIM_QUICKSTART_URL}\n"
        f"Active model: {settings.nim_model}\n"
        f"Local alias: {settings.public_model}"
        + (f"\nUpstream detail: {short_detail}" if short_detail else "")
    )


def rate_limit_notice_text(settings: NIMSettings) -> str:
    return nim_issue_notice_text(settings, 429)


def anthropic_text_message(
    settings: NIMSettings,
    text: str,
    response_model: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "id": "msg_" + uuid.uuid4().hex[:24],
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "model": response_model or settings.public_model,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": max(1, len(text.split()))},
    }


async def anthropic_text_stream(settings: NIMSettings, text: str, response_model: Optional[str] = None):
    msg = anthropic_text_message(settings, text, response_model=response_model)
    yield _sse("message_start", {
        "type": "message_start",
        "message": {
            "id": msg["id"],
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": response_model or settings.public_model,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    })
    yield _sse("content_block_start", {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": ""},
    })
    yield _sse("content_block_delta", {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": text},
    })
    yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
    yield _sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": msg["usage"]["output_tokens"]},
    })
    yield _sse("message_stop", {"type": "message_stop"})


async def call_nim(settings: NIMSettings, oai_body: Dict[str, Any]) -> httpx.Response:
    headers = {
        "Authorization": "Bearer " + settings.api_key,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=settings.timeout_s) as client:
        return await client.post(settings.chat_url, headers=headers, json=oai_body)


_PREFLIGHT_CACHE: Dict[str, tuple[float, int, str]] = {}


async def nim_preflight_error(settings: NIMSettings) -> Optional[JSONResponse]:
    if not settings.preflight_enabled:
        return None
    now = time.time()
    key = settings.api_key[:12] + ":" + settings.nim_model
    cached = _PREFLIGHT_CACHE.get(key)
    if cached is not None:
        until, status_code, text = cached
        if now < until:
            if status_code == 200:
                return None
            return _upstream_error(status_code, text)

    headers = {
        "Authorization": "Bearer " + settings.api_key,
        "Content-Type": "application/json",
    }
    body = {
        "model": settings.nim_model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
        "stream": False,
    }
    try:
        async with httpx.AsyncClient(timeout=settings.preflight_timeout_s) as client:
            response = await client.post(settings.chat_url, headers=headers, json=body)
    except httpx.TimeoutException:
        return None
    text = response.text
    _PREFLIGHT_CACHE[key] = (now + settings.preflight_ttl_s, response.status_code, text)
    if response.status_code == 200:
        return None
    return _upstream_error(response.status_code, text)


def _sse(event: str, payload: Dict[str, Any]) -> str:
    return "event: " + event + "\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _json_response_error_detail(response: JSONResponse) -> str:
    raw = getattr(response, "body", b"")
    try:
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return raw.decode("utf-8", errors="replace") if raw else ""
    error = parsed.get("error") if isinstance(parsed, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error.get("type") or "")
    return str(parsed)


def _iter_stream_blocks(message: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    for index, block in enumerate(message.get("content") or []):
        yield {"index": index, "block": block}


def _stop_reason_from_openai(finish_reason: Any) -> str:
    return {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
    }.get(finish_reason, "end_turn")


async def open_nim_stream(
    settings: NIMSettings,
    body: Dict[str, Any],
) -> tuple[Optional[httpx.AsyncClient], Optional[httpx.Response], Optional[JSONResponse]]:
    oai_body = build_openai_body(body, settings, stream=True)
    headers = {
        "Authorization": "Bearer " + settings.api_key,
        "Content-Type": "application/json",
    }
    timeout = httpx.Timeout(
        settings.stream_header_timeout_s,
        connect=min(5.0, settings.stream_header_timeout_s),
        read=settings.stream_header_timeout_s,
        write=settings.stream_header_timeout_s,
        pool=5.0,
    )
    client = httpx.AsyncClient(timeout=timeout)
    try:
        request = client.build_request(
            "POST",
            settings.chat_url,
            headers=headers,
            json=oai_body,
        )
        response = await client.send(request, stream=True)
    except httpx.TimeoutException:
        await client.aclose()
        return None, None, JSONResponse(
            {
                "type": "error",
                "error": {
                    "type": "upstream_timeout",
                    "message": "NVIDIA NIM did not return response headers inside the local fast-fail window",
                },
            },
            status_code=504,
        )
    except httpx.HTTPError as exc:
        await client.aclose()
        return None, None, JSONResponse(
            {"type": "error", "error": {"type": "upstream_error", "message": str(exc)}},
            status_code=502,
        )
    if response.status_code != 200:
        body_bytes = await response.aread()
        await response.aclose()
        await client.aclose()
        return None, None, _upstream_error(
            response.status_code,
            body_bytes.decode("utf-8", errors="replace"),
        )
    return client, response, None


async def anthropic_stream(
    settings: NIMSettings,
    response: httpx.Response,
    client: httpx.AsyncClient,
    response_model: Optional[str] = None,
):
    msg_id = "msg_" + uuid.uuid4().hex[:24]
    start = {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": response_model or settings.public_model,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    }
    yield _sse("message_start", start)

    next_index = 0
    text_index: Optional[int] = None
    text_open = False
    output_tokens = 0
    stop_reason = "end_turn"
    tool_calls: Dict[int, Dict[str, Any]] = {}

    try:
        async for raw in response.aiter_lines():
            if not raw or not raw.startswith("data:"):
                continue
            data_str = raw[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            usage = event.get("usage") or {}
            if usage.get("completion_tokens") is not None:
                output_tokens = int(usage.get("completion_tokens") or output_tokens)
            for choice in event.get("choices", []) or []:
                if choice.get("finish_reason") is not None:
                    stop_reason = _stop_reason_from_openai(choice.get("finish_reason"))
                delta = choice.get("delta") or {}
                text = delta.get("content")
                if text:
                    if text_index is None:
                        text_index = next_index
                        next_index += 1
                    if not text_open:
                        yield _sse("content_block_start", {
                            "type": "content_block_start",
                            "index": text_index,
                            "content_block": {"type": "text", "text": ""},
                        })
                        text_open = True
                    output_tokens += 1
                    yield _sse("content_block_delta", {
                        "type": "content_block_delta",
                        "index": text_index,
                        "delta": {"type": "text_delta", "text": str(text)},
                    })
                for call in delta.get("tool_calls") or []:
                    call_index = int(call.get("index", 0))
                    current = tool_calls.setdefault(call_index, {
                        "id": "",
                        "name": "",
                        "arguments": "",
                    })
                    if call.get("id"):
                        current["id"] = str(call["id"])
                    fn = call.get("function") or {}
                    if fn.get("name"):
                        current["name"] = str(fn["name"])
                    if fn.get("arguments"):
                        current["arguments"] += str(fn["arguments"])
    finally:
        await response.aclose()
        await client.aclose()

    if text_open and text_index is not None:
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": text_index})

    for _, call in sorted(tool_calls.items(), key=lambda item: item[0]):
        index = next_index
        next_index += 1
        tool_id = call["id"] or "toolu_" + uuid.uuid4().hex[:24]
        yield _sse("content_block_start", {
            "type": "content_block_start",
            "index": index,
            "content_block": {
                "type": "tool_use",
                "id": tool_id,
                "name": call["name"],
                "input": {},
            },
        })
        yield _sse("content_block_delta", {
            "type": "content_block_delta",
            "index": index,
            "delta": {
                "type": "input_json_delta",
                "partial_json": call["arguments"] or "{}",
            },
        })
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": index})

    yield _sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": output_tokens},
    })
    yield _sse("message_stop", {"type": "message_stop"})


async def anthropic_stream_from_nim(
    settings: NIMSettings,
    body: Dict[str, Any],
    response_model: Optional[str] = None,
):
    msg_id = "msg_" + uuid.uuid4().hex[:24]
    yield _sse("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": response_model or settings.public_model,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    })
    yield _sse("content_block_start", {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": ""},
    })

    open_task = asyncio.create_task(open_nim_stream(settings, body))
    ping_interval = max(0.05, settings.stream_ping_interval_s)
    while not open_task.done():
        try:
            await asyncio.wait_for(asyncio.shield(open_task), timeout=ping_interval)
        except asyncio.TimeoutError:
            yield _sse("ping", {"type": "ping"})

    client, response, stream_error = await open_task
    output_tokens = 0
    stop_reason = "end_turn"
    tool_calls: Dict[int, Dict[str, Any]] = {}

    if stream_error is not None:
        detail = _json_response_error_detail(stream_error)
        text = nim_issue_notice_text(settings, stream_error.status_code, detail)
        output_tokens = max(1, len(text.split()))
        yield _sse("content_block_delta", {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": text},
        })
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        yield _sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": output_tokens},
        })
        yield _sse("message_stop", {"type": "message_stop"})
        return

    assert client is not None
    assert response is not None
    try:
        async for raw in response.aiter_lines():
            if not raw or not raw.startswith("data:"):
                continue
            data_str = raw[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            usage = event.get("usage") or {}
            if usage.get("completion_tokens") is not None:
                output_tokens = int(usage.get("completion_tokens") or output_tokens)
            for choice in event.get("choices", []) or []:
                if choice.get("finish_reason") is not None:
                    stop_reason = _stop_reason_from_openai(choice.get("finish_reason"))
                delta = choice.get("delta") or {}
                text = delta.get("content")
                if text:
                    output_tokens += 1
                    yield _sse("content_block_delta", {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": str(text)},
                    })
                for call in delta.get("tool_calls") or []:
                    call_index = int(call.get("index", 0))
                    current = tool_calls.setdefault(call_index, {
                        "id": "",
                        "name": "",
                        "arguments": "",
                    })
                    if call.get("id"):
                        current["id"] = str(call["id"])
                    fn = call.get("function") or {}
                    if fn.get("name"):
                        current["name"] = str(fn["name"])
                    if fn.get("arguments"):
                        current["arguments"] += str(fn["arguments"])
    finally:
        await response.aclose()
        await client.aclose()

    yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})

    next_index = 1
    for _, call in sorted(tool_calls.items(), key=lambda item: item[0]):
        index = next_index
        next_index += 1
        tool_id = call["id"] or "toolu_" + uuid.uuid4().hex[:24]
        yield _sse("content_block_start", {
            "type": "content_block_start",
            "index": index,
            "content_block": {
                "type": "tool_use",
                "id": tool_id,
                "name": call["name"],
                "input": {},
            },
        })
        yield _sse("content_block_delta", {
            "type": "content_block_delta",
            "index": index,
            "delta": {
                "type": "input_json_delta",
                "partial_json": call["arguments"] or "{}",
            },
        })
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": index})

    yield _sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": output_tokens},
    })
    yield _sse("message_stop", {"type": "message_stop"})


def estimate_tokens(body: Dict[str, Any], settings: NIMSettings) -> int:
    rendered = _json_dumps(build_openai_body(body, settings))
    return max(1, len(rendered) // 4)


def make_app(settings: Optional[NIMSettings] = None) -> FastAPI:
    if settings is None:
        key = os.environ.get("NVIDIA_NIM_API_KEY") or os.environ.get("NVIDIA_API_KEY") or ""
        settings = NIMSettings(
            api_key=key,
            api_base=os.environ.get("NVIDIA_NIM_API_BASE", DEFAULT_API_BASE),
            public_model=os.environ.get("PULSAR_NIM_PUBLIC_MODEL", PUBLIC_MODEL),
            nim_model=os.environ.get("PULSAR_NIM_MODEL", NIM_MODEL),
            preflight_enabled=os.environ.get("PULSAR_NIM_PREFLIGHT", "1") != "0",
            preflight_ttl_s=float(os.environ.get("PULSAR_NIM_PREFLIGHT_TTL", "30")),
            preflight_timeout_s=float(os.environ.get("PULSAR_NIM_PREFLIGHT_TIMEOUT", "5")),
            stream_header_timeout_s=float(os.environ.get("PULSAR_NIM_STREAM_HEADER_TIMEOUT", "180")),
            stream_ping_interval_s=float(os.environ.get("PULSAR_NIM_STREAM_PING_INTERVAL", "5")),
        )

    app = FastAPI(title="pulsarcode NVIDIA NIM Anthropic adapter", version="0.1")
    trace_enabled = os.environ.get("PULSAR_NIM_PROXY_TRACE", "0") == "1"

    @app.middleware("http")
    async def trace_requests(request: Request, call_next):
        if not trace_enabled:
            return await call_next(request)
        started = time.time()
        length = request.headers.get("content-length", "0")
        print(
            f"nim-proxy request start method={request.method} path={request.url.path} "
            f"length={length}",
            flush=True,
        )
        response = await call_next(request)
        elapsed_ms = int((time.time() - started) * 1000)
        print(
            f"nim-proxy request done method={request.method} path={request.url.path} "
            f"status={response.status_code} elapsed_ms={elapsed_ms}",
            flush=True,
        )
        return response

    @app.get("/healthz")
    async def healthz():
        return {
            "status": "ok",
            "model": settings.public_model,
            "upstream_model": settings.nim_model,
            "adapter_protocol": ADAPTER_PROTOCOL,
            "api_key_present": bool(settings.api_key),
        }

    @app.get("/v1/models")
    async def models():
        records = runtime_catalog(
            api_base=settings.api_base,
            api_key=settings.api_key,
            public_default=settings.public_model,
        )
        return openai_models_payload(records, public_default=settings.public_model)

    @app.get("/v1/models/{model_id:path}")
    async def model_detail(model_id: str):
        records = runtime_catalog(
            api_base=settings.api_base,
            api_key=settings.api_key,
            public_default=settings.public_model,
        )
        payload = openai_models_payload(records, public_default=settings.public_model)
        for item in payload["data"]:
            if str(item.get("id")) == model_id:
                return item
        normalized = normalize_requested_model_alias(model_id)
        for item in payload["data"]:
            if normalize_requested_model_alias(str(item.get("id"))) == normalized:
                return item
        return JSONResponse(
            {"type": "error", "error": {"type": "not_found_error", "message": "model not found"}},
            status_code=404,
        )

    @app.post("/v1/messages/count_tokens")
    async def count_tokens(req: Request):
        body = await req.json()
        return {"input_tokens": estimate_tokens(body, settings)}

    @app.post("/v1/messages")
    async def messages(req: Request):
        body = await req.json()
        response_model = presentation_model_for_request(body, settings)
        if not settings.api_key:
            text = nim_issue_notice_text(settings, 401, "NVIDIA_NIM_API_KEY is not set")
            if bool(body.get("stream", False)):
                return StreamingResponse(
                    anthropic_text_stream(settings, text, response_model=response_model),
                    media_type="text/event-stream",
                )
            return anthropic_text_message(settings, text, response_model=response_model)
        preflight_error = await nim_preflight_error(settings)
        if preflight_error is not None:
            if (
                should_return_api_issue_as_message(preflight_error.status_code)
                and (preflight_error.status_code != 429 or should_return_rate_limit_as_message())
            ):
                text = nim_issue_notice_text(settings, preflight_error.status_code)
                if bool(body.get("stream", False)):
                    return StreamingResponse(
                        anthropic_text_stream(settings, text, response_model=response_model),
                        media_type="text/event-stream",
                    )
                return anthropic_text_message(settings, text, response_model=response_model)
            return preflight_error
        if bool(body.get("stream", False)):
            return StreamingResponse(
                anthropic_stream_from_nim(settings, body, response_model=response_model),
                media_type="text/event-stream",
            )
        oai_body = build_openai_body(body, settings)
        response = await call_nim(settings, oai_body)
        if response.status_code != 200:
            if (
                should_return_api_issue_as_message(response.status_code)
                and (response.status_code != 429 or should_return_rate_limit_as_message())
            ):
                return anthropic_text_message(
                    settings,
                    nim_issue_notice_text(settings, response.status_code, response.text),
                    response_model=response_model,
                )
            return _upstream_error(response.status_code, response.text)
        return openai_to_anthropic_message(response.json(), settings, response_model=response_model)

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("PULSAR_NIM_PROXY_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PULSAR_NIM_PROXY_PORT", "4000")))
    parser.add_argument("--api-base", default=os.environ.get("NVIDIA_NIM_API_BASE", DEFAULT_API_BASE))
    parser.add_argument("--model", default=os.environ.get("PULSAR_NIM_MODEL", NIM_MODEL))
    parser.add_argument("--public-model", default=os.environ.get("PULSAR_NIM_PUBLIC_MODEL", PUBLIC_MODEL))
    args = parser.parse_args()

    key = os.environ.get("NVIDIA_NIM_API_KEY") or os.environ.get("NVIDIA_API_KEY") or ""
    settings = NIMSettings(
        api_key=key,
        api_base=args.api_base,
        public_model=args.public_model,
        nim_model=args.model,
        preflight_enabled=os.environ.get("PULSAR_NIM_PREFLIGHT", "1") != "0",
        preflight_ttl_s=float(os.environ.get("PULSAR_NIM_PREFLIGHT_TTL", "30")),
        preflight_timeout_s=float(os.environ.get("PULSAR_NIM_PREFLIGHT_TIMEOUT", "5")),
        stream_header_timeout_s=float(os.environ.get("PULSAR_NIM_STREAM_HEADER_TIMEOUT", "180")),
        stream_ping_interval_s=float(os.environ.get("PULSAR_NIM_STREAM_PING_INTERVAL", "5")),
    )
    uvicorn.run(make_app(settings), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
