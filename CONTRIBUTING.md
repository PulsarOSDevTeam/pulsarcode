# Contributing

Short version: **this project does not accept outside contributions**. Read on for the longer version.

---

## What this project is

`pulsarcode` is a source-available sovereign Claude Code launcher maintained by **PulsarOS Intelligence Inc.** (Ottawa, Canada). It is published here so anyone can:

- **Read** the source.
- **Download** a release zip and run it on their own machine.
- **Fork** their own copy under the AGPL-3.0-or-later license and modify it for their own use, subject to AGPL's network-use disclosure clause.

## What this project is not

It is **not** a community project. We do not run an open contribution process. Specifically:

- **We do not accept pull requests** from outside PulsarOSDevTeam. The maintainer team is the only write surface. External PRs will be closed without review.
- **We do not accept feature requests** in this repository. Issue tracking is disabled.
- **We do not run a community forum**. Discussions is disabled.
- **We do not respond to support tickets opened here**. The license disclaims warranty; the AGPL-3.0 terms apply.

This is a deliberate posture, not a hostile one. The codebase is small, opinionated, and tightly coupled to upstream design choices documented in the project. We publish it because we believe the sovereign Claude Code launcher is a useful primitive for any developer who wants to run Claude Code against a model of their own choosing. We do not publish it because we want to coordinate a community around it.

## If you have something to say

- **Bug report on a release zip**: email `yassine@pulsaros.ca`. Include the release version, your OS, and the exact reproduction. We will read it. We will not promise a fix.
- **Security disclosure**: see `SECURITY.md` if present, otherwise the same email. Please give us a reasonable window before public disclosure.
- **You want to use it for commercial work**: the AGPL-3.0 terms apply. Read the `LICENSE` file. If the network-disclosure clause is incompatible with your use case, contact PulsarOS Intelligence Inc. for a commercial license discussion.
- **You want to modify it**: fork the repository, change your fork freely under AGPL-3.0, run your fork as you wish. We do not coordinate or merge.

## Why source-available instead of closed

We publish source so the sovereign-Canadian deeptech provenance of the project is verifiable: every byte that runs on your machine is auditable in this repository. There are no hidden phones-home, no closed binary blobs, no obfuscated logic. The codebase is small enough to read end to end in an afternoon. Trust through visibility, not through marketing.

## Why source-available instead of fully open

The mechanisms in this launcher compose with upstream technology that is **not** in this repository. The PulsarOS Intelligence Inc. upstream stack is sovereign and coordinated outside the public OSS process. The launcher is small, AGPL-licensed, and forkable; the upstream is not.

---

Copyright (C) 2026 PulsarOS Intelligence Inc. AGPL-3.0-or-later. See `LICENSE`.
