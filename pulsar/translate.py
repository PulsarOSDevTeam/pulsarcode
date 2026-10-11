# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Anthropic Messages <-> OpenAI chat-completions translation.

Pure functions plus one streaming state machine. No I/O, so every edge
case is unit-testable without a network.

Reliability layer for open models:

* Ids. Every tool_use block handed to Claude Code carries a freshly minted
  id. Some hosts reuse ids ("call_0" on every turn) and Claude Code then
  marks the repeat as interrupted and loops; upstream ids only tell
  parallel calls apart inside one response.
* Names. An unknown tool name is mapped to the real one when it differs
  only by case or separators, a "functions." style prefix, a wrong MCP
  server prefix, a well-known alias, or a unique fuzzy match.
* Arguments. At block close the arguments are repaired (fenced,
  double-encoded or concatenated JSON), unwrapped
  ({"arguments": {...}}), renamed (filePath -> file_path) and coerced to
  the tool's JSON schema ("5" -> 5, "true" -> true, "[..]" -> list).
* Rescue. Tool calls a host returned as text (Hermes/Qwen <tool_call>,
  Qwen3-Coder and Llama <function=...>, Mistral [TOOL_CALLS], Kimi,
  DeepSeek, GLM, MiniMax tags, or a bare JSON call that is the whole
  message) become real tool_use blocks, but only where a call can be: the
  reply opens with it, or nothing but whitespace and further calls follows
  it. Markup quoted inside prose (a README, a fetched page) stays text,
  exactly as written. Reasoning is searched only when the caller asks, and
  replies of models that always call tools natively only when the caller
  says the host needs it (StreamTranslator rescue, rescue_reasoning).
* Usage. Cached prompt tokens and per-request cost are reported.
* Healing. Hosts without native tool calling get the tools in the prompt
  (and their text replies are rescued); Mistral-family id and role rules,
  missing system roles and strictly alternating templates are handled.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

Event = Tuple[str, Dict[str, Any]]
Call = Tuple[str, Dict[str, Any]]


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:24]


_B62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def mistral_id(value: str) -> str:
    """A stable 9-character [A-Za-z0-9] id (Mistral-family validators demand exactly that)."""
    value = str(value or "")
    if re.fullmatch(r"[A-Za-z0-9]{9}", value):
        return value
    import hashlib
    n = int(hashlib.sha1(value.encode("utf-8", "replace")).hexdigest(), 16)
    out = []
    for _ in range(9):
        n, r = divmod(n, 62)
        out.append(_B62[r])
    return "".join(out)


def _int(value: Any) -> Optional[int]:
    """A count from an upstream field, or None. Never raises: json.loads accepts Infinity and NaN, gateways send
    figures as strings, and a superscript digit passes str.isdigit() although int() refuses it."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float) and math.isfinite(value):
        return max(0, int(value))
    if isinstance(value, str):
        s = value.strip()
        if s.isdecimal() and len(s) <= 18:
            return int(s)
    return None


def _float(value: Any) -> Optional[float]:
    """A finite number from an upstream field, or None (never raises)."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        out = float(value)
    except (ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


# ---------------------------------------------------------------------------
# Request: Anthropic -> OpenAI
# ---------------------------------------------------------------------------

def system_text(system: Any) -> str:
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n\n".join(str(b.get("text", "")) for b in system
                           if isinstance(b, dict) and b.get("type") == "text" and b.get("text"))
    return ""


def _cc(block: Any) -> Optional[Dict[str, Any]]:
    """An Anthropic cache_control marker, normalized for OpenRouter (ephemeral, optional ttl)."""
    cc = block.get("cache_control") if isinstance(block, dict) else None
    if not isinstance(cc, dict):
        return None
    out: Dict[str, Any] = {"type": "ephemeral"}
    if cc.get("ttl") in ("5m", "1h"):
        out["ttl"] = cc["ttl"]
    return out


def _flat(content: Any) -> str:
    """Any OpenAI message content as plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") == "text":
                    parts.append(str(p.get("text", "")))
                elif p.get("type") == "image_url":
                    parts.append("[image]")
            elif isinstance(p, str):
                parts.append(p)
        return "\n\n".join(x for x in parts if x)
    return str(content)


def _append_text(msg: Dict[str, Any], text: str) -> None:
    c = msg.get("content")
    if isinstance(c, list):
        msg["content"] = c + [{"type": "text", "text": text}]
    else:
        msg["content"] = (c + "\n\n" + text) if c else text


def _image_url(block: Dict[str, Any]) -> Optional[str]:
    src = block.get("source")
    if not isinstance(src, dict):          # absent, or malformed (a string): nothing to forward
        return None
    if src.get("type") == "base64" and src.get("data"):
        return "data:%s;base64,%s" % (src.get("media_type") or "image/png", src["data"])
    if src.get("type") == "url" and src.get("url"):
        return str(src["url"])
    return None


def _result_images(content: Any) -> List[str]:
    """data: or https URLs of the images inside a tool_result."""
    if not isinstance(content, list):
        return []
    return [u for u in (_image_url(b) for b in content if isinstance(b, dict) and b.get("type") == "image") if u]


def _result_text(content: Any, images_follow: bool = False) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(str(b.get("text", "")))
                elif b.get("type") == "image":
                    parts.append("[image: attached in the next message]" if images_follow
                                 else "[image omitted: this model cannot view tool images]")
                elif b.get("type") == "document":
                    parts.append("[document omitted]")
                else:
                    parts.append(_dumps(b))
            elif b is not None:
                parts.append(str(b))
        return "\n".join(p for p in parts if p)
    return _dumps(content)


def _user_parts(blocks: List[Any], vision: bool, cache: bool = False) -> Any:
    parts: List[Dict[str, Any]] = []
    for b in blocks:
        if not isinstance(b, dict):
            parts.append({"type": "text", "text": str(b)})
            continue
        t = b.get("type")
        if t == "text":
            if b.get("text"):
                parts.append({"type": "text", "text": str(b["text"])})
        elif t == "image":
            url = _image_url(b)
            if url and vision:
                parts.append({"type": "image_url", "image_url": {"url": url}})
            else:
                parts.append({"type": "text", "text": "[image attached; not forwarded to this model]"})
        elif t == "document":
            src = b.get("source") if isinstance(b.get("source"), dict) else {}
            title = b.get("title") or src.get("media_type") or "document"
            if src.get("type") == "text" and src.get("data"):
                parts.append({"type": "text", "text": "[document: %s]\n%s" % (title, src["data"])})
            else:
                parts.append({"type": "text", "text": "[document omitted: %s]" % title})
        elif t in ("thinking", "redacted_thinking", "tool_result"):
            continue
        else:
            parts.append({"type": "text", "text": _dumps(b)})
        cc = _cc(b) if cache else None
        if cc:
            # OpenRouter honours breakpoints on text parts only: pin it to the nearest one.
            for p in reversed(parts):
                if p["type"] == "text":
                    p["cache_control"] = cc
                    break
    if not parts:
        return None
    if all(p["type"] == "text" and "cache_control" not in p for p in parts):
        return "\n\n".join(p["text"] for p in parts)
    return parts


class _Ids:
    """Tool-call ids as sent upstream: duplicates made unique, optional Mistral 9-char form."""

    def __init__(self, style: str) -> None:
        self.style = style
        self.used: set = set()
        self.turn: Dict[str, List[str]] = {}

    def new_turn(self) -> None:
        self.turn = {}

    def call(self, oid: str) -> str:
        oid = oid or new_id("toolu_")
        nid, k = oid, 2
        while nid in self.used:
            nid, k = "%s_%d" % (oid, k), k + 1
        self.used.add(nid)
        self.turn.setdefault(oid, []).append(nid)
        return self.wire(nid)

    def result(self, oid: str) -> str:
        q = self.turn.get(oid)
        nid = (q.pop(0) if len(q) > 1 else q[0]) if q else oid
        return self.wire(nid)

    def wire(self, nid: str) -> str:
        return mistral_id(nid) if self.style == "mistral9" else nid


def _system_message(system: Any, cache: bool) -> Optional[Dict[str, Any]]:
    if cache and isinstance(system, list) and any(_cc(b) for b in system):
        parts: List[Dict[str, Any]] = []
        for b in system:
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                part: Dict[str, Any] = {"type": "text", "text": str(b["text"])}
                cc = _cc(b)
                if cc:
                    part["cache_control"] = cc
                parts.append(part)
        return {"role": "system", "content": parts} if parts else None
    text = system_text(system)
    return {"role": "system", "content": text} if text else None


def _calls_as_text(text: str, calls: List[Tuple[str, Any]]) -> str:
    blocks = ["<tool_call>\n%s\n</tool_call>" % _dumps({"name": n, "arguments": a if isinstance(a, dict) else {}})
              for n, a in calls]
    return (text + "\n\n" if text else "") + "\n".join(blocks)


def messages_to_openai(body: Dict[str, Any], vision: bool = True, *, cache: bool = False,
                       tool_id_style: str = "default", tools_as_text: bool = False) -> List[Dict[str, Any]]:
    """Anthropic messages -> OpenAI chat messages.

    cache keeps cache_control markers as OpenRouter content-part breakpoints;
    tool_id_style "mistral9" rewrites tool-call ids to 9 alphanumerics;
    tools_as_text renders tool calls and results as Hermes-style text for
    hosts that have no native tool calling.
    """
    out: List[Dict[str, Any]] = []
    ids = _Ids(tool_id_style)
    sys_msg = _system_message(body.get("system"), cache)
    if sys_msg:
        out.append(sys_msg)

    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or "user"
        content = msg.get("content")
        if role == "system":
            text = content if isinstance(content, str) else _result_text(content)
            if not text:
                continue
            if all(m["role"] == "system" for m in out):
                if out:
                    _append_text(out[0], text)
                else:
                    out.append({"role": "system", "content": text})
            else:
                # Mid-conversation system notes: many OpenAI-dialect servers reject a
                # late system role, so carry them as a tagged user-side note in place.
                out.append({"role": "user", "content": "<system-reminder>\n%s\n</system-reminder>" % text,
                            "_soft": True})
            continue
        if role not in ("user", "assistant"):
            role = "user"
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            out.append({"role": role, "content": _result_text(content)})
            continue

        if role == "assistant":
            ids.new_turn()
            texts: List[str] = []
            calls: List[Dict[str, Any]] = []
            raw_calls: List[Tuple[str, Any]] = []
            marker: Optional[Dict[str, Any]] = None
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text"):
                    texts.append(str(b["text"]))
                elif b.get("type") in ("tool_use", "server_tool_use"):
                    name = str(b.get("name", ""))
                    raw_calls.append((name, b.get("input") or {}))
                    calls.append({
                        "id": ids.call(str(b.get("id") or "")),
                        "type": "function",
                        "function": {"name": wire_name(name), "arguments": _dumps(b.get("input") or {})},
                    })
                else:
                    continue
                if cache and _cc(b):
                    marker = _cc(b)
            text = "".join(texts)
            if tools_as_text and raw_calls:
                text, calls = _calls_as_text(text, raw_calls), []
            item: Dict[str, Any] = {"role": "assistant", "content": text or None}
            if marker and text:
                item["content"] = [{"type": "text", "text": text, "cache_control": marker}]
            if calls:
                item["tool_calls"] = calls
            elif item["content"] is None:
                item["content"] = ""
            out.append(item)
            continue

        # user turn: tool results first (they must follow the tool calls),
        # then any free text or images the user added alongside them. Tool messages cannot carry
        # images on the chat wire, so a vision model gets a tool result's screenshots right after.
        result_images: List[Dict[str, Any]] = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                urls = _result_images(b.get("content")) if vision and not tools_as_text else []
                result_images += [{"type": "image_url", "image_url": {"url": u}} for u in urls]
                text = _result_text(b.get("content"), images_follow=bool(urls))
                if b.get("is_error"):
                    text = "Error: " + (text or "tool failed")
                text = text or "(no output)"
                if tools_as_text:
                    out.append({"role": "user", "content": "<tool_response>\n%s\n</tool_response>" % text})
                    continue
                cc = None
                if cache:
                    inner = b.get("content") if isinstance(b.get("content"), list) else []
                    cc = _cc(b) or next((_cc(x) for x in inner if _cc(x)), None)
                out.append({"role": "tool", "tool_call_id": ids.result(str(b.get("tool_use_id", ""))),
                            "content": [{"type": "text", "text": text, "cache_control": cc}] if cc else text})
        rest = _user_parts([b for b in content if not (isinstance(b, dict) and b.get("type") == "tool_result")],
                           vision, cache)
        if result_images:
            lead = [{"type": "text", "text": "Images from the tool results above:"}] + result_images
            if rest is None:
                rest = lead
            elif isinstance(rest, str):
                rest = lead + [{"type": "text", "text": rest}]
            else:
                rest = lead + list(rest)
        if rest is not None:
            out.append({"role": "user", "content": rest})

    return repair_sequence(out)


def repair_sequence(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Make the transcript legal for strict OpenAI-style validators.

    * every assistant tool_call gets a tool reply before the next turn
      (interrupted tool runs leave holes that upstream rejects with 400)
    * tool replies without a matching call become plain user text
    * consecutive plain-text user turns merge into one
    """
    fixed: List[Dict[str, Any]] = []
    pending: List[str] = []
    open_ids: set = set()
    deferred: List[Dict[str, Any]] = []

    def push(m: Dict[str, Any]) -> None:
        m = {k: v for k, v in m.items() if k != "_soft"}
        if (m.get("role") == "user" and fixed and fixed[-1].get("role") == "user"
                and isinstance(m.get("content"), str) and isinstance(fixed[-1].get("content"), str)):
            fixed[-1] = {"role": "user", "content": fixed[-1]["content"] + "\n\n" + m["content"]}
        else:
            fixed.append(m)

    def close_pending() -> None:
        for cid in pending:
            fixed.append({"role": "tool", "tool_call_id": cid, "content": "(no result: the tool call was interrupted)"})
        pending.clear()
        while deferred:
            push(deferred.pop(0))

    for m in msgs:
        role = m.get("role")
        if m.get("_soft") and pending:
            deferred.append(m)  # never split a tool call from its results
            continue
        if role == "tool":
            cid = m.get("tool_call_id", "")
            if cid in pending:
                pending.remove(cid)
                fixed.append(m)
                if not pending:
                    close_pending()
            elif cid in open_ids:
                continue  # duplicate result for an id we already answered
            else:
                push({"role": "user", "content": "Tool result (%s):\n%s" % (cid or "unknown", _flat(m.get("content")))})
            continue
        close_pending()
        push(m)
        if role == "assistant" and m.get("tool_calls"):
            ids = [c["id"] for c in m["tool_calls"]]
            pending.extend(ids)
            open_ids.update(ids)
    close_pending()
    if not any(m.get("role") != "system" for m in fixed):
        fixed.append({"role": "user", "content": "(empty)"})
    return fixed


def _fold_after_tool(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Mistral-family validators reject a user turn right after tool results: fold it into the last result."""
    out: List[Dict[str, Any]] = []
    for m in msgs:
        if m.get("role") == "user" and out and out[-1].get("role") == "tool":
            prev = dict(out[-1])
            prev["content"] = _flat(prev.get("content")) + "\n\n" + _flat(m.get("content"))
            out[-1] = prev
            continue
        out.append(m)
    return out


def _inline_system(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """For templates without a system role: carry the system text in the first user turn."""
    sys_text = "\n\n".join(_flat(m.get("content")) for m in msgs if m.get("role") == "system")
    rest = [m for m in msgs if m.get("role") != "system"]
    if not sys_text:
        return rest
    for i, m in enumerate(rest):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):
                rest[i] = dict(m, content=[{"type": "text", "text": sys_text}] + c)
            else:
                rest[i] = dict(m, content=sys_text + "\n\n" + (c or ""))
            return rest
    return [{"role": "user", "content": sys_text}] + rest


def _alternate(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """For templates that insist on strict user/assistant alternation (system turns stay in front)."""
    head = [m for m in msgs if m.get("role") == "system"]
    out: List[Dict[str, Any]] = []
    for m in msgs:
        role = m.get("role")
        content: Any = m.get("content")
        if role == "system":
            continue
        if role == "tool":
            role, content = "user", "<tool_response>\n%s\n</tool_response>" % _flat(content)
        elif role != "assistant":
            role = "user"
        if out and out[-1]["role"] == role:
            out[-1] = {"role": role, "content": _flat(out[-1]["content"]) + "\n\n" + _flat(content)}
        else:
            out.append({"role": role, "content": content if content is not None else ""})
    if not out or out[0]["role"] == "assistant":
        out.insert(0, {"role": "user", "content": "(continue)"})
    return head + out


def _cap_breakpoints(msgs: List[Dict[str, Any]], limit: int = 4) -> None:
    """Anthropic accepts at most four cache breakpoints: keep the first and the latest ones."""
    spots = [p for m in msgs if isinstance(m.get("content"), list)
             for p in m["content"] if isinstance(p, dict) and "cache_control" in p]
    if len(spots) <= limit:
        return
    keep = {id(p) for p in [spots[0]] + spots[-(limit - 1):]}
    for p in spots:
        if id(p) not in keep:
            del p["cache_control"]


_STRICT_DROP = {"$schema", "additionalProperties", "propertyNames", "patternProperties", "$id", "$ref",
                "$defs", "definitions", "exclusiveMinimum", "exclusiveMaximum", "const", "examples",
                "default", "unevaluatedProperties", "contentEncoding", "contentMediaType", "if", "then", "else"}


def sanitize_schema(schema: Any, strict: bool = False) -> Any:
    if isinstance(schema, list):
        return [sanitize_schema(s, strict) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out = {}
    for k, v in schema.items():
        if k == "$schema":
            continue
        if strict and k in _STRICT_DROP:
            continue
        if strict and k == "format" and v not in ("enum", "date-time"):
            continue
        if k == "properties" and isinstance(v, dict):
            out[k] = {pk: sanitize_schema(pv, strict) for pk, pv in v.items()}
        else:
            out[k] = sanitize_schema(v, strict)
    if strict and isinstance(out.get("type"), list):
        types = [t for t in out["type"] if t != "null"]
        out["type"] = types[0] if types else "string"
    return out


_WIRE_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def wire_name(name: str) -> str:
    """The function name sent upstream: the tool's own name when OpenAI-compatible hosts accept it,
    else a stable alias (prefix + hash) so long MCP names never collide when cut to 64 characters.
    Replies naming the alias are mapped back by ToolIndex."""
    name = str(name or "")
    if _WIRE_OK.match(name):
        return name
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", name)[:52]
    import hashlib
    return safe + "_" + hashlib.sha1(name.encode("utf-8", "replace")).hexdigest()[:10]


def tools_to_openai(tools: Any, strict: bool = False) -> List[Dict[str, Any]]:
    out = []
    for t in tools or []:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        if t.get("type") and t["type"] not in ("custom",) and not t.get("input_schema"):
            continue  # server tools (web_search_20250305, bash_2024...) have no portable form
        params = sanitize_schema(t.get("input_schema") or {"type": "object", "properties": {}}, strict)
        if params.get("type") != "object":
            params = {"type": "object", "properties": {}}
        out.append({"type": "function", "function": {
            "name": wire_name(str(t["name"])),
            "description": str(t.get("description") or "")[:8000],
            "parameters": params,
        }})
    return out


def tool_choice_to_openai(choice: Any) -> Any:
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
        return {"type": "function", "function": {"name": wire_name(str(choice["name"]))}}
    return None


def tools_prompt(tools: List[Dict[str, Any]], choice: Any = None) -> str:
    """Tool definitions as Hermes-style prompt text, for hosts without native tool calling."""
    text = ("# Tools\n\nYou may call one or more functions to assist with the user query.\n\n"
            "You are provided with function signatures within <tools></tools> XML tags:\n<tools>\n"
            + "\n".join(_dumps(t) for t in tools) + "\n</tools>\n\n"
            "For each function call, return a json object with function name and arguments within "
            "<tool_call></tool_call> XML tags:\n<tool_call>\n"
            "{\"name\": <function-name>, \"arguments\": <args-json-object>}\n</tool_call>\n\n"
            "Function results come back inside <tool_response></tool_response> tags.")
    t = choice.get("type") if isinstance(choice, dict) else None
    if t == "any":
        text += "\n\nYou must call at least one function in this reply."
    elif t == "tool" and choice.get("name"):
        text += "\n\nYou must call the function %s in this reply." % choice["name"]
    elif t == "none":
        text += "\n\nDo not call any function in this reply."
    return text


def uses_completion_tokens(provider_id: str, model: str) -> bool:
    low = model.lower().split("/")[-1]
    return provider_id in ("openai", "custom") and bool(re.match(r"^(o\d|gpt-5)", low))


def wants_cache(provider_id: str, model: str) -> bool:
    """Hosts that honour content-part cache_control breakpoints."""
    return provider_id == "openrouter" and (model or "").lower().startswith(("anthropic/", "google/"))


def build_request(body: Dict[str, Any], model: str, provider_id: str, *, stream: bool,
                  max_output: int = 0, usage_in_stream: bool = False, strict_schema: bool = False,
                  heal: Optional[Dict[str, Any]] = None, vision: bool = True, cache: Optional[bool] = None,
                  tool_id_style: str = "default") -> Dict[str, Any]:
    """Anthropic Messages request -> OpenAI chat-completions request.

    cache None decides by host (OpenRouter anthropic/ and google/ models keep
    their cache breakpoints); True or False forces it. metadata, top_k and
    the Anthropic betas are never forwarded.
    """
    heal = heal or {}
    tools = tools_to_openai(body.get("tools"), strict=strict_schema or bool(heal.get("strict_schema")))
    send_tools = bool(tools) and not heal.get("drop_tools")
    alternate = bool(heal.get("alternate"))
    as_text = alternate or bool(heal.get("prompt_tools"))
    style = str(heal.get("tool_id_style") or tool_id_style or "default")
    use_cache = (wants_cache(provider_id, model) if cache is None else bool(cache)) and not heal.get("drop_cache")

    msgs = messages_to_openai(body, vision=vision, cache=use_cache, tool_id_style=style, tools_as_text=as_text)
    if as_text and send_tools:
        prompt = tools_prompt(tools, body.get("tool_choice"))
        if msgs and msgs[0].get("role") == "system":
            _append_text(msgs[0], prompt)
        else:
            msgs.insert(0, {"role": "system", "content": prompt})
    if heal.get("fold_after_tool") or style == "mistral9":
        msgs = _fold_after_tool(msgs)
    if alternate:
        msgs = _alternate(msgs)
    if heal.get("no_system") or alternate:
        msgs = _inline_system(msgs)
    if use_cache:
        _cap_breakpoints(msgs)
    req: Dict[str, Any] = {"model": model, "messages": msgs, "stream": stream}

    max_tokens = int(body.get("max_tokens") or 4096)
    for cap in (max_output, int(heal.get("max_tokens_cap") or 0)):
        if cap:
            max_tokens = min(max_tokens, cap)
    if heal.get("max_completion_tokens") or uses_completion_tokens(provider_id, model):
        req["max_completion_tokens"] = max_tokens
    elif not heal.get("drop_max_tokens"):
        req["max_tokens"] = max_tokens

    reasoning_family = uses_completion_tokens(provider_id, model)
    if "temperature" in body and not heal.get("drop_temperature") and not reasoning_family:
        req["temperature"] = float(body["temperature"])
    if "top_p" in body and not heal.get("drop_top_p") and not reasoning_family:
        req["top_p"] = float(body["top_p"])
    if body.get("stop_sequences") and not heal.get("drop_stop"):
        req["stop"] = list(body["stop_sequences"])[:4]

    if send_tools and not as_text:
        req["tools"] = tools
        tc = body.get("tool_choice")
        choice = tool_choice_to_openai(tc)
        if choice is not None and not heal.get("drop_tool_choice"):
            req["tool_choice"] = choice
        if (isinstance(tc, dict) and tc.get("disable_parallel_tool_use") and tc.get("type") != "none"
                and not heal.get("drop_parallel_tool_calls")):
            req["parallel_tool_calls"] = False
    thinking = body.get("thinking")
    if (provider_id == "openrouter" and isinstance(thinking, dict) and thinking.get("type") in ("enabled", "adaptive")
            and not (model or "").lower().startswith("anthropic/") and not heal.get("drop_reasoning")):
        # Anthropic models are left alone: their signed thinking cannot be carried back through chat-completions.
        req["reasoning"] = {"enabled": True}
    if stream and usage_in_stream and not heal.get("drop_stream_options"):
        req["stream_options"] = {"include_usage": True}
    return req


_NO_TOOLS_RE = re.compile(
    r"(?:does not|doesn't|do not|cannot|can't) (?:support|use) (?:tools?|tool use|tool calling|tool calls|"
    r"function calling|function calls)\b|tools? (?:are|is) not supported|not supported (?:for|with) tools|"
    r"tool(?:s| use| calling| calls)? (?:is |are )?not (?:supported|enabled|available)|"
    r"function calling is not (?:supported|enabled|available)|"
    r"enable-auto-tool-choice|tool-call-parser|--jinja")
_ID9_RE = re.compile(r"tool[_ ]?call[_ ]?id.{0,120}?(?:length of 9|9 char|a-z, ?a-z, ?0-9)", re.S)
_AFTER_TOOL_RE = re.compile(r"unexpected role .{0,4}user.{0,4} after role .{0,4}tool")
_ALTERNATE_RE = re.compile(r"roles must alternate|must alternate (?:between )?user|alternate user/assistant")
_NO_SYSTEM_RE = re.compile(r"system (?:role|message|prompt)s? (?:is |are )?not supported|"
                           r"does not support (?:the )?system|system role not")


_LIMIT_PHRASE = re.compile(r"(?:at most|<=|less than or equal to|no more than|not exceed|maximum(?: value)?(?: of| is| allowed(?: is)?)?"
                           r"|max(?:imum)? (?:of|is)|limit(?: of| is)?|up to|\[\s*\d+\s*,)\s*:?\s*(\d{3,7})")


def output_cap(text: str, current: int) -> int:
    """The output-token cap to retry with after an upstream rejected `current` (both OpenAI wires use it).

    Never reads a limit out of a model id ("qwen3-coder-480b-a35b" is not a cap of 480, "o4-mini-2025-04-16"
    not one of 2025): every token that mixes letters and digits is dropped, then numbers that follow a limit
    phrase win. Without a usable number, a smaller budget.
    """
    plain = re.sub(r"\S*[a-z]\S*\d\S*|\S*\d\S*[a-z]\S*", " ", (text or "").lower())
    said = [int(n) for n in _LIMIT_PHRASE.findall(plain)]
    nums = said or [int(n) for n in re.findall(r"(?<![\d.])(\d{3,7})(?![\d.])", plain)]
    limit = min([n for n in nums if 256 <= n < current], default=0)
    return limit or max(1024, current // 2 if current > 8192 else 4096 if current > 4096 else current // 2)


def heal_from_error(status: int, text: str, req: Dict[str, Any], heal: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Given an upstream 400/404/422, return adjusted heal flags or None if hopeless."""
    if status not in (400, 404, 422):
        return None
    low = (text or "").lower()
    new = dict(heal)

    def changed() -> Optional[Dict[str, Any]]:
        return new if new != heal else None

    if "tools" in req and _NO_TOOLS_RE.search(low):
        new["prompt_tools"] = True          # no native tool calling here: describe the tools in the prompt
        return changed()
    if _ID9_RE.search(low):
        new["tool_id_style"] = "mistral9"
        return changed()
    if _AFTER_TOOL_RE.search(low):
        new["fold_after_tool"] = True
        return changed()
    if _ALTERNATE_RE.search(low):
        new["alternate"] = True
        return changed()
    if _NO_SYSTEM_RE.search(low):
        new["no_system"] = True
        return changed()
    if "cache_control" in low:
        new["drop_cache"] = True
        return changed()
    if "parallel_tool_calls" in low and "parallel_tool_calls" in req:
        new["drop_parallel_tool_calls"] = True
        return changed()
    if "reasoning" in req and re.search(r"reasoning|thinking", low):
        new["drop_reasoning"] = True
        return changed()
    if "max_completion_tokens" in low and "max_tokens" in low and ("unsupported" in low or "not supported" in low):
        new["max_completion_tokens"] = True
        return changed()
    if re.search(r"max_(completion_)?tokens|max_output|maximum output|output tokens|max_new_tokens", low):
        current = int(req.get("max_tokens") or req.get("max_completion_tokens") or 4096)
        new["max_tokens_cap"] = output_cap(low, current)
        return changed()
    if "stream_options" in low or "include_usage" in low:
        new["drop_stream_options"] = True
        return changed()
    if "temperature" in low:
        new["drop_temperature"] = True
        return changed()
    if "top_p" in low:
        new["drop_top_p"] = True
        return changed()
    if "tool_choice" in low:
        new["drop_tool_choice"] = True
        return changed()
    if re.search(r"\bstop\b", low) and "stop" in req:
        new["drop_stop"] = True
        return changed()
    if ("schema" in low or "parameters" in low or "function" in low) and "tools" in req and not heal.get("strict_schema"):
        new["strict_schema"] = True
        return changed()
    if re.search(r"image|vision|multimodal|image_url", low) and not heal.get("no_vision"):
        new["no_vision"] = True
        return changed()
    return None


_OVERFLOW_RE = re.compile(
    r"context[_ ]length[_ ]exceeded|context_window_exceeded|maximum context length|context window|"
    r"context (?:length|size|limit)|prompt is too long|prompt too long|input is too long|too many (?:input )?tokens|"
    r"reduce the length|exceeds? (?:the )?(?:model'?s? )?(?:maximum |max |available )?(?:context|token limit|input)|"
    r"exceed_context_size|input token count|maximum prompt length|model token limit|prompt exceeds|"
    r"max_input_tokens|inputs?.? tokens \+ .?max_new_tokens|too large for model with", re.I)
_RATE_RE = re.compile(r"per (?:minute|hour|day)|rate.?limit|\b[tr]pm\b|quota|tokens per", re.I)
_CTX_LIMIT_RE = re.compile(r"(?:maximum context length|context length|context window|maximum prompt length|"
                           r"must be <=)\D{0,24}?(\d{3,8})", re.I)
_CTX_USED_RE = re.compile(r"(\d{2,8}) (?:tokens )?in the messages|given: (\d{2,8}) .?inputs.? tokens|"
                          r"(\d{2,8}) input tokens", re.I)


def context_overflow(status: int, text: str) -> bool:
    """True when an upstream error means the prompt itself no longer fits the model's context.

    Rate limits (tokens per minute) are not overflows, and "N in the
    messages, M in the completion" errors whose prompt fits are not either:
    a smaller max_tokens fixes those.
    """
    if status not in (400, 413, 422, 500):
        return False
    t = text or ""
    if not _OVERFLOW_RE.search(t) or _RATE_RE.search(t):
        return False
    lim, used = _CTX_LIMIT_RE.search(t), _CTX_USED_RE.search(t)
    if lim and used:
        if int(next(g for g in used.groups() if g)) < int(lim.group(1)):
            return False
    return True


IMAGE_TOKENS = 1600          # a typical screenshot; providers bill images by pixels, not by base64 length


def _without_media(value: Any, counter: List[int]) -> Any:
    """A copy of message content with base64 media replaced by a stub, counting what was removed."""
    if isinstance(value, list):
        return [_without_media(v, counter) for v in value]
    if isinstance(value, dict):
        src = value.get("source")
        if value.get("type") in ("image", "document") and isinstance(src, dict) and src.get("type") == "base64":
            counter[0] += 1
            return {"type": value.get("type")}
        if "content" in value and isinstance(value["content"], list):
            return dict(value, content=_without_media(value["content"], counter))
        return value
    return value


def estimate_tokens(body: Dict[str, Any]) -> int:
    media = [0]
    messages = _without_media(body.get("messages") or [], media)
    size = len(_dumps(messages)) + len(_dumps(body.get("system") or "")) + len(_dumps(body.get("tools") or []))
    return max(1, size // 4 + media[0] * IMAGE_TOKENS)


# ---------------------------------------------------------------------------
# Tool calls: JSON repair, names, schema coercion
# ---------------------------------------------------------------------------

STOP_MAP = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use",
            "function_call": "tool_use", "content_filter": "end_turn", "eos": "end_turn",
            "eos_token": "end_turn", "end_turn": "end_turn", "tool_use": "tool_use", "max_tokens": "max_tokens",
            "model_length": "max_tokens", "stop_sequence": "end_turn"}

_DECODER = json.JSONDecoder()


def _strip_fence(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[\w+-]*[ \t]*\n?", "", s)
        s = re.sub(r"\n?```\s*$", "", s)
    return s.strip()


def _jsonable(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, default=lambda o: list(o) if isinstance(o, (set, frozenset, tuple)) else str(o)))
    except (TypeError, ValueError):
        return None


def _literal(s: str) -> Any:
    """Python-literal arguments ({'a': True}) from weak models; ast.literal_eval never executes code."""
    if not s or len(s) > 100000 or s[0] not in "{[":
        return None
    try:
        import ast
        value = ast.literal_eval(s)
    except Exception:
        return None
    return _jsonable(value) if isinstance(value, (dict, list)) else None


def _loads_lenient(raw: str, repair: bool = True) -> Any:
    """json.loads that survives fences, prose, trailing junk, Python literals and truncation. None if hopeless.

    repair=False refuses the last step (closing a truncated value): a cut-off tool call must never be
    completed into a different, valid one.
    """
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except ValueError:
        pass
    s = _strip_fence(s)
    try:
        return json.loads(s)
    except ValueError:
        pass
    starts = [i for i in (s.find("{"), s.find("[")) if i >= 0]
    start = min(starts) if starts else -1
    if start >= 0:
        try:
            return _DECODER.raw_decode(s, start)[0]
        except ValueError:
            pass
        lit = _literal(s[start:])
        if lit is not None:
            return lit
    return repair_json(s) if repair else None


def parse_arguments(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if raw is None:
        return {}
    if not isinstance(raw, str):
        raw = _dumps(raw)
    raw = raw.strip()
    if not raw or raw in ("null", "None", "{}"):
        return {}
    val = _loads_lenient(raw)
    if isinstance(val, str) and val.strip()[:1] in ("{", "["):
        inner = _loads_lenient(val)          # double-encoded arguments
        if isinstance(inner, dict):
            return inner
    if isinstance(val, dict):
        return val
    return {"value": val} if val is not None else {"_raw": raw}


def complete_arguments(raw: Any) -> Optional[Dict[str, Any]]:
    """Tool arguments only when they are complete (fences, junk and literals tolerated, truncation not)."""
    if isinstance(raw, dict):
        return raw
    if raw is None:
        return {}
    if not isinstance(raw, str):
        raw = _dumps(raw)
    raw = raw.strip()
    if not raw or raw in ("null", "None", "{}"):
        return {}
    val = _loads_lenient(raw, repair=False)
    if isinstance(val, str) and val.strip()[:1] in ("{", "["):
        val = _loads_lenient(val, repair=False)
    return val if isinstance(val, dict) else None


def final_stop(stop: str, tools: int, notice: bool, limit_cut_tool: bool) -> str:
    """The stop_reason Claude Code receives, one rule for both OpenAI wires.

    `limit_cut_tool`: the output cap cut only a tool call, which was withheld with a note, so the visible text
    is whole. Saying max_tokens would make Claude Code resume the same reply again and again, then report a
    bogus "exceeded the 32000 output token maximum" for a reply of a few hundred tokens: the turn ends instead,
    and calls that did finish still run (tool_use).
    """
    if limit_cut_tool:
        stop = "end_turn"
    if tools and stop == "end_turn" and not notice:
        stop = "tool_use"
    if stop == "tool_use" and not tools:
        stop = "end_turn"
    return stop


def cut_note(name: str, limit: bool = False) -> str:
    """Shown instead of a tool call that never finished: it is never run. `limit`: the provider ended the
    reply at its output token cap (finish_reason "length")."""
    if limit:
        return ("Pulsar Code: the provider stopped this reply at its output token limit while the model was "
                "writing its call to %s, so the call was cut off and not run. Ask the model to work in smaller "
                "steps (for example, write a long file in parts)." % (name or "a tool"))
    return ("Pulsar Code: the model's call to %s was cut off before its arguments were complete, so it was "
            "not run. Ask the model to try again (smaller steps help)." % (name or "a tool"))


_NAME_GUESS = re.compile(r'"name"\s*:\s*"([^"]{1,128})"|<function=([^>\s]{1,128})|<invoke name="([^"]{1,128})"')


def _guess_name(content: str) -> str:
    m = _NAME_GUESS.search(content or "")
    return next((g for g in m.groups() if g), "") if m else ""


def _close_json(s: str) -> str:
    stack, in_str, esc = [], False, False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    if in_str and esc:
        s = s[:-1]
    return s + ('"' if in_str else "") + "".join(reversed(stack))


def repair_json(raw: str) -> Any:
    """Best-effort close of a truncated JSON value (dropping a dangling last member if needed)."""
    s = _strip_fence(raw)
    try:
        return json.loads(s)
    except ValueError:
        pass
    for _ in range(8):
        try:
            return json.loads(_close_json(s))
        except ValueError:
            pass
        cut = s.rfind(",")
        if cut <= 0:
            return None
        s = s[:cut]
    return None


def _canon(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


_NAME_PREFIX = re.compile(r"^(?:functions?|tools?|default_api|api|plugins?|actions?)[.:/]", re.I)

# Names other agents and training sets use for Claude Code's built-in tools (canonical form).
_NAME_ALIASES = {
    "shell": "Bash", "runshellcommand": "Bash", "runterminalcmd": "Bash", "runterminalcommand": "Bash",
    "executecommand": "Bash", "executebash": "Bash", "execcommand": "Bash", "bashcommand": "Bash",
    "terminal": "Bash", "runcommand": "Bash", "runbash": "Bash", "shellexec": "Bash", "executeshell": "Bash",
    "readfile": "Read", "viewfile": "Read", "openfile": "Read", "fileread": "Read", "cat": "Read",
    "writefile": "Write", "createfile": "Write", "writetofile": "Write", "filewrite": "Write", "savefile": "Write",
    "editfile": "Edit", "strreplace": "Edit", "replaceinfile": "Edit", "fileedit": "Edit",
    "searchreplace": "Edit", "applyedit": "Edit",
    "globsearch": "Glob", "filesearch": "Glob", "findfiles": "Glob", "fileglob": "Glob",
    "grepsearch": "Grep", "searchfiles": "Grep", "searchcode": "Grep", "codesearch": "Grep",
    "ripgrep": "Grep", "rg": "Grep", "searchfilecontent": "Grep",
    "fetch": "WebFetch", "fetchurl": "WebFetch", "urlfetch": "WebFetch", "browse": "WebFetch",
    "openurl": "WebFetch", "fetchwebpage": "WebFetch",
    "searchweb": "WebSearch", "googlesearch": "WebSearch", "internetsearch": "WebSearch",
    "updatetodos": "TodoWrite", "writetodos": "TodoWrite", "todo": "TodoWrite", "todos": "TodoWrite",
    "todolist": "TodoWrite", "updatetodolist": "TodoWrite",
}

# Property-name aliases (canonical forms), used only to fill a missing schema property from an unknown key.
_KEY_ALIASES = {
    "filepath": ("path", "file", "filename", "targetfile", "absolutepath", "fullpath", "filepathname"),
    "notebookpath": ("path", "file", "filepath", "notebook"),
    "command": ("cmd", "shellcommand", "bashcommand", "script", "commandline", "shell"),
    "content": ("contents", "text", "data", "filecontent", "filecontents", "body", "code"),
    "oldstring": ("old", "oldstr", "oldtext", "search", "find", "original", "searchstring", "oldcontent"),
    "newstring": ("new", "newstr", "newtext", "replace", "replacement", "replacewith", "replacementtext",
                  "newcontent"),
    "pattern": ("regex", "query", "search", "glob", "searchpattern", "globpattern", "expression"),
    "url": ("link", "uri", "href", "website", "address"),
    "query": ("q", "searchquery", "searchterm", "search", "keywords", "term"),
    "path": ("dir", "directory", "folder", "cwd", "root", "searchpath", "dirpath", "directorypath"),
    "description": ("desc",),
    "todos": ("tasks", "items", "todolist"),
}

_WRAPPERS = ("arguments", "parameters", "input", "args", "params", "tool_input", "kwargs")
_WRAP_EXTRA = ("name", "tool", "tool_name", "function", "type", "id")


def _clean_name(name: str) -> str:
    n = str(name).strip().split("<|")[0].strip().strip("`'\"").strip()
    n = re.sub(r"\(\s*\)$", "", n)
    n = re.sub(r":\d+$", "", n)
    return n.strip()


class ToolIndex:
    """The request's tool list, for name repair, argument coercion and call rescue."""

    def __init__(self, tools: Any = None) -> None:
        self.names: List[str] = []
        self.schemas: Dict[str, Dict[str, Any]] = {}
        for t in tools if isinstance(tools, list) else []:
            if not isinstance(t, dict):
                continue
            if t.get("type") == "function" and isinstance(t.get("function"), dict):
                name, schema = t["function"].get("name"), t["function"].get("parameters")
            else:
                name, schema = t.get("name"), t.get("input_schema")
            if not isinstance(name, str) or not name or name in self.schemas:
                continue
            self.names.append(name)
            self.schemas[name] = schema if isinstance(schema, dict) else {}
        self._set = set(self.names)
        self._alias = {wire_name(n): n for n in self.names if wire_name(n) != n}
        self._canon: Dict[str, List[str]] = {}
        for n in self.names:
            self._canon.setdefault(_canon(n), []).append(n)
        self._memo: Dict[str, str] = {}

    def __bool__(self) -> bool:
        return bool(self.names)

    def known(self, name: Any) -> bool:
        return isinstance(name, str) and self.resolve(name) in self._set

    def schema(self, name: str) -> Dict[str, Any]:
        return self.schemas.get(name) or {}

    def resolve(self, name: Any) -> Any:
        if not isinstance(name, str) or not self.names or name in self._set:
            return name
        if name in self._alias:
            return self._alias[name]
        if name not in self._memo:
            try:
                self._memo[name] = self._resolve(name)
            except Exception:
                self._memo[name] = name
        return self._memo[name]

    def _unique(self, canon: str) -> Optional[str]:
        hit = self._canon.get(canon)
        return hit[0] if hit and len(hit) == 1 else None

    def _resolve(self, name: str) -> str:
        n = _clean_name(name)
        cands = [n]
        while True:
            m = _NAME_PREFIX.match(cands[-1])
            if not m or m.end() >= len(cands[-1]):
                break
            cands.append(cands[-1][m.end():])
        for c in cands:
            if c in self._set:
                return c
            hit = self._canon.get(_canon(c))
            if hit:
                return hit[0] if len(hit) == 1 else name   # two tools differ only by case: do not guess
        base = cands[-1]
        # MCP tools: a wrong or missing server prefix ("mcp__other__x", "x") on a real "mcp__srv__x".
        suffix = base.split("__")[-1] if "__" in base else base
        cs = _canon(suffix)
        if cs:
            hits = [t for t in self.names if "__" in t and _canon(t.split("__")[-1]) == cs]
            if len(hits) == 1:
                return hits[0]
            if "__" in base:
                hit = self._unique(cs)
                if hit and "__" not in hit:
                    return hit
        alias = _NAME_ALIASES.get(_canon(base))
        if alias in self._set:
            return alias
        cb = _canon(base)
        if len(cb) >= 4:
            import difflib
            # High bar: a near miss between verbs (NotebookRead vs NotebookEdit) must never turn a read into a write.
            good = [k for k in self._canon if difflib.SequenceMatcher(None, cb, k).ratio() >= 0.88]
            if len(good) == 1 and len(self._canon[good[0]]) == 1:
                return self._canon[good[0]][0]
        return name

    def infer(self, args: Any) -> Optional[str]:
        """The tool a nameless call meant, judged by its argument keys (None when unsure)."""
        if len(self.names) == 1:
            return self.names[0]
        if not isinstance(args, dict) or not args:
            return None
        keys = set(args)
        hits = []
        for n in self.names:
            s = self.schemas.get(n) or {}
            props = s.get("properties") if isinstance(s.get("properties"), dict) else {}
            req = {r for r in s.get("required") or [] if isinstance(r, str)}
            if props and keys <= set(props) and req <= keys:
                hits.append(n)
        return hits[0] if len(hits) == 1 else None

    def fix(self, name: str, args: Any) -> Call:
        """Repaired tool name and schema-coerced arguments."""
        real = self.resolve(name) if self.names else name
        if not isinstance(args, dict):
            args = parse_arguments(args)
        schema = self.schemas.get(real)
        if schema:
            args = coerce_arguments(args, schema)
        return str(real), args


def repair_tool_name(name: str, tools: Any) -> str:
    """Map a mangled tool name onto the request's real tool, or return it unchanged."""
    index = tools if isinstance(tools, ToolIndex) else ToolIndex(tools)
    return index.resolve(name)


def fix_tool_input(name: str, args: Any, tools: Any) -> Call:
    index = tools if isinstance(tools, ToolIndex) else ToolIndex(tools)
    return index.fix(name, args)


_FAIL = object()


def _types(schema: Dict[str, Any]) -> List[str]:
    t = schema.get("type")
    if isinstance(t, str):
        return [t]
    if isinstance(t, list):
        return [x for x in t if isinstance(x, str)]
    if isinstance(schema.get("properties"), dict):
        return ["object"]
    if "items" in schema:
        return ["array"]
    return []


def _is(value: Any, t: str) -> bool:
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "string":
        return isinstance(value, str)
    if t == "object":
        return isinstance(value, dict)
    if t == "array":
        return isinstance(value, list)
    if t == "null":
        return value is None
    return False


def _convert(value: Any, t: str) -> Any:
    if t == "integer":
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            x = value.strip()
            if re.fullmatch(r"[+-]?\d+", x):
                return int(x)
            if re.fullmatch(r"[+-]?\d+\.0*", x):
                return int(float(x))
        return _FAIL
    if t == "number":
        if isinstance(value, str):
            x = value.strip()
            if re.fullmatch(r"[+-]?\d+", x):
                return int(x)
            if re.fullmatch(r"[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?", x):
                return float(x)
        return _FAIL
    if t == "boolean":
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
        return _FAIL
    if t == "object":
        if isinstance(value, str) and value.strip().startswith("{"):
            parsed = _loads_lenient(value)
            if isinstance(parsed, dict):
                return parsed
        return _FAIL
    if t == "array":
        if isinstance(value, str):
            if value.strip().startswith("["):
                parsed = _loads_lenient(value)
                if isinstance(parsed, list):
                    return parsed
            return [value]
        if value is not None and not isinstance(value, list):
            return [value]
        return _FAIL
    if t == "string":
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, indent=2)
        return _FAIL
    if t == "null":
        if isinstance(value, str) and value.strip().lower() in ("null", "none"):
            return None
        return _FAIL
    return _FAIL


def _enum_fix(value: Any, enum: Any) -> Any:
    if not isinstance(value, str) or not isinstance(enum, list) or value in enum:
        return value
    for key in (lambda s: s.strip().lower(), _canon):
        hits = [e for e in enum if isinstance(e, str) and key(e) == key(value)]
        if len(hits) == 1:
            return hits[0]
    return value


def _rename_keys(args: Dict[str, Any], props: Dict[str, Any], required: Sequence[str], aliases: bool) -> None:
    missing = [p for p in required if p in props and p not in args]
    missing += [p for p in props if p not in args and p not in missing]
    unknown = [k for k in args if k not in props]
    for p in missing:
        if not unknown:
            return
        cp = _canon(p)
        cands = [k for k in unknown if _canon(k) == cp]
        if not cands and aliases:
            al = _KEY_ALIASES.get(cp, ())
            cands = [k for k in unknown if _canon(k) in al]
        if len(cands) == 1:
            renamed = {(p if k == cands[0] else k): v for k, v in args.items()}
            args.clear()
            args.update(renamed)      # same key order, new name
            unknown.remove(cands[0])


def _coerce_value(value: Any, schema: Any, depth: int) -> Any:
    if depth > 24 or not isinstance(schema, dict):
        return value
    branches = [b for b in (schema.get("anyOf") or schema.get("oneOf") or []) if isinstance(b, dict)]
    if branches and not schema.get("type"):
        for b in branches:
            if any(_is(value, t) for t in _types(b)):
                return _coerce_value(value, b, depth + 1)
        for b in branches:
            nv = _coerce_value(value, b, depth + 1)
            if any(_is(nv, t) for t in _types(b)):
                return nv
        return value
    if "enum" in schema:
        value = _enum_fix(value, schema["enum"])
    types = _types(schema)
    if not types:
        return value
    match = next((t for t in types if _is(value, t)), None)
    if match is None:
        for t in types:
            nv = _convert(value, t)
            if nv is not _FAIL:
                value, match = nv, t
                break
    if match == "object":
        return _coerce_object(value, schema, depth)
    if match == "array" and isinstance(schema.get("items"), dict):
        return [_coerce_value(v, schema["items"], depth + 1) for v in value]
    return value


def _coerce_object(value: Dict[str, Any], schema: Dict[str, Any], depth: int, top: bool = False) -> Dict[str, Any]:
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = [r for r in schema.get("required") or [] if isinstance(r, str)]
    out = dict(value)
    if props:
        _rename_keys(out, props, required, aliases=top)
    extra = schema.get("additionalProperties")
    for k in list(out):
        sub = props.get(k) if k in props else (extra if isinstance(extra, dict) else None)
        if sub is not None:
            out[k] = _coerce_value(out[k], sub, depth + 1)
    return out


def coerce_arguments(args: Any, schema: Any) -> Any:
    """Bend tool arguments toward the tool's JSON schema. Drops nothing and never raises.

    Unwraps {"arguments": {...}} style wrappers, routes a lone {"value": x}
    or unparseable {"_raw": s} to the single required property, renames
    near-miss keys (filePath, path -> file_path), and converts "5" -> 5,
    "true" -> true, JSON strings -> objects/arrays, scalar -> [scalar],
    case-insensitive enum values, and non-strings to strings.
    """
    try:
        return _coerce_top(args, schema)
    except Exception:
        return args


def _wrapped(value: Dict[str, Any], props: Dict[str, Any]) -> Optional[Tuple[Any, bool]]:
    """(inner value, came from a string) when value is only a wrapper such as {"arguments": ...}."""
    wraps = [k for k in value if k in _WRAPPERS and k not in props]
    if len(wraps) != 1 or any(k in props or k not in _WRAP_EXTRA for k in value if k != wraps[0]):
        return None
    inner = value[wraps[0]]
    if isinstance(inner, str):
        parsed = _loads_lenient(inner) if inner.strip()[:1] == "{" else None
        return (parsed, True) if isinstance(parsed, dict) else None
    return (inner, False) if isinstance(inner, dict) else None


def _unwrap(args: Dict[str, Any], props: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Peel {"arguments": {...}} style wrappers (up to three deep) when the inside is what the tool takes."""
    if not props:
        return None
    known = {_canon(p) for p in props}
    cur, from_text = args, False
    for _ in range(3):
        got = _wrapped(cur, props)
        if got is None:
            break
        cur, text = got
        from_text = from_text or text
        if cur and {_canon(k) for k in cur} & known:
            return dict(cur)
    if cur is not args and not cur and not from_text:
        return {}                     # {"arguments": {}}: a call without arguments
    return None


def _coerce_top(args: Any, schema: Any) -> Any:
    if not isinstance(schema, dict) or not isinstance(args, dict):
        return args
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = [r for r in schema.get("required") or [] if isinstance(r, str)]
    args = dict(args)
    unwrapped = _unwrap(args, props)
    if unwrapped is not None:
        args = unwrapped
    if props and len(args) == 1:
        target = required[0] if len(required) == 1 else (next(iter(props)) if len(props) == 1 else None)
        if target and target not in args:
            key = next(iter(args))
            value = args[key]
            tschema = props.get(target) if isinstance(props.get(target), dict) else {}
            if key == "value" and key not in props:
                args = {target: value}
            elif (key == "_raw" and key not in props and isinstance(value, str) and value.strip()[:1] not in ("{", "[")
                  and _types(tschema) in ([], ["string"])):
                args = {target: value}
    return _coerce_object(args, schema, 0, top=True)


# ---------------------------------------------------------------------------
# Tool-call rescue: calls a host returned as text
# ---------------------------------------------------------------------------

_DS_CALLS_BEGIN = "<\uff5ctool\u2581calls\u2581begin\uff5c>"
_DS_CALLS_END = "<\uff5ctool\u2581calls\u2581end\uff5c>"
_DS_CALL_BEGIN = "<\uff5ctool\u2581call\u2581begin\uff5c>"
_DS_CALL_END = "<\uff5ctool\u2581call\u2581end\uff5c>"
_DS_SEP = "<\uff5ctool\u2581sep\uff5c>"

# opener, closers, parser kind
_TAGS: Tuple[Tuple[str, Tuple[str, ...], str], ...] = (
    ("<tool_call>", ("</tool_call>",), "hermes"),
    ("<tool_calls>", ("</tool_calls>",), "hermes"),
    ("<function_calls>", ("</function_calls>",), "hermes"),
    ("<minimax:tool_call>", ("</minimax:tool_call>",), "hermes"),
    ("<function=", ("</function>",), "llama"),
    ("<|tool_call|>", ("<|/tool_call|>", "</|tool_call|>", "<|tool_call_end|>", "<|end_tool_call|>"), "json"),
    ("<|tool_calls_section_begin|>", ("<|tool_calls_section_end|>",), "kimi"),
    ("<|tool_call_begin|>", ("<|tool_call_end|>",), "kimi1"),
    ("<|python_tag|>", ("<|eom_id|>", "<|eot_id|>"), "json"),
    ("[TOOL_CALLS]", (), "mistral"),
    (_DS_CALLS_BEGIN, (_DS_CALLS_END,), "deepseek"),
    (_DS_CALL_BEGIN, (_DS_CALL_END,), "deepseek1"),
)
_TAG_BY_OPENER = {t[0]: t for t in _TAGS}
_OPEN_RE = re.compile("|".join(re.escape(t[0]) for t in sorted(_TAGS, key=lambda t: -len(t[0]))))
_PREFIXES = frozenset(t[0][:i] for t in _TAGS for i in range(1, len(t[0])))
_MAX_OPEN = max(len(t[0]) for t in _TAGS)
_TAG_CAP = 4 * 1024 * 1024
_JSON_CAP = 4 * 1024 * 1024
_JSON_LANGS = ("", "json", "json5", "jsonc", "tool_call", "tool_calls", "tool_code", "function_call")
_CALL_KEYS = frozenset(("name", "arguments", "parameters", "function", "tool", "tool_name", "type", "id", "input",
                        "args", "tool_input", "function_name", "params", "tool_calls", "recipient_name", "call_id"))
_NAME_KEYS = ("name", "tool_name", "tool", "function_name", "recipient_name", "function")
_ARG_KEYS = ("arguments", "parameters", "input", "args", "tool_input", "params")
_FIRST_KEY = re.compile(r'"((?:[^"\\]|\\.){0,64})"')
_NAME_VALUE = re.compile(r'"(?:name|tool_name|function_name|tool)"\s*:\s*"((?:[^"\\]|\\.){1,128})"')
_KIMI_CALL = re.compile(r"<\|tool_call_begin\|>\s*(.*?)\s*<\|tool_call_argument_begin\|>(.*?)(?:<\|tool_call_end\|>|\Z)", re.S)
_DS_CALL = re.compile(re.escape(_DS_CALL_BEGIN) + r"(.*?)" + re.escape(_DS_SEP) + r"(.*?)(?:"
                      + re.escape(_DS_CALL_END) + r"|\Z)", re.S)


def _call_from_obj(obj: Any, strict: bool = False) -> Optional[Call]:
    if not isinstance(obj, dict):
        return None
    src = obj
    if isinstance(obj.get("function"), dict):
        src = dict(obj)
        src.pop("function")
        src.update(obj["function"])
    name = next((v.strip() for k in _NAME_KEYS for v in [src.get(k)] if isinstance(v, str) and v.strip()), None)
    if not name:
        return None
    key = next((k for k in _ARG_KEYS if k in src), None)
    if key is None:
        if strict:
            return None
        args: Any = {}
    else:
        args = src[key]
    if isinstance(args, str):
        args = parse_arguments(args)
    elif args is None:
        args = {}
    elif not isinstance(args, dict):
        args = {"value": args}
    return name, args


def _calls_from_value(value: Any, strict: bool = False) -> List[Call]:
    if isinstance(value, dict) and isinstance(value.get("tool_calls"), list):
        value = value["tool_calls"]
    items = value if isinstance(value, list) else [value]
    out = []
    for it in items:
        c = _call_from_obj(it, strict)
        if c is not None:
            out.append(c)
    return out


def _calls_from_json_text(text: str) -> List[Call]:
    c = _strip_fence(text)
    values = []
    i, n = 0, len(c)
    while i < n:
        while i < n and c[i] in " \t\r\n,;":
            i += 1
        if i >= n or c[i] not in "{[":
            break
        try:
            v, i = _DECODER.raw_decode(c, i)
        except ValueError:
            v = _literal(c[i:])
            if v is None:
                v = repair_json(c[i:])
            if v is None:
                break
            i = n
        values.append(v)
    out: List[Call] = []
    for v in values:
        out += _calls_from_value(v)
    return out


def _xml_value(v: str) -> str:
    if v.startswith("\r\n"):
        v = v[2:]
    elif v.startswith("\n"):
        v = v[1:]
    if v.endswith("\r\n"):
        v = v[:-2]
    elif v.endswith("\n"):
        v = v[:-1]
    return v


def _function_body(body: str) -> Dict[str, Any]:
    b = body.strip()
    if not b:
        return {}
    if b.startswith("{") or b.startswith("```"):
        return parse_arguments(b)
    return {k.strip(): _xml_value(v) for k, v in re.findall(
        r"<parameter=([^>\n]+)>(.*?)(?:</parameter>|(?=<parameter=)|(?=</function>)|\Z)", b, re.S)}


def _parse_hermes(c: str) -> List[Call]:
    if c.startswith(("{", "[", "```")):
        return _calls_from_json_text(c)
    if c.startswith("<tool_call>"):
        out: List[Call] = []
        for inner in re.findall(r"<tool_call>(.*?)(?:</tool_call>|\Z)", c, re.S):
            if inner.strip():
                out += _parse_hermes(inner.strip())
        return out
    if "<invoke" in c[:400]:
        out = []
        for name, body in re.findall(r'<invoke\s+name\s*=\s*"([^"]+)"\s*>(.*?)(?:</invoke>|\Z)', c, re.S):
            params = {k: _xml_value(v) for k, v in re.findall(
                r'<parameter\s+name\s*=\s*"([^"]+)"\s*>(.*?)(?:</parameter>|\Z)', body, re.S)}
            out.append((name.strip(), params))
        return out
    if c.startswith("<function="):
        return [(name.strip(), _function_body(body)) for name, body in
                re.findall(r"<function=([^>\n]+)>(.*?)(?:</function>|\Z)", c, re.S)]
    m = re.match(r"([\w.\-:]+)\s*", c)        # GLM: NAME then <arg_key>/<arg_value> pairs (or JSON)
    if m is None:
        return []
    rest = c[m.end():]
    pairs = re.findall(r"<arg_key>(.*?)</arg_key>\s*<arg_value>(.*?)(?:</arg_value>|\Z)", rest, re.S)
    if pairs:
        return [(m.group(1), {k.strip(): _xml_value(v) for k, v in pairs})]
    if rest.strip().startswith("{"):
        return [(m.group(1), parse_arguments(rest))]
    return [(m.group(1), {})] if not rest.strip() else []


def _parse_mistral(c: str) -> List[Call]:
    out: List[Call] = []
    for piece in c.split("[TOOL_CALLS]"):
        p = piece.strip()
        if not p:
            continue
        if p[0] in "{[":
            out += _calls_from_json_text(p)
            continue
        m = (re.match(r"([\w.\-:]+)\s*(?:\[CALL_ID\]\s*[\w-]*\s*)?\[ARGS\]\s*(.*)\Z", p, re.S)
             or re.match(r"([\w.\-:]+)\s*(\{.*)\Z", p, re.S))
        if m is None:
            return []
        out.append((m.group(1), parse_arguments(m.group(2))))
    return out


def _ds_call(head: str, body: str) -> Optional[Call]:
    head = head.strip()
    if head in ("function", ""):
        head, _, body = body.lstrip().partition("\n")
    name = head.strip()
    return (name, parse_arguments(_strip_fence(body))) if name else None


def _parse_tag(kind: str, content: str) -> List[Call]:
    c = content.strip()
    if not c:
        return []
    try:
        if kind == "hermes":
            return _parse_hermes(c)
        if kind == "json":
            return _calls_from_json_text(c)
        if kind == "llama":
            name, sep, body = c.partition(">")
            name = name.strip()
            if not sep or not re.fullmatch(r"[\w.\-:]+", name):
                return []
            return [(name, _function_body(body))]
        if kind == "kimi":
            return [(n.strip(), parse_arguments(a)) for n, a in _KIMI_CALL.findall(c) if n.strip()]
        if kind == "kimi1":
            name, sep, body = c.partition("<|tool_call_argument_begin|>")
            return [(name.strip(), parse_arguments(body))] if sep and name.strip() else []
        if kind == "mistral":
            return _parse_mistral(c)
        if kind == "deepseek":
            return [x for x in (_ds_call(h, b) for h, b in _DS_CALL.findall(c)) if x]
        if kind == "deepseek1":
            h, sep, b = c.partition(_DS_SEP)
            x = _ds_call(h, b) if sep else None
            return [x] if x else []
    except Exception:
        return []
    return []


def _not_a_call(kind: str, s: str) -> bool:
    """For tags that also appear in prose (<tool_call>, <function=): does the text after the opener rule out a call?"""
    if kind == "llama":
        m = re.match(r"[\w.\-:]+", s)
        if m is None:
            return bool(s)
        rest = s[m.end():]
        if not rest:
            return len(s) > 80
        if rest[0] != ">":
            return True
        after = rest[1:].lstrip()
        return bool(after) and after[0] not in "{<`"
    t = s.lstrip()
    if not t:
        return len(s) > 200
    if t[0] in "{[<`":
        return False
    m = re.match(r"[\w.\-:]+", t)
    if m is None:
        return True
    rest = t[m.end():]
    if not rest:
        return len(t) > 80
    return re.match(r"[ \t]*(?:\r?\n|\{|<arg_key>)", rest) is None


# What may sit around the JSON values inside a tag that never closed: separators, fences and each format's own
# call markers (a tool name only right before its marker or its JSON). Anything else is prose.
_GLUE_ANY = r"\s+|[,;]|```[\w+-]*"
_GLUE_NAME = r"[\w.\-:]+(?=\s*\{)"
_GLUE_KIMI = re.compile(_GLUE_ANY + r"|<\|tool_call(?:s_section)?_(?:begin|end)\|>"
                                    r"|[^<>{}\[\]]*?<\|tool_call_argument_begin\|>")
_GLUE_DS = re.compile(_GLUE_ANY + "|" + "|".join(re.escape(t) for t in (
    _DS_CALLS_BEGIN, _DS_CALLS_END, _DS_CALL_BEGIN, _DS_CALL_END, _DS_SEP))
    + r"|[\w.\-:]+(?=\s*(?:" + re.escape(_DS_SEP) + r"|\{|```))")
_GLUE: Dict[str, Any] = {
    "hermes": re.compile(_GLUE_ANY + r"|</?tool_call>|<function=[^>\n]+>|</function>|" + _GLUE_NAME),
    "json": re.compile(_GLUE_ANY),
    "llama": re.compile(_GLUE_ANY + r"|[\w.\-:]+\s*>"),
    "mistral": re.compile(_GLUE_ANY + r"|\[TOOL_CALLS\]|[\w.\-:]+\s*(?:\[CALL_ID\]\s*[\w-]*\s*)?\[ARGS\]|"
                          + _GLUE_NAME),
    "kimi": _GLUE_KIMI, "kimi1": _GLUE_KIMI, "deepseek": _GLUE_DS, "deepseek1": _GLUE_DS,
}


def _markup_only(kind: str, content: str) -> bool:
    """Does a tag that never closed hold nothing but call markup? It runs to the end of the reply, so prose
    after its JSON means the tag was quoted, not called."""
    glue = _GLUE.get(kind)
    if glue is None:
        return False
    i, n = 0, len(content)
    while i < n:
        m = glue.match(content, i)
        if m is not None and m.end() > i:
            i = m.end()
        elif content[i] in "{[":
            try:
                i = _DECODER.raw_decode(content, i)[1]
            except ValueError:
                return _literal(content[i:].rstrip()) is not None
        else:
            return False
    return True


def _json_doomed(s: str, tools: ToolIndex) -> bool:
    """Can a buffered message prefix no longer become a bare JSON tool call?"""
    b = s.lstrip()
    if b.startswith("["):
        b = b[1:].lstrip()
        if not b:
            return False
        if b[0] != "{":
            return True
    if not b.startswith("{"):
        return True
    inner = b[1:].lstrip()
    if not inner:
        return False
    if inner[0] != '"':
        return True
    m = _FIRST_KEY.match(inner)
    if m is None:
        return len(inner) > 72
    if m.group(1) not in _CALL_KEYS:
        return True
    n = _NAME_VALUE.search(b)
    if n:
        try:
            name = json.loads('"%s"' % n.group(1))
        except ValueError:
            name = n.group(1)
        if not tools.known(name):
            return True
    return False


def _bare_calls(text: str, tools: ToolIndex) -> List[Call]:
    try:
        value = json.loads(text)
    except ValueError:
        return []
    if isinstance(value, dict) and isinstance(value.get("tool_calls"), list):
        value = value["tool_calls"]
    items = value if isinstance(value, list) else [value]
    calls = []
    for it in items:
        c = _call_from_obj(it, strict=True)
        if c is None or not tools.known(c[0]):
            return []
        calls.append(c)
    return calls


class _JsonScan:
    """Incremental scan of one JSON object or array: where does the top-level value end?"""

    __slots__ = ("depth", "in_str", "esc", "state")
    _OUT = re.compile(r'["{}\[\]]')
    _IN = re.compile(r'["\\]')

    def __init__(self) -> None:
        self.depth = 0
        self.in_str = False
        self.esc = False
        self.state = 0          # 0 waiting, 1 inside, 2 done, 3 not an object/array

    def feed(self, s: str) -> int:
        """Consume s; the offset just past the closing bracket if the value ends in s, else -1."""
        if self.state >= 2:
            return -1
        i, n = 0, len(s)
        if self.state == 0:
            while i < n and s[i] in " \t\r\n":
                i += 1
            if i == n:
                return -1
            if s[i] not in "{[":
                self.state = 3
                return -1
            self.state = 1
        while i < n:
            if self.in_str:
                if self.esc:
                    self.esc = False
                    i += 1
                    continue
                m = self._IN.search(s, i)
                if m is None:
                    return -1
                i = m.start()
                if s[i] == "\\":
                    self.esc = True
                else:
                    self.in_str = False
                i += 1
            else:
                m = self._OUT.search(s, i)
                if m is None:
                    return -1
                i = m.start()
                ch = s[i]
                i += 1
                if ch == '"':
                    self.in_str = True
                elif ch in "{[":
                    self.depth += 1
                else:
                    self.depth -= 1
                    if self.depth <= 0:
                        self.state = 2
                        return i
        return -1


Item = Tuple[Any, ...]   # ("text", str) or ("call", name, args)


class TextToolParser:
    """Streaming finder of tool calls a host returned as text.

    feed() takes visible text and returns items: ("text", s) to show and
    ("call", name, args) to emit as tool_use. Text that might start a tag
    is held back only while it still could (a lone "<" waits one chunk);
    a message that opens like a JSON call is held until it is ruled in or
    out. Tags inside ``` code fences are left alone. Buffers grow by
    appending chunks, so a huge call costs linear time.

    Position decides, so markup quoted in prose never becomes a call: a tag
    is a call when the reply opens with it, or when nothing but whitespace
    and further calls follows it to the end of the reply (flush). A closed
    tag after prose is held until that is known; if more prose comes, it is
    shown exactly as written. A tag that never closed is a call only when
    it holds nothing but call markup.
    """

    def __init__(self, tools: Any, bare_json: bool = True) -> None:
        self.tools = tools if isinstance(tools, ToolIndex) else ToolIndex(tools)
        self.mode = "start" if bare_json else "text"
        self.buf = ""                 # unprocessed text (short outside tag/json modes)
        self.parts: List[str] = []    # tag content or JSON candidate seen so far
        self.size = 0
        self.head = ""                # first 4 KB of the tag content or JSON candidate
        self.tag: Optional[Tuple[str, Tuple[str, ...], str]] = None
        self.tag_ok = False
        self.lead = True              # only whitespace and calls so far: a tag here opens the reply
        self.tag_lead = False         # the current tag opened the reply
        self.waiting: List[str] = []  # closed tags after prose, as written, and the whitespace after them
        self.waiting_calls: List[Call] = []
        self.fence = False
        self.ticks = ""
        self.after_call = False
        self.calls = 0
        self.jkind = ""
        self.jscan = _JsonScan()
        self.jend = -1
        self.jafter = ""
        self.jok = False

    def feed(self, text: str) -> List[Item]:
        if not text:
            return []
        self.buf += text
        return self._run(False)

    def flush(self) -> List[Item]:
        """Settle everything held (end of stream, or a native tool call follows)."""
        out = self._run(True)
        if self.mode == "start":
            self.mode = "text"
        if self.waiting_calls:
            calls, self.waiting, self.waiting_calls = self.waiting_calls, [], []
            self._call(out, calls)        # only whitespace followed: the reply ends with these calls
        return out

    finish = flush

    # -- engine ---------------------------------------------------------------
    def _run(self, final: bool) -> List[Item]:
        out: List[Item] = []
        for _ in range(100000):
            if self.mode == "start":
                more = self._start(out, final)
            elif self.mode == "tag":
                more = self._in_tag(out, final)
            elif self.mode == "json":
                more = self._in_json(out, final)
            else:
                more = self._in_text(out, final)
            if not more:
                break
        merged: List[Item] = []
        for it in out:
            if it[0] == "text" and merged and merged[-1][0] == "text":
                merged[-1] = ("text", merged[-1][1] + it[1])
            else:
                merged.append(it)
        return merged

    def _take(self, s: str) -> None:
        if s:
            self.parts.append(s)
            self.size += len(s)
            if len(self.head) < 4096:
                self.head = (self.head + s)[:4096]

    def _held(self) -> str:
        """Everything taken so far plus the unprocessed rest, as one string."""
        text = "".join(self.parts) + self.buf
        self.parts, self.size, self.head = [], 0, ""
        return text

    def _emit(self, out: List[Item], text: str) -> None:
        if not text:
            return
        if self.waiting_calls:
            if not text.strip():
                self.waiting.append(text)    # still nothing but whitespace after the waiting calls
                return
            # Prose follows: the waiting tags were quoted, not called. Shown as written.
            text, self.waiting, self.waiting_calls = "".join(self.waiting) + text, [], []
        if self.after_call:
            text = text.lstrip()
            if not text:
                return
            self.after_call = False
        combined = self.ticks + text
        if combined.count("```") % 2:
            self.fence = not self.fence
        run = len(combined) - len(combined.rstrip("`"))
        self.ticks = "`" * (run % 3)
        if text.strip():
            self.lead = False
        out.append(("text", text))

    def _call(self, out: List[Item], calls: List[Call]) -> None:
        for name, args in calls:
            out.append(("call", name, args))
            self.calls += 1
        self.after_call = True

    def _start(self, out: List[Item], final: bool) -> bool:
        s = self.buf
        t = s.lstrip()
        if not t:
            if final:
                self.buf = ""
            return False
        if any(t.startswith(op) for op in _TAG_BY_OPENER):
            self.buf, self.mode = t, "text"
            return True
        if not final and len(t) < _MAX_OPEN and t in _PREFIXES:
            return False
        if t[0] in "{[" or t.startswith("```"):
            self.buf, self.mode = t, "json"
            self.jkind = "fence" if t[0] == "`" else "raw"
            self.jscan, self.jend, self.jafter, self.jok = _JsonScan(), -1, "", False
            self.parts, self.size, self.head = [], 0, ""
            return True
        if not final and t in ("`", "``"):
            return False
        self.mode = "text"
        return True

    def _in_text(self, out: List[Item], final: bool) -> bool:
        s = self.buf
        pos = 0
        while True:
            m = _OPEN_RE.search(s, pos)
            if m is None:
                break
            before = self.ticks + s[:m.start()]
            if self.fence ^ (before.count("```") % 2 == 1):
                pos = m.end()          # inside a code block: a quoted example, not a call
                continue
            self._emit(out, s[:m.start()])
            self.tag = _TAG_BY_OPENER[m.group(0)]
            self.tag_ok = False
            self.tag_lead = self.lead
            self.parts, self.size, self.head = [], 0, ""
            self.buf = s[m.end():]
            self.mode = "tag"
            return True
        keep = 0
        if not final:
            for k in range(min(_MAX_OPEN - 1, len(s)), 0, -1):
                if s[-k:] in _PREFIXES:
                    keep = k
                    break
        self._emit(out, s[:len(s) - keep])
        self.buf = s[len(s) - keep:]
        return False

    def _in_tag(self, out: List[Item], final: bool) -> bool:
        assert self.tag is not None
        opener, closers, kind = self.tag
        s = self.buf                  # a short tail of earlier content plus the new text
        best, closer = -1, ""
        for c in closers:
            p = s.find(c)
            if p >= 0 and (best < 0 or p < best):
                best, closer = p, c
        if best >= 0:
            self.buf = s[:best]
            content = self._held()
            self.buf = s[best + len(closer):]
            self._close_tag(out, content, closer)
            self.mode = "text"
            return True
        if kind in ("hermes", "llama") and not self.tag_ok:
            head = self.head + s
            if _not_a_call(kind, head):
                self._emit(out, opener)   # prose that merely mentions the tag: rescan the rest as text
                self.buf = self._held()
                self.mode = "text"
                return True
            if len(head.strip()) > 80:
                self.tag_ok = True
        if final or self.size + len(s) > _TAG_CAP:
            content = self._held()
            self.buf = ""
            if content.strip():
                brace = content.find("{")
                whole = brace >= 0 and complete_arguments(content[brace:]) is not None
                # A reply cut inside the closing tag itself still holds a whole call.
                part = next((k for c in closers for k in range(len(c) - 1, 0, -1) if content.endswith(c[:k])), 0)
                if whole and _markup_only(kind, content[:len(content) - part]):
                    self._close_tag(out, content, "")    # a complete call whose closing tag never came
                elif not whole and _parse_tag(kind, content):
                    self._emit(out, cut_note(_guess_name(content)))   # a call, but cut off: never run
                else:
                    self._emit(out, opener + content)    # prose that mentioned or quoted the tag
            self.mode = "text"
            return False
        keep = max((len(c) for c in closers), default=1) - 1
        cut = max(0, len(s) - keep)
        self._take(s[:cut])
        self.buf = s[cut:]
        return False

    def _close_tag(self, out: List[Item], content: str, closer: str) -> None:
        assert self.tag is not None
        opener, _, kind = self.tag
        calls = _parse_tag(kind, content)
        if not calls:
            self._emit(out, opener + content + closer)
        elif self.tag_lead:
            self._call(out, calls)        # the reply opens with it
        else:
            # After prose: a call only if the reply ends here (see flush), else a quote shown as written.
            self.waiting.append(opener + content + closer)
            self.waiting_calls += calls

    def _bail(self) -> bool:
        self.buf = self._held() + self.jafter
        self.jafter = ""
        self.mode = "text"
        return True

    def _in_json(self, out: List[Item], final: bool) -> bool:
        s, self.buf = self.buf, ""
        if self.jend >= 0:
            self.jafter += s          # after a complete value: only whitespace may follow
            if self.jafter.strip():
                return self._bail()
        elif self.jkind == "raw":
            end = self.jscan.feed(s)
            if end >= 0:
                self.jend = self.size + end
                self._take(s[:end])
                self.jafter = s[end:]
            else:
                self._take(s)
        else:
            self._take(s)
        if self.size > _JSON_CAP:
            return self._bail()
        if self.jkind == "fence":
            return self._in_fence(out, final)
        if not self.jok:
            if _json_doomed(self.head, self.tools):
                return self._bail()
            self.jok = len(self.head) >= 4096 or _NAME_VALUE.search(self.head) is not None
        if self.jend >= 0:
            if self.jafter.strip():
                return self._bail()
            if not final:
                return False
            return self._convert(out, "".join(self.parts))
        return self._bail() if final else False

    def _in_fence(self, out: List[Item], final: bool) -> bool:
        text = "".join(self.parts)    # fenced calls are small: decided within the first lines
        if len(self.parts) > 1:
            self.parts = [text]
        nl = text.find("\n")
        if nl < 0:
            return self._bail() if final or len(text) > 64 else False
        if text[3:nl].strip().lower() not in _JSON_LANGS:
            return self._bail()
        body = text[nl + 1:]
        lead = body.lstrip()
        if not lead:
            return self._bail() if final else False
        if lead[0] not in "{[":
            return self._bail()
        close = body.find("```")
        inner = body if close < 0 else body[:close]
        if not self.jok:
            if _json_doomed(inner[:4096], self.tools):
                return self._bail()
            self.jok = len(inner) >= 4096
        if close >= 0 and body[close + 3:].strip():
            return self._bail()
        if not final:
            return False
        if close < 0 and complete_arguments(inner.strip()) is None:
            return self._bail()        # the fence never closed and the JSON is cut: text, not a call
        return self._convert(out, inner)

    def _convert(self, out: List[Item], text: str) -> bool:
        calls = _bare_calls(text.strip(), self.tools)
        if not calls:
            return self._bail()
        self._call(out, calls)
        self.parts, self.size, self.head, self.jafter = [], 0, "", ""
        self.buf = ""
        self.mode = "text"
        return False


def extract_tool_calls(text: str, tools: Any) -> Tuple[str, List[Call]]:
    """Split a complete text reply into its visible text and the (repaired, coerced) tool calls in it."""
    index = tools if isinstance(tools, ToolIndex) else ToolIndex(tools)
    parser = TextToolParser(index)
    items = parser.feed(text or "") + parser.flush()
    calls = [index.fix(it[1], it[2]) for it in items if it[0] == "call"]
    return "".join(it[1] for it in items if it[0] == "text"), calls


# Closed models that call tools natively wherever they are served: tool-call markup in their text is a quote.
_NATIVE_CALLERS = re.compile(r"(?i)(^|/)(gpt-\d|chatgpt-|o\d+(-|$)|gemini-|claude-|grok-)")


def may_send_text_calls(model: str) -> bool:
    """Whether a reply from `model` may carry its tool calls as text (open models on hosts without a matching
    tool-call parser, or tools described in the prompt). False for models that always call tools natively."""
    return _NATIVE_CALLERS.search(model or "") is None


# ---------------------------------------------------------------------------
# Response: OpenAI -> Anthropic
# ---------------------------------------------------------------------------

def split_think(text: str) -> Tuple[str, str]:
    """Pull <think>...</think> out of a full (non-streamed) completion."""
    if "</think>" in text and "<think>" not in text.split("</think>", 1)[0]:
        text = "<think>" + text      # the template opened the think block inside the prompt
    thinking = "".join(re.findall(r"<think>(.*?)</think>", text, flags=re.S))
    visible = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.S)
    return thinking.strip(), visible


def _pick_choice(choices: Any) -> Optional[Dict[str, Any]]:
    """Choice index 0 (n > 1 streams interleave several choices; only the first is used)."""
    if not isinstance(choices, list):
        return None
    first = None
    for c in choices:
        if not isinstance(c, dict):
            continue
        if first is None:
            first = c
        if (c.get("index") or 0) == 0:
            return c
    return first


def _join_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_join_text(v.get("text") if isinstance(v, dict) else v) for v in value)
    return ""


def _content_parts(content: Any) -> List[Tuple[str, str]]:
    """Delta/message content (string or part list) -> [("text"|"think", text)]."""
    if content is None:
        return []
    if isinstance(content, str):
        return [("text", content)] if content else []
    if isinstance(content, dict):
        content = [content]
    out: List[Tuple[str, str]] = []
    if isinstance(content, list):
        for p in content:
            if isinstance(p, str):
                out.append(("text", p))
            elif isinstance(p, dict):
                t = p.get("type")
                if t in ("thinking", "reasoning"):
                    out.append(("think", _join_text(p.get("thinking", p.get("reasoning", p.get("text"))))))
                elif t == "refusal":
                    out.append(("text", _join_text(p.get("refusal"))))
                elif t in (None, "text", "output_text"):
                    out.append(("text", _join_text(p.get("text"))))
    return [(k, v) for k, v in out if v]


class _Tool:
    __slots__ = ("uid", "idx", "name", "hint", "parts", "scan", "emitted")

    def __init__(self, uid: Optional[str], idx: Optional[int], name: Optional[str] = None) -> None:
        self.uid = uid
        self.idx = idx
        self.name = name
        self.hint: Optional[str] = None
        self.parts: List[str] = []
        self.scan = _JsonScan()
        self.emitted = False

    @property
    def args(self) -> str:
        return "".join(self.parts)

    @property
    def streaming(self) -> bool:
        """Arguments still open (more fragments are expected). State 0 means only whitespace so far."""
        return self.scan.state == 1 or (self.scan.state == 0 and bool(self.name))


_NEXT_CALL = re.compile(r"\s*[,;]?\s*(?=\{)")


def _cached_tokens(u: Dict[str, Any]) -> Optional[int]:
    for d in (u.get("prompt_tokens_details"), u.get("input_tokens_details")):
        if isinstance(d, dict) and _int(d.get("cached_tokens")) is not None:
            return _int(d.get("cached_tokens"))
    for k in ("prompt_cache_hit_tokens", "cached_tokens", "cache_read_input_tokens"):
        if _int(u.get(k)) is not None:
            return _int(u.get(k))
    return None


def _cache_write_tokens(u: Dict[str, Any]) -> Optional[int]:
    d = u.get("prompt_tokens_details")
    if isinstance(d, dict):
        for k in ("cache_write_tokens", "cache_creation_tokens"):
            if _int(d.get(k)) is not None:
                return _int(d.get(k))
    return _int(u.get("cache_creation_input_tokens"))


class StreamTranslator:
    """OpenAI chat.completion.chunk stream -> Anthropic SSE events.

    Block order follows the upstream: thinking, text and tool calls open
    and close as they appear. Tool arguments are buffered per call and
    emitted once, when the call is complete, validated, repaired and
    coerced to the tool's schema, so a truncated or chatty upstream never
    hands Claude Code unparseable JSON. Every tool_use gets a fresh id.

    rescue: look for tool calls the host sent as text (TextToolParser).
    None decides by model (may_send_text_calls); a caller that knows the
    host passes True (a local server, tools described in the prompt) or
    False. rescue_reasoning: also look in reasoning when nothing else came,
    for hosts whose reasoning parser swallows the whole reply; off by
    default, because reasoning is not the answer.
    """

    def __init__(self, model: str, input_tokens: int = 0, emit_thinking: bool = True, tools: Any = None,
                 rescue: Optional[bool] = None, rescue_reasoning: bool = False) -> None:
        self.model = model
        self.input_tokens = input_tokens
        self.emit_thinking = emit_thinking
        self.toolset = tools if isinstance(tools, ToolIndex) else ToolIndex(tools)
        self.index = -1
        self.open_type: Optional[str] = None
        self.tools_emitted = 0
        self.out_chars = 0
        self.usage_out: Optional[int] = None
        self.usage_in: Optional[int] = None
        self.cache_read = 0
        self.cache_creation = 0
        self.cost = 0.0
        self.stop_reason = "end_turn"
        self.active = False        # any upstream token (visible or reasoning) seen
        self.visible = False       # any visible content seen
        self._said = False         # any visible text other than whitespace
        self._think_tag = False    # inside an inline <think> section
        self._tag_buf = ""
        self._started = False
        self._cache_known = False
        wanted = may_send_text_calls(model) if rescue is None else bool(rescue)
        self._parser = TextToolParser(self.toolset) if self.toolset and wanted else None
        self.rescue_reasoning = bool(rescue_reasoning) and self._parser is not None
        self._pending: List[_Tool] = []
        self._by_idx: Dict[int, _Tool] = {}
        self._by_id: Dict[str, _Tool] = {}
        self._last: Optional[_Tool] = None
        self._deferred: List[Tuple[str, str]] = []
        self._seen: set = set()
        self._reason: List[str] = []
        self._reason_len = 0
        self._finish_reason = ""
        self.cut_calls: List[str] = []   # calls withheld because they never finished

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

    def _close(self) -> List[Event]:
        ev: List[Event] = []
        if self.open_type is None:
            return ev
        if self.open_type == "thinking":
            ev.append(("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                               "delta": {"type": "signature_delta", "signature": ""}}))
        ev.append(("content_block_stop", {"type": "content_block_stop", "index": self.index}))
        self.open_type = None
        return ev

    def _open(self, kind: str, block: Dict[str, Any]) -> List[Event]:
        ev = self._close()
        self.index += 1
        self.open_type = kind
        ev.append(("content_block_start", {"type": "content_block_start", "index": self.index, "content_block": block}))
        return ev

    def _text(self, text: str, count: bool = True) -> List[Event]:
        if not text:
            return []
        ev: List[Event] = []
        if self.open_type != "text":
            ev += self._open("text", {"type": "text", "text": ""})
        self.visible = True
        self._said = self._said or bool(text.strip())
        if count:
            self.out_chars += len(text)
        ev.append(("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                           "delta": {"type": "text_delta", "text": text}}))
        return ev

    def _thinking(self, text: str) -> List[Event]:
        if not text:
            return []
        self.out_chars += len(text)
        # Kept (bounded) while it may be needed: as the answer when thinking is off (see finish), and for the
        # tool-call rescue of hosts that put the whole reply into reasoning.
        if (self.rescue_reasoning or not self.emit_thinking) and self._reason_len < 2_000_000:
            self._reason.append(text)
            self._reason_len += len(text)
        if not self.emit_thinking:
            return []
        ev: List[Event] = []
        if self.open_type != "thinking":
            ev += self._open("thinking", {"type": "thinking", "thinking": "", "signature": ""})
        ev.append(("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                           "delta": {"type": "thinking_delta", "thinking": text}}))
        return ev

    def _visible(self, text: str) -> List[Event]:
        if self._parser is None:
            return self._text(text)
        self.out_chars += len(text)   # counted on the way in: held or rescued text is output too
        return self._items(self._parser.feed(text))

    def _items(self, items: List[Item]) -> List[Event]:
        ev: List[Event] = []
        for it in items:
            if it[0] == "text":
                ev += self._text(it[1], count=False)
            else:
                ev += self._emit_call(it[1], it[2])
        return ev

    def _content(self, text: str) -> List[Event]:
        """Route inline <think> tags to thinking, the rest to visible text."""
        data = self._tag_buf + text
        self._tag_buf = ""
        ev: List[Event] = []
        while data:
            tag = "</think>" if self._think_tag else "<think>"
            pos = data.find(tag)
            if pos >= 0:
                chunk, data = data[:pos], data[pos + len(tag):]
                ev += self._thinking(chunk) if self._think_tag else self._visible(chunk)
                self._think_tag = not self._think_tag
                if not self._think_tag:
                    data = data.lstrip("\n")
                continue
            keep = 0
            for k in range(min(len(tag) - 1, len(data)), 0, -1):
                if tag.startswith(data[-k:]):
                    keep = k
                    break
            chunk = data[:len(data) - keep] if keep else data
            self._tag_buf = data[len(data) - keep:] if keep else ""
            ev += self._thinking(chunk) if self._think_tag else self._visible(chunk)
            data = ""
        return ev

    def _drain(self) -> List[Event]:
        """Settle text held back for tag detection (a tool call or the end of the stream follows)."""
        ev: List[Event] = []
        if self._tag_buf:
            buf, self._tag_buf = self._tag_buf, ""
            ev += self._thinking(buf) if self._think_tag else self._visible(buf)
        if self._parser is not None:
            ev += self._items(self._parser.flush())
        return ev

    def _route(self, kind: str, text: str) -> List[Event]:
        """Text or reasoning, kept in order with tool calls that are still streaming."""
        if self._deferred or any(t.streaming for t in self._pending):
            if text.strip():
                self._deferred.append((kind, text))
            return []
        ev: List[Event] = []
        if self._pending:
            if not text.strip():
                return ev            # whitespace between tool calls
            ev += self._settle(True)
        return ev + (self._thinking(text) if kind == "think" else self._content(text))

    # -- tool calls ------------------------------------------------------------
    def _emit_call(self, name: str, args: Any) -> List[Event]:
        name, args = self.toolset.fix(name, args)
        try:
            key = name + "\x00" + json.dumps(args, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            args = {}
            key = name + "\x00{}"
        if key in self._seen:
            return []                 # an upstream that repeats a finished call verbatim
        self._seen.add(key)
        ev = self._open("tool_use", {"type": "tool_use", "id": new_id("toolu_"), "name": name, "input": {}})
        ev.append(("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                           "delta": {"type": "input_json_delta", "partial_json": _dumps(args)}}))
        ev += self._close()
        self.tools_emitted += 1
        return ev

    def _target(self, cid: Optional[str], idx: Optional[int], name: Optional[str]) -> _Tool:
        st: Optional[_Tool] = None
        last = self._last if self._last is not None and not self._last.emitted else None
        if idx is not None:
            st = self._by_idx.get(idx)
            if st is not None and cid and st.uid and cid != st.uid and (name or st.scan.state == 2):
                st = None                 # a new call reusing the index
            elif st is None and last is not None and last.idx is None and not name and last.scan.state != 2:
                st = last                 # the index only showed up on a continuation chunk
                st.idx = idx
                self._by_idx[idx] = st
        elif cid is not None:
            st = self._by_id.get(cid)
            if st is None and last is not None and last.idx is None and not name and last.scan.state != 2:
                st = last                 # an unstable id on a continuation chunk
        else:
            st = last
        if st is not None and name and st.name and st.scan.state in (2, 3) and name != st.name:
            st = None                     # a different call announced in the same slot
        if st is None:
            st = _Tool(cid, idx)
            self._pending.append(st)
            if idx is not None:
                self._by_idx[idx] = st
        if cid and not st.uid:
            st.uid = cid
        if cid and cid not in self._by_id:
            self._by_id[cid] = st
        self._last = st
        return st

    def _feed_args(self, st: _Tool, frag: str) -> None:
        while frag:
            if st.scan.state == 2:
                m = _NEXT_CALL.match(frag)
                if m is None:
                    if frag.strip():
                        st.parts.append(frag)   # trailing junk: the lenient parser copes
                    return
                # A second JSON object in the same slot: a parallel call the host glued on.
                frag = frag[m.end():]
                nxt = _Tool(st.uid, st.idx, st.hint or st.name)
                st.hint = None
                self._pending.append(nxt)
                if st.idx is not None and self._by_idx.get(st.idx) is st:
                    self._by_idx[st.idx] = nxt
                if st.uid and self._by_id.get(st.uid) is st:
                    self._by_id[st.uid] = nxt
                self._last = st = nxt
                continue
            end = st.scan.feed(frag)
            if end < 0:
                st.parts.append(frag)
                return
            st.parts.append(frag[:end])
            frag = frag[end:]

    def _tool_delta(self, call: Dict[str, Any]) -> List[Event]:
        fn = call.get("function") if isinstance(call.get("function"), dict) else {}
        raw_id = call.get("id")
        cid = str(raw_id) if isinstance(raw_id, (str, int)) and not isinstance(raw_id, bool) and str(raw_id) else None
        idx = call.get("index")
        if isinstance(idx, str):
            idx = _int(idx)
        if not isinstance(idx, int) or isinstance(idx, bool):
            idx = None
        name = fn.get("name") if fn.get("name") is not None else call.get("name")
        name = name.strip() if isinstance(name, str) and name.strip() else None
        args = fn.get("arguments") if "arguments" in fn else call.get("arguments")
        if args is None:
            args = ""
        elif not isinstance(args, str):
            args = _dumps(args)
        if not (cid or name or args):
            return []
        self.active = True
        ev = self._drain()
        st = self._target(cid, idx, name)
        if name:
            if not st.name:
                st.name = name
            elif st.scan.state in (2, 3):
                st.hint = name            # maybe the next call in this slot; it opens when its arguments do
        if args:
            self.out_chars += len(args)
            self._feed_args(st, args)
        return ev

    def _forget(self, st: _Tool) -> None:
        st.emitted = True
        if st.idx is not None and self._by_idx.get(st.idx) is st:
            del self._by_idx[st.idx]
        for k in [k for k, v in self._by_id.items() if v is st]:
            del self._by_id[k]

    def _settle(self, final: bool, clean: bool = True) -> List[Event]:
        """Emit finished tool calls in order (all of them when final).

        A call whose arguments never closed is NEVER completed into a valid one and run: repairing
        `rm -rf ./build/cache && ...` into `rm -rf ./build`, or a Write into a cut-off file, is worse
        than running nothing. It becomes a visible note instead. `clean` is False when the stream
        was cut or hit its output limit: then even a call with no arguments yet is withheld.
        """
        ev: List[Event] = []
        while self._pending:
            st = self._pending[0]
            if not final and (st is self._last or st.scan.state != 2):
                break
            self._pending.pop(0)
            self._forget(st)
            raw = st.args
            if not st.name and not raw.strip():
                continue
            if st.scan.state == 2:
                args: Optional[Dict[str, Any]] = parse_arguments(raw)
            elif not raw.strip():
                args = {} if clean else None
            else:
                args = complete_arguments(raw)
            if args is None:
                name = self.toolset.fix(st.name or "", {})[0] if st.name else _guess_name(raw)
                self.cut_calls.append(name or "a tool")
                ev += self._text(("\n\n" if self.visible else "") + cut_note(name, limit=self.stop_reason == "max_tokens"))
                continue
            name = st.name or self.toolset.infer(args) or "unknown_tool"
            ev += self._emit_call(name, args)
        if self._deferred and not any(t.streaming for t in self._pending):
            held, self._deferred = self._deferred, []
            for kind, text in held:
                ev += self._route(kind, text)
        return ev

    def _usage(self, u: Any) -> None:
        if not isinstance(u, dict):
            return
        out = _int(u.get("completion_tokens"))
        if out is None:
            out = _int(u.get("output_tokens"))
        if out is not None:
            self.usage_out = out
        cached = _cached_tokens(u)
        if cached is not None:
            self.cache_read, self._cache_known = cached, True
        write = _cache_write_tokens(u)
        if write is not None:
            self.cache_creation = write
        pin = _int(u.get("prompt_tokens"))
        if pin is None:
            pin = _int(u.get("input_tokens"))
        if pin is not None:
            # Anthropic counts cache reads and writes apart from input_tokens.
            self.usage_in = max(0, pin - (self.cache_read if self._cache_known else 0) - self.cache_creation)
        for k in ("cost", "total_cost"):
            c = _float(u.get(k))
            if c is not None:
                self.cost = c
                break

    # -- feeding -------------------------------------------------------------
    def feed(self, chunk: Dict[str, Any]) -> List[Event]:
        if not isinstance(chunk, dict):
            return []
        self._usage(chunk.get("usage"))
        if isinstance(chunk.get("x_groq"), dict):
            self._usage(chunk["x_groq"].get("usage"))
        choice = _pick_choice(chunk.get("choices"))
        if choice is None:
            return []
        self._usage(choice.get("usage"))
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            delta = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        ev: List[Event] = []
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            self.active = True
            ev += self._route("think", reasoning)
        for kind, text in _content_parts(delta.get("content")):
            self.active = True
            ev += self._route("think" if kind == "think" else "content", text)
        refusal = delta.get("refusal")
        if isinstance(refusal, str) and refusal:
            self.active = True
            ev += self._route("content", refusal)
        calls = delta.get("tool_calls")
        if isinstance(calls, dict):
            calls = [calls]
        for call in calls if isinstance(calls, list) else []:
            if isinstance(call, dict):
                ev += self._tool_delta(call)
        fc = delta.get("function_call")
        if isinstance(fc, dict):
            ev += self._tool_delta({"function": fc})
        fr = choice.get("finish_reason")
        if isinstance(fr, str) and fr:
            self.stop_reason = STOP_MAP.get(fr, "end_turn")
            self._finish_reason = fr
        if self._pending:
            ev += self._settle(False)
        return ev

    def _rescue_reasoning(self) -> List[Event]:
        """A host that swallowed the whole reply into reasoning, tool call included (only with rescue_reasoning:
        elsewhere markup in reasoning is a plan or a quote). The same position rules as for text apply."""
        if not self.rescue_reasoning or self.tools_emitted or self.visible or not self._reason:
            return []
        text = "".join(self._reason)
        if not _OPEN_RE.search(text):
            return []
        parser = TextToolParser(self.toolset, bare_json=False)
        ev: List[Event] = []
        for it in parser.feed(text) + parser.flush():
            if it[0] == "call":
                ev += self._emit_call(it[1], it[2])
        return ev

    def finish(self, notice: Optional[str] = None) -> List[Event]:
        clean = (not notice and bool(self._finish_reason) and self.stop_reason in ("tool_use", "end_turn")
                 and self._finish_reason != "content_filter")
        ev = self._drain()
        tool_last = bool(self._pending)          # the reply ended inside tool calls, not inside text
        cut_before = len(self.cut_calls)
        ev += self._settle(True, clean)
        ev += self._drain()
        ev += self._rescue_reasoning()
        # The output cap cut only a tool call (dropped above, with a note): see final_stop.
        limit_cut_tool = self.stop_reason == "max_tokens" and tool_last and len(self.cut_calls) > cut_before
        if notice:
            if self.open_type == "text":
                ev += self._text("\n\n" + notice)
            else:
                ev += self._text(notice)
        elif not self._said and not self.tools_emitted and not self.emit_thinking and self._reason:
            # The whole answer came as reasoning (some hosts' reasoning parsers swallow it) and the client did
            # not ask for thinking: it is the answer, never an empty reply. Shown as written, never searched
            # for tool calls.
            ev += self._text("".join(self._reason).strip(), count=False)
        if self.index < 0:
            ev += self._open("text", {"type": "text", "text": ""})
        ev += self._close()
        stop = final_stop(self.stop_reason, self.tools_emitted, bool(notice), limit_cut_tool)
        usage: Dict[str, int] = {"output_tokens": self.usage_out if self.usage_out is not None else max(1, self.out_chars // 4)}
        if self.usage_in is not None:
            usage["input_tokens"] = self.usage_in
        if self._cache_known:
            usage["cache_read_input_tokens"] = self.cache_read
        if self.cache_creation:
            usage["cache_creation_input_tokens"] = self.cache_creation
        ev.append(("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                                     "usage": usage}))
        ev.append(("message_stop", {"type": "message_stop"}))
        return ev


def to_anthropic_message(data: Dict[str, Any], model: str, input_estimate: int = 0, tools: Any = None, *,
                         rescue: Optional[bool] = None, rescue_reasoning: bool = False) -> Dict[str, Any]:
    """A non-streamed chat completion -> an Anthropic message (same repairs as the stream path; rescue and
    rescue_reasoning as for StreamTranslator)."""
    data = data if isinstance(data, dict) else {}
    choice = _pick_choice(data.get("choices")) or {}
    msg = choice.get("message") if isinstance(choice.get("message"), dict) else (
        choice.get("delta") if isinstance(choice.get("delta"), dict) else {})
    msg = dict(msg)
    if isinstance(msg.get("content"), str):
        thought, msg["content"] = split_think(msg["content"])
        if thought:
            # Reasoning, like reasoning_content: the answer when nothing else came (see StreamTranslator.finish).
            before = msg.get("reasoning_content") or msg.get("reasoning")
            msg["reasoning_content"] = before + "\n\n" + thought if isinstance(before, str) and before else thought
    tr = StreamTranslator(model, input_tokens=input_estimate, emit_thinking=False, tools=tools, rescue=rescue,
                          rescue_reasoning=rescue_reasoning)
    tr.start()
    events = tr.feed({"choices": [dict(choice, delta=msg, message=None)], "usage": data.get("usage")})
    events += tr.finish()
    content: List[Dict[str, Any]] = []
    stop = "end_turn"
    for name, p in events:
        if name == "content_block_start":
            content.append(dict(p["content_block"]))
        elif name == "content_block_delta" and content:
            d = p["delta"]
            if d["type"] == "text_delta":
                content[-1]["text"] = content[-1].get("text", "") + d["text"]
            elif d["type"] == "input_json_delta":
                content[-1]["input"] = json.loads(d["partial_json"])
        elif name == "message_delta":
            stop = p["delta"]["stop_reason"]
    kept = [b for b in content if b.get("type") != "text" or b.get("text", "").strip()]
    content = kept or [{"type": "text", "text": ""}]
    usage: Dict[str, Any] = {"input_tokens": tr.usage_in if tr.usage_in is not None else int(input_estimate),
                             "output_tokens": tr.usage_out or max(1, tr.out_chars // 4)}
    if tr._cache_known:
        usage["cache_read_input_tokens"] = tr.cache_read
    if tr.cache_creation:
        usage["cache_creation_input_tokens"] = tr.cache_creation
    return {
        "id": new_id("msg_"), "type": "message", "role": "assistant", "model": model,
        "content": content, "stop_reason": stop, "stop_sequence": None, "usage": usage,
    }


def text_message(text: str, model: str) -> Dict[str, Any]:
    return {"id": new_id("msg_"), "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": max(1, len(text) // 4)}}


def sse(event: Event) -> str:
    name, payload = event
    return "event: " + name + "\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
