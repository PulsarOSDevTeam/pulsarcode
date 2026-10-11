# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""`pulsarcode notify <attention|done|session-start>`: pings fired by Claude Code hooks.

The plugin runs it as `python_argv("<event>", module="pulsar.notify")` (the CLI is never imported).

Notification (attention) and Stop (done) answer with hook JSON whose `terminalSequence` Claude Code
writes to the terminal on our behalf: a desktop notification as OSC 9 (iTerm2, WezTerm, Ghostty,
Windows Terminal, ...), OSC 777 (foot, urxvt, VTE) or OSC 99 (kitty, only while unfocused), plus a
BEL for attention. No subprocess, and it works over SSH because the terminal on your desk shows it.

  PULSAR_NOTIFY=on      default: terminal notification (+ bell on attention); vibration under Termux
  PULSAR_NOTIFY=bell    the bell only
  PULSAR_NOTIFY=os      the OS notifier instead (osascript, notify-send, termux-notification), opt-in
  PULSAR_NOTIFY=loud    like on, and the bell on done too
  PULSAR_NOTIFY=off     nothing
  PULSAR_NOTIFY_OSC=9|777|99   force the notification escape

Never blocks Claude Code: OS notifiers and termux-vibrate are started fire-and-forget (detached, no
pipes back to Claude Code), and the hook always exits 0.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, List, Mapping, Optional

# Hook payloads and model ids end up in a terminal title escape, the session banner and OS
# notifications: no control character (ESC, BEL, C1, bidi) may pass through.
_UNPRINTABLE = re.compile("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028\u2029\u202a-\u202e\u2060-\u2069\ufeff]")

OFF = ("off", "0", "none", "false", "no")
TITLE = "PulsarCode"
# The terminal (OSC 2) title.
TERMINAL_TITLE = "◉ PulsarCode"
# Early 3.0.0 preview builds gave every session this Claude Code session title, so the /resume picker listed
# them all alike and the `claude --resume "◉ Pulsar Code"` hint printed at /exit matched none or many.
# Sessions are no longer titled; an old session still carrying it gets a unique one (legacy_session_title).
SESSION_TITLE = "◉ Pulsar Code"
# The PulsarCode persona: the mark (a core inside one ring, a beam to each side) as three rows of block cells.
# None of its glyph pairs occurs in Claude Code's mascot (tools/demo/svgterm.py, CLI_MASCOT), so the checks
# that keep that mascot out of the recordings never take one for the other.
PERSONA = ("     ▄▟▀▀▀▙▄     ",
           "▁▂▃▄▅█  ◉  █▅▄▃▂▁",
           "     ▀▜▄▄▄▛▀     ")
# Text drawn beside the persona stays this short, so the block holds together in an 80-column terminal
# under Claude Code's indent; a longer model line goes below it.
BESIDE = 52
_EMBER, _FLARE, _PLASMA, _BEAM = (255, 106, 0), (255, 46, 136), (157, 77, 255), (54, 226, 255)
_ASH, _DUST, _GO = (150, 146, 172), (88, 84, 108), (80, 250, 170)
BEL = "\x07"
# Claude Code prints `claude --resume <id>` at /exit, but bare `claude` neither sees Pulsar's sessions (they
# live in the isolated profile, PULSAR_HOME/claude_config) nor goes through Pulsar Core. The banner says so,
# in one line that fits an 80-column terminal under Claude Code's indent.
RESUME_HINT = "resume with pulsarcode --continue or pulsarcode --resume <id>, not claude"
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")      # a Claude Code session id; anything else is ignored


def _clip(s: Any, n: int = 180) -> str:
    text = " ".join(_UNPRINTABLE.sub(" ", str(s if s is not None else "")).split())
    return text[:n]


def mode_of(environ: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if environ is None else environ
    return (env.get("PULSAR_NOTIFY") or "on").strip().lower()


def _termux(env: Mapping[str, str]) -> bool:
    return bool(env.get("TERMUX_VERSION") or "com.termux" in (env.get("PREFIX") or ""))


def _message(event: str, payload: Dict[str, Any]) -> str:
    if event == "attention":
        return _clip(payload.get("message") or "Claude Code needs your input") or "Claude Code needs your input"
    return "Done. Your turn."


def _project(payload: Dict[str, Any]) -> str:
    cwd = payload.get("cwd")
    return _clip(os.path.basename(str(cwd).rstrip("/\\")), 40) if isinstance(cwd, str) and cwd else ""


def _title(payload: Dict[str, Any]) -> str:
    """`PulsarCode · <project>`, so pings from several sessions can be told apart."""
    name = _project(payload)
    return TITLE + (" · " + name if name else "")


def _session_id(payload: Optional[Dict[str, Any]]) -> str:
    sid = payload.get("session_id") if isinstance(payload, dict) else None
    return sid if isinstance(sid, str) and _SESSION_ID.match(sid) else ""


def legacy_session_title(payload: Optional[Dict[str, Any]]) -> Optional[str]:
    """A unique title for a session an early 3.0.0 preview build titled SESSION_TITLE, else None (pure, for tests).

    A hook cannot clear a session title, so a resumed 3.0.0 session would keep the shared title and its
    /exit hint would stay ambiguous: `◉ Pulsar Code · <project> · <first 8 of the session id>` instead.
    A title the user chose (/rename) is never touched.
    """
    if not isinstance(payload, dict) or payload.get("session_title") != SESSION_TITLE:
        return None
    sid = _session_id(payload)
    if not sid:
        return None
    name = _project(payload)
    return SESSION_TITLE + (" · " + name if name else "") + " · " + sid[:8]


def osc_style(environ: Optional[Mapping[str, str]] = None) -> str:
    """Which desktop-notification escape this terminal understands: "9", "777" or "99"."""
    env = os.environ if environ is None else environ
    forced = (env.get("PULSAR_NOTIFY_OSC") or "").strip()
    if forced in ("9", "777", "99"):
        return forced
    term = (env.get("TERM") or "").lower()
    prog = (env.get("TERM_PROGRAM") or "").lower()
    if env.get("KITTY_WINDOW_ID") or "kitty" in term:
        return "99"
    if prog in ("ghostty", "iterm.app", "wezterm") or "ghostty" in term or env.get("WT_SESSION"):
        return "9"
    if term.startswith(("foot", "rxvt")) or env.get("VTE_VERSION"):
        return "777"
    return "9"


def osc_notification(title: str, body: str, environ: Optional[Mapping[str, str]] = None) -> str:
    """A desktop notification escape (OSC 9, 777 or 99) that passes Claude Code's terminalSequence allowlist.

    Control characters are stripped from both parts, `;` (a field separator) is replaced, and the text
    never starts with a digit (OSC 9 bodies may not, unless they are the 9;4 progress form).
    """
    title = _clip(title, 80).replace(";", ",") or TITLE
    body = _clip(body, 180).replace(";", ",")
    style = osc_style(environ)
    if style == "777":
        return "\x1b]777;notify;%s;%s\x07" % (title, body)
    text = "%s: %s" % (title, body) if body else title
    if text[:1].isdigit():
        text = TITLE + ": " + text
    if style == "99":
        return "\x1b]99;o=unfocused;%s\x1b\\" % text      # kitty: only when the window is not focused
    return "\x1b]9;%s\x07" % text


def hook_output(event: str, payload: Dict[str, Any], mode: str,
                environ: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Hook JSON for Notification (attention) and Stop (done): {} or {"terminalSequence": ...} (pure)."""
    env = os.environ if environ is None else environ
    if mode in OFF or event not in ("attention", "done"):
        return {}
    seq = ""
    if event == "attention" or mode == "loud":
        seq += BEL
    if mode not in ("bell", "os"):
        seq += osc_notification(_title(payload), _message(event, payload), env)
    return {"terminalSequence": seq} if seq else {}


def _which(name: str, environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Absolute path of `name` from an absolute PATH entry only, else None.

    Hooks run in the project folder, so an empty, "." or relative PATH entry would run the project's
    own `termux-vibrate` (or `notify-send`...) on every hook. Same rule as launch._which, copied here
    so the hook never imports the launcher.
    """
    env = os.environ if environ is None else environ
    for d in (env.get("PATH") or "").split(os.pathsep):
        d = d.strip().strip('"')
        if not d or not os.path.isabs(d):
            continue
        cand = os.path.join(d, name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def plan(event: str, payload: Dict[str, Any], mode: str, environ: Optional[Mapping[str, str]] = None) -> List[List[str]]:
    """External commands to start (pure, for tests). All of them run fire-and-forget.

    Termux: vibration by default (termux-notification only with PULSAR_NOTIFY=os). Elsewhere nothing
    unless PULSAR_NOTIFY=os opts into the OS notifier (osascript on macOS, notify-send on Linux).
    """
    env = os.environ if environ is None else environ
    cmds: List[List[str]] = []
    if mode in OFF or mode == "bell" or event not in ("attention", "done"):
        return cmds
    title, msg = _title(payload), _message(event, payload)
    if _termux(env):
        vibrate = _which("termux-vibrate", env)
        if vibrate:
            cmds.append([vibrate, "-d", "70" if event == "attention" else "25"])
        tnotify = _which("termux-notification", env) if mode == "os" else None
        if tnotify:
            cmds.append([tnotify, "--title", title, "--content", msg])
        return cmds
    if mode != "os":
        return cmds
    if sys.platform == "darwin":
        osa = _which("osascript", env)
        if osa:
            # The texts travel as arguments of the script's run handler, never inside its source: AppleScript
            # string literals have no \u escapes (a JSON-escaped title such as "PulsarCode · x" does not
            # even compile), and as arguments a title like "PulsarCode · café" stays as it is. The title goes
            # first: it always starts with "PulsarCode", so osascript's option parsing stops there and a
            # message starting with "-" (say "-e ...") is never read as an option.
            cmds.append([osa, "-e", "on run argv", "-e",
                         'display notification (item 2 of argv) with title (item 1 of argv) sound name "Glass"',
                         "-e", "end run", title, msg])
    elif sys.platform.startswith("linux") and (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        send = _which("notify-send", env)
        if send:
            cmds.append([send, "-a", TITLE, "-u", "normal", title, msg])
    return cmds


def spawn_detached(cmd: List[str]) -> bool:
    """Start `cmd` and return at once: no pipe to Claude Code, its own session, never waited for."""
    import subprocess
    kw: Dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                          "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen(cmd, **kw)
        return True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def session_start_output(stats: Optional[Dict[str, Any]], environ: Optional[Mapping[str, str]] = None,
                         payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """SessionStart hook JSON: a branded banner with the resume command and a terminal title (pure, for tests).

    `payload` is the hook input (session_id, cwd, session_title). No session title is set: one shared by
    every session made `claude --resume "<title>"` ambiguous and hid each session's own name in /resume.
    """
    from pulsar.statusline import _c, color_depth, provider_label
    env = os.environ if environ is None else environ
    plan_ = (stats or {}).get("plan") if isinstance(stats, dict) else None
    plan_ = plan_ if isinstance(plan_, list) else []
    primary = plan_[0] if plan_ and isinstance(plan_[0], dict) else {}
    model = _clip(primary.get("model") or env.get("ANTHROPIC_MODEL") or "any model", 120) or "any model"
    provider = _clip(provider_label(primary), 40)          # "Groq", or a custom endpoint's host, as the picker shows it
    relay = [_clip(t.get("model"), 80) for t in plan_[1:] if isinstance(t, dict)]
    depth = color_depth(env)

    def c(rgb, t, bold=False):
        return _c(rgb, t, bold, depth)
    from pulsar import __version__
    from pulsar.statusline import _grad, _strong
    name = _grad(TITLE, depth) + c(_ASH, " " + _clip(__version__, 20))
    via = c(_ASH, " via " + provider) if provider else ""
    chain = c(_ASH, "relay ") + c(_GO, " → ".join(relay)) if relay else ""
    commands = "/pulsar:status  /pulsar:stage  /pulsar:switch"
    source = str(payload.get("source") or "") if isinstance(payload, dict) else ""
    if source in ("clear", "compact"):
        # the same session going on: one line, no persona
        line = (c(_BEAM, "◉", True) + " " + name + c(_ASH, "  ·  ") + _strong(model, depth) + via
                + (c(_ASH, "  ·  ") + chain if chain else "") + c(_DUST, "  ·  " + commands)
                + "\n" + c(_ASH, RESUME_HINT))
    else:
        # A session starts: the persona with the name, the model and the commands beside it, as Claude Code's
        # own header sets its mascot. The first line follows Claude Code's "SessionStart:startup says:" label,
        # so the block starts on the second line, where every row gets the same indent.
        fits = len(model) + (len(" via " + provider) if provider else 0) <= BESIDE
        second = _strong(model, depth) + via if fits else c(_ASH, ("via " + provider) if provider else "uplink ready")
        beside = [name, second, c(_DUST, commands)]
        rows = [art + "   " + text for art, text in zip(persona(depth), beside)]
        below = ([_strong(model, depth)] if not fits else []) + ([chain] if chain else []) + [c(_ASH, RESUME_HINT)]
        line = "\n".join([c(_BEAM, "◉", True) + c(_ASH, " uplink ready")] + rows + below)
    # Model-independent: the terminal title is set once, and a /pulsar:switch, a /model pick or a relay would
    # leave a model name in it stale. The status line names the live model.
    out: Dict[str, Any] = {"systemMessage": line, "terminalSequence": "\x1b]2;" + TERMINAL_TITLE + "\x07"}
    specific: Dict[str, Any] = {}
    note = session_note(primary)
    if note:
        specific["additionalContext"] = note
    legacy = legacy_session_title(payload)
    if legacy:
        specific["sessionTitle"] = legacy
    if specific:
        out["hookSpecificOutput"] = dict({"hookEventName": "SessionStart"}, **specific)
    return out


def _mix(a: Any, b: Any, k: float) -> Any:
    return tuple(int(a[i] + (b[i] - a[i]) * k) for i in range(3))


def persona(depth: int = 24) -> List[str]:
    """PERSONA's three rows coloured for `depth` (plain text at 0), as the mark is: the ring ember to flare
    on top and plasma to cyan below, the left beam plasma, the right beam cyan, the core in the terminal's
    own text colour."""
    from pulsar.statusline import _c, _strong
    rows = []
    for y, row in enumerate(PERSONA):
        cells = []
        for x, ch in enumerate(row):
            if ch == " ":
                cells.append(ch)
            elif ch == "◉":
                cells.append(_strong(ch, depth))         # the core, in the terminal's own text colour
            elif x < 5:                                     # the left beam brightens toward the ring
                cells.append(_c(_mix((0, 0, 0), _PLASMA, 0.4 + 0.15 * x), ch, False, depth))
            elif x > 11:                                    # the right beam fades away from it
                cells.append(_c(_mix((0, 0, 0), _BEAM, 1.0 - 0.15 * (x - 12)), ch, False, depth))
            elif y == 1:
                cells.append(_c(_mix(_FLARE, _PLASMA, 0.5) if x == 5 else _mix(_PLASMA, _BEAM, 0.5), ch, False, depth))
            else:
                a, b = (_EMBER, _FLARE) if y == 0 else (_PLASMA, _BEAM)
                cells.append(_c(_mix(a, b, (x - 5) / 6.0), ch, False, depth))
        rows.append("".join(cells))
    return rows


_NOTE_ID = re.compile(r"[^A-Za-z0-9._:/@+-]")
_NOTE_NAME = re.compile(r"[^A-Za-z0-9 ._:/-]")


def session_note(primary: Mapping[str, Any]) -> str:
    """What the model is told once, at session start, when it is not a Claude model: which model it is, and to
    report a Pulsar tool as done only when the tool's result says so. Claude Code's own prompt introduces the
    assistant as Claude Code, so another model otherwise answers "who are you" with that name, and a weaker one
    tells the user a visual is on screen when the call was refused. "" for a Claude model or an unknown one.

    Only the model id and the provider name go in, cut to a safe alphabet: a catalog entry cannot write
    instructions into the session through its name.
    """
    from pulsar.statusline import provider_label
    model = _NOTE_ID.sub("", str(primary.get("model") or ""))[:120]
    if not model or model.split("/")[-1].lower().startswith("claude"):
        return ""
    provider = _NOTE_NAME.sub("", provider_label(dict(primary)))[:40].strip()
    return ("You are %s%s, working in Claude Code through PulsarCode. You are not Claude: when asked who you "
            "are or what you can do, say so in a few plain sentences. Pulsar adds tools, among them stage_present "
            "(a slide deck), stage_show (one visual: a chart, table, diagram or progress tracker) and council "
            "(the same question to other models). Tell the user a tool worked only when its result says so."
            % (model, " (served by %s)" % provider if provider else ""))


def _read_payload(limit: int = 1_000_000) -> Dict[str, Any]:
    try:
        stream = sys.stdin
        if stream is None or stream.isatty():
            return {}
        raw = stream.read(limit)
        data = json.loads(raw) if raw and raw.strip() else {}
    except (ValueError, OSError, AttributeError, UnicodeDecodeError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def _emit(obj: Dict[str, Any]) -> None:
    if not obj:
        return
    try:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()
    except (OSError, ValueError, UnicodeEncodeError):
        pass


def main(argv: Optional[List[str]] = None) -> int:
    try:
        argv = list(argv or [])
        event = argv[0] if argv else "attention"
        payload = _read_payload()
        if event == "session-start":
            stats = None
            try:
                from pulsar.statusline import core_get
                stats = core_get("/pulsar/stats", timeout=1.0)
            except Exception:
                stats = None
            _emit(session_start_output(stats, payload=payload))
            return 0
        mode = mode_of()
        if mode in OFF:
            return 0
        _emit(hook_output(event, payload, mode))
        for cmd in plan(event, payload, mode):
            spawn_detached(cmd)
    except Exception:
        pass                       # a hook must never fail loudly or block Claude Code
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
