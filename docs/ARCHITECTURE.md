# Pulsar Code architecture

Pulsar Code turns the official Claude Code CLI into a harness for any model
provider. Claude Code stays an unmodified binary; everything Pulsar adds
lives beside it and plugs into documented integration points:
`ANTHROPIC_BASE_URL`, per-session `--settings`, `--plugin-dir` (MCP server,
slash commands, hooks), and its own terminal UIs before, beside and after
the session.

```
 you ─▶ pulsarcode ── BOOT · UPLINK · PICKER · WARP ──▶ exec claude --plugin-dir … --settings …
            │                                                   │
            │ registers a session                               │ Anthropic Messages (session token)
            ▼                                                   ▼
   ┌──────────────────────────── Pulsar Core (127.0.0.1:4747, one per user) ───────────────────────────┐
   │ gateway  /v1/messages · /v1/messages/count_tokens · /v1/models · /api/hello                         │
   │ router   session plan = [primary, relay…] · circuit breakers · retries · loop guard · self-healing │
   │ wires    OpenAI chat-completions (translate) · OpenAI Responses (responses) · Anthropic native     │
   │ bus      /pulsar/events (SSE) · /pulsar/stats · /pulsar/stage (+ /stage web view) · /pulsar/council │
   └──────────────────────────────────────────────────────────────────────────────────────────────────────┘
        ▲            ▲                 ▲                    ▲
        │            │                 │                    │
  statusline    pulsar MCP server   pulsarcode top    pulsarcode stage / browser
  (in Claude)   (in Claude: Stage,  (Observatory)     (Stage viewers)
                 Council, Pilot)
```

## Processes

| Process | Started by | Lifetime | Talks to |
|---|---|---|---|
| `pulsarcode` launcher | you | until it `exec`s Claude Code (same PID) | Core (spawn, register session) |
| Pulsar Core | first launcher (`pulsar.core.spawn`) | until about ten seconds after the last session ends (and at least two minutes after it starts); sooner when a newer Core takes over, or `pulsarcode stop` retires it, and none of its own sessions is live (`pulsarcode stop --force`: at once) | providers, all Pulsar clients |
| Claude Code | launcher (`exec`) | your session | Core only (`ANTHROPIC_BASE_URL`) |
| `pulsar` MCP server | Claude Code (plugin) | the session | Core, as the session |
| status line | Claude Code (`statusLine` setting) | one shot per refresh | Core, as the session |
| hooks (`notify`) | Claude Code (plugin hooks) | one shot | local OS (bell, notifications, vibration) |
| Observatory, Stage viewer | you, or the MCP server (auto split; on macOS and Linux only when Claude Code runs in a terminal) | until closed | Core (admin or view token) |

## Routes

`pulsarcode` picks one of three routes per launch:

| Route | When | Claude Code talks to | Core's role |
|---|---|---|---|
| core | every OpenAI-dialect or Anthropic-compatible provider (the default) | Core, with a session token | router, translator, relay |
| cloud | Amazon Bedrock, Google Vertex AI (see note) | the cloud directly, with its own auth (`CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`) | side channel only: Stage, Council, status line |
| direct | `PULSAR_DIRECT=1` with an Anthropic-native provider | the provider directly | side channel only |

Note: the cloud route also covers one profile that is left out of public lists
(`pulsar/screens.py`, `UNADVERTISED`): an Anthropic login token profile
(`claude` in `pulsar/providers.py`), which Claude Code uses itself through
`CLAUDE_CODE_OAUTH_TOKEN`.

On the cloud and direct routes Core's session token rides in
`PULSAR_SESSION_TOKEN` instead of `ANTHROPIC_AUTH_TOKEN`; it grants nothing
beyond that session's own Stage, stats and council on loopback. Cloud targets
never enter a relay chain or a council, because Core cannot reach them.

A self-hosted server joins through `PULSARCODE_LOCAL_ENDPOINT` (alias
`PULSAR_LOCAL_ENDPOINT`): an OpenAI-compatible URL, or an Anthropic Messages
URL when it ends in `/anthropic`. The launcher adds it as a custom profile and,
when one GET of its model list gets any HTTP answer at launch, runs that launch
on it (`State.override`) without making it the saved default; `0`, `off`,
`none`, `false` or an empty value switch it off, and `PULSARCODE_PREFER_LOCAL=0`
keeps the profile without preferring it. When the variable is unset nothing is
probed. The same `/anthropic` rule makes any pasted custom URL a native
(Anthropic Messages) endpoint, whose model list comes from `<url>/v1/models`.

## Sessions and tokens

* The launcher creates `run/sessions/<sid>.json` (mode 0600) with the
  route **plan** and flags, and hands Claude Code a random session token as
  `ANTHROPIC_AUTH_TOKEN`. Only the token's SHA-256 is stored.
* Core maps the bearer token to its session on every request and re-reads
  the session file when it changes, so `/pulsar:switch <model>` (the
  `pulsar_switch` tool, this session only) or `pulsarcode switch <model>`
  from another terminal (the one running session, or `--session <id>` /
  `--all` when several run) changes the model on the next message. No
  restart.
* The MCP server, status line and hooks run inside Claude Code's process
  tree, inherit `ANTHROPIC_AUTH_TOKEN`, `PULSAR_CORE` and `PULSAR_SESSION`,
  and therefore act as exactly that session.
* On the core route, provider keys stay out of Claude Code's environment.
  Core reads them from `keys.json` (0600, re-read on change) or, for keys
  that exist only in your shell's environment, receives them in memory over
  the token-gated loopback (`/pulsar/ephemeral-key`). Provider keys exported
  in your shell are removed from Claude Code's environment unless
  `PULSAR_KEEP_ENV_KEYS=1`. On the cloud and direct routes Claude Code holds
  the credentials it needs to reach that cloud or provider itself.
* Token classes: session (`pls_`), admin (`pla_`, in `run/core.json`
  0600, used by your own shell tools), view (`plv_`, derived, read-only
  stage access for the browser view). Only a view token is accepted in a URL
  (`?t=`); session and admin tokens must come in a header. The launcher hands
  Core its admin token on stdin (`PULSAR_TOKEN_STDIN=1`), never in its
  environment or arguments; a lookup of an unknown token costs Core one `stat`
  per session file, not a reread.
* Claude Code runs with an isolated profile, `claude_config/` in the Pulsar
  home (`CLAUDE_CONFIG_DIR`). `PULSAR_SKILLS` (comma-separated folder names)
  copies those skills from `PULSAR_SKILLS_DIRS` (separated like `PATH`
  entries, default `~/.claude/skills`; the first folder that has a skill
  wins) into its `skills/` at launch, skipping links and refusing a skill
  over 2000 files or 64 MB. With `PULSAR_CLAUDE_CONFIG=shared`, Pulsar uses
  your own configuration: it sets no `CLAUDE_CONFIG_DIR` of its own (a value
  you set is kept), seeds nothing and copies no skills.
* The Pulsar home must be Pulsar's and private: an empty `PULSAR_HOME` means
  `~/.pulsarcode`; a relative path, a filesystem root or your home folder
  itself is refused; on POSIX a home owned by another user, or still writable
  by others after `chmod 0700`, is refused before anything is written. An
  existing folder is adopted only with Pulsar's own evidence in it (a Pulsar
  `config.json`, v1's key file, the profile or the engine), and only a new or
  empty one is marked as created by Pulsar.

## Routing

A plan is an ordered list of targets (`profile`, `provider`, `model`). For
each request Core:

1. orders targets healthy-first (open circuit breakers go last, never dropped);
2. opens the first target, sending SSE `ping` events to Claude Code while it
   waits (Claude Code accepts pings before `message_start`);
3. on a failure **before the first byte** (429, 5xx, timeouts, network,
   401/402/404, unhealable 400) records it in that target's breaker and
   **relays** to the next target in the same response;
4. **commits** to the first target that answers 200 and streams only from it:
   translated (OpenAI chat-completions or Responses) or forwarded verbatim
   (Anthropic-native providers, so real usage and thinking pass through);
5. after the commit, a mid-stream failure closes the reply cleanly with a
   readable notice (`message_stop` is always sent).

Breakers cool down with exponential backoff (auth failures for 5 minutes,
rate limits honour `Retry-After`).

Before anything is sent, one check (`engine.transport_refusal`) covers every
way a target is reached (the plan, a relay, a `/model` pick, Council, a
switch): a key never goes over plain `http` to a host outside this machine and
the local network unless you opted in with `PULSAR_ALLOW_HTTP=1`, and an
endpoint URL that carries a user name or password is never used. Such a
target fails with a readable notice and the request relays on. Every upstream
body is read with a cap (error pages 64 KiB, whole answers 32 MiB, stream lines
8 MiB, one Anthropic event 16 MiB), so an endless answer cannot exhaust Core.

The plan is the primary target followed by the relay chain. With `auto` (the
default) a request relays only when the primary is one of the listed hosted
providers, and only to up to two other listed hosted providers that have a
model chosen: a local server or a custom endpoint as primary gets no automatic
relay, and local servers, custom endpoints and cloud routes are never
automatic targets (a listed provider pointed at a host on this machine or the
local network counts as local). A chain named with `pulsarcode relay` applies
to any primary. A session with Pilot armed never relays, so its screenshots
reach only the chosen model.

### Wires

Each target resolves to one wire, decided per model:

* **native** (Anthropic Messages, passed through untranslated) for Anthropic,
  Anthropic-compatible vendors, and Anthropic models behind gateways that
  expose a Messages endpoint (OpenRouter, Vercel AI Gateway, Requesty route
  `anthropic/*` this way). Reasoning signatures minted by Pulsar for other
  models are stripped before a transcript is relayed to a native target.
* **responses** (OpenAI Responses API) for models that only call tools
  there (current codex and gpt-6 families on OpenAI and Azure).
* **chat** (OpenAI chat-completions) for everything else.

Auth headers come from the provider (`Authorization: Bearer`, Azure's
`api-key`, `x-api-key`); Anthropic beta headers are forwarded to Anthropic
itself and not to other providers. A profile's `base_url` override moves its
native traffic to that host too.

### Never a silent reply

A provider that answers a streaming request with a plain JSON completion,
an error object with status 200, an HTML page or an empty body still
produces a visible answer: the JSON completion is replayed as a stream, and
everything else becomes a readable notice. Non-streaming garbage relays to
the next target instead of failing the request.

### Self-healing

When a provider rejects a parameter (`max_tokens must be <= 8192`,
`max_completion_tokens`, `temperature` on reasoning models,
`stream_options`, strict JSON schemas, images, or unknown Anthropic fields
on native endpoints), Core adjusts the request, retries, and persists the fix
per model in `cache/heals-<profile>.json`, so a known error does not cost
another round trip. Learned fixes expire after a week, so a provider that
lifts a limit is picked up again.

"max_tokens is too large for the context" is not an overflow when the prompt
itself fits: Core retries once with the answer budget that fits
(context minus input) and does not persist it. On both OpenAI wires an
output cap is read from the error's limit phrase, never from a number inside a
model id, and an output-cap error ("max_tokens is too large for model X; at
most 8192") is healed, not taken for an overflow.

### Loop guard

If the transcript shows the model repeating the identical step four times
(same tool call and same result, or the same text) since your last message,
Core answers with an explanation instead of calling the provider again. This
stops runaway loops that would otherwise burn quota.

### Context overflow

Provider context-length errors become Anthropic's own
`invalid_request_error: prompt is too long: N tokens > M maximum`, so Claude
Code's compaction logic engages. An overflow that arrives before anything was
sent to Claude Code is answered as an HTTP 400 with that error, so Claude Code
compacts at once instead of retrying the whole prompt. One detector
(`translate.context_overflow`) reads the wordings of OpenAI-compatible servers,
vLLM, llama.cpp, TGI, Gemini and Anthropic, in error responses, stream error
events and error bodies sent with status 200; a rate-limit message that counts
tokens is not an overflow. `M` is the model's known window, else the limit the
error states, else just under the estimate.

Every model row carries `behavesAs`, so Claude Code treats the model as a
Claude model with a 200k window and ignores `CLAUDE_CODE_MAX_CONTEXT_TOKENS`
(it reads that variable only for models it does not know; Pulsar still sets
it, and the compat probe reads it). The real window reaches Claude Code like
this instead (`pulsar/launch.py`, `claude_model_id` and `compact_env`):

* 1M or more (Core routes): the model id gets the `[1m]` suffix, Claude
  Code's 1M window. Core strips the suffix and never forwards the 1M beta header to a provider
  other than Anthropic. Larger windows are used as 1M.
* 200k to 1M: nothing; Claude Code works with 200k.
* below 200k: Claude Code still reports 200k, so auto-compact is moved to the
  real window: `CLAUDE_CODE_AUTO_COMPACT_WINDOW` (Claude Code floors it at
  100k), below 100k also `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`, and
  `CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE` so no request can exceed the window.

`tools/compat/run.py` (check `context_effective`) verifies this against the
real Claude Code.

## Translation (OpenAI dialects)

`pulsar/translate.py` (chat-completions) and `pulsar/responses.py` (OpenAI
Responses API) are pure modules with a streaming state machine:

* ordered thinking, text and tool-use blocks; fresh unique tool ids;
  tool-call arguments are buffered, schema-coerced and repaired at block
  close;
* tool-call rescue for hosts that return calls as text (`<tool_call>`,
  `<function=…>`, `[TOOL_CALLS]`, bare JSON), only where a call can be: the
  reply opens with it, or nothing but whitespace and further calls follows
  it. Markup quoted in prose (a README, a fetched page) stays text, exactly
  as written. Reasoning is searched only when the caller asks
  (`rescue_reasoning`), and replies of models that call tools natively
  wherever they are served (GPT, the o-series, Gemini, Claude, Grok) only when
  the caller says the host needs it (`rescue=True`); tool-name repair against
  the declared tools;
* images as `image_url`/`input_image`; documents; mid-conversation system
  notes folded so they never split a tool call from its results;
* interrupted tool calls get synthetic results; orphaned results become text;
* a reply whose whole answer arrives as reasoning, when the client did not
  ask for thinking, becomes the visible answer instead of an empty reply (it
  is shown as written and never searched for calls);
* one stop rule on both wires (`translate.final_stop`): when the output cap
  cut only a tool call, that call is withheld with a note and the reply ends
  with `end_turn`, or with `tool_use` when another call finished, so finished
  calls still run;
* a malformed number or shape in an upstream event is ignored, never the end
  of the stream; the error text of a failed Responses answer is cleaned,
  redacted and capped, and becomes a notice, never the reply;
* usage, cached tokens and cost are reported back to Claude Code.

## Plugin

`pulsar/plugin.py` writes the plugin and settings Claude Code loads per
launch with `--plugin-dir` and `--settings`, so it does not edit your Claude
configuration:

* `plugin/<version>-<hash>/` is immutable: written once and not changed while
  a session uses it (the hash covers the content: interpreter, app path,
  `PULSAR_NOTIFY`); copies nobody launched with for two days are removed;
* `run/sessions/<sid>/settings.json` (0600) is per session, so two launches
  with different models never overwrite each other (`settings.json` in the
  Pulsar home when there is no Core session);
* MCP server `pulsar`: `stage_present`, `stage_show`, `stage_clear`,
  `council`, `pulsar_status`, `pulsar_switch`, and `pilot_*` when Pilot is
  enabled;
* commands `/pulsar:status`, `/pulsar:switch`, `/pulsar:stage`, `/pulsar:council`;
* hooks: `SessionStart` (it prints the startup banner: the PulsarCode persona
  beside the name, the model Core really routes to, its provider and the relay
  chain, under Claude Code's own header, which a launcher cannot change), and unless
  `PULSAR_NOTIFY=off`, `Notification` (attention) and `Stop` (done), all
  through `pulsar.notify`. The default (`PULSAR_NOTIFY=on`) answers with a
  terminal notification escape that Claude Code writes for it (OSC 9, 777 or
  99), a bell on attention, and vibration under Termux. `bell` rings the bell
  only, `loud` also rings it when done, `os` uses the OS notifier instead
  (opt-in), `off` disables the pings; `PULSAR_NOTIFY_OSC=9|777|99` forces the
  escape;
* permissions: `stage_present`, `stage_show`, `stage_clear` and
  `pulsar_status` stay on this machine and are pre-approved. `council` sends
  the question to your other connected providers (by default the session's
  other models, up to four; a session with one model asks it together with up
  to two of its provider's recommended models from other vendors), so Claude
  Code asks first unless you typed `/pulsar:council`; `pulsar_switch` is
  allowed only through `/pulsar:switch`; Pilot tools are not pre-approved.
  `council` and `pilot_screenshot` are annotated as tools that send data away,
  never as read-only. Deny rules (`plugin._deny_rules`, `DENY_READ` and
  `DENY_EDIT`) keep Claude Code's Read and Edit tools away from `keys.json`,
  v1's `nim.key`, `config.json`, `settings.json`, `run/`, `logs/` and the
  isolated profile's credentials, and its Edit tool also away from `cache/`,
  `plugin/`, `app/`, `bin/`, `venv/` and the profile's `.claude.json` and
  `settings.json` (`Edit(...)` covers every file-editing tool, so there is no
  separate `Write(...)` rule). The rest of the isolated profile stays
  editable, for Claude Code's memory and your skills. The rules name the home
  as given and resolved, and on Windows use Claude Code's `//c/...` spelling;
* environment pins: the per-session settings repeat the launcher's own value
  (or an empty one) for every variable that routes, authenticates or arms
  the session (`ANTHROPIC_BASE_URL`, the proxies, `NODE_OPTIONS`,
  `PULSAR_CORE`, `PULSAR_PILOT`, ...) and for each `PULSAR_` setting that
  changes what session code does (`plugin.SESSION_VARS`), plus an empty
  `apiKeyHelper`. `--settings` outranks a project's `.claude/settings.json`,
  so a repository cannot redirect the session, swap its key or arm Pilot;
  only looks (`PULSAR_ASCII`, `PULSAR_COLOR`, `PULSAR_SEED`,
  `PULSAR_HAPTICS`) stay free. A provider key is never written there;
* settings: branded status line, spinner verbs, the model picker, the Pulsar
  theme (isolated profile), `disableAutoMode` with `PULSAR_AUTO_MODE=off` and,
  on every route but the Anthropic provider and the cloud route, commit and
  pull request attribution: empty (off) by default, so no line credits a
  Claude model, and a "Built with Pulsar Code" line naming the model with
  `PULSAR_ATTRIBUTION=on`. On the Anthropic provider and the cloud route
  Claude Code keeps its own. Status line colours follow the terminal
  (`COLORTERM`, `NO_COLOR`, `TERM=dumb`); `PULSAR_COLOR=24|256|0` forces a
  depth.

## Stage

Scenes (deck, card, markdown, code, table, chart, diagram, metrics,
timeline, progress) are validated and sanitized by `pulsar/stage/schema.py`
(control characters and escape sequences are stripped, single-line fields
kept on one line, sizes capped), posted to Core, and fanned out to viewers:
an animated terminal viewer, which never writes a control character to the
terminal, and a self-contained web view served by Core under a content
security policy that blocks all external requests. The page's one inline
script runs by its hash (`pulsar.stage.web.script_hash`), so a script
injected into the page would never run, and its lookup tables have no
prototype, so scene text such as `constructor` is only text. Decks export to
standalone HTML under the same policy; an export never overwrites a file it
did not write.

When the model first presents, `pulsar/stage/launcher.py` puts a viewer on
screen (`PULSAR_STAGE_VIEWER`): `auto` tries a tmux, Zellij, WezTerm, kitty
or Windows Terminal split, then the browser (on macOS opened behind your
window); `split` also drives iTerm2, through AppleScript, only on request;
`browser` and `none` do what they say. On macOS and Linux nothing opens when
Claude Code runs without a terminal (cron, CI, a script). Every helper runs
by an absolute path from an absolute `PATH` entry that is not the current
folder, so a program a project ships never runs.

## Pilot (computer use)

Off by default (`pulsarcode pilot on`). Screenshot, pointer and keyboard
backends use ctypes only (X11/XTest, macOS CoreGraphics, Windows user32/gdi32);
Wayland is reported as unsupported. Pilot tools are not pre-approved: Claude
Code's permission system decides each action (in its default mode it asks
you). Actions also pass a failsafe corner, a rate limit, and a local audit
log that never stores typed text or images. A session with Pilot armed never
relays, so screenshots reach only the chosen model. A separate overlay process
draws the animated pulsar cursor where the model is about to act; it gets no
credentials from the environment.

Each action runs: enabled? -> rate limit -> failsafe corner -> the cursor
glides there while the corner is watched -> failsafe corner and front window
checked again -> real input -> audit line. The front-window check compares
what has the keyboard focus with what had it at the last screenshot, where
the backend can tell (macOS, Windows, an X11 window manager), so input never
goes to an app the model has not seen. Shortcuts that log out, lock,
force-quit or power off (`backends.refuse_keys`) are never pressed.
`pulsarcode pilot off` holds across a restart of the MCP server.

Arming refuses inside a Claude Code session, without a terminal, and when a
Claude Code process is among the command's ancestors. These checks catch the
model's Bash tool as it normally runs; a command built to evade them can pass,
so the boundary is Claude Code's permission prompt before it runs a command.
`pulsarcode pilot status` only reports; `--gui` also starts a test cursor
window.

## Networking

`pulsar/netcfg.py` makes one TLS decision for Core (httpx) and the CLI
(urllib): an explicit bundle (`PULSAR_CA_BUNDLE`, `SSL_CERT_FILE`,
`REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`), else the OS trust store
(`truststore` when installed) plus certifi, plus `NODE_EXTRA_CA_CERTS`. A key
that verifies in the CLI therefore also works in Core behind TLS-inspecting
corporate proxies. For a bundle you name (or `NODE_EXTRA_CA_CERTS`), Python
3.13's `VERIFY_X509_STRICT` profile checks are relaxed, because many
inspecting CAs lack the key usage extension they demand; chain, hostname and
expiry checks stay. Verify flags apply to a whole SSL context, so with
`NODE_EXTRA_CA_CERTS` on top of the OS store the relaxation covers the OS
store too; a bundle you name replaces the OS store, so trust is limited to it
(and `NODE_EXTRA_CA_CERTS`); with neither, Python's own settings apply. Model
servers on loopback, private ranges and `*.local` never go through
`HTTP(S)_PROXY` (`PULSAR_PROXY_LOCAL=1` opts back in), and a SOCKS-only
`ALL_PROXY` without SOCKS support degrades to the HTTP proxy instead of
failing every request.

## Security model

* Core binds `127.0.0.1` only, checks `Host` (DNS rebinding) and refuses
  foreign `Origin`s (browser CSRF); bodies are size-capped; upstream
  redirects are never followed, so a key cannot be bounced to another host.
  The launcher only trusts a Core that answers `/healthz?nonce=` with an HMAC
  of its admin token, so a process squatting on the port is never handed
  keys or a session. Claude Code itself sends its session token and the
  conversation to the port without such a check: Core keeps the port while a
  session it serves is live (`pulsarcode stop` retires it), but if Core dies
  mid-session, another local process that takes the port could receive the
  next request.
* Keys are detected locally by shape and sent only to the provider they
  matched; ambiguous shapes ask instead of probing vendors, and credentials
  that cannot run a model (Admin keys, GitHub tokens, Stripe keys) are
  refused. Core never sends a key over plain `http` to a host outside this
  machine and the local network without `PULSAR_ALLOW_HTTP=1`, nor to a URL
  that carries credentials (see Routing).
* State lives in `~/.pulsarcode` (0700) with secrets 0600, written atomically,
  in a home that must be Pulsar's and private (see Sessions and tokens).
* Pulsar processes do not import from the current directory. The shim, hooks,
  status line, MCP server and Core all start through a bootstrap
  (`pulsar.BOOT`, `pulsar.python_argv`) that drops the working directory
  from `sys.path` before the first import and runs Python with `-E`, so a
  repository that ships its own `pulsar/` package or shadows the standard
  library cannot run code when you start Pulsar inside it. The exception is
  the `pulsarcode` console script of a pip, pipx or uv install, which
  honours `PYTHONPATH`: keep empty or relative entries out of it.
* Text that Pulsar itself produces from upstream answers is redacted
  (`pulsar/redact.py`, applied in `pulsar/core/engine.py`): error details,
  notices, diagnostics, event bus and stats entries, and council answers.
  The target's own key, `Bearer` values and known key shapes are masked
  (error details and notices are also stripped of control, bidi and
  zero-width characters first), so an error that echoes your key is masked
  before it reaches the transcript or the next provider in a relay chain.
  A successful model reply is passed through to Claude Code unchanged.
* When another model answers Claude Code's auto-mode safety classifier, Core
  accepts a verdict only as a `<block>` tag at the very start of the reply;
  anything else is passed through untouched, so Claude Code's own safe
  default applies.
* `key`, `logout`, `update`, `uninstall`, switching other sessions and nested
  launches with permission flags refuse inside a Claude Code session unless
  a person confirms at a real terminal, and `pilot on` is refused there.
  Like Pilot's arming checks, they catch a model's shell as it normally
  runs; Claude Code's permission prompt is the boundary.
* Model switching from inside a session goes through the `pulsar_switch` MCP
  tool, which only moves the calling session. It is not pre-approved for the
  model: Claude Code asks you, except for `/pulsar:switch`, which you type
  yourself and which allows the tool for that command.
* No Pulsar telemetry. Claude Code telemetry and error reporting are
  disabled for Pulsar sessions, and so is its auto-update unless
  `PULSAR_CLAUDE_AUTOUPDATE=1`.

## Files

```
pulsar/
  cli.py          commands and the launch flow
  launch.py       Claude Code discovery, session registration, environment
  plugin.py       Claude Code plugin + settings generation
  core/           state, spawn, server, engine (router), stats, client
  translate.py    Anthropic <-> OpenAI chat-completions
  responses.py    Anthropic <-> OpenAI Responses API
  providers.py    provider registry and local key detection
  catalog.py      model catalogs, ranking, tiers, search
  local.py        loopback discovery of local model servers
  netcfg.py       TLS trust and proxy decisions shared by Core and the CLI
  redact.py       secret masking for errors, notices, diagnostics, stats and council answers
  mcp/server.py   the pulsar MCP server
  stage/          scene schema, terminal and web viewers, export, split launcher
  pilot/          computer-use backends, safety, overlay cursor, tools
  observatory.py  pulsarcode top
  statusline.py   the in-session status line
  notify.py       attention pings and haptics
  fx.py term.py screens.py hardware.py   the terminal experience
```
