# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Scripted model providers for the Claude Code compatibility harness (stdlib only).

Two loopback servers stand in for real providers:

  * OpenAIUpstream: an OpenAI chat-completions endpoint (GET {base}/models, POST
    {base}/chat/completions), the wire Pulsar Core translates;
  * AnthropicUpstream: an Anthropic Messages endpoint (GET {base}/v1/models, POST
    {base}/v1/messages and {base}/v1/messages/count_tokens), the wire Core forwards verbatim.

Both play the same short agent script, decided from the request alone:

  1. a request that declares the Bash tool and whose user text carries MARKER is answered with
     one Bash tool call running `probe_command`;
  2. a request whose last message is that tool's result is answered with streamed text that ends
     in "COMPAT-OK <nonce>", and the probe output found in the result is recorded;
  3. anything else (Claude Code's side requests: titles, topic checks) gets a short "ok".

Only keys that contain "fake-key-not-real" are accepted (401 otherwise), so a real key that
reaches them by mistake is refused. Every request is recorded in `log` for the harness.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

MARKER = "COMPAT-TURN"
PROBE_TAG = "COMPAT_PROBE"
FAKE_KEY = "fake-key-not-real-compat"
_PROBE_RE = re.compile(PROBE_TAG + r" (\{.*?\})\s*(?:$|\n)", re.S)



def realistic_usage(body: Dict[str, Any]) -> Dict[str, int]:
    """Prompt tokens that grow with the request (about 4 characters per token), so context checks see
    Claude Code's real prefix instead of a constant."""
    prompt = max(1, len(json.dumps(body)) // 4)
    return {"prompt_tokens": prompt, "completion_tokens": 8, "total_tokens": prompt + 8}

def final_words(nonce: str) -> List[str]:
    """The final answer, split into the pieces each upstream streams one by one."""
    return ["The probe ", "ran through ", "Pulsar Core. ", "COMPAT-OK ", nonce]


def text_of(content: Any) -> str:
    """Plain text of an OpenAI or Anthropic message content (string or list of parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict):
                if isinstance(part.get("text"), str):
                    out.append(part["text"])
                elif "content" in part:
                    out.append(text_of(part.get("content")))
            elif isinstance(part, str):
                out.append(part)
        return "\n".join(out)
    return ""


def parse_probe(text: str) -> Optional[Dict[str, Any]]:
    """The JSON object the probe printed after PROBE_TAG, or None."""
    m = _PROBE_RE.search(text or "")
    if not m:
        return None
    try:
        obj = json.loads(m.group(1))
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _Base:
    """Shared bookkeeping: the script's inputs and a thread-safe request log."""

    dialect = ""

    def __init__(self, model: str, nonce: str, probe_command: str, context: int = 0,
                 extra_models: Optional[List[Any]] = None) -> None:
        self.model = model
        self.nonce = nonce
        self.probe_command = probe_command
        self.context = context
        self.extra_models = [(str(m), int(c)) for m, c in (extra_models or [])]   # more catalog rows: (id, window)
        self.log: List[Dict[str, Any]] = []
        self.probe: Optional[Dict[str, Any]] = None
        self.tool_calls = 0
        self.tool_results = 0
        self._lock = threading.Lock()
        self._server: Optional[_Server] = None
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle -------------------------------------------------------------
    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    def start(self) -> "_Base":
        self._server = _Server(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.1},
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def __enter__(self) -> "_Base":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    def record(self, entry: Dict[str, Any]) -> None:
        with self._lock:
            self.log.append(entry)

    # -- the script ------------------------------------------------------------
    def decide(self, tool_names: List[str], user_text: str, tool_result: Optional[str]) -> str:
        """'final' after the tool result, 'tool' for the scripted turn, 'plain' otherwise."""
        if tool_result is not None:
            with self._lock:
                self.tool_results += 1
                found = parse_probe(tool_result)
                if found is not None:
                    self.probe = found
            return "final"
        if "Bash" in tool_names and MARKER in user_text:
            with self._lock:
                self.tool_calls += 1
            return "tool"
        return "plain"

    def tool_input(self) -> Dict[str, Any]:
        return {"command": self.probe_command, "description": "Pulsar compat probe"}

    def _handler(self):  # pragma: no cover - overridden
        raise NotImplementedError


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "compat-upstream"
    sys_version = ""
    owner: _Base

    def log_message(self, *a: Any) -> None:
        pass

    def authorized(self) -> bool:
        auth = self.headers.get("authorization", "") + " " + self.headers.get("x-api-key", "")
        return "fake-key-not-real" in auth

    def body(self) -> Any:
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {}

    def reply(self, status: int, obj: Any) -> None:
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def stream_start(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def chunk(self, data: bytes) -> None:
        self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
        self.wfile.flush()

    def stream_end(self) -> None:
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


# ---------------------------------------------------------------------------
# OpenAI chat-completions
# ---------------------------------------------------------------------------

class OpenAIUpstream(_Base):
    dialect = "openai-chat"

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:%d/v1" % self.port

    def _handler(self):
        owner = self

        class H(_Handler):
            def do_GET(self) -> None:
                owner.record({"method": "GET", "path": self.path})
                if not self.path.rstrip("/").endswith("/models"):
                    return self.reply(404, {"error": {"message": "not found"}})
                if not self.authorized():
                    return self.reply(401, {"error": {"message": "compat upstream only accepts fake keys"}})
                self.reply(200, {"object": "list", "data": [
                    {"id": m, "object": "model", "owned_by": "compat", "context_length": c}
                    for m, c in [(owner.model, owner.context)] + owner.extra_models]})

            def do_POST(self) -> None:
                body = self.body()
                path = self.path.rstrip("/")
                if not path.endswith("/chat/completions"):
                    owner.record({"method": "POST", "path": self.path})
                    return self.reply(404, {"error": {"message": "not found"}})
                if not self.authorized():
                    owner.record({"method": "POST", "path": self.path, "status": 401})
                    return self.reply(401, {"error": {"message": "compat upstream only accepts fake keys"}})
                body = body if isinstance(body, dict) else {}
                msgs = [m for m in body.get("messages") or [] if isinstance(m, dict)]
                tools = [str((t.get("function") or {}).get("name", "")) for t in body.get("tools") or []
                         if isinstance(t, dict)]
                last = msgs[-1] if msgs else {}
                result = text_of(last.get("content")) if last.get("role") == "tool" else None
                user = "\n".join(text_of(m.get("content")) for m in msgs if m.get("role") == "user")
                action = owner.decide(tools, user, result)
                owner.record({"method": "POST", "path": self.path, "model": body.get("model"),
                              "stream": bool(body.get("stream")), "tools": len(tools), "action": action})
                if not body.get("stream"):
                    return self.plain_json(body, action)
                self.stream(body, action)

            def plain_json(self, body: Dict[str, Any], action: str) -> None:
                msg: Dict[str, Any] = {"role": "assistant", "content": "ok"}
                finish = "stop"
                if action == "final":
                    msg["content"] = "".join(final_words(owner.nonce))
                elif action == "tool":
                    msg = {"role": "assistant", "content": None, "tool_calls": [
                        {"id": "call_compat_1", "type": "function",
                         "function": {"name": "Bash", "arguments": json.dumps(owner.tool_input())}}]}
                    finish = "tool_calls"
                self.reply(200, {"id": "chatcmpl-compat", "object": "chat.completion",
                                 "model": body.get("model") or owner.model,
                                 "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                                 "usage": realistic_usage(body)})

            def stream(self, body: Dict[str, Any], action: str) -> None:
                model = body.get("model") or owner.model

                def delta(d: Dict[str, Any], finish: Optional[str] = None) -> bytes:
                    obj = {"id": "chatcmpl-compat", "object": "chat.completion.chunk", "model": model,
                           "choices": [{"index": 0, "delta": d, "finish_reason": finish}]}
                    return ("data: " + json.dumps(obj) + "\n\n").encode()

                self.stream_start()
                self.chunk(delta({"role": "assistant", "content": ""}))
                if action == "tool":
                    args = json.dumps(owner.tool_input())
                    half = len(args) // 2
                    self.chunk(delta({"tool_calls": [{"index": 0, "id": "call_compat_1", "type": "function",
                                                      "function": {"name": "Bash", "arguments": args[:half]}}]}))
                    self.chunk(delta({"tool_calls": [{"index": 0, "function": {"arguments": args[half:]}}]}))
                    self.chunk(delta({}, "tool_calls"))
                else:
                    words = final_words(owner.nonce) if action == "final" else ["ok"]
                    for w in words:
                        self.chunk(delta({"content": w}))
                    self.chunk(delta({}, "stop"))
                usage = realistic_usage(body)
                self.chunk(("data: " + json.dumps({"id": "chatcmpl-compat", "object": "chat.completion.chunk",
                                                   "model": model, "choices": [], "usage": usage}) + "\n\n").encode())
                self.chunk(b"data: [DONE]\n\n")
                self.stream_end()

        return H


# ---------------------------------------------------------------------------
# Anthropic Messages
# ---------------------------------------------------------------------------

def _anthropic_tool_result(msgs: List[Dict[str, Any]]) -> Optional[str]:
    """Text of the tool_result blocks in the last user message, or None when there are none."""
    if not msgs or msgs[-1].get("role") != "user" or not isinstance(msgs[-1].get("content"), list):
        return None
    found = [b for b in msgs[-1]["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]
    if not found:
        return None
    return "\n".join(text_of(b.get("content")) for b in found)


class AnthropicUpstream(_Base):
    dialect = "anthropic-messages"

    @property
    def base_url(self) -> str:
        # Pulsar treats a custom URL that ends in /anthropic as an Anthropic Messages endpoint.
        return "http://127.0.0.1:%d/anthropic" % self.port

    def _handler(self):
        owner = self

        class H(_Handler):
            def do_GET(self) -> None:
                owner.record({"method": "GET", "path": self.path})
                if not self.path.rstrip("/").endswith("/models"):
                    return self.reply(404, {"type": "error", "error": {"type": "not_found_error",
                                                                       "message": "not found"}})
                if not self.authorized():
                    return self.reply(401, {"type": "error", "error": {"type": "authentication_error",
                                                                       "message": "fake keys only"}})
                self.reply(200, {"data": [{"id": owner.model, "type": "model", "display_name": owner.model,
                                           "created_at": "2026-01-01T00:00:00Z"}],
                                 "has_more": False, "first_id": owner.model, "last_id": owner.model})

            def do_POST(self) -> None:
                body = self.body()
                body = body if isinstance(body, dict) else {}
                path = self.path.split("?", 1)[0].rstrip("/")
                if not self.authorized():
                    owner.record({"method": "POST", "path": self.path, "status": 401})
                    return self.reply(401, {"type": "error", "error": {"type": "authentication_error",
                                                                       "message": "fake keys only"}})
                if path.endswith("/v1/messages/count_tokens"):
                    owner.record({"method": "POST", "path": self.path, "action": "count_tokens"})
                    return self.reply(200, {"input_tokens": 42})
                if not path.endswith("/v1/messages"):
                    owner.record({"method": "POST", "path": self.path})
                    return self.reply(404, {"type": "error", "error": {"type": "not_found_error",
                                                                       "message": "not found"}})
                msgs = [m for m in body.get("messages") or [] if isinstance(m, dict)]
                tools = [str(t.get("name", "")) for t in body.get("tools") or [] if isinstance(t, dict)]
                user = "\n".join(text_of(m.get("content")) for m in msgs if m.get("role") == "user")
                action = owner.decide(tools, user, _anthropic_tool_result(msgs))
                owner.record({"method": "POST", "path": self.path, "model": body.get("model"),
                              "stream": bool(body.get("stream")), "tools": len(tools), "action": action})
                if not body.get("stream"):
                    return self.plain_json(body, action)
                self.stream(body, action)

            def blocks(self, action: str) -> List[Dict[str, Any]]:
                if action == "tool":
                    return [{"type": "tool_use", "id": "toolu_compat_1", "name": "Bash",
                             "input": owner.tool_input()}]
                text = "".join(final_words(owner.nonce)) if action == "final" else "ok"
                return [{"type": "text", "text": text}]

            def plain_json(self, body: Dict[str, Any], action: str) -> None:
                self.reply(200, {"id": "msg_compat", "type": "message", "role": "assistant",
                                 "model": body.get("model") or owner.model, "content": self.blocks(action),
                                 "stop_reason": "tool_use" if action == "tool" else "end_turn",
                                 "stop_sequence": None, "usage": {"input_tokens": 120, "output_tokens": 8}})

            def stream(self, body: Dict[str, Any], action: str) -> None:
                def ev(name: str, data: Dict[str, Any]) -> bytes:
                    data = dict(data, type=name)
                    return ("event: %s\ndata: %s\n\n" % (name, json.dumps(data))).encode()

                self.stream_start()
                self.chunk(ev("message_start", {"message": {
                    "id": "msg_compat", "type": "message", "role": "assistant",
                    "model": body.get("model") or owner.model, "content": [], "stop_reason": None,
                    "stop_sequence": None, "usage": {"input_tokens": 120, "output_tokens": 0}}}))
                if action == "tool":
                    args = json.dumps(owner.tool_input())
                    half = len(args) // 2
                    self.chunk(ev("content_block_start", {"index": 0, "content_block": {
                        "type": "tool_use", "id": "toolu_compat_1", "name": "Bash", "input": {}}}))
                    for part in (args[:half], args[half:]):
                        self.chunk(ev("content_block_delta", {"index": 0, "delta": {
                            "type": "input_json_delta", "partial_json": part}}))
                    stop = "tool_use"
                else:
                    words = final_words(owner.nonce) if action == "final" else ["ok"]
                    self.chunk(ev("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}))
                    for w in words:
                        self.chunk(ev("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": w}}))
                    stop = "end_turn"
                self.chunk(ev("content_block_stop", {"index": 0}))
                self.chunk(ev("message_delta", {"delta": {"stop_reason": stop, "stop_sequence": None},
                                                "usage": {"output_tokens": 8}}))
                self.chunk(ev("message_stop", {}))
                self.stream_end()

        return H
