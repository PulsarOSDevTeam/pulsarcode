# Local Pulsar / pulsarcode  (team distribution)

Sovereign Claude Code for teams. Each developer brings their own free NVIDIA NIM
API key, picks any model from the live Sonar catalog, and gets Claude Code's
exact UX with the model layer swapped underneath.

The model is commodity GPU. The moat is the files, the skills, the memory, and
the living files around you. Every model swap leaves the workshop unchanged.

---

## What you get

- One command (`pulsarcode`) that launches Claude Code against any NIM model.
- A first-launch wizard that walks you through NVIDIA NIM signup, key paste,
  arrow-key Sonar picker, and model selection.
- A live catalog of 55+ NIM routes (Kimi K2.6, Qwen 3 Coder 480B, DeepSeek V4,
  GPT-OSS 120B, Llama 3.3, GLM 5.1, Magistral, plus lightweight options), with
  dynamic discovery via PulsarOS API Sonar.
- Persisted model selection at `~/.pulsarcode/active_model` so your choice
  survives across shells and reboots.
- Four in-session slash commands: `/api`, `/sonar`, `/pick`, `/model <alias>`.
- Zero modification of your system claude binary or your `~/.claude` profile.
  Everything is isolated to `~/.pulsarcode/`.

---

## Install

```bash
bash install.sh
```

The installer is idempotent. Re-run it any time to refresh the canonical files.
It never touches your stored NIM key, your active model selection, or your
Claude Code profile.

System requirements:

- macOS 14+ or Linux (Ubuntu 22.04+ / Debian 13+ / Fedora 40+).
- Python 3.11 or newer (`brew install python@3.14` on macOS;
  `apt-get install python3.12 python3.12-venv` on Debian/Ubuntu).
- Claude Code already installed (`https://claude.com/claude-code`).
- ~1 GB free disk for the venv.

Windows is on the roadmap. Use WSL2 today.

---

## First launch

After install, open a new terminal tab so the `PATH` update takes effect, then:

```bash
cd /path/to/your/project
pulsarcode
```

Three numbered steps run automatically on first launch:

### Step 1: NVIDIA NIM API key

You will see the pulsarcode API Core panel. It tells you to:

1. Open `https://build.nvidia.com`.
2. Sign in (or create a free NVIDIA account, no credit card needed).
3. Click `Get API Key` on any model card, then `Generate Key`.
4. Copy the key (starts with `nvapi-`).
5. Paste it into the pulsarcode prompt. The key goes to
   `~/.pulsarcode/nim.key` at mode 0600 and never leaves your Mac except as a
   bearer header to NVIDIA NIM.

Each free NIM key has its own 1000-credit bucket and its own request-per-minute
limit. Per-developer keys mean every teammate has independent throughput.

### Step 2: Sonar arrow-key picker

The picker shows the live NVIDIA NIM catalog grouped into tiers:

- **CODING TIER**: Kimi K2.6, Kimi K2 Thinking, Kimi K2 Instruct, Qwen 3 Coder
  480B, Qwen 2.5 Coder 32B / 7B, DeepSeek V4 Pro / Flash, GPT-OSS 120B.
- **GENERAL TIER**: Llama 3.3 70B, GLM 5.1 / 4.7, Magistral, MiniMax M2.7,
  Mistral Nemotron, Mixtral, Nemotron families.
- **LIGHTWEIGHT TIER**: Phi-4 Mini, Gemma 2 2B, Llama 3.1 8B, Codegemma,
  smaller Nemotron / Llama variants.
- **OTHER**: uncategorized public routes.

Move with arrow keys, Enter to select, Esc to keep the default. Your choice is
written to `~/.pulsarcode/active_model` and every subsequent `pulsarcode`
launch inherits it.

### Step 3: Claude Code launches

The launcher exports an isolated Claude Code config dir, points
`ANTHROPIC_BASE_URL` at the local NIM adapter on `127.0.0.1:4000`, and execs the
`claude` binary. From here it is the Claude Code UX you know.

---

## Switching model later

Three paths, each with different scope:

| Command | When | Scope |
|---|---|---|
| `pulsarcode pick` | fresh terminal tab | arrow-key picker, persists for next launch |
| `pulsarcode model <alias>` | shell prompt | persists for next launch (no picker) |
| `/model <alias>` | inside a Claude Code session | persists for next launch |
| Claude Code native `/model` | inside a Claude Code session | live, this session only |

The persistence file is always `~/.pulsarcode/active_model`. The launcher
resolves the alias to the upstream NIM model id via the live Sonar catalog at
each launch.

---

## Commands

```
pulsarcode                  # launch Claude Code with the active model
pulsarcode -p "prompt"      # single-prompt mode
pulsarcode /api             # paste / replace your NVIDIA NIM key
pulsarcode sonar            # print the full live Sonar catalog (table)
pulsarcode pick             # arrow-key interactive picker (fresh tab)
pulsarcode model            # print the current selection
pulsarcode model <alias>    # set the active model (validates against catalog)
pulsarcode status           # stack health, active model, NIM key state
pulsarcode help             # full help
```

---

## Troubleshooting

### `429 Too Many Requests`

Your NVIDIA NIM key bucket is throttled. Free tier limits are per-key,
per-developer, not shared across the team. Wait for the rate-limit window to
roll over (usually one minute) and retry. If you hit 429 repeatedly, you have
likely exhausted your 1000-credit allotment for the month; rotate your key by
generating a new one at `https://build.nvidia.com` and running `pulsarcode /api`.

This is a per-developer ceiling. Other teammates on the same project keep
working on their own keys at their own pace.

### `504 Gateway Timeout` or stalled response

The local NIM adapter waits up to 180 seconds for NVIDIA to return streaming
headers. If NVIDIA is slow, you see `ping` events on the wire while the wait
proceeds. If NVIDIA is genuinely down, the adapter returns a clean assistant
message describing the upstream condition instead of hanging.

### "No NVIDIA NIM key found"

Run `pulsarcode /api` and paste a fresh key.

### "Model not recognized"

Run `pulsarcode sonar` to see the live catalog. If the alias is missing,
your key may not yet have access to that model. Try a different alias.

### Adapter port already in use

The launcher tries ports `4000-4010` until it finds a free one. If all ten are
held by stale adapters from earlier sessions, restart the Mac or kill the
orphaned `python` processes whose command line includes `nim_anthropic_proxy`.

---

## Architecture

```
Your terminal
      |
      | $ pulsarcode
      v
~/.local/bin/pulsarcode  ->  ~/.pulsarcode/canonical/pulsarcode (canonical launcher)
      |
      | spawns
      v
~/.pulsarcode/venv/bin/python -m proxy.nim_anthropic_proxy   (local Python adapter)
      |
      | HTTPS, bearer = your nvapi-... key
      v
NVIDIA NIM cloud  (Kimi K2.6 / Qwen / DeepSeek / your choice)
```

The adapter on `127.0.0.1:4000` translates Anthropic Messages requests into
NVIDIA's OpenAI-compatible chat completions and streams responses back to
Claude Code with SSE keep-alive pings. Your code stays on your Mac except as
the prompt content sent to NVIDIA, which you authorize with your own key.

Privacy posture:

- Key file at `~/.pulsarcode/nim.key`, mode 0600, owned by you.
- No telemetry. No outbound calls to PulsarOS Intelligence Inc. servers.
- Claude Code's nonessential traffic is disabled (`DISABLE_TELEMETRY=1`,
  `DISABLE_AUTOUPDATER=1`, `DISABLE_ERROR_REPORTING=1`).
- Adapter binds `127.0.0.1` only. Other hosts on your LAN cannot reach it.

---

## License

AGPL-3.0-or-later. See `LICENSE` in this directory.

Copyright (C) 2026 PulsarOS Intelligence Inc. / Collapse Technologies Inc.

---

## Support

- Direct: `yassine@pulsaros.ca`.
- Issues: open a ticket on this repo.
