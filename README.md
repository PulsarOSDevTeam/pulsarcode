# PulsarCode Team Distribution

**Sovereign Claude Code for Teams** - A free local AI coding system powered by NVIDIA NIM Kimi K2.6.

- **Zero Anthropic API calls** at runtime
- **NVIDIA NIM Kimi K2.6** as default model (1T MoE, 256K context)
- **One-command install** for macOS and Linux
- **Team-ready** with shared configuration support

---

## Quick Start

### 1. Download and Extract

Download the release ZIP and extract it:

```bash
unzip pulsarcode-team-v1.0.0.zip
cd pulsarcode-team-v1.0.0
```

### 2. Run Installer

```bash
bash install.sh
```

The installer will:
- Detect your OS (macOS/Linux)
- Check Python 3.12+ availability
- Create an isolated Python virtual environment
- Install all dependencies
- Guide you through NVIDIA NIM API key setup
- Add `pulsarcode` to your PATH

### 3. Start Coding

```bash
# Navigate to your project
cd ~/my-project

# Launch pulsarcode (same UX as Claude Code)
pulsarcode

# Or with a direct prompt
pulsarcode -p "explain this function"
```

---

## NVIDIA NIM Setup

PulsarCode uses NVIDIA NIM to provide free access to Kimi K2.6. You need a free NVIDIA account.

### First-Time Setup

Run the setup wizard:

```bash
pulsarcode /api
```

This will:
1. Open the NVIDIA Kimi K2.6 model page in your browser
2. Guide you to sign in or create a free NVIDIA account
3. Generate your API key (starts with `nvapi-`)
4. Securely store it locally

### Already Have a Key?

The installer will detect and offer to reuse your existing key. Or run:

```bash
pulsarcode /api
```

### Verify Your Setup

```bash
# Check stack status
pulsarcode status

# List available models
pulsarcode sonar
```

---

## Commands

| Command | Description |
|---------|-------------|
| `pulsarcode` | Launch with default NVIDIA NIM Kimi K2.6 |
| `pulsarcode -p "prompt"` | Single prompt mode |
| `pulsarcode /api` | Configure NVIDIA NIM API key |
| `pulsarcode sonar` | List available NVIDIA NIM models |
| `pulsarcode status` | Check stack health |
| `pulsarcode help` | Show help |

---

## Architecture

```
Your Terminal
      |
      | pulsarcode
      v
[NVIDIA NIM Adapter]  <- Local, on your machine
      |
      | HTTPS
      v
[NVIDIA NIM Cloud]    <- Kimi K2.6 (free tier)
```

- **Local adapter**: Lightweight proxy on `127.0.0.1:4000`
- **No data persistence**: Your code stays local
- **API key security**: Stored in `~/.pulsarcode/nim.key` (chmod 600)

---

## System Requirements

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| OS | macOS 14+ / Ubuntu 22.04+ | macOS 15+ / Ubuntu 24.04+ |
| Python | 3.12 | 3.14 |
| RAM | 4 GB | 8 GB+ |
| Disk | 500 MB | 1 GB |
| Network | Broadband | Broadband |

---

## Team Configuration

### Shared Team Defaults

Create `~/.pulsarcode/team_defaults.env`:

```bash
# Default model for team
PULSAR_NIM_MODEL=moonshotai/kimi-k2.6

# Timeout settings
PULSAR_NIM_STREAM_HEADER_TIMEOUT=180

# Optional: Custom API base (for enterprise NVIDIA NIM)
NVIDIA_NIM_API_BASE=https://integrate.api.nvidia.com/v1
```

### Per-Project Configuration

Create `.pulsarcode.env` in your project root:

```bash
# Project-specific model
PULSAR_NIM_MODEL=qwen/qwen3-coder-480b-a35b-instruct
```

---

## Troubleshooting

### "No NVIDIA NIM key found"

Run `pulsarcode /api` and follow the setup wizard.

### "Rate limited (429)"

NVIDIA NIM free tier has rate limits. Wait a moment and retry. For higher limits, consider NVIDIA NIM paid plans.

### "Model not found"

Run `pulsarcode sonar` to see available models. Update your key if needed:

```bash
pulsarcode /api
```

### Connection Issues

Check your internet connection and firewall settings. The adapter needs outbound HTTPS to `integrate.api.nvidia.com`.

---

## License

AGPL-3.0-or-later. See LICENSE.

Copyright (C) 2026 PulsarOS Intelligence Inc. / Collapse Technologies Inc.

---

## Support

- **Issues**: Open a ticket in this private repo
- **Email**: yassine@pulsaros.ca
- **Docs**: See docs/ directory

---

**PulsarOS Intelligence Inc.** - Sovereign Canadian AI Infrastructure
