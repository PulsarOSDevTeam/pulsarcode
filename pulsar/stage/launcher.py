# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Put Pulsar Stage on screen the first time the model presents something.

PULSAR_STAGE_VIEWER=auto (default) | split | browser | none

auto tries, in order: a tmux split, a zellij pane, a WezTerm split, a kitty
split (remote control), a Windows Terminal split, then the browser when a GUI
is plausible (on macOS in the background: it never takes the focus). split
adds an iTerm2 split, which is driven through AppleScript (a command typed
into a new session), so only when you ask for it. A run with no terminal
behind it (cron, CI, a scripted job) opens nothing. Nothing here blocks (every
subprocess has a short timeout), nothing raises, and no argument ever passes
through a shell unquoted. Every helper runs by its absolute path, found in an
absolute PATH entry that is not the current folder: a program a project ships
(./node_modules/.bin, an empty PATH entry) never runs. The viewer pane starts through the
package bootstrap (pulsar.python_argv), so it runs this install, and gets the
PULSAR_* display settings, never credentials.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence

from pulsar import python_argv

PERCENT = 42
INSTRUCTION = "run `pulsarcode stage` in another terminal (or `pulsarcode stage --web`) to watch"
PASS_VARS = ("PULSAR_HOME", "PULSAR_FX", "PULSAR_REDUCED_MOTION", "PULSAR_HAPTICS", "PULSAR_ASCII", "PULSAR_COLOR",
             "PULSAR_SEED", "NO_COLOR", "COLORTERM", "LANG", "LC_ALL", "LC_CTYPE")
_SID_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_GUID_OK = re.compile(r"^[0-9A-Fa-f-]{8,64}$")


def pkg_root() -> str:
    return str(Path(__file__).resolve().parent.parent.parent)


def safe_sid(sid: str) -> str:
    sid = str(sid or "")
    return sid if _SID_OK.match(sid) else ""


def viewer_argv(sid: str) -> List[str]:
    """The viewer command. Never `-m pulsar`: the split starts in the project directory, and
    `-m` would import a `pulsar/` package (or a stdlib shadow) that the project ships."""
    sid = safe_sid(sid)
    return python_argv("stage", *(["--sid", sid] if sid else []))


def env_assignments(env: Mapping[str, str]) -> List[str]:
    root = pkg_root()
    pp = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    out = ["PYTHONPATH=" + pp]
    for var in PASS_VARS:
        val = env.get(var)
        if val and "\n" not in val and "\x00" not in val:
            out.append(var + "=" + val)
    return out


def posix_command(env: Mapping[str, str], sid: str) -> List[str]:
    """argv that runs the viewer with the right environment, via the system's env(1)."""
    return ["/usr/bin/env"] + env_assignments(env) + viewer_argv(sid)


def shell_line(argv: Sequence[str]) -> str:
    return " ".join(shlex.quote(a) for a in argv)


# ---------------------------------------------------------------------------
# subprocess wrappers (monkeypatched in tests)
# ---------------------------------------------------------------------------

def has_terminal() -> bool:
    """A person's terminal behind this process: the controlling terminal Claude Code was started from.

    Scripted runs (cron, CI, a job with no terminal) have none, and open_viewer opens nothing there.
    """
    if os.name == "nt":
        return True
    try:
        fd = os.open("/dev/tty", os.O_RDONLY | getattr(os, "O_NOCTTY", 0))
    except OSError:
        return False
    os.close(fd)
    return True


def _which(name: str) -> Optional[str]:
    """The absolute path of `name` from an absolute PATH entry that is not the current folder, else None."""
    from pulsar.hardware import which
    return which(name)


def _system(name: str, platform: str) -> Optional[str]:
    """macOS's own /usr/bin/<name> (osascript, open), else _which(name)."""
    if platform == "darwin" and os.path.isfile("/usr/bin/" + name):
        return "/usr/bin/" + name
    return _which(name)


def _run(argv: List[str], timeout: float = 4.0, detach: bool = False) -> int:
    """Run briefly; return the exit code, or -1 on any failure. Never raises, never inherits our stdio."""
    try:
        kw: Dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                    "timeout": timeout}
        if detach and os.name != "nt":
            kw["start_new_session"] = True
        return subprocess.run(argv, **kw).returncode
    except (OSError, ValueError, subprocess.SubprocessError):
        return -1


def _spawn(argv: List[str]) -> bool:
    """Fire and forget (browser openers)."""
    try:
        kw: Dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        subprocess.Popen(argv, **kw)
        return True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


# ---------------------------------------------------------------------------
# split strategies
# ---------------------------------------------------------------------------

def _tmux(env: Mapping[str, str], sid: str, platform: str) -> Optional[str]:
    tmux = _which("tmux") if env.get("TMUX") else None
    if not tmux:
        return None
    line = shell_line(posix_command(env, sid))
    target = ["-t", env["TMUX_PANE"]] if re.match(r"^%\d+$", env.get("TMUX_PANE", "")) else []
    for size in (["-l", "%d%%" % PERCENT], ["-p", str(PERCENT)]):   # tmux >= 3.1 / older
        if _run([tmux, "split-window", "-h", "-d", "-c", pkg_root()] + size + target + [line]) == 0:
            return "opened in a tmux split"
    return None


def _zellij(env: Mapping[str, str], sid: str, platform: str) -> Optional[str]:
    zellij = _which("zellij") if env.get("ZELLIJ") else None
    if not zellij:
        return None
    cmd = posix_command(env, sid)
    for extra in (["--close-on-exit"], []):
        if _run([zellij, "run", "--name", "Pulsar Stage", "--direction", "right", "--cwd", pkg_root()] + extra + ["--"] + cmd,
                timeout=5) == 0:
            return "opened in a zellij pane"
    return None


def _wezterm(env: Mapping[str, str], sid: str, platform: str) -> Optional[str]:
    pane = env.get("WEZTERM_PANE", "")
    wezterm = _which("wezterm") if pane and pane.isdigit() else None
    if not wezterm:
        return None
    cmd = posix_command(env, sid) if os.name != "nt" else viewer_argv(sid)
    if _run([wezterm, "cli", "split-pane", "--right", "--percent", str(PERCENT), "--pane-id", pane,
             "--cwd", pkg_root(), "--"] + cmd,
            timeout=5) == 0:
        _run([wezterm, "cli", "activate-pane", "--pane-id", pane], timeout=2)   # keep focus on Claude Code
        return "opened in a WezTerm split"
    return None


def _kitty(env: Mapping[str, str], sid: str, platform: str) -> Optional[str]:
    kitty = _which("kitty") if env.get("KITTY_LISTEN_ON") or env.get("KITTY_WINDOW_ID") else None
    if not kitty:
        return None
    to = env.get("KITTY_LISTEN_ON", "")
    base = [kitty, "@"] + (["--to", to] if to else [])
    if not to and _run(base + ["ls"], timeout=1.5, detach=True) != 0:
        # no socket: remote control over the tty would fight Claude Code for the terminal, so skip it
        return None
    cmd = posix_command(env, sid)
    for extra in (["--keep-focus"], []):
        if _run(base + ["launch", "--location=vsplit", "--title", "Pulsar Stage", "--cwd", pkg_root()] + extra + cmd,
                timeout=4,
                detach=True) == 0:
            return "opened in a kitty split"
    return None


def _applescript_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _iterm(env: Mapping[str, str], sid: str, platform: str) -> Optional[str]:
    osascript = _system("osascript", platform) if env.get("TERM_PROGRAM") == "iTerm.app" and platform == "darwin" \
        else None
    if not osascript:
        return None
    line = "cd " + shlex.quote(pkg_root()) + " && exec " + shell_line(posix_command(env, sid))
    guid = env.get("ITERM_SESSION_ID", "").split(":")[-1]
    find = ""
    if _GUID_OK.match(guid):
        find = ("repeat with w in windows\n repeat with tb in tabs of w\n repeat with s in sessions of tb\n"
                "  if (unique id of s) is %s then set target to s\n end repeat\n end repeat\nend repeat\n"
                % _applescript_str(guid))
    script = ('tell application "iTerm2"\n'
              "set target to missing value\n" + find +
              "if target is missing value then set target to current session of current window\n"
              "tell target\n set newSession to (split vertically with default profile)\nend tell\n"
              "tell newSession to write text %s\n"
              "tell target to select\n"
              "end tell" % _applescript_str(line))
    if _run([osascript, "-e", script], timeout=8) == 0:
        return "opened in an iTerm2 split"
    return None


def _wt_escape(a: str) -> str:
    return a.replace(";", "\\;")


def _windows_terminal(env: Mapping[str, str], sid: str, platform: str) -> Optional[str]:
    wt = _which("wt") if env.get("WT_SESSION") and platform.startswith("win") else None
    if not wt:
        return None
    argv = viewer_argv(sid)
    if env.get("PULSAR_HOME"):
        argv += ["--home", env["PULSAR_HOME"]]
    args = [wt, "-w", "0", "split-pane", "-V", "--size", "0.%d" % PERCENT, "-d", pkg_root()] + \
        [_wt_escape(a) for a in argv]
    if _run(args, timeout=5) == 0:
        return "opened in a Windows Terminal split"
    return None


STRATEGIES: List[Callable[[Mapping[str, str], str, str], Optional[str]]] = [
    _tmux, _zellij, _wezterm, _kitty, _iterm, _windows_terminal]


# ---------------------------------------------------------------------------
# browser
# ---------------------------------------------------------------------------

def gui_plausible(env: Optional[Mapping[str, str]] = None, platform: Optional[str] = None) -> bool:
    env = os.environ if env is None else env
    platform = platform or sys.platform
    if platform == "darwin" or platform.startswith("win"):
        return not env.get("SSH_CONNECTION")
    return bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


def open_browser(url: str, env: Optional[Mapping[str, str]] = None, platform: Optional[str] = None) -> bool:
    """Open a local URL without ever handing our stdio to a text-mode browser.

    On macOS the browser opens in the background (`open -g`): the page is there, the focus stays where you
    were typing.
    """
    env = os.environ if env is None else env
    platform = platform or sys.platform
    if not url.startswith("http://127.0.0.1:") and not url.startswith("http://localhost:") and \
            not url.startswith("file://"):
        return False
    if not gui_plausible(env, platform):
        return False
    if platform == "darwin":
        opener = _system("open", platform)
        return _spawn([opener, "-g", url]) if opener else False
    if not platform.startswith("win"):
        wsl = bool(env.get("WSL_DISTRO_NAME") or env.get("WSL_INTEROP"))
        for opener_argv in ((["wslview"], ["xdg-open"], ["gio", "open"]) if wsl else
                            (["xdg-open"], ["gio", "open"], ["wslview"])):
            path = _which(opener_argv[0])
            if path:
                return _spawn([path] + opener_argv[1:] + [url])
        return False
    try:
        os.startfile(url)  # type: ignore[attr-defined]
        return True
    except (OSError, AttributeError):
        pass
    try:
        import webbrowser
        th = threading.Thread(target=webbrowser.open, args=(url,), daemon=True)
        th.start()
        th.join(3.0)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def choose(env: Mapping[str, str], platform: str) -> List[str]:
    """Names of the strategies that apply in this environment, in the order they are tried."""
    mode = (env.get("PULSAR_STAGE_VIEWER") or "auto").strip().lower()
    if mode in ("none", "off", "0", "false", "no"):
        return []
    names: List[str] = []
    if mode in ("auto", "split"):
        if env.get("TMUX"):
            names.append("tmux")
        if env.get("ZELLIJ"):
            names.append("zellij")
        if env.get("WEZTERM_PANE"):
            names.append("wezterm")
        if env.get("KITTY_LISTEN_ON") or env.get("KITTY_WINDOW_ID"):
            names.append("kitty")
        if env.get("TERM_PROGRAM") == "iTerm.app" and platform == "darwin" and mode == "split":
            names.append("iterm")          # AppleScript types a command into a new session: on request only
        if env.get("WT_SESSION") and platform.startswith("win"):
            names.append("wt")
    if mode in ("auto", "browser") and gui_plausible(env, platform):
        names.append("browser")
    return names


_BY_NAME = {"tmux": _tmux, "zellij": _zellij, "wezterm": _wezterm, "kitty": _kitty, "iterm": _iterm,
            "wt": _windows_terminal}


def open_viewer(url: str, sid: str, env: Optional[Mapping[str, str]] = None, platform: Optional[str] = None) -> str:
    """Open the stage viewer next to the session. Returns a short description of what happened."""
    try:
        env = dict(os.environ if env is None else env)
        platform = platform or sys.platform
        mode = (env.get("PULSAR_STAGE_VIEWER") or "auto").strip().lower()
        if mode in ("none", "off", "0", "false", "no"):
            return "auto-open is off (PULSAR_STAGE_VIEWER=none); " + INSTRUCTION
        if not has_terminal():              # a scripted run: nothing pops up on a screen nobody watches
            return INSTRUCTION
        for name in choose(env, platform):
            if name == "browser":
                if url and open_browser(url, env, platform):
                    return "opened in your browser"
                continue
            res = _BY_NAME[name](env, sid, platform)
            if res:
                return res
        return INSTRUCTION
    except Exception:
        return INSTRUCTION
