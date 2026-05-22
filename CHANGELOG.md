# Changelog

All notable changes to pulsarcode are documented in this file.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/),
and the project follows [Semantic Versioning](https://semver.org/) for
public releases.

---

## v1.0.10  -  Reasoning-model annotation, first-token timeout

### Added

- **`reasoning` tag in the catalog.** Known reasoning / chain-of-thought
  upstream model IDs are annotated with a `reasoning` tag in
  `nim_api_sonar`. The picker surfaces the tag in the meta column as
  `reasoning, slower first token` so the model latency profile is
  visible at pick time, not at "Hello" time. Initial tagged set:
  `deepseek-ai/deepseek-v4-pro`,
  `nvidia/llama-3.1-nemotron-ultra-253b-v1`. Any upstream ID whose
  name contains `reasoning` or `thinking` is tagged automatically.
- **First-token timeout in the streaming adapter.** The
  `anthropic_stream_from_nim` path now tracks time since stream-open
  and aborts the upstream request if no content token arrives within
  the configured window. Default 90 seconds for standard models;
  reasoning-tagged models get 300 seconds. When the timeout fires,
  the adapter yields a clean text message inside Claude Code with the
  upstream model id, the timeout value, and explicit recovery steps
  (switch model via `pulsarcode pick`, or extend the timeout via env
  var). The session no longer hangs silently when an upstream model
  fails to produce content.
- **Two new environment overrides**:
  `PULSAR_NIM_FIRST_TOKEN_TIMEOUT` (default `90`) and
  `PULSAR_NIM_FIRST_TOKEN_TIMEOUT_REASONING` (default `300`).

### Notes

This release does not change the wire format the adapter speaks to
Claude Code, the catalog merge algorithm, the picker keypress
handling, or the install flow. It adds two settings and one watchdog
inside the streaming path.

### Tests

21/21 pass on every CI matrix slot.

---

## v1.0.9  -  Catalog truth, dependency hygiene, copy polish

### Changed

- **Static model catalog rewritten against the live NVIDIA NIM `/v1/models` endpoint.**
  Every upstream model ID in `STATIC_OFFICIAL_MODELS` is now present in the
  authoritative `integrate.api.nvidia.com/v1/models` response at release time.
  Sixteen entries that no longer existed upstream are removed. Six entries are
  renamed to match the upstream ID format (`step-3.5-flash` not `step-3-5-flash`,
  `glm-5.1` not `glm5.1`, `qwen3.5-122b-a10b` not `qwen3-5-122b-a10b`,
  `mixtral-8x22b-v0.1` not `mixtral-8x22b-instruct`, `mixtral-8x7b-instruct-v0.1`
  not `mixtral-8x7b-instruct`, `riva-translate-4b-instruct-v1.1` not
  `riva-translate-4b-instruct-v1_1`). Eleven new live entries added, including
  `google/gemma-4-31b-it`, `meta/llama-4-maverick-17b-128e-instruct`,
  `mistralai/codestral-22b-instruct-v0.1`, `mistralai/mistral-large-3-675b-instruct-2512`,
  `qwen/qwen3.5-397b-a17b`. Selecting any catalog entry now reaches a
  model NVIDIA actually serves.

- **`requirements.txt` pinned to actual runtime dependencies.** Removes `numpy`,
  `zstandard`, `sse-starlette`, and `pydantic`, all of which had zero imports
  across the proxy modules. Removes the commented MLX block (unused here).
  Three pins remain: `fastapi`, `uvicorn[standard]`, `httpx`. Smaller install
  surface; faster venv build.

- **README and CHANGELOG corrected to match actual dependencies.** The adapter
  uses FastAPI's built-in `StreamingResponse` for SSE; the prior claim that
  `sse-starlette` was a dependency is removed everywhere it appeared.

- **README architecture diagram updated.** The Claude Code box now reflects
  the v1.0.6 behavior: the installer auto-pulls Claude Code via
  `npm i -g @anthropic-ai/claude-code` when it is missing.

- **README ASCII demo refreshed.** The example picker view now lists models
  that are actually in the catalog.

- **`pulsarcode API Sonar` branding standardized.** The picker module
  argparse description and the `nim_api_sonar` module docstring now use
  the `pulsarcode` brand consistently.

- **CHANGELOG entries v1.0.0 through v1.0.8 rewritten in third-person
  declarative voice.** Prior entries described changes in narrative-recap
  form; this release brings every entry to a consistent production tone.

### Removed

- `_CODING_ORDER_HINTS` entries for models no longer in the catalog
  (`kimi-k2-thinking`, `kimi-k2-instruct`, `qwen2.5-coder-32b`,
  `qwen2.5-coder-7b`).
- The `kimi-k2-thinking` special-case branch in `display_name_for_record`,
  unreachable since the model was removed from the catalog.
- Stale `tempfile` import in the test module; tests use the `pytest`
  `tmp_path` fixture exclusively.

### Added

- Python 3.14 added to the CI test matrix (Ubuntu + macOS × 3.11 / 3.12 /
  3.13 / 3.14). README already listed 3.14 as recommended; CI now matches.

### Fixed

- `display_name_for_record` no longer references a removed model.
- `SECURITY.md` "Hall of fame" copy refreshed; the prior text anchored on
  v1.0.0 as "the first public release" and read stale after eight releases.

### Tests

21/21 pass on every CI matrix slot. No behavior change in the picker key
logic, the adapter wire format, the install flow, or the catalog merge
algorithm; this release moves the inputs (static catalog data, requirements,
copy) into alignment with reality.

### Upgrade

```bash
bash install.sh
```

Idempotent. Stored NIM key, current `active_model`, and `model_history` carry
over.

### One-line install (latest)

```bash
curl -sL https://github.com/PulsarOSDevTeam/pulsarcode/releases/download/v1.0.9/pulsarcode-v1.0.9.tar.gz | tar -xzf - -C /tmp && bash /tmp/pulsarcode-1.0.9/install.sh && ~/.local/bin/pulsarcode
```

---

## v1.0.8  -  Catalog correction, branding, installer rendering

### Fixed

- Removed `moonshotai/kimi-k2-thinking` and `moonshotai/kimi-k2-instruct`
  from `STATIC_OFFICIAL_MODELS`. NVIDIA NIM no longer serves either
  upstream ID; requests to them returned HTTP 410 Gone.
- Replaced remaining legacy product-name references in the picker
  title bar, status-line strings, and `__init__.py` docstrings with
  `pulsarcode / API Sonar`. Corrected the shell-rc PATH comment written
  by the installer.
- Switched `install.sh` ANSI color variables to ANSI-C `$'\033...'`
  quoting so banners render in color instead of printing literal
  `\033[...m` as text.

### Notes

A documentation-only patch landed on the same `v1.0.8` tag's `main`
branch after release: the README Quick Start opens with a four-bullet
"Prerequisites at a glance" callout, the Requirements table gained a
dedicated Node.js row and a Notes column, and the curl one-liner was
bound to the actual tag instead of `vX.Y.Z` placeholders. The
`pulsarcode-v1.0.8.tar.gz` release asset itself is unchanged from
publish time.

---

## v1.0.7  -  `_read_key` byte-comparison fix

### Fixed

- `proxy/nim_sonar_picker.py` `_read_key` final block now compares the
  byte read by `os.read(fd, 1)` directly (`b == 0x0D or b == 0x0A`
  for ENTER, `b == 0x03` for CTRL-C, `b == 0x09` for TAB, `chr(b)` for
  printable ASCII). The prior code referenced an undefined `ch`
  variable left from the earlier `sys.stdin.read(1)` implementation,
  which raised `NameError` on Enter, Ctrl-C, Tab, or any unmapped key
  and prevented the picker from persisting the chosen alias.
- The numeric-CSI drain branch (Page Up / Down / Home / End) now uses
  `_sel.select([fd], ...)` and `os.read(fd, 1)`, matching the rest of
  the function.

Tests: 21/21 pass on the CI matrix.

---

## v1.0.6  -  Claude Code pre-flight, tar.gz release asset

### Added

- `install.sh` step 1b validates the Anthropic Claude Code CLI before
  building the venv. If `claude` is not on PATH and `npm` is available,
  the installer runs `npm install -g @anthropic-ai/claude-code` and
  continues. If neither is available, the installer halts with a clear
  pointer at [nodejs.org](https://nodejs.org) and the
  [Claude Code install docs](https://claude.com/claude-code).
- The launcher validates `CLAUDE_BIN` at the top of the launch path,
  before the every-launch wizard runs.
- `.tar.gz` release asset published alongside the `.zip`. The published
  one-liner uses `tar -xzf` so install works on minimal Linux images
  where `unzip` is absent (Alpine, slim Debian, fresh Ubuntu cloud).

---

## v1.0.5  -  Documentation alignment

### Changed

- README, `install.sh` "Next step" block, launcher help text, and
  picker module docstring updated to describe the v1.0.4 reality:
  every-launch two-step wizard, `RECENTLY USED` tier, `PULSAR_SKIP_WIZARD`
  escape hatch, 21-test smoke suite. The prior copy still described
  the v1.0.0 three-step first-launch wizard with a 12-test count.
- README hardcoded `v1.0.0` version badge replaced with a dynamic
  GitHub `latest release` badge.
- `LOCAL_PULSAR` tier header neutralized; the tier is reserved for
  future on-device routes and is hidden when empty.
- `.gitignore` added: `__pycache__/`, `.pytest_cache/`, `.venv/`, editor
  scratch files.

No code behavior change.

---

## v1.0.4  -  Picker first-keypress and anchored repaint

### Fixed

- `_read_key` reads bytes directly from the tty file descriptor with
  `os.read` instead of going through `sys.stdin.read(1)`, and the
  picker calls `termios.tcflush(fd, TCIFLUSH)` at cbreak-entry. The
  prior code's reliance on Python's `TextIOWrapper` buffer caused the
  first keypress on a fresh picker session to be misread as ENTER
  (consuming a trailing newline from the prior shell prompt) or as
  ESC (when the CSI bytes arrived in two chunks across the buffer
  boundary).
- Picker repaints anchor to a saved cursor position (DEC `\x1b7` /
  `\x1b8`) rather than a relative cursor-up move. The repaint reserves
  enough vertical space up front for the full picker height before
  saving the anchor, so terminal scrolling during the first paint does
  not desynchronize the redraw.

Tests: 21/21 pass on the CI matrix.

---

## v1.0.3  -  Every-launch wizard, recently-used tier

### Added

- The wizard runs on every `pulsarcode` invocation, not only first
  launch. When a NIM key is already stored, Step 1 collapses to a
  single `Replace stored NIM API key now? [y/N]` prompt with default
  `N`. The welcome banner still gates on the onboarding sentinel and
  shows only on first launch.
- `RECENTLY USED` tier prepended to the picker. The picker reads
  `~/.pulsarcode/model_history` (newline-delimited, mode `0600`,
  capped at 20 entries) and places matching records at the top in
  history order. Each model appears once across the whole list.
- `/model <alias>` (the in-session slash command) and
  `pulsarcode model <alias>` write to history as well as
  `active_model`, so direct switches promote into `RECENTLY USED` on
  the next launch.
- `PULSAR_SKIP_WIZARD=1` env var bypasses the wizard for scripted or
  fast-relaunch use.
- Nine new picker tests covering history file round-trip, mode `0600`,
  cap-at-20 with overflow, missing-file silence, `RECENT` prepend
  order, stale-alias filtering, empty-history rendering, `RECENT`
  header emission.

### Removed

- The Gmail-alias rotation hint from the launcher's credit-exhaustion
  message and the README rotation flow. Both surfaces now point at
  the NVIDIA paid plans page.

---

## v1.0.2  -  Picker keypress timing and windowed render

### Fixed

- Extended inter-byte CSI timeout from 20 ms to 200 ms. On macOS
  Terminal and iTerm, the three bytes of an arrow-key sequence
  (`\x1b`, `[`, direction) can arrive several tens of milliseconds
  apart on the first keypress after entering cbreak mode; the
  previous 20 ms window let the second `select` time out and the
  arrow press read as a plain ESC.
- Catalog rendering switched to a 10-row sliding window with `(N more
  above)` and `(N more below)` indicators. Each arrow press scrolls
  one row.
- Numeric CSI sequence drain so trailing bytes from Page Up / Page
  Down / Home / End do not leak into the next read.

---

## v1.0.1  -  Tagline correction, README polish

### Changed

- Hero tagline rewritten to distinguish three layers in the reader's
  mind: the Claude Code CLI binary (free Anthropic download), the
  Claude model family (paid subscription), and pulsarcode (this
  AGPL-3.0 wrapper that swaps the model backend).
- Comparison table reframed as capabilities-additive rather than
  vendor-vs-vendor.

---

## v1.0.0  -  First public release

### Added

- `pulsarcode` bash launcher that wraps the official Anthropic Claude
  Code CLI via the documented `ANTHROPIC_BASE_URL` environment
  variable and routes its model traffic through NVIDIA NIM.
- `proxy/nim_anthropic_proxy.py`: a local FastAPI adapter that binds
  `127.0.0.1` only, translates Anthropic Messages API requests into
  NVIDIA NIM's OpenAI-compatible chat completions, signs them with the
  user's NVIDIA NIM bearer, and streams responses back with SSE
  keep-alive `ping` events while waiting for upstream headers.
- `proxy/nim_api_sonar.py`: API Sonar catalog discovery. Merges a
  curated static seed of upstream model IDs with the public NVIDIA
  reference and build pages and the authenticated `/v1/models`
  endpoint.
- `proxy/nim_sonar_picker.py`: arrow-key interactive picker over the
  Sonar catalog. Groups models into `CODING`, `GENERAL`, `LIGHTWEIGHT`,
  and `OTHER` tiers. Falls back to a numbered-list prompt on platforms
  without a raw TTY.
- First-launch wizard: API key paste, model picker, Claude Code
  handoff.
- Four managed slash commands installed into an isolated Claude Code
  config directory: `/api`, `/sonar`, `/pick`, `/model <alias>`.
- 12-test smoke suite runs at install time inside the freshly built
  virtualenv.
- Zero telemetry, zero phone-home. The local adapter binds
  `127.0.0.1` only; the only outbound network call is the
  bearer-authenticated HTTPS request to NVIDIA NIM with the user's
  selected model and prompt.
- Idempotent installer; re-running `install.sh` refreshes the
  canonical files without touching stored credentials, active-model
  selection, or Claude Code profile.

### Contribution posture

This is a source-available, read-only project. Pull requests, feature
requests, and community discussions are not accepted on this
repository. The codebase is small, AGPL-3.0 licensed, and forkable.
See `CONTRIBUTING.md`.

### License

AGPL-3.0-or-later. PulsarOS Intelligence Inc., Ottawa, Canada.
