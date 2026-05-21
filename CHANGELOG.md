# Changelog

All notable changes to pulsarcode are documented in this file.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/),
and the project follows [Semantic Versioning](https://semver.org/) for
public releases.

---

## v1.0.8  -  Three demo-blocking fixes

> **Documentation polish landed 2026-05-21** (no version bump, no code
> change, tarball asset unchanged): the README Quick Start now opens
> with a four-bullet "Prerequisites at a glance" block, the
> Requirements table grew a dedicated Node.js row plus an inline note
> on the Claude Code row explaining that the installer auto-pulls it
> via npm if absent, and the curl one-liner in the Quick Start was
> bound to the actual `v1.0.8` tag instead of `vX.Y.Z` placeholders.
> The v1.0.8 release-page notes were updated in place via
> `gh release edit` to mirror the same Requirements block. The tag,
> the commit (`0d005ce`), and the downloaded `pulsarcode-v1.0.8.tar.gz`
> bytes are identical to publish time.

User-visible demo bugs caught while screen-recording the launch film
against a real fresh-install on macOS:

### Fix 1: Dead model routes removed from the catalog

`moonshotai/kimi-k2-thinking` and `moonshotai/kimi-k2-instruct` were
seeded in the static catalog but NVIDIA NIM no longer hosts them. The
live `/v1/models` endpoint returns only `moonshotai/kimi-k2.6` from
Moonshot. Any user who picked `nim-kimi-k2-thinking` or
`nim-kimi-k2-instruct` in the picker then got `HTTP 410 Gone` from
NVIDIA on their first Claude Code message. Both entries are now
removed from `STATIC_OFFICIAL_MODELS` and from the context-token
override map.

If NVIDIA reinstates these endpoints in the future, the runtime
catalog will pick them up live; nothing else needs to change.

### Fix 2: `pulsarcode` branding consistent across all user-facing surfaces

The picker title bar and ten status-line strings still read
`Local Pulsar / API Sonar`. Those are now `pulsarcode / API Sonar`.
The `proxy/__init__.py` and `tests/__init__.py` docstrings were
updated for consistency. The shell-rc PATH comment that the installer
writes into `~/.zshrc` / `~/.bashrc` is also corrected.

### Fix 3: install.sh banners render in color instead of printing literal `\033[...m`

The installer used double-quoted strings for the ANSI escape codes
(`EMBER="\033[38;5;208m"`), which bash treats as four literal
characters. The banners therefore printed the raw escape codes as
plain text on screen. v1.0.8 switches to ANSI-C `$'\033...'` quoting
so bash assigns the actual ESC byte (0x1B). The installer now renders
the ember-orange / green / dim / bold colors it always intended.

### Tests

21/21 pass on the CI matrix. No behavior change in the picker,
the adapter, or the API Sonar catalog beyond the two removed model
ids; the existing suite covers every code path touched.

### Upgrade

```bash
bash install.sh
```

Idempotent. Your stored NIM key, current `active_model`, and
`model_history` carry over.

### One-line install (latest)

```bash
curl -sL https://github.com/PulsarOSDevTeam/pulsarcode/releases/download/v1.0.8/pulsarcode-v1.0.8.tar.gz | tar -xzf - -C /tmp && bash /tmp/pulsarcode-1.0.8/install.sh && ~/.local/bin/pulsarcode
```

---

## v1.0.7  -  Picker NameError on Enter (and numeric-CSI drain)

Bug-fix release. v1.0.4 rewrote `_read_key` to read bytes directly from
the tty file descriptor via `os.read`, but the rewrite missed two
trailing sections of the function that still referenced the old
`ch = sys.stdin.read(1)` variable name and a bare `select` import. The
result was a Python `NameError` flashing on screen every time a picker
user pressed Enter, Ctrl-C, Tab, or any unmapped key, and another
NameError when any function key sent a numeric CSI sequence (Page Up,
Page Down, Home, End). The picker's launcher caller swallowed the
traceback so Claude Code still opened, but the user's model selection
never persisted, which is why the banner kept showing the default
model after Enter on the picker.

### What changed

- `_read_key` final block now does byte-precise checks against `b`
  (the byte read by `os.read(fd, 1)` at the top of the function):
  `b == 0x0D or b == 0x0A` for ENTER, `b == 0x03` for CTRL-C,
  `b == 0x09` for TAB, `chr(b)` for printable ASCII passthrough.
- Numeric-CSI drain branch now uses `_sel.select([fd], ...)` and
  `os.read(fd, 1)` instead of the bare `select.select` /
  `sys.stdin.read(1)` references that were also leftover from the
  pre-v1.0.4 implementation.

The fix is twelve lines across one file. Behavior of the picker is
unchanged on every other path; this release only stops the flash and
makes Enter actually persist the chosen alias to
`~/.pulsarcode/active_model` and `~/.pulsarcode/model_history`.

### Upgrade

```bash
bash install.sh
```

Idempotent. Your stored NIM key, current `active_model`, and
`model_history` carry over from v1.0.x.

Tests still 21/21 on the CI matrix.

---

## v1.0.6  -  Claude Code pre-flight + tar.gz release asset

Two fixes that close the last gaps a first-time user could hit with the
one-line install command.

### Fix 1: Claude Code pre-flight

The launcher now validates that the official Anthropic Claude Code CLI
is installed BEFORE running the every-launch wizard. Previously a user
without Claude Code would go through API key paste plus arrow-key model
picker, and only then hit a `127` exit code. That UX is gone.

The installer also pre-flights `claude` before building the venv. If
the binary is missing and `npm` is on the user's machine, the installer
auto-runs `npm install -g @anthropic-ai/claude-code` and continues. If
`npm` is also missing, the installer halts with a clear pointer at
[nodejs.org](https://nodejs.org) and at the
[Claude Code install docs](https://claude.com/claude-code).

This means the one-liner can succeed end to end for a user with nothing
but Node.js installed: pulsarcode pulls in Claude Code for them.

### Fix 2: `.tar.gz` release asset

A `.tar.gz` release asset (`pulsarcode-vX.Y.Z.tar.gz`) ships alongside
the existing `.zip`. The published one-liner uses `tar -xzf` so it
works out of the box on minimal Linux images (Alpine, slim Debian,
fresh Ubuntu cloud images) that do not have `unzip` installed by
default. The `.zip` asset is still published for users who prefer it.

### Upgrade

```bash
bash install.sh
```

Idempotent. Your stored NIM key, current `active_model`, and
`model_history` carry over from v1.0.x.

### One-line install (new)

```bash
curl -sL https://github.com/PulsarOSDevTeam/pulsarcode/releases/download/v1.0.6/pulsarcode-v1.0.6.tar.gz \
  | tar -xzf - -C /tmp && \
bash /tmp/pulsarcode-1.0.6/install.sh && \
~/.local/bin/pulsarcode
```

Tests still 21/21 on the CI matrix.

---

## v1.0.5  -  Copy polish across every user-facing surface

Documentation-only release. No code behavior change. Brings every
user-visible string in the project up to v1.0.4 reality and clears the
last of the v1.0.0 anchors that survived through patch releases.

### What changed

- **README**: the "what you see when you run it" demo now matches the
  every-launch wizard (two-step `y/N` and `pick a model` flow) with a
  populated `RECENTLY USED` tier on top. The hardcoded version badge
  was replaced with a dynamic GitHub-rendered "latest release" badge.
  Hardcoded `pulsarcode-v1.0.0.zip` references in the quick start were
  replaced with a one-line `curl` install snippet against the latest
  release. The "12-test smoke suite" is now "21-test". The features
  list documents the `RECENTLY USED` tier and the `model_history` file.
  Troubleshooting entry "macOS first-launch wizard does not appear" is
  obsolete (the wizard now runs every launch) and was replaced with a
  `PULSAR_SKIP_WIZARD=1` recipe.
- **install.sh**: banner is `pulsarcode installer` (the previous header
  carried internal branding from the developer's local tree). The
  "Next step" block describes the every-launch two-step flow with the
  `PULSAR_SKIP_WIZARD=1` escape hatch. The smoke-test step now reports
  the actual pass count parsed from pytest output instead of hardcoding
  `12/12`.
- **Launcher** (`pulsarcode`): top-of-file comment and `pulsarcode help`
  describe the every-launch wizard with explicit `PULSAR_SKIP_WIZARD=1`
  documentation. The `pick` subcommand's help mentions
  `~/.pulsarcode/model_history` and the `RECENTLY USED` tier.
- **Picker module docstring**: rewritten for an external code reader.
  References to internal doctrine numbering and design memos were
  removed; the file now documents what the picker does and how to
  invoke it.
- **`LOCAL_PULSAR` tier header text**: neutralized so it does not read
  as operator-specific framing. The tier itself is reserved for future
  on-device routes and is silently hidden on the public surface today.

### Tests

Suite still 21/21 on the CI matrix. No behavior changes; the existing
tests cover every code path touched by this release.

### Upgrade

```bash
bash install.sh
```

Idempotent. Your stored NIM key, current `active_model`, and
`model_history` all carry over from v1.0.x.

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
