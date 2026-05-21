# Changelog

All notable changes to pulsarcode are documented in this file.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/),
and the project follows [Semantic Versioning](https://semver.org/) for
public releases.

---

## v1.0.4  -  Picker first-keypress fix and anchored repaint

Bug-fix release. Two issues in v1.0.3 caused a visibly broken picker:

- **First keypress decoded as ENTER.** A trailing newline from the y/N
  prompt was sitting in the kernel input queue when the picker took
  over `stdin`; the picker's `sys.stdin.read(1)` consumed that newline
  and selected the default model immediately. Fixed by reading bytes
  directly from the tty file descriptor (`os.read`) instead of going
  through Python's `TextIOWrapper` buffer, AND by flushing the kernel
  input queue at cbreak-entry time via `termios.tcflush`.
- **Visual drift on first paint and scroll.** The repaint loop used a
  cursor-up move proportional to the previous paint height, which
  desynchronizes whenever the terminal scrolls underneath. Fixed by
  reserving enough vertical real estate for a full-height paint up
  front (causing any necessary scroll to happen before any drawing),
  saving that anchor cursor position (`\x1b7`), and restoring
  (`\x1b8`) + clear-to-end (`\x1b[J`) before every repaint. The picker
  now sits at a stable origin row through every arrow press.

Suite still 21/21 on the CI matrix. The fix is keyboard-input plumbing
plus terminal-control codes; behavioral tests are unchanged.

### Upgrade

```bash
bash install.sh    # idempotent; refreshes canonical files
```

Your stored NIM key, current active model, and history file all carry
over from v1.0.x.

---

## v1.0.3  -  Every-launch wizard, recently-used model tier

User-experience release. The picker now runs on every launch with a
one-keystroke replace-or-keep prompt for the API key, and a RECENTLY
USED section at the top of the model list so re-picking your usual
model is one arrow keystroke from any fresh shell.

### Changes

- **Every-launch wizard**. The two-step wizard (API key check, then model
  picker) runs on every `pulsarcode` invocation, not just first launch.
  Welcome banner still gates on the onboarding sentinel so subsequent
  launches are silent.
- **`Replace stored NIM API key now? [y/N]`**. When a key is already
  stored, Step 1 collapses to a single y/N prompt with default N. Hit
  Enter and the launcher moves straight to Step 2 with the stored key
  intact. Hit Y to paste a different key without leaving the wizard.
- **RECENTLY USED tier**. The picker reads `~/.pulsarcode/model_history`
  and prepends a `RECENTLY USED` section above CODING / GENERAL /
  LIGHTWEIGHT / OTHER. Most-recent first, deduplicated, capped at 20.
  Each model appears once across the whole list.
- **`/model <alias>` writes history too**. Direct model switches through
  the in-session slash command now promote into RECENTLY USED on the
  next launch.
- **Skip the wizard for fast relaunches**: `PULSAR_SKIP_WIZARD=1
  pulsarcode` bypasses both steps and goes straight to launch with the
  stored key and active model.
- **9 new tests** covering the history file round-trip, file mode 0600,
  cap-at-20 with overflow, missing-file silence, RECENT prepend order,
  stale-alias filtering, empty-history rendering, RECENT header
  emission. Suite is now 21/21 on CI matrix (Ubuntu + macOS x Python
  3.11 / 3.12 / 3.13).
- **Removed Gmail-alias documentation** from the launcher's
  credit-exhaustion hint and the README rotation flow. The hint now
  points operators at NVIDIA's paid plans on `build.nvidia.com`.

### Engineering details

- Wizard is non-blocking. Network failure on catalog fetch falls back
  to the static catalog (no behavior change). Picker failure is non-
  fatal and the launch continues with whatever `active_model` already
  says. History file corruption is silently ignored.
- `~/.pulsarcode/model_history` is mode 0600, newline-delimited, capped
  at 20 entries. Created lazily, never written by anything other than
  the picker module's `write_history`.
- Picker exits 0 only when an entry is selected with Enter (history
  updated). Esc / Ctrl+C / Q exit 1, leaving `active_model` unchanged.

### Upgrade

```bash
bash install.sh    # idempotent; refreshes canonical files
```

Your stored NIM key, current active model, and Claude Code profile are
untouched. The first launch after upgrade shows the welcome banner one
more time (the sentinel name is unchanged from v1.0.x) and then settles
into the silent-banner every-launch flow.

---

## v1.0.2  -  Picker fixes

- Fixed first-arrow-keypress race where the down-arrow on a fresh
  picker session was decoded as plain ESC and cancelled the picker.
  Inter-byte CSI timeout extended from 20 ms to 200 ms.
- Fixed catalog rendering to show only a 10-row visible window with
  `(N more above)` / `(N more below)` indicators, scrolling one row per
  arrow press.
- Numeric CSI sequence drain so trailing bytes from Page Up / Page Down
  / Home / End do not leak into the next read.

---

## v1.0.1  -  Tagline correction, README polish

- Clarified the hero tagline to distinguish the Claude Code CLI binary
  (free Anthropic download) from the Claude family models (paid
  subscription) and from pulsarcode (this AGPL-3.0 wrapper).
- Cleaned up the comparison table to be capabilities-additive rather
  than vendor-vs-vendor.

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
- **Credit-exhaustion UX**: when the 1000-credit NVIDIA bucket runs out,
  the message inside Claude Code points operators at `build.nvidia.com`
  for paid plans and at `pulsarcode /api` to paste a different key.
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

- Anthropic Messages to NIM chat-completions adapter, under 1000 lines
  of Python with `httpx + fastapi + uvicorn + sse-starlette`.
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
