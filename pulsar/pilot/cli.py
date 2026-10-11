# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""`pulsarcode pilot on|off|status|tour|log`."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from pulsar import fx, python_argv
from pulsar.fx import ASH, BAD, BEAM, DUST, FLARE, ICE, OK, WARN
from pulsar.pilot import backends as B
from pulsar.pilot import safety as S

USAGE = """\
{mark} PULSAR PILOT   computer use, with the pulsar cursor

  pulsarcode pilot status    backend, screen, permissions (--gui: also start a test cursor window)
  pulsarcode pilot on        let the model see the screen, move, click and type
  pulsarcode pilot off       disarm (running sessions stop at their next action)
  pulsarcode pilot tour      watch the pulsar cursor (moves nothing, clicks nothing)
  pulsarcode pilot log       last actions from the local audit log

  Failsafe: slam the mouse into the top-left corner of the main display.
  Env: PULSAR_PILOT=1 (this session only), PULSAR_PILOT_OVERLAY=0 (no cursor),
       PULSAR_PILOT_PYTHON (a Python with tkinter for the cursor),
       PULSAR_PILOT_MAX_W / _MAX_H (screenshot size, default 1280).
"""


class Out:
    def __init__(self, stream=None) -> None:
        from pulsar.term import Caps
        self.stream = stream or sys.stdout
        # Refusals and errors: stderr for the real CLI; a caller that hands its own stream gets them there.
        self.err = sys.stderr if stream is None or stream is sys.stdout else stream
        caps = Caps(self.stream)
        self.ink = fx.Ink(caps.color)
        self.u = caps.unicode
        self.tty = caps.tty

    def mark(self) -> str:
        return fx.MARK if self.u else "@"

    def line(self, s: str = "") -> None:
        if not self.u:
            s = s.replace("·", "-").replace("…", "...").replace("▸", ">")
            enc = getattr(self.stream, "encoding", None) or "ascii"
            try:
                s = s.encode(enc, "replace").decode(enc)
            except LookupError:
                s = s.encode("ascii", "replace").decode("ascii")
        self.stream.write(s + "\n")
        self.stream.flush()

    def head(self, tail: str) -> None:
        self.line(self.ink.paint(self.mark(), BEAM, True) + " " + self.ink.gradient("PULSAR PILOT", bold=True) +
                  self.ink.paint("   " + tail, DUST))

    def row(self, glyph: str, color: fx.Color, key: str, value: str, detail: str = "") -> None:
        g = {"ok": "✓", "warn": "!", "bad": "✗", "on": "●", "off": "○"}.get(glyph, glyph)
        if not self.u:
            g = {"✓": "+", "●": "*", "○": "o", "✗": "x"}.get(g, g)
        self.line("  " + self.ink.paint(g, color, True) + " " + self.ink.paint("%-10s" % key, ASH) + " " + value +
                  (self.ink.paint("  " + detail, DUST) if detail else ""))

    def bullet(self, s: str, color: fx.Color = ICE) -> None:
        self.line("    " + self.ink.paint("▸ " if self.u else "> ", FLARE, True) + self.ink.paint(s, color))


def _short_home(p: str) -> str:
    h = os.path.expanduser("~")
    return "~" + p[len(h):] if h and p.startswith(h) else p


def probe_overlay(timeout: float = 12.0) -> Dict[str, Any]:
    """Start the cursor's own probe (it opens and closes a hidden Tk window). Only on request: on macOS even
    a hidden window can show a Dock icon and take the focus."""
    from pulsar.pilot import tools as T
    why = T.overlay_blocker()
    if why:
        return {"ok": False, "error": why}
    env = T.overlay_env()
    root = T._package_root()
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        res = subprocess.run(python_argv("--probe", module="pulsar.pilot.overlay", python=T.overlay_python()),
                             capture_output=True, timeout=timeout, env=env, cwd=root)
        data = json.loads(res.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
        return data if isinstance(data, dict) else {"ok": False, "error": "bad probe reply"}
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
        return {"ok": False, "error": "probe failed: %s" % type(exc).__name__}


def cmd_status(out: Out, args: Optional[List[str]] = None) -> int:
    """Pilot's state, backend, screen and permissions. `--gui` (and `pilot doctor`) also starts the cursor's
    test window; plain status never opens one."""
    gui = "--gui" in (args or [])
    on, why = S.state()
    out.head("computer use, with the pulsar cursor")
    detail = why[why.find("(") + 1:why.rfind(")")] if "(" in why and why.endswith(")") else why
    out.row("on" if on else "off", OK if on else DUST, "state", "armed" if on else "off", detail)
    kind = B.platform_kind()
    backend: Optional[B.Backend] = None
    try:
        backend = B.get_backend()
        info = backend.info()
        pos = ""
        corner = False
        try:
            cx, cy = backend.cursor()
            pos = "pointer at (%d, %d)" % (cx, cy)
            corner = B.in_failsafe(cx, cy, backend.origin())
        except B.PilotError:
            pass
        out.row("ok" if info.ok else "warn", OK if info.ok else WARN, "backend", info.label,
                "  ·  ".join(x for x in (info.detail, pos) if x))
        for k, v in info.perms.items():
            state = "granted" if v else ("not granted" if v is False else "unknown")
            out.row("ok" if v else ("bad" if v is False else "warn"), OK if v else (BAD if v is False else WARN),
                    "permission", "%s: %s" % (k.replace("_", " "), state))
        hints = list(info.hints)
        if corner:
            hints.append("The pointer is in the failsafe corner right now: Pilot refuses to act until it moves.")
    except B.PilotError as exc:
        out.row("bad", BAD, "backend", B.platform_label(kind), str(exc))
        hints = []
    if backend is not None:
        try:
            from pulsar.pilot import png as PNG
            from pulsar.pilot.tools import max_size
            sw, sh = backend.size()
            iw, ih = PNG.fit(sw, sh, *max_size())
            out.row("ok", OK, "screen", "%dx%d" % (sw, sh), "the model sees %dx%d screenshots" % (iw, ih))
        except Exception:
            pass
    _vision_row(out)
    from pulsar.pilot import tools as T
    blocked = T.overlay_blocker()
    ov = probe_overlay() if gui and not blocked else {"ok": False, "error": blocked}
    if ov.get("ok"):
        out.row("ok", OK, "cursor", "pulsar cursor ready", "Tk %s · %s" % (ov.get("tk", "?"), ov.get("platform", "")))
    elif gui or blocked:
        out.row("warn", WARN, "cursor", "no overlay (Pilot still works)", str(ov.get("error") or ""))
    else:
        out.row("ok", OK, "cursor", "not tested", "pulsarcode pilot status --gui starts a test window")
    out.row("ok", OK, "failsafe", "slam the mouse into the top-left corner to stop Pilot")
    out.row("ok", OK, "audit", _short_home(str(S.log_path())), "actions and coordinates, never typed text")
    for h in hints:
        out.bullet(h, WARN)
    if not on:
        out.line()
        out.line("  " + out.ink.paint("pulsarcode pilot on", BEAM, True) + out.ink.paint(" to arm it, ", DUST) +
                 out.ink.paint("pulsarcode pilot tour", BEAM, True) + out.ink.paint(" to meet the cursor.", DUST))
    return 0


def model_vision() -> Tuple[Optional[bool], str]:
    """(can the model Pilot would drive view images, its id): this session's model when run inside one,
    else the default model of the active profile. The flag is None when unknown."""
    from pulsar.pilot import tools as T
    vision, model = T.active_vision()
    if model:
        return vision, model
    try:
        from pulsar import catalog as C
        from pulsar import config as K
        prof = K.load().current()
        if prof is None or not prof.model:
            return None, ""
        return C.model_vision(prof.id, prof.model, prof.resolve()), prof.model
    except Exception:
        return None, ""


def _vision_row(out: Out) -> None:
    vision, model = model_vision()
    name = _clean_id(model) or "no model chosen"
    if vision is True:
        out.row("ok", OK, "vision", name, "sees screenshots")
    elif vision is False:
        out.row("bad", BAD, "vision", name, "cannot view images: Pilot needs a vision model (pulsarcode pick)")
    else:
        out.row("warn", WARN, "vision", name, "unknown: if the model cannot see images, Pilot cannot work")


def _clean_id(text: str) -> str:
    from pulsar import providers as P
    return P.clean_text(text or "", 80)


def _refuse(out: Out, msg: str) -> None:
    out.err.write(msg + "\n")
    try:
        out.err.flush()
    except (OSError, ValueError):
        pass


IN_SESSION_VARS = ("CLAUDECODE", "PULSAR_SESSION", "PULSAR_SESSION_TOKEN", "CLAUDE_CODE_ENTRYPOINT")


def cmd_on(out: Out, args: List[str], ask: Callable[[str], str] = input) -> int:
    # Arming computer use must be a human decision at a real terminal: never from a model's shell
    # inside a Claude Code session, never from a script. (PULSAR_PILOT=1 enables it for one launch.)
    # The refusals come before the banner, on stderr.
    #
    # These checks catch the model's Bash tool as it normally runs (session markers set, no terminal, a
    # Claude Code process among its ancestors). They are not the boundary: a command that unsets the
    # markers, fakes a terminal with a pty and detaches from the process tree can pass them. The boundary
    # is Claude Code's permission prompt, which asks you before such a command runs.
    inside = [v for v in IN_SESSION_VARS if os.environ.get(v)]
    if inside:
        _refuse(out, "pulsarcode: Pilot can only be armed from your own terminal, not from inside a Claude Code "
                "session (%s is set). Open a new terminal and run `pulsarcode pilot on`." % inside[0])
        return 2
    try:
        tty = sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        tty = False
    if not tty:
        _refuse(out, "pulsarcode: `pilot on` needs an interactive terminal. For a single launch, set PULSAR_PILOT=1.")
        return 2
    from pulsar import launch as L
    if L.claude_ancestor():
        _refuse(out, "pulsarcode: Pilot can only be armed from your own terminal: this command runs inside Claude "
                "Code. Open a new terminal and run `pulsarcode pilot on`.")
        return 2
    out.head("arming computer use")
    out.line()
    out.line("  " + out.ink.paint("The model will see your screen and can move the mouse, click and type.", ICE, True))
    out.bullet("Claude Code still asks before each pilot action unless you allow it.", ASH)
    out.bullet("Failsafe: slam the mouse into the top-left corner to stop Pilot instantly.", ASH)
    out.bullet("Off switch: `pulsarcode pilot off` also stops sessions already running.", ASH)
    out.bullet("Audit log: %s (never your typed text, never screenshots)." % _short_home(str(S.log_path())), ASH)
    out.bullet("Rate limited to %d actions per second. Screenshots go only to the model you chose: a session "
               "with Pilot armed never relays to another provider." % int(S.RATE), ASH)
    out.line()
    try:
        ans = ask("  Enable Pilot? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        out.line()
        return 1
    if ans.strip().lower() not in ("y", "yes"):
        out.line(out.ink.paint("  Pilot stays off.", DUST))
        return 1
    S.set_enabled(True)
    out.line("  " + out.ink.paint("✓" if out.u else "+", OK, True) + " " + out.ink.paint("Pilot armed.", ICE, True) +
             out.ink.paint(" New pulsarcode sessions get the pilot_* tools.", DUST))
    try:
        info = B.get_backend().info()
        for h in info.hints:
            out.bullet(h, WARN)
    except B.PilotError as exc:
        out.bullet(str(exc), WARN)
    out.line(out.ink.paint("  Meet the cursor: ", DUST) + out.ink.paint("pulsarcode pilot tour", BEAM, True))
    return 0


def cmd_off(out: Out) -> int:
    S.set_enabled(False)
    out.line("  " + out.ink.paint("○" if out.u else "o", DUST, True) + " " + out.ink.paint("Pilot off.", ICE, True) +
             out.ink.paint(" Running sessions stop at their next action.", DUST))
    return 0


def cmd_log(out: Out, args: List[str]) -> int:
    n = 20
    for a in args:
        if a.isdigit():
            n = max(1, min(500, int(a)))
    path = S.log_path()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        out.line(out.ink.paint("  No pilot actions logged yet (%s)." % _short_home(str(path)), DUST))
        return 0
    for ln in lines:
        out.line("  " + ln)
    return 0


TourStep = Tuple[str, str, Callable[[int, int], Dict[str, Any]], float]


def tour_steps(w: int, h: int, mac: bool = False) -> List[TourStep]:
    def at(fx_: float, fy_: float) -> Tuple[int, int]:
        return int(w * fx_), int(h * fy_)
    s = "cmd+s" if mac else "ctrl+s"
    return [
        ("scan", "screenshot sweep (what the model sees)", lambda x, y: {"op": "scan", "label": "%dx%d" % (w, h)}, 0.9),
        ("glide", "comet glide, 250 to 450 ms ease-in-out", lambda x, y: dict(
            zip(("x", "y"), at(0.32, 0.36)), op="glide", label="hello", verb="pilot"), 1.3),
        ("aim", "lock-on before a click", lambda x, y: dict(zip(("x", "y"), at(0.62, 0.32)), op="aim",
                                                              label="Save", verb="click"), 0.75),
        ("click", "gradient ripple (nothing was clicked)", lambda x, y: dict(
            zip(("x", "y"), at(0.62, 0.32)), op="click", label="Save"), 1.3),
        ("type", "typing caret", lambda x, y: dict(zip(("x", "y"), at(0.46, 0.56)), op="type", chars=42, dur=1.6), 2.2),
        ("key", "shortcut keycaps", lambda x, y: dict(zip(("x", "y"), at(0.46, 0.56)), op="key", label=s), 1.6),
        ("scroll", "scroll chevrons", lambda x, y: dict(zip(("x", "y"), at(0.74, 0.58)), op="scroll", dy=3), 1.4),
        ("drag", "drag tether", lambda x, y: dict(zip(("x1", "y1"), at(0.74, 0.58)), **dict(
            zip(("x2", "y2"), at(0.56, 0.78))), op="drag", dur=0.7, label="file"), 1.6),
        ("right", "right click", lambda x, y: dict(zip(("x", "y"), at(0.36, 0.7)), op="click", button="right",
                                                   label="menu"), 1.3),
        ("double", "double click", lambda x, y: dict(zip(("x", "y"), at(0.24, 0.5)), op="click", count=2,
                                                     label="word"), 1.4),
        ("home", "back to your pointer", lambda x, y: {"op": "glide", "x": x, "y": y, "label": "done", "verb": "pilot"},
         1.0),
    ]


def cmd_tour(out: Out, client=None, sleep: Callable[[float], None] = time.sleep) -> int:
    from pulsar.pilot.tools import OverlayClient
    out.head("tour  ·  the pulsar cursor moves nothing and clicks nothing")
    ov = client or OverlayClient()
    if not ov.start():
        out.row("warn", WARN, "cursor", "unavailable", ov.disabled)
        return 1
    w, h = int(ov.hello.get("w") or 1280), int(ov.hello.get("h") or 800)
    home = (w // 2, h // 2)
    try:
        home = B.get_backend().cursor()
    except Exception:
        pass
    mac = ov.hello.get("platform") == "aqua"
    try:
        ov.send({"op": "show", "from": list(home)})
        for i, (name, what, make, pause) in enumerate(tour_steps(w, h, mac)):
            op = make(home[0], home[1])
            op.setdefault("from", list(home))
            out.line("  " + out.ink.paint("%2d" % (i + 1), DUST) + "  " + out.ink.paint("%-7s" % name, BEAM, True) +
                     " " + out.ink.paint(what, ASH))
            ov.send(op)
            sleep(pause)
        ov.send({"op": "hide"}, wait=1.0)
    except KeyboardInterrupt:
        out.line()
        return 130
    finally:
        ov.close()
    out.line("  " + out.ink.paint("✓" if out.u else "+", OK, True) + " " +
             out.ink.paint("That is what you will see while the model drives.", ICE))
    return 0


def cmd_pilot(args: List[str]) -> int:
    out = Out()
    sub = args[0].lower() if args else "status"
    rest = args[1:]
    if sub in ("status", "doctor"):
        return cmd_status(out, rest + (["--gui"] if sub == "doctor" else []))
    if sub in ("on", "enable", "arm"):
        return cmd_on(out, rest)
    if sub in ("off", "disable", "disarm"):
        return cmd_off(out)
    if sub in ("tour", "demo"):
        return cmd_tour(out)
    if sub == "log":
        return cmd_log(out, rest)
    if sub in ("help", "-h", "--help"):
        out.line(USAGE.format(mark=out.mark()))
        return 0
    out.line("pulsarcode pilot: unknown command %r\n" % sub)
    out.line(USAGE.format(mark=out.mark()))
    return 2


def quiet_broken_pipe() -> int:
    """`pulsarcode pilot status | head`: the reader left. Point stdout at devnull so the interpreter's
    final flush does not print a second error, and exit like a shell pipeline would (141)."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)
    except (OSError, ValueError, AttributeError):
        pass
    return 141


def main(argv: Optional[List[str]] = None) -> int:
    try:
        return cmd_pilot(list(sys.argv[1:] if argv is None else argv))
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return quiet_broken_pipe()


if __name__ == "__main__":
    sys.exit(main())
