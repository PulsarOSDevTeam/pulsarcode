# Security policy

Thank you for taking the time to make pulsarcode more secure.

## Supported versions

We only support the latest release tag on this repository. Issues found
in older versions are typically already fixed in the current release;
upgrade first, retest, then report what you still see.

## Reporting a vulnerability

**Please do not open public issues for security disclosures.** This repo
has Issues disabled to keep the read-only posture clean, and a public
security disclosure that names the bug before a fix is shipped puts
every user of the latest release at risk.

Email `yassine@pulsaros.ca` with:

1. A clear title and one-paragraph summary of the issue.
2. The minimum reproduction (a few lines of shell or Python).
3. The version you tested (`pulsarcode help` or check `CHANGELOG.md`).
4. Your OS, Python version, and a rough sketch of the threat model
   you have in mind.

We acknowledge security reports within five business days. We do not
promise a fix on any particular timeline, but we work in good faith
toward a coordinated disclosure window after a fix is published.

## Out of scope

The following are out of scope for this repository's security policy:

- Bugs in the upstream Claude Code binary (report those to Anthropic).
- Bugs in NVIDIA NIM (report those to NVIDIA).
- Bugs in any model served by NVIDIA NIM (report those to the model
  vendor).
- Feature requests or quality-of-life suggestions (we do not accept
  these on this repository; see `CONTRIBUTING.md`).

## What we consider a security issue here

- Unauthorized exfiltration of a user's NVIDIA NIM key from
  `~/.pulsarcode/nim.key`.
- Adapter binding to a non-loopback interface.
- Outbound network traffic to any host other than `integrate.api.nvidia.com`
  (or whatever the user explicitly configured via
  `NVIDIA_NIM_API_BASE`).
- Command injection in the launcher or the adapter.
- Path traversal in the installer.
- Privilege escalation through the symlinks the installer creates.
- Any leakage of prompt content to a third party other than NVIDIA NIM.

## What we do not consider a security issue

- The fact that prompts you send to a model are visible to NVIDIA. That
  is the documented architecture; if you do not want this, do not use
  NVIDIA NIM. The local adapter binds 127.0.0.1 only and only forwards
  to NVIDIA when you make a request.
- The fact that AGPL-3.0 requires you to disclose modifications if you
  run a modified version as a network service. That is the license, not
  a vulnerability.
- The fact that the launcher writes the NIM key to disk at
  `~/.pulsarcode/nim.key` mode 0600. We chose disk-stored over
  environment-variable so users can run multiple terminal sessions
  without re-pasting; if you want stricter handling, fork and modify
  your fork.

## Hall of fame

We list responsible reporters here, by name (with permission) or
anonymously, after each fixed vulnerability ships. The list is empty
today; once a reported vulnerability lands a fix, the reporter's entry
appears here.
