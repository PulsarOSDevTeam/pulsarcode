# pulsarcode

**Run the official Claude Code CLI on any model.** One command for NVIDIA NIM, OpenRouter,
OpenAI, Gemini, Groq, DeepSeek, Ollama and more.

This package is published to npm from the first public release of Pulsar Code (3.0.0) on.

```bash
npm install -g pulsarcode
pulsarcode
```

The first run installs Pulsar Code with its official installer (the same one as
`curl -fsSL https://raw.githubusercontent.com/PulsarOSDevTeam/pulsarcode/main/install.sh | bash`,
or `install.ps1` on Windows), taken from the release tag that matches this package's version,
then starts it. Every later run starts the installed copy straight away, passing your
arguments, signals and exit code through:

```bash
pulsarcode -p "explain this repo"
pulsarcode pick
pulsarcode status
```

`npx pulsarcode` works too, but only use it in a folder you trust (your home folder, your own
projects). Inside someone else's repository npm itself reads that repository's `.npmrc` and
`node_modules/.bin` before this package gets control, so a hostile repository could run its own
code or have npx fetch a different package. The wrapper removes npm's `node_modules/.bin` PATH
entries and npm-injected variables from everything it starts, but it cannot protect npm's own
start-up. The global install has no such step.

Paste an API key from any provider when asked. Pulsar Code recognises the provider from the key,
picks the strongest coding model it can reach, and opens the official Claude Code CLI on it.

## Environment

| Variable | Effect |
|---|---|
| `PULSAR_HOME` | where Pulsar Code lives (default `~/.pulsarcode`) |
| `PULSAR_REF` | branch or tag the installer is fetched from (default: the tag of this package's version, e.g. `v3.0.0`); must match `^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}$` |

Requires Node.js 18 or newer. This package has no dependencies and never builds a shell command
out of your arguments or environment.

## Links

* Source and documentation: https://github.com/PulsarOSDevTeam/pulsarcode

AGPL-3.0-or-later. Made by PulsarOS Intelligence Inc., Ottawa, Canada. Pulsar Code is an
independent project, not affiliated with or endorsed by Anthropic or any model provider.
