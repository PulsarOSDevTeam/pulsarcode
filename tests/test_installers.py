# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""The installers and the `pulsarcode` shim: syntax, portability, PATH handling, integrity.

Everything runs offline in a minimal environment (never the caller's): curl and wget are stubs, uv
is a stub, and the Python dependencies come from the interpreter running these tests. HOME stays the
caller's own, because on macOS a program started with a made-up HOME can raise a Keychain dialog;
whatever the installer writes goes to the test's scratch folders instead (PULSAR_HOME, PULSAR_BIN_DIR,
TMPDIR, and PULSAR_NO_PATH=1 for the startup files). Logic that reads HOME (startup files,
~/.local/bin/claude) runs with a scratch HOME that only bash itself sees: it is not exported, so no
program a test starts ever gets it. Extra interpreters are used when present: PULSAR_TEST_PWSH (or
`pwsh` on PATH) for install.ps1, PULSAR_TEST_BUSYBOX and PULSAR_TEST_BASH32 for the shim and the
installer under busybox ash and bash 3.2.
"""

import glob
import hashlib
import io
import json
import os
import re
import select
import shutil
import site
import socket
import stat
import subprocess
import sys
import sysconfig
import tarfile
import time
from pathlib import Path

import pytest

import pulsar

REPO = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO / "install.sh"
INSTALL_PS1 = REPO / "install.ps1"
SHIM = REPO / "pulsarcode"
GITATTRIBUTES = REPO / ".gitattributes"
POSIX = os.name == "posix" and shutil.which("bash") is not None
needs_posix = pytest.mark.skipif(not POSIX, reason="needs a POSIX system with bash")
SYS_PATH = "/usr/bin:/bin"
PWSH = (os.environ.get("PULSAR_TEST_PWSH") or shutil.which("pwsh")
        or (shutil.which("powershell.exe") if os.name == "nt" else None) or "")   # Windows PowerShell 5.1 itself
needs_pwsh = pytest.mark.skipif(not PWSH, reason="PowerShell not installed")
WINDOWS_ENV = ("SYSTEMROOT", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "APPDATA",
               "LOCALAPPDATA", "PROGRAMDATA", "PATH")


def _non_bash_sh():
    """argv of a POSIX sh that is not bash (dash, busybox ash, zsh as sh), or None."""
    candidates = []
    if os.environ.get("PULSAR_TEST_BUSYBOX"):
        candidates.append([os.environ["PULSAR_TEST_BUSYBOX"], "sh"])
    candidates += [["/bin/sh"], ["/bin/dash"], ["/usr/bin/dash"]]
    for argv in candidates:
        if os.path.exists(argv[0]):
            r = subprocess.run(argv + ["-c", 'printf "%s" "${BASH_VERSION:-}"'], capture_output=True, text=True,
                               env={"PATH": SYS_PATH})
            if r.returncode == 0 and not r.stdout:
                return argv
    return None


REAL_HOME = os.environ.get("HOME") or os.path.expanduser("~")


def _env(tmp_path, **extra):
    """A minimal environment: nothing from the caller but HOME (no keys, no proxies, no PATH).

    Everything the installer could write lands in tmp_path: PULSAR_HOME, PULSAR_BIN_DIR and TMPDIR point
    there and PULSAR_NO_PATH=1 keeps the startup files untouched. A value of None removes the variable."""
    tmp = tmp_path / "tmp"
    tmp.mkdir(exist_ok=True)
    env = {"HOME": REAL_HOME, "PATH": SYS_PATH, "TMPDIR": str(tmp), "TERM": "dumb", "LANG": "C",
           "PULSAR_FX": "off", "PULSAR_OFFLINE": "1", "PULSAR_HOME": str(tmp_path / "ph-default"),
           "PULSAR_BIN_DIR": str(tmp_path / "bin-default"), "PULSAR_NO_PATH": "1"}
    env.update({k: v for k, v in extra.items() if v is not None})
    for k in [k for k, v in extra.items() if v is None]:
        env.pop(k, None)
    assert env.get("HOME") in (None, REAL_HOME), "never a made-up HOME: use _bash(..., home=...)"
    return env


def _scratch_home(root: Path) -> Path:
    """A folder that stands in for HOME inside bash (see _bash)."""
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    return home


def _bash(script, env, *args, cwd=None, timeout=120, stdin=None, home=None):
    """Run `script` in bash after sourcing install.sh (which then defines everything and runs nothing).

    home: a scratch folder bash uses as HOME while the script runs. It stays a shell variable (export -n),
    so the programs the script starts get no HOME at all, never a made-up one."""
    full = 'source "$INSTALL_SH"\n'
    if home is not None:
        full += 'export -n HOME; HOME="$SCRATCH_HOME"\n'
        env = dict(env, SCRATCH_HOME=str(home))
    env = dict(env, INSTALL_SH=str(INSTALL_SH))
    return subprocess.run(["bash", "-c", full + script, "test"] + list(args), env=env, cwd=cwd or env["TMPDIR"],
                          input=stdin, capture_output=True, text=True, timeout=timeout)


def _stub(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def _hostile_dir(tmp_path) -> Path:
    """A working directory that would hijack any Python started inside it the wrong way."""
    evil = tmp_path / "evilrepo"
    (evil / "pulsar").mkdir(parents=True)
    mark = tmp_path / "pwned.txt"
    payload = "open(%r, 'a').write(__name__ + '\\n')\nraise SystemExit('PWNED')\n" % str(mark)
    for rel in ("pulsar/__init__.py", "pulsar/__main__.py", "typing.py", "runpy.py", "json.py", "sitecustomize.py"):
        (evil / rel).write_text(payload)
    return evil


# ---------------------------------------------------------------------------
# syntax, encodings, line endings
# ---------------------------------------------------------------------------

@needs_posix
def test_scripts_parse():
    for cmd in (["bash", "-n", str(INSTALL_SH)], ["bash", "-n", str(SHIM)], ["/bin/sh", "-n", str(SHIM)]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        assert r.returncode == 0, (cmd, r.stderr)
    old_bash = os.environ.get("PULSAR_TEST_BASH32")
    if old_bash:
        for f in (INSTALL_SH, SHIM):
            assert subprocess.run([old_bash, "-n", str(f)]).returncode == 0, f


def test_install_ps1_is_pure_ascii():
    data = INSTALL_PS1.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    bad = [(i + 1, line) for i, line in enumerate(data.splitlines()) if any(b > 0x7E or (b < 0x20 and b not in (9, 13))
                                                                        for b in line)]
    assert not bad, "install.ps1 must stay pure ASCII (Windows PowerShell 5.1 reads it as ANSI): %r" % bad[:3]


def test_shell_scripts_have_lf_endings_and_no_em_dash():
    for f in (INSTALL_SH, SHIM, INSTALL_PS1, Path(__file__)):
        data = f.read_bytes()
        assert "\u2014".encode("utf-8") not in data, f
        if f.suffix != ".ps1":
            assert b"\r" not in data, f
    assert SHIM.read_bytes().startswith(b"#!/bin/sh\n")          # POSIX: busybox and Alpine have no bash


def test_gitattributes_pins_line_endings():
    rules = {}
    for line in GITATTRIBUTES.read_text(encoding="ascii").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            pattern, *attrs = line.split()
            rules[pattern] = set(attrs)
    for pattern in ("*.sh", "pulsarcode", "*.py"):
        assert {"text", "eol=lf"} <= rules[pattern], pattern
    for pattern in ("*.ps1", "*.cmd", "*.bat"):
        assert {"text", "eol=crlf"} <= rules[pattern], pattern
    assert "binary" in rules["*.png"]


# ---------------------------------------------------------------------------
# the shim
# ---------------------------------------------------------------------------

@needs_posix
def test_shim_is_posix_and_survives_missing_home(tmp_path):
    evil = _hostile_dir(tmp_path)
    link_dir = tmp_path / "bin" / "nested"
    link_dir.mkdir(parents=True)
    os.symlink(os.path.relpath(str(SHIM), str(link_dir)), str(link_dir / "rel"))   # a relative chain
    os.symlink(str(link_dir / "rel"), str(tmp_path / "bin" / "pulsarcode"))
    shells = [["/bin/sh"], ["bash"]]
    if os.environ.get("PULSAR_TEST_BUSYBOX"):
        shells.append([os.environ["PULSAR_TEST_BUSYBOX"], "sh"])
    if os.environ.get("PULSAR_TEST_BASH32"):
        shells.append([os.environ["PULSAR_TEST_BASH32"]])
    for sh in shells:
        for env in (_env(tmp_path, PULSAR_PYTHON=sys.executable),
                    _env(tmp_path, HOME=None, PULSAR_PYTHON=sys.executable)):      # HOME unset: user database
            r = subprocess.run(sh + [str(tmp_path / "bin" / "pulsarcode"), "version"], env=env, cwd=str(evil),
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
            assert r.returncode == 0 and r.stdout.startswith("pulsarcode " + pulsar.__version__), (sh, r.stderr)
    assert not (tmp_path / "pwned.txt").exists()


# ---------------------------------------------------------------------------
# install.sh: not bash, HOME, locale, Ctrl+C
# ---------------------------------------------------------------------------

@needs_posix
def test_sh_preamble_reexecs_bash_or_explains(tmp_path):
    env = _env(tmp_path)
    # `sh install.sh` with bash available: re-run under bash (the bash-only option parser answers).
    # On macOS /bin/sh is bash itself, which simply carries on.
    r = subprocess.run(["/bin/sh", str(INSTALL_SH), "--bogus"], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and "unknown option --bogus" in r.stderr, r.stderr
    sh = _non_bash_sh()
    if sh is None:
        pytest.skip("no POSIX sh other than bash here (dash, busybox)")
    # `curl ... | sh`: the text is on stdin, so it can only explain
    r = subprocess.run(sh, env=env, input=INSTALL_SH.read_text(encoding="utf-8"), capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 1 and "| bash" in r.stderr and "syntax error" not in r.stderr, r.stderr
    # an Alpine-like system: sh and apk, no bash at all
    alpine = tmp_path / "alpine"
    alpine.mkdir()
    os.symlink(os.path.realpath(sh[0]), str(alpine / "sh"))       # busybox picks its applet from the name
    _stub(alpine / "apk", "exit 0\n")
    for how in ("file", "pipe"):
        cmd = [str(alpine / "sh")] + ([str(INSTALL_SH)] if how == "file" else [])
        r = subprocess.run(cmd, env=dict(env, PATH=str(alpine)), capture_output=True, text=True, timeout=60,
                           input=None if how == "file" else INSTALL_SH.read_text(encoding="utf-8"))
        assert r.returncode == 1 and "apk add bash" in r.stderr and "not installed" in r.stderr, (how, r.stderr)


@needs_posix
def test_home_unset_is_resolved_not_fatal(tmp_path):
    import pwd
    want = pwd.getpwuid(os.getuid()).pw_dir
    r = _bash('printf "%s|%s\\n" "$HOME" "$PULSAR_HOME"', _env(tmp_path, HOME=None, PULSAR_HOME=None),
              cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "%s|%s/.pulsarcode" % (want, want)


@needs_posix
def test_tools_come_only_from_absolute_path_entries(tmp_path):
    """An empty, "." or relative PATH entry names the folder the installer runs from: a cloned repository
    there could supply its own tar, curl, python3 or uv. The installer keeps the absolute entries only."""
    evil = tmp_path / "repo"
    (evil / "bin").mkdir(parents=True)
    mark = tmp_path / "pwned.txt"
    for tool in ("tar", "curl", "python3", "uv"):
        for d in (evil, evil / "bin"):
            _stub(d / tool, 'echo "%s" >> "%s"\n' % (tool, mark))
    for path in (":.:bin:" + SYS_PATH, SYS_PATH + "::bin:.", "bin:" + SYS_PATH + ":"):
        r = _bash('printf "PATH=%s\\n" "$PATH"; for t in tar curl python3; do command -v "$t" || echo "no $t"; done; '
                  'need tar && echo tar-ok; find_uv || echo no-uv',
                  _env(tmp_path, PATH=path), cwd=str(evil), home=_scratch_home(tmp_path))
        assert r.returncode == 0, (path, r.stdout + r.stderr)
        assert r.stdout.splitlines()[0] == "PATH=" + SYS_PATH, (path, r.stdout)
        found = r.stdout.splitlines()[1:4]
        assert all(f.startswith("/") and not f.startswith(str(evil)) for f in found if not f.startswith("no ")), found
        assert "tar-ok" in r.stdout and "no-uv" in r.stdout, r.stdout
    assert not mark.exists(), mark.read_text()
    # nothing absolute left (bash itself is started by its path, and the PATH is set inside)
    r = subprocess.run(["/bin/bash", "-c", 'PATH=.:bin; source "$INSTALL_SH"; printf "PATH=%s\\n" "$PATH"', "test"],
                       env=dict(_env(tmp_path), INSTALL_SH=str(INSTALL_SH)), cwd=str(evil), capture_output=True,
                       text=True, timeout=60)
    assert r.stdout.strip() == "PATH=/usr/bin:/bin", r.stdout + r.stderr


@needs_posix
def test_relative_paths_resolve_against_the_starting_folder(tmp_path):
    """The installer works from inside PULSAR_HOME once the home is checked (so uv and npm never read a
    project's settings), so every path it was given is made absolute first, against the folder it was
    started from, the way install.ps1 resolves them; "~" is the home folder, and a relative TMPDIR or one
    that is gone falls back to /tmp."""
    start = tmp_path / "start here"
    start.mkdir()
    (start / "ca.pem").write_text("x\n")
    script = 'printf "%s\\n" "$PULSAR_HOME" "$BIN_DIR" "$TMPBASE" "$CA_FILE" "${SSL_CERT_FILE:-}"'
    r = _bash(script, _env(tmp_path, PULSAR_HOME="ph", PULSAR_BIN_DIR="tools/bin", TMPDIR="rel-tmp",
                           SSL_CERT_FILE="ca.pem"), cwd=str(start))
    real = os.path.realpath(str(start))
    assert r.returncode == 0 and r.stdout.splitlines() == [real + "/ph", real + "/tools/bin", "/tmp",
                                                           real + "/ca.pem", real + "/ca.pem"], r.stdout + r.stderr
    r = _bash(script, _env(tmp_path, PULSAR_HOME="~/pc", PULSARCODE_BIN_DIR="~/b", PULSAR_BIN_DIR=None),
              cwd=str(start))
    lines = r.stdout.splitlines()
    assert lines[:2] == [REAL_HOME + "/pc", REAL_HOME + "/b"] and lines[2] == str(tmp_path / "tmp"), lines
    # main enters the home right after checking it, before anything runs there
    main = INSTALL_SH.read_text(encoding="utf-8").split("\nmain() {", 1)[1]
    assert main.index("prepare_home") < main.index("enter_home") < main.index("check_bin") < main.index("setup_python")
    home = tmp_path / "the home"
    r = _bash('prepare_home; enter_home; pwd -P', _env(tmp_path, PULSAR_HOME=str(home)), cwd=str(start))
    assert r.returncode == 0 and r.stdout.strip() == os.path.realpath(str(home)), r.stdout + r.stderr
    # and the launch at the end goes back to the starting folder, with the PATH the installer was given
    launch = main[main.index("Launching."):]
    assert launch.index('cd "$START_DIR"') < launch.index('PATH="$ORIG_PATH" exec "$BIN_DIR/pulsarcode"'), launch


@needs_posix
def test_glyphs_follow_the_locale(tmp_path):
    r = _bash('printf "%s %s %s\\n" "$UTF8" "${SPIN[0]}" "$G_OK"', _env(tmp_path, LANG="C"))
    assert r.stdout.split() == ["0", "|", "+"]
    r = _bash('printf "%s %s\\n" "$UTF8" "${#SPIN[@]}"', _env(tmp_path, LANG="C", LC_ALL="en_US.UTF-8"))
    assert r.stdout.split() == ["1", "10"]
    r = _bash('printf "%s\\n" "$UTF8"', _env(tmp_path, LANG="en_US.UTF-8", LC_ALL="C"))
    assert r.stdout.split() == ["0"]                             # LC_ALL wins over LANG


@needs_posix
def test_a_failed_run_never_ends_with_success(tmp_path):
    """An install run that ends before commit_install has failed, however it ended, so `pulsarcode update` never
    reports a failed install as done. bash ends a script with the status of its EXIT trap, and enters that trap
    with 0 after an unset variable (set -u): the cleanup there answers for the run. A verified run, the hand-off
    to install.ps1 and functions used one at a time (as these tests do) keep their own status."""
    env = _env(tmp_path)
    as_main = "ROLL_BACK=1; "                                       # what main() sets
    for script, want in ((as_main + 'f() { printf "%s" "$NOT_SET_ANYWHERE"; }; f; echo after', 1),
                         (as_main + "false; echo after", 1), (as_main + 'die "stop here"; echo after', 1),
                         (as_main + "COMMITTED=1; echo verified", 0), ("echo fine", 0), ("false; echo after", 1)):
        r = _bash(script, env)
        assert r.returncode == want and "after" not in r.stdout, (script, r.returncode, r.stdout, r.stderr)
    handoff = INSTALL_SH.read_text(encoding="utf-8").split("\nwindows_handoff() {", 1)[1].split("\n}\n", 1)[0]
    assert handoff.index("ROLL_BACK=0") < handoff.index("exit $rc"), handoff        # install.ps1's status stands


def _process_gone(pid: int, wait: float = 3.0) -> bool:
    end = time.time() + wait
    while time.time() < end:
        st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        if not st or st.startswith("Z"):
            return True
        time.sleep(0.05)
    return False


@needs_posix
@pytest.mark.parametrize("lang", ["C", "C.UTF-8"])
def test_spinner_ascii_without_utf8_and_ctrl_c_restores_the_cursor(tmp_path, lang):
    pty = pytest.importorskip("pty")
    mark = tmp_path / "step.pid"
    env = _env(tmp_path, TERM="xterm-256color", LANG=lang, INSTALL_SH=str(INSTALL_SH))
    script = ('source "$INSTALL_SH"; run "slow step" sh -c "echo \\$\\$ > \\"$1\\"; exec sleep 30"; '
              'echo after-run')
    pid, fd = pty.fork()
    if pid == 0:                                                   # child: becomes bash on the pty
        try:
            os.execve("/bin/sh", ["/bin/sh", "-c", 'exec bash -c "$0" x "$1"', script, str(mark)], env)
        finally:
            os._exit(127)
    out = b""

    def pump(seconds, until=None):
        nonlocal out
        end = time.time() + seconds
        while time.time() < end:
            if until is not None and until():
                return
            ready, _, _ = select.select([fd], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    return
                if not chunk:
                    return
                out += chunk

    pump(10, until=lambda: mark.exists() and out.count(b"slow step") >= 3)
    before = len(out)
    assert mark.exists(), out
    os.write(fd, b"\x03")
    pump(5)
    status = None
    end = time.time() + 10                                         # bounded: a wedged bash fails, never hangs
    while status is None and time.time() < end:
        done, st = os.waitpid(pid, os.WNOHANG)
        if done:
            status = st
        else:
            pump(0.1)
    # The step itself is ended only by the installer under test, never from here: its process number was
    # read from a file and proves nothing. A step the installer failed to end is a `sleep 30` that ends
    # on its own.
    if status is None:
        os.kill(pid, 9)                                            # our own child, not reaped yet
        os.waitpid(pid, 0)
        os.close(fd)
        pytest.fail("bash did not exit within 10 s of Ctrl+C; output: %r" % out[-400:])
    os.close(fd)
    step = int(mark.read_text())
    assert b"\x1b[?25l" in out[:before]                             # the spinner hid the cursor
    assert b"\x1b[?25h" in out[before:]                             # and Ctrl+C brought it back
    assert b"after-run" not in out
    assert os.WEXITSTATUS(status) == 130
    assert _process_gone(step), "the running step outlived the installer"
    if lang == "C":
        assert all(b < 0x80 for b in out), out[:200]               # no UTF-8 cut into bytes


# ---------------------------------------------------------------------------
# install.sh: PATH and the shell startup file
# ---------------------------------------------------------------------------

def _unwritable_rc(tmp_path, home: Path) -> Path:
    rc = home / ".bashrc"
    if os.geteuid() != 0:
        rc.write_text("# managed elsewhere\n")
        rc.chmod(0o444)
        home.chmod(0o555)
    elif os.path.exists("/proc/version"):
        os.symlink("/proc/version", str(rc))                       # even root cannot append there
    else:
        pytest.skip("running as root without /proc: no file root cannot write")
    return rc


@needs_posix
def test_read_only_rc_warns_and_prints_the_line(tmp_path):
    env = _env(tmp_path, SHELL="/bin/bash")
    home = _scratch_home(tmp_path)
    _unwritable_rc(tmp_path, home)
    bin_dir = tmp_path / "my bin"
    try:
        r = _bash('BIN_DIR="$1"; OS=linux; add_to_path; echo "rc=$? hint=$PATH_HINT"; echo continuing',
                  env, str(bin_dir), home=home)
    finally:
        home.chmod(0o755)
    assert r.returncode == 0, r.stderr
    assert "could not write ~/.bashrc" in r.stdout
    assert 'export PATH="%s:$PATH"' % bin_dir in r.stdout
    assert "rc=0 hint=2" in r.stdout and "continuing" in r.stdout


@needs_posix
def test_path_line_is_added_once_to_the_right_file(tmp_path):
    home = _scratch_home(tmp_path)
    env = _env(tmp_path, SHELL="/usr/bin/zsh", ZDOTDIR=str(tmp_path / "zdot"))
    r = _bash('BIN_DIR="$1"; OS=linux; add_to_path; add_to_path; echo "hint=$PATH_HINT"', env, str(tmp_path / "b"),
              home=home)
    assert r.returncode == 0 and "hint=1" in r.stdout, r.stderr
    text = (tmp_path / "zdot" / ".zshrc").read_text()
    assert text.count("# Pulsar Code") == 1 and 'export PATH="%s:$PATH"' % (tmp_path / "b") in text
    env = _env(tmp_path, SHELL="/usr/local/bin/fish", XDG_CONFIG_HOME=str(tmp_path / "xdg"))
    r = _bash('BIN_DIR="$1"; OS=linux; add_to_path', env, str(tmp_path / "b"), home=home)
    assert "fish_add_path -g %s\n" % (tmp_path / "b") in (tmp_path / "xdg" / "fish" / "config.fish").read_text()
    # a folder name that would run code inside the startup file is never written there
    for bad in ("$(touch x)", "it's", 'a"b'):
        env = _env(tmp_path, SHELL="/bin/bash")
        r = _bash('BIN_DIR="$1"; OS=linux; add_to_path; echo "hint=$PATH_HINT"', env, str(tmp_path / bad), home=home)
        assert "hint=2" in r.stdout and not (home / ".bashrc").exists(), bad
    assert sorted(os.listdir(str(home))) == []                    # neither file landed in HOME


@needs_posix
def test_fish_path_line_is_global_never_universal(tmp_path, monkeypatch):
    """fish gets `fish_add_path -g`, which lives only in config.fish: plain fish_add_path also stores the
    folder in fish's universal fish_user_paths, which no edit of a startup file (so no uninstall) takes
    back. A line an earlier install wrote (plain fish_add_path, v1's set -gx PATH) counts as present."""
    from pulsar import cli
    home = _scratch_home(tmp_path)
    xdg = tmp_path / "xdg"
    rc = xdg / "fish" / "config.fish"
    env = _env(tmp_path, SHELL="/usr/bin/fish", XDG_CONFIG_HOME=str(xdg))
    for name in ("plain", "with space"):
        r = _bash('BIN_DIR="$1"; OS=linux; add_to_path; add_to_path; echo "hint=$PATH_HINT"', env,
                  str(tmp_path / name), home=home)
        assert r.returncode == 0 and "hint=1" in r.stdout, r.stdout + r.stderr
    text = rc.read_text()
    assert "\nfish_add_path -g %s\n" % (tmp_path / "plain") in text
    assert "\nfish_add_path -g '%s'\n" % (tmp_path / "with space") in text
    assert text.count("# Pulsar Code") == 2 and not re.search(r"(?m)^fish_add_path (?!-g )", text), text
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    for name in ("plain", "with space"):
        assert (rc, str(tmp_path / name)) in cli.rc_blocks(str(home)), name
        assert not cli._fish_universal(rc, str(tmp_path / name)), name      # nothing left behind in fish
    # what earlier installs wrote: left as it is, and no second block
    for old in ("# Pulsar Code\nfish_add_path %s\n", '# pulsarcode\nset -gx PATH "%s" $PATH\n'):
        rc.write_text("set -x A 1\n\n" + old % (tmp_path / "plain"))
        before = rc.read_text()
        r = _bash('BIN_DIR="$1"; OS=linux; add_to_path; echo "hint=$PATH_HINT"', env, str(tmp_path / "plain"),
                  home=home)
        assert r.returncode == 0 and "hint=1" in r.stdout and "added" not in r.stdout, r.stdout + r.stderr
        assert rc.read_text() == before, old
    assert sorted(os.listdir(str(home))) == []


@needs_posix
def test_macos_bash_writes_the_startup_file_bash_reads(tmp_path):
    """A macOS terminal opens a login shell, and bash reads only the first of ~/.bash_profile, ~/.bash_login
    and ~/.profile that exists. The PATH line goes into that one: creating ~/.bash_profile next to an existing
    ~/.profile would make every new terminal skip ~/.profile."""
    from pulsar import cli
    bin_dir = tmp_path / "b"
    mine = "# mine\nexport FOO=1\n"
    for i, (existing, expect) in enumerate((((), ".bash_profile"), ((".profile",), ".profile"),
                                            ((".bash_login", ".profile"), ".bash_login"),
                                            ((".bash_profile", ".profile"), ".bash_profile"))):
        root = tmp_path / ("case%d" % i)
        root.mkdir()
        env = _env(root, SHELL="/bin/bash")
        home = _scratch_home(root)
        for name in existing:
            (home / name).write_text(mine)
        r = _bash('BIN_DIR="$1"; OS=macos; add_to_path; echo "hint=$PATH_HINT"', env, str(bin_dir), home=home)
        assert r.returncode == 0 and "hint=1" in r.stdout, (existing, r.stdout + r.stderr)
        assert 'export PATH="%s:$PATH"' % bin_dir in (home / expect).read_text(), existing
        for name in existing:
            if name != expect:
                assert (home / name).read_text() == mine, (existing, name)
        if ".bash_profile" not in existing and expect != ".bash_profile":
            assert not (home / ".bash_profile").exists(), existing
        if expect != ".bash_login":
            assert (home / expect, str(bin_dir)) in cli.rc_blocks(str(home)), existing


@needs_posix
def test_path_line_is_matched_exactly_never_by_substring(tmp_path):
    """A startup file that only mentions the folder (a comment, another tool's line) still gets the PATH line,
    so "New terminals have `pulsarcode` on PATH" is never claimed for a line that was never written."""
    bin_dir = tmp_path / "b"
    env = _env(tmp_path, SHELL="/bin/bash")
    home = _scratch_home(tmp_path)
    rc = home / ".bashrc"
    rc.write_text("# the tools in %s are mine\n" % bin_dir)
    r = _bash('BIN_DIR="$1"; OS=linux; add_to_path; add_to_path; echo "hint=$PATH_HINT"', env, str(bin_dir),
              home=home)
    assert r.returncode == 0 and "hint=1" in r.stdout, r.stdout + r.stderr
    text = rc.read_text()
    assert text.startswith("# the tools in") and text.count('export PATH="%s:$PATH"' % bin_dir) == 1, text


@needs_posix
@pytest.mark.parametrize("shell,os_name,rc", [("/bin/bash", "linux", ".bashrc"), ("/bin/bash", "macos", ".bash_profile"),
                                              ("/bin/zsh", "linux", ".zshrc"), ("/usr/bin/fish", "linux",
                                                                                ".config/fish/config.fish"),
                                              ("/bin/sh", "linux", ".profile")])
def test_uninstall_finds_the_block_the_installer_wrote(tmp_path, shell, os_name, rc):
    from pulsar import cli
    env = _env(tmp_path, SHELL=shell)
    home = _scratch_home(tmp_path)
    bin_dir = tmp_path / "bin.d"
    r = _bash('BIN_DIR="$1"; OS="$2"; add_to_path', env, str(bin_dir), os_name, home=home)
    assert r.returncode == 0, r.stderr
    assert (home / rc, str(bin_dir)) in cli.rc_blocks(str(home))


@needs_posix
@pytest.mark.parametrize("shell,var,sub,rc", [("/bin/zsh", "ZDOTDIR", "zdot", ".zshrc"),
                                              ("/usr/bin/fish", "XDG_CONFIG_HOME", "xdg", "fish/config.fish")])
def test_uninstall_finds_blocks_under_zdotdir_and_xdg(tmp_path, monkeypatch, shell, var, sub, rc):
    """install.sh and `pulsarcode uninstall` agree on $ZDOTDIR / $XDG_CONFIG_HOME, both absolute
    (used) and relative (ignored: the default file under HOME instead)."""
    from pulsar import cli
    for name in ("ZDOTDIR", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)
    home = _scratch_home(tmp_path)
    bin_dir = tmp_path / "bin dir"                                 # fish: the quoted form too
    # absolute: the installer writes there and uninstall (same environment) finds it
    env = _env(tmp_path, SHELL=shell, **{var: str(tmp_path / sub)})
    r = _bash('BIN_DIR="$1"; OS=linux; add_to_path', env, str(bin_dir), home=home)
    assert r.returncode == 0, r.stderr
    monkeypatch.setenv(var, str(tmp_path / sub))
    assert (tmp_path / sub / rc, str(bin_dir)) in cli.rc_blocks(str(home))
    # relative: ignored by both, so the block lands in the default file uninstall always scans
    monkeypatch.delenv(var)
    env = _env(tmp_path, SHELL=shell, **{var: "rel-" + sub})
    r = _bash('BIN_DIR="$1"; OS=linux; add_to_path', env, str(bin_dir), cwd=str(tmp_path), home=home)
    assert r.returncode == 0, r.stderr
    default = home / (".zshrc" if var == "ZDOTDIR" else ".config/fish/config.fish")
    assert not (tmp_path / ("rel-" + sub)).exists()
    assert (default, str(bin_dir)) in cli.rc_blocks(str(home))


# ---------------------------------------------------------------------------
# install.sh: uv and Python selection
# ---------------------------------------------------------------------------

@needs_posix
def test_old_uv_on_path_is_ignored(tmp_path):
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    ph = tmp_path / "ph"
    (ph / "bin").mkdir(parents=True)
    _stub(stubs / "uv", 'echo "uv 0.2.9 (old)"\n')
    _stub(ph / "bin" / "uv", 'echo "uv 0.12.22 (private)"\n')
    env = _env(tmp_path, PATH=str(stubs) + ":" + SYS_PATH, PULSAR_HOME=str(ph))
    r = _bash('for v in "uv 0.4.30" "uv 0.5.0" "uv 0.10.2" "uv 1.0.0" "uv 0.08.1" "ruff 9.9"; do\n'
              '  printf "#!/bin/sh\\necho \\"%s\\"\\n" "$v" > "$TMPDIR/u"; chmod +x "$TMPDIR/u"\n'
              '  if uv_usable "$TMPDIR/u"; then echo "$v=yes"; else echo "$v=no"; fi\n'
              'done\n'
              'find_uv && echo "picked=$UV"', env)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.split("\n")
    for want in ("uv 0.4.30=no", "uv 0.5.0=yes", "uv 0.10.2=yes", "uv 1.0.0=yes", "uv 0.08.1=yes", "ruff 9.9=no"):
        assert want in lines, (want, lines)
    assert "picked=%s" % (ph / "bin" / "uv") in lines


@needs_posix
def test_macos_prefers_managed_python_and_never_probes_the_xcode_stub(tmp_path):
    calls = tmp_path / "uv.calls"
    uv = _stub(tmp_path / "uv", 'echo "$*" >> "%s"\nexit 1\n' % calls)
    env = _env(tmp_path, UV=str(uv))
    r = _bash('OS=macos; macos_clt() { return 1; }\n'
              'safe_to_probe /usr/bin/python3 && echo stub-probed || echo stub-skipped\n'
              'safe_to_probe /opt/homebrew/bin/python3 && echo brew-ok\n'
              'macos_clt() { return 0; }; safe_to_probe /usr/bin/python3 && echo clt-ok\n'
              'UV="$1"; venv_with_uv "" || echo venv-failed', env, str(uv))
    assert r.stdout.split() == ["stub-skipped", "brew-ok", "clt-ok", "venv-failed"], r.stdout + r.stderr
    first = calls.read_text().splitlines()[0]
    assert "--python-preference only-managed" in first and "system" not in first


@needs_posix
def test_uv_trusts_the_os_store_and_pip_index(tmp_path):
    r = _bash('echo "$UV_NATIVE_TLS|${UV_DEFAULT_INDEX:-}|${UV_INDEX:-}|$CA_FILE"',
              _env(tmp_path, PIP_INDEX_URL="https://mirror.example/simple", SSL_CERT_FILE=str(INSTALL_SH)))
    assert r.stdout.strip() == "1|https://mirror.example/simple||%s" % INSTALL_SH
    r = _bash('echo "$UV_NATIVE_TLS|$CA_FILE"', _env(tmp_path, UV_NATIVE_TLS="0", SSL_CERT_FILE="/nonexistent"))
    assert r.stdout.strip() == "0|"                                # the user's choice wins; a missing file is ignored


@needs_posix
def test_wsl_ignores_windows_claude_and_npm(tmp_path):
    win = tmp_path / "mnt" / "c" / "Users" / "x" / "AppData" / "Roaming" / "npm"
    lin = tmp_path / "linux-bin"
    for d in (win, lin):
        d.mkdir(parents=True)
    for name in ("claude", "npm"):
        _stub(win / name, "echo windows\n")
    env = _env(tmp_path)
    script = ('WSL_DRIVES="$1/mnt"; PATH="$2:::$3:$PATH"\n'
              'OS=wsl; native_cmd claude || echo "wsl: no claude"; native_cmd npm || echo "wsl: no npm"\n'
              'claude_present || echo "wsl: claude absent"\n'
              '_stub() { printf "#!/bin/sh\\n" > "$1"; chmod +x "$1"; }; _stub "$3/claude"\n'
              'echo "wsl: $(native_cmd claude)"\n'
              'OS=linux; echo "linux: $(native_cmd npm)"\n')
    r = _bash(script, env, str(tmp_path), str(win), str(lin), cwd=str(win),     # cwd holds a claude too
              home=_scratch_home(tmp_path))                          # no ~/.local/bin/claude there
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == ["wsl: no claude", "wsl: no npm", "wsl: claude absent",
                                     "wsl: %s" % (lin / "claude"), "linux: %s" % (win / "npm")], r.stdout


# ---------------------------------------------------------------------------
# install.sh: release integrity
# ---------------------------------------------------------------------------

FAKE_CURL = r'''out=""; fmt=""; url=""
while [ $# -gt 0 ]; do
    case "$1" in
        -o) out=$2; shift 2 ;;
        -w) fmt=$2; shift 2 ;;
        --proto|--proto-redir|--retry|--connect-timeout|--cacert) shift 2 ;;
        -*) shift ;;
        *) url=$1; shift ;;
    esac
done
printf '%s\n' "$url" >> "$FAKE_NET/requests.log"
if [ -n "${FAKE_NET_DOWN:-}" ]; then
    [ -n "$fmt" ] && printf 000
    echo "curl: (7) Failed to connect" >&2
    exit 7
fi
f="$FAKE_NET/files/${url#https://}"
if [ -f "$f" ]; then cp "$f" "$out"; [ -n "$fmt" ] && printf 200; exit 0; fi
[ -n "$fmt" ] && printf 404
echo "curl: (22) The requested URL returned error: 404" >&2
exit 22
'''


def _tarball(top: str, marker: str) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for rel, data in (("pulsar/__init__.py", b"__version__ = '9.9.9'\n"), ("marker.txt", marker.encode()),
                          ("pulsarcode", b"#!/bin/sh\n"), ("requirements.txt", b"")):
            info = tarfile.TarInfo(top + "/" + rel)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return raw.getvalue()


class FakeNet:
    def __init__(self, tmp_path):
        self.root = tmp_path / "net"
        self.files = self.root / "files" / "github.com" / pulsar.REPO
        self.files.mkdir(parents=True)
        self.stubs = tmp_path / "netstubs"
        self.stubs.mkdir()
        _stub(self.stubs / "curl", FAKE_CURL)
        _stub(self.stubs / "wget", "exit 4\n")
        self.tmp_path = tmp_path

    def put(self, rel: str, data: bytes) -> None:
        p = self.files / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def requests(self):
        log = self.root / "requests.log"
        return log.read_text().split() if log.exists() else []

    def get_source(self, ref: str, **extra):
        env = _env(self.tmp_path, PATH=str(self.stubs) + ":" + SYS_PATH, FAKE_NET=str(self.root), PULSAR_REF=ref,
                   PULSAR_NO_LOCAL_SRC="1", **extra)
        return _bash('get_source; echo "SRC-MARKER=$(cat "$SRC/marker.txt")"', env)


@needs_posix
def test_release_tag_is_verified_against_sha256sums(tmp_path):
    net = FakeNet(tmp_path)
    good = _tarball("pulsarcode-9.9.9", "release-asset")
    net.put("releases/download/v9.9.9/pulsarcode-app-9.9.9.tar.gz", good)
    net.put("releases/download/v9.9.9/SHA256SUMS",
            ("%s  pulsarcode-app-9.9.9.zip\n%s *pulsarcode-app-9.9.9.tar.gz\n"
             % ("0" * 64, hashlib.sha256(good).hexdigest().upper())).encode())
    net.put("archive/refs/tags/v9.9.9.tar.gz", _tarball("pulsarcode-9.9.9", "tag-archive"))
    r = net.get_source("v9.9.9")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SRC-MARKER=release-asset" in r.stdout and "matches the release SHA256SUMS" in r.stdout
    assert not any("/archive/" in u for u in net.requests())        # never the unverified archive


@needs_posix
def test_release_tag_fails_closed(tmp_path):
    net = FakeNet(tmp_path)
    good = _tarball("pulsarcode-9.9.9", "release-asset")
    net.put("archive/refs/tags/v9.9.9.tar.gz", _tarball("pulsarcode-9.9.9", "tag-archive"))
    net.put("releases/download/v9.9.9/pulsarcode-app-9.9.9.tar.gz", good)
    # 1. the digest does not match
    net.put("releases/download/v9.9.9/SHA256SUMS", ("%s  pulsarcode-app-9.9.9.tar.gz\n" % ("a" * 64)).encode())
    r = net.get_source("v9.9.9")
    assert r.returncode == 1 and "checksum mismatch" in r.stderr and "SRC-MARKER" not in r.stdout, r.stderr
    # 2. SHA256SUMS does not list the archive
    net.put("releases/download/v9.9.9/SHA256SUMS", ("%s  something-else.tar.gz\n" % ("a" * 64)).encode())
    r = net.get_source("v9.9.9")
    assert r.returncode == 1 and "does not list" in r.stderr, r.stderr
    # 3. the asset itself is missing
    (net.files / "releases/download/v9.9.9/pulsarcode-app-9.9.9.tar.gz").unlink()
    net.put("releases/download/v9.9.9/SHA256SUMS",
            ("%s  pulsarcode-app-9.9.9.tar.gz\n" % hashlib.sha256(good).hexdigest()).encode())
    r = net.get_source("v9.9.9")
    assert r.returncode == 1 and "could not download pulsarcode-app-9.9.9.tar.gz" in r.stderr, r.stderr
    # 4. SHA256SUMS cannot be fetched (network, proxy): no silent fallback to an unverified archive
    r = net.get_source("v9.9.9", FAKE_NET_DOWN="1")
    assert r.returncode == 1 and "SHA256SUMS" in r.stderr and "SRC-MARKER" not in r.stdout, r.stderr
    assert not any("/archive/" in u for u in net.requests())


@needs_posix
def test_tag_without_sha256sums_and_branches_use_the_archive(tmp_path):
    net = FakeNet(tmp_path)
    net.put("archive/refs/tags/v2.0.0.tar.gz", _tarball("pulsarcode-2.0.0", "old-tag"))
    net.put("archive/refs/heads/main.tar.gz", _tarball("pulsarcode-main", "main-branch"))
    r = net.get_source("v2.0.0")
    assert r.returncode == 0 and "SRC-MARKER=old-tag" in r.stdout, r.stderr
    assert "publishes no SHA256SUMS" in r.stdout
    r = net.get_source("main")
    assert r.returncode == 0 and "SRC-MARKER=main-branch" in r.stdout, r.stderr
    assert sum("SHA256SUMS" in u for u in net.requests()) == 1        # only for the tag


@needs_posix
def test_v3_tag_without_sha256sums_fails_closed(tmp_path):
    """From v3 on every release publishes SHA256SUMS: a 404 there means a release that is still being
    published (or is gone), never an older one, so nothing is installed and the unverified tag archive
    is never fetched."""
    net = FakeNet(tmp_path)
    for ref in ("v3.0.0", "v3.0.1", "v3.1.0rc1", "v10.0.0"):
        net.put("archive/refs/tags/%s.tar.gz" % ref, _tarball("pulsarcode-" + ref[1:], "tag-archive"))
        r = net.get_source(ref)
        assert r.returncode == 1 and "SRC-MARKER" not in r.stdout, (ref, r.stdout + r.stderr)
        assert "SHA256SUMS" in r.stderr and "not checksum-verified" not in r.stdout, (ref, r.stdout + r.stderr)
    assert not any("/archive/" in u for u in net.requests())


# ---------------------------------------------------------------------------
# install.sh: the Pulsar home (what is adopted, and what may ever be replaced there)
# ---------------------------------------------------------------------------

def _tree(root: Path) -> dict:
    """Every entry under root with its bytes, its link target or "dir": compared before and after a step."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(str(root)):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            rel = p.relative_to(root).as_posix()
            out[rel] = ("link", os.readlink(str(p))) if p.is_symlink() else "dir" if p.is_dir() else p.read_bytes()
    return out


def _plant(root: Path, files: dict) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


TRUSTED_MARKER = "Pulsar Code home.\ncreated 2026-01-01T00:00:00Z\n"
LEGACY_MARKER = "Pulsar Code home. `pulsarcode uninstall` only removes folders that carry this file.\n"
# A project a mistaken PULSAR_HOME can name: its config.json mentions "profiles" and its venv has a broken
# interpreter. This is the shape that used to be adopted, then emptied by the runtime and engine steps.
PROJECT = {"config.json": '{"name": "my-service", "profiles": {"dev": {}}}\n', "app/main.py": "print('svc')\n",
           "app/models/user.py": "class User: pass\n", "src/lib.py": "x = 1\n", "bin/deploy.sh": "echo deploy\n",
           "logs/app.log": "started\n", "cache/blob": "data\n", "settings.json": '{"theme": "dark"}\n',
           "README.md": "# my-service\n", "venv/pyvenv.cfg": "home = /nonexistent\n"}
# What Pulsar Code v1 kept in its home.
V1_HOME = {"canonical/pulsarcode": "#!/bin/bash\n", "canonical/proxy/__init__.py": "", "venv/pyvenv.cfg": "home = /usr/bin\n",
           "nim.key": "nvapi-x\n", "active_model": "nim-kimi\n", "model_history": "nim-kimi\n", "onboarding_complete": "",
           "nim_sonar_catalog.json": "{}\n", "claude_config/commands/api.md": "x\n", "run/proxy.log": ""}
STEP_STUBS = ('stop_running_core() { :; }\nmake_venv() { echo EV:venv; mkdir -p "$VENV/bin"; }\n'
              'find_uv() { return 1; }\npip_plain() { echo EV:deps; }\nrun() { shift; "$@"; }\n')


@needs_posix
def test_foreign_folder_is_never_adopted_or_emptied(tmp_path):
    """A folder holding anyone else's files is refused before anything is written to it, whatever Pulsar
    Code names it also holds: a "profiles" key, a nim.key, a claude_config/ folder or an app/pulsar package."""
    cases = {
        "profiles": PROJECT,
        "nim.key": {"nim.key": "k\n", "src/main.go": "package main\n"},
        "claude_config": {"claude_config/settings.json": "{}\n", "README.md": "# notes\n"},
        "engine": {"app/pulsar/__init__.py": "", "app/main.py": "print('svc')\n", "Makefile": "all:\n"},
    }
    for name, files in cases.items():
        home = _plant(tmp_path / name, files)
        if name == "profiles":
            (home / "venv" / "bin").mkdir()
            os.symlink("/nonexistent/python3", str(home / "venv" / "bin" / "python"))
        home.chmod(0o755)
        before = _tree(home)
        env = _env(tmp_path, PULSAR_HOME=str(home), PULSAR_BIN_DIR=str(tmp_path / "bin"), PULSAR_NO_PATH="1")
        r = _bash("prepare_home; echo prepared", env)
        assert r.returncode == 1 and "prepared" not in r.stdout, (name, r.stdout + r.stderr)
        assert "did not create" in r.stderr, (name, r.stderr)
        if name == "profiles":                                     # what is in the way is named
            assert "README.md" in r.stderr and "config.json" in r.stderr, r.stderr
        assert _tree(home) == before, name                         # no marker, nothing moved or removed
        assert stat.S_IMODE(home.stat().st_mode) == 0o755, name


def _json_path(tmp_path) -> str:
    """PATH with this interpreter as python3 first: the installer reads config.json with a python3 of the
    system before its own runtime exists, and the tests must not depend on (or probe) the system's."""
    d = tmp_path / "pybin"
    d.mkdir(exist_ok=True)
    if not (d / "python3").exists():
        os.symlink(sys.executable, str(d / "python3"))
    return str(d) + ":" + SYS_PATH


PULSAR_CFG = '{"version": 2, "active": "", "profiles": {}}\n'
# Existing folders and what both installers make of them: "ours" (a marker with provenance), "created" (empty:
# the only kind uninstall may remove whole), "adopted" (Pulsar Code's own evidence and nothing else), "unproven"
# (only names Pulsar Code uses, none of its evidence) or "foreign". The engine (pulsar/config.py, ensure_home)
# decides the same, except that it also adopts the two cases marked "engine adopts": the installers write far
# more into the folder, so they want nothing foreign next to an older install, and the engine itself next to
# a marker without provenance.
HOME_CASES = {
    "empty": ({}, "created"),
    "a-crash-left-a-temp-file": ({".config.json.ab12cd": "{"}, "created"),
    "v1": (V1_HOME, "adopted"),
    "v1-key-retired": ({"nim.key.migrated": "nvapi-x\n", "onboarding_complete": "", "config.json": PULSAR_CFG,
                        "nim_sonar_catalog.json": "{}\n", "claude_config/x": ""}, "adopted"),
    "dev-build": ({".pulsarcode-home": LEGACY_MARKER, "app/pulsar/__init__.py": "", "venv/pyvenv.cfg": "x\n"},
                  "adopted"),
    "state": ({"config.json": PULSAR_CFG, "keys.json": "{}\n", "cache/x": ""}, "adopted"),
    "common-names": ({"settings.json": "{}\n", "logs/x.log": "", "cache/y": ""}, "unproven"),
    "cache-only": ({"cache/models.json": "{}\n"}, "unproven"),
    "retired-key-only": ({"nim.key.migrated": "nvapi-x\n"}, "unproven"),
    "untrusted-marker-no-engine": ({".pulsarcode-home": LEGACY_MARKER, "config.json": PULSAR_CFG},
                                   "unproven"),                                        # engine adopts
    "v1-and-a-file-of-its-owner": (dict(V1_HOME, **{"notes.txt": "mine\n"}), "foreign"),  # engine adopts
    "project": (PROJECT, "foreign"),
    "profiles-not-an-object": ({"config.json": '{"active": "", "profiles": []}\n', "cache/x": ""}, "foreign"),
    "profiles-nested": ({"config.json": '{"tool": {"active": "", "profiles": {}}}\n', "cache/x": ""}, "foreign"),
    "keys-not-json": ({"keys.json": "precious\n", "cache/x": ""}, "foreign"),
    "provenance-on-the-first-line": ({".pulsarcode-home": "created 2026-01-01T00:00:00Z\n", "notes.txt": "x\n"},
                                     "foreign"),
    "trusted": ({".pulsarcode-home": TRUSTED_MARKER, "notes.txt": "x\n"}, "ours"),
}
REFUSAL = {"foreign": "did not create", "unproven": "cannot tell are its own"}


def _prepare_home_case(tmp_path, name, files, kind):
    """install.sh's prepare_home on one planted folder, checked against the kind HOME_CASES gives it."""
    home = _plant(tmp_path / name, files)
    before = _tree(home)
    env = _env(tmp_path, PATH=_json_path(tmp_path), PULSAR_HOME=str(home), PULSAR_BIN_DIR=str(tmp_path / "bin"))
    r = _bash("prepare_home; echo prepared", env)
    if kind in REFUSAL:
        assert r.returncode == 1 and "prepared" not in r.stdout, (name, r.stdout + r.stderr)
        assert REFUSAL[kind] in r.stderr and "Nothing was written" in r.stderr, (name, r.stderr)
        assert _tree(home) == before, name                         # no marker, nothing moved or removed
    else:
        assert r.returncode == 0 and "prepared" in r.stdout, (name, r.stdout + r.stderr)
        marker = (home / ".pulsarcode-home").read_text()
        if kind == "ours":
            assert marker == files[".pulsarcode-home"], name       # already ours: left as it was
        else:
            assert marker.splitlines()[1].startswith(kind + " "), name
    return r


@needs_posix
def test_older_homes_are_adopted_only_with_nothing_foreign(tmp_path):
    """An earlier install (v1's layout, a development build's engine, the state an older build wrote) is
    adopted when it carries Pulsar Code's own evidence (its engine, a Pulsar config.json, the v1 key or the
    isolated Claude Code profile) and holds nothing else. config.json counts only when it parses as the
    engine reads it: a JSON object with a "profiles" object and an "active" key, never a text match."""
    for name, (files, kind) in HOME_CASES.items():
        _prepare_home_case(tmp_path, name, files, kind)
    # a trailing comma is not JSON: Python's parser refuses it as the engine's does
    _prepare_home_case(tmp_path, "not-json", {"config.json": '{"active": "", "profiles": {}, }\n', "keys.json": "{}\n"},
                       "foreign")


@needs_posix
def test_only_an_empty_folder_is_created(tmp_path):
    """Only a new or empty folder is marked "created" (uninstall may then remove it whole). Names alone prove
    nothing: settings.json, logs/ and cache/ are just as likely another tool's, so a folder holding only such
    names, with none of Pulsar Code's evidence, is refused and named, exactly as the engine refuses it."""
    for name, (files, kind) in HOME_CASES.items():
        if kind not in ("created", "unproven"):
            continue
        r = _prepare_home_case(tmp_path, name, files, kind)
        for entry in files if kind == "unproven" else ():
            assert entry.split("/")[0] in r.stderr, (name, entry, r.stderr)        # what is there is named
    # A folder whose only evidence is config.json, on a Mac whose python3 is the developer tools' stub
    # (never run: it opens a dialog): nothing can read the file, so nothing is adopted.
    home = _plant(tmp_path / "config-unread", {"config.json": '{"active": "", "profiles": {}}\n'})
    env = _env(tmp_path, PULSAR_HOME=str(home), PULSAR_BIN_DIR=str(tmp_path / "bin"))
    r = _bash("OS=macos; macos_clt() { return 1; }; prepare_home; echo prepared", env)
    assert r.returncode == 1 and "config.json" in r.stderr and "Nothing was written" in r.stderr, r.stderr
    assert sorted(os.listdir(str(home))) == ["config.json"]


@needs_posix
def test_runtime_and_engine_folders_are_replaced_only_when_ours(tmp_path):
    """Even in a home the installer trusts, venv/ is deleted only when it is a virtualenv (pyvenv.cfg) or empty,
    and app/ is only ever replaced by a newer engine: anyone else's folder under those names stops the install."""
    home = _plant(tmp_path / "ph", {".pulsarcode-home": TRUSTED_MARKER, "venv/tool.cfg": "mine\n",
                                    "app/main.py": "print('mine')\n"})
    src = _plant(tmp_path / "src", {"pulsar/__init__.py": "", "pulsarcode": "#!/bin/sh\n", "requirements.txt": ""})
    before = _tree(home)
    env = _env(tmp_path, PULSAR_HOME=str(home), PULSAR_BIN_DIR=str(tmp_path / "bin"), PULSAR_NO_PATH="1")
    r = _bash(STEP_STUBS + "setup_python", env)
    assert r.returncode == 1 and "EV:venv" not in r.stdout and "not a Python environment" in r.stderr, \
        r.stdout + r.stderr
    r = _bash(STEP_STUBS + 'SRC="$1"; place_app', env, str(src))
    assert r.returncode == 1 and "not a Pulsar Code engine" in r.stderr, r.stdout + r.stderr
    assert _tree(home) == before
    # a virtualenv whose interpreter broke, and an empty folder, are rebuilt
    for name, files in (("broken", {"venv/pyvenv.cfg": "home = /nonexistent\n"}), ("empty", {})):
        h = _plant(tmp_path / name, dict(files, **{".pulsarcode-home": TRUSTED_MARKER}))
        (h / "venv").mkdir(exist_ok=True)
        r = _bash(STEP_STUBS + "setup_python",
                  _env(tmp_path, PULSAR_HOME=str(h), PULSAR_BIN_DIR=str(tmp_path / "bin"), PULSAR_NO_PATH="1"))
        assert r.returncode == 0 and "EV:venv" in r.stdout, (name, r.stdout + r.stderr)
        assert not (h / "venv" / "pyvenv.cfg").exists(), name


@needs_posix
def test_v1_upgrade_says_what_it_replaces(tmp_path):
    """A v1 home's engine (canonical/) goes only once the new engine has verified itself (commit_install), with
    one line that says so; until then it stays, so an upgrade that stops halfway leaves v1 working."""
    home = _plant(tmp_path / "ph", V1_HOME)
    (home / "venv" / "bin").mkdir()
    _stub(home / "venv" / "bin" / "python", 'echo "pulsarcode 9.9.9"\n')       # compileall and `pulsar version`
    src = _plant(tmp_path / "src", {"pulsar/__init__.py": "", "pulsarcode": "#!/bin/sh\n", "requirements.txt": ""})
    env = _env(tmp_path, PULSAR_HOME=str(home), PULSAR_BIN_DIR=str(tmp_path / "bin"), PULSAR_NO_PATH="1")
    r = _bash('SRC="$1"; place_app; [[ -d "$PULSAR_HOME/canonical" ]] && echo v1-engine-kept; commit_install', env,
              str(src))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "v1-engine-kept" in r.stdout and r.stdout.index("v1-engine-kept") < r.stdout.index("Pulsar Code v1 home")
    assert not (home / "canonical").exists() and (home / "app" / "pulsar" / "__init__.py").is_file()
    assert r.stdout.count("Pulsar Code v1 home") == 1 and "canonical" in r.stdout, r.stdout
    assert (home / "nim.key").read_text() == "nvapi-x\n"                       # the engine imports it at launch


@needs_posix
def test_a_stopped_install_puts_the_previous_one_back(tmp_path):
    """Until the new engine has verified itself, what an install run replaces is only set aside: when the run
    stops first (a failed self-test, Claude Code that cannot be installed, Ctrl+C), the previous engine and the
    command it pointed at come back, and a v1 home keeps its engine and its link, so it works as before."""
    src = _plant(tmp_path / "src", {"pulsar/__init__.py": "NEW = 1\n", "pulsarcode": "#!/bin/sh\n",
                                    "requirements.txt": ""})
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    link = bin_dir / "pulsarcode"
    as_main = 'ROLL_BACK=1; SRC="$1"; place_app; link_bin; '         # what main() runs up to the self-test
    # an earlier v3 install: its engine comes back, and goes only once the new one is verified
    v3 = _plant(tmp_path / "v3", {".pulsarcode-home": TRUSTED_MARKER, "app/pulsar/__init__.py": "OLD = 1\n",
                                  "app/pulsarcode": "#!/bin/sh\n"})
    (v3 / "venv" / "bin").mkdir(parents=True)
    _stub(v3 / "venv" / "bin" / "python", 'echo "pulsarcode 9.9.9"\n')
    os.symlink(str(v3 / "app" / "pulsarcode"), str(link))
    env = _env(tmp_path, PULSAR_HOME=str(v3), PULSAR_BIN_DIR=str(bin_dir))
    r = _bash(as_main + 'die "self-test failed"', env, str(src))
    assert r.returncode == 1 and "self-test failed" in r.stderr and "restored" in r.stdout, r.stdout + r.stderr
    assert (v3 / "app" / "pulsar" / "__init__.py").read_text() == "OLD = 1\n" and not (v3 / "app.old").exists()
    assert os.readlink(str(link)) == str(v3 / "app" / "pulsarcode")
    r = _bash(as_main + "commit_install", env, str(src))
    assert r.returncode == 0 and "restored" not in r.stdout, r.stdout + r.stderr
    assert (v3 / "app" / "pulsar" / "__init__.py").read_text() == "NEW = 1\n" and not (v3 / "app.old").exists()
    if os.geteuid() != 0:            # a leftover that cannot be removed is named, and the verified install stands
        locked = v3 / "app" / "pulsar" / "locked"
        locked.mkdir()
        (locked / "f").write_text("x\n")
        locked.chmod(0o555)
        try:
            r = _bash(as_main + "commit_install", env, str(src))
        finally:
            (v3 / "app.old" / "pulsar" / "locked").chmod(0o755)
        assert r.returncode == 0 and "could not remove" in r.stdout and "app.old" in r.stdout, r.stdout + r.stderr
        assert (v3 / "app" / "pulsar" / "__init__.py").read_text() == "NEW = 1\n"
    link.unlink()
    # a v1 home: its engine and its command stay as they were
    v1 = _plant(tmp_path / "v1", V1_HOME)
    (v1 / "venv" / "bin").mkdir()
    _stub(v1 / "venv" / "bin" / "python", 'echo "pulsarcode 9.9.9"\n')
    os.symlink(str(v1 / "canonical" / "pulsarcode"), str(link))
    env = _env(tmp_path, PULSAR_HOME=str(v1), PULSAR_BIN_DIR=str(bin_dir))
    r = _bash(as_main + 'die "Claude Code could not be installed automatically"', env, str(src))
    assert r.returncode == 1 and "restored" in r.stdout, r.stdout + r.stderr
    assert (v1 / "canonical" / "pulsarcode").read_text() == "#!/bin/bash\n"
    assert os.readlink(str(link)) == str(v1 / "canonical" / "pulsarcode")
    # functions used one at a time, as these tests do, are no install run: nothing is put back
    r = _bash('SRC="$1"; place_app; link_bin', env, str(src))
    assert r.returncode == 0 and "restored" not in r.stdout, r.stdout + r.stderr
    assert os.readlink(str(link)) == str(v1 / "app" / "pulsarcode")


UV_MANAGED_STUB = r'''echo "$*" >> "$UV_LOG"
case "$1" in
    --version) echo "uv 0.12.22" ;;
    python) [ "$2" = dir ] && echo "$UV_PY" ;;
    venv) [ -n "${UV_VENV_FAIL:-}" ] && exit 1
          for v; do :; done
          mkdir -p "$v/bin" && ln -s "$UV_PYTHON" "$v/bin/python" &&
              printf 'home = %s/cpython-3.12.12-macos-aarch64-none/bin\n' "$UV_PY" > "$v/pyvenv.cfg" ;;
esac
exit 0
'''


@needs_posix
def test_macos_runtime_moves_to_a_uv_managed_python(tmp_path):
    """On macOS a runtime built on a Python uv does not manage (Homebrew's, which `brew upgrade` replaces under it,
    Apple's, v1's) is rebuilt on a uv-managed one. The previous runtime waits in venv.old until the install is
    verified, comes back if the run stops first, and stays in use when no managed Python can be had."""
    uvpy = tmp_path / "uv-pythons"
    src = _plant(tmp_path / "src", {"requirements.txt": ""})

    def home(name, base):
        h = _plant(tmp_path / name, {".pulsarcode-home": TRUSTED_MARKER, "venv/pyvenv.cfg": "home = %s\n" % base,
                                     "venv/the-old-one": ""})
        (h / "venv" / "bin").mkdir()
        os.symlink(sys.executable, str(h / "venv" / "bin" / "python"))
        (h / "bin").mkdir()
        _stub(h / "bin" / "uv", UV_MANAGED_STUB)
        return h

    def go(h, script, os_name="macos", **extra):
        log = tmp_path / (h.name + ".log")
        env = _env(tmp_path, PULSAR_HOME=str(h), UV_LOG=str(log), UV_PY=str(uvpy), UV_PYTHON=sys.executable, **extra)
        r = _bash('stop_running_core() { :; }; run() { shift; "$@"; }; ROLL_BACK=1; SRC="$1"; OS=%s; setup_python; %s'
                  % (os_name, script), env, str(src))
        return r, (log.read_text().splitlines() if log.exists() else [])

    brew = "/opt/homebrew/opt/python@3.12/bin"
    h = home("brew", brew)                       # rebuilt; the previous runtime goes once the install is verified
    r, log = go(h, 'ls "$VENV.old"; commit_install')
    assert r.returncode == 0 and "the-old-one" in r.stdout, r.stdout + r.stderr
    assert any(c.startswith("venv ") and "--python-preference only-managed" in c for c in log), log
    assert (h / "venv" / "pyvenv.cfg").read_text().startswith("home = %s/" % uvpy) and not (h / "venv.old").exists()
    h = home("stopped", brew)                    # the run stops before it is verified: the previous one comes back
    r, log = go(h, 'die "self-test failed"')
    assert r.returncode == 1 and (h / "venv" / "the-old-one").exists() and not (h / "venv.old").exists(), r.stdout
    h = home("interrupted", brew)                # a step still writing into the new runtime: it is left alone
    r, log = go(h, 'VENV_BUSY=1; roll_back 1; ls "$VENV.old"')
    assert "the-old-one" in r.stdout and "run the installer again" in r.stdout, r.stdout + r.stderr
    assert (h / "venv.old" / "the-old-one").exists()
    if os.geteuid() != 0:                        # a venv.old that cannot be removed: never moved into, never lost
        h = home("stale", brew)
        locked = h / "venv.old" / "locked"
        locked.mkdir(parents=True)
        (locked / "f").write_text("x\n")
        locked.chmod(0o555)
        try:
            r, log = go(h, "commit_install")
        finally:
            locked.chmod(0o755)
        assert r.returncode == 0 and "reusing" in r.stdout, r.stdout + r.stderr
        assert (h / "venv" / "the-old-one").exists() and not (h / "venv.old" / "venv").exists()
        assert not [c for c in log if c.startswith("venv ")], log
    h = home("offline", brew)                    # no managed Python to be had: the runtime that works stays
    r, log = go(h, "commit_install", UV_VENV_FAIL="1")
    assert r.returncode == 0 and "reusing" in r.stdout and (h / "venv" / "the-old-one").exists(), r.stdout + r.stderr
    assert not (h / "venv.old").exists()
    for name, base, os_name in (("managed", "%s/cpython-3.12.1-macos-aarch64-none/bin" % uvpy, "macos"),
                                ("linux", "/usr/bin", "linux")):
        h = home(name, base)                     # already on uv's own Python, or not macOS: reused as it is
        r, log = go(h, "commit_install", os_name)
        assert r.returncode == 0 and "reusing" in r.stdout and (h / "venv" / "the-old-one").exists(), (name, r.stdout)
        assert not [c for c in log if c.startswith("venv ")], (name, log)


@needs_posix
def test_bin_link_of_another_install_is_never_replaced(tmp_path):
    """BIN_DIR/pulsarcode is replaced only when it leads into this home (an earlier install of it, v1's link
    into canonical/ included, even once canonical/ is gone). Another install's command, a pipx or uv tool
    launcher, or any file of the user's stays unless PULSAR_FORCE=1, and the check runs before anything
    in the home changes."""
    other = _plant(tmp_path / "other", {"app/pulsarcode": "#!/bin/sh\n"})
    ph = _plant(tmp_path / "ph", {"canonical/pulsarcode": "#!/bin/bash\n", "app/pulsarcode": "#!/bin/sh\n"})
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    link = bin_dir / "pulsarcode"
    env = _env(tmp_path, PULSAR_HOME=str(ph), PULSAR_BIN_DIR=str(bin_dir), PULSAR_NO_PATH="1")

    def state():
        return os.readlink(str(link)) if link.is_symlink() else link.read_text()

    for make in (lambda: os.symlink(str(other / "app" / "pulsarcode"), str(link)),
                 lambda: link.write_text("#!/bin/sh\nexec /opt/pipx/venvs/pulsarcode/bin/pulsarcode\n")):
        make()
        before = state()
        r = _bash("link_bin; echo linked", env)
        assert r.returncode == 1 and "linked" not in r.stdout and "PULSAR_FORCE=1" in r.stderr, r.stdout + r.stderr
        assert state() == before
        r = _bash("link_bin; echo linked", dict(env, PULSAR_FORCE="1"))
        assert r.returncode == 0 and "linked" in r.stdout, r.stdout + r.stderr
        assert os.readlink(str(link)) == str(ph / "app" / "pulsarcode")
        link.unlink()
    # this home's own commands: the v1 link into canonical/, a relative link, a link whose target is gone
    gone = _plant(tmp_path / "gone", {})
    for home, target in ((ph, str(ph / "canonical" / "pulsarcode")),
                         (ph, os.path.relpath(str(ph / "app" / "pulsarcode"), str(bin_dir))),
                         (gone, str(gone / "canonical" / "pulsarcode"))):
        os.symlink(target, str(link))
        r = _bash("link_bin; echo linked", dict(env, PULSAR_HOME=str(home)))
        assert r.returncode == 0 and "linked" in r.stdout, (target, r.stdout + r.stderr)
        assert os.readlink(str(link)) == str(home / "app" / "pulsarcode"), target
        link.unlink()
    main = INSTALL_SH.read_text(encoding="utf-8").split("\nmain() {", 1)[1]
    assert main.index("check_bin") < main.index("setup_python"), "refuse before the home changes"


@needs_posix
def test_reinstall_links_where_the_home_was_installed(tmp_path):
    """`pulsarcode update` runs the installer for this home without PULSAR_BIN_DIR: the command goes back to the
    folder this home's install recorded in its marker, not to ~/.local/bin next to another install's command."""
    custom_bin = tmp_path / "custom bin"
    custom_bin.mkdir()
    home = _plant(tmp_path / "ph", {".pulsarcode-home": TRUSTED_MARKER + "bin %s\n" % custom_bin})
    env = _env(tmp_path, PULSAR_HOME=str(home), PULSAR_BIN_DIR=str(tmp_path / "bin"), PULSAR_NO_PATH="1")
    r = _bash('unset PULSAR_BIN_DIR PULSARCODE_BIN_DIR; prepare_home; printf "BIN=%s\\n" "$BIN_DIR"', env)
    assert r.returncode == 0 and "BIN=%s\n" % custom_bin in r.stdout, r.stdout + r.stderr
    r = _bash('prepare_home; printf "BIN=%s\\n" "$BIN_DIR"', env)              # an explicit PULSAR_BIN_DIR wins
    assert r.returncode == 0 and "BIN=%s\n" % (tmp_path / "bin") in r.stdout, r.stdout + r.stderr


@needs_posix
def test_shim_runs_against_the_home_it_is_installed_in(tmp_path):
    """<home>/app/pulsarcode (the installer's layout) hands that home to the engine and runs its runtime when
    PULSAR_HOME is unset, so an install into a custom PULSAR_HOME never runs against ~/.pulsarcode; a set
    PULSAR_HOME still wins, and a shim outside an installed home exports nothing."""
    out = tmp_path / "seen.txt"

    def home(path: Path, marker: bool) -> Path:
        (path / "app" / "pulsar").mkdir(parents=True)
        shutil.copy(str(SHIM), str(path / "app" / "pulsarcode"))
        (path / "app" / "pulsarcode").chmod(0o755)
        (path / "venv" / "bin").mkdir(parents=True)
        _stub(path / "venv" / "bin" / "python", 'printf "%%s|%%s\\n" "$0" "${PULSAR_HOME:-unset}" > "%s"\n' % out)
        if marker:
            (path / ".pulsarcode-home").write_text(TRUSTED_MARKER)
        return path

    custom, other, plain = home(tmp_path / "custom", True), home(tmp_path / "other", True), home(tmp_path / "plain", False)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    os.symlink(str(custom / "app" / "pulsarcode"), str(bin_dir / "pulsarcode"))
    real = os.path.realpath

    def run(launcher, **extra):
        env = _env(tmp_path, **extra)
        if "PULSAR_HOME" not in extra:
            env.pop("PULSAR_HOME", None)                            # unset: the shim decides
        r = subprocess.run(["/bin/sh", str(launcher), "version"], env=env, cwd=str(tmp_path), capture_output=True,
                           text=True, timeout=60, stdin=subprocess.DEVNULL)
        assert r.returncode == 0, r.stderr
        python, seen = out.read_text().strip().split("|")
        return [real(python), real(seen) if seen != "unset" else seen]

    stub = str(other / "venv" / "bin" / "python")
    assert run(bin_dir / "pulsarcode", PULSAR_PYTHON=stub)[1] == real(str(custom))       # the engine's home
    assert run(bin_dir / "pulsarcode") == [real(str(custom / "venv" / "bin" / "python")), real(str(custom))]
    assert run(bin_dir / "pulsarcode", PULSAR_HOME=str(other)) == [real(stub), real(str(other))]
    assert run(plain / "app" / "pulsarcode", PULSAR_PYTHON=stub)[1] == "unset"


# ---------------------------------------------------------------------------
# install.sh end to end, offline
# ---------------------------------------------------------------------------

def _provision_venv(venv: Path) -> None:
    """A venv whose dependencies resolve to this interpreter's (so no index is ever needed)."""
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True, timeout=120)
    paths = list(site.getsitepackages()) + [sysconfig.get_paths()["purelib"], sysconfig.get_paths()["platlib"]]
    user = site.getusersitepackages() if site.ENABLE_USER_SITE else ""
    paths += [user] if user else []
    paths = [p for p in dict.fromkeys(paths) if os.path.isdir(p)]
    sp = glob.glob(str(venv / "lib" / "python*" / "site-packages"))[0]
    Path(sp, "_pulsar_test_deps.pth").write_text("\n".join(paths) + "\n")


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@needs_posix
def test_install_sh_end_to_end_offline(tmp_path):
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    net_log = tmp_path / "network.log"
    for tool in ("curl", "wget"):
        _stub(stubs / tool, 'echo "%s $*" >> "%s"\necho "network disabled in this test" >&2\nexit 7\n' % (tool, net_log))
    uv_log = tmp_path / "uv.log"
    _stub(stubs / "uv", 'if [ "$1" = "--version" ]; then echo "uv 0.1.44"; exit 0; fi\necho "$*" >> "%s"\nexit 1\n'
          % uv_log)
    ph, bin_dir = tmp_path / "pulsar home", tmp_path / "bin"
    # An earlier install that got as far as the runtime: the marker comes first, then the venv. Its private
    # uv answers --version and fails everything else, so the dependencies go through pip, and a uv the
    # caller has in ~/.local/bin or ~/.cargo/bin (looked up after this one) is never run.
    _plant(ph, {".pulsarcode-home": TRUSTED_MARKER})
    _provision_venv(ph / "venv")
    private_log = tmp_path / "private-uv.log"
    (ph / "bin").mkdir()
    _stub(ph / "bin" / "uv", 'if [ "$1" = "--version" ]; then echo "uv 0.12.22"; exit 0; fi\n'
          'echo "$* @ $(pwd -P)" >> "%s"\nexit 1\n' % private_log)
    env = _env(tmp_path, PATH=str(stubs) + ":" + SYS_PATH, PULSAR_HOME=str(ph), PULSAR_BIN_DIR=str(bin_dir),
               PULSAR_SKIP_CLAUDE="1", PULSAR_NO_PATH="1", PULSAR_NO_LAUNCH="1", PIP_NO_INDEX="1",
               PIP_NO_CACHE_DIR="1", PIP_DISABLE_PIP_VERSION_CHECK="1", UV_OFFLINE="1", SHELL="/bin/bash")
    evil = _hostile_dir(tmp_path)

    def install():
        r = subprocess.run(["bash", str(INSTALL_SH), "--local"], env=env, cwd=str(evil), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=600)
        assert r.returncode == 0, r.stdout + r.stderr
        return r

    r = install()
    assert "Pulsar Code is installed" in r.stdout and "skipped (PULSAR_SKIP_CLAUDE=1)" in r.stdout
    launcher = bin_dir / "pulsarcode"
    assert launcher.is_symlink() and os.path.realpath(str(launcher)) == os.path.realpath(str(ph / "app" / "pulsarcode"))
    run_env = _env(tmp_path, PULSAR_HOME=str(ph))
    v = subprocess.run([str(launcher), "version"], env=run_env, cwd=str(evil), stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=60)
    assert v.returncode == 0 and v.stdout.strip() == "pulsarcode " + pulsar.__version__, v.stderr
    assert not (tmp_path / "pwned.txt").exists()
    assert not net_log.exists() and not uv_log.exists()           # no network, and the old uv was never used
    calls = private_log.read_text().splitlines()
    assert any(c.startswith("pip install") for c in calls), calls      # the private uv first, then pip
    assert not [c for c in calls if c.startswith("venv")], calls        # the runtime was reused
    # uv reads uv.toml and pyproject.toml from the folder it runs in: never the hostile one it was started from
    assert all(c.endswith(" @ " + os.path.realpath(str(ph))) for c in calls), calls
    assert not os.listdir(env["TMPDIR"])                          # no log or download left behind
    assert stat.S_IMODE((ph).stat().st_mode) == 0o700

    # Re-run over a Core descriptor (its process is gone): the installer asks the old engine to stop
    # it before swapping the app, which clears the descriptor; the venv is reused.
    run_dir = ph / "run"
    run_dir.mkdir()
    (run_dir / "core.json").write_text(json.dumps({"port": _free_port(), "pid": _dead_pid(), "version": "0.0.1",
                                                   "admin_token": "pla_fake-not-a-token", "started": 1.0}))
    r = install()
    assert "reusing" in r.stdout and "still running" not in r.stdout
    assert not (run_dir / "core.json").exists()
    v = subprocess.run([str(launcher), "version"], env=run_env, cwd=str(evil), stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=60)
    assert v.returncode == 0, v.stderr


# ---------------------------------------------------------------------------
# install.ps1 (through pwsh when it is available)
# ---------------------------------------------------------------------------

def _ps_env(tmp_path, **extra):
    env = _env(tmp_path, **extra)
    if os.name == "nt":                     # PowerShell cannot start without these (none is a secret)
        env.update({k: os.environ[k] for k in WINDOWS_ENV if k in os.environ})
        env["TMPDIR"] = env["TEMP"] = env["TMP"] = str(tmp_path / "tmp")
    return env


def _pwsh_argv(*args):
    return [PWSH, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File"] + [str(a) for a in args]


def _pwsh(script: str, env, cwd, *args, timeout=120):
    path = Path(env["TMPDIR"]) / ("t%d.ps1" % int(time.time() * 1e6))
    path.write_text(script, encoding="ascii")
    return subprocess.run(_pwsh_argv(path, *args), env=env, cwd=str(cwd), capture_output=True, text=True,
                          timeout=timeout, stdin=subprocess.DEVNULL)


PARSE_PS = r'''
param($installer)
$t = $null; $e = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($installer, [ref]$t, [ref]$e)
"errors=" + $e.Count
$e | ForEach-Object { "error: " + $_.ToString() }
# Windows PowerShell 5.1 has no ternary, pipeline chains (&& ||), null-coalescing or null-conditional access
$n = @($ast.FindAll({ param($x) $x.GetType().Name -in @('TernaryExpressionAst', 'PipelineChainAst') }, $true)).Count
$k = @($t | Where-Object { $_.Kind -in @('QuestionQuestion', 'QuestionQuestionEquals', 'QuestionDot', 'QuestionLBracket', 'AndAnd', 'OrOr') }).Count
"ps7only=" + ($n + $k)
# what Windows PowerShell 5.1 sees when it reads the file in the ANSI code page
$text = [Text.Encoding]::GetEncoding(1252).GetString([IO.File]::ReadAllBytes($installer))
$t2 = $null; $e2 = $null
[void][System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$t2, [ref]$e2)
"cp1252errors=" + $e2.Count
'''


@needs_pwsh
def test_install_ps1_parses_for_windows_powershell_5(tmp_path):
    r = _pwsh(PARSE_PS, _ps_env(tmp_path), tmp_path, str(INSTALL_PS1))
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["errors=0", "ps7only=0", "cp1252errors=0"], r.stdout


LEAK_PS = r'''
param($installer)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'
$vars = @(Get-Variable | ForEach-Object Name)
$funcs = @(Get-ChildItem function: | ForEach-Object Name)
$envs = @(Get-ChildItem env: | ForEach-Object Name)
$env:PULSAR_REF = 'bad ref;x'
Get-Content -Raw $installer | Invoke-Expression
"alive LASTEXITCODE=$LASTEXITCODE EAP=$ErrorActionPreference"
$skip = @('vars', 'funcs', 'envs', 'skip', 'LASTEXITCODE')
"vars=" + (@(Get-Variable | ForEach-Object Name | Where-Object { $vars -notcontains $_ -and $skip -notcontains $_ }) -join ',')
"funcs=" + (@(Get-ChildItem function: | ForEach-Object Name | Where-Object { $funcs -notcontains $_ }) -join ',')
"env=" + (@(Get-ChildItem env: | ForEach-Object Name | Where-Object { $envs -notcontains $_ -and $_ -ne 'PULSAR_REF' }) -join ',')
try { $notDefinedAnywhere | Out-Null; "strict=off" } catch { "strict=on" }
'''


@needs_pwsh
def test_install_ps1_via_iex_never_closes_the_window_or_leaks(tmp_path):
    env = _ps_env(tmp_path)
    r = _pwsh(LEAK_PS, env, tmp_path, str(INSTALL_PS1))
    assert r.returncode == 0, r.stdout + r.stderr
    lines = r.stdout.splitlines()
    assert "alive LASTEXITCODE=1 EAP=Continue" in lines, lines
    found = {line.split("=", 1)[0]: line for line in lines if line.split("=", 1)[0] in ("vars", "funcs", "env")}
    for kind in ("vars", "funcs", "env"):
        assert found.get(kind) == kind + "=", "install.ps1 leaked into the caller's session: %r" % found.get(kind)
    assert "strict=on" in lines, lines
    assert any("PULSAR_REF must be a tag" in line for line in lines)
    # run as a file, a failure is exit code 1
    r = subprocess.run(_pwsh_argv(INSTALL_PS1), env=dict(env, PULSAR_REF="bad ref"), cwd=str(tmp_path),
                       capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    assert r.returncode == 1 and "PULSAR_REF must be a tag" in r.stdout, r.stdout + r.stderr
    if os.name != "nt":                     # PowerShell on macOS / Linux: refused before anything is written
        ph = tmp_path / "never-created"
        r = subprocess.run(_pwsh_argv(INSTALL_PS1), env=dict(env, PULSAR_HOME=str(ph)), cwd=str(tmp_path),
                           capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        assert r.returncode == 1 and "install.sh" in r.stdout and not ph.exists(), r.stdout + r.stderr


SHIMS_PS = r'''
param($installer, $bin, $ph, $out)
$t = $null; $e = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($installer, [ref]$t, [ref]$e)
foreach ($f in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
    if ($f.Name -in @('Get-RelativePath', 'Test-Ascii', 'New-CmdShim', 'New-Ps1Shim', 'New-ShShim', 'Write-TextFile', 'Find-Sum')) {
        . ([scriptblock]::Create($f.Extent.Text))
    }
}
$boot = $env:TEST_BOOT
"rel=" + (Get-RelativePath $bin $ph)
"rel-win=" + (Get-RelativePath 'C:\Users\you\.local\bin' 'C:\Users\you\.pulsarcode')
"rel-drive=" + [string](Get-RelativePath '\\server\share\bin' 'D:\x')
$c = New-CmdShim $bin $ph $boot
Write-TextFile (Join-Path $bin 'pulsarcode.cmd') $c.Text $(if ($c.Utf8) { 'utf8' } else { 'ascii' })
"cmd-utf8=" + $c.Utf8
"ascii-home-utf8=" + (New-CmdShim '/srv/u/.local/bin' '/srv/u/.pulsarcode' $boot).Utf8
$u = New-CmdShim '\\server\share\bin' ('D:\Pulsar' + [char]0x00E9) $boot
Write-TextFile (Join-Path $out 'fallback.cmd') $u.Text 'utf8'
"fallback-utf8=" + $u.Utf8
Write-TextFile (Join-Path $bin 'pulsarcode') (New-ShShim $bin $ph $boot) 'utf8'
Write-TextFile (Join-Path $bin 'pulsarcode.ps1') (New-Ps1Shim $bin $ph $boot) 'utf8bom'
$sums = Join-Path $out 'SHA256SUMS'
Write-TextFile $sums (('A' * 64) + " *pulsarcode-app-1.0.0.zip`r`n" + ('b' * 64) + "  other.zip`n") 'ascii'
"sum=" + (Find-Sum $sums 'pulsarcode-app-1.0.0.zip')
"nosum=" + [string](Find-Sum $sums 'missing.zip')
'''


@needs_pwsh
@needs_posix
def test_install_ps1_launchers_work_from_a_non_ascii_profile(tmp_path):
    profile = tmp_path / ("Jos\u00e9 M'\u00fcller")
    bin_dir, ph = profile / ".local" / "bin", profile / ".pulsarcode"
    bin_dir.mkdir(parents=True)
    (ph / "venv" / "Scripts").mkdir(parents=True)
    (ph / "app").mkdir()
    os.symlink(sys.executable, str(ph / "venv" / "Scripts" / "python.exe"))
    os.symlink(str(REPO / "pulsar"), str(ph / "app" / "pulsar"))
    out = tmp_path / "out"
    out.mkdir()
    env = _env(tmp_path, TEST_BOOT=pulsar.BOOT, LANG="C.UTF-8")
    r = _pwsh(SHIMS_PS, env, tmp_path, str(INSTALL_PS1), str(bin_dir), str(ph), str(out))
    assert r.returncode == 0, r.stdout + r.stderr
    lines = r.stdout.splitlines()
    assert "rel=..\\..\\.pulsarcode" in lines and "rel-drive=" in lines, lines
    assert "cmd-utf8=True" in lines and "ascii-home-utf8=False" in lines and "fallback-utf8=True" in lines, lines
    assert "sum=" + "a" * 64 in lines and "nosum=" in lines
    cmd = (bin_dir / "pulsarcode.cmd").read_bytes()
    assert not cmd.startswith(b"\xef\xbb\xbf") and b"\r\n" in cmd and b"\n" not in cmd.replace(b"\r\n", b"")
    for line in cmd.split(b"\r\n"):                        # every line cmd.exe executes is ASCII
        assert line.startswith(b"rem ") or all(b < 0x80 for b in line), line
    assert ('rem Pulsar Code home: "%s"' % ph).encode("utf-8") in cmd
    assert b'for %%I in ("%~dp0..\\..\\.pulsarcode") do set "PULSAR_APP_HOME=%%~fI"' in cmd
    assert b"-E -X utf8 -B -c" in cmd and b"-m pulsar" not in cmd and cmd.rstrip().endswith(b"pulsar %*")
    from pulsar import cli
    for name in ("pulsarcode", "pulsarcode.cmd", "pulsarcode.ps1"):     # `pulsarcode uninstall` knows them
        assert cli._our_shim(str(bin_dir / name), ph), name
        assert not cli._our_shim(str(bin_dir / name), tmp_path / "another-home"), name
    fallback = (out / "fallback.cmd").read_bytes()
    assert not fallback.startswith(b"\xef\xbb\xbf") and b"chcp 65001" in fallback
    assert (fallback.index(b"chcp 65001") < fallback.index(b'set "PULSAR_APP_HOME=') < fallback.index(b"chcp %PULSAR_CP%")
            < fallback.index(b"python.exe"))
    sh_shim = (bin_dir / "pulsarcode").read_bytes()
    assert sh_shim.startswith(b"#!/bin/sh\n") and b"\r" not in sh_shim
    assert (bin_dir / "pulsarcode.ps1").read_bytes().startswith(b"\xef\xbb\xbf")
    evil = _hostile_dir(tmp_path)
    run_env = _env(tmp_path, PULSAR_HOME=str(ph))
    for argv in (["/bin/sh", str(bin_dir / "pulsarcode"), "version"],
                 _pwsh_argv(bin_dir / "pulsarcode.ps1", "version")):
        v = subprocess.run(argv, env=run_env, cwd=str(evil), capture_output=True, text=True, timeout=120,
                           stdin=subprocess.DEVNULL)
        assert v.returncode == 0 and v.stdout.strip() == "pulsarcode " + pulsar.__version__, (argv, v.stdout, v.stderr)
    assert not (tmp_path / "pwned.txt").exists()
    # Without PULSAR_HOME each launcher hands the engine its own home: an install into a custom
    # PULSAR_HOME never runs against the default one.
    assert cmd.index(b'if not defined PULSAR_HOME set "PULSAR_HOME=%PULSAR_APP_HOME%"') < cmd.index(b"python.exe")
    custom = profile / "custom home"
    (custom / "venv" / "Scripts").mkdir(parents=True)
    os.symlink(sys.executable, str(custom / "venv" / "Scripts" / "python.exe"))
    (custom / "app" / "pulsar").mkdir(parents=True)
    (custom / "app" / "pulsar" / "__init__.py").write_text("")
    (custom / "app" / "pulsar" / "__main__.py").write_text(
        "import os\nprint('SEEN=' + os.environ.get('PULSAR_HOME', 'unset'))\n", encoding="utf-8")
    bin2, out2 = tmp_path / "bin2", tmp_path / "out2"
    bin2.mkdir()
    out2.mkdir()
    r = _pwsh(SHIMS_PS, env, tmp_path, str(INSTALL_PS1), str(bin2), str(custom), str(out2))
    assert r.returncode == 0, r.stdout + r.stderr
    for argv in (["/bin/sh", str(bin2 / "pulsarcode"), "version"], _pwsh_argv(bin2 / "pulsarcode.ps1", "version")):
        v = subprocess.run(argv, env=_env(tmp_path, LANG="C.UTF-8", PULSAR_HOME=None), cwd=str(evil),
                           capture_output=True, text=True, encoding="utf-8", timeout=120, stdin=subprocess.DEVNULL)
        assert v.returncode == 0 and v.stdout.strip() == "SEEN=" + str(custom), (argv, v.stdout, v.stderr)


HOME_PS = r'''
param($installer, $root)
$t = $null; $e = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($installer, [ref]$t, [ref]$e)
foreach ($f in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
    if ($f.Name -in @('Read-JsonLikeEngine', 'Test-JsonObject', 'Test-PulsarConfig', 'Get-MarkerKind', 'Get-HomeKind')) {
        . ([scriptblock]::Create($f.Extent.Text))
    }
}
$Marker = '.pulsarcode-home'
foreach ($d in @(Get-ChildItem -LiteralPath $root -Directory | Sort-Object Name)) {
    $k = Get-HomeKind $d.FullName
    $d.Name + '=' + $k.Kind + ':' + ((@($k.Foreign) | Sort-Object) -join ',')
}
'''


@needs_pwsh
def test_install_ps1_home_rule_matches_install_sh(tmp_path):
    """install.ps1 classifies an existing PULSAR_HOME by the same rule as install.sh (and the engine): every
    folder of HOME_CASES gets the kind install.sh gives it."""
    root = tmp_path / "homes"
    root.mkdir()
    for name, (files, _) in HOME_CASES.items():
        _plant(root / name, files)
    r = _pwsh(HOME_PS, _ps_env(tmp_path), tmp_path, str(INSTALL_PS1), str(root))
    assert r.returncode == 0, r.stdout + r.stderr
    got = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    for name, (_, kind) in HOME_CASES.items():
        assert got.get(name, "").split(":", 1)[0] == kind, (name, got.get(name))
    assert {"README.md", "app", "bin", "config.json", "src"} <= set(got["project"].split(":", 1)[1].split(","))
    assert set(got["common-names"].split(":", 1)[1].split(",")) == {"cache", "logs", "settings.json"}
    # what install.ps1 says when it refuses: the same two reasons as install.sh
    ps = INSTALL_PS1.read_text(encoding="ascii")
    assert "'cannot tell are its own'" in ps and "'did not create'" in ps


def test_installers_agree_on_boot_and_knobs():
    sh = INSTALL_SH.read_text(encoding="utf-8")
    ps = INSTALL_PS1.read_text(encoding="ascii")
    assert 'BOOT="%s"' % pulsar.BOOT in sh and '$Boot = "%s"' % pulsar.BOOT in ps
    for knob in ("PULSAR_REF", "PULSAR_HOME", "PULSAR_BIN_DIR", "PULSAR_NO_LAUNCH", "PULSAR_SKIP_CLAUDE",
                 "PULSAR_NO_PATH", "PULSAR_SRC", "PULSAR_NO_LOCAL_SRC", "UV_NATIVE_TLS", "SHA256SUMS"):
        assert knob in sh and knob in ps, knob
    # the user PATH is read raw from the registry and written back as REG_EXPAND_SZ, never via .NET's
    # expanding Get/SetEnvironmentVariable('Path', ..., 'User')
    assert "DoNotExpandEnvironmentNames" in ps and "ExpandString" in ps
    assert not re.search(r"""SetEnvironmentVariable\(\s*['"]Path['"]""", ps)
    # nested installers run in their own process, never `| iex` inside ours
    code = "\n".join(line for line in ps.splitlines() if not line.lstrip().startswith("#"))
    assert "Invoke-Expression" not in code and "| iex" not in code
    assert re.search(r"(?m)^\s*\} @args\s*$", ps) and re.search(r"(?m)^& \{\s*$", ps)


# ---------------------------------------------------------------------------
# second-round regressions: Core stop order, execution policy, no foreground sleep
# ---------------------------------------------------------------------------

@needs_posix
def test_install_sh_stops_the_core_before_touching_the_runtime(tmp_path):
    env = _env(tmp_path, PULSAR_HOME=str(tmp_path / "ph"))
    r = _bash('stop_running_core() { echo EV:stop; }\n'
              'make_venv() { echo EV:venv; mkdir -p "$VENV/bin"; }\n'
              'find_uv() { return 1; }\n'
              'pip_plain() { echo EV:deps; }\n'
              'run() { shift; "$@"; }\n'
              'setup_python', env)
    assert r.returncode == 0, r.stdout + r.stderr
    events = [x for x in r.stdout.split() if x.startswith("EV:")]
    assert events[:3] == ["EV:stop", "EV:venv", "EV:deps"], events


def test_a_core_still_serving_sessions_is_never_cut_off():
    """Both installers ask a running Core to stop (`pulsar stop`), which lets it serve its open Claude Code
    sessions to the end, never `--force`. When it is still there afterwards, the note says how to end it now."""
    for name, text in (("install.sh", INSTALL_SH.read_text(encoding="utf-8")),
                       ("install.ps1", INSTALL_PS1.read_text(encoding="ascii"))):
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        assert "pulsar stop" in code and "pulsar stop --force" not in code, name
        assert "still running" in code and "pulsarcode stop --force" in code, name


def test_install_ps1_stops_the_core_before_the_runtime_is_touched():
    ps = INSTALL_PS1.read_text(encoding="ascii")
    body = ps[ps.index("# ------------------------------------------------------------------ install"):]
    stop = body.index("Stop-RunningCore $AppFwd")
    assert stop < body.index("Remove-Tree $Venv")
    assert stop < body.index("& $uv venv")
    assert stop < body.index("& $uv pip install")
    assert body.index("$AppFwd = ") < stop


def test_install_ps1_deletes_only_what_it_owns():
    """The venv, app.new and app.old go through Remove-Tree (a junction is removed as a link, never followed),
    a venv without pyvenv.cfg and an app\\ without the engine stop the install, an existing pulsarcode.ps1 is
    removed only when this installer wrote it, and a download a redirect took off HTTPS is refused."""
    ps = INSTALL_PS1.read_text(encoding="ascii")
    body = ps[ps.index("# ------------------------------------------------------------------ install"):]
    code = "\n".join(line.split("#", 1)[0] for line in body.splitlines())
    assert not re.search(r"Remove-Item[^\n]*-Recurse[^\n]*\$(Venv|stage|old)\b", code), "use Remove-Tree"
    for target in ("$Venv", "$stage", "$old"):
        assert "Remove-Tree " + target in code, target
    func = ps[ps.index("function Remove-Tree"):ps.index("\nfunction ", ps.index("function Remove-Tree") + 1)]
    assert "ReparsePoint" in func and "[IO.Directory]::Delete($item.FullName, $false)" in func
    venv = body.index("Remove-Tree $Venv")
    assert body.rindex("Join-Path $Venv 'pyvenv.cfg'", 0, venv) < venv
    engine = body.index("# --- engine")
    assert body.index("Join-Path $App 'pulsar\\__init__.py'", engine) < body.index("Remove-Tree $stage")
    removal = body.index("Remove-Item -Force -LiteralPath $ps1")
    assert body.rindex("-like '# Pulsar Code launcher for PowerShell*'", 0, removal) > body.rindex("elseif", 0, removal)
    get = ps[ps.index("function Get-Download"):ps.index("\nfunction ", ps.index("function Get-Download") + 1)]
    assert "-PassThru" in get and "$final.Scheme -ne 'https'" in get and "return 0" in get
    # PULSAR_HOME and PULSAR_BIN_DIR resolve against PowerShell's location before anything is created
    resolve = body.index("GetUnresolvedProviderPathFromPSPath($PulsarHome)")
    assert resolve < body.index("CreateDirectory($PulsarHome)") and "GetUnresolvedProviderPathFromPSPath($BinDir)" in body


def test_install_ps1_v3_tags_fail_closed_like_install_sh():
    ps = INSTALL_PS1.read_text(encoding="ascii")
    m = re.search(r"\} elseif \(\$code -eq 404 -and \$Ref -cmatch '\^v\(\\d\{1,9\}\)\(\\\.\|\$\)' -and "
                  r"\[int\]\$Matches\[1\] -lt 3\) \{\s*#[^\n]*\n\s*Warn [^\n]*not checksum-verified[^\n]*\n"
                  r"\s*\} elseif \(\$code -eq 404\) \{\s*Die \"release \$Ref publishes no SHA256SUMS\.", ps)
    assert m, "a 404 on SHA256SUMS may only fall back for tags older than v3"
    sh = INSTALL_SH.read_text(encoding="utf-8")
    assert "older_than_v3 \"$REF\"" in sh and "local re='^v([0-9]{1,9})(\\.|$)'" in sh


def test_install_ps1_execution_policy_ignores_the_process_scope():
    """`pulsarcode update` and the Git Bash hand-off run the installer with -ExecutionPolicy Bypass;
    the Process scope must not decide whether pulsarcode.ps1 (which new windows would refuse) is written."""
    ps = INSTALL_PS1.read_text(encoding="ascii")
    start = ps.index("function Get-PersistentExecutionPolicy")
    func = ps[start:ps.index("\nfunction ", start + 1)]
    assert "Get-ExecutionPolicy -List" in func and "'Process'" not in func
    order = [func.index("'%s'" % s) for s in ("MachinePolicy", "UserPolicy", "CurrentUser", "LocalMachine")]
    assert order == sorted(order)
    assert "return 'Restricted'" in func
    # every other use of Get-ExecutionPolicy is gone: the shim decision goes through the function
    rest = ps[:start] + ps[start + len(func):]
    assert not re.search(r"Get-ExecutionPolicy(?!\s*-List)", re.sub(r"#[^\n]*", "", rest))
    assert "$policy = Get-PersistentExecutionPolicy" in ps


def test_install_sh_never_sleeps_in_the_foreground():
    """A foreground `sleep` killed by Ctrl+C was seen livelocking bash in its SIGINT re-raise path;
    every pause goes through nap (a background sleep plus the wait builtin)."""
    sh = INSTALL_SH.read_text(encoding="utf-8")
    code = [re.sub(r"#.*", "", line) for line in sh.splitlines()]
    sleeps = [line.strip() for line in code if re.search(r"\bsleep\b", line)]
    assert sleeps == ['sleep "$1" &'], sleeps
    for sig in ("INT", "TERM", "HUP"):
        assert re.search(r"trap 'trap - %s;" % sig, sh), sig


def test_install_sh_ends_only_the_processes_it_started():
    """No pkill or killall: a process is only ever signalled through the handle the installer kept
    when it started it (the running step, the nap sleep, a with_timeout child), never by name or
    by parent."""
    for script in (INSTALL_SH, SHIM):
        code = [re.sub(r"#.*", "", line) for line in script.read_text(encoding="utf-8").splitlines()]
        assert not [line.strip() for line in code if re.search(r"\b(pkill|killall)\b", line)], script
    code = [re.sub(r"#.*", "", line) for line in INSTALL_SH.read_text(encoding="utf-8").splitlines()]
    targets = set()
    for line in code:
        for m in re.finditer(r"\bkill\b(?! -0)\s+([^ ;|&]+)", line):
            targets.add(m.group(1))
    assert targets == {'"$RUN_PID"', '"$NAP_PID"', '"$p"'}, targets


@needs_posix
def test_ctrl_c_during_nap_runs_the_trap_and_reaps_the_sleep(tmp_path):
    env = _env(tmp_path)
    p = subprocess.Popen(["bash", "-c", 'source "$INSTALL_SH"; echo ready; nap 30; echo after-nap',
                          "test"], env=dict(env, INSTALL_SH=str(INSTALL_SH)), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "ready"
        time.sleep(0.3)
        kids = []
        if shutil.which("pgrep"):
            kids = subprocess.run(["pgrep", "-P", str(p.pid)], capture_output=True, text=True).stdout.split()
        p.send_signal(2)
        out, _ = p.communicate(timeout=10)
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
    assert p.returncode == 130 and "after-nap" not in out
    for k in kids:
        assert _process_gone(int(k)), "the nap sleep outlived the installer"
