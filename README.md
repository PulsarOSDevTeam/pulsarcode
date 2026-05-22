<div align="center">

# pulsarcode

**Run Claude Code with free NVIDIA-hosted models.**

Use Kimi K2.6, Qwen 3 Coder, DeepSeek V4, Llama 3.3, GPT-OSS 120B, and 50+
other frontier coding models inside the official Claude Code CLI, with no
subscription and no credit card.

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL%203.0-orange.svg)](LICENSE)
[![Latest release](https://img.shields.io/github/v/release/PulsarOSDevTeam/pulsarcode?label=latest&color=blue)](https://github.com/PulsarOSDevTeam/pulsarcode/releases)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey.svg)](#requirements)
[![tests](https://github.com/PulsarOSDevTeam/pulsarcode/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/PulsarOSDevTeam/pulsarcode/actions/workflows/test.yml)
[![Source available](https://img.shields.io/badge/source-available-blue.svg)](CONTRIBUTING.md)
[![Made in Canada](https://img.shields.io/badge/made%20in-Canada%20%F0%9F%87%A8%F0%9F%87%A6-red.svg)](#made-by)

</div>

---

## What you actually see when you run it

```text
$ cd ~/my-side-project
$ pulsarcode

+----------------------------------------------------------------------------+
| Welcome to pulsarcode                                                      |
| Sovereign Claude Code launcher, your choice of model                       |
+----------------------------------------------------------------------------+

 Every launch walks through two quick steps:
   1. API key check  (replace if you want, default is keep)
   2. Model picker   (your recently-used models appear first;
                      Esc keeps current and launches)

 Step 1 of 2: NVIDIA NIM API key
 Stored key : nvapi-Lm9Yk...c4uB
 Replace stored NIM API key now? [y/N] _

 Step 2 of 2: pick a model
 Arrows up / down to move, Enter to select, Esc to keep current.
 Your recently-used models appear at the top.

+----------------------------------------------------------------------------+
| pulsarcode  /  API Sonar  /  pick a model                                  |
+----------------------------------------------------------------------------+

  RECENTLY USED  your previous selections, newest first
  >  [x]  nim-kimi                                   moonshotai/kimi-k2.6
     [ ]  nim-deepseek-ai-deepseek-v4-pro            deepseek-ai/deepseek-v4-pro
     [ ]  nim-qwen-qwen3-coder-480b-a35b-instruct    qwen/qwen3-coder-480b-a35b-instruct

  CODING TIER  largest coding-tuned routes
     [ ]  nim-openai-gpt-oss-120b                    openai/gpt-oss-120b
     [ ]  nim-mistralai-codestral-22b-instruct-v0-1  mistralai/codestral-22b-instruct-v0.1
     [ ]  nim-meta-codellama-70b                     meta/codellama-70b

  GENERAL TIER  mid to large general-purpose routes
     [ ]  nim-meta-llama-3-3-70b-instruct            meta/llama-3.3-70b-instruct
     [ ]  nim-z-ai-glm-5-1                           z-ai/glm-5.1
     [ ]  nim-minimaxai-minimax-m2-7                 minimaxai/minimax-m2.7
     (a few more below)
```

Hit Enter on what you want, code. The wizard runs on every launch; Esc on
the picker keeps your current model and goes straight to Claude Code.

---

## Quick start (60 seconds)

**Prerequisites at a glance** (the installer halts cleanly with a clear pointer if any are missing):

- macOS 14+ or modern Linux (Ubuntu 22.04+ / Debian 13+ / Fedora 40+ / Alpine). Windows users need WSL2.
- Python 3.11 or newer. `brew install python@3.14` on macOS, `apt-get install python3.12 python3.12-venv` on Debian / Ubuntu.
- Either Claude Code already installed, **OR** Node.js with `npm` on your machine so the installer can auto-pull it via `npm i -g @anthropic-ai/claude-code`. If you do not have Node.js, get it from [nodejs.org](https://nodejs.org) or your package manager first.
- A free NVIDIA NIM API key from [build.nvidia.com](https://build.nvidia.com). No credit card. 1000 credits per account. The first-launch wizard walks you through generating it.

One command. The latest stable release tag is the version below; the badge at the top of this README always reflects the current tag if you want to upgrade:

```bash
curl -sL https://github.com/PulsarOSDevTeam/pulsarcode/releases/download/v1.0.8/pulsarcode-v1.0.8.tar.gz \
  | tar -xzf - -C /tmp && \
bash /tmp/pulsarcode-1.0.8/install.sh && \
~/.local/bin/pulsarcode
```

That is it. The installer pre-flights the Claude Code CLI (auto-installing
it via `npm i -g @anthropic-ai/claude-code` if Node.js is on your machine
and the binary is missing), builds an isolated Python venv, installs the
adapter, and hands off to the first `pulsarcode` launch. The wizard walks
you through generating a free NVIDIA NIM API key, picking a model with
arrow keys, and dropping you into the official Claude Code CLI with your
chosen model wired in.

The one-liner uses `tar` instead of `unzip` so it works on minimal
Linux images (Alpine, slim Debian) where `unzip` is not installed by
default. A `.zip` release asset is published too for users who prefer it.

---

## Why this exists

Claude Code is one of the most polished coding-agent CLIs available in
2026. The CLI binary itself is a free download from
[claude.com/claude-code](https://claude.com/claude-code). The Claude
models behind it are accessed through a paid Anthropic subscription
(the Claude Pro plan that includes Claude Code access lists at **$20 USD
per month**).

For developers in high-income countries that is friction; for developers
in Argentina, Vietnam, Nigeria, Tunisia, India, Indonesia, Egypt,
Bangladesh, the Philippines, Pakistan, and most of the world, $240 per
year is multiple months of average salary.

Meanwhile NVIDIA hosts Kimi K2.6 (a 1-trillion-parameter mixture-of-
experts model with 256K context), plus Qwen 3 Coder 480B, DeepSeek V4,
Llama 3.3 70B, GPT-OSS 120B, and 50+ more, through their
`build.nvidia.com` developer platform. The free tier ships with 1000
credits per NVIDIA account, no credit card required, no auto-charge.

Claude Code natively supports redirecting model traffic to any backend
that speaks the Anthropic Messages wire format, via the documented
`ANTHROPIC_BASE_URL` environment variable (the same hook Anthropic
documents for AWS Bedrock and Google Vertex routing). pulsarcode uses
that hook to point Claude Code at a tiny local adapter on
`127.0.0.1:4000` which translates Anthropic Messages requests into
NVIDIA's OpenAI-compatible chat completions and forwards them to the
NIM model you picked.

**pulsarcode is the bridge.** AGPL-3.0, source-available, sovereign
Canadian, donated by a Canadian federal corporation that earns zero
dollars from this project.

---

## What it costs

The launcher itself is free, forever, AGPL-3.0.

NVIDIA's free NIM developer tier ships with 1000 credits per account.
That covers roughly 10 to 50 prompts depending on prompt size and
model. When you exhaust your credits, NVIDIA offers paid plans on the
same `build.nvidia.com` platform; pulsarcode surfaces a clear message
inside the Claude Code chat when it sees a `429` from the API and
points you at the NVIDIA documentation.

We earn zero dollars from this launcher. The repository is AGPL-3.0
forever. There is no premium tier, no paid plan, no SaaS upsell, no
enterprise edition, no analytics, no telemetry, no phone-home. The
only outbound network traffic the launcher generates is your bearer-
authenticated HTTPS request to NVIDIA, with the model and prompt you
selected.

This launcher is a donation to every developer in the world who needs
a frontier-grade coding agent and cannot or will not pay a monthly
subscription to get one.

---

## What pulsarcode adds on top of the official Claude Code

Claude Code on its own is a polished CLI with a paid model subscription
behind it. pulsarcode adds:

| Capability | Without pulsarcode | With pulsarcode |
|---|---|---|
| Default model backend | Anthropic Claude (subscription) | NVIDIA NIM (your free key, 55+ models) |
| Subscription required | Yes for Claude family | No (free NIM developer tier) |
| Models selectable from one config | Claude family | 55+ via NIM, including Kimi K2.6 |
| First-launch wizard for backend setup | API key paste only | NIM signup walk-through + arrow-key model picker |
| Live model picker grouped by tier | n/a | Yes, 55+ routes grouped CODING / GENERAL / LIGHTWEIGHT / OTHER |
| Persistent model choice across shells | n/a | Yes, sticky via `/model <alias>` or `pulsarcode pick` |
| Local adapter source code | n/a | AGPL-3.0, ~1000 lines of Python, auditable |
| Project license | Proprietary EULA | AGPL-3.0 (this launcher) |
| Maintainer revenue from this launcher | n/a | None (donated to the developer community) |

This is a side-by-side of capabilities, not a positioning against
Claude Code. Claude Code remains the official Anthropic CLI you install
separately. pulsarcode wraps it via the documented `ANTHROPIC_BASE_URL`
hook; it does not replace, modify, or redistribute it.

---

## Use cases (every developer fits at least one)

- **The Claude Pro subscription is too expensive for you** and you still want to use the Claude Code CLI.
- **You want to compare models** (Kimi K2.6 vs Qwen 3 Coder vs DeepSeek V4 vs Llama 3.3) without paying for multiple API subscriptions.
- **You are in a region** where $20 / mo is a meaningful chunk of your income.
- **You are a student, indie hacker, or open-source maintainer** who needs frontier coding model access on a zero budget.
- **You care about transparency** and want to read every line of code that touches your prompts before you trust it.
- **You want to learn how Claude Code's backend hook works** by reading a real adapter implementation in under 1000 lines of Python.
- **You are evaluating Claude Code's CLI workflow** before deciding which model subscription to license.
- **You are on a network** that allows outbound HTTPS to NVIDIA but not to other model providers.

---

## Features in detail

### Every-launch wizard

Two quick steps on every `pulsarcode` invocation. Step 1 is a one-keystroke
`Replace stored NIM API key now? [y/N]` prompt (default keep) when a key
is already stored, or a mandatory paste prompt the first time you run it.
Step 2 is the model picker. Esc on the picker keeps your current model
and launches immediately.

Want the wizard out of the way for a fast relaunch? Export
`PULSAR_SKIP_WIZARD=1` and pulsarcode goes straight to Claude Code with
your stored key and current model.

### Arrow-key API Sonar picker with recently-used tier

Hit `pulsarcode pick` in any fresh terminal tab, or let the every-launch
wizard show it for you. The picker fetches the **live** NVIDIA NIM
catalog (55+ models, refreshed every 6 hours, plus whatever extra routes
your specific account is authorized for) and groups them into five
sections:

| Tier | Contents |
|---|---|
| `RECENTLY USED` | Your previous selections, newest first (only appears once you have history) |
| `CODING TIER` | Kimi K2.6, Qwen 3 Coder 480B, DeepSeek V4, GPT-OSS 120B, and other coding-tuned routes |
| `GENERAL TIER` | Llama 3.3 70B, GLM 5.1, Magistral, MiniMax, Mixtral, Nemotron |
| `LIGHTWEIGHT TIER` | Phi-4 Mini, Gemma 2 2B, Llama 3.1 8B, smaller Nemotron variants |
| `OTHER` | Uncategorized public NIM routes |

Each model appears exactly once across the whole list. Arrow keys move,
Enter selects, Esc keeps your current pick. Your choice persists to
`~/.pulsarcode/active_model` and is appended to `~/.pulsarcode/model_history`
(capped at 20 entries, mode 0600) so re-picking your usual model on the
next launch is one arrow keystroke from the top.

### Four managed slash commands inside Claude Code

| Command | What it does |
|---|---|
| `/api` | Re-paste your NIM API key (rotation flow, full wizard inside the chat) |
| `/sonar` | Print the live catalog (table view) |
| `/pick` | Tells you to open a fresh terminal and run `pulsarcode pick` |
| `/model <alias>` | Persist a different model alias for the next launch |

### Clear messaging when you hit the free-tier ceiling

When your NIM account hits its rate ceiling or burns through its free
credits, NIM returns HTTP 429. The adapter catches it and surfaces a
clear, friendly message inside the Claude Code chat pointing you at
`pulsarcode /api` so you can update your key, and at
`https://build.nvidia.com` for NVIDIA's paid plans if you want a
higher quota. Your model selection, Claude Code session, and chat
history all carry over.

The message is intentionally short and actionable: read it, follow
the steps, keep working.

### Zero telemetry, zero phone-home

The launcher exports an isolated Claude Code config directory at
`~/.pulsarcode/claude_config/`, so it never touches your `~/.claude/`
profile or the Anthropic-side identity stored there. It also sets these
official Claude Code disable flags:

```
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
DISABLE_TELEMETRY=1
DISABLE_AUTOUPDATER=1
DISABLE_ERROR_REPORTING=1
DISABLE_BUG_COMMAND=1
DISABLE_NON_ESSENTIAL_MODEL_CALLS=1
```

The local adapter binds `127.0.0.1` only (loopback, never on the LAN).
The only outbound network traffic the launcher generates is the
bearer-authenticated HTTPS call to NVIDIA NIM with your prompt. You can
verify with `tcpdump`, Little Snitch, or `lsof -i`.

### 21-test smoke suite at install time

The installer's last step runs the unit tests inside the freshly built
virtualenv. If anything fails, the install tells you. Twenty-one tests
cover the tier classifier (letter-boundary regex to avoid the
substring-overlap bug class), tier ordering, group rendering, active
model round-trip, the history-file round-trip (cap-at-20, dedupe,
mode 0600, RECENTLY USED prepend order, stale-alias filtering), and an
em-dash-leak assertion on the rendered picker output.

```
[step 8] smoke test (picker tier classifier + active_model round-trip)
  ok  21/21 tests passed
```

---

## Architecture (one ASCII diagram, the whole stack)

```
+-------------------+
| your terminal     |
| $ pulsarcode      |
+-------------------+
         |
         v
+----------------------------------------------------+
| ~/.local/bin/pulsarcode                            |
| (symlink into ~/.pulsarcode/canonical/pulsarcode)  |
+----------------------------------------------------+
         |
         v
+---------------------------------------+
| ~/.pulsarcode/canonical/pulsarcode    |
| sets:                                  |
|   ANTHROPIC_BASE_URL = 127.0.0.1:4000  |
|   ANTHROPIC_MODEL    = <your pick>     |
|   CLAUDE_CONFIG_DIR  = ~/.pulsarcode/  |
| then execs claude                      |
+---------------------------------------+
         |                  |
         |                  v
         |     +------------------------------+
         |     | claude (Claude Code)         |
         |     | (official Anthropic binary;  |
         |     |  the installer auto-pulls it |
         |     |  via `npm i -g @anthropic-ai |
         |     |  /claude-code` if missing)   |
         |     +------------------------------+
         |                  |
         |   Anthropic Messages API
         |   (public wire format)
         v                  v
+--------------------------------------------+
| local NIM adapter (127.0.0.1:4000)         |
| ~/.pulsarcode/canonical/proxy/             |
|   nim_anthropic_proxy.py                   |
|                                            |
| translates: Anthropic Messages             |
|     -> OpenAI-compatible chat completions  |
| signs:      with your NVIDIA NIM bearer    |
| streams:    SSE keep-alive pings every 5s  |
+--------------------------------------------+
                                |
                  HTTPS, bearer = your nvapi-...
                                v
+--------------------------------------------+
| NVIDIA NIM cloud                           |
| integrate.api.nvidia.com/v1                |
| Kimi K2.6 / Qwen 3 Coder / DeepSeek V4 /   |
| Llama 3.3 / GPT-OSS 120B / ... 55+ routes  |
+--------------------------------------------+
```

The local adapter is about a thousand lines of Python with three
non-stdlib dependencies: `httpx` (HTTPS client to NVIDIA), `fastapi`
(HTTP routing), and `uvicorn` (ASGI server). Streaming uses FastAPI's
own `StreamingResponse`; no extra SSE library. The full stack
(launcher + 3 proxy modules + tests) is roughly 4000 to 5000 lines.
Readable in one sitting. Auditable in one afternoon.

---

## Privacy posture

| Layer | What we store | Where | Who can read it |
|---|---|---|---|
| API key | The `nvapi-...` you paste | `~/.pulsarcode/nim.key`, mode `0600` | You only (filesystem perms) |
| Active model | The alias you picked | `~/.pulsarcode/active_model`, mode `0600` | You only |
| Claude Code chat history | Same as Claude Code default | `~/.pulsarcode/claude_config/` | You only |
| Adapter logs | Off by default | `~/.pulsarcode/run/` if enabled via env var | You only |
| Telemetry | None | Nowhere | Nobody |
| Maintainer access to your data | None | Never | Not even us |

We do not run any cloud infrastructure that touches your data. We do
not have a server, an analytics endpoint, a feature-flag service, an
A/B test, or an error-reporting tunnel. Read the
[install.sh](install.sh), read the [launcher](pulsarcode), read the
[adapter](proxy/nim_anthropic_proxy.py). If you find anything that
phones home, file a security report (`SECURITY.md`).

---

## Slash commands inside a running Claude Code session

```
/api                    re-paste / replace your NVIDIA NIM API key
/sonar                  list the live model catalog (table form)
/pick                   tells you to open a fresh terminal tab and run `pulsarcode pick`
/model <alias>          persist a different model alias for the next launch
```

There are also Claude Code's BUILT-IN slash commands, which all keep
working. The native `/model` picker switches model live for the current
session (uses the list of aliases the adapter announces). The
`pulsarcode model <alias>` and `/model <alias>` we ship are the
**persistent** model switch (writes `~/.pulsarcode/active_model`); the
next `pulsarcode` launch in any shell will inherit your choice.

---

## Posture: source-available, read-only

This repository is published so anyone can:

- **Read** the source.
- **Download** a release zip and run it.
- **Fork** under AGPL-3.0 and modify their own fork.

It is **not** a community project. We do not run an inbound contribution
process:

- We do not accept pull requests from outside the maintainer team.
- We do not accept feature requests on this repository (Issues is off).
- We do not run a community forum (Discussions is off).
- We do not respond to support tickets opened here. The license disclaims warranty.

This is a deliberate posture, not a hostile one. The codebase is small,
opinionated, and tightly coupled to upstream design choices documented
in the project itself. We publish it because we believe the launcher is
a useful primitive for any developer who wants Claude Code's UX without
the subscription. We do not publish it because we want to coordinate a
community around it. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the
full posture and the AGPL-3.0 fork-and-run path.

---

## Requirements

| Component | Minimum | Recommended | Notes |
|---|---|---|---|
| OS | macOS 14 / Ubuntu 22.04 / Debian 13 / Fedora 40 / Alpine 3.19 | Latest | Windows: use WSL2 for now (native Windows is planned). |
| Python | 3.11 | 3.14 | Installer halts cleanly with a `brew` / `apt` one-liner if missing. |
| Node.js (with `npm`) | Any LTS (18+) | Latest LTS | Needed so the installer can auto-pull Claude Code if it is not already present. Skip if Claude Code is already installed. Get it from [nodejs.org](https://nodejs.org). |
| Claude Code | Installed from [claude.com/claude-code](https://claude.com/claude-code), **OR** auto-installed by the installer via `npm i -g @anthropic-ai/claude-code` if Node.js is present | Latest | The installer's step 1b decides. |
| Disk | 500 MB free for the venv | 1 GB | |
| NVIDIA NIM account | Free, no card, no auto-charge | Same | First-launch wizard walks you through generating the key at [build.nvidia.com](https://build.nvidia.com). 1000 credits per account. |
| Network | Outbound HTTPS to `integrate.api.nvidia.com` | Same | The local adapter binds `127.0.0.1` only; the only outbound call is your bearer-authenticated request to NVIDIA NIM. |

---

## Install in detail

The installer creates this layout under your home directory:

```
~/.pulsarcode/
  canonical/             the launcher and proxy modules
    pulsarcode           the bash launcher you invoke
    proxy/               API Sonar + NIM adapter + arrow-key picker (Python)
    tests/               the 21-test smoke suite
    requirements.txt
  venv/                  isolated Python virtualenv (no system pip pollution)
  nim.key                your NVIDIA NIM key (chmod 600, never leaves this machine)
  active_model           last alias selected via /model or pulsarcode pick
  model_history          your recently-used picks, newest first, capped at 20
  claude_config/         isolated Claude Code profile (no leak to ~/.claude)
    commands/            the four managed slash commands
  onboarding_complete    sentinel; the welcome banner shows only on first launch
  run/                   adapter PIDs and logs (logs off by default)

~/.local/bin/pulsarcode  -> symlink into ~/.pulsarcode/canonical/pulsarcode
```

`install.sh` is **idempotent**. Re-run it any time to refresh the canonical
files. It never touches your stored NIM key, your active model selection,
your model history, or your Claude Code profile.

Two environment flags worth knowing about:

- `PULSARCODE_SKIP_PATH_EXPORT=1` skips appending `~/.local/bin` to your
  shell rc during install (manage your shell rc by hand).
- `PULSAR_SKIP_WIZARD=1` bypasses the every-launch wizard for a single
  invocation; useful for scripted runs or fast relaunches.

---

## Switching model

Three independent paths, each with its own scope:

| Command | Where you run it | Scope |
|---|---|---|
| `pulsarcode pick` | Fresh terminal tab | Persists for next launch (arrow-key picker) |
| `pulsarcode model <alias>` | Shell prompt | Persists for next launch (direct alias) |
| `/model <alias>` | Inside a Claude Code session | Persists for next launch |
| Claude Code's built-in `/model` | Inside a Claude Code session | Live this session only |

The persistence file is always `~/.pulsarcode/active_model`. Read or
overwrite it directly if you want to script around the launcher.

---

## Troubleshooting

### `429 Too Many Requests`

Your NVIDIA NIM key is rate-limited. Two flavors:

1. **Short-term**: too many requests per minute. Wait 60 seconds and retry.
2. **You burned your free credits.** Sign in to `build.nvidia.com` to
   review your account's quota and upgrade options. NVIDIA publishes
   paid plans alongside the free developer tier.

To update your stored key once you have a new one:

```
pulsarcode /api
```

Paste the new key. Continue working.

### `504 Gateway Timeout` / stalled response

The adapter waits up to 180 seconds for NVIDIA to return streaming
headers. `ping` events keep the connection alive while it waits. If
NVIDIA is genuinely down, the adapter returns a clean assistant message
naming the upstream condition instead of hanging.

### `No NVIDIA NIM key found`

Run `pulsarcode /api` and paste a key.

### `Model not recognized`

Run `pulsarcode sonar` to see the live catalog. If the alias you typed
is missing, your key may not yet have access to that model. Pick a
different one with `pulsarcode pick`.

### Adapter port already in use

The launcher tries ports 4000-4010 until it finds a free one. If all
ten are held by stale adapters from earlier sessions, restart your
machine or kill the orphaned `python` processes whose command line
includes `nim_anthropic_proxy`.

### Picker arrow keys do nothing

You are probably running inside an environment where stdin is not a TTY
(some CI runners, certain VS Code terminal panes). The picker falls back
to numbered-list input: type a number, press enter.

### Wizard is too noisy, I want to relaunch fast

Set `PULSAR_SKIP_WIZARD=1` for one-off fast launches:

```
PULSAR_SKIP_WIZARD=1 pulsarcode
```

Or export it from your shell rc to skip permanently. Your stored key and
current model are used as-is; no prompts.

### Welcome banner shows every launch

It should only show on the very first launch after a fresh install. If it
keeps showing, the onboarding sentinel was deleted or never created. Run
`pulsarcode` once and let it complete; the sentinel will be created at
`~/.pulsarcode/onboarding_complete` and the banner will not show again.

---

## License

AGPL-3.0-or-later. See [`LICENSE`](LICENSE) for the full text.

The AGPL-3.0 license means:

- You may use this software for any purpose, free of charge.
- If you modify it, your modifications must also be AGPL-3.0.
- If you run a modified version as a network service to other users,
  you must offer those users the modified source.

The PulsarOS Intelligence Inc. upstream technology (separate from this
repository) is not licensed by this AGPL. The launcher contains no
patented technology; it is an installer, an NVIDIA NIM adapter, and a
model picker. For commercial licensing inquiries about upstream
technology, contact `yassine@pulsaros.ca`.

---

## Made by

[PulsarOS Intelligence Inc.](https://pulsaros.ca), Ottawa, Canada.

- Independent. Self-funded.
- No VC backing.
- No paid plan. No premium tier. No SaaS upsell.
- No telemetry. No analytics. No phone-home.
- This launcher is a donation to the developer community.

If you want to support: **tell another developer about it**. That is the
only thing we ask.

If you want to reach out for commercial inquiries about the upstream
technology (which is separate from this repository):
[yassine@pulsaros.ca](mailto:yassine@pulsaros.ca).

---

<div align="center">

**pulsarcode**  ·  AGPL-3.0  ·  Made in Canada

</div>
