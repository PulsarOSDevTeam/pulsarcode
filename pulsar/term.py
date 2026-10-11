# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Terminal capabilities and raw keyboard input (POSIX + Windows).

Caps answers "what can this terminal do": color depth (0 none, 4 for the
16 ANSI colors, 8 for the 256-color cube, 24 for truecolor), Unicode, and
whether motion is welcome. Keyboard puts the tty in cbreak mode and turns
raw input into key tokens. Both are built to leave the terminal exactly
as they found it: on normal exit, on Ctrl-C, on SIGTERM/SIGHUP, and
around a Ctrl-Z suspend.
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from typing import Callable, List, Mapping, Optional, Tuple

IS_WIN = os.name == "nt"

# Bumped every time the process resumes from a job-control stop. Screens compare it
# to know their last frame is gone and a full repaint is due.
RESUME_EPOCH = 0


def enable_windows_vt() -> None:
    if not IS_WIN:
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        for handle_id in (-11, -12):
            h = k32.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if k32.GetConsoleMode(h, ctypes.byref(mode)):
                k32.SetConsoleMode(h, mode.value | 0x0004)
        k32.SetConsoleOutputCP(65001)
    except Exception:
        pass


# Terminals that announce truecolor through TERM_PROGRAM even when COLORTERM is lost
# (sudo, ssh and tmux commonly drop it).
_TRUECOLOR_PROGRAMS = ("iTerm.app", "WezTerm", "vscode", "ghostty", "Hyper", "Tabby", "rio", "WarpTerminal")
_TRUECOLOR_TERMS = ("kitty", "ghostty", "wezterm", "alacritty", "foot", "contour", "rio", "-direct")
# TERM values that mean "the 16 ANSI colors, nothing more".
_SIXTEEN_TERMS = ("linux", "cons25", "ansi", "cygwin", "xterm-color", "xterm-16color", "rxvt", "rxvt-unicode",
                  "vt100", "vt102", "vt220", "putty", "screen.linux", "wsvt25", "eterm-color", "konsole-16color")
# TERM values whose fonts rarely carry box drawing, braille or the pulsar mark.
_ASCII_TERMS = ("linux", "cons25", "vt100", "vt102", "vt220", "wsvt25", "ansi")

_DEPTH_NAMES = {"0": 0, "none": 0, "off": 0, "mono": 0,
                "4": 4, "16": 4, "ansi": 4, "ansi16": 4,
                "8": 8, "256": 8, "ansi256": 8,
                "24": 24, "truecolor": 24, "24bit": 24, "rgb": 24}


def detect_color(env: Mapping[str, str], tty: bool = True, is_win: bool = IS_WIN) -> int:
    """Color depth for a terminal described by env: 0, 4 (16 colors), 8 (256) or 24 (truecolor).

    Precedence: PULSAR_COLOR forces a depth; NO_COLOR, TERM=dumb or a non-tty mean 0; then
    COLORTERM, known truecolor terminals, *-256color, known 16-color TERMs, and 256 as the
    safe modern default.
    """
    forced = (env.get("PULSAR_COLOR") or "").strip().lower()
    if forced in _DEPTH_NAMES:
        return _DEPTH_NAMES[forced]
    term = (env.get("TERM") or "").lower()
    if env.get("NO_COLOR") or not tty or term == "dumb":
        return 0
    if (env.get("COLORTERM") or "").lower() in ("truecolor", "24bit"):
        return 24
    if env.get("TERM_PROGRAM") in _TRUECOLOR_PROGRAMS or env.get("WT_SESSION") or any(k in term for k in _TRUECOLOR_TERMS):
        return 24
    if is_win:
        return 24  # Windows 10+ consoles (and mintty) take 24-bit SGR once VT processing is on
    if "256" in term:
        return 8
    if term in _SIXTEEN_TERMS:
        return 4
    return 8


def detect_unicode(env: Mapping[str, str], encoding: str, is_win: bool = IS_WIN) -> bool:
    forced = env.get("PULSAR_ASCII")
    if forced == "1":
        return False
    if forced == "0":
        return True
    if (env.get("TERM") or "").lower() in _ASCII_TERMS:
        return False  # the Linux VT and serial consoles: ASCII renders, everything else is a gamble
    if not is_win and not locale_is_utf8(env):
        # LANG=C: Python coerces its own streams to UTF-8 (PEP 538/540), but the terminal still runs in
        # the C locale, so the stream encoding alone would claim Unicode.
        return False
    return "utf" in (encoding or "").lower() or is_win


# The LC_CTYPE values Python's C-locale coercion (PEP 538) writes into its own environment.
_COERCED_CTYPE = ("C.UTF-8", "C.utf8", "UTF-8")


def locale_is_utf8(env: Mapping[str, str]) -> bool:
    """False when the effective locale (LC_ALL, else LC_CTYPE, else LANG) is C, POSIX or a non-UTF-8
    charset. True when it is UTF-8, or when none of them is set (the stream encoding decides then)."""
    lang = (env.get("LANG") or "").strip().lower()
    if (not (env.get("LC_ALL") or "").strip() and lang in ("c", "posix")
            and (env.get("LC_CTYPE") or "").strip() in _COERCED_CTYPE):
        # PEP 538: Python itself replaced the C locale with LC_CTYPE=C.UTF-8 in its environment;
        # the terminal around it still runs with LANG=C.
        return False
    for var in ("LC_ALL", "LC_CTYPE", "LANG"):
        value = (env.get(var) or "").strip()
        if not value:
            continue
        low = value.lower()
        if low in ("c", "posix"):
            return False
        charset = low.split(".", 1)[1].split("@", 1)[0] if "." in low else ""
        return charset.replace("-", "").replace("_", "") in ("utf8",)
    return True


def _fx_mode(env: Mapping[str, str]) -> str:
    return (env.get("PULSAR_FX") or "auto").strip().lower()


FX_OFF = ("0", "off", "false", "none", "no")


class Caps:
    def __init__(self, stream=None) -> None:
        self.out = stream or sys.stderr
        self.tty = _isatty(self.out) and _isatty(sys.stdin)
        env = os.environ
        self.color = detect_color(env, _isatty(self.out))
        enc = getattr(self.out, "encoding", "") or ""
        self.unicode = detect_unicode(env, enc)
        self.fx = _fx_mode(env)
        self.reduced = env.get("PULSAR_REDUCED_MOTION") == "1"
        self.ci = bool(env.get("CI"))
        self.motion = self.tty and self.color > 0 and self.fx not in FX_OFF and self.fx != "plain" \
            and not self.reduced and not self.ci

    @property
    def size(self) -> Tuple[int, int]:
        """Live size of the terminal (re-read every call, so resizes are picked up mid-animation)."""
        cols = rows = 0
        for stream in (self.out, sys.stdin, sys.stdout):
            try:
                s = os.get_terminal_size(stream.fileno())
            except (AttributeError, OSError, ValueError):
                continue
            cols, rows = s.columns, s.lines
            if cols > 0 and rows > 0:
                break
        if cols <= 0 or rows <= 0:
            s = shutil.get_terminal_size((80, 24))
            cols, rows = s.columns, s.lines
        return max(20, cols), max(8, rows)


def _isatty(stream) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

_SEQ = {
    "\x1b[A": "up", "\x1b[B": "down", "\x1b[C": "right", "\x1b[D": "left",
    "\x1bOA": "up", "\x1bOB": "down", "\x1bOC": "right", "\x1bOD": "left",
    "\x1b[H": "home", "\x1b[F": "end", "\x1bOH": "home", "\x1bOF": "end",
    "\x1b[1~": "home", "\x1b[4~": "end", "\x1b[7~": "home", "\x1b[8~": "end",
    "\x1b[5~": "pgup", "\x1b[6~": "pgdn", "\x1b[3~": "delete", "\x1b[Z": "shift-tab",
}
_SEQ_ORDER = sorted(_SEQ, key=len, reverse=True)
_CTRL = {"\r": "enter", "\n": "enter", "\t": "tab", "\x7f": "backspace", "\x08": "backspace",
         "\x03": "ctrl-c", "\x04": "ctrl-d", "\x15": "ctrl-u", "\x17": "ctrl-w", "\x0f": "ctrl-o",
         "\x0e": "down", "\x10": "up", "\x01": "home", "\x05": "end", "\x0c": "ctrl-l", "\x0b": "ctrl-k"}

PASTE_START = "\x1b[200~"
PASTE_END = "\x1b[201~"


def parse_keys(buf: str) -> List[str]:
    """Split a raw input burst into key tokens. Pastes become 'paste:<text>'."""
    keys: List[str] = []
    i = 0
    while i < len(buf):
        if buf.startswith(PASTE_START, i):
            end = buf.find(PASTE_END, i + len(PASTE_START))
            if end < 0:
                keys.append("paste:" + buf[i + len(PASTE_START):])
                break
            keys.append("paste:" + buf[i + len(PASTE_START):end])
            i = end + len(PASTE_END)
            continue
        ch = buf[i]
        if ch == "\x1b":
            if i + 1 >= len(buf):
                keys.append("esc")
                i += 1
                continue
            matched = False
            for seq in _SEQ_ORDER:
                if buf.startswith(seq, i):
                    keys.append(_SEQ[seq])
                    i += len(seq)
                    matched = True
                    break
            if matched:
                continue
            nxt = buf[i + 1]
            if nxt in "[O":
                j = i + 2
                while j < len(buf) and not ("@" <= buf[j] <= "~"):
                    j += 1
                i = j + 1
                continue  # unknown CSI: swallow
            if nxt == "\x1b":
                keys.append("esc")
                i += 1
                continue
            keys.append("alt-" + nxt)
            i += 2
            continue
        if ch in _CTRL:
            keys.append(_CTRL[ch])
            if ch == "\r" and i + 1 < len(buf) and buf[i + 1] == "\n":
                i += 1
        elif ord(ch) < 32:
            pass
        else:
            # Coalesce a burst of printable text (an unbracketed paste) into one token.
            j = i
            while j < len(buf) and buf[j] not in _CTRL and buf[j] != "\x1b" and ord(buf[j]) >= 32:
                j += 1
            run = buf[i:j]
            if len(run) > 1:
                keys.append("paste:" + run)
            else:
                keys.append(run)
            i = j
            continue
        i += 1
    return keys


# ---------------------------------------------------------------------------
# Terminal state guards
# ---------------------------------------------------------------------------

# Escape sequences registered by whoever currently owns the screen: (leave, enter).
# Written straight to the fd on suspend/resume so a signal can never re-enter Python's
# buffered writer mid-frame.
_SCREEN_SEQ: List[Tuple[str, str]] = []
_GUARD_LOCK = threading.Lock()


def register_screen(leave: str, enter: str) -> Callable[[], None]:
    """Remember how to leave and re-enter a full-screen mode across a Ctrl-Z. Returns an unregister."""
    item = (leave, enter)
    with _GUARD_LOCK:
        _SCREEN_SEQ.append(item)

    def unregister() -> None:
        with _GUARD_LOCK:
            try:
                _SCREEN_SEQ.remove(item)
            except ValueError:
                pass
    return unregister


def _fd_write(fd: Optional[int], text: str) -> None:
    if fd is None or not text:
        return
    try:
        os.write(fd, text.encode("utf-8", "replace"))
    except OSError:
        pass


class Keyboard:
    """Raw, no-echo keyboard reader. Use as a context manager.

    On POSIX it also makes SIGTERM and SIGHUP unwind through the context managers (so the
    terminal is restored) when nobody else handles them, and turns Ctrl-Z into a clean
    suspend: the screen and tty modes are restored while stopped and re-applied on resume.
    """

    def __init__(self, out=None) -> None:
        self.out = out or sys.stderr
        self.fd: Optional[int] = None
        self._saved = None
        self._cbreak = None
        self._queue: List[str] = []
        self._prev_handlers: dict = {}
        self._out_fd: Optional[int] = None

    def __enter__(self) -> "Keyboard":
        if IS_WIN:
            enable_windows_vt()
        else:
            import termios
            import tty
            self.fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
            attrs = termios.tcgetattr(self.fd)
            attrs[3] &= ~termios.ECHO
            termios.tcsetattr(self.fd, termios.TCSADRAIN, attrs)
            self._cbreak = termios.tcgetattr(self.fd)
            try:
                termios.tcflush(self.fd, termios.TCIFLUSH)
            except OSError:
                pass
            try:
                self._out_fd = self.out.fileno()
            except (AttributeError, OSError, ValueError):
                self._out_fd = None
            self._install_signals()
        self.out.write("\x1b[?2004h")
        self.out.flush()
        return self

    def __exit__(self, *exc) -> None:
        try:
            self.out.write("\x1b[?2004l")
            self.out.flush()
        except (OSError, ValueError):
            pass
        self._restore_signals()
        if not IS_WIN and self.fd is not None and self._saved is not None:
            import termios
            try:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self._saved)
            except (OSError, termios.error):
                pass

    # -- signals ------------------------------------------------------------------
    def _install_signals(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        import signal

        def bail(signum, frame):  # unwind through every `with`, restoring the terminal
            raise KeyboardInterrupt

        for name in ("SIGTERM", "SIGHUP"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                if signal.getsignal(sig) is signal.SIG_DFL:   # never steal someone else's handler
                    self._prev_handlers[sig] = signal.signal(sig, bail)
            except (OSError, ValueError):
                pass
        tstp = getattr(signal, "SIGTSTP", None)
        if tstp is not None:
            try:
                if signal.getsignal(tstp) is signal.SIG_DFL:
                    self._prev_handlers[tstp] = signal.signal(tstp, self._on_suspend)
            except (OSError, ValueError):
                pass

    def _restore_signals(self) -> None:
        if not self._prev_handlers:
            return
        import signal
        for sig, handler in self._prev_handlers.items():
            try:
                signal.signal(sig, handler)
            except (OSError, ValueError, TypeError):
                pass
        self._prev_handlers = {}

    def _on_suspend(self, signum, frame) -> None:
        """Ctrl-Z: hand the terminal back in the state we found it, stop, then take it again."""
        global RESUME_EPOCH
        import signal
        import termios
        with _GUARD_LOCK:
            seqs = list(_SCREEN_SEQ)
        _fd_write(self._out_fd, "\x1b[?2004l" + "".join(leave for leave, _ in reversed(seqs)))
        if self.fd is not None and self._saved is not None:
            try:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self._saved)
            except (OSError, termios.error):
                pass
        try:
            signal.signal(signal.SIGTSTP, signal.SIG_DFL)
            os.kill(os.getpid(), signal.SIGTSTP)          # actually stop; returns after SIGCONT
        finally:
            try:
                signal.signal(signal.SIGTSTP, self._on_suspend)
            except (OSError, ValueError):
                pass
            if self.fd is not None and self._cbreak is not None:
                try:
                    termios.tcsetattr(self.fd, termios.TCSADRAIN, self._cbreak)
                except (OSError, termios.error):
                    pass
            _fd_write(self._out_fd, "".join(enter for _, enter in seqs) + "\x1b[?2004h")
            RESUME_EPOCH += 1

    # -- reading ------------------------------------------------------------------
    def _read_raw(self, timeout: Optional[float]) -> str:
        if IS_WIN:
            import msvcrt
            end = None if timeout is None else time.time() + timeout
            while not msvcrt.kbhit():  # type: ignore[attr-defined]
                if end is not None and time.time() >= end:
                    return ""
                time.sleep(0.01)
            chars = []
            while msvcrt.kbhit():  # type: ignore[attr-defined]
                c = msvcrt.getwch()  # type: ignore[attr-defined]
                if c in ("\x00", "\xe0"):
                    code = msvcrt.getwch()  # type: ignore[attr-defined]
                    chars.append({"H": "\x1b[A", "P": "\x1b[B", "M": "\x1b[C", "K": "\x1b[D", "G": "\x1b[H",
                                  "O": "\x1b[F", "I": "\x1b[5~", "Q": "\x1b[6~", "S": "\x1b[3~"}.get(code, ""))
                else:
                    chars.append(c)
            return "".join(chars)
        import select
        assert self.fd is not None
        try:
            ready, _, _ = select.select([self.fd], [], [], timeout)
        except (OSError, ValueError):
            return ""
        if not ready:
            return ""
        try:
            data = os.read(self.fd, 4096)
        except OSError:
            return ""
        # Gather the rest of an escape sequence or paste burst.
        while True:
            more, _, _ = select.select([self.fd], [], [], 0.025 if data.endswith(b"\x1b") or b"\x1b[200~" in data
                                       and b"\x1b[201~" not in data else 0.004)
            if not more:
                break
            chunk = os.read(self.fd, 65536)
            if not chunk:
                break
            data += chunk
        return data.decode("utf-8", errors="replace")

    def read(self, timeout: Optional[float] = None) -> Optional[str]:
        """Next key token, or None on timeout."""
        if self._queue:
            return self._queue.pop(0)
        raw = self._read_raw(timeout)
        if not raw:
            return None
        self._queue.extend(parse_keys(raw))
        return self._queue.pop(0) if self._queue else None

    def pending(self) -> bool:
        return bool(self._queue)
