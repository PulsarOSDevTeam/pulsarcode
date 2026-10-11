# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Anthropic Messages <-> OpenAI Responses API (POST {base}/responses).

Some current models only call tools through the Responses API (OpenAI's
gpt-5.x-codex, gpt-6.x; see Provider.wire_for). This module mirrors
pulsar.translate so Core can pick the wire per model:

    build_request(...)            Anthropic request   -> Responses request
    ResponsesStreamTranslator     Responses SSE data  -> Anthropic SSE events
    to_anthropic_message(...)     Responses response  -> Anthropic message
    heal_from_error(...)          Responses 400s      -> adjusted heal flags

Pure functions plus one streaming state machine. No I/O.

Requests are stateless (store: false). When the caller asked for thinking,
reasoning models are asked for their encrypted reasoning, which is handed
to Claude Code as the signature of a thinking block
("pulsar-rs1:<model tag>:<item id>:<encrypted>") and turned back into a
reasoning item on the next turn, so the model keeps its chain of thought
across tool calls. Only signatures minted for the same model are sent back;
others (Anthropic signatures after a /model switch, another model's
reasoning) are dropped. Use strip_carried() before relaying a transcript to
an Anthropic upstream.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pulsar.providers import clean_text
from pulsar.redact import redact
from pulsar.translate import (Event, _int, context_overflow, cut_note, final_stop, new_id, output_cap, sanitize_schema,
                              sse, system_text)

__all__ = [
    "CARRY_PREFIX", "RENDERED_PARTS", "ResponsesStreamTranslator", "build_request", "cut_note", "error_of",
    "heal_from_error", "input_items", "is_reasoning_family", "sse", "strict_arguments", "strip_carried",
    "to_anthropic_message", "tool_choice_to_responses", "tools_to_responses",
]

CARRY_PREFIX = "pulsar-rs1:"
# The parts of an output message that to_anthropic_message turns into text (Core's _answered reads the same).
RENDERED_PARTS = ("output_text", "refusal", "text")

_FAMILY = re.compile(r"(?i)(^|/)(o\d+(-|$)|gpt-5|gpt-([6-9]|\d{2,})(\b|[.-])|codex)")
_OVERFLOW = re.compile(r"(context[_ ]length|maximum context|context window|too many tokens|prompt is too long|"
                       r"input is too long|reduce the length|exceeds? the (model'?s? )?(context|maximum)|"
                       r"token limit|max_input_tokens)", re.I)
_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _obj(value: Any) -> Dict[str, Any]:
    """An upstream field that should hold an object: the object, or {} for anything else."""
    return value if isinstance(value, dict) else {}


def _provider_error(message: Any, secrets: Sequence[str] = ()) -> str:
    """Error text a provider sent, safe for Claude Code's transcript and the terminal: control characters
    (terminal escapes) removed first, so a key they split is still masked, then keys masked, then cut short."""
    raw = message if isinstance(message, str) else "" if message is None else str(message)
    text = redact(clean_text(" ".join(raw.split()), len(raw)), secrets)
    return text if len(text) <= 500 else text[:500] + "..."


def is_reasoning_family(model: str) -> bool:
    """Models that take a `reasoning` object (o-series, gpt-5+, codex), excluding chat aliases."""
    low = (model or "").lower()
    return bool(_FAMILY.search(low)) and "chat" not in low.split("/")[-1]


def _allowed_efforts(model: str) -> Tuple[str, ...]:
    low = (model or "").lower()
    if re.search(r"(^|/)gpt-([6-9]|\d{2,})(\b|[.-])", low):
        return _EFFORTS
    if re.search(r"(^|/)gpt-5\.\d", low):
        return _EFFORTS[:4]
    return _EFFORTS[:3]


def _thinking(body: Dict[str, Any]) -> Dict[str, Any]:
    t = body.get("thinking")
    return t if isinstance(t, dict) else {}


def _thinking_requested(body: Dict[str, Any]) -> bool:
    return _thinking(body).get("type") in ("enabled", "adaptive")


def _effort(body: Dict[str, Any], model: str, cap: str = "") -> str:
    oc = body.get("output_config")
    want = str(oc.get("effort") or "").lower() if isinstance(oc, dict) else ""
    th = _thinking(body)
    if not want and th.get("type") == "enabled":
        budget = int(th.get("budget_tokens") or 0)
        want = "low" if 0 < budget < 4096 else "medium" if budget < 16384 else "high"
    if want not in _EFFORTS:
        return ""
    allowed = list(_allowed_efforts(model))
    if cap in _EFFORTS:
        allowed = [e for e in allowed if _EFFORTS.index(e) <= _EFFORTS.index(cap)]
    while want not in allowed and _EFFORTS.index(want) > 0:
        want = _EFFORTS[_EFFORTS.index(want) - 1]
    return want if want in allowed else ""


# ---------------------------------------------------------------------------
# Request: Anthropic -> Responses
# ---------------------------------------------------------------------------

def _data_url(block: Dict[str, Any]) -> Optional[str]:
    src = block.get("source") or {}
    if src.get("type") == "base64" and src.get("data"):
        return "data:%s;base64,%s" % (src.get("media_type") or "image/png", src["data"])
    if src.get("type") == "url" and src.get("url"):
        return str(src["url"])
    return None


def _result_parts(content: Any, vision: bool) -> Tuple[str, List[Dict[str, Any]]]:
    """Tool result -> (text output, images to show in a follow-up user message)."""
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return _dumps(content), []
    texts: List[str] = []
    images: List[Dict[str, Any]] = []
    for b in content:
        if isinstance(b, dict):
            if b.get("type") == "text":
                texts.append(str(b.get("text", "")))
            elif b.get("type") == "image":
                url = _data_url(b)
                if url and vision:
                    images.append({"type": "input_image", "image_url": url, "detail": "auto"})
                    texts.append("[image %d: attached in the next message]" % len(images))
                else:
                    texts.append("[image omitted: this model cannot view tool images]")
            elif b.get("type") == "document":
                texts.append("[document omitted]")
            else:
                texts.append(_dumps(b))
        elif b is not None:
            texts.append(str(b))
    return "\n".join(t for t in texts if t), images


def _user_parts(blocks: List[Any], vision: bool, files: bool) -> List[Dict[str, Any]]:
    parts: List[Dict[str, Any]] = []
    for b in blocks:
        if not isinstance(b, dict):
            parts.append({"type": "input_text", "text": str(b)})
            continue
        t = b.get("type")
        if t == "text":
            if b.get("text"):
                parts.append({"type": "input_text", "text": str(b["text"])})
        elif t == "image":
            url = _data_url(b)
            if url and vision:
                parts.append({"type": "input_image", "image_url": url, "detail": "auto"})
            else:
                parts.append({"type": "input_text", "text": "[image attached; not forwarded to this model]"})
        elif t == "document":
            src = b.get("source") or {}
            title = str(b.get("title") or src.get("media_type") or "document")
            if src.get("type") == "text" and src.get("data"):
                parts.append({"type": "input_text", "text": "[document: %s]\n%s" % (title, src["data"])})
            elif (files and vision and src.get("type") == "base64" and src.get("data")
                  and src.get("media_type") == "application/pdf"):
                name = re.sub(r"[^A-Za-z0-9._-]+", "_", title)[:60] or "document"
                parts.append({"type": "input_file", "filename": name if name.endswith(".pdf") else name + ".pdf",
                              "file_data": "data:application/pdf;base64,%s" % src["data"]})
            else:
                parts.append({"type": "input_text", "text": "[document omitted: %s]" % title})
        elif t in ("thinking", "redacted_thinking", "tool_result"):
            continue
        else:
            parts.append({"type": "input_text", "text": _dumps(b)})
    return parts


def _user_text(text: str, turn: int, soft: bool = False, role: str = "user") -> Dict[str, Any]:
    item: Dict[str, Any] = {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}],
                            "_turn": turn}
    if soft:
        item["_soft"] = True
    return item


def _tag(model: str) -> str:
    return hashlib.sha1((model or "").strip().lower().encode("utf-8")).hexdigest()[:8]


def _carried(block: Dict[str, Any], model: str) -> Optional[Dict[str, Any]]:
    sig = str(block.get("signature") or "")
    if not sig.startswith(CARRY_PREFIX):
        return None
    parts = sig[len(CARRY_PREFIX):].split(":", 2)
    if len(parts) != 3 or parts[0] != _tag(model) or not parts[1] or not parts[2]:
        return None
    rid, enc = parts[1], parts[2]
    text = str(block.get("thinking") or "")
    return {"type": "reasoning", "id": rid, "encrypted_content": enc,
            "summary": [{"type": "summary_text", "text": text}] if text else []}


def input_items(body: Dict[str, Any], vision: bool = True, carry: bool = True, files: bool = True,
                developer: bool = True, model: str = "") -> Tuple[str, List[Dict[str, Any]]]:
    """Anthropic system + messages -> (instructions, Responses input items).

    Carried reasoning is only replayed when `carry` is set and it was minted for `model`.
    """
    instructions = [system_text(body.get("system"))]
    items: List[Dict[str, Any]] = []
    for turn, msg in enumerate(body.get("messages") or []):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or "user"
        content = msg.get("content")
        if role == "system":
            text = content if isinstance(content, str) else _result_parts(content, False)[0]
            if not text:
                continue
            if not items:
                instructions.append(text)
            elif developer:
                items.append(_user_text(text, turn, soft=True, role="developer"))
            else:
                items.append(_user_text("<system-reminder>\n%s\n</system-reminder>" % text, turn, soft=True))
            continue
        if role not in ("user", "assistant"):
            role = "user"                   # Responses rejects unknown roles ('ctx', 'msg', ...)
        if isinstance(content, str) or not isinstance(content, list):
            text = content if isinstance(content, str) else _result_parts(content, False)[0]
            if role == "assistant":
                if text:
                    items.append({"type": "message", "role": "assistant", "content": text, "_turn": turn})
            else:
                items.append(_user_text(text or "(empty)", turn))
            continue

        if role == "assistant":
            turn_items: List[Dict[str, Any]] = []
            texts: List[str] = []

            def flush() -> None:
                if texts:
                    turn_items.append({"type": "message", "role": "assistant", "content": "".join(texts), "_turn": turn})
                    texts.clear()

            for b in content:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "thinking" and carry:
                    r = _carried(b, model)
                    if r is not None:
                        flush()
                        r["_turn"] = turn
                        turn_items.append(r)
                elif t == "text" and b.get("text"):
                    texts.append(str(b["text"]))
                elif t in ("tool_use", "server_tool_use"):
                    flush()
                    turn_items.append({"type": "function_call", "call_id": str(b.get("id") or new_id("toolu_")),
                                       "name": str(b.get("name", "")), "arguments": _dumps(b.get("input") or {}),
                                       "_turn": turn})
            flush()
            # A reasoning item must be followed by the item it produced.
            while turn_items and turn_items[-1]["type"] == "reasoning":
                turn_items.pop()
            items.extend(turn_items)
            continue

        # user turn: tool outputs first (they answer the previous calls), then
        # images those tools returned, then whatever the user added.
        images: List[Dict[str, Any]] = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                text, imgs = _result_parts(b.get("content"), vision)
                if b.get("is_error"):
                    text = "Error: " + (text or "tool failed")
                items.append({"type": "function_call_output", "call_id": str(b.get("tool_use_id", "")),
                              "output": text or "(no output)", "_turn": turn})
                images.extend(imgs)
        if images:
            items.append({"type": "message", "role": "user", "_turn": turn,
                          "content": [{"type": "input_text", "text": "Images returned by the tool calls above:"}] + images})
        parts = _user_parts([b for b in content if not (isinstance(b, dict) and b.get("type") == "tool_result")],
                            vision, files)
        if parts:
            items.append({"type": "message", "role": "user", "content": parts, "_turn": turn})
    return "\n\n".join(t for t in instructions if t), repair_items(items)


def repair_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Make the input legal for strict validators.

    * every function_call gets an output before the conversation moves on
      (interrupted tool runs leave holes that upstream rejects with 400)
    * outputs without a matching call become plain user text
    * mid-conversation system notes never split calls from their outputs
    """
    fixed: List[Dict[str, Any]] = []
    pending: List[str] = []
    pending_turn = -1
    answered: set = set()
    deferred: List[Dict[str, Any]] = []

    def clean(it: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in it.items() if not k.startswith("_")}

    def close() -> None:
        for cid in pending:
            fixed.append({"type": "function_call_output", "call_id": cid,
                          "output": "(no result: the tool call was interrupted)"})
        pending.clear()
        while deferred:
            fixed.append(clean(deferred.pop(0)))

    for it in items:
        kind = it.get("type")
        turn = int(it.get("_turn", -1))
        if it.get("_soft") and pending:
            deferred.append(it)
            continue
        if kind == "function_call_output":
            cid = it.get("call_id", "")
            if cid in pending:
                pending.remove(cid)
                fixed.append(clean(it))
                if not pending:
                    close()
            elif cid in answered:
                continue                    # duplicate result for an id we already answered
            else:
                fixed.append({"type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": "Tool result (%s):\n%s" % (cid or "unknown", it.get("output", ""))}]})
            continue
        if pending and turn != pending_turn:
            close()
        fixed.append(clean(it))
        if kind == "function_call":
            cid = it.get("call_id", "")
            if cid in answered:
                cid = new_id("toolu_")      # ids must be unique within one input
                fixed[-1]["call_id"] = cid
            pending.append(cid)
            pending_turn = turn
            answered.add(cid)
    close()
    if not any(it.get("type") != "message" or it.get("role") != "developer" for it in fixed):
        fixed.append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "(empty)"}]})
    return fixed


def tools_to_responses(tools: Any, strict: bool = False) -> List[Dict[str, Any]]:
    out = []
    for t in tools or []:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        if t.get("type") and t["type"] not in ("custom",) and not t.get("input_schema"):
            continue  # server tools (web_search_20250305, bash_2024...) have no portable form
        params = sanitize_schema(t.get("input_schema") or {"type": "object", "properties": {}}, strict)
        if not isinstance(params, dict) or params.get("type") != "object":
            params = {"type": "object", "properties": {}}
        out.append({"type": "function", "name": str(t["name"])[:64],
                    "description": str(t.get("description") or "")[:8000],
                    "parameters": params, "strict": False})
    return out


def tool_choice_to_responses(choice: Any) -> Any:
    if not isinstance(choice, dict):
        return None
    t = choice.get("type")
    if t == "auto":
        return "auto"
    if t == "any":
        return "required"
    if t == "none":
        return "none"
    if t == "tool" and choice.get("name"):
        return {"type": "function", "name": str(choice["name"])}
    return None


def build_request(body: Dict[str, Any], model: str, provider_id: str, *, stream: bool,
                  max_output: int = 0, strict_schema: bool = False,
                  heal: Optional[Dict[str, Any]] = None, vision: bool = True,
                  usage_in_stream: bool = False, **_chat_only: Any) -> Dict[str, Any]:
    """Anthropic Messages request -> Responses API request (same contract as translate.build_request).

    usage_in_stream and other chat-only keywords are accepted so Core can call both
    builders the same way; the Responses API always reports usage in response.completed.
    """
    heal = heal or {}
    vision = vision and not heal.get("no_vision")
    family = is_reasoning_family(model)
    store = not heal.get("drop_store")
    carry = (family and store and _thinking_requested(body) and not heal.get("drop_carry")
             and not heal.get("drop_reasoning") and not heal.get("drop_include"))
    instructions, items = input_items(body, vision=vision, carry=family and not heal.get("drop_carry"),
                                      files=not heal.get("no_files"), developer=not heal.get("no_developer"),
                                      model=model)
    req: Dict[str, Any] = {"model": model, "input": items, "stream": stream}
    if instructions:
        req["instructions"] = instructions
    if store:
        req["store"] = False                # stateless: nothing is kept at the provider

    max_tokens = int(body.get("max_tokens") or 4096)
    for cap in (max_output, int(heal.get("max_tokens_cap") or 0)):
        if cap:
            max_tokens = min(max_tokens, cap)
    if not heal.get("drop_max_tokens"):
        req["max_output_tokens"] = max_tokens

    if not family:
        if "temperature" in body and not heal.get("drop_temperature"):
            req["temperature"] = float(body["temperature"])
        if "top_p" in body and not heal.get("drop_top_p"):
            req["top_p"] = float(body["top_p"])
    # The Responses API has no stop sequences; Claude Code rarely sends them.

    tools = tools_to_responses(body.get("tools"), strict=strict_schema or bool(heal.get("strict_schema")))
    if tools and not heal.get("drop_tools"):
        req["tools"] = tools
        tc = body.get("tool_choice")
        choice = tool_choice_to_responses(tc)
        if choice is not None and not heal.get("drop_tool_choice"):
            req["tool_choice"] = choice
        if isinstance(tc, dict) and tc.get("disable_parallel_tool_use") and not heal.get("drop_parallel"):
            req["parallel_tool_calls"] = False

    if family and not heal.get("drop_reasoning"):
        reasoning: Dict[str, Any] = {}
        effort = _effort(body, model, str(heal.get("effort_cap") or ""))
        if effort:
            reasoning["effort"] = effort
        th = _thinking(body)
        if _thinking_requested(body) and th.get("display") != "omitted" and not heal.get("drop_summary"):
            reasoning["summary"] = "auto"
        if reasoning:
            req["reasoning"] = reasoning
    if carry:
        req["include"] = ["reasoning.encrypted_content"]
    return req


def _error_fields(text: str) -> Tuple[str, str, str]:
    """(message, param, code) from an error body (object, or Gemini-style array)."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return text or "", "", ""
    if isinstance(data, list) and data:
        data = data[0]
    err = data.get("error", data) if isinstance(data, dict) else data
    if isinstance(err, dict):
        return (str(err.get("message") or ""), str(err.get("param") or ""), str(err.get("code") or ""))
    return str(err), "", ""


def heal_from_error(status: int, text: str, req: Dict[str, Any], heal: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Given an upstream 400/404/422 from /responses, return adjusted heal flags or None if hopeless."""
    if status not in (400, 404, 422):
        return None
    message, param, code = _error_fields(text)
    low = (message + " " + (text or "")).lower()
    param = param.lower()
    if context_overflow(status, message + " " + (text or "")) or code == "context_length_exceeded":
        return None                         # Core reports overflow so Claude Code compacts
    new = dict(heal)

    def changed() -> Optional[Dict[str, Any]]:
        return new if new != heal else None

    def about(*names: str) -> bool:
        return any(n == param or param.startswith(n + ".") or n in low for n in names)

    if re.search(r"encrypted[ _]content", low) or ("reasoning" in low and ("not found" in low or "without its required" in low
                                                              or "provided without" in low)):
        new["drop_carry"] = True
        return changed()
    if param == "reasoning.summary" or "reasoning.summary" in low or ("summar" in low and "verif" in low):
        new["drop_summary"] = True
        return changed()
    if param == "reasoning.effort" or "reasoning.effort" in low or "reasoning_effort" in low:
        if heal.get("effort_cap") not in ("high", "medium", "low"):
            new["effort_cap"] = "high"
        else:
            new["drop_reasoning"] = True
        return changed()
    if param == "reasoning" or ("reasoning" in low and ("unsupported" in low or "not supported" in low)):
        new["drop_reasoning"] = True
        return changed()
    if param == "include" or "'include'" in low:
        new["drop_include"] = True
        return changed()
    if about("max_output_tokens") or re.search(r"max_(output_|completion_)?tokens|maximum output|output tokens", low):
        new["max_tokens_cap"] = output_cap(low, int(req.get("max_output_tokens") or 4096))
        return changed()
    if about("temperature"):
        new["drop_temperature"] = True
        return changed()
    if about("top_p"):
        new["drop_top_p"] = True
        return changed()
    if about("parallel_tool_calls"):
        new["drop_parallel"] = True
        return changed()
    if about("tool_choice"):
        new["drop_tool_choice"] = True
        return changed()
    if param == "store" or "'store'" in low or "store must" in low:
        new["drop_store"] = True
        return changed()
    if "developer" in low:
        new["no_developer"] = True
        return changed()
    if ("schema" in low or "parameters" in low or param.startswith("tools")) and "tools" in req \
            and not heal.get("strict_schema"):
        new["strict_schema"] = True
        return changed()
    if re.search(r"input_file|\bpdf\b|file_data", low) and not heal.get("no_files"):
        new["no_files"] = True
        return changed()
    if re.search(r"image|vision|multimodal|input_image", low) and not heal.get("no_vision"):
        new["no_vision"] = True
        return changed()
    return None


# ---------------------------------------------------------------------------
# Response: Responses -> Anthropic
# ---------------------------------------------------------------------------

_STATUS_BY_CODE = {"rate_limit_exceeded": 429, "insufficient_quota": 429, "server_error": 500,
                   "server_is_overloaded": 529, "slow_down": 429, "context_length_exceeded": 400,
                   "invalid_prompt": 400, "invalid_request_error": 400, "vector_store_timeout": 504,
                   "invalid_api_key": 401, "authentication_error": 401, "permission_denied": 403,
                   "model_not_found": 404}


def error_of(event: Dict[str, Any]) -> Optional[Tuple[int, str]]:
    """(HTTP-like status, JSON detail) for an "error" or "response.failed" event, else None.

    The detail keeps the provider's code, so Core's overflow detection works.
    """
    if not isinstance(event, dict):
        return None
    kind = event.get("type")
    if kind == "error":
        err = event.get("error") if isinstance(event.get("error"), dict) else event
    elif kind == "response.failed":
        err = _obj(event.get("response")).get("error") or {"message": "the response failed"}
    else:
        return None
    if not isinstance(err, dict):
        err = {"message": str(err)}
    code = str(err.get("code") or err.get("type") or "")
    status = _int(err.get("status")) or _STATUS_BY_CODE.get(code, 500)
    detail = {"message": str(err.get("message") or "upstream error"), "code": code}
    return status, json.dumps({"error": detail})


def _carry_signature(item: Dict[str, Any], model: str) -> str:
    enc = item.get("encrypted_content")
    rid = item.get("id")
    if isinstance(enc, str) and enc and isinstance(rid, str) and rid and ":" not in rid:
        return CARRY_PREFIX + _tag(model) + ":" + rid + ":" + enc
    return ""


def _summary_text(item: Dict[str, Any]) -> str:
    parts = []
    for key in ("summary", "content"):
        for p in item.get(key) or []:
            if isinstance(p, dict) and p.get("text"):
                parts.append(str(p["text"]))
    return "\n\n".join(parts)


def _tool_names(tools: Any) -> Dict[str, str]:
    names: Dict[str, str] = {}
    for t in tools or []:
        if isinstance(t, dict) and t.get("name"):
            name = str(t["name"])
            names.setdefault(name.lower(), name)
    return names


def _fix_name(name: str, names: Dict[str, str]) -> str:
    if not names or name in names.values():
        return name
    return names.get(name.lower(), name)


def strict_arguments(raw: Any) -> Optional[Dict[str, Any]]:
    """Tool arguments only when they are complete, valid JSON objects (an empty string means {})."""
    if isinstance(raw, dict):
        return raw
    text = (raw or "").strip() if isinstance(raw, str) else None
    if text is None:
        return None
    if not text:
        return {}
    try:
        val = json.loads(text)
    except ValueError:
        return None
    return val if isinstance(val, dict) else None


def to_anthropic_message(data: Dict[str, Any], model: str, input_estimate: int = 0,
                         tools: Any = None, *, secrets: Sequence[str] = ()) -> Dict[str, Any]:
    """A non-streamed Responses answer -> an Anthropic message. `secrets` (the target's key) are masked in a
    provider's error text, as every known key shape always is."""
    names = _tool_names(tools)
    content: List[Dict[str, Any]] = []
    limit = data.get("status") == "incomplete" and \
        _obj(data.get("incomplete_details")).get("reason") == "max_output_tokens"
    cut_last = False                    # the reply ends in a call the output cap cut off
    output = data.get("output")
    for item in output if isinstance(output, list) else []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "reasoning":
            sig = _carry_signature(item, model)
            if sig:
                content.append({"type": "thinking", "thinking": _summary_text(item), "signature": sig})
        elif kind == "message":
            text = "".join(str(p.get("text") or p.get("refusal") or "") for p in item.get("content") or []
                           if isinstance(p, dict) and p.get("type") in RENDERED_PARTS)
            if not text:
                continue
            cut_last = False
            if content and content[-1]["type"] == "text":
                content[-1]["text"] += text
            else:
                content.append({"type": "text", "text": text})
        elif kind == "function_call":
            name = _fix_name(str(item.get("name") or "unknown_tool"), names)
            done = item.get("status") in (None, "completed")
            args = strict_arguments(item.get("arguments")) if done else None
            if args is None:
                content.append({"type": "text", "text": cut_note(name, limit=limit and not done)})
            else:
                content.append({"type": "tool_use", "id": new_id("toolu_"), "name": name, "input": args})
            cut_last = not done
    if data.get("status") == "failed" and not any(b["type"] in ("text", "tool_use") for b in content):
        err = data.get("error") or {}
        content.append({"type": "text", "text": "The provider reported an error: %s" % (
            _provider_error(err.get("message") if isinstance(err, dict) else err, secrets) or "unknown error")})
    if not any(b["type"] in ("text", "tool_use") for b in content):
        content.append({"type": "text", "text": ""})
    stop = final_stop("max_tokens" if limit else "end_turn", sum(b["type"] == "tool_use" for b in content), False,
                      limit and cut_last)
    usage = _obj(data.get("usage"))
    cached = _int(_obj(usage.get("input_tokens_details")).get("cached_tokens")) or 0
    text_len = sum(len(b.get("text", "")) for b in content if b["type"] == "text")
    out_usage: Dict[str, int] = {
        "input_tokens": max(0, (_int(usage.get("input_tokens")) or int(input_estimate)) - cached),
        "output_tokens": _int(usage.get("output_tokens")) or max(1, text_len // 4),
    }
    if cached:
        out_usage["cache_read_input_tokens"] = cached
    return {"id": new_id("msg_"), "type": "message", "role": "assistant", "model": model,
            "content": content, "stop_reason": stop, "stop_sequence": None, "usage": out_usage}


# Stream events that mean the model went on writing (more output, or the next item): a call cut before them was
# not where the reply ended.
_MORE_OUTPUT = frozenset((
    "response.output_text.delta", "response.refusal.delta", "response.reasoning_summary_text.delta",
    "response.reasoning_text.delta", "response.reasoning_summary_part.added", "response.output_item.added",
    "response.function_call_arguments.delta", "response.output_item.done"))


class ResponsesStreamTranslator:
    """Responses API stream events -> Anthropic SSE events.

    Same surface as translate.StreamTranslator. feed() takes one parsed SSE
    data object (it carries its own "type"). Text and thinking stream as they
    arrive; each function call is buffered and emitted as one complete
    tool_use block (fresh toolu_ id, complete JSON only) when its item is done, so
    interleaved or truncated calls never hand Claude Code broken JSON.

    After feed(), `failure` holds {"status", "code", "message", "detail"} when
    the upstream reported an error, and `done` turns True once the response
    completed, stopped early or failed.
    """

    def __init__(self, model: str, input_tokens: int = 0, emit_thinking: bool = True, tools: Any = None) -> None:
        self.model = model
        self.input_tokens = input_tokens
        self.emit_thinking = emit_thinking
        self.names = _tool_names(tools)
        self.index = -1
        self.open_type: Optional[str] = None
        self.tools_emitted = 0
        self.cut_calls: List[str] = []   # calls withheld because they never finished
        # Calls whose item ended "incomplete": withheld, and their note waits for the end of the response, which
        # says why (the output token limit or not). Anything the model sends after them settles them first.
        self._held: List[Dict[str, Any]] = []
        self.out_chars = 0
        self.usage_out: Optional[int] = None
        self.usage_in: Optional[int] = None
        self.cache_read = 0
        self.cost = 0.0
        self.stop_reason = "end_turn"
        self.active = False        # any sign the model is working (tokens, reasoning, tool calls)
        self.visible = False       # any visible content seen
        self.failure: Optional[Dict[str, Any]] = None
        self.done = False
        self.response_id = ""
        self._calls: Dict[str, Dict[str, Any]] = {}
        self._order: List[str] = []
        self._by_index: Dict[Any, str] = {}
        self._thinking_item = ""   # reasoning item whose thinking block is open
        self._started = False

    # -- framing -----------------------------------------------------------
    def start(self) -> List[Event]:
        self._started = True
        return [("message_start", {"type": "message_start", "message": {
            "id": new_id("msg_"), "type": "message", "role": "assistant", "content": [], "model": self.model,
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": self.input_tokens, "output_tokens": 1}}})]

    @staticmethod
    def ping() -> Event:
        return ("ping", {"type": "ping"})

    def _close(self, signature: str = "") -> List[Event]:
        ev: List[Event] = []
        if self.open_type is None:
            return ev
        if self.open_type == "thinking":
            ev.append(("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                               "delta": {"type": "signature_delta", "signature": signature}}))
            self._thinking_item = ""
        ev.append(("content_block_stop", {"type": "content_block_stop", "index": self.index}))
        self.open_type = None
        return ev

    def _open(self, kind: str, block: Dict[str, Any]) -> List[Event]:
        ev = self._close()
        self.index += 1
        self.open_type = kind
        ev.append(("content_block_start", {"type": "content_block_start", "index": self.index, "content_block": block}))
        return ev

    def _text(self, text: str) -> List[Event]:
        if not text:
            return []
        ev: List[Event] = []
        if self.open_type != "text":
            ev += self._open("text", {"type": "text", "text": ""})
        self.visible = True
        self.out_chars += len(text)
        ev.append(("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                           "delta": {"type": "text_delta", "text": text}}))
        return ev

    def _thinking(self, text: str, item_id: str) -> List[Event]:
        if not text:
            return []
        self.out_chars += len(text)
        if not self.emit_thinking:
            return []
        ev: List[Event] = []
        if self.open_type != "thinking" or (item_id and self._thinking_item and item_id != self._thinking_item):
            ev += self._open("thinking", {"type": "thinking", "thinking": "", "signature": ""})
            self._thinking_item = item_id
        ev.append(("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                           "delta": {"type": "thinking_delta", "thinking": text}}))
        return ev

    def _reasoning_done(self, item: Dict[str, Any]) -> List[Event]:
        sig = _carry_signature(item, self.model)
        rid = str(item.get("id") or "")
        if self.open_type == "thinking" and (not self._thinking_item or not rid or self._thinking_item == rid):
            return self._close(sig)
        if not sig:
            return []
        # No visible summary was streamed: carry the reasoning in an omitted-display block.
        ev = self._open("thinking", {"type": "thinking", "thinking": "", "signature": ""})
        return ev + self._close(sig)

    # -- tool calls ----------------------------------------------------------
    def _call(self, event: Dict[str, Any], item: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """The buffered call an event belongs to: by item id, else call id, else output index."""
        item = item or {}
        oi = event.get("output_index")
        if not isinstance(oi, (int, str)):
            oi = None                 # a list or an object cannot index anything
        key = str(event.get("item_id") or item.get("id") or item.get("call_id") or "")
        if key not in self._calls:
            if oi is not None and oi in self._by_index:
                key = self._by_index[oi]
            elif not key:
                key = "idx-%s" % (oi if oi is not None else len(self._order))
        call = self._calls.get(key)
        if call is None:
            call = {"name": "", "args": "", "emitted": False}
            self._calls[key] = call
            self._order.append(key)
        if oi is not None:
            self._by_index.setdefault(oi, key)
        return call

    def _emit_call(self, call: Dict[str, Any], complete: bool = True, limit: bool = False) -> List[Event]:
        """One finished function call -> one tool_use block.

        A call that did not finish (the stream was cut, the output budget ran out, the item is
        "incomplete") or whose arguments are not complete JSON is NEVER handed to Claude Code as
        a tool_use: repairing `rm -rf ./build/cache && ...` into `rm -rf ./build` and running it is
        worse than not running anything. It becomes a visible note instead (`limit`: the output
        cap stopped it).
        """
        if call["emitted"]:
            return []
        call["emitted"] = True
        name = _fix_name(call["name"] or "unknown_tool", self.names)
        args = strict_arguments(call["args"]) if complete else None
        if args is None:
            self.cut_calls.append(name)
            return self._text(("\n\n" if self.visible else "") + cut_note(name, limit=limit))
        ev = self._open("tool_use", {"type": "tool_use", "id": new_id("toolu_"), "name": name, "input": {}})
        ev.append(("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                           "delta": {"type": "input_json_delta", "partial_json": _dumps(args)}}))
        ev += self._close()
        self.tools_emitted += 1
        return ev

    def _settle_held(self) -> List[Event]:
        """Notes for incomplete calls the model went on after: the reply did not end inside them."""
        ev: List[Event] = []
        for call in self._held:
            ev += self._emit_call(call, complete=False)
        self._held = []
        return ev

    # -- feeding -------------------------------------------------------------
    def feed(self, event: Dict[str, Any]) -> List[Event]:
        if not isinstance(event, dict):
            return []
        kind = str(event.get("type") or "")
        if self._held and kind in _MORE_OUTPUT:
            return self._settle_held() + self._feed(event, kind)
        return self._feed(event, kind)

    def _feed(self, event: Dict[str, Any], kind: str) -> List[Event]:
        if kind in ("response.created", "response.in_progress", "response.queued"):
            self.response_id = str(_obj(event.get("response")).get("id") or self.response_id)
            return []
        if kind in ("response.output_text.delta", "response.refusal.delta"):
            self.active = True
            return self._text(str(event.get("delta") or ""))
        if kind in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
            self.active = True
            return self._thinking(str(event.get("delta") or ""), str(event.get("item_id") or ""))
        if kind == "response.reasoning_summary_part.added":
            if (_int(event.get("summary_index")) or 0) > 0 and self.open_type == "thinking" and self.emit_thinking:
                return self._thinking("\n\n", str(event.get("item_id") or ""))
            return []
        if kind == "response.output_item.added":
            item = _obj(event.get("item"))
            if item.get("type") == "function_call":
                self.active = True
                call = self._call(event, item)
                call["name"] = call["name"] or str(item.get("name") or "")
                if isinstance(item.get("arguments"), str):
                    call["args"] = item["arguments"] or call["args"]
            elif item.get("type") == "reasoning":
                self.active = True          # the model is thinking; first-token window no longer applies
            return []
        if kind == "response.function_call_arguments.delta":
            self.active = True
            call = self._call(event)
            delta = event.get("delta")
            call["args"] += delta if isinstance(delta, str) else _dumps(delta)
            return []
        if kind == "response.function_call_arguments.done":
            call = self._call(event)
            if isinstance(event.get("arguments"), str) and event["arguments"]:
                call["args"] = event["arguments"]
            if event.get("name") and not call["name"]:
                call["name"] = str(event["name"])
            return []
        if kind == "response.output_item.done":
            item = _obj(event.get("item"))
            if item.get("type") == "function_call":
                call = self._call(event, item)
                call["name"] = call["name"] or str(item.get("name") or "")
                if isinstance(item.get("arguments"), str) and item["arguments"]:
                    call["args"] = item["arguments"]
                self.out_chars += len(call["args"])
                if item.get("status") in (None, "completed"):
                    return self._emit_call(call)
                if not call["emitted"] and not any(c is call for c in self._held):
                    self._held.append(call)   # never run; the next output, or finish() at the end, says why
                return []
            if item.get("type") == "reasoning":
                return self._reasoning_done(item)
            return []
        if kind in ("response.completed", "response.incomplete", "response.failed"):
            self.done = True
            resp = _obj(event.get("response"))
            self._usage(_obj(resp.get("usage")))
            if kind == "response.incomplete" or resp.get("status") == "incomplete":
                reason = _obj(resp.get("incomplete_details")).get("reason")
                self.stop_reason = "max_tokens" if reason == "max_output_tokens" else "end_turn"
            if kind == "response.failed":
                self._fail(event)
            return []
        if kind == "error":
            self.done = True
            self._fail(event)
            return []
        return []

    def _usage(self, usage: Dict[str, Any]) -> None:
        tokens_in = _int(usage.get("input_tokens"))
        if tokens_in is not None:
            cached = _int(_obj(usage.get("input_tokens_details")).get("cached_tokens")) or 0
            self.cache_read = cached
            self.usage_in = max(0, tokens_in - cached)
        tokens_out = _int(usage.get("output_tokens"))
        if tokens_out is not None:
            self.usage_out = tokens_out

    def _fail(self, event: Dict[str, Any]) -> None:
        found = error_of(event)
        if found is None:
            return
        status, detail = found
        err = json.loads(detail)["error"]
        self.failure = {"status": status, "code": err.get("code", ""), "message": err.get("message", ""),
                        "detail": detail}

    def finish(self, notice: Optional[str] = None) -> List[Event]:
        ev: List[Event] = []
        limit = self.stop_reason == "max_tokens"
        cut_before = len(self.cut_calls)
        for key in self._order:
            call = self._calls[key]
            if not call["emitted"] and (call["name"] or call["args"]):
                ev += self._emit_call(call, complete=False, limit=limit)   # never finished: withheld, never run
        # The output cap cut only a tool call (withheld above, with a note): see translate.final_stop.
        limit_cut_tool = limit and len(self.cut_calls) > cut_before
        if notice is None and self.failure is not None:
            notice = "The provider reported an error: %s" % (_provider_error(self.failure.get("message"))
                                                              or "unknown error")
        if notice:
            if self.open_type == "text":
                ev += self._text("\n\n" + notice)
            else:
                ev += self._text(notice)
        if self.index < 0:
            ev += self._open("text", {"type": "text", "text": ""})
        ev += self._close()
        stop = final_stop(self.stop_reason, self.tools_emitted, bool(notice), limit_cut_tool)
        usage: Dict[str, int] = {"output_tokens": self.usage_out if self.usage_out is not None else max(1, self.out_chars // 4)}
        if self.usage_in is not None:
            usage["input_tokens"] = self.usage_in
        if self.cache_read:
            usage["cache_read_input_tokens"] = self.cache_read
        ev.append(("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                                     "usage": usage}))
        ev.append(("message_stop", {"type": "message_stop"}))
        return ev


def strip_carried(body: Dict[str, Any]) -> Dict[str, Any]:
    """Copy of an Anthropic request without thinking blocks an Anthropic upstream would reject.

    Removes thinking blocks whose signature is empty or one of ours (CARRY_PREFIX).
    Use before relaying a transcript that went through an adapter to a native target.
    """
    out = dict(body)
    msgs = []
    for m in body.get("messages") or []:
        if isinstance(m, dict) and m.get("role") == "assistant" and isinstance(m.get("content"), list):
            kept = [b for b in m["content"] if not (isinstance(b, dict) and b.get("type") == "thinking" and (
                not b.get("signature") or str(b.get("signature")).startswith(CARRY_PREFIX)))]
            m = dict(m, content=kept or [{"type": "text", "text": "(thinking omitted)"}])
        msgs.append(m)
    out["messages"] = msgs
    return out
