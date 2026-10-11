# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Process plumbing: find Claude Code, register a Core session, build the env, exec."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, NamedTuple, Optional, Tuple

from pulsar import __version__
from pulsar import config as K
from pulsar import providers as P
from pulsar.core import state as ST

DEFAULT_CONTEXT = 128_000

SCRUB = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_VERTEX_BASE_URL", "ANTHROPIC_VERTEX_PROJECT_ID",
    "ANTHROPIC_FOUNDRY_BASE_URL", "ANTHROPIC_FOUNDRY_API_KEY", "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_CUSTOM_HEADERS", "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS", "PULSAR_SESSION", "PULSAR_CORE", "PULSAR_ADMIN_TOKEN", "PULSAR_SESSION_TOKEN",
    # cloud routes / identity / extra body that would silently re-route or alter the session
    "CLAUDE_CODE_USE_MANTLE", "CLAUDE_CODE_USE_ANTHROPIC_AWS", "ANTHROPIC_BEDROCK_MANTLE_BASE_URL",
    "ANTHROPIC_AWS_BASE_URL", "ANTHROPIC_AWS_WORKSPACE_ID", "ANTHROPIC_FOUNDRY_AUTH_TOKEN", "ANTHROPIC_FOUNDRY_RESOURCE",
    "ANTHROPIC_DEFAULT_FABLE_MODEL", "ANTHROPIC_DEFAULT_MODEL", "ANTHROPIC_BETAS", "CLAUDE_CODE_EXTRA_BODY",
    "ANTHROPIC_PROFILE", "ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID", "AWS_BEARER_TOKEN_BEDROCK",
    # Every provider switch Claude Code knows (2.1.296: its own list of them names these seven), their base
    # URLs, project and workspace ids, and the "skip auth" flags that let a switch work without credentials.
    # tools/compat/run.py (check pins) fails when a Claude Code release adds a CLAUDE_CODE_USE_* or
    # ANTHROPIC_*_BASE_URL that is not here.
    "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD", "CLAUDE_CODE_USE_GATEWAY", "ANTHROPIC_GOOGLE_CLOUD_BASE_URL",
    "ANTHROPIC_GOOGLE_CLOUD_PROJECT", "ANTHROPIC_GOOGLE_CLOUD_LOCATION", "ANTHROPIC_GOOGLE_CLOUD_WORKSPACE_ID",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH", "CLAUDE_CODE_SKIP_VERTEX_AUTH", "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
    "CLAUDE_CODE_SKIP_MANTLE_AUTH", "CLAUDE_CODE_SKIP_ANTHROPIC_AWS_AUTH", "CLAUDE_CODE_SKIP_ANTHROPIC_GOOGLE_CLOUD_AUTH",
    "ANTHROPIC_AWS_API_KEY", "ANTHROPIC_UNIX_SOCKET", "_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL",
    "CLAUDE_CODE_GATEWAY_TOKEN", "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST", "CLAUDE_CODE_HOST_AUTH_ENV_VAR",
    "ANTHROPIC_IDENTITY_TOKEN", "ANTHROPIC_IDENTITY_TOKEN_FILE", "ANTHROPIC_SERVICE_ACCOUNT_ID",
    "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
    "CLAUDE_CODE_GATEWAY_TOKEN_FILE_DESCRIPTOR",
)
# ANTHROPIC_DEFAULT_MODEL is what /model's "Default" row resolves to: the launched model, so Default never gets
# Claude Code's "[1m]" upgrade for a model below 1M (see CLAUDE_WINDOW below).
MODEL_VARS = ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
              "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
              "CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_DEFAULT_MODEL")
# No egress besides the model: set by build_env and pinned in the session's settings, so a project cannot
# turn them back on. With PULSAR_CLAUDE_AUTOUPDATE=1 build_env leaves UPDATE_FLAGS unset, and that is pinned.
TRAFFIC_FLAGS = ("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "DISABLE_TELEMETRY", "DISABLE_ERROR_REPORTING",
                 "DISABLE_BUG_COMMAND", "DISABLE_NON_ESSENTIAL_MODEL_CALLS", "DISABLE_COST_WARNINGS")
UPDATE_FLAGS = ("DISABLE_AUTOUPDATER", "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL")
EGRESS_FLAGS = TRAFFIC_FLAGS + UPDATE_FLAGS + ("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS",)

# Model-provider keys people commonly export besides the registry's own variables (P.PROVIDERS[*].env).
EXTRA_KEY_VARS = (
    "AZURE_OPENAI_API_KEY", "AZURE_API_KEY", "AZURE_AI_API_KEY", "AZURE_INFERENCE_CREDENTIAL",
    "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACEHUB_API_TOKEN", "HF_API_TOKEN", "HF_API_KEY",
    "GOOGLE_GENERATIVE_AI_API_KEY", "GOOGLE_AI_API_KEY", "PALM_API_KEY", "VERTEX_API_KEY",
    "OPENAI_KEY", "OPENAI_ADMIN_KEY", "ANTHROPIC_ADMIN_KEY", "OPENROUTER_KEY",
    "TOGETHERAI_API_KEY", "TOGETHER_AI_API_KEY", "FIREWORKS_AI_API_KEY", "GROK_API_KEY", "XAI_KEY",
    "PERPLEXITY_API_KEY", "PPLX_API_KEY", "QWEN_API_KEY", "ZHIPUAI_API_KEY", "GLM_API_KEY",
    "REPLICATE_API_TOKEN", "AI21_API_KEY", "LLAMA_API_KEY", "SILICONFLOW_API_KEY", "NVIDIA_NGC_API_KEY",
    "NGC_API_KEY", "CODESTRAL_API_KEY", "VOYAGE_API_KEY", "LITELLM_API_KEY", "LITELLM_MASTER_KEY",
)


# Never dropped because their value happens to contain a key-like string: Claude Code needs them to run.
KEEP_ALWAYS = frozenset(("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "TMPDIR", "TEMP", "TMP", "PWD",
                         "SYSTEMROOT", "SystemRoot", "COMSPEC", "PATHEXT", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
                         "COLORTERM", "TERM_PROGRAM", "NO_PROXY", "no_proxy"))


def _secret_values(env: Dict[str, str], extra: Iterable[str]) -> List[str]:
    """Key values to hunt for under any variable name: `extra` plus every provider variable's value.

    Paths and short values are left out (a path is configuration, and a short value would match by accident).
    """
    out: List[str] = []
    for value in list(extra) + [env.get(var, "") for var in provider_key_vars()]:
        v = (value or "").strip()
        if len(v) < 8 or v.startswith(("/", "~", ".")) or (len(v) > 2 and v[1] == ":" and v[2] in "\\/"):
            continue
        if v.lower().startswith("bearer "):
            v = v[7:].strip()
        if len(v) < 8:
            continue
        raw = v.encode("utf-8", "replace")
        # the value itself and its base64 forms (KEY_B64=$(printf %s "$KEY" | base64))
        for form in (v, base64.b64encode(raw).decode("ascii").rstrip("="),
                     base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")):
            if form not in out:
                out.append(form)
    return out


def provider_key_vars() -> Tuple[str, ...]:
    """Every variable that can carry a model-provider key: the registry's plus common aliases."""
    names = set(EXTRA_KEY_VARS)
    for p in P.PROVIDERS:
        names.update(p.env)
        if p.key_env:
            names.add(p.key_env)
    return tuple(sorted(names))


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------

def _nt() -> bool:
    return os.name == "nt"


def is_wsl(environ: Optional[Mapping[str, str]] = None) -> bool:
    """True inside Windows Subsystem for Linux."""
    if _nt() or not sys.platform.startswith("linux"):
        return False
    env = os.environ if environ is None else environ
    if env.get("WSL_DISTRO_NAME") or env.get("WSL_INTEROP"):
        return True
    try:
        with open("/proc/version", encoding="utf-8", errors="replace") as fh:
            return "microsoft" in fh.read(1024).lower()
    except OSError:
        return False


_WINDOWS_DRIVE = re.compile(r"^/mnt/[A-Za-z](/|$)")


def _windows_side(path: str) -> bool:
    """A Windows program seen from WSL: under /mnt/<drive>/ or a .exe/.cmd/.bat."""
    return bool(_WINDOWS_DRIVE.match(path)) or path.lower().endswith((".exe", ".cmd", ".bat", ".ps1"))


def _which(names: Iterable[str], skip: Callable[[str], bool] = lambda d: False,
           path: Optional[str] = None) -> Optional[str]:
    """The first of `names` in an absolute PATH entry (never the current folder, never a relative entry)."""
    entries = (os.environ.get("PATH", "") if path is None else path).split(os.pathsep)
    for d in entries:
        d = d.strip().strip('"')
        if not d or not os.path.isabs(d) or skip(d):
            continue
        for name in names:
            cand = os.path.join(d, name)
            if os.path.isfile(cand) and (_nt() or os.access(cand, os.X_OK)):
                return cand
    return None


def wsl_windows_claude(path: Optional[str] = None) -> str:
    """Under WSL: the Windows Claude Code that Pulsar ignored on the Windows PATH, else ""."""
    if not is_wsl():
        return ""
    return _which(("claude", "claude.exe", "claude.cmd"), skip=lambda d: not _WINDOWS_DRIVE.match(d), path=path) or ""


# The user's umask before the launcher made its own files private (private_umask), restored for Claude Code.
_USER_UMASK: Optional[int] = None
# Folders in the Pulsar home that hold session state, logs and Claude Code's isolated profile.
PRIVATE_DIRS = ("run", "run/sessions", "cache", "logs", "claude_config", "plugin")


def private_umask() -> None:
    """umask 077 for the launcher and the Core it spawns: every file and folder they create is private.

    Pulsar's home is 0700, but its contents must stay private even when that folder's mode is loosened
    (a dotfiles script, an installer, `chmod -R`). The user's own umask is remembered and handed back to
    Claude Code at exec (exec_claude): files the model writes into the project keep their usual modes.
    """
    global _USER_UMASK
    if _nt():
        return
    old = os.umask(0o077)
    if _USER_UMASK is None:
        _USER_UMASK = old


def restore_umask() -> None:
    """Give Claude Code the umask the user started pulsarcode with (see private_umask)."""
    global _USER_UMASK
    if _USER_UMASK is not None and not _nt():
        os.umask(_USER_UMASK)
        _USER_UMASK = None


def tighten_home(root: Optional[Path] = None) -> List[str]:
    """chmod 0700 the folders in the Pulsar home that an older build (or another umask) left 0755.

    Never follows a symlink and never touches a folder owned by someone else. Returns what changed.
    """
    if _nt():
        return []
    root = K.home() if root is None else root
    changed: List[str] = []
    uid = os.getuid() if hasattr(os, "getuid") else -1
    for rel in PRIVATE_DIRS:
        p = root / rel
        try:
            st = os.lstat(str(p))
        except OSError:
            continue
        import stat as _stat
        if not _stat.S_ISDIR(st.st_mode) or st.st_uid != uid or _stat.S_IMODE(st.st_mode) & 0o077 == 0:
            continue
        try:
            os.chmod(str(p), _stat.S_IMODE(st.st_mode) & 0o700)
            changed.append(rel)
        except OSError:
            continue
    return changed


def find_claude() -> Optional[str]:
    """The Claude Code executable to launch, or None.

    The launcher's first step, so it also turns on private_umask: from here on the launcher and a Core
    it spawns create their files private.

    Windows: the native claude.exe first; an npm claude.cmd only when there is nothing else (see
    claude_argv). WSL: Windows programs on the appended Windows PATH (/mnt/c/...) are ignored: they
    run through interop, which drops the session's environment (ANTHROPIC_BASE_URL, the token).
    Relative PATH entries are never searched, so a project's own `claude` is never run.
    """
    private_umask()
    override = os.environ.get("PULSAR_CLAUDE_BIN")
    if override:
        return override if os.path.exists(override) else shutil.which(override)
    home = Path.home()
    if _nt():
        found = _which(("claude.exe",))
        if found:
            return found
        native = home / ".local" / "bin" / "claude.exe"
        if native.is_file():
            return str(native)
        found = _which(("claude.cmd", "claude.bat"))
        if found:
            return found
        npm = home / "AppData" / "Roaming" / "npm" / "claude.cmd"
        return str(npm) if npm.is_file() else None
    wsl = is_wsl()
    found = _which(("claude",), skip=(lambda d: _windows_side(d)) if wsl else (lambda d: False))
    if found:
        return found
    for c in (home / ".local/bin/claude", home / ".claude/local/claude", home / ".claude/bin/claude",
              Path("/usr/local/bin/claude"), Path("/opt/homebrew/bin/claude")):
        if c.is_file() and os.access(str(c), os.X_OK):
            return str(c)
    return None


# cmd.exe interprets these even inside the arguments CreateProcess hands a .cmd/.bat (BatBadBut).
CMD_METACHARS = '&|^%<>"\r\n'
_SHIM_TARGET = re.compile(r'"%~?dp0%?\\?([^"%\r\n]+?\.(?:js|cjs|mjs|exe))"', re.I)


def npm_shim_target(cmd_path: str) -> Optional[List[str]]:
    """[node, cli.js] (or [the .exe]) that an npm `claude.cmd` wraps, so it can run without cmd.exe."""
    try:
        with open(cmd_path, encoding="utf-8", errors="replace") as fh:
            text = fh.read(16384)
    except OSError:
        return None
    base = os.path.dirname(os.path.abspath(cmd_path))
    rels = [m.group(1) for m in _SHIM_TARGET.finditer(text)]
    rels = [r for r in rels if not r.lower().endswith(".exe")] + [r for r in rels if r.lower().endswith(".exe")]
    rels.append(os.path.join("node_modules", "@anthropic-ai", "claude-code", "cli.js"))
    for rel in rels:
        target = os.path.normpath(os.path.join(base, rel.replace("\\", os.sep).replace("/", os.sep)))
        if os.path.basename(target).lower() in ("node.exe", "node"):
            continue                                   # the interpreter the shim prefers, not Claude Code
        try:
            inside = os.path.commonpath([os.path.normcase(target), os.path.normcase(base)]) == os.path.normcase(base)
        except ValueError:
            inside = False
        if not inside or not os.path.isfile(target):
            continue
        if target.lower().endswith(".exe"):
            return [target]
        node = os.path.join(base, "node.exe")
        if not os.path.isfile(node):
            node = _which(("node.exe", "node")) or ""
        if node:
            return [node, target]
    return None


def claude_argv(binary: str, args: Iterable[str]) -> List[str]:
    """argv that runs Claude Code with `args`, never letting cmd.exe read them.

    An npm `claude.cmd` would run through cmd.exe, which expands %VAR% and treats & | < > ^ as commands
    in arguments (CVE-2024-24576 class). So the node + cli.js it wraps is run directly; when that
    cannot be found, arguments with those characters are refused (ValueError, with a clear message).
    """
    args = [str(a) for a in args]
    if not (_nt() and binary.lower().endswith((".cmd", ".bat"))):
        return [binary] + args
    direct = npm_shim_target(binary)
    if direct:
        return direct + args
    bad = sorted({ch for a in args for ch in a if ch in CMD_METACHARS})
    if bad:
        shown = " ".join({"\r": "CR", "\n": "newline"}.get(ch, ch) for ch in bad)
        raise ValueError(
            "Claude Code is installed as a batch file (%s), and Windows runs batch files through cmd.exe, which "
            "would interpret %s in your arguments. Nothing was run. Install the native Claude Code "
            "(irm https://claude.ai/install.ps1 | iex) or leave those characters out." % (binary, shown))
    return [binary] + args


def claude_version(binary: str) -> str:
    try:
        out = subprocess.run(claude_argv(binary, ["--version"]), capture_output=True, text=True, timeout=15,
                             env={**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
        return (out.stdout or out.stderr).strip().split("\n")[0].replace(" (Claude Code)", "")
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""


def shared_config() -> bool:
    """PULSAR_CLAUDE_CONFIG=shared: Claude Code runs with your own configuration, not Pulsar's isolated profile."""
    return os.environ.get("PULSAR_CLAUDE_CONFIG", "").lower() == "shared"


def claude_dir() -> Path:
    if shared_config():
        return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    return K.home() / "claude_config"


def seed_claude_config(directory: Path, approve_key: str = "") -> None:
    """Skip Claude Code's first-run theme wizard inside our isolated profile, and seed its skills (seed_skills).

    The folder-trust prompt is a safety feature and is left alone, and so is the gateway billing notice
    (acknowledge_gateway_notice, for sessions through Pulsar Core only). With PULSAR_CLAUDE_CONFIG=shared
    this does nothing: your own profile is never written.
    """
    if shared_config():
        return
    private_umask()
    if K._inside_home(directory):
        K.ensure_home()                 # never seed a profile into a folder PULSAR_HOME names by mistake
    directory.mkdir(parents=True, exist_ok=True)
    if K._inside_home(directory):
        tighten_home()
    seed_skills(directory)
    path = directory / ".claude.json"
    data: Dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
    changed = False
    if not data.get("hasCompletedOnboarding"):
        data["hasCompletedOnboarding"] = True
        data.setdefault("theme", "dark")
        changed = True
    # `/model` inside Claude Code persists {"model": ...} into settings.json; a stale id from another
    # provider would override the model Pulsar launches with, so clear it in our isolated profile.
    sp = directory / "settings.json"
    try:
        sdata = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else None
        if isinstance(sdata, dict) and "model" in sdata:
            sdata.pop("model")
            K.write_private(sp, json.dumps(sdata, indent=2) + "\n")
    except (OSError, ValueError):
        pass
    if approve_key:
        resp = data.setdefault("customApiKeyResponses", {"approved": [], "rejected": []})
        tail = approve_key[-20:]
        if tail not in resp.setdefault("approved", []):
            resp["approved"].append(tail)
            changed = True
    if changed:
        K.write_private(path, json.dumps(data, indent=2) + "\n")


BILLING_NOTICE = "autoModeClassifierBillingNoticeAcknowledgedAt"


def acknowledge_gateway_notice(directory: Path) -> None:
    """Pre-dismiss Claude Code's gateway billing notice (about its auto-mode classifier, shown in sessions that
    route through a custom base URL) in Pulsar's isolated profile, for sessions through Pulsar Core only.

    The launcher calls it for those alone: where Claude Code reaches a provider itself (Anthropic directly, a
    cloud route, PULSAR_DIRECT=1) the notice may concern your own bill, so it is left for you to read. With
    PULSAR_CLAUDE_CONFIG=shared nothing is written: your own profile is never touched. The acknowledgement
    lasts 24 h and is renewed after 6 h.
    """
    if shared_config():
        return
    path = directory / ".claude.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    now_ms = int(time.time() * 1000)
    try:
        last = int(data.get(BILLING_NOTICE) or 0)
    except (TypeError, ValueError):
        last = 0
    if now_ms - last > 6 * 3600 * 1000:
        data[BILLING_NOTICE] = now_ms
        K.write_private(path, json.dumps(data, indent=2) + "\n")


# PULSAR_SKILLS: skill folders copied into the isolated profile at every launch.
SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
SKILL_MAX_FILES = 2000
SKILL_MAX_BYTES = 64 << 20


def skill_dirs(environ: Optional[Mapping[str, str]] = None) -> List[Path]:
    """PULSAR_SKILLS_DIRS (folders separated like PATH entries), else ~/.claude/skills."""
    env = os.environ if environ is None else environ
    raw = (env.get("PULSAR_SKILLS_DIRS") or "").strip()
    if not raw:
        return [Path.home() / ".claude" / "skills"]
    return [Path(os.path.expanduser(p.strip())) for p in raw.split(os.pathsep) if p.strip()]


def _skill_files(src: Path) -> Optional[List[str]]:
    """Every regular file under `src`, relative; links are skipped. None when over the size limits."""
    out: List[str] = []
    total = 0
    for root, dirs, files in os.walk(str(src)):
        dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(root, d)))
        for name in sorted(files):
            p = os.path.join(root, name)
            if os.path.islink(p) or not os.path.isfile(p):
                continue
            total += os.path.getsize(p)
            out.append(os.path.relpath(p, str(src)))
            if len(out) > SKILL_MAX_FILES or total > SKILL_MAX_BYTES:
                return None
    return out


def _same_file(a: str, b: str) -> bool:
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            return fa.read() == fb.read()
    except OSError:
        return False


def seed_skills(directory: Path, warn: Optional[Callable[[str], None]] = None) -> List[str]:
    """Copy the skill folders PULSAR_SKILLS names (comma separated) into `directory`/skills. Returns them.

    Each name is looked up in the skill_dirs() folders in order and the first one that has it wins. A
    file that changed is copied again at the next launch; nothing already in the profile is removed.
    Links inside a skill are skipped (a skill folder that is itself a link is followed: you named it).
    A name with a path in it, or a skill over SKILL_MAX_FILES files or SKILL_MAX_BYTES, is refused.
    Empty PULSAR_SKILLS (the default): nothing happens. Never called for PULSAR_CLAUDE_CONFIG=shared.
    """
    names = [n.strip() for n in (os.environ.get("PULSAR_SKILLS") or "").split(",") if n.strip()]
    if not names:
        return []
    warn = warn or _default_warn
    dest_root = directory / "skills"
    sources = skill_dirs()
    copied: List[str] = []
    problems: List[str] = []
    for name in names:
        shown = P.clean_text(name, 60)
        if not SKILL_NAME.match(name):
            problems.append("%s (not a folder name)" % shown)
            continue
        src = next((d / name for d in sources if (d / name).is_dir()), None)
        if src is None:
            problems.append("%s (not found)" % shown)
            continue
        files = _skill_files(src)
        dest = dest_root / name
        if files is None:
            problems.append("%s (over %d files or %d MB)" % (shown, SKILL_MAX_FILES, SKILL_MAX_BYTES >> 20))
            continue
        if dest.is_symlink():
            problems.append("%s (the profile's copy is a link)" % shown)
            continue
        try:
            for rel in files:
                s, d = os.path.join(str(src), rel), os.path.join(str(dest), rel)
                if os.path.isfile(d) and _same_file(s, d):
                    continue
                os.makedirs(os.path.dirname(d), exist_ok=True)
                tmp = d + ".pulsar-tmp"
                try:
                    shutil.copyfile(s, tmp)
                    if not _nt():
                        os.chmod(tmp, (os.stat(s).st_mode & 0o700) | 0o600)   # private; scripts stay executable
                    os.replace(tmp, d)
                except OSError:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
        except OSError as exc:
            problems.append("%s (%s)" % (shown, type(exc).__name__))
            continue
        copied.append(name)
    if problems:
        warn("PULSAR_SKILLS: not copied into Pulsar's profile: %s. Skills are looked up in %s." % (
            ", ".join(problems), os.pathsep.join(P.clean_text(str(d), 120) for d in sources)))
    return copied


LEGACY_COMMANDS = ("api.md", "sonar.md", "pick.md", "model.md", "pulsar.md", "pulsar-model.md")
# The command files 2.0.0 wrote into the profile it ran with, which in shared mode was your own
# (~/.claude/commands); 1.x wrote only into its isolated profile. sha256 of their text.
V2_COMMANDS_SHA256 = {
    "pulsar.md": "cf32a9ec989a4dc55775a15b0c3a87f18c2d915860522e4aa766bcb354f906ed",
    "pulsar-model.md": "1c973db6a0f8fee877c1e9168c55c7a9651aa240dec9d1888f0168f2ef558e5a",
}
LEGACY_MARK = "Managed by pulsarcode"


def install_commands(directory: Path) -> List[str]:
    """v3 ships its slash commands in the Pulsar plugin (/pulsar:*); remove the v1/v2 copies Pulsar wrote.

    In Pulsar's isolated profile every legacy command file that carries the LEGACY_MARK line goes. In a
    shared profile (your own) only a file that is exactly what 2.0.0 wrote there goes; anything else stays,
    an edited copy included. Returns the names left in place that still carry the line.
    """
    shared = shared_config()
    d = directory / "commands"
    kept: List[str] = []
    for legacy in LEGACY_COMMANDS:
        f = d / legacy
        try:
            if f.is_symlink() or not f.is_file():
                continue
            text = f.read_text(encoding="utf-8")
            if LEGACY_MARK not in text:
                continue
            if shared and hashlib.sha256(text.encode("utf-8")).hexdigest() != V2_COMMANDS_SHA256.get(legacy):
                kept.append(legacy)
                continue
            f.unlink()
        except (OSError, ValueError):
            pass
    return kept


# ---------------------------------------------------------------------------
# Pulsar Core sessions
# ---------------------------------------------------------------------------

def target_for(prof: K.Profile, model: Optional[str] = None) -> ST.Target:
    return ST.Target(profile=prof.id, provider=prof.provider, model=prof.model if model is None else model,
                     base_url=prof.base_url, native=prof.native, context=prof.context)


def placeholder_url(prov: P.Provider) -> bool:
    """True for a provider that needs your own resource URL (Azure, Foundry) but has none saved: empty, the
    registry placeholder (https://YOUR-RESOURCE...) or the registry's own default base. Never contacted."""
    if not prov.needs_url:
        return False
    default = P.BY_ID.get(prov.id)
    base = (prov.base_url or "").strip().rstrip("/")
    return (not base or "YOUR-RESOURCE" in base.upper()
            or (default is not None and base == (default.base_url or "").strip().rstrip("/")))


def cloud_registry(prof: K.Profile) -> bool:
    """A cloud provider from Pulsar's registry: not a custom endpoint, not a local server, and no URL of
    it on this machine or the local network (a registry entry pointed at a LAN host counts as local)."""
    if prof.provider == "custom":
        return False
    try:
        prov = prof.resolve()
    except KeyError:
        return False
    return not prov.local and not any(P.is_local_url(u) for u in (prov.base_url, prov.native_base()) if u)


def _relay_pref(state: K.State) -> object:
    """prefs["relay"] as relay_plan reads it ("off" when PULSAR_RELAY turns it off for this shell)."""
    env_pref = os.environ.get("PULSAR_RELAY", "").strip().lower()
    if env_pref in ("off", "none", "0"):
        return "off"
    return state.prefs.get("relay", env_pref or "auto")


def relay_plan(state: K.State, primary: K.Profile, key_of, pilot: Optional[bool] = None) -> List[ST.Target]:
    """The relay chain after the primary target.

    prefs["relay"]: "auto" (default) = up to two other connected cloud providers from the registry, and
    only when the primary is one too: a local server, a private-range host or a custom endpoint never
    relays automatically (its prompts would go to a provider nobody chose for them). "off" = none; or a
    list of "profile:model" strings, the explicit chain `pulsarcode relay` saves, used whatever the
    primary. PULSAR_RELAY=off (or none, 0) turns the relay off for that shell even after `pulsarcode
    relay` saved a choice; other PULSAR_RELAY values only apply when nothing was saved.

    A session with Pilot armed never relays, explicit chain included: its screenshots go only to the
    model you chose. pilot: whether this launch arms Pilot (None: read the environment and prefs).
    """
    if pilot is None:
        pilot = pilot_env().get("PULSAR_PILOT") == "1"
    if pilot:
        return []
    pref = _relay_pref(state)
    out: List[ST.Target] = []
    if pref in ("off", "none", "0", False):
        return out
    if isinstance(pref, list):
        for item in pref:
            pid, _, model = str(item).partition(":")
            prof = state.profiles.get(pid)
            if prof is None or (pid, model) == (primary.id, primary.model):
                continue
            try:
                prov = prof.resolve()
            except KeyError:
                continue
            if prov.route == "cloud":
                continue                      # Core cannot reach cloud routes; Claude Code speaks to them itself
            if placeholder_url(prov):
                continue                      # Azure/Foundry without a resource URL: nothing to send it to
            if transport_problem(prov, key_of(pid) or ""):
                continue
            if model and not P.valid_model_id(model):
                continue
            out.append(target_for(prof, model or prof.model))
        return out[:4]
    if not cloud_registry(primary):
        return out
    for pid, prof in state.profiles.items():
        if pid == primary.id or not prof.model or not cloud_registry(prof):
            continue
        try:
            prov = prof.resolve()
        except KeyError:
            continue
        if prov.local or prov.route == "cloud" or (prov.needs_key and not key_of(pid)) or placeholder_url(prov):
            continue
        if transport_problem(prov, key_of(pid) or ""):
            continue                          # never relay a key over plain http to a public host
        out.append(target_for(prof))
        if len(out) >= 2:
            break
    return out


def _target_profile(t: ST.Target) -> K.Profile:
    return K.Profile(id=t.profile, provider=t.provider, model=t.model, base_url=t.base_url, native=t.native,
                     context=t.context)


def switch_plan(state: K.State, plan: List[ST.Target], target: ST.Target, pilot: bool = False) -> List[ST.Target]:
    """A running session's plan after a switch to `target` (`pulsarcode switch`, pulsar_switch).

    The targets it had stay on as relays when relay_plan's rules allow them: in "auto" mode only cloud
    registry providers, and only behind a cloud registry primary; with an explicit chain, the targets of
    that chain; with the relay off, or Pilot armed (pilot), none.
    """
    rest = [t for t in plan if t.key() != target.key()]
    pref = _relay_pref(state)
    if pilot or pref in ("off", "none", "0", False):
        keep: List[ST.Target] = []
    elif isinstance(pref, list):
        chain = [str(item).partition(":") for item in pref]
        keep = [t for t in rest if any(t.profile == pid and (not model or t.model == model)
                                       for pid, _, model in chain)]
    elif cloud_registry(_target_profile(target)):
        keep = [t for t in rest if cloud_registry(_target_profile(t))]
    else:
        keep = []
    return ([target] + keep)[:5]


def transport_problem(prov: P.Provider, key: str, environ: Optional[Dict[str, str]] = None) -> str:
    """Why `key` must not go to this provider's endpoint (plain http to a public host), else ""."""
    if not key or prov.route == "cloud":
        return ""
    for url in (prov.base_url, prov.native_base() if (prov.route == "native" or prov.anthropic_native_url) else ""):
        why = P.plain_http_refusal(url, key, environ)
        if why:
            return why
    return ""


def session_dir(sid: str) -> Path:
    """run/sessions/<sid>/: this session's own files (settings.json, the plugin it loaded)."""
    return ST.session_path(sid).with_suffix("")


def reap_session_dirs(grace: float = 30.0, now: Optional[float] = None) -> List[str]:
    """Delete the per-session folders of sessions that are gone. Returns their ids.

    Gone: the session file names a dead launcher (and is older than `grace`), or the file itself was
    already reaped by Core and the folder is older than `grace`. Called at every launch; Core's reaper
    removes the session files, this removes what the launcher wrote beside them.
    """
    now = time.time() if now is None else now
    try:
        entries = list(ST.sessions_dir().iterdir())
    except OSError:
        return []
    removed: List[str] = []
    for d in entries:
        try:
            if d.is_symlink() or not d.is_dir():
                continue
            sess = ST.load_session(d.name)
            if sess is not None:
                if ST.pid_alive(sess.pid) or now - sess.created <= grace:
                    continue
            elif now - d.stat().st_mtime <= grace:
                continue                      # just created by a launch that is still registering
            shutil.rmtree(str(d), ignore_errors=True)
            removed.append(d.name)
        except OSError:
            continue
    return removed


def _hand_keys(info: ST.CoreInfo, keys: Dict[str, str]) -> Dict[str, str]:
    """Give Core the keys that exist only in this shell's environment (in memory). {profile: why} for failures.

    The admin client makes Core prove it holds the admin token before anything is sent.
    """
    from pulsar.core.client import CoreClient, CoreUnavailable
    from pulsar.redact import redact
    admin = CoreClient(info.url, info.admin_token)
    failed: Dict[str, str] = {}
    for profile, key in keys.items():
        for attempt in range(2):
            try:
                admin.post("/pulsar/ephemeral-key", {"profile": profile, "key": key}, timeout=3)
                failed.pop(profile, None)
                break
            except CoreUnavailable as exc:
                failed[profile] = P.clean_text(redact(str(exc), [key, info.admin_token]), 160) or "no answer"
                if attempt == 0:
                    time.sleep(0.2)
    return failed


def _default_warn(msg: str) -> None:
    try:
        sys.stderr.write("pulsarcode: warning: " + msg + "\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


def default_route(primary: K.Profile, environ: Optional[Mapping[str, str]] = None) -> str:
    """The route a launch of `primary` takes, as the launcher decides it: "cloud" for a cloud provider,
    "direct" for an Anthropic-native one with PULSAR_DIRECT=1, else "core"."""
    env = os.environ if environ is None else environ
    try:
        prov = primary.resolve()
    except KeyError:
        return "core"
    if prov.route == "cloud":
        return "cloud"
    if env.get("PULSAR_DIRECT") == "1" and prov.route == "native":
        return "direct"
    return "core"


def start_session(state: K.State, primary: K.Profile, key_of, label: str = "",
                  warn: Optional[Callable[[str], None]] = None, route: str = "") -> "tuple[ST.CoreInfo, ST.Session, str]":
    """Attach to (or start) Pulsar Core and register a session for this launch.

    route: "core", "direct" or "cloud" (default_route when empty), recorded as flags["route"]. On direct and
    cloud routes Claude Code talks to the provider itself and the session is a side channel only: its plan
    is the primary alone (no relay), and Core refuses its token on /v1/messages and count_tokens.

    Keys found only in this shell's environment go to Core in memory (not for a cloud target, which Core
    never calls). If Core does not take one, a single warning names the profiles (never the key): their
    requests would fail otherwise without a word. Before returning, Core is proven again (the same
    admin-token challenge): the URL handed to Claude Code must still be this user's Core, not whatever
    took its port meanwhile.
    """
    from pulsar.core import spawn
    warn = warn or _default_warn
    route = route if route in ("core", "direct", "cloud") else default_route(primary)
    private_umask()
    try:
        tighten_home()
    except Exception:
        pass
    info = spawn.ensure_core()
    try:
        reap_session_dirs()
    except Exception:
        pass
    pilot = pilot_env().get("PULSAR_PILOT") == "1"
    plan = [target_for(primary)]
    if route == "core":
        plan += relay_plan(state, primary, key_of, pilot=pilot)
    flags = {
        "relay": route == "core" and not pilot,   # never a side-channel route, never a Pilot session
        "loopguard": os.environ.get("PULSAR_LOOPGUARD", "1") != "0",
        "stage": True,
        "council": True,
        "pilot": pilot,
        # This launch's own PULSAR_ALLOW_HTTP=1 (Core's environment may differ), and only for this session.
        "allow_http": P.http_allowed(),
        "route": route,                # Session.side_channel: Core's gateway refuses a direct or cloud session
    }
    sess, token = ST.new_session(plan, flags, label=label, cwd=os.getcwd())
    # Keys that exist only in this shell's environment reach Core in memory, never on disk.
    stored = K.load_keys()
    env_keys: Dict[str, str] = {}
    for t in plan if route != "cloud" else []:
        if t.profile not in stored and t.profile not in env_keys:
            key = key_of(t.profile)
            if key:
                env_keys[t.profile] = key
    failed = _hand_keys(info, env_keys) if env_keys else {}
    # Re-verify: still the proven Core of run/core.json? (It may have exited, or another launch replaced it.)
    again = spawn.attach()
    if again is None:
        again = spawn.ensure_core(prefer_port=info.port)     # RuntimeError when no proven Core can be had
    if (again.url, again.admin_token) != (info.url, info.admin_token):
        info = again                                         # the session file is on disk: the new Core sees it
        failed = _hand_keys(info, env_keys) if env_keys else {}
    if failed:
        names = ", ".join(P.clean_text(p, 40) for p in failed)
        why = next(iter(failed.values()))
        primary_hit = primary.id in failed
        warn("Pulsar Core did not take the key for %s from your environment (%s). %s will fail until you store "
             "it: pulsarcode key --from-env <VAR>." % (
                 names, why, "This session's model" if primary_hit else "Relays to %s" % names))
    return info, sess, token


def stop_core(force: bool = False) -> bool:
    """Stop the Core in run/core.json (spawn.stop_core). By default it retires: it exits once no live session
    needs its port any more. force: now, live sessions or not."""
    from pulsar.core import spawn
    return spawn.stop_core(force=force)


# ---------------------------------------------------------------------------
# Environment + exec
# ---------------------------------------------------------------------------

PILOT_AUTO = "PULSAR_PILOT_AUTO"
PILOT_ARMED_AT = "PULSAR_PILOT_ARMED_AT"   # when this launch armed Pilot: a later `pilot off` wins (pilot.safety)
_SESSION_MARKERS = ("PULSAR_SESSION", "CLAUDECODE", "PULSAR_SESSION_TOKEN")


def _inside_session(env: Mapping[str, str]) -> bool:
    return any(env.get(v) for v in _SESSION_MARKERS)


def _process_table() -> Dict[int, Tuple[int, str]]:
    """{pid: (parent pid, command line)} from /proc or /bin/ps; {} where neither can be read (Windows)."""
    table: Dict[int, Tuple[int, str]] = {}
    if _nt():
        return table
    if os.path.isdir("/proc/self"):
        try:
            names = os.listdir("/proc")
        except OSError:
            names = []
        for name in names:
            if not name.isdigit():
                continue
            try:
                with open("/proc/%s/stat" % name, "rb") as fh:
                    ppid = int(fh.read().decode("ascii", "replace").rsplit(")", 1)[1].split()[1])
                with open("/proc/%s/cmdline" % name, "rb") as fh:
                    args = fh.read(4096).replace(b"\0", b" ").decode("utf-8", "replace").strip()
            except (OSError, ValueError, IndexError):
                continue
            table[int(name)] = (ppid, args)
        return table
    if not os.path.isfile("/bin/ps"):
        return table
    try:
        out = subprocess.run(["/bin/ps", "-A", "-o", "pid=,ppid=,args="], capture_output=True, text=True,
                             timeout=5, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}).stdout
    except (OSError, subprocess.SubprocessError):
        return table
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), parts[2] if len(parts) > 2 else "")
    return table


def _is_claude_code(args: str) -> bool:
    """A command line of Claude Code: its binary (any case: it sets its own process title), or Node or Bun
    running it. A desktop app bundle named Claude is not it."""
    words = args.split()
    if not words:
        return False
    first = os.path.basename(words[0]).lower()
    second = os.path.basename(words[1]).lower() if len(words) > 1 else ""
    return (first == "claude" and ".app/" not in words[0]) or "@anthropic-ai/claude-code" in args or (
        first in ("node", "bun") and second == "claude")


def claude_ancestor(table: Optional[Dict[int, Tuple[int, str]]] = None, pid: Optional[int] = None) -> bool:
    """True when a Claude Code process is among this process's ancestors (POSIX).

    The model's Bash tool runs as a descendant of Claude Code: a shell that unsets the session markers
    and fakes a terminal is still caught here, unless it also detached from the process tree (setsid and a
    double fork). So this is one more check for the "a person typed this" gates, not the boundary: the
    boundary is Claude Code's own permission prompt before it runs a Bash command.
    """
    table = _process_table() if table is None else table
    current = os.getpid() if pid is None else pid
    for _ in range(64):
        entry = table.get(current)
        if entry is None:
            return False
        parent = entry[0]
        if parent <= 1 or parent == current:
            return False
        if _is_claude_code(table.get(parent, (0, ""))[1]):
            return True
        current = parent
    return False


def _human_terminal() -> bool:
    """stdin and stdout are both terminals: a person typed this launch (the model's Bash tool has none)."""
    try:
        return sys.stdin is not None and sys.stdout is not None and sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError, OSError):
        return False


def pilot_env(original: Optional[Mapping[str, str]] = None,
              prefs: Optional[Dict[str, object]] = None) -> Dict[str, str]:
    """PULSAR_PILOT for the launched session (the MCP server registers pilot_* only when it is "1").

    Armed with `pulsarcode pilot on`: what pulsar.pilot.safety.session_env() returns ({"PULSAR_PILOT":
    "1"}), plus PULSAR_PILOT_AUTO=1 marking the value as the launcher's own. Disarmed: nothing, so a
    PULSAR_PILOT inherited from an outer Pulsar session (it carries that marker) is dropped.
    PULSAR_PILOT=0 (or any value but "1") set for this launch is always kept: it can only turn Pilot off.
    PULSAR_PILOT=1 set for this launch is a request: it arms Pilot only when a person started the
    launch on a terminal (stdin and stdout are TTYs) outside any Claude Code session, with no Claude
    Code process among its ancestors. The model's Bash tool has no TTY and carries the session markers;
    this is defence in depth, not a hard boundary (a command that unsets the markers, fakes a pty and
    detaches from the process tree could pass once the user approved it in Claude Code), and the hard
    switch remains `pulsarcode pilot on` / `off`, which needs the prefs file.
    """
    env = os.environ if original is None else original
    value = (env.get("PULSAR_PILOT") or "").strip()
    inherited = value == "1" and env.get(PILOT_AUTO) == "1"      # the marker vouches for a "1" only
    armed = {PILOT_ARMED_AT: "%.3f" % time.time()}
    if value and value != "1":
        return {"PULSAR_PILOT": value}
    if value == "1" and not inherited and not _inside_session(env) and _human_terminal() and not claude_ancestor():
        return dict(armed, PULSAR_PILOT="1")
    clean = {k: v for k, v in env.items() if k not in ("PULSAR_PILOT", PILOT_AUTO, PILOT_ARMED_AT)}
    try:
        from pulsar.pilot import safety
        on = safety.state(env=clean, prefs=prefs)[0]     # == safety.session_env() without the inherited flag
    except Exception:
        on = False
    return dict(armed, PULSAR_PILOT="1", **{PILOT_AUTO: "1"}) if on else {}


# The context window Claude Code works with for a Pulsar model (read from the Claude Code 2.1.288 binary
# and checked by tools/compat/run.py, check context_effective).
#
# Every model row Pulsar offers carries behavesAs (pulsar.plugin.BEHAVES_AS, claude-sonnet-4-5), so Claude
# Code handles the model as that Claude model: its window is that model's 200k, and
# CLAUDE_CODE_MAX_CONTEXT_TOKENS is only read for models Claude Code does not know. behavesAs has to stay:
# for an unknown model Claude Code turns auto mode on, and its safety classifier (which then runs on the
# custom model) blocks every Bash call. So the window is set the ways Claude Code offers for a known model:
#
#   >= 1M        the "[1m]" suffix on the model id: Claude Code's 1M window. Core strips the suffix and
#                never forwards the 1M beta header to a provider other than Anthropic. Windows above 1M
#                are used as 1M.
#   200k to 1M   nothing: 200k is the largest window Claude Code can be given that does not exceed it.
#   < 200k       Claude Code still reports 200k (it has no smaller window for a known model), so
#                auto-compact is moved to the real window instead: CLAUDE_CODE_AUTO_COMPACT_WINDOW (Claude
#                Code floors it at 100k), below 100k CLAUDE_AUTOCOMPACT_PCT_OVERRIDE puts the compaction
#                threshold where the real window needs it, and CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE stops a
#                request before it can exceed the window. Neither ever goes below PREFIX_ROOM: Claude
#                Code's own fixed prompt prefix (system prompt and tool definitions) is about 26.5k tokens
#                through Pulsar, and a threshold under it makes every turn a failed compaction. Windows
#                below SMALLEST_TUNED are left untuned (Core's prompt-too-long rewrite handles them).
#
# The env tuning is session-wide: after /model to a row with another window it stays that of the launch
# model (smaller is safe; Core's prompt-too-long rewrite still makes Claude Code compact when it is not).
CLAUDE_WINDOW = 200_000            # Claude Code's window for the model behavesAs names
CLAUDE_LONG_WINDOW = 1_000_000     # ... with the [1m] suffix
LONG_SUFFIX = "[1m]"
COMPACT_FLOOR = 100_000            # the smallest CLAUDE_CODE_AUTO_COMPACT_WINDOW Claude Code accepts
OUTPUT_RESERVE = 20_000            # Claude Code keeps min(max output tokens, 20k) free for the answer
COMPACT_BUFFER = 13_000            # ... and compacts this far below that
BLOCKING_BUFFER = 3_000            # ... and refuses to send this far below it
PREFIX_ROOM = 30_000               # Claude Code's fixed prompt prefix (~26.5k) plus room for one turn
SMALLEST_TUNED = 32_000            # below this the prefix leaves no room to tune anything
COMPACT_VARS = ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
                "CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE")
COMPACT_MARK = "PULSAR_COMPACT_VARS"   # which COMPACT_VARS Pulsar set (a nested launch drops them)
# Below a 1M window (and wherever Claude Code talks to the provider itself) Claude Code's 1M handling is
# turned off: its /model "Default" row resolves to the opus model variable plus "[1m]" whenever the
# behavesAs model supports the 1M beta, so picking Default would assume a 1M window for a 131K model.
LONG_OFF = "CLAUDE_CODE_DISABLE_1M_CONTEXT"
LONG_OFF_MARK = "PULSAR_1M_OFF"        # set when LONG_OFF is Pulsar's (a nested launch drops it)


def claude_model_id(model: str, context: int, long_ok: bool = True) -> str:
    """The model id Claude Code gets: `model` plus "[1m]" for a window of 1M or more (see above).

    Never for Claude's own ids (Claude Code knows their windows) or an id that already has a suffix.
    long_ok: False on routes where Claude Code talks to the provider itself (the 1M beta header would
    reach it).
    """
    if not (long_ok and model and context >= CLAUDE_LONG_WINDOW):
        return model
    low = model.lower()
    if low.startswith("claude-") or re.search(r"\[\d+[km]\]$", low):
        return model
    return model + LONG_SUFFIX


def _positive(value: Optional[str], kind: type = int) -> Optional[float]:
    try:
        n = kind(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def compact_limits(window: int, answer: int = OUTPUT_RESERVE) -> Tuple[int, int]:
    """(auto-compact threshold, blocking limit) for a window below 200k.

    Claude Code's own rule (window minus the answer reserve minus 13k, and minus 3k) scaled down for small
    windows: the answer reserve shrinks to a fifth of the window (all of `answer` from 100k up), and
    neither value goes below PREFIX_ROOM (a blocking limit that would end up at or under the threshold
    moves halfway between threshold and window).
    """
    reserve = min(answer, window // 5)
    threshold = max(window - reserve - min(COMPACT_BUFFER, window // 8), PREFIX_ROOM)
    blocking = window - reserve - min(BLOCKING_BUFFER, window // 32)
    if blocking <= threshold:
        blocking = threshold + (window - threshold) // 2
    return threshold, blocking


def compact_env(context: int, base: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Auto-compact variables that keep Claude Code inside a window below its own 200k (see above).

    {} for 200k and more. A value already in `base` (the user's own) is kept when it is the safer one.
    """
    if not context or context >= CLAUDE_WINDOW or context < SMALLEST_TUNED:
        return {}
    base = base or {}
    window = int(context)
    # Claude Code keeps min(its max output tokens, 20k) free; it can only be more than `answer` (when it
    # ignores CLAUDE_CODE_MAX_OUTPUT_TOKENS), which moves its thresholds lower, never higher.
    answer = int(min(OUTPUT_RESERVE, _positive(base.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS")) or OUTPUT_RESERVE))
    threshold, blocking = compact_limits(window, answer)
    auto_window = max(window, COMPACT_FLOOR)
    out: Dict[str, str] = {}
    mine = _positive(base.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW"))
    out["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(int(min(auto_window, mine or auto_window)))
    if auto_window > window:
        # Claude Code compacts at min(floor(W' * pct / 100), W' - 13k), W' = its window minus its answer reserve.
        pct = max(0.01, int(threshold * 10000 // (auto_window - answer)) / 100.0)
        theirs = _positive(base.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"), float)
        if theirs is not None and theirs <= 100:
            pct = min(pct, theirs)
        out["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = ("%.2f" % pct).rstrip("0").rstrip(".")
    mine = _positive(base.get("CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE"))
    out["CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE"] = str(int(min(blocking, mine or blocking)))
    return out


def _same_path(a: str, b: str) -> bool:
    def norm(p: str) -> str:
        return os.path.normcase(os.path.realpath(os.path.expanduser(p)))
    try:
        return norm(a) == norm(b)
    except (OSError, ValueError):
        return False


def pilot_ignored(original: Optional[Mapping[str, str]] = None, launched: Optional[Mapping[str, str]] = None) -> bool:
    """True when a PULSAR_PILOT=1 set for this launch was not honoured (worth a note)."""
    env = os.environ if original is None else original
    return ((env.get("PULSAR_PILOT") or "").strip() == "1" and env.get(PILOT_AUTO) != "1"
            and (launched or {}).get("PULSAR_PILOT") != "1")


def build_env(prov: P.Provider, key: str, model: str, context: int, core: Optional[ST.CoreInfo] = None,
              session: Optional[ST.Session] = None, token: str = "", base: Optional[Dict[str, str]] = None,
              options: Optional[Dict[str, str]] = None, route: str = "",
              secrets: Iterable[str] = ()) -> Dict[str, str]:
    """Environment for Claude Code.

    route "core" (the default when Core is given): Claude Code only ever sees Core's URL and this
    session's token; the real provider key stays inside Core.
    route "cloud": Bedrock, Vertex or a Claude subscription token, which Claude Code speaks to itself.
    route "direct": native providers straight from Claude Code (PULSAR_DIRECT=1).
    With Core running, cloud and direct sessions still get it as a side channel (Stage, Council,
    status line) through PULSAR_SESSION_TOKEN, which grants nothing beyond this session on loopback.

    Provider keys exported in your shell (every variable in provider_key_vars(), and any variable
    holding `key`, one of `secrets` or the value of a provider variable) never reach Claude Code, its
    Bash tool, hooks or MCP servers: Core already has them. Only the credential the route itself needs
    is set again below. PULSAR_KEEP_ENV_KEYS=1 keeps them (for tools you run inside Claude Code that need them).

    CLAUDECODE is kept when an outer Claude Code session set it, so Claude Code's own guard against
    nested sessions still applies to a launch from inside a session.
    """
    route = route or ("cloud" if prov.route == "cloud" else "core" if core is not None else "direct")
    original = dict(base if base is not None else os.environ)
    env = dict(original)
    for var in SCRUB:
        env.pop(var, None)
    # Auto-compact tuning a Pulsar session around this launch set for its own window (a nested launch).
    for var in (original.get(COMPACT_MARK) or "").split(","):
        if var in COMPACT_VARS:
            env.pop(var, None)
    env.pop(COMPACT_MARK, None)
    if original.get(LONG_OFF_MARK) == "1":
        env.pop(LONG_OFF, None)
    env.pop(LONG_OFF_MARK, None)
    if original.get("PULSAR_KEEP_ENV_KEYS") != "1":
        # By value too: the same key copied under another name (MY_ROUTER=..., TOKEN="Bearer ...") is
        # still a key. Collected before the provider variables are dropped.
        values = _secret_values(original, [key, *secrets])
        for var in provider_key_vars():
            env.pop(var, None)
        for var in [v for v, val in env.items() if val and v not in KEEP_ALWAYS and any(s in val for s in values)]:
            env.pop(var, None)
    if model and not P.valid_model_id(model):
        raise ValueError("refusing model id %r: %s" % (P.clean_text(model, 60), P.MODEL_ID_RULE))
    if route in ("core", "direct"):
        why = transport_problem(prov, key, original)
        if why:
            raise ValueError(why)
    if shared_config():
        # Your own configuration: CLAUDE_CONFIG_DIR stays only when you set it (Claude Code reads
        # ~/.claude.json beside ~/.claude only while it is unset). An outer Pulsar session's isolated
        # profile is not yours.
        mine = original.get("CLAUDE_CONFIG_DIR") or ""
        if not mine or _same_path(mine, str(K.home() / "claude_config")):
            env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = str(claude_dir())
    env.pop("PULSAR_PILOT", None)
    env.pop(PILOT_AUTO, None)
    env.pop(PILOT_ARMED_AT, None)
    env.update(pilot_env(original))
    if core is not None:
        env["PULSAR_CORE"] = core.url
        if session is not None:
            env["PULSAR_SESSION"] = session.sid
    if route == "core":
        if core is None:
            raise ValueError("route core needs a running Pulsar Core")
        env["ANTHROPIC_BASE_URL"] = core.url
        env["ANTHROPIC_AUTH_TOKEN"] = token
    elif route == "cloud":
        env.update(P.cloud_env(prov, key, options or {}, environ=original))
        if token:
            env["PULSAR_SESSION_TOKEN"] = token
    elif prov.id == "anthropic":
        env["ANTHROPIC_API_KEY"] = key
        if token:
            env["PULSAR_SESSION_TOKEN"] = token
    elif prov.route == "native":
        env["ANTHROPIC_BASE_URL"] = prov.native_base() or prov.base_url
        env["ANTHROPIC_AUTH_TOKEN"] = key or "pulsar-local"
        if token:
            env["PULSAR_SESSION_TOKEN"] = token
    else:
        raise ValueError("%s needs Pulsar Core (OpenAI-dialect provider)" % prov.name)
    if prov.id == "anthropic" or route == "cloud":
        if model:
            env["ANTHROPIC_MODEL"] = model
    else:
        window = context or DEFAULT_CONTEXT
        if model:
            for var in MODEL_VARS:
                env[var] = claude_model_id(model, window, long_ok=route == "core")
        env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(window)
        tuned = compact_env(window, env)
        if tuned:
            env.update(tuned)
            env[COMPACT_MARK] = ",".join(sorted(tuned))
        if (window < CLAUDE_LONG_WINDOW or route != "core") and not env.get(LONG_OFF):
            env[LONG_OFF] = env[LONG_OFF_MARK] = "1"
        # Non-Anthropic upstreams reject Claude Code's experimental request fields (context_management, ...).
        env.setdefault("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS", "1")
    no_proxy = [p for p in (env.get("NO_PROXY") or env.get("no_proxy") or "").split(",") if p]
    for h in ("127.0.0.1", "localhost", "::1"):
        if h not in no_proxy:
            no_proxy.append(h)
    env["NO_PROXY"] = env["no_proxy"] = ",".join(no_proxy)
    env.setdefault("API_TIMEOUT_MS", "900000")
    for flag in TRAFFIC_FLAGS:
        env[flag] = "1"
    if os.environ.get("PULSAR_CLAUDE_AUTOUPDATE") != "1":
        # DISABLE_AUTOUPDATER, and no fetch of the official plugin marketplace (downloads.claude.ai) into
        # Pulsar's profile either: no egress on a local model, and no marketplace added behind the user's back.
        for flag in UPDATE_FLAGS:
            env[flag] = "1"
    env["PULSAR_ROUTE"] = route
    env["PULSARCODE"] = __version__
    return env


def run_child(argv: List[str], env: Dict[str, str], popen=subprocess.Popen) -> int:
    """Run Claude Code as a child and return its exit code (Windows, where there is no exec).

    A console Ctrl+C reaches every process attached to the console. Claude Code handles it itself
    (interrupt the model, clear the input); the launcher must not: subprocess.call() would catch the
    KeyboardInterrupt and kill Claude Code. So SIGINT is ignored here while the child runs, the wait
    is retried if an interrupt still lands, and the previous handler is restored afterwards.
    """
    import signal
    proc = popen(argv, env=env)
    try:
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (ValueError, OSError):        # not the main thread
        previous = None
    try:
        while True:
            try:
                return proc.wait()
            except KeyboardInterrupt:
                continue
    finally:
        if previous is not None:
            try:
                signal.signal(signal.SIGINT, previous)
            except (ValueError, OSError):
                pass


class LaunchRefused(ValueError):
    """Claude Code must not start: its session could not be pinned (the message says why, never a secret)."""


def settings_args(args: List[str]) -> List[Tuple[int, int, str]]:
    """Every `--settings X` / `--settings=X` among Claude Code's arguments: (index, count of argv items, X).

    Scanning stops at a bare "--" (what follows is not an option)."""
    out: List[Tuple[int, int, str]] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            break
        if a == "--settings" and i + 1 < len(args):
            out.append((i, 2, args[i + 1]))
            i += 2
            continue
        if a.startswith("--settings="):
            out.append((i, 1, a[len("--settings="):]))
        i += 1
    return out


def setting_sources(args: List[str]) -> Optional[List[str]]:
    """The sources a `--setting-sources a,b` among Claude Code's arguments keeps (the last one), else None."""
    found: Optional[List[str]] = None
    for i, a in enumerate(args):
        if a == "--":
            break
        value = args[i + 1] if a == "--setting-sources" and i + 1 < len(args) else (
            a[len("--setting-sources="):] if a.startswith("--setting-sources=") else None)
        if value is not None:
            found = [s.strip().lower() for s in value.split(",") if s.strip()]
    return found


def keep_long_rows(env: Dict[str, str], settings: Optional[Mapping[str, object]]) -> bool:
    """Turn Pulsar's own CLAUDE_CODE_DISABLE_1M_CONTEXT back off when /model offers a 1M row. True if it did.

    That flag hides every "[1m]" row of the picker, so it stays only while no row has a 1M window. Without
    it the Default row still resolves to the launched model as given (ANTHROPIC_DEFAULT_MODEL, no "[1m]"
    upgrade). Never touches a CLAUDE_CODE_DISABLE_1M_CONTEXT the user set, or the direct and cloud routes.
    """
    if env.get(LONG_OFF_MARK) != "1" or env.get("PULSAR_ROUTE", "core") != "core" \
            or not env.get("ANTHROPIC_DEFAULT_MODEL") or env["ANTHROPIC_DEFAULT_MODEL"].endswith(LONG_SUFFIX):
        return False
    picker = (settings or {}).get("modelPicker")
    rows = picker.get("options") if isinstance(picker, dict) else None
    if not isinstance(rows, list) or not any(isinstance(r, dict) and str(r.get("model") or "").endswith(LONG_SUFFIX)
                                             for r in rows):
        return False
    env.pop(LONG_OFF, None)
    env.pop(LONG_OFF_MARK, None)
    return True


class PinPlan(NamedTuple):
    """check_pins' decision for one launch, applied by apply_pins (exec_claude) without deciding again."""
    report: Any                                   # plugin.PinReport
    merged: Dict[str, Any]                        # the one settings file Claude Code gets
    path: Path                                    # where it is written
    found: List[Tuple[int, int, str]]             # every --settings among the arguments (settings_args)
    pulsar_at: Optional[Tuple[int, int, str]]     # Pulsar's own among them
    args: Tuple[str, ...]                         # the arguments it was decided for


def _refusal(report: Any) -> str:
    """Why Claude Code must not start for this pin report, or "" (direct and cloud routes only refuse)."""
    if report.swapped:
        one = "it" if len(report.swapped) == 1 else "them"
        if report.route == "direct":
            hint = ", or launch without PULSAR_DIRECT=1 (Pulsar Core then holds the key and overrides %s)" % one
        elif all(n in _plugin().SECRET_VARS for n in report.swapped):
            hint = ""
        else:
            hint = "; to use %s on purpose, set %s in your own shell or Claude user settings instead" % (one, one)
        return ("this project's .claude settings set %s, which would replace the credential this session uses for "
                "%s. Claude Code was not started. Remove %s from the project's .claude/settings.json or "
                "settings.local.json%s." % (", ".join(report.swapped[:8]), report.target, one, hint))
    if report.unreadable and report.route in ("direct", "cloud"):
        many = len(report.unreadable) > 1
        hint = ", or launch without PULSAR_DIRECT=1 (Pulsar Core then holds the key)" if report.route == "direct" else ""
        return ("Pulsar cannot read this project's .claude/%s, which Claude Code may still apply, so it cannot "
                "check whether %s the credential this session uses for %s. Claude Code was not started. Fix or "
                "remove %s%s." % (" and ".join(report.unreadable), "they replace" if many else "it replaces",
                                  report.target, "them" if many else "it", hint))
    return ""


def _plugin():
    from pulsar import plugin
    return plugin


def check_pins(args: List[str], env: Dict[str, str], cwd: Optional[str] = None) -> Optional[PinPlan]:
    """Decide how this launch's settings are pinned (plugin.pin_report), without writing or printing anything.

    The launcher calls it before the WARP banner, so a refused launch never looks like a started one; then
    exec_claude applies the result (apply_pins). Raises LaunchRefused when a user's --settings cannot be read,
    when the project would replace the credential of a direct or cloud route, or when a project settings file
    is there but cannot be read on those routes (Claude Code may apply it, Pulsar cannot check it). `env` may
    change (keep_long_rows). None for an environment that is not a Pulsar session's.
    """
    plugin = _plugin()
    cwd = cwd if cwd is not None else os.getcwd()
    found = settings_args(args)
    pulsar_at: Optional[Tuple[int, int, str]] = None
    base: Optional[Dict[str, Any]] = None
    for item in found:
        data = plugin.read_pulsar_settings(item[2])
        if data is not None:
            pulsar_at, base = item, data
            break
    if pulsar_at is None and not (env.get("PULSAR_SESSION") or env.get("PULSAR_CORE") or env.get("PULSARCODE")):
        return None
    users = []
    for item in found:
        if item is pulsar_at:
            continue
        try:
            users.append(plugin.load_flag_settings(item[2], cwd))
        except ValueError as exc:
            raise LaunchRefused("%s. Claude Code was not started: Pulsar merges your --settings into the "
                                "session's own settings and cannot do that with this one." % exc)
    merged = plugin.merge_settings(base or {}, users, {})
    keep_long_rows(env, merged)
    report = plugin.pin_report(env, cwd, [plugin.env_block(u) for u in users], setting_sources(args))
    why = _refusal(report)
    if why:
        raise LaunchRefused(why)
    merged = plugin.merge_settings(base or {}, users, report.env)
    if pulsar_at is not None:
        path = Path(pulsar_at[2])
        if not path.is_absolute():
            path = Path(cwd) / path
    else:
        path = plugin.settings_path(env.get("PULSAR_SESSION", ""))
    return PinPlan(report, merged, path, found, pulsar_at, tuple(args))


def pin_warnings(report: Any) -> List[str]:
    """The warnings a pin report gives (names, never values), in the order they are printed."""
    out: List[str] = []
    where = ("talks only to Pulsar Core" if report.route == "core"
             else "talks directly to %s" % report.target)
    if report.overridden:
        out.append("this project's .claude settings set %s; Pulsar overrides %s in this session, which %s." % (
            ", ".join(report.overridden[:8]) + (" and %d more" % (len(report.overridden) - 8)
                                                 if len(report.overridden) > 8 else ""),
            "it" if len(report.overridden) == 1 else "them", where))
    if report.unpinned:
        one = len(report.unpinned) == 1
        out.append("this project's .claude settings %sset %s, which can change where this session connects "
                   "or which credential it uses, and Pulsar does not override %s: Claude Code applies the "
                   "project's %s. Remove %s from .claude/settings.json or settings.local.json unless you trust "
                   "this project." % ("also " if report.overridden else "", ", ".join(report.unpinned[:8]),
                                      "it" if one else "them", "value" if one else "values", "it" if one else "them"))
    unreadable = list(getattr(report, "unreadable", ()) or ())
    if unreadable and report.route == "core":
        many = len(unreadable) > 1
        out.append("Pulsar cannot read this project's .claude/%s, which Claude Code may still apply. Routing stays "
                   "pinned to Pulsar Core, but nothing else in %s is checked. Fix or remove %s unless you trust "
                   "this project." % (" and ".join(unreadable), "them" if many else "it", "them" if many else "it"))
    return out


def apply_pins(plan: PinPlan, args: List[str], warn: Optional[Callable[[str], None]] = None) -> None:
    """Write check_pins' settings file, leave exactly that one --settings in `args`, print the warnings.

    Raises LaunchRefused when the file cannot be written."""
    plugin = _plugin()
    try:
        K.write_private(plan.path, plugin.dumps_settings(plan.merged) + "\n")
    except (OSError, ValueError) as exc:
        raise LaunchRefused("could not write this session's settings (%s: %s). Claude Code was not started."
                            % (type(exc).__name__, P.clean_text(str(plan.path), 200)))
    found, pulsar_at = plan.found, plan.pulsar_at
    at = 0
    if pulsar_at is not None:
        at = pulsar_at[0] - sum(n for i, n, _ in found if i < pulsar_at[0])
    for i, n, _ in sorted((f for f in found if f is not pulsar_at), reverse=True):
        del args[i:i + n]
    if pulsar_at is not None:
        args[at:at + pulsar_at[1]] = ["--settings", str(plan.path)]
    else:
        args[:0] = ["--settings", str(plan.path)]
    say = warn or _default_warn
    for msg in pin_warnings(plan.report):
        say(msg)


def pin_session_settings(args: List[str], env: Dict[str, str], warn: Optional[Callable[[str], None]] = None,
                         cwd: Optional[str] = None) -> bool:
    """Pin Claude Code's routing environment in the one --settings file Claude Code gets (plugin.pin_report).

    Flag settings outrank the project's .claude/settings.json and settings.local.json, so a repository
    cannot re-route the session, its prompts or its session token. Claude Code reads only the last
    --settings, so a user's own (a path or inline JSON) is merged into Pulsar's file, the pins applied
    last, and removed from `args`; `args` then holds exactly one --settings. `env` may change too
    (keep_long_rows). Raises LaunchRefused when a user's --settings cannot be read, when the project
    would replace the credential of a direct or cloud route (or has a settings file Pulsar cannot read
    there), or when the file cannot be written: check_pins, then apply_pins.

    Warnings name variables, never their values: one for the project keys the pins override (worded for
    the route), a stronger one for keys that look like routing or credentials and are not pinned, and one
    for a project settings file Pulsar cannot read (core route).
    True when the session was pinned; False for an environment that is not a Pulsar session's.
    """
    plan = check_pins(args, env, cwd)
    if plan is None:
        return False
    apply_pins(plan, args, warn)
    return True


def release_session(sess: Optional[ST.Session]) -> None:
    """Forget a session registered for a launch that did not start Claude Code (its file and folder)."""
    if sess is None:
        return
    try:
        ST.session_path(sess.sid).unlink()
    except OSError:
        pass
    try:
        shutil.rmtree(str(session_dir(sess.sid)), ignore_errors=True)
    except Exception:
        pass


def exec_claude(binary: str, args: List[str], env: Dict[str, str], pins: Optional[PinPlan] = None) -> int:
    """Pin the session's settings (or apply check_pins' `pins`, decided for these same `args`) and start
    Claude Code: exec on POSIX, a child process on Windows."""
    try:
        if pins is not None and tuple(args) == pins.args:
            apply_pins(pins, args)
        else:
            pin_session_settings(args, env)
    except LaunchRefused as exc:
        sys.stderr.write("pulsarcode: %s\n" % exc)
        return 2
    except Exception as exc:                     # never start a Pulsar session unpinned
        if env.get("PULSAR_SESSION") or env.get("PULSAR_CORE") or env.get("PULSARCODE"):
            sys.stderr.write("pulsarcode: could not pin this session's settings (%s). Claude Code was not "
                             "started.\n" % type(exc).__name__)
            return 1
    restore_umask()
    sys.stdout.flush()
    sys.stderr.flush()
    if _nt():
        try:
            argv = claude_argv(binary, args)
        except ValueError as exc:
            sys.stderr.write("pulsarcode: %s\n" % exc)
            return 2
        return run_child(argv, env)
    os.execvpe(binary, [binary] + args, env)
    return 0  # unreachable
