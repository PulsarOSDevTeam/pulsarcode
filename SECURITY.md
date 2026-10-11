# Security policy

Thank you for taking the time to make Pulsar Code more secure.

## Supported versions

| Version | Supported |
|---|---|
| 3.x (from its first release) | Yes |
| 1.x | No: install 3.x with the installer (v1 has no `pulsarcode update`) |

Fixes ship in the latest 3.x release. Issues found in older versions are often already fixed:
upgrade first, retest, then report what you still see.

## Reporting a vulnerability

**Please do not open public issues for security disclosures.** A public report that names the
bug before a fix ships puts every user of the latest release at risk.

Email `yassine@pulsaros.ca` with:

1. A clear title and one-paragraph summary of the issue.
2. The minimum reproduction (a few lines of shell or Python). Use obviously fake keys, never a
   real one.
3. The version you tested (`pulsarcode version`) and, if relevant, `claude --version`.
4. Your OS, Python version, and a rough sketch of the threat model you have in mind.

If GitHub private vulnerability reporting is enabled on this repository, you can also use
**Security, Report a vulnerability** on the repository page; both channels reach the same people.

We acknowledge security reports within five business days. We do not promise a fix on any
particular timeline, but we work in good faith toward a coordinated disclosure window after a fix
is published.

## Threat model (summary)

The full design is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), section "Security model".

- **Loopback only.** Pulsar Core binds `127.0.0.1`, checks `Host` (DNS rebinding) and refuses
  foreign `Origin`s (browser CSRF). Request bodies are size-capped, and upstream redirects are not
  followed, so a key cannot be bounced to another host.
- **Token-gated.** Every Core route except `/healthz` and `/api/hello` needs a token: a per-session token (`pls_`, only its
  SHA-256 is stored) that acts as exactly one session, an admin token (`pla_`, in
  `run/core.json`, mode 0600) for your own shell tools, or a derived read-only view token
  (`plv_`) for the Stage browser view. Only a view token is accepted in a URL; session and admin
  tokens must come in a header.
- **Keys go only to their provider.** Keys are detected locally by shape and sent only to the
  provider they matched (or the endpoint you typed); ambiguous shapes ask instead of probing
  vendors, and credentials that cannot run a model (Admin keys, GitHub tokens, Stripe keys) are
  refused. On every route (the plan, relays, `/model` picks, Council, switches) Core refuses to send
  a key over plain `http` to a host outside your machine and local network unless
  `PULSAR_ALLOW_HTTP=1`, and never uses an endpoint URL that carries a user name or password.
- **Keys stay out of Claude Code.** On the default route Claude Code only sees a local session
  token. Core reads keys from `keys.json` (0600) or receives shell-only keys in memory over the
  token-gated loopback. Provider keys exported in your shell are removed from Claude Code's
  environment unless `PULSAR_KEEP_ENV_KEYS=1`. Deny rules keep Claude Code's Read and Edit tools
  away from Pulsar's keys (`keys.json`, v1's `nim.key`), `config.json`, the tokens in `run/` and
  `settings.json`, the logs and the isolated profile's credentials, and its Edit tool away from
  what Pulsar runs (`app/`, `venv/`, `bin/`, `plugin/`, `cache/`). The isolated profile itself
  stays editable, for Claude Code's memory and your skills.
- **Pinned session settings.** The per-session settings file repeats the launcher's own value (or
  an empty one) for every variable that routes, authenticates or arms the session and for each
  `PULSAR_` setting that changes what session code does, plus an empty `apiKeyHelper`. A
  project's `.claude/settings.json` therefore cannot send the session to another host, swap its
  key, route it through its own proxy or arm Pilot.
- **State at rest.** `~/.pulsarcode` is 0700, secrets are 0600, writes are atomic. A home that
  another user owns, or that stays writable by others after `chmod 0700`, is refused before
  anything is written (POSIX), and so is a folder Pulsar Code cannot tell is its own.
- **No imports from the working directory.** The hooks, status line, MCP server, viewers and Core
  start through a bootstrap that drops the current directory from `sys.path` and runs Python with
  `-E`, so a repository that ships its own `pulsar/` package or shadows the standard library
  cannot run code when you start Pulsar inside it. The `pulsarcode` command starts the same way
  when installed by the curl or PowerShell installer, Homebrew, Scoop or npm. pip, pipx and uv
  installs start it through pip's console script, which honours `PYTHONPATH`: keep empty or
  relative entries out of `PYTHONPATH`.
- **Redaction.** Text Pulsar builds from upstream answers (error details, notices, diagnostics,
  event bus and stats entries, council answers) has the target's own key, `Bearer` values and
  known key shapes masked; error details and notices are also stripped of control characters, and
  of bidi and zero-width characters before masking, so a hidden character cannot split an echoed
  key. A successful model reply is passed through to Claude Code unchanged.
- **Tool calls in text.** Calls a host returns as text are turned into real tool calls only where a
  call can stand (the reply opens with it, or only further calls follow it); markup quoted in
  prose or in reasoning stays text.
- **Human gates.** Model switching from inside a session goes through the `pulsar_switch` tool,
  which moves only the calling session. Council, which sends a question to your other connected
  providers, and Pilot (computer use) are not pre-approved: Claude Code asks you. From a shell
  inside a Claude Code session, `pilot on` is refused, and `key`, `logout`, `update` and
  `uninstall` refuse unless a person confirms at a real terminal. These checks catch a model's shell
  as it normally runs (session variables, no terminal, a Claude Code process among its ancestors);
  a command built to evade them can pass, so Claude Code's permission prompt, which asks you before
  a command runs, is the boundary.
- **Pilot.** Off by default; armed with `pulsarcode pilot on`, or with `PULSAR_PILOT=1` for one
  launch you start yourself at a terminal. The failsafe corner is watched while the cursor glides
  and checked again before each input; input is refused when the window in front changed since the
  model's last screenshot (where the system reports it: macOS, Windows, an X11 window manager);
  shortcuts that log out, lock, force-quit or power off are never pressed; `pulsarcode pilot off`
  also holds for sessions already running; the cursor overlay gets no credentials.
- **Loopback port.** The launcher only trusts a Core that proves it holds the admin token, so a
  process squatting on the port is never handed keys or a session. Claude Code itself, though,
  sends its session token and the conversation to Core's port without such a check. Core keeps the
  port while any session it serves is live (`pulsarcode stop` waits for them unless you add
  `--force`), but if Core dies mid-session, another local process that takes the port could receive
  that session's next request.
- **Data flow.** Prompts go to the provider you picked; to your relay chain when it fails before
  the first byte (default `auto`: only from one of the listed hosted providers to up to two other
  listed hosted providers, never from a local server or a custom URL, and never in a session with
  Pilot armed; `pulsarcode relay off` disables it); and to other connected providers when Council
  is used. Nothing goes to PulsarOS. There is no Pulsar telemetry, and Claude Code's telemetry, error reporting and
  auto-update are switched off for Pulsar sessions.
- **Supply chain.** A release tag is installed only after its archive matches the release's
  `SHA256SUMS` (a mismatch, a missing entry or an unreachable `SHA256SUMS` stops the install);
  older tags whose release publishes no `SHA256SUMS` fall back to GitHub's tag archive with a
  warning. The checksum covers Pulsar Code's own archive; its Python dependencies come from PyPI
  within the version ranges in `requirements.txt`. `SHA256SUMS` comes from the same release, so it
  shows a download is intact, not who built it: release artifacts published while the repository
  is public carry build provenance attestations (`gh attestation verify`), and the files of a
  published release are never replaced. The uv installer the installer may fetch is pinned by
  hash. The default one-line installer tracks `main` and is protected by HTTPS only; pin
  `PULSAR_REF=<tag>` for a checksum-checked install. `pulsarcode update` installs release tags only
  (v3.0.0 or later, and never older than the running version unless you add `--allow-downgrade`),
  never falls back to `main` on its own and does not read an exported `PULSAR_REF`; it runs the
  installer by absolute path, from the installer's own folder, with a `PATH` free of empty and
  relative entries. The installer ends only the processes it started, through their own handles,
  never by name or pattern, and never replaces another install's `pulsarcode` command unless
  `PULSAR_FORCE=1`.

## In scope

- Any path that sends an API key to a host other than the provider it was matched to (or the
  endpoint the operator typed), including through redirects, relays, council calls or logs, or
  over plain `http` to a host outside your machine and local network without
  `PULSAR_ALLOW_HTTP=1`.
- A key, a session or admin token, or other secret reaching Claude Code's environment or
  transcript, the event bus, stats, the Observatory, Stage, a log file, a URL, argv or the screen.
- Pulsar Core reachable from anything other than loopback, serving a request without a valid
  token, or letting one session's token act on another session, the admin API or another user's
  Core.
- DNS rebinding, CSRF or cross-origin access to Core or the Stage web view; script execution in
  the Stage web view or terminal escape injection through scene content or upstream text.
- Code execution from the working directory (a planted `pulsar/` package, stdlib shadows,
  `node_modules/.bin`, `PYTHONPATH` tricks) when you start Pulsar, its hooks, viewers or Core.
- A model arming Pilot, approving its own Council or switch calls, or running `key`, `logout`,
  `update` or `uninstall` without a human at a real terminal.
- A request from a session with Pilot armed (its screenshots included) reaching any provider other
  than the one you chose, or Pilot input reaching a window that changed since the model's last
  screenshot on a system that reports the window in front.
- A project's own settings changing a variable Pulsar pins for the session (where it sends
  requests, which key or proxy it uses, whether Pilot is armed).
- Unauthorized reads or writes of `~/.pulsarcode/keys.json`, `config.json` or `run/`.
- Command injection, path traversal or privilege escalation in the launcher, installers (including
  the symlink and PATH changes they make), the npm wrapper or `pulsarcode update`.
- Installing a release tag whose archive does not match its `SHA256SUMS`.
- Outbound traffic other than: the providers you connected (and their model catalogs, including a
  self-hosted endpoint you named with `PULSARCODE_LOCAL_ENDPOINT`), GitHub during install and
  update, PyPI (`pypi.org`, `files.pythonhosted.org`) for Pulsar Code's Python dependencies during
  install and update, `astral.sh` (uv) and Anthropic's official Claude Code installer when those
  are missing, and the Pulsar Code site links you open yourself.

## Out of scope

- Bugs in the upstream Claude Code binary (report those to Anthropic).
- Bugs in a model provider's API (report those to that provider).
- Bugs in any model a provider serves, including what a model chooses to do with tools you
  approved (report model behaviour to the model vendor).
- The fact that the prompts you send are visible to the provider you picked, to your relay chain
  and, when Council is used, to the providers it asks. That is the architecture; pick providers
  you trust, turn relay off (`pulsarcode relay off`), or run a local model.
- The default one-line installer installing `main` without a checksum. That is documented; pin a
  release tag for a checksum-checked install.
- A command you approved in Claude Code getting past the in-session checks (session variables, a
  real terminal, Claude Code among its ancestors). Those checks are defence in depth; Claude Code's
  permission prompt is the boundary.
- The fact that keys are stored on disk (mode 0600) rather than only in environment variables.
  Exported variables such as `OPENROUTER_API_KEY` are still honored if you prefer not to store
  anything.
- Attacks that need an attacker already running code as your user on your machine, beyond the
  working-directory cases listed above.
- The fact that AGPL-3.0 requires you to disclose modifications if you run a modified version as
  a network service. That is the license, not a vulnerability.
- Feature requests or quality-of-life suggestions (see [CONTRIBUTING.md](CONTRIBUTING.md)).

## Hall of fame

We list responsible reporters here, by name (with permission) or anonymously, after each fixed
vulnerability ships. The list is empty today.
