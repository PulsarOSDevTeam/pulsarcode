# Changelog

All notable changes to Pulsar Code are documented in this file.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/),
and the project follows [Semantic Versioning](https://semver.org/) for
public releases.

---

## 3.0.0 (unreleased)

Pulsar Code 3 rebuilds the v1 NVIDIA NIM launcher from the ground up. It still runs the official,
unmodified Claude Code CLI, now on any model: a key from any listed provider, a local server or
any compatible URL, routed through Pulsar Core, one loopback router shared by all your sessions.
Pulsar configures Claude Code through gateway and other environment variables, a per-session
settings file and a plugin directory, and pre-seeds the isolated profile's first-run state
(onboarding done, the gateway billing notice acknowledged for sessions through Pulsar Core, and on the
direct Anthropic route your
own Anthropic API key approved in that profile). Tested with Claude Code 2.1.287 through 2.1.296.

Version 2.0.0 was never released; its changes are part of 3.0.0.

### Added

- **One command install:** `curl -fsSL .../install.sh | bash` (macOS, Linux, WSL) and
  `irm .../install.ps1 | iex` (Windows). They download from GitHub, so they need the repository
  to be public; `install.sh --local` installs a clone. The installer needs only bash, tar and curl, brings its
  own Python through uv when the system has none (or only an old one), installs Claude Code with
  Anthropic's official installer when it is missing, links `pulsarcode` and launches straight into
  the key uplink.
- **Any API key:** the provider is recognised from the key's shape, offline. A key is sent only to
  the provider it matched; ambiguous shapes ask you instead of trying the key against several
  vendors. `export NAME=value` lines, keys already exported in your shell, `https://host/v1` plus a
  key, and `ollama` / `lmstudio` work too; `pulsarcode key --stdin` and `--from-env VAR` read a
  key without typing it.
- **Providers:** 36 listed provider profiles in v3.0.0 (plus any compatible URL), including Azure
  OpenAI, Azure AI Foundry, Vercel AI Gateway, Requesty, DeepInfra, SambaNova, Novita, Alibaba
  Qwen, Nebius, Venice, Chutes, Cohere, llama.cpp, vLLM, Jan and LocalAI. Connect as many as you
  like: they share one picker, and `pulsarcode use` switches the active one.
- **Self-hosted endpoint:** `PULSARCODE_LOCAL_ENDPOINT` (alias `PULSAR_LOCAL_ENDPOINT`) names a
  self-hosted OpenAI-compatible URL, or an Anthropic Messages URL when it ends in `/anthropic`. It
  becomes a custom profile and is preferred at launch while it answers, without becoming your saved
  default; `0`, `off`, `none`, `false` or an empty value switch it off, and
  `PULSARCODE_PREFER_LOCAL=0` keeps it as a profile without preferring it. Nothing is probed when
  the variable is unset.
- **Local servers at the key prompt:** the uplink offers a model server it finds on this machine's
  loopback (Ollama, LM Studio, llama.cpp, vLLM, Jan, LocalAI); `PULSAR_OFFLINE=1` skips the lookup.
- **Pulsar Core** (`127.0.0.1:4747` by default, one per user): token-gated sessions with a route
  plan, relay to the next target on a failure before the first byte (429, 5xx, timeouts, network,
  auth errors), circuit breakers with backoff, SSE pings while a provider is opened, a loop guard
  that counts only the steps since your last message, context overflow mapped to Claude Code's
  "prompt is too long" (an overflow before any output is answered as an HTTP 400, so Claude Code
  compacts at once), an event bus and stats. It stops by itself shortly after the last session
  ends.
- **Relay rules:** automatic relay (`auto`) moves a request only from a listed hosted provider to
  up to two other listed hosted providers that have a model chosen. A local server or a custom URL
  as primary never relays automatically (a listed provider pointed at a host on your machine or
  local network counts as local); a chain you name with `pulsarcode relay` works from any primary.
  A session with Pilot armed never relays, so screenshots reach only the model you chose.
- **Three wires per model:** Anthropic Messages forwarded as is (Anthropic, Anthropic-compatible
  vendors, and Anthropic models on OpenRouter, Vercel AI Gateway and Requesty), OpenAI
  chat-completions, and the OpenAI Responses API for models that call tools only there.
- **Cloud routes** for Amazon Bedrock and Google Vertex AI, where Claude Code talks to the cloud
  directly and Core is a side channel; `PULSAR_DIRECT=1` for Anthropic-native providers.
- **Live model hot swap:** `/pulsar:switch <model>` (the `pulsar_switch` tool, this session only),
  `pulsarcode switch <model>` from another terminal, or Claude Code's `/model`; no restart.
- **`pulsarcode relay`** to show or set the relay chain (`auto`, `off`, `profile:model ...`).
- **Claude Code plugin** loaded per launch with `--plugin-dir` and `--settings`: the `pulsar` MCP
  server, `/pulsar:status`, `/pulsar:switch`, `/pulsar:stage`, `/pulsar:council`, attention hooks,
  a session header with the PulsarCode persona, a branded status line, theme and spinner verbs. MCP
  tools return typed results, and Council reports progress while the other models answer.
- **Skills in the isolated profile:** `PULSAR_SKILLS=<names>` (comma-separated skill folder names)
  copies those folders from `PULSAR_SKILLS_DIRS` (separated like `PATH` entries, default
  `~/.claude/skills`; the first folder that has a skill wins) into the isolated profile's skills
  folder at launch. Links inside a skill are skipped, and a skill over 2000 files or 64 MB is
  refused. Empty by default; not used with `PULSAR_CLAUDE_CONFIG=shared`.
- **Opt-in attribution:** on non-Claude models Claude Code's commit and pull request attribution
  is off, so no line credits a Claude model for another model's work; with
  `PULSAR_ATTRIBUTION=on` it becomes a "Built with Pulsar Code" line naming the model. With
  Anthropic and on the cloud routes, Claude Code keeps its own attribution.
- **Pulsar Stage:** decks, cards, markdown, code, tables, charts, diagrams, metrics, timelines and
  progress, validated and sanitized, shown in an animated terminal viewer (tmux, Zellij, WezTerm,
  kitty or Windows Terminal split, and iTerm2 with `PULSAR_STAGE_VIEWER=split`), a web view whose
  content security policy blocks all external requests and runs only its own script, a standalone
  HTML export, and on Claude Code 2.1.287 and later a live pane inside Claude Code through its
  function-hooks plugin API (early access).
- **Council:** ask your other connected models the same question, answers side by side. By default
  the session's other models are asked; a session with one model asks it together with up to two of
  its provider's recommended models from other vendors, on the same key.
- **Pulsar Pilot:** opt-in computer use with an animated pulsar cursor. ctypes backends for X11,
  macOS and Windows; off by default, armed with `pulsarcode pilot on` in your own terminal (never
  from inside a Claude Code session), not pre-approved (Claude Code's permission system decides),
  failsafe corner, 10 actions per second, an audit log without typed text or images. Input is
  refused when the window in front changed since the model's last screenshot (where the system
  reports it), and shortcuts that log out, lock, force-quit or power off are never pressed.
  `pulsarcode pilot tour` shows the cursor without acting.
- **Observatory (`pulsarcode top`):** live sessions, throughput, time to first token, routes,
  provider health with breaker countdowns (a provider with some failing targets is degraded, one
  with all of them failing is open), an event feed; `--once` and `--json` for pipes.
- **The terminal experience:** a boot sequence with ignition, beams, a wordmark decrypt and a
  SYSTEMS panel with a hardware row (any key skips; M opens the picker, K the uplink); an uplink
  with live provider detection and masked input; a picker with fuzzy search across your connected
  providers, recent and suggested tiers, free / thinks / no-tools tags and context sizes; haptics
  (visual pulses, optional bell); truecolor, 256-colour, `NO_COLOR`, ASCII and reduced-motion
  fallbacks, and a plain line mode for pipes and CI. `pulsarcode demo` labels its canned values as
  demo mode.
- **Distribution, ready for the first public release:** reproducible release archives with
  `SHA256SUMS`, and packaging for PyPI (`pipx`, `uv tool`), npm (pinned to its matching release
  tag), Homebrew and Scoop, all published with the first public release. PyPI and npm publish only
  after the GitHub release exists. Build provenance attestations and npm provenance are recorded
  only when the repository is public, and the files of an existing release are never replaced (a
  re-run adds missing files and fails on one that differs).
- **CI:** pull requests run the tests on Ubuntu with Python 3.9, 3.11 and 3.13, on Ubuntu ARM and
  macOS (Apple silicon) with Python 3.12, and on Windows (experimental); pushes to main and manual
  runs add macOS Intel. Also installer and wheel smoke tests from a hostile directory, em dash and
  secret scans (keys, real home folders, private addresses and secret files), and a weekly
  compatibility workflow (Mondays, and on demand) that runs Claude Code's stable and latest
  releases through Pulsar against scripted providers, plus the Stage pane plugin's own tests.
- **Docs:** `docs/ARCHITECTURE.md`, a rewritten README with recorded demos, and the project site in
  `docs/` (GitHub Pages is opt-in with `tools/github/setup.sh --pages`, which refuses while the
  repository is private).

### Changed

- Pulsar Core replaces v1's NIM-only local adapter, and v1's per-request 1-token "preflight" call
  is gone (it cost credits and latency on every prompt).
- v1's managed slash commands (`/api`, `/sonar`, `/pick`, `/model <alias>`) are removed from the
  isolated profile; the plugin's `/pulsar:*` commands and Claude Code's own `/model` replace them.
- Translation for open models: every tool call gets a fresh id, tool names are repaired against the
  declared tools (names over 64 characters get a stable alias), arguments are coerced to the tool's
  schema, tool calls emitted as text are rescued where a call can stand, interrupted tool calls get
  synthetic results and orphaned results become text, images inside tool results reach vision
  models, cached tokens and cost are reported to Claude Code, and pasted images cost a flat 1600
  tokens instead of their base64 length. A reply whose whole answer arrives as reasoning, when
  Claude Code did not ask for thinking, becomes the visible answer instead of an empty reply.
- Self-healing: rejected parameters are adjusted and retried, unknown Anthropic fields on native
  endpoints included; learned fixes expire after a week, and "max_tokens too large for the
  context" is retried once with a budget that fits.
- One TLS decision for Core and the CLI: explicit CA bundles, else the OS trust store plus certifi
  plus `NODE_EXTRA_CA_CERTS`; local model servers bypass `HTTP(S)_PROXY`; a SOCKS-only proxy
  without SOCKS support falls back to the HTTP proxy.
- A `base_url` override also moves a provider's native (Anthropic Messages) traffic to that host.
- `pulsarcode update` installs the latest release tag. It refuses tags older than v3.0.0 and, unless
  you add `--allow-downgrade`, any version older than the one running; when the latest release
  cannot be found it stops with a clear message and never falls back to `main` on its own
  (`pulsarcode update main` installs `main` when you ask for it). It ignores an exported
  `PULSAR_REF`: pass the ref as an argument. Package-managed installs print their own upgrade
  command instead.
- `PULSAR_CLAUDE_CONFIG=shared` leaves `CLAUDE_CONFIG_DIR` alone (a value you set yourself is kept),
  so Claude Code reads its configuration as it does when you run `claude` directly.
- `pulsarcode stop` retires Pulsar Core: it takes no new launches and exits once the sessions it
  serves end, so a live session's port is never left free for another process.
  `pulsarcode stop --force` stops it at once.
- Core's deadline for a local connection to send its request head has its own setting,
  `PULSAR_CORE_HEADER_TIMEOUT` (default 15 s); `PULSAR_HEADER_TIMEOUT` is only the wait for a
  provider.
- On macOS and Linux, Stage opens nothing in a run with no terminal (cron, CI, a script); on macOS
  the browser view opens behind your window, and iTerm2 is driven only with
  `PULSAR_STAGE_VIEWER=split`.
- `pulsarcode pilot status` opens no window; `pulsarcode pilot status --gui` also tests the cursor
  window.
- Wording: Pulsar Code is described as running the official Claude Code CLI on any model, with the
  trademark disclaimer and an independence line under the heading; the uplink and site list only
  API-key providers.

### Security

- Pulsar's own processes do not import from the working directory: the hooks, status line, MCP
  server, Stage viewers and Core start through a bootstrap that drops the current directory from
  `sys.path` and runs Python with `-E`, and so does the `pulsarcode` command installed by the curl
  or PowerShell installer, Homebrew, Scoop or npm. pip, pipx and uv installs start it through pip's
  console script, which honours `PYTHONPATH`: keep empty or relative entries out of it.
- Core binds `127.0.0.1` only, checks `Host` (DNS rebinding) and `Origin` (CSRF), caps body sizes,
  never follows upstream redirects, and maps each bearer token to exactly one session (only its
  SHA-256 is stored). Admin, session and read-only view tokens are separate classes; only a view
  token is accepted in a URL, and Core receives its admin token on stdin, never in its environment
  or arguments.
- On every route (the plan, relays, `/model` picks, Council, switches) Core refuses to send a key
  over plain `http` to a host outside your machine and local network unless
  `PULSAR_ALLOW_HTTP=1`, and never uses an endpoint URL that carries a user name or password.
- Core caps what it reads from a provider (error pages, whole answers, stream lines, single
  events), so an endless body cannot exhaust its memory, and Council's timeout covers the whole
  answer.
- Upstream error and diagnostic text is redacted (the target's own key, `Bearer` values, known key
  shapes) and stripped of control, bidi and zero-width characters before it reaches Claude Code,
  the event bus or stats; council answers are redacted too, and the error text of a failed
  Responses answer becomes a redacted notice, never the reply. Successful model replies pass
  through unchanged.
- Tool-call markup quoted in prose (a README, a fetched page) or in reasoning is never turned into a
  tool call: calls a host returns as text are rescued only where a call can stand, and replies of
  models that call tools natively are not searched by default.
- When another model answers Claude Code's auto-mode safety classifier, only a verdict at the very
  start of its reply counts; anything else is left to Claude Code's own safe default.
- Provider keys exported in your shell are removed from Claude Code's environment unless
  `PULSAR_KEEP_ENV_KEYS=1`. Deny rules keep Claude Code's Read and Edit tools away from Pulsar's
  keys, tokens, logs and the isolated profile's credentials, and its Edit tool away from what
  Pulsar runs; they are written on Windows too, and the isolated profile stays editable.
- The per-session settings pin every variable that routes, authenticates or arms the session, and
  each `PULSAR_` setting that changes what session code does, to the launcher's values, so a
  project's `.claude/settings.json` cannot redirect the session, swap its key or arm Pilot.
- Council is not pre-approved: Claude Code asks before the question goes to other providers,
  unless you typed `/pulsar:council`. `pulsar_switch` is allowed only through `/pulsar:switch`, and
  Pilot tools are not pre-approved. Tools that send data away (`council`, `pilot_screenshot`) are
  not marked read-only.
- From a shell inside a Claude Code session, `pilot on` is refused, and `key`, `logout`, `update`
  and `uninstall` refuse unless a person confirms at a real terminal. These checks are defence in
  depth; Claude Code's permission prompt is the boundary.
- Pilot: the failsafe corner is watched while the cursor glides and checked again before each
  input; input is refused when the window in front changed since the model's last screenshot
  (where the system reports it); shortcuts that log out, lock, force-quit or power off are never
  pressed; `pulsarcode pilot off` holds across a restart of the MCP server; arming refuses when
  Claude Code is among its ancestors; the cursor overlay gets no credentials, and a failing audit
  log is reported.
- A Pulsar home owned by another user, or one that stays writable by others, is refused before
  anything is written; an empty `PULSAR_HOME` means the default, and a relative one is refused.
- A release tag is installed only after its archive matches the release's `SHA256SUMS` (fail
  closed); older tags whose release publishes no `SHA256SUMS` fall back to GitHub's tag archive
  with a warning, a v3 tag never does. The uv installer is pinned by hash. The checksum covers
  Pulsar Code's own archive; its Python dependencies come from PyPI within the ranges in
  `requirements.txt`. `pulsarcode update` runs the installer by absolute path, from the
  installer's own folder, with a `PATH` free of empty and relative entries.
- The installers never adopt or empty a folder that holds someone else's files (a refused
  `PULSAR_HOME` is named with what is in the way), the command they install runs against the home
  it was installed into, and `install.sh` replaces another install's `pulsarcode` command only with
  `PULSAR_FORCE=1`. The installer never ends processes by name or pattern: it stops only a step it
  started itself, through that step's own handle, and asks a running Pulsar Core to stop with
  `pulsarcode stop`.
- The Windows installer resolves relative paths before it creates anything, deletes a junction as
  a link (never what it points to), and refuses a download that ends on plain `http`.
- Stage scenes drop lone surrogates, bidi controls and zero-width characters, turn line and
  paragraph separators (U+2028, U+2029, U+0085) into newlines and keep titles and other
  single-line fields on one line, and no control character reaches the terminal; a test fails on
  invisible format characters in tracked files (Trojan Source). The web view runs only its own
  script (allowed by hash), Stage's split helpers run by absolute path, an export never overwrites
  a file it did not write, and the viewer no longer copies its tokenized link to the clipboard.
- Anthropic and OpenAI Admin keys, GitHub tokens, Stripe keys and Perplexity keys are refused at
  the key prompt and never sent.

### Fixed

- A tool call cut off mid-arguments (connection drop, output limit, unclosed tag, an `incomplete`
  Responses item) is withheld with a visible note instead of being repaired into a shorter call
  that Claude Code would run. When the output token limit cut only the tool call, the turn ends
  (`end_turn`, on both OpenAI wires, and calls that finished still run) instead of making Claude
  Code resume the same reply; a reply cut inside its text still reports `max_tokens`.
- A provider that answers a streaming request with plain JSON, an error object with status 200,
  an HTML page or an empty body produces a visible answer or relays, instead of a silent reply.
- A malformed number or shape in an upstream event no longer ends the stream.
- Corporate TLS-inspecting proxies: keys that verified in the CLI now also work for chat in Core.
- Output-token limits are read from the error's limit phrase on both OpenAI wires, never from a
  number in a model id, and an output-cap error is healed instead of being taken for a context
  overflow.
- Context-overflow errors from more servers (llama.cpp, vLLM, TGI and others) become "prompt is too
  long", also when they arrive as a stream error event or an error body with status 200, and state
  the model's known window or the limit the error gives; a rate-limit message that counts tokens is
  not an overflow.
- A custom endpoint whose URL ends in `/anthropic` lists its models in the picker.
- A custom endpoint that needs no key relaunches without asking for one, and two endpoints on one
  server (for example `/v1` and `/anthropic`) get separate profiles.
- The isolated profile is recognised with any `PULSAR_HOME`, not only one whose path contains
  `pulsarcode`.
- The MCP server no longer aborts an in-flight tool call at shutdown.
- The uplink field no longer shows a key typed after a URL or a password in URL userinfo.
- `pulsarcode uninstall` removes a launcher only when it names this home exactly, takes the bin
  folder out of the Windows user `PATH` once its last launcher is gone, and prints the command that
  drops a folder fish keeps in its universal `fish_user_paths`.
- On macOS the installer writes its `PATH` line into the startup file a login bash reads
  (`.bash_profile`, `.bash_login` or `.profile`), and checks for it by exact line.
- Release archives include the Stage pane plugin's manifest.
- macOS notifications with `PULSAR_NOTIFY=os` appear: the script Pulsar built never compiled.
- `pulsarcode top --once` calls a provider OPEN only when the dashboard does.

### Migration from v1

- The installer reuses your v1 home folder, replaces v1's `canonical/` engine and says so in one
  line. On the first launch your NIM key (`nim.key`), `active_model` and `model_history` are
  imported, again with one line. The key file is then kept as `nim.key.migrated` (0600), so it is
  never imported a second time; `pulsarcode logout nvidia` removes it.
- When Pulsar imports or adopts an NVIDIA key, `NVIDIA_NIM_API_BASE` is honoured: it becomes that
  profile's base URL, and Pulsar says so in one line.
- The v1 tuning variables are read as fallbacks of their new names:
  `PULSAR_NIM_STREAM_HEADER_TIMEOUT[_REASONING]` for `PULSAR_HEADER_TIMEOUT[_REASONING]`,
  `PULSAR_NIM_FIRST_TOKEN_TIMEOUT[_REASONING]` for `PULSAR_FIRST_TOKEN_TIMEOUT[_REASONING]`, and
  `PULSAR_NIM_STREAM_PING_INTERVAL` for `PULSAR_PING_INTERVAL`; the new name wins when both are
  set. The other `PULSAR_NIM_*` settings (the v1 adapter's host and port, its token cap, the
  preflight and catalog settings) have no v3 equivalent and are not read. v1's
  `PULSAR_SKIP_WIZARD=1` turns the animation off, as `PULSAR_FX=off` does.
- Subcommands of earlier launchers (`local`, `nim`, `nvidia`, `kimi`, `gemma`, `qwen`,
  `sonar-pick`) are refused with a one-line hint on what replaces them; they never reach Claude
  Code as prompt text.
- `pulsarcode uninstall` also removes what v1 left: its files in the home and the `PATH` lines its
  installer wrote.
- v1 has no `pulsarcode update`: install 3.x with the installer (the one-liner once the repository
  is public, or `install.sh --local` from a clone until then).

---

The entries below are the record of Pulsar Code 1.x. Its tags and release files are not part of
this repository, so the download commands quoted in them do not resolve.

## v1.0.11  -  Reasoning-aware header timeout, correct error labels

### Added

- **Reasoning-aware HTTP header timeout.** NVIDIA's NIM endpoint
  frequently holds the HTTP response while a reasoning model
  deliberates; the prior 180-second header timeout aborted requests
  before any response code arrived. The streaming path now resolves
  the upstream model id before opening the connection, checks
  `is_reasoning_model`, and uses `stream_header_timeout_reasoning_s`
  (default 360 seconds) for reasoning models while standard models
  keep the 180-second default. Combined with the first-token timeout
  (90s standard, 300s reasoning), a reasoning-tagged route now has
  up to about 11 minutes total patience before the adapter falls
  back to a recovery notice.
- **One new environment override**:
  `PULSAR_NIM_STREAM_HEADER_TIMEOUT_REASONING` (default `360`).

### Fixed

- **Error notices now name the actual requested upstream model.**
  Calls to `nim_issue_notice_text` previously rendered
  `Active model: moonshotai/kimi-k2.6` even when the user had picked
  a different route, because the function used the launcher-default
  from `settings.nim_model`. The function now accepts an
  `upstream_model_id` and `public_alias`, and the messages endpoint
  resolves the requested model id once and passes it through every
  error path.
- **HTTP 504 (header-timeout) notice rewritten** to explain the
  reasoning-model behavior when applicable, with explicit recovery
  steps ( `pulsarcode pick`, suggested non-reasoning alternatives,
  env override for the timeout ). The previous notice surfaced the
  generic "did not complete this request" copy.

### Notes

This release does not change the wire format the adapter speaks to
Claude Code, the catalog merge algorithm, the picker keypress
handling, or the install flow. It widens reasoning-model patience
and corrects the model labels in error output.

### Tests

21/21 pass on every CI matrix slot.

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

- An outdated hint from the launcher's credit-exhaustion message and
  the README. Both now point at NVIDIA's plans page.

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
- No telemetry, no phone-home. The local adapter binds
  `127.0.0.1` only; the only outbound network call is the
  bearer-authenticated HTTPS request to NVIDIA NIM with the user's
  selected model and prompt.
- Idempotent installer; re-running `install.sh` refreshes the
  canonical files without touching stored credentials, active-model
  selection, or Claude Code profile.

### Contribution posture

Open source under AGPL-3.0-or-later and forkable. At this release,
pull requests, feature requests and community discussions were not
taken on this repository; `CONTRIBUTING.md` has the current policy.

### License

AGPL-3.0-or-later. PulsarOS Intelligence Inc., Ottawa, Canada.
