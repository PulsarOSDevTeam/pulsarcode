# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""End-to-end regression tests for install scenarios: the shim's Python fallback, install.ps1's home rule next
to install.sh's, and the npm package.

HOME stays the caller's own (on macOS a program started with a made-up HOME can raise a Keychain dialog);
what a test writes goes to its scratch folder: PULSAR_HOME, and npm's cache, logs and user config.
"""

import glob
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
NPM = REPO / "packaging" / "npm"
NODE = shutil.which("node")
REAL_HOME = os.environ.get("HOME") or os.path.expanduser("~")


def _fake_python(path: Path, log: Path):
    # Accepts the probe (the launch flags -E -B, then -c CODE and nothing else) only when it imports
    # what Pulsar really needs (starlette, not fastapi) after dropping the current directory.
    path.write_text(
        "#!/bin/sh\n"
        "if [ \"$#\" -eq 4 ] && [ \"$1 $2 $3\" = '-E -B -c' ]; then\n"
        "  case \"$4\" in *fastapi*) exit 1 ;; *\"sys.path[:]\"*starlette*uvicorn*httpx*) exit 0 ;; esac\n"
        "  exit 1\n"
        "fi\n"
        "[ \"$#\" -ge 6 ] || exit 1\n"                  # any other probe: refused; the launch has 6+ args
        "printf '%%s\\n' \"$@\" > '%s'\n"
        "exit 0\n" % log)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.mark.skipif(os.name == "nt", reason="POSIX shim")
def test_shim_falls_back_to_a_python3_with_the_real_requirements(tmp_path):
    app = tmp_path / "app"
    (app / "pulsar").mkdir(parents=True)
    shutil.copy(REPO / "pulsarcode", app / "pulsarcode")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "argv.log"
    _fake_python(bindir / "python3", log)
    env = {"HOME": REAL_HOME, "PULSAR_HOME": str(tmp_path / "ph"), "PATH": "%s:/usr/bin:/bin" % bindir}
    r = subprocess.run(["sh", str(app / "pulsarcode"), "version"], env=env, cwd=str(tmp_path),
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert "virtualenv is missing" not in r.stderr
    argv = log.read_text().splitlines()
    assert argv[0] == "-E" and argv[-2:] == ["pulsar", "version"]
    assert "fastapi" not in (REPO / "pulsarcode").read_text()


def test_install_ps1_requires_marker_provenance_like_install_sh():
    ps = (REPO / "install.ps1").read_text(encoding="ascii")
    start = ps.index("function Get-HomeKind")
    func = ps[start:ps.index("\nfunction ", start + 1)]
    # The scan is skipped only for a marker with a 'created' / 'adopted' line after the first, read as the
    # engine reads it: bytes, lines ending at LF only, the word compared ordinally (never Get-Content, which
    # also ends a line at a lone CR and decodes UTF-16, nor -match / -ceq, which ignore case or NUL).
    assert "if ((Get-MarkerKind $markPath) -eq 'trusted') { return @{ Kind = 'ours'" in func
    reader = ps[ps.index("function Get-MarkerKind"):ps.index("\nfunction ", ps.index("function Get-MarkerKind") + 1)]
    assert "[IO.File]::ReadAllBytes($markPath)" in reader and ".Split([char]10)" in reader
    assert "for ($i = 1;" in reader and reader.count("[StringComparison]::Ordinal") == 2
    assert "Get-Content" not in reader.split("try {", 1)[1] and "-ceq" not in reader.split("try {", 1)[1]
    assert "if (-not (Test-Path -LiteralPath (Join-Path $realHome $Marker))) {" not in ps
    assert "$found = Get-HomeKind $realHome" in ps
    # install.sh's rule, for reference: lines after the first, the same words.
    sh = (REPO / "install.sh").read_text()
    trusted = sh[sh.index("marker_trusted() {"):sh.index("\n}\n", sh.index("marker_trusted() {"))]
    assert "NR > 1" in trusted and "^(created|adopted) " in trusted and '"$1/$MARKER"' in trusted
    assert 'if marker_trusted "$real"; then kind=""' in sh
    # Only an empty folder is ever marked created (uninstall may remove such a folder whole).
    assert "if ($names.Count -eq 0) { return @{ Kind = 'created'" in func
    assert 'elif empty_home "$real"; then kind="created"' in sh
    # With an untrusted marker, only an app\pulsar install may be adopted (install.sh older_pulsar_home):
    # nim.key, claude_config and a Pulsar config count only in a folder without any marker.
    m = re.search(r"\$evidence = Test-Path -LiteralPath \(Join-Path \$dir 'app\\pulsar\\__init__\.py'\)\s+"
                  r"if \(-not \$evidence -and -not \(Test-Path -LiteralPath \$markPath\)\) \{", func)
    assert m, "adopt rule must not trust nim.key/claude_config/config.json when a marker exists"
    for evidence in ("'nim.key'", "'claude_config'", "Test-PulsarConfig (Join-Path $dir 'config.json')"):
        assert func.index(evidence, m.end()) > m.end(), evidence
    older = sh[sh.index("older_pulsar_home() {"):sh.index("\n}\n", sh.index("older_pulsar_home() {"))]
    assert 'ev="$(home_evidence "$1")" || return 1' in older
    assert '[[ "$ev" == app || ! -e "$1/$MARKER" ]] || return 1' in older
    # and an earlier install is adopted only next to nothing foreign, in both installers
    assert "if ($evidence -and $foreign.Count -eq 0)" in func
    assert older.rstrip().endswith('only_pulsar_state "$1"') and '[[ -z "$(foreign_entries "$1")" ]]' in sh
    # config.json is parsed as JSON (as the engine reads it), never matched as text; v1's files count as Pulsar's
    config = sh[sh.index("pulsar_config() {"):sh.index("\n}\n", sh.index("pulsar_config() {"))]
    assert "json.load(f)" in config and 'isinstance(d.get("profiles"), dict) and "active" in d' in config
    assert "grep" not in config
    for name in ("nim.key.migrated", "onboarding_complete", "nim_sonar_catalog.json"):
        assert "'%s'" % name in func and name in sh[sh.index("foreign_entries() {"):], name


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_npm_windows_launcher_uses_the_shim_flags(tmp_path):
    wh = tmp_path / "winhome"
    (wh / "app" / "pulsar").mkdir(parents=True)
    (wh / "app" / "pulsar" / "__init__.py").write_text("")
    (wh / "venv" / "Scripts").mkdir(parents=True)
    (wh / "venv" / "Scripts" / "python.exe").write_text("")
    js = ("const W = require(%s); const l = W.findLauncher({PULSAR_HOME: %s}, 'win32');"
          "process.stdout.write(JSON.stringify({args: l.args, env: l.env, boot: W.BOOT}));"
          % (json.dumps(str(NPM / "bin" / "pulsarcode.js")), json.dumps(str(wh))))
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30,
                       env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": REAL_HOME})
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert got["args"][:6] == ["-E", "-X", "utf8", "-B", "-c", got["boot"]]
    assert got["args"][7] == "pulsar"
    assert "PYTHONUTF8" not in got["env"]


def test_npm_package_ships_the_license(tmp_path):
    assert (NPM / "LICENSE").read_bytes() == (REPO / "LICENSE").read_bytes()
    pkg = json.loads((NPM / "package.json").read_text(encoding="utf-8"))
    assert pkg["license"] == "AGPL-3.0-or-later"
    npm = shutil.which("npm")
    if not npm:
        return
    # npm keeps its cache and logs under the cache folder and reads ~/.npmrc: both are this test's own here
    env = {"PATH": os.path.dirname(npm) + ":/usr/bin:/bin", "HOME": REAL_HOME,
           "npm_config_cache": str(tmp_path / "npm-cache"), "npm_config_userconfig": str(tmp_path / "npmrc"),
           "npm_config_update_notifier": "false", "npm_config_audit": "false", "npm_config_fund": "false"}
    r = subprocess.run([npm, "pack", "--dry-run", "--json"], cwd=str(NPM), capture_output=True, text=True,
                       timeout=120, env=env)
    if r.returncode != 0:
        pytest.skip("npm pack unavailable: %s" % r.stderr[-200:])
    files = {f["path"] for f in json.loads(r.stdout)[0]["files"]}
    assert "LICENSE" in files and "bin/pulsarcode.js" in files


# --- the shim's python3 fallback sees what the launch sees (the user site), never the current folder --------

def _python_with_user_site_and_no_deps(home: Path):
    """(a system python3 whose user site is on and that lacks Pulsar's requirements, its user site), or None."""
    seen = []
    cands = [getattr(sys, "_base_executable", None), "/usr/bin/python3", "/usr/local/bin/python3", shutil.which("python3")]
    cands += sorted(glob.glob("/usr/bin/python3.[0-9]*") + glob.glob("/usr/local/bin/python3.[0-9]*"))
    for c in cands:
        if not c or not re.search(r"python3(\.\d+)?$", c) or not os.path.isfile(c) or os.path.realpath(c) in seen:
            continue
        seen.append(os.path.realpath(c))
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
        r = subprocess.run([c, "-E", "-c", "import site, sys; print(int(site.ENABLE_USER_SITE is True and "
                            "sys.prefix == sys.base_prefix)); print(site.getusersitepackages())"],
                           env=env, capture_output=True, text=True, timeout=30)
        lines = r.stdout.splitlines()
        if r.returncode != 0 or len(lines) != 2 or lines[0] != "1":
            continue
        if subprocess.run([c, "-E", "-s", "-c", "import starlette, uvicorn, httpx"], env=env,
                          capture_output=True, timeout=30).returncode == 0:
            continue                                    # the requirements are in its own site-packages
        return c, Path(lines[1])
    return None


@pytest.mark.skipif(os.name == "nt", reason="POSIX shim")
def test_shim_fallback_accepts_requirements_in_the_user_site(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    found = _python_with_user_site_and_no_deps(home)
    if not found:
        pytest.skip("no system python3 with a user site and without starlette/uvicorn/httpx")
    py, usersite = found
    app = tmp_path / "app"
    (app / "pulsar").mkdir(parents=True)
    (app / "pulsar" / "__init__.py").write_text("")
    (app / "pulsar" / "__main__.py").write_text("import sys\nprint('fake-pulsar ' + ' '.join(sys.argv[1:]))\n")
    shutil.copy(REPO / "pulsarcode", app / "pulsarcode")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    os.symlink(py, str(bindir / "python3"))
    # a folder you might start pulsarcode from: its own starlette.py & co must never be imported
    evil = tmp_path / "evil"
    evil.mkdir()
    pwned = tmp_path / "PWNED"
    for m in ("starlette", "uvicorn", "httpx"):
        (evil / (m + ".py")).write_text("open(%r, 'a').write(%r)\n" % (str(pwned), m))
    env = {"HOME": str(home), "PULSAR_HOME": str(tmp_path / "ph"), "PATH": "%s:/usr/bin:/bin" % bindir,
           "PYTHONPATH": ".:"}

    def run():
        return subprocess.run(["sh", str(app / "pulsarcode"), "version"], env=env, cwd=str(evil),
                              capture_output=True, text=True, timeout=60)

    # requirements only in the user site (pip install --user): accepted, as the launch itself would see them
    for m in ("starlette", "uvicorn", "httpx"):
        (usersite / m).mkdir(parents=True)
        (usersite / m / "__init__.py").write_text("")
    r = run()
    assert r.returncode == 0 and r.stdout.strip() == "fake-pulsar version", (r.stdout, r.stderr)
    # without them: refused with the reinstall hint, and the current folder's modules were not imported
    shutil.rmtree(str(usersite / "starlette"))
    r = run()
    assert r.returncode == 127 and "virtualenv is missing" in r.stderr, (r.stdout, r.stderr)
    assert not pwned.exists(), pwned.read_text()


# --- install.sh, install.ps1 and pulsar/config.py agree on the marker and on which homes to adopt -----------

PWSH = os.environ.get("PULSAR_TEST_PWSH") or shutil.which("pwsh") or ""
HEADER = "Pulsar Code home. `pulsarcode uninstall` only removes folders that carry this file.\n"
PCFG = '{"version": 2, "active": "p", "profiles": {"p": {"id": "p", "provider": "custom"}}}'
M = ".pulsarcode-home"
HOME_CASES = {
    # marker rule (pulsar/config.py marker_kind): a 'created'/'adopted' word after the first line, case-sensitive
    "trusted+user": ({M: HEADER + "created 2026-01-01T00:00:00Z\n", "notes.txt": "hi"}, "trusted"),
    "bare_adopted+user": ({M: HEADER + "adopted\n", "notes.txt": "hi"}, "trusted"),
    "crlf_created+user": ({M: HEADER.replace("\n", "\r\n") + "created 2026-01-01T00:00:00Z\r\n",
                           "notes.txt": "hi"}, "trusted"),
    "not_utf8_then_created+user": ({M: HEADER.encode() + b"\xff\xfe\ncreated 2026-01-01T00:00:00Z\n",
                                    "notes.txt": "hi"}, "trusted"),
    "line1_created+user": ({M: "created 2026-01-01T00:00:00Z\n", "notes.txt": "hi"}, "refused"),
    "upper_CREATED+user": ({M: HEADER + "CREATED 2026-01-01T00:00:00Z\n", "notes.txt": "hi"}, "refused"),
    "createdx+user": ({M: HEADER + "createdx 2026\n", "notes.txt": "hi"}, "refused"),
    "empty_marker+user": ({M: "", "notes.txt": "hi"}, "refused"),
    "legacy+user": ({M: HEADER, "notes.txt": "hi", "src/": None}, "refused"),
    # adoption rule (pulsar/config.py ensure_home). Where a value is a pair it is (engine, installers): the
    # installers put the engine and the runtime into the folder and replace app/ and venv/ whole, so they adopt
    # an earlier install only next to nothing else, and next to a marker without provenance only the engine
    # itself (tests/test_installers.py, HOME_CASES). The engine, which writes only its own state, adopts both.
    "empty": ({"": None}, "created"),
    # only an empty folder is "created"; names alone prove nothing (logs/, cache/, keys.json are as likely
    # another tool's), so state without Pulsar's evidence is refused
    "no_marker+state_only": ({"run/": None, "cache/": None, "keys.json": "{}"}, "refused"),
    "no_marker+nim.key+user": ({"nim.key": "nvapi-FAKE-not-a-key", "notes.txt": "hi"}, ("adopted", "refused")),
    "no_marker+cfg_without_active": ({"config.json": '{"profiles": {}}'}, "refused"),
    "legacy+state_only": ({M: HEADER, "run/": None, "keys.json": "{}"}, "refused"),
    "legacy+nim.key+user": ({M: HEADER, "nim.key": "nvapi-FAKE-not-a-key", "notes.txt": "hi"}, "refused"),
    "legacy+claude_config+user": ({M: HEADER, "claude_config/": None, "notes.txt": "hi"}, "refused"),
    "legacy+cfg+user": ({M: HEADER, "config.json": PCFG, "notes.txt": "hi"}, "refused"),
    "legacy+cfg_without_active": ({M: HEADER, "config.json": '{"profiles": {}}'}, "refused"),
    "legacy+app+user": ({M: HEADER, "app/pulsar/__init__.py": "", "notes.txt": "hi"}, ("adopted", "refused")),
    # a development build's home: a marker without provenance and nothing but Pulsar's own files
    "legacy_dev_home": ({M: HEADER, "cache/": None, "run/": None, "logs/": None, "claude_config/": None,
                         "config.json": PCFG, "keys.json": "{}"}, ("adopted", "refused")),
    "legacy+nim.key+venv+temp": ({M: HEADER, "nim.key": "nvapi-FAKE-not-a-key", "venv/": None,
                                  ".config.json.ab12cd": ""}, ("adopted", "refused")),
    # one line rule everywhere: LF ends a line (one CR before it is allowed); nothing else does, and the bytes
    # are UTF-8 (no BOM detection)
    "cr_only_created+user": ({M: HEADER.rstrip("\n") + "\rcreated 2026-01-01T00:00:00Z\r", "notes.txt": "hi"},
                             "refused"),
    "formfeed_created+user": ({M: HEADER.rstrip("\n") + "\fcreated 2026-01-01T00:00:00Z\n", "notes.txt": "hi"},
                              "refused"),
    "vtab_created+user": ({M: HEADER.rstrip("\n") + "\vcreated 2026-01-01T00:00:00Z\n", "notes.txt": "hi"},
                          "refused"),
    "u2028_created+user": ({M: HEADER.rstrip("\n") + "\u2028created 2026-01-01T00:00:00Z\n", "notes.txt": "hi"},
                           "refused"),
    "utf16le_bom_created+user": ({M: (HEADER + "created 2026-01-01T00:00:00Z\n").replace("\n", "\r\n").encode("utf-16"),
                                  "notes.txt": "hi"}, "refused"),
    "crlf_bare_created+user": ({M: HEADER + "created\r\n", "notes.txt": "hi"}, "trusted"),
    # config.json is Pulsar's only with a top-level "profiles" object and an "active" key (config.py
    # _pulsar_config), decided from the parsed JSON, never because the two words appear in it
    "legacy+cfg_profiles_list+cache": ({M: HEADER, "config.json": '{"profiles": [], "active": ""}', "cache/": None},
                                       "refused"),
    "no_marker+cfg_list+user": ({"config.json": '{"profiles": ["dev", "prod"], "active": "dev"}', "notes.txt": "hi",
                                 "src/": None}, "refused"),
    "no_marker+cfg_nested+user": ({"config.json": '{"name": "my-service", "aws": {"profiles": {"dev": {}}, '
                                                  '"active": "dev"}}', "notes.txt": "hi", "src/": None}, "refused"),
    "no_marker+cfg_list_only": ({"config.json": '{"profiles": ["dev"], "active": "dev"}'}, "refused"),
    "no_marker+cfg_comment": ({"config.json": '{"profiles": {}, /* json5 */ "active": ""}'}, "refused"),
    "no_marker+cfg_bom": ({"config.json": b"\xef\xbb\xbf" + PCFG.encode()}, "refused"),
    "no_marker+cfg_only": ({"config.json": PCFG}, "adopted"),
    "no_marker+cfg+user": ({"config.json": PCFG, "notes.txt": "hi"}, ("adopted", "refused")),
    # nobody adopts next to an app, venv or bin that holds someone else's files: uninstall removes those whole
    "no_marker+nim.key+foreign_app": ({"nim.key": "nvapi-FAKE-not-a-key", "app/notes.txt": "hi"}, "refused"),
    "no_marker+nim.key+foreign_venv": ({"nim.key": "nvapi-FAKE-not-a-key", "venv/notes.txt": "hi"}, "refused"),
    "no_marker+cfg+foreign_bin": ({"config.json": PCFG, "bin/tool": "x"}, "refused"),
    "legacy+app+empty_venv+user": ({M: HEADER, "app/pulsar/__init__.py": "", "venv/": None, "notes.txt": "hi"},
                                   ("adopted", "refused")),
    # keys.json and settings.json must hold a JSON object (config.py _only_pulsar_state)
    "no_marker+bad_keys+run": ({"run/": None, "keys.json": "not json"}, "refused"),
    "no_marker+keys_list+run": ({"run/": None, "keys.json": "[]"}, "refused"),
    "no_marker+bad_settings": ({"settings.json": "{"}, "refused"),
}


def _make_home(d: Path, files):
    d.mkdir(parents=True)
    for rel, text in files.items():
        if rel == "":
            continue
        if rel.endswith("/"):
            (d / rel).mkdir(parents=True, exist_ok=True)
            continue
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_bytes(text if isinstance(text, bytes) else text.encode())


def _outcome(d: Path, before, refused: bool) -> str:
    if refused:
        return "refused"
    after = (d / M).read_bytes() if (d / M).is_file() else None
    if before is not None and after == before:
        return "trusted"
    lines = (after or b"").decode("utf-8", "replace").splitlines()
    return lines[1].split(" ")[0] if len(lines) > 1 else "?"


def _ps_home_step(tmp_path) -> Path:
    """install.ps1 with the Windows-only guard off and a stop right after the home step (nothing else changes)."""
    src = (REPO / "install.ps1").read_text(encoding="ascii")
    guard = "if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {\n"
    uv = "    # --- uv ----"
    assert src.count(guard) == 1 and src.count(uv) == 1
    src = src.replace(guard, "if ($false) {\n").replace(uv, "    Write-Host 'TEST-HOME-DONE'\n    return\n" + uv)
    out = tmp_path / "home_step.ps1"
    out.write_text(src, encoding="ascii")
    return out


@pytest.mark.skipif(os.name == "nt" or not shutil.which("bash"), reason="install.sh needs bash")
def test_installers_and_ensure_home_share_the_marker_and_adoption_rules(tmp_path, monkeypatch):
    from pulsar import config as K
    userhome = tmp_path / "userhome"
    userhome.mkdir()
    monkeypatch.setenv("HOME", str(userhome))
    ps1 = _ps_home_step(tmp_path) if PWSH else None
    got, wants = {}, {}
    for name, (files, want) in HOME_CASES.items():
        engine_want, installer_want = want if isinstance(want, tuple) else (want, want)
        wants[name] = {"py": engine_want, "sh": installer_want, "ps": installer_want}
        row = {}
        for who in ("py", "sh", "ps") if ps1 else ("py", "sh"):
            d = tmp_path / who / name
            _make_home(d, files)
            before = (d / M).read_bytes() if (d / M).is_file() else None
            if who == "py":
                monkeypatch.setenv("PULSAR_HOME", str(d))
                try:
                    K.ensure_home()
                    refused = False
                except K.HomeError:
                    refused = True
            elif who == "sh":
                env = {"HOME": str(userhome), "PATH": "/usr/bin:/bin", "LANG": "C", "TERM": "dumb",
                       "PULSAR_HOME": str(d), "INSTALL_SH": str(REPO / "install.sh")}
                r = subprocess.run(["bash", "-c", 'source "$INSTALL_SH"; prepare_home; echo TEST-HOME-DONE'],
                                   env=env, capture_output=True, text=True, timeout=60)
                refused = "TEST-HOME-DONE" not in r.stdout
                assert not refused or any(w in r.stdout + r.stderr for w in ("did not create", "cannot tell are its own")), (name, r.stdout, r.stderr)
            else:
                env = {"HOME": str(userhome), "PATH": "/usr/bin:/bin", "LANG": "C", "NO_COLOR": "1",
                       "PULSAR_HOME": str(d), "PULSAR_BIN_DIR": str(tmp_path / "psbin"),
                       "TMPDIR": str(tmp_path)}
                r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(ps1)], env=env,
                                   capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
                refused = "TEST-HOME-DONE" not in r.stdout
                assert not refused or any(w in r.stdout + r.stderr for w in ("did not create", "cannot tell are its own")), (name, r.stdout, r.stderr)
            row[who] = _outcome(d, before, refused)
        got[name] = row
    bad = {n: row for n, row in got.items() if any(row[who] != wants[n][who] for who in row)}
    assert not bad, "\n".join("%s: want %s, got %s" % (n, wants[n], row) for n, row in sorted(bad.items()))


@pytest.mark.skipif(os.name == "nt" or not shutil.which("bash"), reason="install.sh needs bash")
def test_install_sh_without_python3_never_takes_a_config_json_for_pulsars(tmp_path):
    # Without a python3 to parse it, config.json (keys.json, settings.json) is never taken for Pulsar's own:
    # a project folder is refused instead of adopted. An older home is still recognised by its other files.
    nopy = tmp_path / "nopy"
    nopy.mkdir()
    for d in ("/usr/bin", "/bin"):
        for n in os.listdir(d):
            if not n.startswith("python") and not (nopy / n).exists() and os.path.isfile(os.path.join(d, n)):
                os.symlink(os.path.join(d, n), str(nopy / n))
    userhome = tmp_path / "userhome"
    userhome.mkdir()
    cases = {
        "nested+user": ({"config.json": '{"aws": {"profiles": {"dev": {}}, "active": "dev"}}', "notes.txt": "hi"},
                        "refused"),
        "pulsar_cfg_only": ({"config.json": PCFG}, "refused"),
        "pulsar_cfg+claude_config": ({"config.json": PCFG, "claude_config/": None, "keys.json": "{}"}, "adopted"),
        "state_without_json": ({"run/": None, "cache/": None}, "refused"),
    }
    for name, (files, want) in cases.items():
        d = tmp_path / name
        _make_home(d, files)
        env = {"HOME": str(userhome), "PATH": str(nopy), "LANG": "C", "TERM": "dumb", "PULSAR_HOME": str(d),
               "INSTALL_SH": str(REPO / "install.sh")}
        r = subprocess.run([str(nopy / "bash"), "-c", 'source "$INSTALL_SH"; json_python && echo HAS-PYTHON; '
                            'prepare_home; echo TEST-HOME-DONE'], env=env, capture_output=True, text=True, timeout=60)
        assert "HAS-PYTHON" not in r.stdout, r.stdout
        refused = "TEST-HOME-DONE" not in r.stdout
        assert not refused or any(w in r.stdout + r.stderr for w in ("did not create", "cannot tell are its own")), (name, r.stdout, r.stderr)
        assert _outcome(d, None, refused) == want, (name, r.stdout, r.stderr)
