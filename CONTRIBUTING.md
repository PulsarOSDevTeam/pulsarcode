# Contributing

Short version: Pulsar Code is open source under the AGPL-3.0-or-later. Bug reports and feature
requests are welcome as issues; outside pull requests are merged only when agreed with the
maintainers first.

---

## What this project is

Pulsar Code runs the official Claude Code CLI on any model. It is free software under the
AGPL-3.0-or-later, maintained by **PulsarOS Intelligence Inc.** (Ottawa, Canada). Everything Pulsar
Code runs on your machine is in this repository; Claude Code itself, uv and the Python
dependencies come from their own publishers. There is no telemetry, no closed binary and no obfuscated code.

You can:

- **Read** and audit the source.
- **Install** it and run it on your own machine.
- **Fork** it and change your copy under the AGPL-3.0-or-later, including its network-use clause.

## How we take input

- **Bug reports:** open an issue with the bug report form. Include the version
  (`pulsarcode version`), your OS, the output of `pulsarcode doctor` and the exact reproduction.
  Never paste an API key. You can also email `yassine@pulsaros.ca` about a bug in a release.
- **Feature requests:** open an issue with the feature request form. We read them; the maintainers
  decide the roadmap, so there is no promise to build a request.
- **Questions and ideas:** Discussions, and the FAQ in the [README](README.md#faq).
- **Pull requests:** we merge an outside pull request only when it was agreed with the maintainers
  first. Open an issue that describes the change before you write it; a pull request without that
  agreement is closed without review.
- **Security:** never in a public issue. See [SECURITY.md](SECURITY.md).
- **Support:** best effort, through issues. The license disclaims warranty; the AGPL-3.0 terms apply.

## Commercial use

The AGPL-3.0-or-later applies; read the `LICENSE` file. If its network-use clause does not fit your
use, contact PulsarOS Intelligence Inc. about a commercial license.

## Working on the code

For a fork, or a change agreed with the maintainers:

- Python 3.9 to 3.13, standard library first. Child processes start through `pulsar.python_argv`
  or `pulsar.BOOT`, never `python -m pulsar`, so a working directory can never shadow the package.
- Tests run offline: `PULSAR_OFFLINE=1 python -m pytest -q`, and `python -m pyflakes pulsar tests
  tools` stays clean.
- No em dash characters (`python tools/release/em_dash_audit.py`) and no secrets
  (`python tools/release/secret_scan.py`, which also fails on real home folders, private network
  addresses outside the tests and secret file names). Examples use `localhost` or the
  documentation range `192.0.2.0/24`, and placeholder names such as `you` or `dev` in paths.
- New files carry the SPDX header (`AGPL-3.0-or-later`).
- Brand assets and the site are generated: change the generator in `tools/brand` or `tools/site`
  and regenerate, never the output.

---

Copyright (C) 2026 PulsarOS Intelligence Inc. AGPL-3.0-or-later. See `LICENSE`.
