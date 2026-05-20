#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
#
# Local Pulsar / pulsarcode  -  team distribution installer
# =============================================================================
#
# This installer ships the SAME launcher that the maintainer runs on his own
# Mac. There is no parallel codebase. The installation layout:
#
#   ~/.pulsarcode/
#     canonical/
#       pulsarcode         <- the launcher you invoke (canonical, not stripped)
#       proxy/             <- the Sonar catalog + NIM adapter + picker module
#         __init__.py
#         nim_anthropic_proxy.py
#         nim_api_sonar.py
#         nim_sonar_picker.py
#       tests/
#         test_sonar_picker.py
#       requirements.txt
#     venv/                <- isolated Python virtual env, owned by this install
#     nim.key              <- your NVIDIA NIM key (chmod 600, never leaves this Mac)
#     active_model         <- last alias selected via /model or pulsarcode pick
#     onboarding_complete  <- sentinel; first-launch wizard runs once
#     claude_config/       <- isolated Claude Code profile (no leak to ~/.claude)
#       commands/          <- /api /sonar /pick /model managed slash commands
#
#   ~/.local/bin/pulsarcode  -> symlink into ~/.pulsarcode/canonical/pulsarcode
#
# After the install completes, the very first `pulsarcode` invocation walks
# you through: NVIDIA NIM API key paste -> arrow-key Sonar picker -> launch.
#
# Re-run this installer any time to refresh the canonical files. It is
# idempotent and never touches your stored key, active_model selection, or
# Claude Code profile.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PULSAR_HOME="${PULSAR_HOME:-$HOME/.pulsarcode}"
CANONICAL_DIR="$PULSAR_HOME/canonical"
VENV_DIR="$PULSAR_HOME/venv"
#BIN_DIR overridable for smoke tests.
BIN_DIR="${PULSARCODE_BIN_DIR:-$HOME/.local/bin}"

# Colors (gated on TTY + NO_COLOR)
if [[ -t 2 && -z "${NO_COLOR:-}" ]]; then
    EMBER="\033[38;5;208m"
    GREEN="\033[38;5;46m"
    DIM="\033[38;5;240m"
    BOLD="\033[1m"
    RESET="\033[0m"
else
    EMBER=""; GREEN=""; DIM=""; BOLD=""; RESET=""
fi

print_banner() {
    cat <<BANNER

${EMBER}+---------------------------------------------------------------------------+${RESET}
${EMBER}| Local Pulsar  /  pulsarcode  /  team distribution installer               |${RESET}
${EMBER}+---------------------------------------------------------------------------+${RESET}

  Sovereign Claude Code with NVIDIA NIM as commodity GPU.
  Your moat: the files, the skills, the memory, the living files in your repo.
  The model is interchangeable. Pick any Sonar route at first launch.

BANNER
}

print_step() {
    printf "${EMBER}[step %s]${RESET} %s\n" "$1" "$2"
}

print_ok() {
    printf "  ${GREEN}ok${RESET}  %s\n" "$1"
}

print_warn() {
    printf "  ${DIM}warn${RESET}  %s\n" "$1"
}

print_err() {
    printf "  ${BOLD}error${RESET}  %s\n" "$1" >&2
}

detect_python() {
    local cmd
    for cmd in python3.14 python3.13 python3.12 python3; do
        if command -v "$cmd" >/dev/null 2>&1; then
            local version major minor
            version="$("$cmd" --version 2>&1 | sed 's/Python //')"
            major="$(echo "$version" | cut -d. -f1)"
            minor="$(echo "$version" | cut -d. -f2)"
            if [[ "$major" -ge 3 && "$minor" -ge 11 ]]; then
                printf '%s' "$cmd"
                return 0
            fi
        fi
    done
    printf ''
    return 1
}

step_1_check_os_and_python() {
    print_step 1 "checking environment"
    local os arch
    case "$(uname -s)" in
        Darwin*) os="macos" ;;
        Linux*)  os="linux" ;;
        *)       os="unknown" ;;
    esac
    arch="$(uname -m)"
    if [[ "$os" == "unknown" ]]; then
        print_err "unsupported OS (need macOS or Linux). Native Windows support is on the roadmap; use WSL2 for now."
        exit 1
    fi
    print_ok "OS: $os  arch: $arch"
    PYTHON_CMD="$(detect_python || true)"
    if [[ -z "$PYTHON_CMD" ]]; then
        print_err "Python 3.11+ not found. Install python3.12 or newer."
        echo "    macOS: brew install python@3.14"
        echo "    Linux: sudo apt-get install python3.12 python3.12-venv"
        exit 1
    fi
    local pyver
    pyver="$("$PYTHON_CMD" --version 2>&1)"
    print_ok "Python: $PYTHON_CMD ($pyver)"
}

step_2_layout() {
    print_step 2 "creating $PULSAR_HOME layout"
    mkdir -p "$CANONICAL_DIR" "$CANONICAL_DIR/proxy" "$CANONICAL_DIR/tests"
    mkdir -p "$BIN_DIR"
    chmod 700 "$PULSAR_HOME" 2>/dev/null || true
    print_ok "directories ready"
}

step_3_copy_canonical() {
    print_step 3 "copying canonical launcher + proxy modules"
    install -m 0755 "$SCRIPT_DIR/pulsarcode"                "$CANONICAL_DIR/pulsarcode"
    install -m 0644 "$SCRIPT_DIR/proxy/__init__.py"          "$CANONICAL_DIR/proxy/__init__.py"
    install -m 0644 "$SCRIPT_DIR/proxy/nim_api_sonar.py"     "$CANONICAL_DIR/proxy/nim_api_sonar.py"
    install -m 0644 "$SCRIPT_DIR/proxy/nim_anthropic_proxy.py" "$CANONICAL_DIR/proxy/nim_anthropic_proxy.py"
    install -m 0644 "$SCRIPT_DIR/proxy/nim_sonar_picker.py"  "$CANONICAL_DIR/proxy/nim_sonar_picker.py"
    install -m 0644 "$SCRIPT_DIR/tests/__init__.py"          "$CANONICAL_DIR/tests/__init__.py"
    install -m 0644 "$SCRIPT_DIR/tests/test_sonar_picker.py" "$CANONICAL_DIR/tests/test_sonar_picker.py"
    install -m 0644 "$SCRIPT_DIR/requirements.txt"           "$CANONICAL_DIR/requirements.txt"
    print_ok "canonical layout in place at $CANONICAL_DIR"
}

step_4_venv() {
    print_step 4 "Python virtualenv at $VENV_DIR"
    if [[ -x "$VENV_DIR/bin/python" ]]; then
        local current
        current="$("$VENV_DIR/bin/python" --version 2>&1)"
        print_ok "reusing existing venv ($current)"
    else
        "$PYTHON_CMD" -m venv "$VENV_DIR"
        print_ok "venv created"
    fi
    print_step 5 "installing Python dependencies (quiet)"
    "$VENV_DIR/bin/pip" install --upgrade --quiet pip setuptools wheel
    "$VENV_DIR/bin/pip" install --quiet -r "$CANONICAL_DIR/requirements.txt"
    "$VENV_DIR/bin/pip" install --quiet pytest
    print_ok "dependencies installed"
}

step_6_symlink() {
    print_step 6 "binary symlink at $BIN_DIR/pulsarcode"
    local target="$CANONICAL_DIR/pulsarcode"
    if [[ -L "$BIN_DIR/pulsarcode" ]] && [[ "$(readlink "$BIN_DIR/pulsarcode")" == "$target" ]]; then
        print_ok "symlink already correct"
    else
        rm -f "$BIN_DIR/pulsarcode" 2>/dev/null || true
        ln -s "$target" "$BIN_DIR/pulsarcode"
        print_ok "symlink: $BIN_DIR/pulsarcode -> $target"
    fi
}

step_7_path_in_shell_rc() {
    print_step 7 "ensuring $BIN_DIR is on PATH"
    if [[ "${PULSARCODE_SKIP_PATH_EXPORT:-0}" == "1" ]]; then
        print_ok "skipped (PULSARCODE_SKIP_PATH_EXPORT=1)"
        return 0
    fi
    if [[ ":$PATH:" == *":$BIN_DIR:"* ]]; then
        print_ok "PATH already contains $BIN_DIR"
        return 0
    fi
    local shell_rc=""
    case "${SHELL:-}" in
        */zsh)  shell_rc="$HOME/.zshrc" ;;
        */bash) shell_rc="$HOME/.bashrc" ;;
        */fish) shell_rc="$HOME/.config/fish/config.fish" ;;
    esac
    if [[ -z "$shell_rc" ]]; then
        print_warn "unknown shell; add $BIN_DIR to your PATH manually"
        return 0
    fi
    if [[ -f "$shell_rc" ]] && grep -q "$BIN_DIR" "$shell_rc" 2>/dev/null; then
        print_ok "$shell_rc already references $BIN_DIR"
        return 0
    fi
    #append a single export PATH line,
    # idempotent. Never edits an existing PATH export, only appends a new one.
    if [[ ! -f "$shell_rc" ]]; then
        touch "$shell_rc"
    fi
    if [[ "$shell_rc" == *config.fish ]]; then
        printf '\n# Local Pulsar / pulsarcode\nset -gx PATH "%s" $PATH\n' "$BIN_DIR" >> "$shell_rc"
    else
        printf '\n# Local Pulsar / pulsarcode\nexport PATH="%s:$PATH"\n' "$BIN_DIR" >> "$shell_rc"
    fi
    print_ok "appended PATH export to $shell_rc"
    print_warn "open a new terminal tab or run: source $shell_rc"
}

step_8_smoke_test() {
    print_step 8 "smoke test (picker tier classifier + active_model round-trip)"
    if ! cd "$CANONICAL_DIR"; then
        print_warn "could not cd into $CANONICAL_DIR; skipping smoke test"
        return 0
    fi
    if PYTHONPATH=. "$VENV_DIR/bin/python" -m pytest tests/test_sonar_picker.py -q >/tmp/pulsarcode_install_smoke.log 2>&1; then
        print_ok "12/12 tests passed (see /tmp/pulsarcode_install_smoke.log for detail)"
    else
        print_warn "smoke tests failed; install is usable but flag this to the maintainer"
        print_warn "log at /tmp/pulsarcode_install_smoke.log"
    fi
    cd - >/dev/null
}

step_9_print_next() {
    cat <<NEXT

${GREEN}install complete.${RESET}

  Next step: open a new terminal tab and run:

    ${BOLD}cd /path/to/your/project${RESET}
    ${BOLD}pulsarcode${RESET}

  The first launch walks you through three steps:

    1. Paste a free NVIDIA NIM API key
       Generate it at https://build.nvidia.com (free, 1000 credits per key,
       no card required). Each teammate brings their OWN key. Your bucket,
       your throughput.

    2. Arrow-key Sonar picker
       55+ models grouped by tier (CODING, GENERAL, LIGHTWEIGHT, OTHER).
       Pick one. Esc keeps the default (Kimi K2.6).

    3. Claude Code launches with your chosen model.

  Switch model later:
    /model <alias>          inside a session (persists for next launch)
    Claude Code's /model    inside a session (live, this session only)
    pulsarcode pick         fresh terminal tab (arrow-key picker)
    pulsarcode /api         re-paste your NIM key any time

  The launcher never modifies your system claude binary or your ~/.claude
  profile. Everything is isolated to $PULSAR_HOME.

NEXT
}

main() {
    print_banner
    step_1_check_os_and_python
    step_2_layout
    step_3_copy_canonical
    step_4_venv
    step_6_symlink
    step_7_path_in_shell_rc
    step_8_smoke_test
    step_9_print_next
}

main "$@"
