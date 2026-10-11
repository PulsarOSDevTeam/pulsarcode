# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""The Claude Code plugin + settings Pulsar Code loads into every session.

Loaded per launch with `claude --plugin-dir <dir> --settings <file>`, so it
never edits anyone's Claude configuration and works with the isolated
profile and with PULSAR_CLAUDE_CONFIG=shared alike.

  plugin/<version>-<hash>/            immutable: written once, never edited while a session uses it
    .claude-plugin/plugin.json        manifest
    .mcp.json                         the `pulsar` MCP server (Stage, Council, status, Pilot)
    commands/*.md                     /pulsar:status  /pulsar:switch  /pulsar:stage  /pulsar:council
    hooks/hooks.json                  attention + done pings (terminal notification, bell, haptics)
  run/sessions/<sid>/settings.json    per session (0600): status line, spinner verbs, pinned env,
                                      model picker; settings.json in the home when there is no session

Two launches with different models therefore never overwrite each other's settings, and a running
session's plugin files never change under it. The folder name hashes the content (the interpreter,
the app path, PULSAR_NOTIFY), so every variant gets its own copy; copies nobody used for two days are
removed. The in-terminal Stage pane (pulsar/fnplug, a function-hooks plugin) is a second
`--plugin-dir` when Claude Code supports it (pulsar.stage.pane.supported).
"""

from __future__ import annotations

import codecs
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, NamedTuple, Optional, Tuple

from pulsar import __version__, python_argv
from pulsar import config as K
from pulsar import providers as P

PKG_ROOT = str(Path(__file__).resolve().parent.parent)

SPINNER_VERBS = ["Pulsing", "Warping", "Igniting", "Orbiting", "Beaming", "Decrypting", "Calibrating",
                 "Transmitting", "Resonating", "Spinning up", "Aligning beams", "Charging core", "Folding space",
                 "Synthesizing", "Triangulating", "Channeling"]


def safe_argv(*args: str, module: str = "pulsar") -> List[str]:
    """python_argv plus -E: PYTHONPATH, PYTHONHOME and friends are ignored.

    Claude Code hands its environment to hooks, the status line and MCP servers. A PYTHONPATH with an
    empty or relative element (`export PYTHONPATH=$PYTHONPATH:/x` when it was unset) puts the project
    folder on sys.path before the bootstrap runs, so its sitecustomize.py or a file shadowing the standard
    library would run on every refresh. -E keeps Python from reading those variables at all.
    """
    argv = list(python_argv(*args, module=module))
    head = argv[1:argv.index("-c")] if "-c" in argv else argv[1:2]
    if "-E" not in head and "-I" not in head:
        argv.insert(1, "-E")
    return argv


def _py_cmd(*args: str, module: str = "pulsar") -> str:
    """A shell command line running `pulsarcode <args>` (or `module` directly) with our interpreter.

    Never imports from the CWD. The status line and hooks name their own module so a refresh does not
    import the whole CLI.
    """
    argv = safe_argv(*args, module=module)
    if os.name == "nt":
        q = lambda s: '"' + s.replace('"', '\\"') + '"'  # noqa: E731
        return " ".join(q(a) for a in argv)
    return " ".join(shlex.quote(a) for a in argv)


COMMANDS: Dict[str, str] = {
    "status.md": """---
description: Pulsar Code status. The live model, relay chain, health and token usage.
---

Call the `pulsar_status` tool and show its result to the user as a compact summary. If the tool is unavailable, say that this session was not started with `pulsarcode`.
""",
    "switch.md": """---
description: Switch this session to another model live (takes effect on the next message).
argument-hint: <model id or provider:model>
allowed-tools: mcp__plugin_pulsar_pulsar__pulsar_switch
---

Call the `pulsar_switch` tool with `model` set to exactly this text (or with no arguments if it is empty, which lists the models): $ARGUMENTS

Report the result in one line. The tool validates the id and only moves this session.
""",
    "stage.md": """---
description: Present the current topic on Pulsar Stage (animated slides in a split pane or browser).
argument-hint: [topic]
---

Use the `stage_present` tool to present $ARGUMENTS (or, if empty, a concise visual summary of what we have been working on) as a deck of 4 to 8 slides. Prefer diagrams for architecture, charts for numbers, code panels for key snippets and a progress scene for plans. Keep each slide short.
""",
    "council.md": """---
description: Ask the other connected models for second opinions on a question.
argument-hint: <question>
allowed-tools: mcp__plugin_pulsar_pulsar__council
---

Call the `council` tool with this prompt and then synthesize the answers for the user, noting where the models agree and disagree:

$ARGUMENTS
""",
}


PLUGIN_KEEP_SECONDS = 2 * 86400           # a plugin copy nobody launched with for this long may go
_PLUGIN_DIR = re.compile(r"^[A-Za-z0-9._+-]{1,40}-[0-9a-f]{12}$")
_LEGACY_PLUGIN_FILES = (".claude-plugin", ".mcp.json", "commands", "hooks")   # the flat layout before 3.0.0
PLUGIN_RECORD = "plugin-dir"                 # run/sessions/<sid>/plugin-dir: the copy that session loaded


def plugin_files() -> Dict[str, str]:
    """Every file of the plugin, relative path -> text (deterministic for one install and environment)."""
    files: Dict[str, str] = {
        ".claude-plugin/plugin.json": json.dumps({
            "name": "pulsar",
            "version": __version__,
            "description": "Pulsar Code: Stage presentations, model Council, live status and model switching.",
            "author": {"name": "PulsarOS Intelligence Inc.", "url": "https://pulsaros.ca"},
            "homepage": "https://github.com/PulsarOSDevTeam/pulsarcode",
            "license": "AGPL-3.0-or-later",
        }, indent=2) + "\n",
        ".mcp.json": json.dumps({"mcpServers": {"pulsar": {
            "command": safe_argv("mcp")[0],
            "args": safe_argv("mcp")[1:],
            # PYTHONSAFEPATH: Python 3.11+ never puts the project directory on sys.path, even in
            # any helper process the server starts (the bootstrap already covers the server itself).
            "env": {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONSAFEPATH": "1"},
        }}}, indent=2) + "\n",
        "hooks/hooks.json": json.dumps({"hooks": _hooks()}, indent=2) + "\n",
    }
    for name, body in COMMANDS.items():
        files["commands/" + name] = body
    return files


def _plugin_name(files: Dict[str, str]) -> str:
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    version = re.sub(r"[^A-Za-z0-9._+-]", "_", __version__)[:40] or "v"
    return "%s-%s" % (version, digest)


def _plugin_complete(root: Path, files: Dict[str, str]) -> bool:
    try:
        return root.is_dir() and not root.is_symlink() and all(
            (root / rel).is_file() and (root / rel).read_text(encoding="utf-8") == text for rel, text in files.items())
    except (OSError, UnicodeDecodeError):
        return False


def _write_tree(root: Path, files: Dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            if p.is_file() and p.read_text(encoding="utf-8") == text:
                continue
        except (OSError, UnicodeDecodeError):
            pass
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix="." + p.name + ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
            os.replace(tmp, str(p))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def build_plugin() -> Path:
    """The plugin folder for `--plugin-dir`: plugin/<version>-<content hash>, written once. Returns its path.

    A new copy is assembled in a temporary folder and renamed into place, so a concurrent launch either
    finds a complete copy or builds its own; a copy a session loaded is never rewritten (any other
    content gets another folder). Raises HomeError for a PULSAR_HOME that is not Pulsar's.
    """
    base = K.ensure_home() / "plugin"
    files = plugin_files()
    root = base / _plugin_name(files)
    if not _plugin_complete(root, files):
        try:
            base.mkdir(parents=True, exist_ok=True)
            tmp = Path(tempfile.mkdtemp(dir=str(base), prefix=".build-"))
            try:
                _write_tree(tmp, files)
                try:
                    os.rename(str(tmp), str(root))
                except OSError:
                    # A peer won the race (or a damaged copy is in the way): never rename over it.
                    if not _plugin_complete(root, files):
                        _write_tree(root, files)      # same bytes as every reader expects
            finally:
                if tmp.exists():
                    shutil.rmtree(str(tmp), ignore_errors=True)
        except OSError:
            pass                                       # Claude Code reports a broken plugin itself
    try:
        os.utime(str(root), None)                      # "last used": keeps it from being collected
    except OSError:
        pass
    collect_plugins(base, keep=root)
    return root


def _plugins_in_use() -> set:
    """Plugin folder names recorded by live sessions (run/sessions/<sid>/plugin-dir)."""
    names = set()
    try:
        from pulsar.core import state as ST
        for s in ST.live_sessions(reap=False):
            rec = ST.session_path(s.sid).with_suffix("") / PLUGIN_RECORD
            try:
                names.add(Path(rec.read_text(encoding="utf-8").strip()).name)
            except OSError:
                names.add("*")                         # a live session from an older build: keep everything
    except Exception:
        names.add("*")
    return names


def collect_plugins(base: Path, keep: Path, now: Optional[float] = None) -> List[str]:
    """Remove plugin copies nobody uses: not `keep`, not loaded by a live session, unused for two days.

    The flat pre-3.0.0 layout goes once no session is running. Returns what was removed.
    """
    now = time.time() if now is None else now
    try:
        entries = list(base.iterdir())
    except OSError:
        return []
    removed: List[str] = []
    in_use: Optional[set] = None
    for p in entries:
        name = p.name
        try:
            if name.startswith(".build-"):
                if now - p.stat().st_mtime > 3600:
                    shutil.rmtree(str(p), ignore_errors=True)
                    removed.append(name)
                continue
            if name == keep.name or p.is_symlink():
                continue
            legacy = name in _LEGACY_PLUGIN_FILES
            if not legacy and not (_PLUGIN_DIR.match(name) and p.is_dir()):
                continue
            if not legacy and now - p.stat().st_mtime < PLUGIN_KEEP_SECONDS:
                continue
            if in_use is None:
                in_use = _plugins_in_use()
            if "*" in in_use or name in in_use or (legacy and in_use):
                continue
            if p.is_dir():
                shutil.rmtree(str(p), ignore_errors=True)
            else:
                p.unlink()
            removed.append(name)
        except OSError:
            continue
    return removed


def _hooks() -> Dict[str, List[Dict[str, Any]]]:
    def hook(event: str) -> Dict[str, Any]:
        return {"type": "command", "command": _py_cmd(event, module="pulsar.notify"), "timeout": 10}
    hooks: Dict[str, List[Dict[str, Any]]] = {"SessionStart": [{"hooks": [hook("session-start")]}]}
    if os.environ.get("PULSAR_NOTIFY", "on").strip().lower() not in ("0", "off", "none", "false", "no"):
        hooks["Notification"] = [{"hooks": [hook("attention")]}]
        hooks["Stop"] = [{"hooks": [hook("done")]}]
    return hooks


# Pre-approved: tools that stay on this machine. council sends the question to your other connected
# providers, so Claude Code asks first unless you typed /pulsar:council (that command allows it).
PULSAR_TOOLS = ("stage_present", "stage_show", "stage_clear", "pulsar_status")
# behavesAs: the Claude model whose request profile Claude Code applies to a model it does not know. It also
# keeps auto mode off for these models (Claude Code supports auto mode from claude-opus-4-6 on, and its
# classifier would run on the custom model and block every Bash call), and it fixes Claude Code's context
# window at that model's 200k; pulsar.launch (CLAUDE_WINDOW) explains how Pulsar sets the real window anyway.
BEHAVES_AS = os.environ.get("PULSAR_BEHAVES_AS") or "claude-sonnet-4-5"

THEME = {
    "name": "Pulsar",
    "base": "dark",
    "overrides": {
        "claude": "#9D4DFF", "claudeShimmer": "#36E2FF", "clawd_body": "#9D4DFF",
        "promptBorder": "#5b3fa8", "promptBorderShimmer": "#36E2FF",
        "userMessageBackground": "rgb(24,16,40)", "permission": "#FF2E88", "suggestion": "#36E2FF",
        "remember": "#FF6A00", "success": "#50FAAA", "warning": "#FFC400", "error": "#FF465A",
    },
}


def _fmt_ctx(n: int) -> str:
    if not n:
        return ""
    return "%gM ctx" % round(n / 1_000_000, 1) if n >= 1_000_000 else "%dK ctx" % round(n / 1000)


def model_picker(options: List[Dict[str, Any]], replace: bool = True, long_ok: bool = True) -> Dict[str, Any]:
    """Branded rows for Claude Code's /model picker.

    options: [{"model", "provider", "context", "tags"}]; behavesAs gives Claude Code a known request profile.
    A row for a window of 1M or more gets the "[1m]" id (pulsar.launch.claude_model_id), so Claude Code
    uses a 1M window after /model too; long_ok=False where Claude Code talks to the provider itself.
    """
    from pulsar import launch as L
    rows = []
    seen = set()
    for o in options:
        mid = str(o.get("model") or "")
        if not mid or mid in seen or not P.valid_model_id(mid):
            continue
        seen.add(mid)
        try:
            context = int(o.get("context") or 0)
        except (TypeError, ValueError):
            context = 0
        tags = [t for t in (o.get("tags") or []) if t in ("free", "reasoning", "local")]
        desc = " · ".join(x for x in (P.clean_text(o.get("provider", ""), 40), _fmt_ctx(context),
                                      " ".join(tags)) if x)
        rows.append({"model": L.claude_model_id(mid, context, long_ok), "label": "◉ " + mid.split("/")[-1],
                     "description": desc[:80], "behavesAs": BEHAVES_AS})
        if len(rows) >= 24:
            break
    return {"replaceBuiltInOptions": replace, "options": rows}


def settings(model_label: str = "", picker: Optional[List[Dict[str, Any]]] = None, anthropic: bool = False,
             isolated: bool = True) -> Dict[str, Any]:
    model_label = P.clean_text(model_label, 120)   # announced on screen and written into commit trailers
    s: Dict[str, Any] = {}
    if os.environ.get("PULSAR_STATUSLINE", "on").lower() not in ("0", "off", "none"):
        try:
            interval = int(os.environ.get("PULSAR_STATUSLINE_INTERVAL", "1") or 1)
        except ValueError:
            interval = 1
        # Its own module: a refresh every second must not import the whole CLI (see statusline.py).
        s["statusLine"] = {"type": "command", "command": _py_cmd(module="pulsar.statusline"), "padding": 0,
                           "refreshInterval": max(1, min(interval, 3600))}
    if os.environ.get("PULSAR_SPINNER", "on").lower() not in ("0", "off", "none"):
        s["spinnerVerbs"] = {"mode": "replace", "verbs": SPINNER_VERBS}
    # No companyAnnouncements: the SessionStart hook (pulsar.notify) prints the one banner, with the model
    # Core really routes to, its provider and the relay chain.
    s["permissions"] = {"allow": ["mcp__plugin_pulsar_pulsar__" + t for t in PULSAR_TOOLS]}
    deny = _deny_rules()
    if deny:
        s["permissions"]["deny"] = deny
    if picker and os.environ.get("PULSAR_MODEL_PICKER", "on").lower() not in ("0", "off", "none"):
        # PULSAR_DIRECT=1: Claude Code may talk to a native provider itself, which must not get the 1M beta.
        s["modelPicker"] = model_picker(picker, replace=not anthropic,
                                        long_ok=not anthropic and os.environ.get("PULSAR_DIRECT") != "1")
    if isolated and os.environ.get("PULSAR_THEME", "on").lower() not in ("0", "off", "none"):
        s["theme"] = "custom:pulsar"
    # Commit and PR attribution in your repositories is off unless PULSAR_ATTRIBUTION=on. Off means no line
    # at all: Claude Code's own default would credit a Claude model for another model's work. Claude routes
    # keep Claude Code's own attribution.
    if not anthropic:
        on = os.environ.get("PULSAR_ATTRIBUTION", "").strip().lower() in ("1", "on", "yes", "true")
        s["attribution"] = {
            "commit": "Built with Pulsar Code (%s) https://github.com/PulsarOSDevTeam/pulsarcode" % (
                model_label or "any model") if on else "",
            "pr": "Built with [Pulsar Code](https://github.com/PulsarOSDevTeam/pulsarcode) using %s" % (
                model_label or "any model") if on else "",
        }
    if os.environ.get("PULSAR_AUTO_MODE", "on").lower() in ("0", "off", "none"):
        s["disableAutoMode"] = "disable"
    return s


# In the Pulsar home: what Claude Code's Read tool may not open (keys, the 1.x key file nim.key and the copy
# the v1 import keeps as nim.key.migrated, the admin and session tokens under run/ and in settings.json, logs,
# the profile's credentials) and what its edit tools may not change (all of that, plus what Pulsar runs or
# trusts). The isolated profile (claude_config) stays readable and editable: Claude Code keeps auto memory and
# your user-level skills there. Only its credentials and the settings that could start programs (hooks, MCP
# servers) are fenced off.
DENY_READ = ("keys.json", "nim.key", "nim.key.migrated", "config.json", "settings.json", "run/**", "logs/**",
             "claude_config/.credentials.json")
DENY_EDIT = ("keys.json", "nim.key", "nim.key.migrated", "config.json", "settings.json", "run/**", "logs/**",
             "cache/**", "plugin/**", "app/**", "bin/**", "venv/**", "claude_config/.credentials.json",
             "claude_config/.claude.json", "claude_config/settings.json")


def _rule_path(path: str, windows: Optional[bool] = None) -> str:
    """An absolute path as Claude Code's permission rules spell it: //home/me/x, or //c/Users/me/x on Windows
    (Claude Code matches Windows paths in that POSIX form). "" when it has no such spelling (UNC, relative)."""
    windows = os.name == "nt" if windows is None else windows
    if windows:
        m = re.match(r"^([A-Za-z]):[\\/]*(.*)$", path)
        if not m:
            return ""
        rest = m.group(2).replace("\\", "/").rstrip("/")
        return "//" + m.group(1).lower() + ("/" + rest if rest else "")
    if not path.startswith("/"):
        return ""
    return "/" + path.rstrip("/")


def _deny_rules() -> List[str]:
    """Keep Claude Code's own file tools away from Pulsar's secrets and session state (DENY_READ, DENY_EDIT).

    Defense in depth: provider keys never enter Claude Code's environment on the Core route, and these
    rules stop the Read/Edit tools (a prompt-injected model included) from opening keys, the admin token
    in run/core.json or session tokens, or changing what Pulsar runs. The home is named both as given and
    resolved, so a home reached through a link is covered either way.
    """
    home = K.home()
    roots: List[str] = []
    for spelled in (os.path.abspath(str(home)), str(home.resolve())):
        root = _rule_path(spelled)
        if root and root not in roots:
            roots.append(root)
    # Edit(...) covers every file-editing tool; Claude Code warns that Write(...) deny rules never match.
    return (["Read(%s/%s)" % (root, rel) for root in roots for rel in DENY_READ] +
            ["Edit(%s/%s)" % (root, rel) for root in roots for rel in DENY_EDIT])


def write_theme(claude_config: Path) -> None:
    p = claude_config / "themes" / "pulsar.json"
    text = json.dumps(THEME, indent=2) + "\n"
    try:
        if not p.exists() or p.read_text(encoding="utf-8") != text:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
    except OSError:
        pass


def settings_path(session_id: str = "") -> Path:
    """run/sessions/<sid>/settings.json for a Core session, else settings.json in the Pulsar home."""
    if session_id:
        from pulsar import launch as L
        return L.session_dir(session_id) / "settings.json"
    return K.home() / "settings.json"


# --- the session's environment, pinned -------------------------------------------------------------------
#
# Claude Code applies the `env` of every settings source to its own process after startup, and --settings
# (flag settings) outranks the project's .claude/settings.json and settings.local.json (only managed
# settings outrank it). Without these pins a repository could send the session (prompts and the pls_
# session token) to its own host with ANTHROPIC_BASE_URL, PULSAR_CORE or another provider switch, route it
# through its proxy, swap the key, turn Claude Code's network traffic back on, or arm Pilot for the MCP
# server. So the per-session settings file pins:
#
#   routing      every variable that routes, authenticates or arms the session (pinned_names(), plus any
#                key a project file sets that looks like one, _ROUTE_LIKE): the launcher's own value, ""
#                for one the launcher unset. A provider key is never written (SECRET_VARS).
#   network      proxies, CA bundle and Node options (USER_VARS, plus project keys matching _USER_LIKE):
#                the value the user's own sources give, in Claude Code's order (managed settings, the
#                user's --settings, the user-level settings.json, then the shell), never a project's.
#   no egress    the flags launch.build_env forces (launch.EGRESS_FLAGS): build_env's decision, else the
#                user's own value.
#
# plus an empty apiKeyHelper. A project setting the credential of a direct or cloud route is refused at
# launch (pin_report: swapped), and keys that look like routing or credentials but are not pinned get a
# warning of their own (pin_report: unpinned). Project files are read the way Claude Code reads them (any
# byte order mark, no size or depth limit to speak of, see read_settings_file); one that is there but cannot
# be read is never taken for "no file": refused on the direct and cloud routes, a warning on core
# (pin_report: unreadable).
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
NO_PROXY_VARS = ("NO_PROXY", "no_proxy")
USER_VARS = PROXY_VARS + NO_PROXY_VARS + ("NODE_OPTIONS", "NODE_EXTRA_CA_CERTS", "NODE_TLS_REJECT_UNAUTHORIZED",
                                          "CLAUDE_CODE_ENABLE_TELEMETRY")
PIN_ALWAYS = USER_VARS + (
    "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_MAX_CONTEXT_TOKENS", "CLAUDE_CODE_DISABLE_1M_CONTEXT",
    "PULSAR_HOME", "PULSAR_PILOT", "PULSAR_PILOT_AUTO", "PULSAR_CORE", "PULSAR_SESSION", "PULSAR_SESSION_TOKEN",
    "PULSAR_ADMIN_TOKEN", "PULSAR_ROUTE", "PULSARCODE",
)
# Credentials: pinned to "" when the launcher unset them, to a pls_ session token (loopback only, this
# session only), and otherwise left out so a provider key never lands on disk (a project that sets one of
# those is refused instead, see pin_report). The endpoint they would go to is pinned either way.
SECRET_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "AWS_BEARER_TOKEN_BEDROCK",
               "ANTHROPIC_FOUNDRY_API_KEY", "ANTHROPIC_FOUNDRY_AUTH_TOKEN", "ANTHROPIC_AWS_API_KEY",
               "PULSAR_SESSION_TOKEN", "PULSAR_ADMIN_TOKEN")
# The credential chain Claude Code itself reads on a cloud route (besides SECRET_VARS): a project changing one
# of these would make the session use someone else's account, so the launch is refused instead.
CLOUD_CREDENTIALS = {
    "bedrock": ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_CONFIG_FILE",
                "AWS_SHARED_CREDENTIALS_FILE", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN",
                "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
                "AWS_CONTAINER_AUTHORIZATION_TOKEN", "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
                "AWS_EC2_METADATA_SERVICE_ENDPOINT"),
    "vertex": ("GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_CONFIG", "GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT",
               "GOOGLE_CLOUD_QUOTA_PROJECT", "GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES", "GCE_METADATA_HOST",
               "GCE_METADATA_ROOT", "GCE_METADATA_IP", "METADATA_SERVER_DETECTION"),
}
# The PULSAR_* settings that change what code inside a session does on your machine: the MCP server, Pilot
# and its cursor overlay, Stage and its viewer, the hooks, and pulsarcode commands the model runs. Pinned to
# the launcher's value, or "" (which each of them reads as unset), so a repository cannot hide the Pilot
# cursor, pick the interpreter its overlay runs, drive another terminal, turn on sounds, trust its own CA,
# or point a nested launch elsewhere.
SESSION_VARS = (
    "PULSAR_PILOT_ARMED_AT", "PULSAR_PILOT_PYTHON", "PULSAR_PILOT_OVERLAY", "PULSAR_PILOT_OVERLAY_GEOM",
    "PULSAR_PILOT_OVERLAY_SCALE",
    "PULSAR_PILOT_MAX_W", "PULSAR_PILOT_MAX_H", "PULSAR_PILOT_X11", "PULSAR_PILOT_RESAMPLE", "PULSAR_MODEL",
    "PULSAR_MODEL_VISION", "PULSAR_FX", "PULSAR_REDUCED_MOTION", "PULSAR_STAGE_VIEWER", "PULSAR_STAGE_AUTO",
    "PULSAR_STAGE_PANE", "PULSAR_NOTIFY", "PULSAR_NOTIFY_OSC", "PULSAR_ALLOW_HTTP", "PULSAR_CA_BUNDLE",
    "PULSAR_TRUSTSTORE", "PULSAR_PROXY_LOCAL", "PULSAR_PORT", "PULSAR_RELAY", "PULSAR_DIRECT", "PULSAR_LOOPGUARD",
    "PULSAR_KEEP_ENV_KEYS", "PULSAR_CLAUDE_BIN", "PULSAR_CLAUDE_CONFIG", "PULSAR_CLAUDE_AUTOUPDATE", "PULSAR_SRC",
    "PULSAR_REF", "PULSAR_NO_LOCAL_SRC", "PULSAR_BIN_DIR", "PULSAR_MANAGED", "PULSAR_PLUGIN", "PULSAR_ATTRIBUTION",
    "PULSAR_AUTO_MODE", "PULSAR_BEHAVES_AS", "PULSAR_SKILLS", "PULSAR_SKILLS_DIRS",
)
# Looks only (colours, glyphs, the animation seed, haptic pulses): a project may set these.
COSMETIC_VARS = ("PULSAR_ASCII", "PULSAR_COLOR", "PULSAR_SEED", "PULSAR_HAPTICS")
_SESSION_TOKEN = re.compile(r"^pls_[A-Za-z0-9_\-]{8,256}$")
# Project keys pinned although no list names them: Claude Code's provider switches, base URLs and auth
# skips, today's and tomorrow's (routing), and network settings (the user's own value).
_ROUTE_LIKE = re.compile(r"^(ANTHROPIC_|CLAUDE_CODE_USE_|CLAUDE_CODE_SKIP_\w*AUTH$)|_BASE_URL$", re.I)
_USER_LIKE = re.compile(r"PROXY$|^NODE_(?!ENV$)|^AWS_ENDPOINT_URL|^OTEL_", re.I)
# Not pinned, but worth a warning of its own when a project sets it: a Claude Code variable that looks like a
# route or a credential, or the cloud SDK settings of the cloud route this session uses.
_SENSITIVE = re.compile(r"(^|_)(API_?KEY|KEY|TOKEN|SECRET|PASSWORD|PASSPHRASE|CREDENTIALS?|AUTH|CERTS?|ENDPOINT|"
                        r"URL|HOST|SOCKET|REGION|PROFILE|PROXY)(_|$)", re.I)
_CLOUD_PREFIXES = {"bedrock": ("AWS_",),
                   "vertex": ("GOOGLE_", "GCLOUD_", "CLOUDSDK_", "GCE_", "VERTEX_", "CLOUD_ML_", "METADATA_")}
_LOOPBACK = ("127.0.0.1", "localhost", "::1")
# The largest settings file Pulsar reads (bytes). A project file above it is "unreadable" (fail closed:
# refused on the direct and cloud routes, a warning on core), never "absent".
_SETTINGS_LIMIT = 16 * 1024 * 1024


def pinned_names() -> List[str]:
    """Every variable a project's settings may never change in a Pulsar session (its keys can add more)."""
    from pulsar import launch as L
    names: List[str] = []
    for n in tuple(L.SCRUB) + tuple(L.MODEL_VARS) + PIN_ALWAYS + tuple(L.EGRESS_FLAGS) + SECRET_VARS + SESSION_VARS:
        if n not in names:
            names.append(n)
    return names


def _env_value(value: Any) -> Optional[str]:
    """A settings `env` value as Claude Code's process sees it (strings, numbers, booleans), else None."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    return None


def env_block(data: Any) -> Dict[str, str]:
    """The `env` of a settings object, as strings (other values dropped)."""
    env = data.get("env") if isinstance(data, dict) else None
    out: Dict[str, str] = {}
    if isinstance(env, dict):
        for k, v in env.items():
            value = _env_value(v)
            if isinstance(k, str) and k and value is not None:
                out[k] = value
    return out


# --- reading settings files the way Claude Code does --------------------------------------------------------
#
# Claude Code reads a settings file whatever its byte order mark (Windows editors and PowerShell write UTF-8
# with a BOM, PowerShell 5 writes UTF-16), turns invalid UTF-8 into U+FFFD instead of failing, has no 1 MB
# limit and no nesting limit. A reader that gives up on any of those would see "no file" where Claude Code
# applies one, so a project could swap the session's credential unseen. read_settings_file() therefore says
# apart a file that is absent from one that is there but could not be read (the caller fails closed).
_BOMS = ((codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be"), (codecs.BOM_UTF8, "utf-8"),
         (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be"))
_JSON_WS = re.compile(r"[ \t\n\r]*")
_JSON_NUMBER = re.compile(r"(-?(?:0|[1-9][0-9]*))(\.[0-9]+)?([eE][-+]?[0-9]+)?")
_JSON_CONSTANTS = (("true", True), ("false", False), ("null", None), ("NaN", float("nan")),
                   ("Infinity", float("inf")), ("-Infinity", float("-inf")))


class SettingsFile(NamedTuple):
    data: Optional[Dict[str, Any]]   # the JSON object; None when absent, empty, not an object or unreadable
    problem: str                     # "" when absent or read; else why a file that is there could not be read


def decode_settings(raw: bytes) -> str:
    """A settings file's bytes as text: by byte order mark (UTF-8, UTF-16, UTF-32), else UTF-8 with U+FFFD
    for invalid bytes, as Claude Code (Bun) reads it."""
    for bom, codec in _BOMS:
        if raw.startswith(bom):
            return raw[len(bom):].decode(codec, errors="replace")
    return raw.decode("utf-8", errors="replace")


def _parse_deep(text: str) -> Any:
    """json.loads without a nesting limit (an explicit stack instead of recursion). Raises ValueError."""
    from json.decoder import scanstring
    ws = _JSON_WS.match
    end = len(text)

    def fail(what: str, at: int) -> None:
        raise ValueError("%s at char %d" % (what, at))

    def key_at(at: int) -> Tuple[str, int]:
        if not text.startswith('"', at):
            fail("expecting a property name", at)
        key, at = scanstring(text, at + 1, True)
        at = ws(text, at).end()
        if not text.startswith(":", at):
            fail("expecting ':'", at)
        return key, ws(text, at + 1).end()

    stack: List[List[Any]] = []          # ["a", list] or ["o", dict, pending key]
    i = ws(text, 0).end()
    while True:
        value: Any = None
        if i >= end:
            fail("expecting a value", i)
        c = text[i]
        if c == "{":
            i = ws(text, i + 1).end()
            if not text.startswith("}", i):
                key, i = key_at(i)
                stack.append(["o", {}, key])
                continue
            value, i = {}, i + 1
        elif c == "[":
            i = ws(text, i + 1).end()
            if not text.startswith("]", i):
                stack.append(["a", []])
                continue
            value, i = [], i + 1
        elif c == '"':
            value, i = scanstring(text, i + 1, True)
        else:
            m = _JSON_NUMBER.match(text, i)
            if m is not None:
                whole, frac, exp = m.groups()
                value = float(whole + (frac or "") + (exp or "")) if frac or exp else int(whole)
                i = m.end()
            else:
                for word, const in _JSON_CONSTANTS:
                    if text.startswith(word, i):
                        value, i = const, i + len(word)
                        break
                else:
                    fail("expecting a value", i)
        while True:                      # hand the value to its container; close the ones that end here
            if not stack:
                i = ws(text, i).end()
                if i != end:
                    fail("extra data", i)
                return value
            top = stack[-1]
            i = ws(text, i).end()
            if top[0] == "a":
                top[1].append(value)
                closer = "]"
            else:
                top[1][top[2]] = value
                closer = "}"
            if text.startswith(",", i):
                i = ws(text, i + 1).end()
                if top[0] == "o":
                    top[2], i = key_at(i)
                break
            if not text.startswith(closer, i):
                fail("expecting ',' or '%s'" % closer, i)
            i += 1
            value = stack.pop()[1]


def parse_settings(text: str) -> Any:
    """A settings file's text as JSON, at any nesting depth. Raises ValueError when it is not JSON."""
    try:
        return json.loads(text)
    except RecursionError:
        return _parse_deep(text)


def dumps_settings(data: Any) -> str:
    """JSON text for a settings file (indented); a value nested deeper than Python's recursion limit (a user's
    own --settings may hold one) is written compact instead of failing."""
    try:
        return json.dumps(data, indent=2)
    except RecursionError:
        pass
    out: List[str] = []
    stack: List[Tuple[str, Any, bool]] = []
    done = object()
    value = data
    while True:
        if isinstance(value, dict):
            out.append("{")
            stack.append(("o", iter(value.items()), True))
        elif isinstance(value, (list, tuple)):
            out.append("[")
            stack.append(("a", iter(value), True))
        else:
            out.append(json.dumps(value))
        while stack:
            kind, it, first = stack[-1]
            nxt = next(it, done)
            if nxt is done:
                out.append("}" if kind == "o" else "]")
                stack.pop()
                continue
            stack[-1] = (kind, it, False)
            if not first:
                out.append(",")
            if kind == "o":
                out.append(json.dumps(nxt[0] if isinstance(nxt[0], str) else str(nxt[0])) + ":")
                value = nxt[1]
            else:
                value = nxt
            break
        else:
            return "".join(out)


def _read_bytes(path: str, regular_only: bool = True) -> bytes:
    """A settings file's bytes (at most _SETTINGS_LIMIT + 1). Raises OSError, FileNotFoundError when absent.

    regular_only: anything but a regular file (a FIFO, a /dev/stdin symlink, a device, a folder) raises
    without being opened or read, so a project cannot hang the launcher or swallow the user's piped input;
    the open is non-blocking and the type checked again on the open file (no swap in between).
    """
    if regular_only:
        st = os.stat(path)
        if not stat.S_ISREG(st.st_mode):
            raise OSError("not a regular file")
        if st.st_size > _SETTINGS_LIMIT:
            raise OSError("larger than %d MB" % (_SETTINGS_LIMIT // (1024 * 1024)))
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOCTTY", 0)
    if regular_only:
        flags |= getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        if regular_only and not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        chunks: List[bytes] = []
        size = 0
        while size <= _SETTINGS_LIMIT:
            chunk = os.read(fd, min(1 << 20, _SETTINGS_LIMIT + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    if size > _SETTINGS_LIMIT:
        raise OSError("larger than %d MB" % (_SETTINGS_LIMIT // (1024 * 1024)))
    return b"".join(chunks)


def read_settings_file(path: str) -> SettingsFile:
    """A settings file as Claude Code reads it (see above): (the JSON object or None, why it is unreadable).

    Absent (or empty, or a JSON value that is not an object, which sets nothing): (None, ""). There but not
    readable (not a regular file, too large, no permission, not JSON): (None, <reason>).
    """
    try:
        raw = _read_bytes(path)
    except (FileNotFoundError, NotADirectoryError):
        return SettingsFile(None, "")
    except OSError as exc:
        reason = exc.strerror or (str(exc.args[0]) if exc.args else "") or type(exc).__name__
        return SettingsFile(None, P.clean_text(reason, 80))
    text = decode_settings(raw)
    if not text.strip(" \t\r\n"):
        return SettingsFile(None, "")
    try:
        data = parse_settings(text)
    except (ValueError, RecursionError, MemoryError):
        return SettingsFile(None, "not valid JSON")
    return SettingsFile(data if isinstance(data, dict) else None, "")


def _read_settings(path: str) -> Optional[Dict[str, Any]]:
    """A settings file as a dict, or None when it is missing, unreadable or not a JSON object."""
    return read_settings_file(path).data


class ProjectSettings(NamedTuple):
    env: Dict[str, str]          # the env the project's files give (settings.local.json wins)
    helper: bool                 # whether one sets apiKeyHelper
    unreadable: List[str]        # files that are there but could not be read: "settings.json (not valid JSON)"


def read_project_settings(cwd: str, sources: Optional[Iterable[str]] = None) -> ProjectSettings:
    """What a project's .claude/settings.json and settings.local.json set, and which of them Pulsar could not
    read although they are there (Claude Code may still apply them: callers fail closed, see pin_report).

    settings.local.json wins over settings.json, as in Claude Code. sources: Claude Code's --setting-sources
    (None: all), which can leave out "project" (settings.json) or "local" (settings.local.json).
    """
    env: Dict[str, str] = {}
    helper = False
    unreadable: List[str] = []
    wanted = None if sources is None else set(sources)
    for source, name in (("project", "settings.json"), ("local", "settings.local.json")):
        if wanted is not None and source not in wanted:
            continue
        data, problem = read_settings_file(os.path.join(cwd, ".claude", name))
        if problem:
            unreadable.append("%s (%s)" % (name, problem))
        if data is None:
            continue
        env.update(env_block(data))
        helper = helper or bool(data.get("apiKeyHelper"))
    return ProjectSettings(env, helper, unreadable)


def project_settings(cwd: str, sources: Optional[Iterable[str]] = None) -> Tuple[Dict[str, str], bool]:
    """(the env a project's .claude/settings.json and settings.local.json give, whether one sets apiKeyHelper).

    See read_project_settings, which also names the files that are there but could not be read.
    """
    found = read_project_settings(cwd, sources)
    return found.env, found.helper


def managed_settings_dir() -> str:
    """Where Claude Code reads managed (policy) settings on this OS."""
    if sys.platform == "darwin":
        return "/Library/Application Support/ClaudeCode"
    if os.name == "nt":
        return r"C:\Program Files\ClaudeCode"
    return "/etc/claude-code"


def managed_settings_env() -> Dict[str, str]:
    """The env of managed-settings.json and its managed-settings.d/*.json drop-ins (later files win)."""
    base = managed_settings_dir()
    out = env_block(_read_settings(os.path.join(base, "managed-settings.json")))
    drop = os.path.join(base, "managed-settings.d")
    try:
        names = sorted(n for n in os.listdir(drop) if n.endswith(".json") and not n.startswith("."))
    except OSError:
        names = []
    for n in names:
        out.update(env_block(_read_settings(os.path.join(drop, n))))
    return out


def user_settings_env(env: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """The env of the user-level settings.json Claude Code reads in this session (its CLAUDE_CONFIG_DIR: the
    isolated profile, or ~/.claude with PULSAR_CLAUDE_CONFIG=shared)."""
    from pulsar import launch as L
    env = os.environ if env is None else env
    folder = env.get("CLAUDE_CONFIG_DIR") or str(L.claude_dir())
    return env_block(_read_settings(os.path.join(folder, "settings.json")))


def user_layers(env: Mapping[str, str], flag_envs: Iterable[Mapping[str, str]] = (),
                sources: Optional[Iterable[str]] = None) -> Dict[str, str]:
    """The env the user's own settings sources give, in Claude Code's order: user-level settings.json (unless
    --setting-sources leaves "user" out), then the user's --settings, then managed settings (each wins over
    the one before; all win over the shell)."""
    out = dict(user_settings_env(env)) if sources is None or "user" in set(sources) else {}
    for extra in flag_envs:
        out.update({k: v for k, v in extra.items() if isinstance(v, str)})
    out.update(managed_settings_env())
    return out


def _route_follow(name: str) -> bool:
    return bool(_ROUTE_LIKE.search(name))


_USER_UPPER = frozenset(n.upper() for n in USER_VARS)


def _user_follow(name: str) -> bool:
    return name.upper() in _USER_UPPER or bool(_USER_LIKE.search(name))


def follow_names(project_env: Mapping[str, str]) -> List[str]:
    """Project keys to pin: pinned names in any spelling, and routing-like or network-like keys."""
    static = {n.upper() for n in pinned_names()}
    return [k for k in project_env if k.upper() in static or _route_follow(k) or _user_follow(k)]


def _cloud(env: Mapping[str, str]) -> str:
    return "bedrock" if env.get("CLAUDE_CODE_USE_BEDROCK") else "vertex" if env.get("CLAUDE_CODE_USE_VERTEX") else ""


def _looks_sensitive(name: str, env: Mapping[str, str]) -> bool:
    """A key that can change where this session connects or which credential it uses, though not pinned."""
    up = name.upper()
    if up == "CLAUDE_CONFIG_DIR":
        return True          # which profile (and credentials) Claude Code and its tools use; unpinned in shared mode
    return up.startswith(_CLOUD_PREFIXES.get(_cloud(env), ())) or (up.startswith("CLAUDE_")
                                                                     and bool(_SENSITIVE.search(up)))


def session_env(env: Dict[str, str], follow: Iterable[str] = (),
                user: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """The `env` block of the per-session settings (pure: every input is an argument).

    env: Claude Code's environment (launch.build_env). follow: more names to pin (follow_names of the project's
    keys). user: the user's own settings env (user_layers); network variables take its values over the shell's.
    """
    from pulsar import launch as L
    user = dict(user or {})
    egress = set(L.EGRESS_FLAGS)
    out: Dict[str, str] = {}
    static = pinned_names()
    static_set = set(static)
    canon = {n.upper(): n for n in static}
    names = static + sorted(k for k in env if k not in static_set and k.startswith((
        "PULSAR_", "CLAUDE_CODE_AUTO_COMPACT", "CLAUDE_AUTOCOMPACT", "CLAUDE_CODE_BLOCKING")))
    known = set(names)
    for k in follow:
        if k not in known:
            names.append(k)
            known.add(k)
    for name in names:
        if name in NO_PROXY_VARS:
            continue
        # Another spelling of a pinned name (Windows reads anthropic_base_url as ANTHROPIC_BASE_URL): its value.
        src = name if name in static_set or name in env else canon.get(name.upper(), name)
        if _user_follow(src):
            value = user[src] if src in user else env.get(src, "")
        elif src in egress:
            value = env.get(src, "") or user.get(src, "")
        else:
            value = env.get(src, "")
            if value and src in SECRET_VARS and not _SESSION_TOKEN.match(value):
                continue                               # a provider key: never written to disk
        if not isinstance(value, str):
            continue
        if src == "CLAUDE_CONFIG_DIR" and not value:
            continue                                   # shared mode without one of yours: "" would be the config home
        if src == "PULSAR_HOME" and not value:
            value = str(K.home())                      # "" would make the MCP server use the project folder
        out[name] = value
    # NO_PROXY: the user's own list (their settings, else the shell's), always with loopback, which Core needs.
    hosts: List[str] = []
    mine = [user[n] for n in NO_PROXY_VARS if user.get(n)]
    for value in mine or [env.get(n, "") for n in NO_PROXY_VARS]:
        for h in (value or "").split(","):
            h = h.strip()
            if h and h not in hosts:
                hosts.append(h)
    for h in _LOOPBACK:
        if h not in hosts:
            hosts.append(h)
    for name in NO_PROXY_VARS + tuple(k for k in follow if k.upper() == "NO_PROXY" and k not in NO_PROXY_VARS):
        out[name] = ",".join(hosts)
    return out


def route_of(env: Mapping[str, str]) -> Tuple[str, str]:
    """(route, what the session talks to) for Claude Code's environment: core, direct or cloud."""
    route = env.get("PULSAR_ROUTE", "")
    token = env.get("ANTHROPIC_AUTH_TOKEN", "")
    if route not in ("core", "direct", "cloud"):
        if env.get("PULSAR_CORE") and env.get("ANTHROPIC_BASE_URL") == env.get("PULSAR_CORE") \
                and _SESSION_TOKEN.match(token):
            route = "core"
        elif env.get("CLAUDE_CODE_USE_BEDROCK") or env.get("CLAUDE_CODE_USE_VERTEX") \
                or env.get("CLAUDE_CODE_OAUTH_TOKEN"):
            route = "cloud"
        else:
            route = "direct"
    if route == "core":
        return route, "Pulsar Core"
    if env.get("CLAUDE_CODE_USE_BEDROCK"):
        return route, "Amazon Bedrock"
    if env.get("CLAUDE_CODE_USE_VERTEX"):
        return route, "Google Vertex AI"
    if route == "cloud":
        return route, "your Claude subscription"
    base = env.get("ANTHROPIC_BASE_URL", "")
    host = re.sub(r"^[a-z][a-z0-9+.-]*://([^/@]*@)?", "", base, flags=re.I).split("/")[0] if base else ""
    return route, P.clean_text(host, 80) or "api.anthropic.com"


def route_credentials(env: Mapping[str, str]) -> List[str]:
    """The variables that hold this session's own provider credential (direct and cloud routes; [] on core)."""
    route = route_of(env)[0]
    if route == "core":
        return []
    names = [n for n in SECRET_VARS if env.get(n) and not _SESSION_TOKEN.match(env.get(n, ""))]
    return names + [n for n in CLOUD_CREDENTIALS.get(_cloud(env), ()) if n not in names]


class PinReport(NamedTuple):
    env: Dict[str, str]          # the pinned `env` block
    overridden: List[str]        # project keys whose value the pins replace (plus apiKeyHelper)
    swapped: List[str]           # project keys that would change this route's credential: refuse the launch
    unpinned: List[str]          # project keys that look like routing or credentials, not pinned, still applied
    route: str                   # core, direct or cloud
    target: str                  # what the session talks to ("Pulsar Core", a host, "Amazon Bedrock", ...)
    # project files that are there but could not be read ("settings.json (not valid JSON)"): Claude Code may
    # still apply them, so nothing above can be known for them. Refused on direct and cloud, a warning on core.
    unreadable: Tuple[str, ...] = ()


def pin_report(env: Dict[str, str], cwd: Optional[str] = None, flag_envs: Iterable[Mapping[str, str]] = (),
               sources: Optional[Iterable[str]] = None) -> PinReport:
    """Pins for this session's settings file and what they mean for the project in `cwd` (see above).

    flag_envs: the env of the user's own --settings; sources: Claude Code's --setting-sources (None: all).
    """
    cwd = os.getcwd() if cwd is None else cwd
    sources = None if sources is None else list(sources)
    project, helper, unreadable = read_project_settings(cwd, sources)
    user = user_layers(env, list(flag_envs), sources)
    pins = session_env(env, follow_names(project), user)
    route, target = route_of(env)
    # Windows reads anthropic_auth_token as ANTHROPIC_AUTH_TOKEN; elsewhere another spelling is another variable.
    fold = (lambda n: n.upper()) if os.name == "nt" else (lambda n: n)   # noqa: E731
    creds = {fold(n): n for n in route_credentials(env)}
    overridden: List[str] = []
    swapped: List[str] = []
    unpinned: List[str] = []
    for k, v in project.items():
        name = P.clean_text(k, 60)
        if k in pins:
            if pins[k] != v:
                overridden.append(name)
        elif fold(k) in creds:                        # a real credential: never pinned, so refused instead
            c = creds[fold(k)]
            if v != (env.get(c, "") if c in SECRET_VARS else user.get(c, env.get(c, ""))):
                swapped.append(name)
        elif _looks_sensitive(k, env) and v != user.get(k, env.get(k, "")):
            unpinned.append(name)
    if helper:
        overridden.append("apiKeyHelper")
    return PinReport(pins, overridden, swapped, unpinned, route, target, tuple(unreadable))


def project_overrides(cwd: str) -> List[str]:
    """Pinned settings a project's .claude/settings*.json sets (every pinned name or pattern, any value)."""
    project, helper = project_settings(cwd)
    found = [P.clean_text(k, 60) for k in follow_names(project)]
    if helper:
        found.append("apiKeyHelper")
    return found


def load_flag_settings(value: str, cwd: Optional[str] = None) -> Dict[str, Any]:
    """A user's own `--settings` value as Claude Code reads it: inline JSON ({...}) or a file path.

    Raises ValueError (a message without the value) when it cannot be read or is not a JSON object.
    """
    text = (value or "").strip()
    if text.startswith("{"):                       # never echoed: inline JSON may hold a key
        try:
            data = parse_settings(text) if text.endswith("}") else None
        except (ValueError, RecursionError):
            data = None
        if data is None:
            raise ValueError("the JSON given to --settings is not valid")
    else:
        # Decoded as Claude Code does (a byte order mark, UTF-16, invalid UTF-8) and at any depth. Not only
        # regular files: the user's own `--settings <(...)` is a pipe, read to its end.
        path = os.path.join(cwd or os.getcwd(), os.path.expanduser(value))
        try:
            raw = _read_bytes(path, regular_only=False)
        except OSError as exc:
            if exc.strerror is None and exc.args and str(exc.args[0]).startswith("larger than"):
                raise ValueError("the --settings file %s is %s" % (P.clean_text(value, 200), exc.args[0])) from None
            raise ValueError("cannot read the --settings file %s (%s)" % (
                P.clean_text(value, 200), type(exc).__name__)) from None
        try:
            data = parse_settings(decode_settings(raw))
        except (ValueError, RecursionError, MemoryError):
            raise ValueError("the --settings file %s is not valid JSON" % P.clean_text(value, 200)) from None
    if not isinstance(data, dict):
        raise ValueError("--settings must hold a JSON object")
    return data


def merge_settings(base: Mapping[str, Any], users: Iterable[Mapping[str, Any]],
                   pins: Mapping[str, str]) -> Dict[str, Any]:
    """Pulsar's settings with the user's own --settings merged in, then the pins (one --settings file).

    The user's keys win over Pulsar's (status line, picker, ...); permission lists are joined, so Pulsar's
    deny rules stay; env is the user's, with the pins applied last, and apiKeyHelper is always "".
    """
    out: Dict[str, Any] = {k: v for k, v in base.items() if k != "env"}
    perms: Dict[str, Any] = dict(base["permissions"]) if isinstance(base.get("permissions"), dict) else {}
    env: Dict[str, Any] = {}
    for user in users:
        for k, v in user.items():
            if k == "env":
                if isinstance(v, dict):
                    env.update(v)
            elif k == "permissions" and isinstance(v, dict):
                for pk, pv in v.items():
                    if isinstance(perms.get(pk), list):
                        if isinstance(pv, list):
                            perms[pk] = perms[pk] + [x for x in pv if x not in perms[pk]]
                    else:
                        perms[pk] = pv
            else:
                out[k] = v
    if perms:
        out["permissions"] = perms
    env.update(pins)
    out["env"] = env
    out["apiKeyHelper"] = ""
    return out


def read_pulsar_settings(path: str) -> Optional[Dict[str, Any]]:
    """The settings file Pulsar wrote at `path` (a settings.json in its home), or None for any other file."""
    try:
        home = K.home().resolve()
        rp = Path(path).resolve()
    except (OSError, RuntimeError):
        return None
    if rp.name != "settings.json" or home not in rp.parents:
        return None
    data = _read_settings(str(rp))
    return data if data is not None else {}


def pin_settings(path: Path, env: Dict[str, str], cwd: Optional[str] = None) -> bool:
    """Add the pinned env and an empty apiKeyHelper to a Pulsar settings file. True on success.

    Only a settings.json Pulsar wrote (in its home); anything else (a user's own --settings) is left alone.
    """
    data = read_pulsar_settings(str(path))
    if data is None:
        return False
    try:
        K.write_private(Path(path).resolve(), dumps_settings(merge_settings(data, [], pin_report(env, cwd).env))
                        + "\n")
    except OSError:
        return False
    return True


def bare_settings(model_label: str = "", picker: Optional[List[Dict[str, Any]]] = None,
                  anthropic: bool = False) -> Dict[str, Any]:
    """The settings a PULSAR_PLUGIN=off launch still needs: one /model row for the launched model, with
    behavesAs, so Claude Code knows how to talk to it (without it Claude Code prints
    "[claude-code:unrecognized_model]" on every headless run and treats the model as unknown)."""
    rows = [o for o in picker or [] if isinstance(o, dict) and str(o.get("model") or "") == model_label][:1]
    if not rows or os.environ.get("PULSAR_MODEL_PICKER", "on").lower() in ("0", "off", "none"):
        return {}
    return {"modelPicker": model_picker(rows, replace=not anthropic,
                                        long_ok=not anthropic and os.environ.get("PULSAR_DIRECT") != "1")}


def write_pins(env: Dict[str, str], session_id: str = "", cwd: Optional[str] = None, model_label: str = "",
               picker: Optional[List[Dict[str, Any]]] = None, anthropic: bool = False) -> Path:
    """A settings file holding only the pinned env, an empty apiKeyHelper and the launched model's /model row
    (bare_settings): PULSAR_PLUGIN=off launches."""
    path = settings_path(session_id)
    data = bare_settings(model_label, picker, anthropic)
    data.update({"env": pin_report(env, cwd).env, "apiKeyHelper": ""})
    K.write_private(path, json.dumps(data, indent=2) + "\n")
    return path


def write_settings(model_label: str = "", picker: Optional[List[Dict[str, Any]]] = None, anthropic: bool = False,
                   claude_config: Optional[Path] = None, isolated: bool = True, session_id: str = "",
                   env: Optional[Dict[str, str]] = None) -> Path:
    """Write this launch's settings (0600) and return the path for `--settings`.

    With a session id each launch gets its own file, so two sessions with different models never
    overwrite each other's status line, picker or announcement. env: Claude Code's environment
    (launch.build_env), pinned so project settings cannot re-route the session (see pin_report);
    launch.exec_claude pins it too when the caller did not pass it.
    """
    if claude_config is not None and isolated:
        write_theme(claude_config)
    path = settings_path(session_id)
    data = settings(model_label, picker, anthropic, isolated)
    if env is not None:
        data["env"] = pin_report(env).env
        data["apiKeyHelper"] = ""
    K.write_private(path, json.dumps(data, indent=2) + "\n")
    if session_id:
        try:
            os.chmod(str(path.parent), 0o700)
        except OSError:
            pass
    return path


def stage_pane_dir(claude_version: str = "") -> Optional[Path]:
    """pulsar/fnplug (the in-terminal Stage pane) when wanted and this Claude Code supports it, else None."""
    if not claude_version:
        return None
    try:
        from pulsar.stage import pane
        d = pane.plugin_dir()                     # None with PULSAR_STAGE_PANE=off or when not shipped
        return d if d is not None and pane.supported(claude_version) else None
    except Exception:
        return None


def claude_args(model_label: str = "", picker: Optional[List[Dict[str, Any]]] = None, anthropic: bool = False,
                claude_config: Optional[Path] = None, isolated: bool = True, claude_version: str = "",
                session_id: str = "", env: Optional[Dict[str, str]] = None) -> List[str]:
    """Extra flags prepended to the user's Claude Code arguments.

    claude_version: what `claude --version` said (the launcher's preload); it decides whether the
    Stage pane plugin is loaded too. session_id: the Core session, for per-session settings.
    """
    if os.environ.get("PULSAR_PLUGIN", "on").lower() in ("0", "off", "none"):
        # No plugin, status line or theme; still one settings file (launch.check_pins adds the pins to it)
        # with the launched model's /model row (bare_settings).
        path = settings_path(session_id)
        data = bare_settings(model_label, picker, anthropic)
        if env is not None:
            data.update({"env": pin_report(env).env, "apiKeyHelper": ""})
        K.write_private(path, json.dumps(data, indent=2) + "\n")
        if session_id:
            try:
                os.chmod(str(path.parent), 0o700)
            except OSError:
                pass
        return ["--settings", str(path)]
    root = build_plugin()
    out = ["--plugin-dir", str(root)]
    pane_dir = stage_pane_dir(claude_version)
    if pane_dir is not None:
        out += ["--plugin-dir", str(pane_dir)]
    out += ["--settings", str(write_settings(model_label, picker, anthropic, claude_config, isolated,
                                             session_id=session_id, env=env))]
    if session_id:
        try:
            K.write_private(settings_path(session_id).parent / PLUGIN_RECORD, str(root) + "\n")
        except OSError:
            pass
    return out
