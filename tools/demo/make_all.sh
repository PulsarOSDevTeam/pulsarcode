#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
#
# Record the README demo scenes (tools/demo/scenes/*.json): replays into assets/demo/<name>.svg, the
# site player's frames into docs/demo/<name>.json, and posters (the last frame, PNG) into
# assets/demo/posters/ and docs/demo/posters/.
#
#   tools/demo/make_all.sh                  every scene: boot uplink picker session stage top
#   tools/demo/make_all.sh picker stage     only these
#   tools/demo/make_all.sh --out DIR ...    write SVGs, JSON and posters to DIR instead (iterating)
#   tools/demo/make_all.sh --raw DIR ...    also keep each untrimmed recording as DIR/<name>.raw.json
#                                           (re-render one with scenes/fixtures/record_scene.py --from-raw)
#   tools/demo/make_all.sh --gif ...        also write GIFs for release media to $PULSAR_LAUNCH_DIR/demo
#                                           (default /tmp/pulsar-launch/demo); never the repo
#   tools/demo/make_all.sh --no-posters     skip the posters (they need Playwright's Chromium)
#   tools/demo/make_all.sh --keep ...       keep the scratch folder (homes, logs) and print its path
#
# Every scene runs under `env -i` with its own scratch HOME and PULSAR_HOME, a local scripted
# upstream (tools/demo/scenes/fixtures/upstream.py) and obviously fake keys. Your ~/.claude,
# ~/.pulsarcode and shell credentials are never read, and nothing leaves the machine: the upstream
# is the only proxy the scenes see and it refuses every host but the demo's own. Every process
# started here is stopped by PID on exit, also after a failure or Ctrl-C. Running it again
# overwrites the outputs and nothing else.
#
# Every scene runs on a pinned wall clock (scenes/fixtures/pinclock.c, built with cc): Claude Code, Core,
# the viewers and the fixtures share one clock that reads 09:41:00 UTC when the scene starts, so no
# replay shows when it was recorded and reruns show the same times (record_scene.py fails a scene
# whose frames carry any other time of day). Core listens on its default port (4747) when it is free,
# so the Observatory shows the same address on every run. A scene that fails is recorded once more
# before the run gives up on it.
#
# The launcher runs on scenes/fixtures/pinned_python, which pins the boot screen's HARDWARE row to an
# illustrative "Demo machine", so no asset carries the recording machine's CPU, RAM or GPU.
#
# Needs: PULSAR_PYTHON, a Python with Pulsar Code's runtime deps (requirements.txt: starlette uvicorn httpx);
# DEMO_PYTHON with pyte and pillow, and playwright for the posters (default: PULSAR_PYTHON); openssl; cc;
# and Claude Code (`claude`) for the session scene. DEMO_PATH replaces the PATH the scenes see
# (default: the folders that hold claude, node and python3, then /usr/local/bin:/usr/bin:/bin).

set -euo pipefail
unset CDPATH

REPO=$(cd -P "$(dirname "$0")/../.." && pwd -P)
SCENES=$REPO/tools/demo/scenes
FIX=$SCENES/fixtures
OUT=$REPO/assets/demo
JSON_OUT=$REPO/docs/demo
POSTERS=1
GIF=0
RAW=""
KEEP=0
ALL="boot uplink picker session stage top"
WANT=""

while [ $# -gt 0 ]; do
    case $1 in
        --out) OUT=$2; JSON_OUT=$2; shift 2 ;;
        --out=*) OUT=${1#--out=}; JSON_OUT=$OUT; shift ;;
        --raw) RAW=$2; shift 2 ;;
        --raw=*) RAW=${1#--raw=}; shift ;;
        --gif) GIF=1; shift ;;
        --no-posters) POSTERS=0; shift ;;
        --keep) KEEP=1; shift ;;
        -h|--help) sed -n '5,36p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "make_all.sh: unknown option $1" >&2; exit 2 ;;
        *) case " $ALL " in
               *" $1 "*) WANT="$WANT $1"; shift ;;
               *) echo "make_all.sh: unknown scene $1 (scenes: $ALL)" >&2; exit 2 ;;
           esac ;;
    esac
done
WANT=${WANT:-$ALL}
mkdir -p "$OUT" "$JSON_OUT"
OUT=$(cd -P "$OUT" && pwd -P)
JSON_OUT=$(cd -P "$JSON_OUT" && pwd -P)
if [ -n "$RAW" ]; then mkdir -p "$RAW"; RAW=$(cd -P "$RAW" && pwd -P); fi

# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

has_mods() { "$1" -c "import $2" >/dev/null 2>&1; }
RUNTIME_MODS="starlette, uvicorn, httpx"     # requirements.txt (Core uses Starlette, not FastAPI)

if [ -z "${PULSAR_PYTHON:-}" ]; then
    for cand in "$REPO/.venv/bin/python" "$(command -v python3 || true)"; do
        if [ -n "$cand" ] && [ -x "$cand" ] && has_mods "$cand" "$RUNTIME_MODS"; then
            PULSAR_PYTHON=$cand; break
        fi
    done
fi
if [ -z "${PULSAR_PYTHON:-}" ] || ! has_mods "$PULSAR_PYTHON" "$RUNTIME_MODS"; then
    echo "make_all.sh: set PULSAR_PYTHON to a Python with Pulsar Code's deps:" >&2
    echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pyte pillow" >&2
    exit 2
fi
DEMO_PYTHON=${DEMO_PYTHON:-$PULSAR_PYTHON}
if ! has_mods "$DEMO_PYTHON" "pyte, PIL"; then
    echo "make_all.sh: DEMO_PYTHON ($DEMO_PYTHON) needs pyte and pillow: pip install pyte pillow" >&2
    exit 2
fi
command -v openssl >/dev/null 2>&1 || { echo "make_all.sh: openssl is required" >&2; exit 2; }
command -v cc >/dev/null 2>&1 || { echo "make_all.sh: a C compiler (cc) is required to pin the clock" >&2; exit 2; }
PY_EXE=$("$PULSAR_PYTHON" -c 'import os, sys; print(os.path.basename(os.path.realpath(sys.executable)))')

if [ -z "${DEMO_PATH:-}" ]; then
    DEMO_PATH=""
    for tool in claude node python3; do
        t=$(command -v "$tool" 2>/dev/null || true)
        [ -n "$t" ] || continue
        dir=$(dirname "$t")
        case ":$DEMO_PATH:" in *":$dir:"*) ;; *) DEMO_PATH=${DEMO_PATH:+$DEMO_PATH:}$dir ;; esac
    done
    DEMO_PATH=${DEMO_PATH:+$DEMO_PATH:}/usr/local/bin:/usr/bin:/bin
fi

SCRATCH=$(mktemp -d "${TMPDIR:-/tmp}/pulsar-demo.XXXXXX")
RUN_ID=demo-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')   # marks every process of this run
PIDS=""
FAILED=""
SCENE_DIR=""
CORE_PORT=""
UP_PORT=""
PROXY_ENV=()
PIN_ENV=()
CORE_DEFAULT_PORT=4747                   # pulsar/core/state.py DEFAULT_CORE_PORT

track() { PIDS="$PIDS $1"; }

alive() { kill -0 "$1" 2>/dev/null; }

stop_pid() {          # stop_pid PID: TERM, then KILL after a grace period
    local pid=$1 i
    alive "$pid" || return 0
    kill "$pid" 2>/dev/null || true
    for i in 1 2 3 4 5 6 7 8 9 10; do alive "$pid" || return 0; sleep 0.2; done
    kill -9 "$pid" 2>/dev/null || true
}

marked_pids() {       # every live process whose environment carries this run's DEMO_RUN marker
    [ -d /proc ] || return 0
    local f pid
    for f in /proc/[0-9]*/environ; do
        pid=${f#/proc/}; pid=${pid%/environ}
        [ "$pid" = "$$" ] && continue
        if { tr '\0' '\n' <"$f"; } 2>/dev/null | grep -qx "DEMO_RUN=$RUN_ID"; then echo "$pid"; fi
    done
}

core_pid() {          # core_pid SCENE_DIR: the pid in that home's run/core.json, if any
    local f=$1/home/.pulsarcode/run/core.json
    [ -f "$f" ] || return 0
    "$PULSAR_PYTHON" -c 'import json,sys; print(int(json.load(open(sys.argv[1])).get("pid") or 0))' "$f" \
        2>/dev/null || true
}

teardown_scene() {    # teardown_scene SCENE_DIR: traffic, upstream, Core, holders, anything else of this run
    local d=$1 pid
    for pid in $PIDS; do stop_pid "$pid"; done
    PIDS=""
    pid=$(core_pid "$d")
    if [ -n "$pid" ] && [ "$pid" != 0 ]; then stop_pid "$pid"; fi
    if [ -f "$d/state.json" ]; then
        for pid in $("$PULSAR_PYTHON" -c 'import json,sys
d = json.load(open(sys.argv[1]))
print(" ".join(str(p) for p in [d.get("core_pid", 0)] + [s["holder"] for s in d.get("sessions", [])] if p))' \
            "$d/state.json" 2>/dev/null || true); do
            stop_pid "$pid"
        done
    fi
    for pid in $(marked_pids); do stop_pid "$pid"; done
}

cleanup() {
    local rc=$?
    trap - EXIT INT TERM
    local d
    for d in "$SCRATCH"/*/; do [ -d "$d" ] && teardown_scene "${d%/}"; done
    for pid in $(marked_pids); do stop_pid "$pid"; done
    if [ "$KEEP" = 1 ]; then
        echo "scratch kept: $SCRATCH"
    else
        rm -rf "$SCRATCH"
    fi
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

free_port() {
    "$PULSAR_PYTHON" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'
}

core_port() {         # Core's default port when nothing listens there (same address in every replay)
    if "$PULSAR_PYTHON" -c 'import socket, sys
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)    # as Core binds: a previous scene in TIME_WAIT is free
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)' "$CORE_DEFAULT_PORT" 2>/dev/null; then
        echo "$CORE_DEFAULT_PORT"
    else
        echo "make_all.sh: port $CORE_DEFAULT_PORT is busy: this run's replays show another Core port" >&2
        free_port
    fi
}

wait_file() {         # wait_file PATH SECONDS
    local i=0
    while [ ! -s "$1" ]; do
        i=$((i + 1))
        [ "$i" -gt $(($2 * 10)) ] && return 1
        sleep 0.1
    done
}

# in_env SCENE_DIR [VAR=value ...] COMMAND ...: a clean environment, nothing inherited from this shell
in_env() {
    local d=$1; shift
    env -i HOME="$d/home" PATH="$DEMO_PATH" LANG=C.UTF-8 TERM=xterm-256color \
        PULSAR_HOME="$d/home/.pulsarcode" PULSAR_PORT="$CORE_PORT" \
        PULSAR_PYTHON="$FIX/pinned_python" DEMO_REAL_PYTHON="$PULSAR_PYTHON" \
        NO_PROXY=127.0.0.1,localhost,::1 no_proxy=127.0.0.1,localhost,::1 \
        DEMO_RUN="$RUN_ID" DEMO_REPO="$REPO" DEMO_FIX="$FIX" DEMO_STATE="$d/state.json" \
        ${PIN_ENV[@]+"${PIN_ENV[@]}"} "$@"
}

new_scene() {         # new_scene NAME: sets SCENE_DIR (a fresh scratch folder), CORE_PORT; no upstream yet
    SCENE_DIR=$SCRATCH/$1
    rm -rf "$SCENE_DIR"
    mkdir -p "$SCENE_DIR/home"
    chmod 700 "$SCENE_DIR/home"
    CORE_PORT=$(core_port)
    UP_PORT=""
    PROXY_ENV=()
    # one pinned clock for every process of the scene: 09:41:00 UTC (tomorrow) now, at the real rate
    PIN_ENV=(LD_PRELOAD="$SCRATCH/pinclock.so" DEMO_PIN_EXE="claude,$PY_EXE"
             DEMO_PIN_ORIGIN="$(date +%s)" DEMO_PIN_EPOCH="$(( ($(date +%s) / 86400 + 1) * 86400 + 9 * 3600 + 41 * 60 ))")
}

# start_upstream SCENE_DIR MODE [PROJECT]: the scripted upstream, then PROXY_ENV sends every outbound
# HTTP(S) request of the scene to it (and trusts its throwaway CA in Pulsar only)
start_upstream() {
    local d=$1 mode=$2 proj=${3:-}
    UP_PORT=$(free_port)
    in_env "$d" "$PULSAR_PYTHON" -E -B "$FIX/upstream.py" --port "$UP_PORT" --state "$d/upstream" \
        --mode "$mode" ${proj:+--project "$proj"} >"$d/upstream.log" 2>&1 &
    track $!
    if ! wait_file "$d/upstream/upstream.ready" 20; then
        echo "make_all.sh: the demo upstream did not start:" >&2
        cat "$d/upstream.log" >&2
        return 1
    fi
    PROXY_ENV=("HTTPS_PROXY=http://127.0.0.1:$UP_PORT" "https_proxy=http://127.0.0.1:$UP_PORT"
               "HTTP_PROXY=http://127.0.0.1:$UP_PORT" "http_proxy=http://127.0.0.1:$UP_PORT"
               "PULSAR_CA_BUNDLE=$d/upstream/ca.pem")
}

seed() {              # seed SCENE_DIR ARGS...: tools/demo/scenes/fixtures/seed.py in the scene's home
    local d=$1; shift
    in_env "$d" ${PROXY_ENV[@]+"${PROXY_ENV[@]}"} "$PULSAR_PYTHON" -E -B "$FIX/seed.py" "$@"
}

record() {            # record SCENE_DIR NAME [VAR=value ...]
    local d=$1 name=$2; shift 2
    echo "== $name"
    local extra=(--json-out "$JSON_OUT/$name.json")
    [ -n "$RAW" ] && extra+=(--raw-out "$RAW/$name.raw.json")
    [ "$GIF" = 1 ] && extra+=(--gif)
    if ! (cd "$REPO" && in_env "$d" ${PROXY_ENV[@]+"${PROXY_ENV[@]}"} \
            PULSAR_LAUNCH_DIR="${PULSAR_LAUNCH_DIR:-/tmp/pulsar-launch}" "$@" \
            "$DEMO_PYTHON" -B "$FIX/record_scene.py" "$SCENES/$name.json" "$OUT/$name" "${extra[@]}"); then
        FAILED="$FAILED $name"
    fi
}

if ! cc -shared -fPIC -O2 -o "$SCRATCH/pinclock.so" "$FIX/pinclock.c" -ldl; then
    echo "make_all.sh: could not build scenes/fixtures/pinclock.c" >&2
    exit 2
fi

sid_of() { "$PULSAR_PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["sessions"][int(sys.argv[2])]["sid"])' "$1/state.json" "$2"; }

# ---------------------------------------------------------------------------
# scenes
# ---------------------------------------------------------------------------

scene_boot() {        # the boot sequence, as `pulsarcode demo` replays it
    new_scene boot; local d=$SCENE_DIR
    record "$d" boot
}

scene_uplink() {      # paste a fake OpenRouter-shaped key: detection, handshake, LINK ESTABLISHED
    new_scene uplink; local d=$SCENE_DIR
    start_upstream "$d" agent
    record "$d" uplink
}

scene_picker() {      # the model picker over seeded catalogs: search "kimi", move, Enter
    new_scene picker; local d=$SCENE_DIR
    start_upstream "$d" agent
    seed "$d" home --profiles openrouter,groq,nvidia --catalog --model openrouter:z-ai/glm-5.3 \
        --recent groq:openai/gpt-oss-120b --launches 4
    record "$d" picker
}

scene_session() {     # Claude Code itself, launched through Pulsar Core, answering with a tool call
    new_scene session; local d=$SCENE_DIR
    local proj=$d/home/code/orbit                # under HOME, so Claude Code shows ~/code/orbit
    mkdir -p "$proj/orbit"
    cp "$FIX/orbit/README.md" "$proj/README.md"
    cp "$FIX/orbit/cli.py" "$proj/orbit/cli.py"
    start_upstream "$d" agent "$proj"
    seed "$d" home --profiles openrouter,groq --catalog --model openrouter:moonshotai/kimi-k3 --launches 3 \
        --trust "$proj"
    record "$d" session DEMO_PROJECT="$proj"      # the pinned clock: "done 9:41 AM" on every run
}

scene_stage() {       # the Stage viewer receiving a scripted sample deck (scenes/fixtures/stage_scenes.json)
    new_scene stage; local d=$SCENE_DIR
    start_upstream "$d" agent
    seed "$d" home --profiles openrouter --model openrouter:moonshotai/kimi-k3 --launches 3
    seed "$d" core --out "$d/state.json" --session "orbit:/home/dev/orbit:openrouter/moonshotai/kimi-k3"
    record "$d" stage DEMO_SID="$(sid_of "$d" 0)"
}

scene_top() {         # the Observatory over three sessions of synthetic traffic: relays, a breaker, a Stage post
    new_scene top; local d=$SCENE_DIR
    start_upstream "$d" traffic
    seed "$d" home --profiles openrouter,groq,nvidia,cerebras --model openrouter:moonshotai/kimi-k3 --launches 3
    seed "$d" core --out "$d/state.json" \
        --session "orbit:/home/dev/orbit:nvidia/moonshotai/kimi-k3>openrouter/moonshotai/kimi-k3" \
        --session "api-gateway:/home/dev/api-gateway:groq/openai/gpt-oss-120b>cerebras/gpt-oss-120b" \
        --session "docs:/home/dev/docs:openrouter/z-ai/glm-5.3"
    # 30 s of history first, so the throughput and latency charts have a shape; the Stage post
    # lands a few seconds into the recording, so the feed shows it arriving
    in_env "$d" "$PULSAR_PYTHON" -E -B "$FIX/traffic.py" "$d/state.json" 55 34 >"$d/traffic.log" 2>&1 &
    track $!
    sleep 30
    record "$d" top
}

for name in $WANT; do
    "scene_$name"
    teardown_scene "$SCENE_DIR"
    case " $FAILED " in
        *" $name "*)          # a live recording can lose a race once: record it again before giving up
            echo "make_all.sh: $name did not record cleanly, recording it once more" >&2
            rest=""
            for f in $FAILED; do [ "$f" = "$name" ] || rest="$rest $f"; done
            FAILED=$rest
            "scene_$name"
            teardown_scene "$SCENE_DIR" ;;
    esac
done

RECORDED=""
for name in $WANT; do case " $FAILED " in *" $name "*) ;; *) RECORDED="$RECORDED $name" ;; esac; done
if [ "$POSTERS" = 1 ] && [ -n "$RECORDED" ]; then
    # the last frame of each replay, rendered by Chromium from the SVG itself (reduced motion)
    POSTER_COPY=()
    [ "$OUT" = "$REPO/assets/demo" ] && POSTER_COPY=(--copy "$REPO/docs/demo/posters")
    if ! env -i HOME="$SCRATCH" PATH="$DEMO_PATH" "$DEMO_PYTHON" -B "$REPO/tools/demo/posters.py" \
            --src "$OUT" --out "$OUT/posters" ${POSTER_COPY[@]+"${POSTER_COPY[@]}"} $RECORDED; then
        FAILED="$FAILED posters"
    fi
fi
if [ -f "$REPO/tools/brand/check.py" ]; then
    # the brand validator (copy rules, key shapes, counts, budgets) over the replays, wherever they went
    if ! env -i HOME="$SCRATCH" PATH="$DEMO_PATH" "$DEMO_PYTHON" -B "$REPO/tools/brand/check.py" "$OUT"; then
        FAILED="$FAILED check.py"
    fi
fi

if [ -n "$FAILED" ]; then
    echo "make_all.sh: these scenes did not record cleanly:$FAILED (rerun with --keep to inspect)" >&2
    exit 1
fi
echo "done: $OUT (frames: $JSON_OUT)"
