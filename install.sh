#!/usr/bin/env bash
# pulsarcode-installer (this line lets the sh preamble below recognise the file)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
#
# Pulsar Code installer. One command, any terminal (macOS, Linux, WSL; *BSD experimental):
#
#   curl -fsSL https://raw.githubusercontent.com/PulsarOSDevTeam/pulsarcode/main/install.sh | bash
#
# Needs only bash (3.2+), tar and curl (or wget). Everything else is fetched:
#   * the Pulsar Code engine          -> ~/.pulsarcode/app
#   * a private Python runtime + deps -> ~/.pulsarcode/venv   (via uv when the
#                                        system Python is missing or too old)
#   * Claude Code, if absent          -> Anthropic's official native installer
#   * the `pulsarcode` command        -> ~/.local/bin/pulsarcode
# Then it launches straight into the key uplink. Paste any API key. Done.
#
# Re-run any time to update; keys, model choice and history are kept. A running Pulsar Core is
# stopped first, so the next launch starts the new engine. What an update replaces (the engine, the
# runtime, a v1 engine, the command) goes only once the new engine has passed its self-test; a run that
# stops before then puts it back.
# Knobs: PULSAR_REF=<tag|branch|commit> PULSAR_HOME=<dir> PULSAR_BIN_DIR=<dir>
#        PULSAR_NO_LAUNCH=1 PULSAR_SKIP_CLAUDE=1 PULSAR_NO_PATH=1 NO_COLOR=1
#        PULSAR_FORCE=1 (replace a `pulsarcode` command in PULSAR_BIN_DIR that another install made)
# A PULSAR_HOME that already holds files Pulsar Code did not create, or cannot tell are its own, is
# refused, and nothing is written there. The installed command always runs against the home it was
# installed into.
# Networks: uv uses the OS trust store (UV_NATIVE_TLS=1 unless you set it) and SSL_CERT_FILE is
# honoured by curl, wget, pip and uv, so TLS-inspecting corporate proxies work. PIP_INDEX_URL,
# PIP_EXTRA_INDEX_URL and PIP_FIND_LINKS are passed on to uv (which ignores pip's settings).
#
# Which source gets installed:
#   PULSAR_REF=v3.1.0 (a release tag): the release's own archive, checked against the release's
#       SHA256SUMS before anything is unpacked. A mismatch, a missing entry or a SHA256SUMS that
#       cannot be fetched stops the install (fail closed). Only a tag older than v3, whose release
#       publishes no SHA256SUMS at all, falls back to GitHub's tag archive, with a warning.
#   PULSAR_REF=main (the default, kept so the one-liner above always gets current fixes) or any
#       branch or commit: GitHub's archive over HTTPS, without a checksum (a branch has none).
#       `pulsarcode update` installs the latest release tag, so updates are verified.
#   --local / PULSAR_SRC=<dir>: a checkout or an unpacked release archive you already have.
#
# Install a checkout or an unpacked release archive instead of downloading:
#   bash /path/to/pulsarcode/install.sh --local        (or PULSAR_SRC=/path/to/pulsarcode)
# Only on request, and only from a folder you own: the folder this script happens to sit in (the
# shared temp dir during `pulsarcode update`, or the current folder under `curl | bash`) is never
# taken as the source. PULSAR_NO_LOCAL_SRC=1 (set by `pulsarcode update`) forbids local sources.
#
# Pin a release for a reproducible install:  PULSAR_REF=vX.Y.Z (fetch this script from the same tag;
# release tags come with the first public release).
# Every Python this script runs uses -I or the bootstrap below, so a folder you run it from can never
# shadow `pulsar`, `venv`, `pip` or the standard library.

# ---------------------------------------------------------------- not bash? (POSIX sh from here
# to the next marker: dash, busybox ash and zsh read this far before anything bash-only runs)
if [ -z "${BASH_VERSION:-}" ]; then
    if command -v bash >/dev/null 2>&1; then
        if [ -f "$0" ] && grep -q '^# pulsarcode-installer' "$0" 2>/dev/null; then
            exec bash "$0" "$@"
        fi
        echo "Pulsar Code: this installer needs bash. Pipe it to bash instead of sh:" >&2
    else
        echo "Pulsar Code: this installer needs bash, and bash is not installed." >&2
        if command -v apk >/dev/null 2>&1; then
            echo "  Alpine:   apk add bash curl        (as root, or with sudo / doas)" >&2
        elif command -v pkg >/dev/null 2>&1; then
            echo "  FreeBSD:  pkg install bash" >&2
        elif command -v pkg_add >/dev/null 2>&1; then
            echo "  OpenBSD / NetBSD:  pkg_add bash" >&2
        elif command -v apt-get >/dev/null 2>&1; then
            echo "  Debian / Ubuntu:   apt-get install bash" >&2
        elif command -v dnf >/dev/null 2>&1; then
            echo "  Fedora / RHEL:     dnf install bash" >&2
        else
            echo "  Install bash with your package manager." >&2
        fi
        echo "Then run:" >&2
    fi
    echo "  curl -fsSL https://raw.githubusercontent.com/PulsarOSDevTeam/pulsarcode/main/install.sh | bash" >&2
    exit 1
fi
# ---------------------------------------------------------------- bash from here on

set +o posix 2>/dev/null || true   # macOS /bin/sh is bash 3.2 in POSIX mode
set -euo pipefail
unset CDPATH                        # `cd dir` must never print or jump elsewhere

# Programs come from absolute PATH entries only: an empty entry, "." or a relative one names the folder
# this script runs from, where a cloned repository could keep its own curl, tar, python3 or uv. The PATH
# you gave comes back for the launch at the end.
ORIG_PATH="${PATH:-/usr/bin:/bin}"
_path="" _rest="${PATH:-}:"
while [[ -n "$_rest" ]]; do
    _d="${_rest%%:*}"; _rest="${_rest#*:}"
    if [[ "$_d" == /* ]]; then _path="${_path:+$_path:}$_d"; fi
done
PATH="${_path:-/usr/bin:/bin}"; export PATH
unset _path _rest _d
# The folder this script was started from: relative paths are resolved against it, and the launch at the
# end runs there. The install itself works from inside PULSAR_HOME (enter_home).
START_DIR="$(pwd -P 2>/dev/null)" || START_DIR="${PWD:-}"

REPO="PulsarOSDevTeam/pulsarcode"
REF="${PULSAR_REF:-main}"
WANT_LOCAL=0
# uv is pinned to one release and its installer is checked against this digest before it runs.
UV_VERSION="0.12.22"
UV_INSTALLER_SHA256="58488ae8dbd0773134c92c85e901430e33f99d975bd7f929d26aa9ab0c2f9390"
UV_MIN_MINOR=5                      # an older uv on PATH lacks flags used below: use a private one
# Same as pulsar.BOOT: drop the current directory from sys.path and pin the app dir before any import.
BOOT="import sys;r=sys.argv.pop(1);m=sys.argv.pop(1);sys.path[:]=[r]+[p for p in sys.path if p not in ('','.',r)];import runpy;sys.argv[0]=m;runpy.run_module(m,run_name='__main__',alter_sys=True)"
PY_MIN_MINOR=9

need() { command -v "$1" >/dev/null 2>&1; }

abs_path() { # path -> absolute: "~" and "~/..." in your home folder, anything else against START_DIR
    case "$1" in
        /*) printf '%s' "$1" ;;
        "~") printf '%s' "$HOME" ;;
        "~/"*) printf '%s/%s' "$HOME" "${1#"~/"}" ;;
        *) if [[ -n "$START_DIR" ]]; then printf '%s/%s' "$START_DIR" "$1"; else printf '%s' "$1"; fi ;;
    esac
}

# HOME can be unset (systemd units, some CI runners, `env -i`): find it in the user database.
if [[ -z "${HOME:-}" ]]; then
    _h=""
    if need getent; then _h="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)" || _h=""; fi
    if [[ -z "$_h" ]]; then
        _u="$(id -un 2>/dev/null)" || _u=""
        _re='^[A-Za-z0-9._][A-Za-z0-9._-]*$'
        if [[ "$_u" =~ $_re ]]; then eval "_h=~$_u"; [[ "$_h" == "~"* ]] && _h=""; fi
    fi
    if [[ -z "$_h" || ! -d "$_h" ]]; then
        echo "Pulsar Code: HOME is not set and your home folder could not be found. Run: HOME=/path/to/home bash install.sh" >&2
        exit 1
    fi
    HOME="$_h"; export HOME
    unset _h _u _re
fi

PULSAR_HOME="$(abs_path "${PULSAR_HOME:-$HOME/.pulsarcode}")"
BIN_DIR="$(abs_path "${PULSAR_BIN_DIR:-${PULSARCODE_BIN_DIR:-$HOME/.local/bin}}")"
APP="$PULSAR_HOME/app"
VENV="$PULSAR_HOME/venv"
TMPBASE="${TMPDIR:-/tmp}"
# A stale TMPDIR must not stop the install, nor a relative one follow it into PULSAR_HOME.
[[ "$TMPBASE" == /* && -d "$TMPBASE" && -w "$TMPBASE" ]] || TMPBASE=/tmp
LOG="$(mktemp "$TMPBASE/pulsarcode-install.XXXXXX")"
TMP_SRC=""
RUN_PID=""
NAP_PID=""
# An install run (main) only sets aside what it replaces until the new engine has verified itself
# (commit_install); when the run stops before that, cleanup puts it back (roll_back). Functions used one
# at a time, as the tests do, are no install run.
ROLL_BACK=0                         # 1 inside main()
COMMITTED=0                         # 1 once commit_install ran
PLACED=0                            # 1 once the new engine is in app/ (app.old holds the one it replaced)
VENV_ASIDE=0                        # 1 while the previous runtime waits in venv.old
VENV_BUSY=0                         # 1 while a step writes into the runtime
LINK_PREV=""                        # where BIN_DIR/pulsarcode pointed before link_bin, if somewhere else
OS=""
UV=""
WSL_DRIVES="/mnt"                   # where WSL mounts the Windows drives (its default automount root)

# TLS-inspecting proxies: uv's bundled roots do not know the corporate CA, the OS store does.
export UV_NATIVE_TLS="${UV_NATIVE_TLS:-1}"
# uv ignores pip's configuration; carry a corporate index or wheel folder over.
if [[ -n "${PIP_INDEX_URL:-}" && -z "${UV_DEFAULT_INDEX:-}${UV_INDEX_URL:-}" ]]; then export UV_DEFAULT_INDEX="$PIP_INDEX_URL"; fi
if [[ -n "${PIP_EXTRA_INDEX_URL:-}" && -z "${UV_INDEX:-}${UV_EXTRA_INDEX_URL:-}" ]]; then export UV_INDEX="$PIP_EXTRA_INDEX_URL"; fi
if [[ -n "${PIP_FIND_LINKS:-}" && -z "${UV_FIND_LINKS:-}" ]]; then export UV_FIND_LINKS="$PIP_FIND_LINKS"; fi
CA_FILE=""
if [[ -n "${SSL_CERT_FILE:-}" && -f "${SSL_CERT_FILE:-}" ]]; then
    CA_FILE="$(abs_path "$SSL_CERT_FILE")"
    SSL_CERT_FILE="$CA_FILE"        # uv and pip read it too, from inside PULSAR_HOME
fi

# ---------------------------------------------------------------- look & feel

if [[ -t 1 && -z "${NO_COLOR:-}" && "${TERM:-}" != "dumb" ]]; then
    FANCY=1
    case "${COLORTERM:-}" in truecolor|24bit) TRUE=1 ;; *) TRUE=0 ;; esac
    [[ "${TERM_PROGRAM:-}" == "iTerm.app" || "${TERM_PROGRAM:-}" == "WezTerm" || "${TERM_PROGRAM:-}" == "vscode" ]] && TRUE=1
    R=$'\033[0m'; B=$'\033[1m'
else
    FANCY=0; TRUE=0; R=""; B=""
fi

# The locale decides the glyphs: under LANG=C a terminal may not be UTF-8, and bash would cut
# multi-byte characters apart. LC_ALL overrides LC_CTYPE, which overrides LANG.
case "${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" in
    *[Uu][Tt][Ff]-8*|*[Uu][Tt][Ff]8*) UTF8=1 ;;
    *) UTF8=0 ;;
esac
if [[ $UTF8 -eq 1 ]]; then
    G_OK="✓"; G_BAD="✗"; G_DOT="·"; G_MARK="◉"
    SPIN=("⠋" "⠙" "⠹" "⠸" "⠼" "⠴" "⠦" "⠧" "⠇" "⠏")
else
    G_OK="+"; G_BAD="x"; G_DOT="-"; G_MARK="*"
    SPIN=("|" "/" "-" "\\")
fi

cleanup() {
    # bash ends the script with the status this trap returns, and enters it with 0 after some shell errors
    # (an unset variable): an install run that ends before commit_install has failed, whatever it says.
    local rc=$? stopped=0
    if [[ $rc -eq 0 && $ROLL_BACK -eq 1 && $COMMITTED -eq 0 ]]; then rc=1; fi
    # A step still running in the background is ended through the handle `run` kept for it, never
    # by name or by parent: nothing else on the machine can be reached from here.
    if [[ -n "$RUN_PID" ]]; then
        kill "$RUN_PID" 2>/dev/null || true
        RUN_PID=""
        stopped=1
    fi
    if [[ -n "$NAP_PID" ]]; then kill "$NAP_PID" 2>/dev/null || true; NAP_PID=""; fi
    if [[ $FANCY -eq 1 ]]; then printf '\033[?25h\033[0m'; fi   # never leave the cursor hidden
    roll_back "$stopped"
    if [[ -n "$TMP_SRC" ]]; then rm -rf "$TMP_SRC"; fi
    # The downloaded source never outlives the installer; the log is kept only when something failed.
    if [[ ! -s "$LOG" ]]; then rm -f "$LOG"; fi
    return "$rc"
}
trap cleanup EXIT
# Each handler first drops itself: a second signal during cleanup (or bash re-raising the one it
# just handled) then takes the default path instead of re-entering the trap.
trap 'trap - INT; printf "\n" >&2; exit 130' INT
trap 'trap - TERM; exit 143' TERM
trap 'trap - HUP; exit 129' HUP

# nap seconds : sleep without a foreground child. A foreground `sleep` killed by Ctrl+C sends bash
# through its wait-for-a-SIGINTed-child path, which under load was seen spinning in a
# kill(self, SIGINT) loop with the INT trap never running. Waiting on a background sleep with the
# `wait` builtin returns at once on a trapped signal and the trap runs normally.
nap() {
    sleep "$1" &
    NAP_PID=$!
    wait "$NAP_PID" 2>/dev/null || true
    NAP_PID=""
}

rgb() { # r g b -> fg escape
    if [[ $FANCY -eq 0 ]]; then return; fi
    if [[ $TRUE -eq 1 ]]; then printf '\033[38;2;%d;%d;%dm' "$1" "$2" "$3"
    else printf '\033[38;5;%dm' "$(( 16 + 36*($1*5/255) + 6*($2*5/255) + ($3*5/255) ))"; fi
}
EMBER="$(rgb 255 106 0)"; FLARE="$(rgb 255 46 136)"; PLASMA="$(rgb 157 77 255)"
BEAM="$(rgb 54 226 255)"; ASH="$(rgb 150 146 172)"; DUST="$(rgb 88 84 108)"; OKC="$(rgb 80 250 170)"
BADC="$(rgb 255 70 90)"; ICE="$(rgb 236 246 255)"

# Spectrum stops: ember -> flare -> plasma -> beam
STOPS=(255 106 0  255 46 136  157 77 255  54 226 255)
grad_line() { # text width -> gradient-painted line
    local text="$1" w="$2" i ch n=${#1} seg t r g b i0
    if [[ $FANCY -eq 0 ]]; then printf '%s\n' "$text"; return; fi
    local out=""
    for (( i=0; i<n; i++ )); do
        ch="${text:i:1}"
        if [[ "$ch" == " " ]]; then out+=" "; continue; fi
        t=$(( i * 300 / (w > 1 ? w - 1 : 1) ))          # 0..300 across 3 segments
        seg=$(( t / 100 )); (( seg > 2 )) && seg=2
        local f=$(( t - seg * 100 ))
        i0=$(( seg * 3 ))
        r=$(( STOPS[i0]   + (STOPS[i0+3] - STOPS[i0])   * f / 100 ))
        g=$(( STOPS[i0+1] + (STOPS[i0+4] - STOPS[i0+1]) * f / 100 ))
        b=$(( STOPS[i0+2] + (STOPS[i0+5] - STOPS[i0+2]) * f / 100 ))
        out+="$(rgb $r $g $b)${B}${ch}"
    done
    printf '%s%s\n' "$out" "$R"
}

banner() {
    local L=(
        "██████╗ ██╗   ██╗██╗     ███████╗ █████╗ ██████╗ "
        "██╔══██╗██║   ██║██║     ██╔════╝██╔══██╗██╔══██╗"
        "██████╔╝██║   ██║██║     ███████╗███████║██████╔╝"
        "██╔═══╝ ██║   ██║██║     ╚════██║██╔══██║██╔══██╗"
        "██║     ╚██████╔╝███████╗███████║██║  ██║██║  ██║"
        "╚═╝      ╚═════╝ ╚══════╝╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝"
    )
    local cols; cols="$(tput cols 2>/dev/null || echo 80)"
    [[ "$cols" =~ ^[0-9]+$ ]] || cols=80
    printf '\n'
    if [[ $FANCY -eq 1 && $UTF8 -eq 1 && $cols -ge 54 ]]; then
        local line
        for line in "${L[@]}"; do printf '  '; grad_line "$line" 49; nap 0.03; done
        printf '  %s━━━━━━━━━━━━━━━━━  %sC  O  D  E%s  ━━━━━━━━━━━━━━━━━━%s\n' "$DUST" "$ICE$B" "$R$DUST" "$R"
    else
        printf '  %sP U L S A R   C O D E%s\n' "$B" "$R"
    fi
    printf '  %sany intelligence  %s  one terminal%s\n\n' "$ASH" "$G_DOT" "$R"
}

step_n=0
say()  { printf '  %s%s%s\n' "$ASH" "$*" "$R"; }
ok()   { printf '  %s%s%s %s\n' "$OKC$B" "$G_OK" "$R" "$*"; }
warn() { printf '  %s!%s %s\n' "$EMBER$B" "$R" "$*"; }
die()  {
    printf '\n  %s%s %s%s\n' "$BADC$B" "$G_BAD" "$*" "$R" >&2
    if [[ -s "$LOG" ]]; then printf '\n  %slast lines of %s:%s\n' "$DUST" "$LOG" "$R" >&2; tail -n 15 "$LOG" | sed 's/^/    /' >&2; fi
    exit 1
}

# run "label" cmd...  : spinner while it runs, a mark after, output to $LOG
run() {
    local label="$1"; shift
    step_n=$((step_n + 1))
    echo "### $label: $*" >>"$LOG"
    if [[ $FANCY -eq 0 ]]; then
        printf '  [%d] %s ... ' "$step_n" "$label"
        if "$@" >>"$LOG" 2>&1; then echo "ok"; return 0; else echo "failed"; return 1; fi
    fi
    "$@" >>"$LOG" 2>&1 &
    RUN_PID=$!
    local i=0 n=${#SPIN[@]} colors=("$EMBER" "$FLARE" "$PLASMA" "$BEAM")
    printf '\033[?25l'
    while kill -0 "$RUN_PID" 2>/dev/null; do
        printf '\r  %s%s%s %s' "${colors[$(( (i / 3) % 4 ))]}$B" "${SPIN[$(( i % n ))]}" "$R" "$label"
        i=$((i + 1)); nap 0.08
    done
    local rc=0; wait "$RUN_PID" || rc=$?
    RUN_PID=""
    printf '\r\033[K\033[?25h'
    if [[ $rc -eq 0 ]]; then ok "$label"; else printf '  %s%s%s %s\n' "$BADC$B" "$G_BAD" "$R" "$label"; fi
    return $rc
}

with_timeout() { # seconds cmd... : run cmd, kill it after `seconds` (124)
    local limit=$(( $1 * 10 )) i=0 p rc=0; shift
    "$@" &
    p=$!
    while kill -0 "$p" 2>/dev/null; do
        if (( i >= limit )); then kill "$p" 2>/dev/null || true; wait "$p" 2>/dev/null || true; return 124; fi
        nap 0.1; i=$((i + 1))
    done
    wait "$p" || rc=$?
    return $rc
}

tl() { local p="$1"; [[ "$p" == "$HOME"* ]] && p="~${p#"$HOME"}"; printf '%s' "$p"; }

FETCH_CODE="000"
FETCH_RC=0
FETCH_TOOL=""
fetch_why() { # a readable reason for the last failed fetch: the HTTP status, or the tool's exit code
    if [[ "$FETCH_CODE" == "000" ]]; then
        printf 'no answer from github.com (%s exit %s, details in %s)' "${FETCH_TOOL:-download}" "$FETCH_RC" "$LOG"
    else
        printf 'github.com answered HTTP %s' "$FETCH_CODE"
    fi
}
fetch() { # url dest  (https only, never downgrades on a redirect); FETCH_CODE = last HTTP status, 000 if none
    local rc=0 out=""
    FETCH_CODE="000"
    if need curl; then
        local ca=()
        if [[ -n "$CA_FILE" ]]; then ca=(--cacert "$CA_FILE"); fi
        # -sS: progress stays quiet, but curl's own error (DNS, refused, TLS) reaches stderr (the log)
        out="$(curl -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2 --retry 3 --connect-timeout 15 \
                    ${ca[@]+"${ca[@]}"} -w '%{http_code}' -o "$2" "$1")" || rc=$?
        [[ "$out" =~ ^[0-9]{3}$ ]] && FETCH_CODE="$out"
        FETCH_TOOL="curl"
    elif need wget; then
        local flags=(-S -O "$2")                            # -S: the status lines parsed below
        if ! wget --help 2>&1 | grep -q BusyBox; then      # busybox wget knows neither option
            flags+=(--https-only --tries=3)
            if [[ -n "$CA_FILE" ]]; then flags+=(--ca-certificate="$CA_FILE"); fi
        fi
        out="$(wget "${flags[@]}" "$1" 2>&1)" || rc=$?
        [[ $rc -ne 0 ]] && printf '%s\n' "$out" | tail -n 5 >&2       # wget's own error, for the log
        out="$(printf '%s\n' "$out" | sed -n 's/^ *HTTP\/[0-9.]* \([0-9][0-9][0-9]\).*/\1/p' | tail -n 1)"
        [[ -n "$out" ]] && FETCH_CODE="$out"
        FETCH_TOOL="wget"
    else
        return 127
    fi
    FETCH_RC=$rc
    return $rc
}

sha256_of() { # file -> lowercase hex digest, or nothing when no tool is available
    local d=""
    if need sha256sum; then d="$(sha256sum "$1" | cut -d' ' -f1)"
    elif need shasum; then d="$(shasum -a 256 "$1" | cut -d' ' -f1)"
    elif need sha256; then d="$(sha256 -q "$1")"                       # FreeBSD, OpenBSD
    elif need openssl; then d="$(openssl dgst -sha256 "$1" 2>/dev/null | sed 's/^.*= //')"
    fi
    printf '%s' "$d" | tr 'A-F' 'a-f'
}

check_ref() {
    if [[ ! "$REF" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}$ || "$REF" == *..* || "$REF" == *//* || "$REF" == */ \
          || "$REF" == *. || "$REF" == *.lock ]]; then
        die "PULSAR_REF must be a tag, branch or commit (letters, digits, . _ / - only)"
    fi
}

# ---------------------------------------------------------------- steps

windows_handoff() { # Git Bash / MSYS2 / Cygwin: install.ps1 does the Windows install
    local ps="" root dir rc=0
    root="${SYSTEMROOT:-${SystemRoot:-C:\\Windows}}"
    if need cygpath; then
        ps="$(cygpath -u "$root")/System32/WindowsPowerShell/v1.0/powershell.exe"
        [[ -x "$ps" ]] || ps=""
    fi
    if [[ -z "$ps" || "$WANT_LOCAL" == "1" || -n "${PULSAR_SRC:-}" ]]; then
        die "On Windows, run in PowerShell:  irm https://raw.githubusercontent.com/$REPO/main/install.ps1 | iex"
    fi
    dir="$(mktemp -d "$TMPBASE/pulsarcode-ps.XXXXXX")"; TMP_SRC="$dir"
    fetch "https://raw.githubusercontent.com/$REPO/$REF/install.ps1" "$dir/install.ps1" ||
        die "could not download install.ps1 ($REF). In PowerShell:  irm https://raw.githubusercontent.com/$REPO/main/install.ps1 | iex"
    say "Windows: handing over to install.ps1 (it also installs a \`pulsarcode\` command for Git Bash)"
    "$ps" -NoProfile -ExecutionPolicy Bypass -File "$(cygpath -w "$dir/install.ps1")" || rc=$?
    ROLL_BACK=0                                # install.ps1 did the install: its status is the run's
    exit $rc
}

detect_platform() {
    local sys; sys="$(uname -s)"
    case "$sys" in
        Darwin) OS=macos ;;
        Linux)  OS=linux; grep -qi microsoft /proc/version 2>/dev/null && OS=wsl ;;
        FreeBSD|OpenBSD|NetBSD|DragonFly)
            OS=bsd
            warn "$sys support is experimental: Pulsar Code installs, but Claude Code comes from npm and needs ripgrep (pkg install ripgrep node npm)" ;;
        MINGW*|MSYS*|CYGWIN*) windows_handoff ;;
        *) die "unsupported OS $sys" ;;
    esac
    ARCH="$(uname -m)"
    need tar || die "tar is required"
    need curl || need wget || die "curl or wget is required"
    ok "platform  $OS $ARCH"
}

script_dir() { # the folder of this script when bash runs it from a real file, else nothing
    local f="${BASH_SOURCE[0]:-}"
    [[ -n "$f" && -f "$f" ]] || return 1
    (cd "$(dirname "$f")" 2>/dev/null && pwd -P)
}

local_source_ok() { # dir -> 0 when it holds the engine and is yours (not a shared or world-writable folder)
    local d="$1"
    [[ -f "$d/pulsar/__init__.py" && -f "$d/pulsarcode" && -f "$d/requirements.txt" ]] || return 1
    [[ -O "$d" && -O "$d/pulsar" && -O "$d/pulsar/__init__.py" ]] || return 1
    [[ -z "$(find "$d" "$d/pulsar" -maxdepth 0 -perm -002 -print 2>/dev/null)" ]]
}

sums_lookup() { # SHA256SUMS name -> its lowercase digest (accepts "hash  name" and "hash *name")
    tr -d '\r' <"$1" | awk -v f="$2" '{ n = $2; sub(/^\*/, "", n); if (n == f) { print tolower($1); exit } }'
}

older_than_v3() { # tag -> 0 for v0.x, v1.x and v2.x: releases made before every release published SHA256SUMS
    local re='^v([0-9]{1,9})(\.|$)'
    [[ "$1" =~ $re ]] && (( 10#${BASH_REMATCH[1]} < 3 ))
}

fetch_release() { # PULSAR_REF is a release tag: its archive, verified against its SHA256SUMS.
    # 0: verified and unpacked. 1: a release older than v3 that publishes no SHA256SUMS (use the tag
    # archive). Anything that does not verify ends the install here.
    local base="https://github.com/$REPO/releases/download/$REF" sums="$TMP_SRC/SHA256SUMS"
    local name="pulsarcode-app-${REF#v}.tar.gz" want got
    echo "### SHA256SUMS: $base/SHA256SUMS" >>"$LOG"
    if ! fetch "$base/SHA256SUMS" "$sums" 2>>"$LOG"; then
        rm -f "$sums"
        if [[ "$FETCH_CODE" == "404" ]] && older_than_v3 "$REF"; then
            warn "verify    release $REF publishes no SHA256SUMS: installing its tag archive (HTTPS only, not checksum-verified)"
            return 1
        fi
        if [[ "$FETCH_CODE" == "404" ]]; then
            die "release $REF publishes no SHA256SUMS. Every release from v3 on does, and is only installed after its checksum verifies: check the tag, or wait for the release to finish publishing and re-run."
        fi
        die "could not download SHA256SUMS for $REF: $(fetch_why). A release tag is only installed after its checksum verifies: check your connection or proxy and re-run."
    fi
    [[ "$name" =~ ^pulsarcode-app-[A-Za-z0-9._+-]+\.tar\.gz$ ]] || die "unexpected release asset name $name"
    want="$(sums_lookup "$sums" "$name")"
    [[ "$want" =~ ^[0-9a-f]{64}$ ]] || die "SHA256SUMS of $REF does not list $name: refusing to install it unverified"
    run "download  Pulsar Code $REF (release archive)" fetch "$base/$name" "$TMP_SRC/src.tgz" ||
        die "could not download $name from the $REF release"
    got="$(sha256_of "$TMP_SRC/src.tgz")"
    [[ -n "$got" ]] || die "no SHA-256 tool found (sha256sum, shasum, sha256 or openssl) to verify the download"
    if [[ "$got" != "$want" ]]; then
        rm -f "$TMP_SRC/src.tgz"
        die "checksum mismatch for $name: SHA256SUMS says $want, the download is $got. Nothing was installed."
    fi
    ok "verify    $name matches the release SHA256SUMS"
    return 0
}

get_source() {
    local want="${PULSAR_SRC:-}" here=""
    if [[ "${PULSAR_NO_LOCAL_SRC:-0}" == "1" ]]; then
        want=""; WANT_LOCAL=0
    fi
    if [[ -z "$want" && "$WANT_LOCAL" == "1" ]]; then
        want="$(script_dir)" || die "--local needs this script as a file: bash /path/to/pulsarcode/install.sh --local"
    fi
    if [[ -n "$want" ]]; then
        want="$(cd "$want" 2>/dev/null && pwd -P)" || die "PULSAR_SRC is not a folder"
        local_source_ok "$want" ||
            die "refusing to install from $want: it must hold pulsar/, pulsarcode and requirements.txt, be owned by you and not be world-writable"
        SRC="$want"
        ok "source    local checkout $(tl "$SRC")"
        return
    fi
    if [[ "${PULSAR_NO_LOCAL_SRC:-0}" != "1" ]] && here="$(script_dir)" && [[ -f "$here/pulsar/__init__.py" && -e "$here/.git" ]]; then
        say "this script sits in a Pulsar Code checkout; installing $REF from GitHub (add --local to install the checkout)"
    fi
    TMP_SRC="$(mktemp -d "$TMPBASE/pulsarcode-src.XXXXXX")"
    if [[ "$REF" == v[0-9]* ]] && fetch_release; then
        :
    else
        local url urls=()
        if [[ "$REF" == v[0-9]* ]]; then urls=("https://github.com/$REPO/archive/refs/tags/$REF.tar.gz")
        elif [[ "$REF" =~ ^[0-9a-f]{40}$ ]]; then urls=("https://github.com/$REPO/archive/$REF.tar.gz")
        else urls=("https://github.com/$REPO/archive/refs/heads/$REF.tar.gz" "https://github.com/$REPO/archive/refs/tags/$REF.tar.gz"); fi
        for url in "${urls[@]}"; do
            if run "download  Pulsar Code ($REF)" fetch "$url" "$TMP_SRC/src.tgz"; then break; fi
            rm -f "$TMP_SRC/src.tgz"
        done
        [[ -s "$TMP_SRC/src.tgz" ]] || die "could not download Pulsar Code $REF from github.com/$REPO"
    fi
    tar -xzf "$TMP_SRC/src.tgz" -C "$TMP_SRC" || die "corrupt download"
    SRC="$(find "$TMP_SRC" -mindepth 1 -maxdepth 1 -type d | head -1)"
    [[ -n "$SRC" && -f "$SRC/pulsar/__init__.py" ]] || die "download does not contain the Pulsar Code engine"
}

MARKER=".pulsarcode-home"
# Which existing folder may become (or already is) the Pulsar home follows the engine's own rule
# (pulsar/config.py, ensure_home): a marker with provenance is ours; otherwise only an empty folder is
# "created", and any other folder is "adopted" only on Pulsar Code's own evidence. The installer, which
# writes far more there (the engine, the runtime), is stricter in two places: an earlier install is
# adopted only when nothing else sits next to it, and next to a marker without provenance (which a
# development build wrote into any folder) only the engine itself counts as evidence.

json_object() { # file -> 0 when its first non-blank character opens a JSON object
    [[ "$(awk '{ sub(/^[ \t\r]+/, "") } length($0) { print substr($0, 1, 1); exit }' "$1" 2>/dev/null)" == "{" ]]
}

json_python() { # a python3 to read JSON with before the private runtime exists, or nothing (status 1): found
                # in absolute PATH entries only, and never the macOS stub that opens a dialog
    local p
    p="$(native_cmd python3)" || return 1
    safe_to_probe "$p" || return 1
    printf '%s' "$p"
}

pulsar_config() { # config.json -> 0 when it is Pulsar Code's own, read as pulsar/config.py reads it: a JSON
                  # object with a "profiles" object and an "active" key. A file nothing could read proves nothing.
    local py
    py="$(json_python)" || return 1
    "$py" -I -S -c 'import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        d = json.load(f)
except (OSError, ValueError):
    sys.exit(1)
sys.exit(0 if isinstance(d, dict) and isinstance(d.get("profiles"), dict) and "active" in d else 1)' "$1" \
        </dev/null >/dev/null 2>&1
}

marker_trusted() { # dir -> 0 when its marker carries provenance: a "created" or "adopted" line after the first
    awk 'NR > 1 { sub(/\r$/, ""); if ($0 == "created" || $0 == "adopted" || $0 ~ /^(created|adopted) /) f = 1 }
         END { exit !f }' "$1/$MARKER" 2>/dev/null
}

home_evidence() { # dir -> what shows it is (or was) Pulsar Code's home, as pulsar/config.py checks it: app (the
                  # engine), config.json, nim.key (v1's key) or claude_config (the isolated profile); else status 1
    if [[ -f "$1/app/pulsar/__init__.py" ]]; then printf 'app'
    elif [[ -f "$1/config.json" ]] && pulsar_config "$1/config.json"; then printf 'config.json'
    elif [[ -f "$1/nim.key" ]]; then printf 'nim.key'
    elif [[ -d "$1/claude_config" ]]; then printf 'claude_config'
    else return 1
    fi
}

home_entries() { # dir -> its names, one per line, apart from the temp files of the engine's atomic writes
    ( shopt -s nullglob dotglob
      for p in "$1"/*; do
          n="${p##*/}"
          case "$n" in .config.json.*|.keys.json.*|.settings.json.*) continue ;; esac
          printf '%s\n' "$n"
      done )
}

foreign_entries() { # dir -> the names in dir that Pulsar Code (or this installer) never writes, one per line:
                    # pulsar/config.py's list, where a JSON file, venv/, app/ and bin/ also need Pulsar's content
    ( shopt -s nullglob dotglob
      for p in "$1"/*; do
          n="${p##*/}"
          case "$n" in
              run|cache|logs|plugin|claude_config|stage|canonical|claude-install.sh|"$MARKER") continue ;;
              nim.key|nim.key.migrated|active_model|model_history|onboarding_complete|nim_sonar_catalog.json) continue ;;
              .config.json.*|.keys.json.*|.settings.json.*) continue ;;
              # With no Python to read it, a JSON file is unknown: neither evidence nor foreign (as the engine,
              # which can read it, decides).
              config.json) pulsar_config "$p" && continue; json_python >/dev/null || continue ;;
              keys.json|settings.json) json_object "$p" && continue; json_python >/dev/null || continue ;;
              venv|venv.old) [[ -f "$p/pyvenv.cfg" || -x "$p/bin/python" ]] && continue ;;  # an interrupted install
              app|app.new|app.old) [[ -f "$p/pulsar/__init__.py" ]] && continue ;;
              bin) if [[ -d "$p" ]]; then
                       mine=1
                       for b in "$p"/*; do
                           case "${b##*/}" in uv|uvx|uv-install.sh) ;; *) mine=0 ;; esac
                       done
                       [[ $mine -eq 1 ]] && continue
                   fi ;;
          esac
          printf '%s\n' "$n"
      done )
}

empty_home() { # dir -> 0 when nothing is in it: the only folder that is ever marked "created"
    [[ -z "$(home_entries "$1")" ]]
}

only_pulsar_state() { # dir -> 0 when it holds nothing but what Pulsar Code (or this installer) writes there
    [[ -z "$(foreign_entries "$1")" ]]
}

older_pulsar_home() { # dir -> 0 when an earlier Pulsar Code install lives there next to nothing else. It needs
                      # Pulsar's evidence (home_evidence); next to a marker without provenance only app/ counts.
    local ev
    ev="$(home_evidence "$1")" || return 1
    [[ "$ev" == app || ! -e "$1/$MARKER" ]] || return 1
    only_pulsar_state "$1"
}

prepare_home() {
    # Never move into a folder that holds someone else's files: a mistaken PULSAR_HOME (a project,
    # ~/.local) would get Pulsar's files, and `pulsarcode uninstall` would later empty it.
    local kind="" real shown="" n why
    if [[ -e "$PULSAR_HOME" && ! -d "$PULSAR_HOME" ]]; then die "PULSAR_HOME=$PULSAR_HOME is not a folder"; fi
    if [[ -d "$PULSAR_HOME" ]]; then
        json_python || true             # found once here: only_pulsar_state reads JSON in a subshell
        real="$(cd "$PULSAR_HOME" && pwd -P)"
        [[ "$real" != "/" ]] || die "PULSAR_HOME=$PULSAR_HOME is a filesystem root"
        [[ "$real" != "$(cd "$HOME" 2>/dev/null && pwd -P)" ]] || die "PULSAR_HOME=$PULSAR_HOME is your home directory"
        [[ -r "$real" ]] || die "PULSAR_HOME=$PULSAR_HOME cannot be read, so what it holds cannot be checked. Nothing was written there."
        if marker_trusted "$real"; then kind=""
        elif empty_home "$real"; then kind="created"
        elif older_pulsar_home "$real"; then kind="adopted"
        else
            # Only Pulsar Code's names but none of its evidence (settings.json, logs/, cache/ are just as
            # likely another tool's), or files it never writes: name what is there either way.
            why="did not create"
            if only_pulsar_state "$real"; then why="cannot tell are its own"; fi
            while IFS= read -r n; do shown+="${shown:+, }${n//[^[:print:]]/?}"; done < <(
                if [[ "$why" == "did not create" ]]; then foreign_entries "$real"; else home_entries "$real"; fi | head -n 5)
            die "PULSAR_HOME=$PULSAR_HOME already holds files Pulsar Code $why${shown:+ ($shown)}. Nothing was written there. Point PULSAR_HOME at a new or empty folder."
        fi
    else
        mkdir -p "$PULSAR_HOME" || die "could not create PULSAR_HOME=$PULSAR_HOME"
        kind="created"
    fi
    chmod 700 "$PULSAR_HOME" 2>/dev/null || true
    if [[ -n "$kind" ]]; then
        printf 'Pulsar Code home. `pulsarcode uninstall` only removes folders that carry this file.\n%s %s\n' \
            "$kind" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$PULSAR_HOME/$MARKER"
    fi
    # A re-install (`pulsarcode update`) puts the command where this home's install put it, unless
    # PULSAR_BIN_DIR says otherwise.
    if [[ -z "${PULSAR_BIN_DIR:-}${PULSARCODE_BIN_DIR:-}" ]]; then
        local recorded
        recorded="$(sed -n 's/^bin \(\/.*\)$/\1/p' "$PULSAR_HOME/$MARKER" 2>/dev/null | tail -n 1)" || recorded=""
        if [[ -n "$recorded" && -d "$recorded" ]]; then BIN_DIR="$recorded"; fi
    fi
}

enter_home() { # uv and npm read project settings (uv.toml, pyproject.toml, .npmrc) from the current folder
               # and its parents: from here on that is PULSAR_HOME, never the folder this script was started
               # from (anyone's repository, or a shared folder)
    cd "$PULSAR_HOME" || die "could not enter PULSAR_HOME=$PULSAR_HOME"
}

py_ok() { # python -> 0 if >= 3.9 with venv  (-I: the current folder can't shadow venv or ensurepip)
    "$1" -I - <<'PY' >/dev/null 2>&1
import sys, venv, ensurepip
sys.exit(0 if sys.version_info >= (3, 9) else 1)
PY
}

macos_clt() { [[ -x /usr/bin/xcode-select ]] && /usr/bin/xcode-select -p >/dev/null 2>&1; }

safe_to_probe() { # python path -> 0 when running it cannot pop a dialog
    # On a Mac without the Command Line Tools, /usr/bin/python3 is a stub that opens the
    # "install developer tools?" dialog the moment it runs.
    [[ "$OS" == macos ]] || return 0
    case "$1" in /usr/bin/*) macos_clt ;; *) return 0 ;; esac
}

find_system_python() { # prints a usable system python (>= 3.9 with venv), or nothing
    local c p
    for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3 python3.9; do
        p="$(command -v "$c" 2>/dev/null)" || continue
        [[ -n "$p" ]] && safe_to_probe "$p" || continue
        if py_ok "$p"; then printf '%s' "$p"; return 0; fi
    done
    return 0
}

uv_usable() { # path -> 0 when it is a uv new enough for the flags used here
    local v re='^uv ([0-9]+)\.([0-9]+)'
    v="$("$1" --version 2>/dev/null | head -n 1)" || return 1
    [[ "$v" =~ $re ]] || return 1
    (( 10#${BASH_REMATCH[1]} > 0 || 10#${BASH_REMATCH[2]} >= UV_MIN_MINOR ))
}

find_uv() {
    local c
    for c in "$(command -v uv 2>/dev/null || true)" "$PULSAR_HOME/bin/uv" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [[ -n "$c" && -x "$c" ]]; then
            if uv_usable "$c"; then UV="$c"; return 0; fi
            echo "### ignoring $c: not uv >= 0.$UV_MIN_MINOR ($("$c" --version 2>&1 | head -n 1))" >>"$LOG"
        fi
    done
    UV=""
    return 1
}

install_uv() {
    [[ "$OS" != bsd ]] || { echo "uv publishes no builds for $(uname -s)" >&2; return 1; }
    mkdir -p "$PULSAR_HOME/bin"
    local inst="$PULSAR_HOME/bin/uv-install.sh" got
    fetch "https://astral.sh/uv/$UV_VERSION/install.sh" "$inst" || return 1
    got="$(sha256_of "$inst")"
    if [[ "$got" != "$UV_INSTALLER_SHA256" ]]; then
        echo "uv $UV_VERSION installer checksum mismatch: got ${got:-none}" >&2
        rm -f "$inst"; return 1
    fi
    env UV_INSTALL_DIR="$PULSAR_HOME/bin" UV_UNMANAGED_INSTALL="$PULSAR_HOME/bin" INSTALLER_NO_MODIFY_PATH=1 \
        sh "$inst" && rm -f "$inst" && [[ -x "$PULSAR_HOME/bin/uv" ]]
}

venv_with_uv() { # [system python] -> the virtualenv, made by $UV
    if [[ "$OS" == macos ]]; then
        # A uv-managed Python: never the /usr/bin stub, never Apple's 3.9 with LibreSSL, and
        # `brew upgrade` cannot pull it out from under the venv.
        "$UV" venv --quiet --allow-existing --python 3.12 --python-preference only-managed "$VENV" && return 0
        [[ -n "${1:-}" ]] && "$UV" venv --quiet --allow-existing --python "$1" "$VENV"
        return
    fi
    "$UV" venv --quiet --allow-existing --python ">=3.$PY_MIN_MINOR" --python-preference system "$VENV" ||
        "$UV" venv --quiet --allow-existing --python 3.12 --python-preference only-managed "$VENV"
}

make_venv() { # the first way that works; 1 when none did
    local sys_py; sys_py="$(find_system_python)"
    if find_uv; then
        run "runtime   Python via uv" venv_with_uv "$sys_py" && return 0
        rm -rf "$VENV"
    fi
    if [[ "$OS" != macos && -n "$sys_py" ]]; then
        run "runtime   $("$sys_py" -V 2>&1)" "$sys_py" -I -m venv "$VENV" && return 0
        rm -rf "$VENV"
    fi
    if [[ "$OS" != bsd && "$UV" != "$PULSAR_HOME/bin/uv" ]]; then
        if run "runtime   fetching uv $UV_VERSION (private copy)" install_uv; then
            UV="$PULSAR_HOME/bin/uv"
            run "runtime   Python via uv" venv_with_uv "$sys_py" && return 0
            rm -rf "$VENV"
        fi
    fi
    if [[ "$OS" == macos && -n "$sys_py" ]]; then
        run "runtime   $("$sys_py" -V 2>&1)" "$sys_py" -I -m venv "$VENV" && return 0
        rm -rf "$VENV"
    fi
    return 1
}

pip_with_uv() { "$UV" pip install --quiet --python "$VENV/bin/python" -r "$SRC/requirements.txt"; }

pip_plain() {
    local e=()
    if [[ -z "${PIP_CERT:-}" && -n "$CA_FILE" ]]; then e=(PIP_CERT="$CA_FILE"); fi
    # A venv made by uv has no pip: bring the bundled one in (works offline).
    if ! "$VENV/bin/python" -I -c 'import pip' >/dev/null 2>&1; then
        "$VENV/bin/python" -I -m ensurepip --default-pip || return 1
    fi
    # No --upgrade: an index is only consulted when pip really is older than 23.
    env ${e[@]+"${e[@]}"} "$VENV/bin/python" -I -m pip install --quiet --disable-pip-version-check "pip>=23" || true
    env ${e[@]+"${e[@]}"} "$VENV/bin/python" -I -m pip install --quiet --disable-pip-version-check -r "$SRC/requirements.txt"
}

setup_python() {
    # Before the runtime is rebuilt or upgraded: a running Core has the venv's files loaded (on
    # Windows that locks them; here it would keep running old dependencies).
    stop_running_core
    if [[ -x "$VENV/bin/python" ]] && "$VENV/bin/python" -I -c "import sys; sys.exit(0 if sys.version_info>=(3,$PY_MIN_MINOR) else 1)" 2>/dev/null; then
        if [[ "$OS" != macos ]] || ! rebuild_on_managed_python; then
            ok "runtime   reusing $("$VENV/bin/python" -V 2>&1)"
        fi
    else
        # Only a virtualenv (it has pyvenv.cfg) or an empty folder is ever rebuilt: a venv/ folder of
        # anyone else's stops the install instead.
        if [[ -e "$VENV" && ! -f "$VENV/pyvenv.cfg" ]] && ! rmdir "$VENV" 2>/dev/null; then
            die "$(tl "$VENV") is not a Python environment this installer made (no pyvenv.cfg): move it out of the way and re-run"
        fi
        rm -rf "$VENV"
        VENV_BUSY=1
        make_venv || die "could not create a Python 3.$PY_MIN_MINOR+ runtime (no usable python3, and uv could not provide one)"
    fi
    VENV_BUSY=1
    if find_uv; then
        run "deps      starlette, uvicorn, httpx" pip_with_uv || run "deps      retry with pip" pip_plain || die "dependency install failed"
    else
        run "deps      starlette, uvicorn, httpx" pip_plain || die "dependency install failed"
    fi
    VENV_BUSY=0
}

rebuild_on_managed_python() { # macOS: 0 when the runtime was rebuilt on a uv-managed Python. A runtime on a
    # Python uv does not manage (Homebrew's, which `brew upgrade` replaces under it; Apple's; v1's) moves to
    # one when uv can provide it. The previous runtime waits in venv.old until the install has verified itself.
    local base dir was
    base="$(awk -F' *= *' '$1 == "home" { print $2; exit }' "$VENV/pyvenv.cfg" 2>/dev/null)" || base=""
    [[ -n "$base" ]] || return 1
    if ! find_uv; then
        run "runtime   fetching uv $UV_VERSION (private copy)" install_uv || return 1
        UV="$PULSAR_HOME/bin/uv"
    fi
    dir="$("$UV" python dir 2>/dev/null)" || return 1
    [[ -n "$dir" && "$base" != "$dir"/* ]] || return 1                      # already on uv's own Python
    was="$("$VENV/bin/python" -V 2>&1)" || was="Python"
    # Never moved into a venv.old that is still there (mv would nest it): then the runtime stays as it is.
    rm -rf "$VENV.old" 2>/dev/null || true
    [[ ! -e "$VENV.old" ]] && mv "$VENV" "$VENV.old" || return 1
    VENV_ASIDE=1; VENV_BUSY=1
    if run "runtime   Python via uv, replacing $was (not managed by uv)" venv_with_uv ""; then
        VENV_BUSY=0
        return 0
    fi
    rm -rf "$VENV" 2>/dev/null || true                                      # keep the one that works
    if [[ ! -e "$VENV" ]] && mv "$VENV.old" "$VENV"; then VENV_ASIDE=0; fi
    VENV_BUSY=0
    return 1
}

stop_running_core() { # a Core started from the old engine must not outlive it
    local desc="$PULSAR_HOME/run/core.json" out="" i
    [[ -f "$desc" ]] || return 0
    [[ -f "$APP/pulsar/__init__.py" && -x "$VENV/bin/python" ]] || return 0
    out="$(with_timeout 15 env PULSAR_HOME="$PULSAR_HOME" "$VENV/bin/python" -E -B -c "$BOOT" "$APP" pulsar stop \
            </dev/null 2>>"$LOG")" || true
    echo "### pulsar stop: $out" >>"$LOG"
    for (( i = 0; i < 50; i++ )); do
        [[ -f "$desc" ]] || break
        nap 0.1
    done
    if [[ -f "$desc" ]]; then
        warn "core      a Pulsar Core is still running with the old engine: it stops once its open sessions end (pulsarcode stop --force ends it now)"
    elif [[ "$out" == *"shutting down"* ]]; then
        ok "core      stopped the running Pulsar Core (restart open sessions to use the new engine)"
    fi
}

place_app() {
    local stage="$PULSAR_HOME/app.new"
    # The engine folder is only ever replaced by a newer engine, never when it holds anything else.
    if [[ -e "$APP" || -L "$APP" ]] && [[ ! -f "$APP/pulsar/__init__.py" ]]; then
        die "$(tl "$APP") is not a Pulsar Code engine: move it out of the way and re-run"
    fi
    rm -rf "$stage"; mkdir -p "$stage"
    cp -R "$SRC/pulsar" "$stage/pulsar"
    cp "$SRC/pulsarcode" "$SRC/requirements.txt" "$stage/"
    [[ -f "$SRC/LICENSE" ]] && cp "$SRC/LICENSE" "$stage/"
    chmod 755 "$stage/pulsarcode"
    find "$stage" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    stop_running_core                          # no-op unless something started a Core meanwhile
    # The engine this replaces waits in app.old until the new one has verified itself (commit_install). An
    # app.old next to no app/ is the engine an interrupted run set aside: then that is the one kept.
    if [[ -d "$APP" ]]; then rm -rf "$APP.old"; mv "$APP" "$APP.old"; fi
    mv "$stage" "$APP"; PLACED=1
    "$VENV/bin/python" -I -m compileall -q "$APP/pulsar" >/dev/null 2>&1 || true
    ok "engine    $("$VENV/bin/python" -E -B -c "$BOOT" "$APP" pulsar version)  $(tl "$APP")"
}

native_cmd() { # name -> its path on PATH, skipping empty (current folder) entries and, under WSL, the
                # Windows folders WSL appends (/mnt/c/...): a Windows claude or npm there would bypass
                # Pulsar (WSL interop drops ANTHROPIC_BASE_URL and friends) or install the Windows package.
    local d IFS=:
    for d in $PATH; do
        [[ -n "$d" && "$d" == /* && -x "$d/$1" && ! -d "$d/$1" ]] || continue
        if [[ "$OS" == wsl && "$d" == "$WSL_DRIVES"/[a-zA-Z]/* ]]; then continue; fi
        printf '%s' "$d/$1"
        return 0
    done
    return 1
}

claude_present() {
    native_cmd claude >/dev/null || [[ -x "$HOME/.local/bin/claude" || -x "$HOME/.claude/local/claude" ]]
}

install_claude_native() {
    fetch "https://claude.ai/install.sh" "$PULSAR_HOME/claude-install.sh" && bash "$PULSAR_HOME/claude-install.sh" </dev/null
    local rc=$?; rm -f "$PULSAR_HOME/claude-install.sh"; return $rc
}

setup_claude() {
    if [[ "${PULSAR_SKIP_CLAUDE:-0}" == "1" ]]; then warn "claude    skipped (PULSAR_SKIP_CLAUDE=1)"; return; fi
    if claude_present; then
        local c v
        c="$(native_cmd claude)" || c="$HOME/.local/bin/claude"
        v="$("$c" --version 2>/dev/null </dev/null | head -1)" || v=""
        ok "claude    ${v:-present}"
        return
    fi
    # The native installer has no BSD build; npm's package runs anywhere node does.
    if [[ "$OS" != bsd ]] && run "claude    installing Claude Code (official installer)" install_claude_native && claude_present; then
        return
    fi
    local npm
    if npm="$(native_cmd npm)" && run "claude    installing via npm" "$npm" install -g @anthropic-ai/claude-code &&
            claude_present; then
        return
    fi
    die "Claude Code could not be installed automatically. Install it (https://claude.com/claude-code) and re-run."
}

rc_file() { # the startup file of the user's login shell, and the line that puts BIN_DIR on PATH
    RC_LINE="export PATH=\"$BIN_DIR:\$PATH\""
    case "${SHELL:-}" in
        */zsh)  RC_FILE="$HOME/.zshrc"
                # $ZDOTDIR only when absolute: the same rule `pulsarcode uninstall` uses to find it
                if [[ "${ZDOTDIR:-}" == /* ]]; then RC_FILE="$ZDOTDIR/.zshrc"; fi ;;
        */bash) RC_FILE="$HOME/.bashrc"
                # A macOS terminal opens a login shell, and bash reads only the first of these that
                # exists: a new ~/.bash_profile would hide an existing ~/.bash_login or ~/.profile.
                if [[ "$OS" == macos ]]; then
                    RC_FILE="$HOME/.bash_profile"
                    if [[ ! -e "$HOME/.bash_profile" ]]; then
                        if [[ -e "$HOME/.bash_login" ]]; then RC_FILE="$HOME/.bash_login"
                        elif [[ -e "$HOME/.profile" ]]; then RC_FILE="$HOME/.profile"; fi
                    fi
                fi ;;
        */fish) RC_FILE="$HOME/.config/fish/config.fish"
                if [[ "${XDG_CONFIG_HOME:-}" == /* ]]; then RC_FILE="$XDG_CONFIG_HOME/fish/config.fish"; fi
                # -g keeps the folder in this file only: plain fish_add_path also stores it in fish's
                # universal fish_user_paths, which no edit of a startup file (so no `pulsarcode
                # uninstall`) takes back. Unquoted when it can be (the form uninstall removes again).
                if [[ "$BIN_DIR" =~ ^[A-Za-z0-9._/~+-]+$ ]]; then RC_LINE="fish_add_path -g $BIN_DIR"
                else RC_LINE="fish_add_path -g '$BIN_DIR'"; fi ;;
        *)      RC_FILE="$HOME/.profile" ;;
    esac
    return 0
}

rc_has_line() { # -> 0 when RC_FILE already puts BIN_DIR on PATH: the exact line, or in config.fish the
                # forms earlier installs wrote (plain fish_add_path, v1's set -gx PATH)
    grep -qsxF -- "$RC_LINE" "$RC_FILE" 2>/dev/null && return 0
    [[ "$RC_LINE" == "fish_add_path -g "* ]] || return 1
    grep -qsxF -e "fish_add_path ${RC_LINE#fish_add_path -g }" -e "set -gx PATH \"$BIN_DIR\" \$PATH" -- "$RC_FILE" \
        2>/dev/null
}

add_to_path() { # BIN_DIR on PATH for new shells. Never fails the install: a read-only or root-owned
                # startup file (NixOS home-manager, managed dotfiles) gets the line printed instead.
    rc_file
    if [[ "$BIN_DIR" == *[\"\$\`\\\']* ]]; then
        PATH_HINT=2
        warn "PATH      $BIN_DIR contains quotes or \$; add it to PATH yourself"
        return 0
    fi
    # The exact line, never a mention of the folder: a comment naming it puts nothing on PATH.
    if rc_has_line; then PATH_HINT=1; return 0; fi
    if { mkdir -p "$(dirname "$RC_FILE")" && [[ ! -e "$RC_FILE" || -w "$RC_FILE" ]] \
           && printf '\n# Pulsar Code\n%s\n' "$RC_LINE" >>"$RC_FILE"; } 2>/dev/null; then
        PATH_HINT=1
        ok "PATH      added $(tl "$BIN_DIR") to $(tl "$RC_FILE")"
    else
        PATH_HINT=2
        warn "PATH      could not write $(tl "$RC_FILE") (read-only?). Add this line to your shell startup file:"
        printf '              %s\n' "$RC_LINE"
    fi
    return 0
}

link_target() { # path -> where its chain of symbolic links ends, as written (it may not exist)
    local p="$1" t n=0
    while [[ -L "$p" ]] && (( n < 40 )); do
        t="$(readlink "$p")" || return 1
        case "$t" in /*) p="$t" ;; *) p="$(dirname "$p")/$t" ;; esac
        n=$((n + 1))
    done
    printf '%s' "$p"
}

ours_or_free() { # path -> 0 when nothing is there, or a link leading into PULSAR_HOME: a command an earlier
                 # install of this home made (v1's link into canonical/ too, even once canonical/ is gone)
    local t d h hp
    [[ -e "$1" || -L "$1" ]] || return 0
    [[ -L "$1" ]] || return 1                     # a file of its own: a pipx or uv tool launcher, a copy
    t="$(link_target "$1")" || return 1
    h="$(cd "$PULSAR_HOME" 2>/dev/null && pwd)" || return 1
    hp="$(cd "$PULSAR_HOME" 2>/dev/null && pwd -P)" || return 1
    d="$(dirname "$t")"
    if [[ -d "$d" ]]; then
        d="$(cd "$d" 2>/dev/null && pwd -P)" || return 1
        [[ "$d/" == "$hp"/* ]]
    else
        [[ "$t" != *"/../"* && ( "$t" == "$h"/* || "$t" == "$hp"/* ) ]]
    fi
}

check_bin() { # another install's `pulsarcode` in BIN_DIR is never replaced without PULSAR_FORCE=1
    local cmd="$BIN_DIR/pulsarcode" t=""
    if [[ "${PULSAR_FORCE:-0}" == "1" ]] || ours_or_free "$cmd"; then return 0; fi
    if [[ -L "$cmd" ]]; then t="$(link_target "$cmd")" || t=""; fi
    t="${t//[^[:print:]]/?}"
    die "$(tl "$cmd") is another install's pulsarcode command${t:+ (it runs $(tl "$t"))}. Nothing was changed. Install this one with PULSAR_BIN_DIR=<another folder>, or set PULSAR_FORCE=1 to replace that command."
}

link_bin() {
    local prev=""
    check_bin
    mkdir -p "$BIN_DIR" || die "could not create $BIN_DIR"
    # What the command pointed at before (v1's link into canonical/, say) comes back if the run stops.
    if [[ -L "$BIN_DIR/pulsarcode" ]]; then prev="$(readlink "$BIN_DIR/pulsarcode")" || prev=""; fi
    ln -sfn "$APP/pulsarcode" "$BIN_DIR/pulsarcode"
    if [[ -n "$prev" && "$prev" != "$APP/pulsarcode" ]]; then LINK_PREV="$prev"; fi
    ok "command   $(tl "$BIN_DIR/pulsarcode")"
    record_bin_dir
    if [[ ":$PATH:" == *":$BIN_DIR:"* || "${PULSAR_NO_PATH:-0}" == "1" ]]; then return 0; fi
    add_to_path
}

record_bin_dir() { # note BIN_DIR in the home marker, so `pulsarcode uninstall` finds the command even
    # when that folder is not on PATH (it only removes a pulsarcode file that points into this home)
    local marker="$PULSAR_HOME/$MARKER" abs
    [[ -f "$marker" ]] || return 0
    abs="$(cd "$BIN_DIR" 2>/dev/null && pwd)" || return 0
    case "$abs" in *$'\n'*|*$'\r'*|"") return 0 ;; esac
    grep -qxF "bin $abs" "$marker" 2>/dev/null || printf 'bin %s\n' "$abs" >>"$marker" 2>/dev/null || true
    return 0
}

commit_install() { # the new engine has verified itself: only now does what it replaced go. A leftover that
                   # cannot be removed is named; it never turns the verified install into a failure.
    local left=""
    COMMITTED=1
    rm -rf "$APP.old" 2>/dev/null || left="$(tl "$APP.old")"
    if [[ -f "$VENV.old/pyvenv.cfg" ]]; then rm -rf "$VENV.old" 2>/dev/null || left="${left:+$left, }$(tl "$VENV.old")"; fi
    if [[ -d "$PULSAR_HOME/canonical" ]]; then      # a Pulsar Code v1 home: its engine lived in canonical/
        if rm -rf "$PULSAR_HOME/canonical" 2>/dev/null; then
            ok "upgrade   Pulsar Code v1 home: its v1 engine (canonical/) was removed; the next launch imports what v1 kept (key, model, history)"
        else
            left="${left:+$left, }$(tl "$PULSAR_HOME/canonical")"
        fi
    fi
    if [[ -n "$left" ]]; then warn "cleanup   could not remove $left, which nothing uses any more"; fi
    return 0
}

roll_back() { # [stopped] : an install run ended before commit_install. The engine, the runtime and the command
              # it replaced come back, so the earlier install (v1's too) works as before. stopped=1: a step was
              # still running, and one that writes into the new runtime may still be at it, so that stays.
    [[ $ROLL_BACK -eq 1 && $COMMITTED -eq 0 ]] || return 0
    local back=""
    if [[ -n "$LINK_PREV" ]] && ln -sfn "$LINK_PREV" "$BIN_DIR/pulsarcode" 2>/dev/null; then back="command"; fi
    if [[ $PLACED -eq 1 && -d "$APP.old" ]]; then
        rm -rf "$APP" 2>/dev/null || true
        if [[ ! -e "$APP" ]] && mv "$APP.old" "$APP" 2>/dev/null; then back="${back:+$back, }engine"; fi
    fi
    if [[ $VENV_ASIDE -eq 1 && -d "$VENV.old" ]]; then
        if [[ "${1:-0}" == 1 && $VENV_BUSY -eq 1 ]]; then
            warn "runtime   stopped while it was rebuilt: the previous one waits in $(tl "$VENV.old"); run the installer again"
        else
            rm -rf "$VENV" 2>/dev/null || true
            if [[ ! -e "$VENV" ]] && mv "$VENV.old" "$VENV" 2>/dev/null; then back="${back:+$back, }runtime"; fi
        fi
    fi
    PLACED=0; VENV_ASIDE=0; LINK_PREV=""
    if [[ -n "$back" ]]; then warn "restored  what this run replaced ($back): the previous install works as before"; fi
    return 0
}

self_test() {
    # -I: neither the current folder nor PYTHONPATH can stand in for the engine being tested.
    "$VENV/bin/python" -I -B - "$APP" <<'PY'
import os, sys
app = os.path.realpath(sys.argv[1])
sys.path.insert(0, app)
import pulsar
assert os.path.realpath(pulsar.__file__).startswith(app + os.sep), pulsar.__file__
from pulsar import translate, responses, providers, catalog, launch, cli, plugin, redact  # noqa: F401
from pulsar.core import server, engine, spawn, state  # noqa: F401
from pulsar.mcp import server as mcp_server  # noqa: F401
req = translate.build_request({"messages": [{"role": "user", "content": "hi"}], "max_tokens": 10}, "m", "openai", stream=True)
assert req["messages"][-1]["content"] == "hi"
assert providers.detect("nvapi-abc123").best.id == "nvidia"
assert catalog.parse_models(providers.get("openai"), "openai", {"data": [{"id": "x\x1b]52;c;e\x07"}]}) == []
assert not providers.valid_model_id("x';touch${IFS}/tmp/p;'")
assert redact.redact("key sk-or-v1-0123456789abcdef") != "key sk-or-v1-0123456789abcdef"
argv = pulsar.python_argv("version")
assert "-c" in argv and argv[argv.index("-c") + 1] == pulsar.BOOT, argv
hook = plugin.safe_argv("statusline")
assert {"-E", "-I"} & set(hook[1:hook.index("-c")]), hook
PY
}

main() {
    local a
    ROLL_BACK=1                                # an install run: what it replaces comes back if it stops early
    for a in "$@"; do
        case "$a" in
            --local) WANT_LOCAL=1 ;;
            *) die "unknown option $a (only --local)" ;;
        esac
    done
    banner
    check_ref
    detect_platform
    get_source
    prepare_home
    enter_home                                 # from here on, work from inside PULSAR_HOME
    check_bin                                  # before the home changes: refuse another install's command
    setup_python
    place_app
    setup_claude
    link_bin
    run "self-test engine, Core, translators, MCP" self_test || die "self-test failed"
    commit_install                             # verified: only now does what it replaced go
    [[ -n "$TMP_SRC" ]] && rm -rf "$TMP_SRC"
    TMP_SRC=""
    rm -f "$LOG"

    printf '\n  %s%s%s Pulsar Code is installed.%s\n' "$BEAM" "$B" "$G_MARK" "$R"
    local shown="$BIN_DIR"; [[ "$shown" == "$HOME"* ]] && shown="\$HOME${shown#"$HOME"}"
    if [[ "${PATH_HINT:-}" == "1" ]]; then
        say "New terminals have \`pulsarcode\` on PATH. For this one:  export PATH=\"$shown:\$PATH\""
    elif [[ "${PATH_HINT:-}" == "2" ]]; then
        say "Once that line is in place, new terminals have \`pulsarcode\`. For this one:  export PATH=\"$shown:\$PATH\""
    fi
    if [[ "${PULSAR_NO_LAUNCH:-0}" != "1" ]] && { : </dev/tty; } 2>/dev/null && [[ -t 1 ]]; then
        printf '  %sLaunching. Paste any API key when asked.%s\n\n' "$ASH" "$R"
        nap 0.6
        # Your session, not the installer's: the folder you started from and the PATH you gave.
        if [[ -z "$START_DIR" ]] || ! cd "$START_DIR" 2>/dev/null; then cd "$HOME" 2>/dev/null || true; fi
        PATH="$ORIG_PATH" exec "$BIN_DIR/pulsarcode" </dev/tty
    fi
    printf '  Run %spulsarcode%s in any project folder.\n\n' "$B$BEAM" "$R"
}

# Sourced (the tests do this to reach single functions): define everything, run nothing.
if (return 0 2>/dev/null); then
    :
else
    main "$@"
fi
