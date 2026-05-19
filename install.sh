#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc. / Collapse Technologies Inc.
#
# install.sh - One-command installer for pulsarcode team distribution
# ===================================================================
# Usage: bash install.sh
#
# This script:
#   1. Detects OS (macOS/Linux)
#   2. Checks Python 3.12+ availability
#   3. Creates isolated venv
#   4. Installs dependencies
#   5. Configures NVIDIA NIM API key
#   6. Sets up pulsarcode command
#   7. Verifies installation

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PULSAR_HOME="${PULSAR_HOME:-$HOME/.pulsarcode}"
VENV_DIR="$PULSAR_HOME/venv"
BIN_DIR="$PULSAR_HOME/bin"
CONFIG_DIR="$PULSAR_HOME/config"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

print_banner() {
    echo ""
    echo "============================================================"
    echo "  PULSARCODE - Sovereign Claude Code for Your Team"
    echo "  PulsarOS Intelligence Inc. / Collapse Technologies Inc."
    echo "============================================================"
    echo ""
}

print_step() {
    echo -e "${BLUE}[STEP $1]${NC} $2"
}

print_success() {
    echo -e "${GREEN}[OK]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

detect_os() {
    case "$(uname -s)" in
        Darwin*) echo "macos" ;;
        Linux*) echo "linux" ;;
        *) echo "unknown" ;;
    esac
}

detect_arch() {
    case "$(uname -m)" in
        arm64|aarch64) echo "arm64" ;;
        x86_64) echo "x86_64" ;;
        *) echo "unknown" ;;
    esac
}

check_python() {
    local python_cmd=""
    for cmd in python3.14 python3.13 python3.12 python3; do
        if command -v "$cmd" &>/dev/null; then
            local version
            version="$($cmd --version 2>&1 | sed 's/Python //')"
            local major minor
            major="$(echo "$version" | cut -d. -f1)"
            minor="$(echo "$version" | cut -d. -f2)"
            if [[ "$major" -ge 3 && "$minor" -ge 12 ]]; then
                python_cmd="$cmd"
                break
            fi
        fi
    done
    echo "$python_cmd"
}

setup_nim_key() {
    print_step "5" "Configuring NVIDIA NIM API key"
    echo ""
    echo "  NVIDIA NIM provides access to frontier models including Kimi K2.6."
    echo "  You need a free NVIDIA account and API key."
    echo ""

    local existing_key=""
    if [[ -f "$PULSAR_HOME/nim.key" ]]; then
        existing_key="$(tr -d '\r\n' < "$PULSAR_HOME/nim.key" 2>/dev/null || true)"
    fi

    if [[ -n "$existing_key" ]]; then
        echo "  Existing key found."
        read -rp "  Use existing key? [Y/n] " confirm
        if [[ "$confirm" =~ ^[Nn]$ ]]; then
            existing_key=""
        else
            print_success "Using existing NVIDIA NIM key"
            return 0
        fi
    fi

    if [[ -z "$existing_key" ]]; then
        echo ""
        echo "  Setup steps:"
        echo "    1. Visit: https://build.nvidia.com/moonshotai/kimi-k2.6"
        echo "    2. Sign in (or create free NVIDIA account)"
        echo "    3. Click 'Get API Key' then 'Generate Key'"
        echo "    4. Copy the key (starts with 'nvapi-')"
        echo ""

        # Try to open browser on macOS
        if command -v open &>/dev/null; then
            read -rp "  Open NVIDIA page in browser? [Y/n] " open_browser
            if [[ ! "$open_browser" =~ ^[Nn]$ ]]; then
                open "https://build.nvidia.com/moonshotai/kimi-k2.6" &>/dev/null || true
            fi
        fi

        echo ""
        read -rsp "  Paste your NVIDIA NIM API key (hidden): " nim_key
        echo ""

        # Validate key format
        if [[ -z "$nim_key" ]]; then
            print_error "No key provided. You can configure later with: pulsarcode /api"
            return 1
        fi

        if [[ ! "$nim_key" =~ ^nvapi- ]]; then
            print_warning "Key does not start with 'nvapi-'. This may not be a valid NVIDIA NIM key."
            read -rp "  Continue anyway? [y/N] " continue_anyway
            if [[ ! "$continue_anyway" =~ ^[Yy]$ ]]; then
                return 1
            fi
        fi

        # Store key securely
        mkdir -p "$PULSAR_HOME"
        chmod 700 "$PULSAR_HOME"
        printf '%s\n' "$nim_key" > "$PULSAR_HOME/nim.key"
        chmod 600 "$PULSAR_HOME/nim.key"
        print_success "NVIDIA NIM key stored securely"
    fi
}

verify_installation() {
    print_step "6" "Verifying installation"
    echo ""

    local errors=0

    # Check pulsarcode binary
    if [[ -x "$BIN_DIR/pulsarcode" ]]; then
        print_success "pulsarcode command installed"
    else
        print_error "pulsarcode command not found"
        errors=$((errors + 1))
    fi

    # Check Python venv
    if [[ -x "$VENV_DIR/bin/python" ]]; then
        print_success "Python venv ready"
    else
        print_error "Python venv not found"
        errors=$((errors + 1))
    fi

    # Check key
    if [[ -f "$PULSAR_HOME/nim.key" ]]; then
        print_success "NVIDIA NIM key configured"
    else
        print_warning "NVIDIA NIM key not yet configured"
        print_warning "Run: pulsarcode /api"
    fi

    echo ""
    if [[ $errors -eq 0 ]]; then
        print_success "Installation complete!"
        echo ""
        echo "  Quick start:"
        echo "    cd <your-project-directory>"
        echo "    pulsarcode"
        echo ""
        echo "  Or with a direct prompt:"
        echo "    pulsarcode -p 'explain this code'"
        echo ""
        echo "  Need help?"
        echo "    pulsarcode help"
        echo "    pulsarcode /api     # Reconfigure NVIDIA NIM key"
        echo "    pulsarcode sonar    # List available models"
        echo ""
        return 0
    else
        print_error "Installation had $errors error(s). Check output above."
        return 1
    fi
}

main() {
    print_banner

    OS="$(detect_os)"
    ARCH="$(detect_arch)"

    print_step "1" "Detecting environment"
    echo "  OS: $OS"
    echo "  Arch: $ARCH"
    echo "  Install dir: $PULSAR_HOME"

    if [[ "$OS" == "unknown" ]]; then
        print_error "Unsupported OS. pulsarcode supports macOS and Linux."
        exit 1
    fi

    print_step "2" "Checking Python"
    PYTHON_CMD="$(check_python)"
    if [[ -z "$PYTHON_CMD" ]]; then
        print_error "Python 3.12+ not found. Please install Python 3.12 or later."
        echo "  macOS: brew install python@3.14"
        echo "  Linux: sudo apt install python3.12"
        exit 1
    fi
    print_success "Found $PYTHON_CMD"

    print_step "3" "Creating isolated environment"
    if [[ -d "$VENV_DIR" ]]; then
        print_warning "Existing venv found at $VENV_DIR"
        read -rp "  Reinstall? [y/N] " reinstall
        if [[ "$reinstall" =~ ^[Yy]$ ]]; then
            rm -rf "$VENV_DIR"
            "$PYTHON_CMD" -m venv "$VENV_DIR"
            print_success "Fresh venv created"
        else
            print_success "Using existing venv"
        fi
    else
        "$PYTHON_CMD" -m venv "$VENV_DIR"
        print_success "Venv created at $VENV_DIR"
    fi

    print_step "4" "Installing dependencies"
    echo "  This may take a few minutes..."
    "$VENV_DIR/bin/pip" install --upgrade pip setuptools wheel &>/dev/null || true

    # Install from requirements
    if [[ -f "$SCRIPT_DIR/requirements.txt" ]]; then
        "$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt" &>/dev/null || {
            print_error "Failed to install dependencies"
            exit 1
        }
    fi

    # Install pulsarcode package
    if [[ -f "$SCRIPT_DIR/setup.py" || -f "$SCRIPT_DIR/pyproject.toml" ]]; then
        "$VENV_DIR/bin/pip" install "$SCRIPT_DIR" &>/dev/null || {
            print_warning "Package install had issues, but core tools should work"
        }
    fi

    print_success "Dependencies installed"

    # Setup directories
    mkdir -p "$BIN_DIR" "$CONFIG_DIR"

    # Install pulsarcode command
    cp "$SCRIPT_DIR/pulsarcode" "$BIN_DIR/pulsarcode"
    chmod +x "$BIN_DIR/pulsarcode"

    # Add to PATH if not already there
    if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
        SHELL_RC=""
        case "$SHELL" in
            */zsh) SHELL_RC="$HOME/.zshrc" ;;
            */bash) SHELL_RC="$HOME/.bashrc" ;;
            */fish) SHELL_RC="$HOME/.config/fish/config.fish" ;;
        esac

        if [[ -n "$SHELL_RC" && -f "$SHELL_RC" ]]; then
            if ! grep -q "pulsarcode" "$SHELL_RC" 2>/dev/null; then
                echo "export PATH=\"$BIN_DIR:\$PATH\"" >> "$SHELL_RC"
                print_success "Added pulsarcode to PATH in $SHELL_RC"
                print_warning "Run 'source $SHELL_RC' or restart your terminal to use pulsarcode"
            fi
        fi
    fi

    # Setup NVIDIA NIM key
    setup_nim_key || true

    # Verify
    verify_installation
}

main "$@"
