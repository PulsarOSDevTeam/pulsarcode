#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
#
# pulsarcode  -  installer
# =============================================================================
#
# Installs pulsarcode under ~/.pulsarcode/ with an isolated Python venv and
# a symlink at ~/.local/bin/pulsarcode. Idempotent: re-run any time to
# refresh the canonical files; never touches your stored key, current model
# selection, model history, or Claude Code profile.
#
# Layout produced by this installer:
#
#   ~/.pulsarcode/
#     canonical/
#       pulsarcode             the launcher you invoke
#       proxy/                 API Sonar + NIM adapter + arrow-key picker
#         __init__.py
#         nim_anthropic_proxy.py
#         nim_api_sonar.py
#         nim_sonar_picker.py
#       tests/
#         test_sonar_picker.py
#       requirements.txt
#     venv/                    isolated Python virtual env
#     nim.key                  your NVIDIA NIM key (chmod 600)
#     active_model             last alias selected
#     model_history            recently-used aliases, newest first, cap 20
#     onboarding_complete      sentinel; welcome banner shows on first launch only
#     claude_config/           isolated Claude Code profile
#       commands/              /api /sonar /pick /model managed slash commands
#
#   ~/.local/bin/pulsarcode  -> symlink into ~/.pulsarcode/canonical/pulsarcode
#
# After this installer finishes, run `pulsarcode` in any project directory.
# The every-launch wizard walks you through API key check and model pick.

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
${EMBER}| pulsarcode installer                                                      |${RESET}
${EMBER}+---------------------------------------------------------------------------+${RESET}

  Sovereign Claude Code launcher with NVIDIA NIM as the model backend.
  Bring your own free NVIDIA key. Pick any of 55+ frontier coding models.
  AGPL-3.0, no telemetry, no phone-home.

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

# v1.0.6: pre-flight the Claude Code dependency BEFORE building the venv
# so the user is never told "Claude Code missing" after a successful install.
# If the binary is absent and npm is available, auto-install Anthropic's
# official package. If neither is available, print clear instructions and
# halt with a non-zero exit code so the failure is loud.
step_1b_check_claude_code() {
    print_step "1b" "checking for Claude Code CLI (Anthropic's official binary)"

    if command -v claude >/dev/null 2>&1; then
        local v
        v="$(claude --version 2>&1 | head -1 | tr -d '\r\n')"
        if [[ -n "$v" ]]; then
            print_ok "Claude Code present ($v)"
        else
            print_ok "Claude Code present"
        fi
        return 0
    fi

    print_warn "Claude Code CLI not found on PATH"
    print_warn "pulsarcode is a wrapper; the official Anthropic CLI is required upstream"

    if command -v npm >/dev/null 2>&1; then
        echo "         attempting auto-install via npm ..."
        local npm_log
        npm_log="$(mktemp -t pulsarcode_npm.XXXXXX)"
        if npm install -g @anthropic-ai/claude-code >"$npm_log" 2>&1; then
            if command -v claude >/dev/null 2>&1; then
                local v2
                v2="$(claude --version 2>&1 | head -1 | tr -d '\r\n')"
                print_ok "Claude Code installed via npm${v2:+ ($v2)}"
                rm -f "$npm_log"
                return 0
            fi
            print_warn "npm install completed but 'claude' is still not on PATH"
            print_warn "check your npm global prefix: 'npm config get prefix'"
            print_warn "details: $npm_log"
        else
            print_warn "npm install -g @anthropic-ai/claude-code failed"
            print_warn "log saved at $npm_log"
            print_warn "common cause: global install needs sudo, OR npm prefix is misconfigured"
        fi
    else
        print_warn "npm not found; cannot auto-install Claude Code"
    fi

    print_err "Claude Code is required for pulsarcode to function."
    echo "    Quickest manual install (any platform with Node.js):"
    echo "        npm install -g @anthropic-ai/claude-code"
    echo "    If you do not have Node.js, install it first:"
    echo "        https://nodejs.org  or your distro's package manager."
    echo "    Full Claude Code install docs:"
    echo "        https://claude.com/claude-code"
    echo "    Then re-run this installer."
    exit 1
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
        local passed
        passed="$(grep -Eo '[0-9]+ passed' /tmp/pulsarcode_install_smoke.log | head -1)"
        if [[ -n "$passed" ]]; then
            print_ok "$passed (see /tmp/pulsarcode_install_smoke.log for detail)"
        else
            print_ok "all smoke tests passed (see /tmp/pulsarcode_install_smoke.log for detail)"
        fi
    else
        print_warn "smoke tests failed; install is usable but please file a bug report"
        print_warn "log at /tmp/pulsarcode_install_smoke.log"
    fi
    cd - >/dev/null
}

step_9_print_next() {
    cat <<NEXT

${GREEN}install complete.${RESET}

  Run pulsarcode from any project directory:

    ${BOLD}cd /path/to/your/project${RESET}
    ${BOLD}pulsarcode${RESET}

  Every launch walks through two quick steps:

    1. NVIDIA NIM API key
       First time only, paste a free key from https://build.nvidia.com
       (no card, 1000 credits per account). On subsequent launches you
       get a one-keystroke ${BOLD}Replace stored NIM API key now? [y/N]${RESET}
       (default keeps your key).

    2. Model picker
       Live catalog of 55+ frontier coding routes grouped into
       RECENTLY USED, CODING, GENERAL, LIGHTWEIGHT, OTHER. Arrow
       keys move, Enter selects, Esc keeps your current pick and
       launches. Your previous selections appear on top.

  Switch model any time:
    ${BOLD}/model <alias>${RESET}          inside a session (persists for next launch)
    ${BOLD}Claude Code's /model${RESET}    inside a session (live, this session only)
    ${BOLD}pulsarcode pick${RESET}         fresh terminal tab (arrow-key picker)
    ${BOLD}pulsarcode /api${RESET}         re-paste your NIM key any time

  Want a fast relaunch with no prompts? ${BOLD}PULSAR_SKIP_WIZARD=1 pulsarcode${RESET}.

  The launcher never modifies your system claude binary or your ~/.claude
  profile. Everything is isolated to $PULSAR_HOME.

NEXT
}

main() {
    print_banner
    step_1_check_os_and_python
    step_1b_check_claude_code
    step_2_layout
    step_3_copy_canonical
    step_4_venv
    step_6_symlink
    step_7_path_in_shell_rc
    step_8_smoke_test
    step_9_print_next
}

main "$@"
