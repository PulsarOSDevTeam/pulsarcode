<div align="center">

# pulsarcode

**Claude Code, free, your choice of model.**

Use Kimi K2.6, Qwen 3 Coder, DeepSeek V4, Llama 3.3, GPT-OSS 120B, and 50+
other frontier coding models inside Claude Code, with no subscription, no
credit card, and your code never leaves your machine.

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL%203.0-orange.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-1.0.0-blue.svg)](https://github.com/PulsarOSDevTeam/pulsarcode/releases)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey.svg)](#requirements)
[![Tests](https://img.shields.io/badge/tests-12%2F12%20passing-brightgreen.svg)](tests/)
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

 Step 1 of 3: NVIDIA NIM API key (free, 1000 credits, no card required)
 Step 2 of 3: pick a model from API Sonar
 Step 3 of 3: launching Claude Code

+----------------------------------------------------------------------------+
| pulsarcode  /  API Sonar  /  pick a model                                  |
+----------------------------------------------------------------------------+

  arrows up / down to move,  enter to select,  esc to keep current

  CODING TIER  largest coding-tuned routes
  -----------------------------------------
  >  [x]  nim-kimi                      moonshotai/kimi-k2.6        ctx=256K
     [ ]  nim-kimi-k2-thinking          moonshotai/kimi-k2-thinking ctx=256K
     [ ]  nim-qwen-qwen3-coder-480b     qwen/qwen3-coder-480b
     [ ]  nim-deepseek-v4-flash         deepseek-ai/deepseek-v4-flash
     [ ]  nim-openai-gpt-oss-120b       openai/gpt-oss-120b

  GENERAL TIER  mid to large general-purpose routes
  ---------------------------------------------------
     [ ]  nim-meta-llama-3-3-70b-instruct
     [ ]  nim-z-ai-glm5-1
     [ ]  nim-minimaxai-minimax-m2-7
     ...
```

Pick one, hit enter, code.

---

## Quick start (60 seconds)

```bash
# 1. Download the release zip from the latest release on GitHub.
# 2. Unzip and install.
unzip pulsarcode-v1.0.0.zip
cd pulsarcode-1.0.0
bash install.sh

# 3. Open a new terminal tab so $PATH refreshes, then in any project:
cd ~/anything
pulsarcode
```

That is it. The first launch walks you through generating a free NVIDIA NIM
API key, picking a model with arrow keys, and handing off to the official
Claude Code CLI with your chosen model wired in.

**Requires** Claude Code installed from
[claude.com/claude-code](https://claude.com/claude-code) (free download,
official Anthropic CLI). pulsarcode wraps it; it does not redistribute it.

---

## Why this exists

Claude Code's UX is, frankly, the best coding-agent UX shipped in 2026.

But the official Pro tier is **$20 USD per month**. For developers in
high-income countries that is friction; for developers in Argentina,
Vietnam, Nigeria, Tunisia, India, Indonesia, Egypt, Bangladesh, the
Philippines, Pakistan, and most of the world, $240 per year is multiple
months of average salary.

Meanwhile NVIDIA hosts Kimi K2.6 (a 1-trillion-parameter mixture-of-experts
model with 256K context, **at the time of writing arguably the strongest
open coding model on Earth**), plus Qwen 3 Coder 480B, DeepSeek V4,
Llama 3.3 70B, GPT-OSS 120B, and 50+ more, free of charge through their
`build.nvidia.com` developer platform. Free tier: 1000 credits per
NVIDIA account, no credit card required, no auto-charge.

These two things had not been connected. Now they are.

**pulsarcode is the bridge.** AGPL-3.0, source-available, sovereign
Canadian, made by a small team that earns zero dollars from this and
never will.

---

## What it costs

Nothing. Forever.

Every NVIDIA account ships with 1000 free credits. That covers roughly
10 to 50 prompts depending on prompt size and model. When you burn
through, the launcher tells you exactly how to spin up another free
NVIDIA account in 30 seconds (Gmail aliases like `you+nim2@gmail.com`
count as separate accounts on the signup form; same inbox, different
account, fresh 1000 credits). You can do this indefinitely.

We have zero financial gain from this project. The repository is
AGPL-3.0 forever. There is no premium tier, no paid plan, no SaaS
upsell, no enterprise edition, no analytics, no telemetry, no phone-home.
The only outbound network traffic is your bearer-authenticated HTTPS
request to NVIDIA, with the model and prompt you selected.

This is a donation to every developer in the world who needs a
frontier-grade coding agent and cannot or will not pay a monthly
subscription to get one.

---

## How it compares

|  | Official Claude Code | Cursor | Aider | pulsarcode |
|---|---|---|---|---|
| Subscription cost | $20 / mo (Pro) | $20 / mo (Pro) | Free, BYO API key | **Free, BYO NIM key** |
| Frontier coding models available | Claude family only | Claude + GPT mix | Any OpenAI-compatible | **55+ via NIM, incl. Kimi K2.6** |
| Per-developer free credits | No (shared org limits) | No | Provider-dependent | **Yes, 1000 per account, renewable** |
| Open source | No (closed binary) | No (closed binary) | Yes (Apache 2.0) | **Yes (AGPL-3.0)** |
| First-launch wizard | API key only | Sign-in flow | Manual config | **NIM signup + arrow-key picker** |
| Live model picker in UI | No | Limited | No | **Yes, 55+ routes, tiered** |
| Persistent model choice across shells | No | No | Manual flag | **Yes, sticky via `/model <alias>`** |
| Credit-rotation guidance | N/A | N/A | N/A | **Inline, 30-second flow** |
| Telemetry | Yes (Anthropic) | Yes (Cursor) | No | **No, zero, audited** |
| Sovereign jurisdiction provenance | US | US | US | **Canadian (Ottawa)** |
| Maintainer financial gain | $20 × N users / mo | $20 × N users / mo | None | **None** |

---

## Use cases (every developer fits at least one)

- **You cannot afford the $20 / mo subscription** but you want Claude Code's UX.
- **You want to compare models** (Kimi K2.6 vs Qwen 3 Coder vs DeepSeek V4 vs Llama 3.3) without paying for multiple subscriptions.
- **You are in a region** where $20 / mo is a meaningful chunk of your income.
- **You are a student, indie hacker, or open-source maintainer** who needs frontier model access but cannot justify the subscription.
- **You care about privacy and sovereignty** and do not want your prompts going through Anthropic's billing infrastructure.
- **You want to learn how Claude Code talks to a model** by reading a real adapter implementation (it is 600 lines of clean Python).
- **You are evaluating Claude Code for your company** and want to road-test the workflow against multiple models before deciding what to license.
- **You are stuck on a closed corporate network** that allows outbound HTTPS to NVIDIA but not to Anthropic.

---

## Features in detail

### First-launch wizard

Three numbered steps. The wizard prints a panel with the URL to open
(`build.nvidia.com`), tells you what to click, where to paste the key it
generates, and walks you straight into the model picker. No menus to hunt
through, no docs to read.

### Arrow-key API Sonar picker

Hit `pulsarcode pick` in any fresh terminal tab. The picker fetches the
**live** NVIDIA NIM catalog (55+ models, refreshed every 6 hours, plus
whatever extra routes your specific account is authorized for), groups
them into four tiers (CODING, GENERAL, LIGHTWEIGHT, OTHER), and lets
you arrow-down through the list. Enter selects. Esc keeps the current.
Choice persists to `~/.pulsarcode/active_model` so every future
`pulsarcode` launch in any shell inherits it until you change again.

### Four managed slash commands inside Claude Code

| Command | What it does |
|---|---|
| `/api` | Re-paste your NIM API key (rotation flow, full wizard inside the chat) |
| `/sonar` | Print the live catalog (table view) |
| `/pick` | Tells you to open a fresh terminal and run `pulsarcode pick` |
| `/model <alias>` | Persist a different model alias for the next launch |

### Credit rotation, when you hit your free 1000

When your account hits its rate ceiling, NIM returns HTTP 429. The
adapter catches it, formats a clear message inside the Claude Code
chat:

```
NVIDIA NIM returned 429 Too Many Requests.

Your NVIDIA NIM key is rate-limited or has burned through its free 1000
credits.

Rotate to a fresh NVIDIA account and paste its key:
  pulsarcode /api

Why a NEW account: the 1000 free credits belong to a single NVIDIA
account and do not reset for about 30 days. Sign up at
https://build.nvidia.com with a fresh email (Gmail aliases like
you+nim2@gmail.com work), generate a new nvapi- key, paste it. Your
model selection, Claude Code session, and chat history all carry over.
```

You read it, you do it, you keep working. 30 seconds.

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

### 12-test smoke suite at install time

The installer's last step runs the unit tests inside the freshly built
virtualenv. If anything fails, the install tells you. Twelve tests
covering the tier classifier (letter-boundary regex to avoid the
substring-overlap bug class), tier ordering, group rendering, active
model round-trip, and an em-dash-leak assertion on the rendered picker
output.

```
[step 8] smoke test (picker tier classifier + active_model round-trip)
  ok  12/12 tests passed
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
         |     +------------------------+
         |     | claude (Claude Code)   |
         |     | (official Anthropic    |
         |     |  binary, you install   |
         |     |  separately)           |
         |     +------------------------+
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

The local adapter is 600 lines of Python with zero non-stdlib dependencies
beyond `httpx`, `fastapi`, `uvicorn`, and `sse-starlette`. Readable in
one sitting. Auditable in one afternoon.

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

| Component | Minimum | Recommended |
|---|---|---|
| OS | macOS 14 / Ubuntu 22.04 / Debian 13 / Fedora 40 | Latest |
| Python | 3.11 | 3.14 |
| Disk | 500 MB free for the venv | 1 GB |
| Claude Code | Installed from [claude.com/claude-code](https://claude.com/claude-code) | Latest |
| NVIDIA NIM account | Free, no card, no auto-charge | Same |
| Network | Outbound HTTPS to `integrate.api.nvidia.com` | Same |

Windows: use WSL2 for now (full native Windows support is planned).

---

## Install in detail

The installer creates this layout under your home directory:

```
~/.pulsarcode/
  canonical/         <- the launcher and proxy modules
    pulsarcode       <- the bash launcher you invoke
    proxy/           <- API Sonar + NIM adapter + arrow-key picker (Python)
    tests/           <- the 12-test smoke suite
    requirements.txt
  venv/              <- isolated Python virtualenv (no system pip pollution)
  nim.key            <- your NVIDIA NIM key (chmod 600, never leaves this Mac)
  active_model       <- last alias selected via /model or pulsarcode pick
  claude_config/     <- isolated Claude Code profile (no leak to ~/.claude)
    commands/        <- the four managed slash commands
  onboarding_complete  <- sentinel so the first-launch wizard runs exactly once
  run/               <- adapter PIDs and logs (logs off by default)

~/.local/bin/pulsarcode  -> symlink into ~/.pulsarcode/canonical/pulsarcode
```

`install.sh` is **idempotent**. Re-run it any time to refresh the canonical
files. It never touches your stored NIM key, your active model selection,
or your Claude Code profile.

Skip the PATH-export step with `PULSARCODE_SKIP_PATH_EXPORT=1` if you want
to manage your shell rc by hand.

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
2. **You burned your 1000 free credits**. They do not reset on the same
   account for ~30 days. Re-pasting the same key will not help.

For the second case, rotate to a fresh NVIDIA account:

```
pulsarcode /api
```

The wizard explains the Gmail-alias trick (`you+nim2@gmail.com` counts
as a new signup). Paste the new key. Continue working. 30 seconds.

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

### macOS first-launch wizard does not appear

The wizard runs once per machine via the `~/.pulsarcode/onboarding_complete`
sentinel. Delete that file to make the wizard run again:

```
rm ~/.pulsarcode/onboarding_complete
pulsarcode
```

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

**pulsarcode v1.0.0**  ·  AGPL-3.0  ·  Made in Canada

</div>
