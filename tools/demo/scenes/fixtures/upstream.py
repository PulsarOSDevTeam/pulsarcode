#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Scripted stand-in for model providers, for the README recordings only (stdlib only).

One loopback port plays two roles:

  * an OpenAI-compatible server: GET .../models, POST .../chat/completions;
  * an HTTPS proxy for HTTPS_PROXY that answers CONNECT only for the provider hosts in HOSTS and
    terminates TLS with a throwaway demo CA (trusted through PULSAR_CA_BUNDLE in the recorded
    processes only). Pulsar's real code paths (key detection, the catalog request, Core's router
    and relay) run unchanged, and nothing ever leaves the machine: every other host is refused.

Only keys that contain "fake" are accepted, so a real key pasted by mistake is rejected (401) and
never forwarded anywhere.

    upstream.py --port N --state DIR [--project DIR] [--mode agent|traffic] [--seed N]

--state holds the generated CA (ca.pem) and leaf certificate; --mode agent scripts one short
coding turn for the session recording (read README.md, then summarize it), --mode traffic serves
variable latency, occasional rate limits, one unhealthy provider and a learnable max_tokens limit
for the Observatory recording.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Provider hosts the proxy role terminates. Anything else gets 403 and no connection.
HOSTS = ("openrouter.ai", "api.groq.com", "integrate.api.nvidia.com", "api.cerebras.ai", "api.deepseek.com",
         "api.moonshot.ai", "api.z.ai")

# A demo OpenRouter catalog (OpenRouter's /models shape). Ids and windows are illustrative.
OPENROUTER = [
    ("moonshotai/kimi-k3", "MoonshotAI: Kimi K3", 1048576, False, True),
    ("moonshotai/kimi-k2.6", "MoonshotAI: Kimi K2.6", 262144, False, True),
    ("moonshotai/kimi-k2.6:free", "MoonshotAI: Kimi K2.6 (free)", 262144, True, True),
    ("moonshotai/kimi-k2-thinking", "MoonshotAI: Kimi K2 Thinking", 262144, False, True),
    ("z-ai/glm-5.3", "Z.ai: GLM 5.3", 204800, False, True),
    ("z-ai/glm-5.3-flash", "Z.ai: GLM 5.3 Flash", 204800, False, True),
    ("deepseek/deepseek-v4-pro", "DeepSeek: V4 Pro", 163840, False, True),
    ("deepseek/deepseek-v4.1-flash", "DeepSeek: V4.1 Flash", 163840, False, True),
    ("qwen/qwen3-coder-plus", "Qwen: Qwen3 Coder Plus", 1000000, False, True),
    ("qwen/qwen3.5-397b-a17b", "Qwen: Qwen3.5 397B A17B", 262144, False, True),
    ("minimax/minimax-m3", "MiniMax: M3", 1000000, False, True),
    ("openai/gpt-oss-120b", "OpenAI: gpt-oss-120b", 131072, False, True),
    ("openai/gpt-oss-20b:free", "OpenAI: gpt-oss-20b (free)", 131072, True, True),
    ("google/gemini-3.5-flash", "Google: Gemini 3.5 Flash", 1048576, False, True),
    ("x-ai/grok-4.6", "xAI: Grok 4.6", 256000, False, True),
    ("mistralai/devstral-2", "Mistral: Devstral 2", 262144, False, True),
    ("meta-llama/llama-4-maverick", "Meta: Llama 4 Maverick", 1048576, False, True),
    ("nvidia/nemotron-3-super-120b-a12b", "NVIDIA: Nemotron 3 Super", 262144, False, True),
    ("google/gemma-4-31b-it:free", "Google: Gemma 4 31B (free)", 131072, True, False),
]
SMALL = {  # tiny catalogs for the other hosts (OpenAI /models shape)
    "api.groq.com": ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.3-70b-versatile"],
    "integrate.api.nvidia.com": ["moonshotai/kimi-k3", "z-ai/glm-5.3", "deepseek-ai/deepseek-v4.1-flash"],
    "api.cerebras.ai": ["gpt-oss-120b", "qwen-3.8-27b"],
}

ARGS = argparse.Namespace()
RNG = random.Random(7)
RNG_LOCK = threading.Lock()


def rand(a: float, b: float) -> float:
    with RNG_LOCK:
        return RNG.uniform(a, b)


def chance(p: float) -> bool:
    with RNG_LOCK:
        return RNG.random() < p


# ---------------------------------------------------------------------------
# throwaway certificates (openssl CLI; nothing here is ever reused)
# ---------------------------------------------------------------------------

def ensure_certs(state: str) -> ssl.SSLContext:
    ca, ca_key = os.path.join(state, "ca.pem"), os.path.join(state, "ca.key")
    leaf, leaf_key = os.path.join(state, "leaf.pem"), os.path.join(state, "leaf.key")
    if not (os.path.exists(leaf) and os.path.exists(ca)):
        def run(*cmd: str) -> None:
            subprocess.run(list(cmd), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        run("openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
            "-keyout", ca_key, "-out", ca, "-days", "2", "-subj", "/CN=Pulsar Code demo CA (throwaway)",
            "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
            "-addext", "subjectKeyIdentifier=hash")
        csr = os.path.join(state, "leaf.csr")
        run("openssl", "req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
            "-keyout", leaf_key, "-out", csr, "-subj", "/CN=demo upstream")
        ext = os.path.join(state, "leaf.ext")
        with open(ext, "w") as f:
            f.write("basicConstraints=critical,CA:FALSE\n"
                    "keyUsage=critical,digitalSignature,keyEncipherment\n"
                    "extendedKeyUsage=serverAuth\n"
                    "subjectKeyIdentifier=hash\n"
                    "authorityKeyIdentifier=keyid,issuer\n"
                    "subjectAltName=" + ",".join("DNS:" + h for h in HOSTS) + "\n")
        run("openssl", "x509", "-req", "-in", csr, "-CA", ca, "-CAkey", ca_key, "-CAcreateserial",
            "-out", leaf, "-days", "2", "-extfile", ext)
        os.unlink(ca_key)                # nothing can be signed with this CA again
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(leaf, leaf_key)
    return ctx


# ---------------------------------------------------------------------------
# scripted replies
# ---------------------------------------------------------------------------

def sse(obj) -> bytes:
    return ("data: " + json.dumps(obj) + "\n\n").encode()


def delta(d, finish=None, model="demo") -> bytes:
    return sse({"id": "chatcmpl-demo", "object": "chat.completion.chunk", "model": model,
                "choices": [{"index": 0, "delta": d, "finish_reason": finish}]})


def text_of(content) -> str:
    if isinstance(content, list):
        return " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return str(content or "")


SUMMARY = ("**orbit** predicts satellite passes for your location, from the command line.\n\n"
           "- `orbit next --city Montreal`: the next visible pass, its direction and peak\n"
           "- `orbit watch ISS` streams live positions as the satellite crosses the sky\n"
           "- passes are computed locally from cached orbital data; nothing is uploaded\n\n"
           "The entry point is `orbit/cli.py`.")


def agent_turn(body):
    """(text, tool_call or None) for the session recording."""
    msgs = body.get("messages") or []
    tools = [t.get("function", {}).get("name", "") for t in body.get("tools") or []]
    last = msgs[-1] if msgs else {}
    if last.get("role") == "tool":
        return SUMMARY, None
    if "Read" in tools and ARGS.project:
        args = {"file_path": os.path.join(ARGS.project, "README.md")}
        return "Let me read the README first.", ("Read", args)
    return "Hello from the demo upstream.", None


WORDS = ("the router keeps every session on its plan and relays before the first byte when a provider "
         "is busy so the answer keeps streaming while the observatory watches each request ").split()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "demo-upstream"
    sys_version = ""
    tunnel_host = ""

    def log_message(self, *a) -> None:  # quiet: the recording owns the terminal
        pass

    # -- proxy role -----------------------------------------------------------
    def do_CONNECT(self) -> None:
        host = self.path.rsplit(":", 1)[0].strip("[]").lower()
        if host not in HOSTS or self.server.tls is None:
            self.send_response(403, "demo upstream: host not allowed")
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            return
        self.send_response(200, "Connection established")
        self.end_headers()
        self.wfile.flush()
        try:
            conn = self.server.tls.wrap_socket(self.connection, server_side=True)
        except (ssl.SSLError, OSError):
            self.close_connection = True
            return
        self.connection = conn
        self.rfile = conn.makefile("rb", -1)
        self.wfile = conn.makefile("wb", 0)
        self.tunnel_host = host
        self.close_connection = False

    # -- helpers --------------------------------------------------------------
    def host(self) -> str:
        return self.tunnel_host or "local"

    def authorized(self) -> bool:
        auth = self.headers.get("authorization", "") or self.headers.get("x-api-key", "")
        return "fake" in auth

    def reply(self, status: int, obj) -> None:
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if status == 429:
            self.send_header("Retry-After", "1")
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {}

    def stream_start(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def chunk(self, data: bytes) -> None:
        self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))

    def stream_end(self) -> None:
        self.wfile.write(b"0\r\n\r\n")

    # -- routes ---------------------------------------------------------------
    def proxied(self) -> bool:
        """A plain-http proxy request (absolute URI): refused, the demo never forwards anything."""
        if self.path.startswith("/"):
            return False
        self.reply(403, {"error": {"message": "demo upstream: forwarding is disabled"}})
        self.close_connection = True
        return True

    def do_GET(self) -> None:
        if self.proxied():
            return
        if self.path.rstrip("/").endswith("/models"):
            if not self.authorized():
                return self.reply(401, {"error": {"message": "demo upstream only accepts fake keys"}})
            if self.host() in ("openrouter.ai", "local"):
                data = [{"id": i, "name": n, "context_length": c, "created": 1790000000 - k * 86400,
                         "pricing": {"prompt": "0" if free else "0.0000006", "completion": "0" if free else "0.0000025"},
                         "supported_parameters": ["tools", "tool_choice", "max_tokens"] if tools else ["max_tokens"]}
                        for k, (i, n, c, free, tools) in enumerate(OPENROUTER)]
            else:
                data = [{"id": i, "object": "model"} for i in SMALL.get(self.host(), ["demo/model"])]
            return self.reply(200, {"data": data})
        self.reply(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        body = self.body()
        if self.proxied():
            return
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self.reply(404, {"error": {"message": "not found"}})
        if not self.authorized():
            return self.reply(401, {"error": {"message": "demo upstream only accepts fake keys"}})
        if ARGS.mode == "traffic":
            return self.traffic(body)
        return self.agent(body)

    def agent(self, body) -> None:
        model = body.get("model") or "demo"
        text, call = agent_turn(body)
        if not body.get("stream"):
            return self.reply(200, {"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                                 "finish_reason": "stop"}],
                                    "usage": {"prompt_tokens": 12, "completion_tokens": 1}})
        self.stream_start()
        self.chunk(delta({"role": "assistant"}, model=model))
        time.sleep(0.5)
        for word in re.findall(r"\S+\s*|\n+", text):
            self.chunk(delta({"content": word}, model=model))
            time.sleep(0.045)
        if call is not None:
            name, args = call
            self.chunk(delta({"tool_calls": [{"index": 0, "id": "call_demo_1", "type": "function",
                                              "function": {"name": name, "arguments": json.dumps(args)}}]},
                             model=model))
            self.chunk(delta({}, "tool_calls", model=model))
        else:
            self.chunk(delta({}, "stop", model=model))
        self.chunk(sse({"choices": [], "usage": {"prompt_tokens": 18400 if call is None else 17900,
                                                 "completion_tokens": 120 if call is None else 24,
                                                 "prompt_tokens_details": {"cached_tokens": 16384}}}))
        self.chunk(b"data: [DONE]\n\n")
        self.stream_end()

    def traffic(self, body) -> None:
        host = self.host()
        model = body.get("model") or "demo"
        if host == "integrate.api.nvidia.com" and chance(0.7):
            time.sleep(rand(0.05, 0.3))
            return self.reply(503, {"error": {"message": "upstream overloaded, try again later"}})
        if (body.get("max_tokens") or body.get("max_completion_tokens") or 0) > 16384 and host == "api.groq.com":
            return self.reply(400, {"error": {"message": "max_tokens must be less than or equal to 16384"}})
        if chance(0.06):
            return self.reply(429, {"error": {"message": "rate limited, slow down"}})
        ttft = rand(1.2, 3.0) if "think" in model else rand(0.25, 1.4)
        n = int(rand(30, 160))
        speed = rand(35, 110)
        if not body.get("stream"):
            time.sleep(ttft)
            return self.reply(200, {"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                                 "finish_reason": "stop"}],
                                    "usage": {"prompt_tokens": 1200, "completion_tokens": 2}})
        self.stream_start()
        self.chunk(delta({"role": "assistant"}, model=model))
        time.sleep(ttft)
        for i in range(n):
            self.chunk(delta({"content": WORDS[i % len(WORDS)] + " "}, model=model))
            time.sleep(1.0 / speed)
        self.chunk(delta({}, "stop", model=model))
        self.chunk(sse({"choices": [], "usage": {"prompt_tokens": int(rand(4000, 30000)), "completion_tokens": n,
                                                 "prompt_tokens_details": {"cached_tokens": int(rand(0, 20000))}}}))
        self.chunk(b"data: [DONE]\n\n")
        self.stream_end()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    tls = None


def main(argv) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--project", default="")
    ap.add_argument("--mode", choices=("agent", "traffic"), default="agent")
    ap.add_argument("--seed", type=int, default=7)
    ns = ap.parse_args(argv)
    ARGS.__dict__.update(vars(ns))
    RNG.seed(ns.seed)
    os.makedirs(ns.state, exist_ok=True)
    srv = Server(("127.0.0.1", ns.port), Handler)
    srv.tls = ensure_certs(ns.state)
    srv.socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    with open(os.path.join(ns.state, "upstream.ready"), "w") as f:
        f.write(str(os.getpid()))
    try:
        srv.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
