# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Pulsar Core: the per-user loopback router every Claude Code session talks to.

One Core process per PULSAR_HOME serves any number of sessions. A session is
identified by its bearer token (Claude Code sends it as ANTHROPIC_AUTH_TOKEN);
the token maps to a session file that holds the route plan (an ordered list
of provider/model targets). Rewriting that file hot-swaps the model of a
running session; the plan's extra targets are the relay chain used when the
first target is rate-limited or down.

  state.py    core.json + session files + leases (shared by every process)
  client.py   tiny stdlib client used by statusline, MCP server, top, stage
  server.py   the FastAPI app (gateway, pulsar endpoints, stage bus, web stage)
  router.py   plan execution, circuit breakers, relay
  upstream.py OpenAI-dialect streaming + Anthropic-native passthrough
  stats.py    in-memory metrics, event ring buffer, SSE fan-out
"""
