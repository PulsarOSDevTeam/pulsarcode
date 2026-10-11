# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pulsar import local as L
from pulsar import providers as P


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def handle_error(self, request, client_address):
        pass  # clients that time out and hang up are expected here


class Fake:
    """A loopback HTTP server answering fixed JSON per path, recording every request."""

    def __init__(self, routes, delay=0.0):
        self.routes = routes
        self.delay = delay
        self.hits = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                fake.hits.append((self.path, dict(self.headers)))
                if fake.delay:
                    time.sleep(fake.delay)
                route = fake.routes.get(self.path)
                if route is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                status, payload, headers = route if len(route) == 3 else (route[0], route[1], {})
                data = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = _Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fakes():
    made = []

    def make(routes, delay=0.0):
        f = Fake(routes, delay)
        made.append(f)
        return f
    yield make
    for f in made:
        f.close()


def test_ollama_merges_v1_models_and_tags(fakes):
    f = fakes({"/v1/models": (200, {"object": "list", "data": [{"id": "qwen3:8b", "object": "model"}]}),
               "/api/tags": (200, {"models": [{"name": "qwen3:8b"}, {"name": "gpt-oss:20b"}]})})
    found = L.discover_local(servers=(("ollama", f.port),))
    assert found == [{"provider_id": "ollama", "base_url": "http://127.0.0.1:%d/v1" % f.port, "port": f.port,
                      "models": ["qwen3:8b", "gpt-oss:20b"]}]
    for path, headers in f.hits:
        assert not any(h.lower() in ("authorization", "x-api-key", "api-key") for h in headers)


def test_llamacpp_and_localai_are_told_apart(fakes):
    llama = fakes({"/v1/models": (200, {"object": "list", "data": [
        {"id": "Qwen3.8-27B-Q4_K_M.gguf", "object": "model", "owned_by": "llamacpp", "meta": {"n_ctx_train": 262144}}]})})
    props = fakes({"/v1/models": (200, {"data": [{"id": "model.gguf"}]}),
                   "/props": (200, {"default_generation_settings": {}, "total_slots": 1})})
    localai = fakes({"/v1/models": (200, {"object": "list", "data": [{"id": "phi-4", "object": "model"}]})})
    found = L.discover_local(servers=(("llamacpp", llama.port), ("llamacpp", props.port), ("llamacpp", localai.port)))
    assert [x["provider_id"] for x in found] == ["llamacpp", "llamacpp", "localai"]
    assert found[0]["models"] == ["Qwen3.8-27B-Q4_K_M.gguf"]
    for item in found:
        assert P.get(item["provider_id"]) is not None and P.get(item["provider_id"]).local


def test_non_model_servers_and_closed_ports_are_ignored(fakes):
    web = fakes({"/": (200, {"hello": "world"})})            # e.g. a dev web server on :8000
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    closed = s.getsockname()[1]
    s.close()
    found = L.discover_local(servers=(("vllm", web.port), ("jan", closed)))
    assert found == []


def test_duplicate_ports_probe_once(fakes):
    f = fakes({"/v1/models": (200, {"data": [{"id": "m"}]})})
    found = L.discover_local(servers=(("llamacpp", f.port), ("localai", f.port)))
    assert len(found) == 1
    assert [p for p, _ in f.hits].count("/v1/models") == 1


def test_redirects_are_never_followed(fakes):
    target = fakes({"/v1/models": (200, {"data": [{"id": "stolen"}]})})
    bouncer = fakes({"/v1/models": (302, b"", {"Location": "http://127.0.0.1:%d/v1/models" % target.port})})
    assert L.discover_local(servers=(("lmstudio", bouncer.port),)) == []
    assert target.hits == []


def test_proxy_environment_is_ignored(fakes, monkeypatch):
    f = fakes({"/v1/models": (200, {"data": [{"id": "m"}]})})
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    found = L.discover_local(servers=(("jan", f.port),))
    assert found and found[0]["models"] == ["m"]


def test_never_contacts_a_non_loopback_host(monkeypatch):
    calls = []
    monkeypatch.setattr(L._OPENER, "open", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(L, "LOOPBACK", "93.184.216.34")
    with pytest.raises(ValueError):
        L._get_json(11434, "/v1/models", 0.1)
    assert L.discover_local(timeout=0.05) == []
    assert calls == []


def test_slow_servers_time_out_concurrently(fakes):
    slow = [fakes({"/v1/models": (200, {"data": [{"id": "m"}]})}, delay=1.0) for _ in range(3)]
    started = time.time()
    found = L.discover_local(timeout=0.2, servers=tuple(("vllm", f.port) for f in slow))
    assert found == []
    assert time.time() - started < 1.5


def test_garbage_answers_are_ignored(fakes):
    f = fakes({"/v1/models": (200, b"<html>not json</html>")})
    assert L.discover_local(servers=(("lmstudio", f.port),)) == []


@pytest.mark.parametrize("hw,needle", [
    ({}, "Hardware unknown"),
    ({"ram_gb": 4}, "struggle"),
    ({"ram_gb": 8}, "3-4B"),
    ({"ram_gb": 16, "vram_gb": 8}, "7-9B"),
    ({"ram_gb": 32, "vram_gb": 16}, "gpt-oss-20b"),
    ({"ram_gb": 32, "vram_gb": 24}, "27-32B"),
    ({"ram_gb": 64, "os": "darwin", "arch": "arm64"}, "35B-A3B"),
    ({"ram_gb": 128, "unified": True}, "gpt-oss-120b"),
    ({"ram_gb": 512, "unified": True}, "200B+"),
    ({"ram_gb": "lots"}, "Hardware unknown"),
])
def test_recommend_local(hw, needle):
    text = L.recommend_local(hw)
    assert needle in text and chr(0x2014) not in text and len(text) < 300


def test_cpu_only_advice_mentions_speed():
    assert "CPU" in L.recommend_local({"ram_gb": 32})
    assert "CPU" not in L.recommend_local({"ram_gb": 32, "vram_gb": 24})


# ---------------------------------------------------------------------------
# a self-hosted endpoint named in the environment
# ---------------------------------------------------------------------------

def test_configured_endpoint_names_alias_and_kill_switch():
    f = L.configured_endpoint
    assert f({}) == ""
    assert f({"PULSARCODE_LOCAL_ENDPOINT": " http://127.0.0.1:8081 "}) == "http://127.0.0.1:8081"
    assert f({"PULSAR_LOCAL_ENDPOINT": "http://192.168.1.50:8000/v1"}) == "http://192.168.1.50:8000/v1"
    for off in ("0", "off", "OFF", "none", "false", "", "  "):               # the first name set decides
        assert f({"PULSARCODE_LOCAL_ENDPOINT": off, "PULSAR_LOCAL_ENDPOINT": "http://h:1/v1"}) == "", off
    assert f({"PULSARCODE_LOCAL_ENDPOINT": "http://a:1/v1", "PULSAR_LOCAL_ENDPOINT": "http://b:2/v1"}) == "http://a:1/v1"


def test_endpoint_base_keeps_only_a_clean_http_url():
    b = L.endpoint_base
    assert b("http://127.0.0.1:8081/v1/") == "http://127.0.0.1:8081/v1"
    assert b("HTTPS://u:pw@llm.example.com/anthropic?k=1#x") == "https://llm.example.com/anthropic"
    for bad in ("localhost:8081", "ftp://h/v1", "http://", "http://h/v1 extra", "http://h/v1\x1b]0;x\x07", "0"):
        assert b(bad) == "", bad


def test_prefer_local_switch():
    assert L.prefer_local({}) and L.prefer_local({"PULSARCODE_PREFER_LOCAL": "1"})
    for off in ("0", "off", "false", "no"):
        assert not L.prefer_local({"PULSARCODE_PREFER_LOCAL": off}), off


def test_endpoint_answers_on_any_http_status(fakes, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")          # never through a proxy for loopback
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    ok = fakes({"/v1/models": (200, {"data": [{"id": "m"}]})})
    assert L.endpoint_answers("http://127.0.0.1:%d/v1" % ok.port)
    nothing = fakes({})                                              # a 404 is still a server answering
    assert L.endpoint_answers("http://127.0.0.1:%d/v1/" % nothing.port)
    anthropic = fakes({"/anthropic/v1/models": (401, {"error": "key"})})
    assert L.endpoint_answers("http://127.0.0.1:%d/anthropic" % anthropic.port)
    assert [p for p, _ in anthropic.hits] == ["/anthropic/v1/models"]
    for f in (ok, nothing, anthropic):
        for _path, headers in f.hits:
            assert not any(h.lower() in ("authorization", "x-api-key", "api-key") for h in headers)
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    closed = s.getsockname()[1]
    s.close()
    assert not L.endpoint_answers("http://127.0.0.1:%d/v1" % closed, timeout=0.5)
    assert not L.endpoint_answers("not a url")
