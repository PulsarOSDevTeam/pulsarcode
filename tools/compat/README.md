# Claude Code compatibility check

[![compat](https://github.com/PulsarOSDevTeam/pulsarcode/actions/workflows/compat.yml/badge.svg)](https://github.com/PulsarOSDevTeam/pulsarcode/actions/workflows/compat.yml)

`run.py` runs the official Claude Code CLI, unmodified and headless, through Pulsar Core against
scripted providers on loopback, and records what worked in `compat.json`. The `compat`
workflow (`.github/workflows/compat.yml`) runs it every Monday and on demand for the `stable` and
`latest` npm channels of `@anthropic-ai/claude-code`, and uploads each `compat.json` as an
artifact (`compat-stable`, `compat-latest`).

## Run it locally

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python tools/compat/run.py                       # the claude on your PATH
.venv/bin/python tools/compat/run.py --claude /path/to/claude --out /tmp/compat.json
```

To test a particular channel without touching your own install:

```sh
npm install -g --prefix /tmp/cc-latest @anthropic-ai/claude-code@latest
.venv/bin/python tools/compat/run.py --claude /tmp/cc-latest/bin/claude --channel latest
```

Options:

| Option | Meaning |
|---|---|
| `--claude PATH` | Claude Code executable (default: `$PULSAR_CLAUDE_BIN`, else `claude` on `PATH`) |
| `--out FILE` | summary file (default: `compat.json`) |
| `--cases LIST` | comma list of cases to run (default: `openai,anthropic,openai-65536,openai-1000000,picker,pins`); `openai-<window>` runs the OpenAI fake with any window |
| `--timeout SECONDS` | per Claude Code run (default 240) |
| `--channel NAME` | label stored in the summary (`stable`, `latest`, ...) |
| `--strict` | advisory checks fail the run too (none are advisory at the moment) |
| `--env NAME=VALUE` | add a variable to the scrubbed environment (repeatable), e.g. `PULSAR_PLUGIN=off` |
| `--work DIR`, `--keep` | where the scratch folder goes, and keep it (with Claude Code's transcript, stderr and debug log per case under `logs/`) |

Exit status: 0 when every required check passed, 1 when one failed, 2 for a setup problem
(Claude Code not found, an unknown case, a bad `--env`). A summary is written in every case,
including these setup failures (as a failed `setup` case).

## What a run does

For each case (`openai`: an OpenAI chat-completions provider with a 196608 token window, which
Core translates; `anthropic`: an Anthropic Messages provider, which Core forwards verbatim;
`openai-<window>`: the OpenAI provider serving that window, of which `openai-65536` and
`openai-1000000` run by default):

1. `fakes.py` starts the scripted provider on `127.0.0.1` (a random port). It only accepts keys
   containing `fake-key-not-real`.
2. A scratch `PULSAR_HOME` connects it the way a user would: `pulsarcode key --stdin` with
   `<base URL> fake-key-not-real-compat`, then `pulsarcode model <id>`.
3. `pulsarcode -p ... --output-format stream-json --verbose --include-partial-messages
   --allowedTools Bash --max-turns 4 --debug-file ...` runs Claude Code with a fully scrubbed
   environment (like `env -i`): a scratch `HOME`, no credentials, no proxy variables,
   `PULSAR_RELAY=off`.
4. The scripted model asks for one Bash tool call: `run.py probe`, which prints what the session
   sees (the context window and auto-compact variables it was given, Core's URL, whether a
   provider key is visible) and calls Core's `/v1/messages/count_tokens` with the session's own
   token. After the tool result the model streams its final answer.

Two more cases check what a session is given rather than a conversation:

- `picker`: the OpenAI fake serves `compat-chat-model` (131072 tokens) and `compat-long-model`
  (1000000), the session is launched on the small one, and `pulsarcode -p --input-format stream-json
  --output-format stream-json --verbose` gets Claude Code's SDK `initialize` request on stdin. Claude
  Code answers with the rows its `/model` picker shows, without calling a model.
- `pins`: no run at all. The Claude Code executable is read (never executed) for the names of its
  provider switches (`CLAUDE_CODE_USE_*`), Anthropic base URLs (`ANTHROPIC_*BASE_URL`) and auth skips
  (`CLAUDE_CODE_SKIP_*_AUTH`), and each one must be in the list a Pulsar session pins
  (`pulsar.plugin.pinned_names`), so a project's settings cannot switch the session to another
  provider. A few `CLAUDE_CODE_USE_*` names that do not route the model are exempt (`NOT_ROUTING` in
  `run.py`); other `*_BASE_URL` names are listed as pinned only when a project sets them.

After all cases, Core is stopped (`pulsarcode stop`, then by its recorded PID) and the scratch
folder removed (kept with `--keep`).

| Check | Passes when |
|---|---|
| `launch` | Claude Code exits 0 with a result event that holds the scripted final answer |
| `streaming` | the final answer reached Claude Code as at least three text deltas, and every scripted turn was a streaming request upstream |
| `tool_roundtrip` | the upstream asked for a Bash call, Claude Code ran it, and the tool result (with the probe output) came back upstream |
| `count_tokens` | `POST /v1/messages/count_tokens` with the session token answered 200 with a positive `input_tokens` |
| `context_window` | `CLAUDE_CODE_MAX_CONTEXT_TOKENS` in Claude Code's environment equals the model's window (the fake catalog's `context_length` for the `openai` cases, Pulsar's table for `anthropic`) and the window Pulsar stored |
| `context_effective` | Claude Code works with the largest window it can that does not exceed the model's: the `contextWindow` its final result event reports is 1000000 for a window of 1M or more (the `[1m]` model id), 200000 from 200k to 1M, and below 200k the window itself or 200000 (Claude Code has no smaller window for a model it handles as a Claude model, so there the next two limits carry the window); the auto-compact threshold, from the `effectiveWindow` in its debug log and the `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` it was given, is within 1% below where that window needs it (its size minus the answer reserve and Claude Code's 13k buffer, scaled down for small windows); and the blocking limit (`CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE`, else Claude Code's own) is below the window |
| `key_isolation` | Claude Code sees a loopback Core URL and a session token, and the provider key is nowhere in its environment |
| `picker_default` | (`picker`) the Default row of Claude Code's model list resolves to the launched model exactly, without the `[1m]` (1M window) upgrade |
| `picker_long_row` | (`picker`) the 1M model's `compat-long-model[1m]` row is still listed (`CLAUDE_CODE_DISABLE_1M_CONTEXT` hides every such row) |
| `pins` | (`pins`) every provider switch, Anthropic base URL and auth skip in the Claude Code executable is pinned in a Pulsar session |

Every check is required at the moment (`ADVISORY` in `run.py` is empty). A check listed there
would be recorded in `advisories` and printed with `--`, but would only fail the run with
`--strict`.

## compat.json

```json
{
  "claude_version": "2.1.288",
  "claude_channel": "latest",
  "ok": true,
  "advisories": [],
  "strict": false,
  "pulsar_version": "3.0.0",
  "generated_at": "2026-10-03T04:17:00Z",
  "results": [
    {"case": "openai", "wire": "openai-chat", "model": "compat-chat-model", "ok": true, "seconds": 3.1,
     "upstream_requests": 4, "checks": {"launch": {"ok": true, "detail": "..."}}}
  ]
}
```

## Safety

- Nothing is inherited from your environment: no `~/.claude`, no `~/.pulsarcode`, no shell
  credentials, no proxy settings. Everything runs on loopback.
- The key is obviously fake, and the scripted providers refuse any key that is not.
- Every process the harness starts is stopped by PID, including after a timeout.

Claude and Claude Code are trademarks of Anthropic PBC. Pulsar Code is independent and not
affiliated with or endorsed by Anthropic.
