# Changelog

All notable changes to pulsarcode are documented in this file.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/),
and the project follows [Semantic Versioning](https://semver.org/) for
public releases.

---

## v1.0.0  -  First public release

First general-availability release. Source-available, AGPL-3.0, made free
to every developer who needs Claude Code's UX without the subscription.

### Headline features

- **First-launch wizard**: three numbered steps walking through NVIDIA
  NIM signup, key paste, arrow-key model picker, and Claude Code handoff.
  Runs exactly once per machine, gated by an idempotent sentinel.
- **Arrow-key API Sonar picker**: dynamic catalog of 55+ frontier coding
  models from `build.nvidia.com`, grouped into CODING, GENERAL,
  LIGHTWEIGHT, and OTHER tiers. Arrow keys to move, enter to select,
  esc to keep the current selection. Falls back to numbered-list input
  on platforms without a raw TTY.
- **Persistent model selection** at `~/.pulsarcode/active_model`,
  inherited by every future launch in any shell.
- **Four managed slash commands** installed into an isolated Claude Code
  config dir: `/api`, `/sonar`, `/pick`, `/model <alias>`.
- **Credit-rotation UX**: when the 1000-credit NVIDIA bucket exhausts,
  the message inside Claude Code explicitly tells the operator they need
  a new account and points them at `pulsarcode /api`, with the Gmail-alias
  trick documented inline.
- **12-test smoke suite** runs at install time inside the freshly built
  virtualenv. Tier-classifier coverage, persistence round-trip, em-dash
  sweep on rendered picker output, render-shape verification.
- **Zero telemetry, zero phone-home**. Adapter binds 127.0.0.1 only.
  Outbound network traffic is the bearer-authenticated HTTPS call to
  NVIDIA NIM with the operator's selected model and prompt. Verifiable
  by `tcpdump`, `lsof -i`, or any host firewall.
- **Idempotent installer**: re-running `install.sh` refreshes the
  canonical files without touching stored credentials, active-model
  selection, or Claude Code profile.

### Engineering details

- Anthropic Messages to NIM chat-completions adapter, ~600 lines of
  Python with `httpx + fastapi + uvicorn + sse-starlette`.
- Streaming SSE keep-alive pings every 5 seconds while waiting for
  upstream headers; default upstream timeout 180 s.
- Letter-boundary regex tier classifier prevents substring-overlap bugs
  (e.g. `mini` matching `minimax` is correctly rejected).
- Layout-flexible launcher: works whether placed at `cli/pulsarcode`
  with sibling `proxy/`, or at the dist root with sibling `proxy/`.
- Cross-platform venv discovery: env override -> `~/.pulsarcode/venv`
  -> sensible fallback.

### Source-available, read-only posture

This first public release establishes the project's contribution
posture: **we do not accept inbound pull requests, issues, or community
contributions**. The codebase is small, opinionated, and tightly
coupled to upstream design choices. We publish so anyone can read,
download, run, or fork under AGPL-3.0. See `CONTRIBUTING.md`.

### License

AGPL-3.0-or-later. PulsarOS Intelligence Inc., Ottawa, Canada.
