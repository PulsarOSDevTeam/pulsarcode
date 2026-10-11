# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Packaging and release plumbing: pyproject, npm wrapper, Homebrew, Scoop, workflows, tools.

Standard library only (tomllib on 3.11+, PyYAML when installed); every check that needs a
missing tool skips instead of failing, so this runs anywhere Python 3.9+ runs.
"""

import ast
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

import pulsar

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
NPM = ROOT / "packaging" / "npm"
FORMULA = ROOT / "packaging" / "homebrew" / "pulsarcode.rb"
SCOOP = ROOT / "packaging" / "scoop" / "pulsarcode.json"
WORKFLOWS = ROOT / ".github" / "workflows"
RELEASE_TOOLS = ROOT / "tools" / "release"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
SHA_PIN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$")
# Actions whose commit SHA could not be resolved yet; each one carries a TODO(pin) comment.
UNPINNED_ALLOWED = set()
REL_VERSIONS = ["3.0.0", "3.1.0a1", "3.1.0b2", "3.1.0rc1", "3.1.0.dev3", "3.1.0rc1.dev2", "10.0.12"]
# Programs keep the caller's HOME: on macOS one started with a made-up HOME can raise a Keychain dialog.
REAL_HOME = os.environ.get("HOME") or os.path.expanduser("~")
# A test whose programs need a scratch HOME of their own runs in CI and on Linux, never on a desktop Mac.
scratch_home_only_in_ci = pytest.mark.skipif(sys.platform == "darwin" and not os.environ.get("CI"),
                                             reason="gives its programs a made-up HOME: CI and Linux only")

sys.path.insert(0, str(RELEASE_TOOLS))


def _tool(name):
    return importlib.import_module(name)


def _toml():
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        pytest.skip("tomllib needs Python 3.11+")
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _yaml():
    try:
        import yaml
    except ImportError:
        pytest.skip("PyYAML not installed")
    return yaml


def _requirements():
    out = []
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def _norm_req(req):
    return re.sub(r"\s+", "", req).lower()


def _js_boot():
    src = (NPM / "bin" / "pulsarcode.js").read_text(encoding="utf-8")
    m = re.search(r"const BOOT = ((?:\s*\+?\s*\"[^\"]*\")+);", src)
    assert m, "BOOT constant not found in pulsarcode.js"
    return "".join(re.findall(r"\"([^\"]*)\"", m.group(1)))


# --------------------------------------------------------------------------- pyproject

def test_pyproject_metadata():
    data = _toml()
    project = data["project"]
    assert project["name"] == "pulsarcode"
    assert project["requires-python"] == ">=3.9"
    assert "version" in project["dynamic"] and "version" not in project
    assert project["license"] == "AGPL-3.0-or-later"
    assert project["scripts"] == {"pulsarcode": "pulsar.cli:main"}
    for kw in ("claude-code", "llm", "ai-coding", "nvidia-nim", "openrouter", "ollama", "terminal",
               "cli", "agents", "mcp"):
        assert kw in project["keywords"], kw
    assert data["build-system"]["build-backend"] == "hatchling.build"
    assert data["tool"]["hatch"]["version"]["path"] == "pulsar/__init__.py"
    targets = data["tool"]["hatch"]["build"]["targets"]
    assert targets["wheel"]["packages"] == ["pulsar"]
    # twine < 7 (bundled by pypa/gh-action-pypi-publish <= v1.14.1) rejects Metadata-Version 2.5,
    # hatchling's default since 1.27.
    assert targets["wheel"]["core-metadata-version"] == "2.4"
    assert targets["sdist"]["core-metadata-version"] == "2.4"
    assert project["urls"]["Source"] == "https://github.com/" + pulsar.REPO
    assert "readme" in project["dynamic"] and "readme" not in project
    assert any(r.startswith("hatch-fancy-pypi-readme") for r in data["build-system"]["requires"])


def test_package_metadata_claims_only_what_exists():
    """Until the first public release: Beta, not Production/Stable, and every homepage and documentation
    link points at the repository, never at a GitHub Pages site that does not exist yet."""
    data = _toml()
    project = data["project"]
    assert "Development Status :: 4 - Beta" in project["classifiers"]
    assert not [c for c in project["classifiers"] if c.startswith("Development Status") and "Beta" not in c]
    repo = "https://github.com/" + pulsar.REPO
    assert project["urls"]["Homepage"] == repo and project["urls"]["Documentation"] == repo + "#readme"
    pkg = json.loads((NPM / "package.json").read_text(encoding="utf-8"))
    assert pkg["homepage"] == repo + "#readme"
    assert re.search(r'(?m)^  homepage "([^"]+)"$', FORMULA.read_text(encoding="utf-8")).group(1) == repo
    assert json.loads(SCOOP.read_text(encoding="utf-8"))["homepage"] == repo
    for path in (PYPROJECT, NPM / "package.json", NPM / "README.md", FORMULA, SCOOP):
        assert "github.io" not in path.read_text(encoding="utf-8"), path


def test_pypi_readme_has_no_relative_links():
    """The fancy-pypi-readme substitutions turn every relative link in README.md absolute."""
    data = _toml()
    hook = data["tool"]["hatch"]["metadata"]["hooks"]["fancy-pypi-readme"]
    assert [f["path"] for f in hook["fragments"]] == ["README.md"]

    def render(text):
        for sub in hook["substitutions"]:
            text = re.sub(sub["pattern"], sub["replacement"], text)
        return text.replace("$HFPR_VERSION", "9.9.9")

    sample = "\n".join([
        "[![License](https://img.shields.io/badge/x.svg)](LICENSE)",
        "[Platform](#requirements) and [made](#made-by)",
        "![demo](assets/brand/hero.png) ![abs](https://example.com/a.png)",
        "See [CONTRIBUTING.md](CONTRIBUTING.md), [docs](docs/ARCHITECTURE.md) and [mail](mailto:a@b.c).",
        '<img src="assets/brand/logo.svg" alt="x"> <a href="SECURITY.md">s</a> <a href="#install">i</a>',
        "[ref]: docs/x.md",
        "[anchor-ref]: #usage",
    ])
    out = render(sample)
    blob = "https://github.com/PulsarOSDevTeam/pulsarcode/blob/v9.9.9/"
    raw = "https://raw.githubusercontent.com/PulsarOSDevTeam/pulsarcode/v9.9.9/"
    for expected in ("](%sLICENSE)" % blob, "](%sREADME.md#requirements)" % blob, "](%sassets/brand/hero.png)" % raw,
                     "](https://example.com/a.png)", "](%sdocs/ARCHITECTURE.md)" % blob, "](mailto:a@b.c)",
                     'src="%sassets/brand/logo.svg"' % raw, 'href="%sSECURITY.md"' % blob,
                     'href="%sREADME.md#install"' % blob, "[ref]: %sdocs/x.md" % blob,
                     "[anchor-ref]: %sREADME.md#usage" % blob):
        assert expected in out, (expected, out)
    real = render((ROOT / "README.md").read_text(encoding="utf-8"))
    links = re.findall(r"\]\(([^)\s]+)\)|(?:src|href)=\"([^\"]+)\"", real)
    relative = [a or b for a, b in links if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", a or b)]
    assert not relative, relative


def test_build_locks_are_hash_pinned_and_match_pyproject():
    for name in ("requirements-build.txt", "requirements-check.txt"):
        text = (RELEASE_TOOLS / name).read_text(encoding="utf-8")
        reqs = re.findall(r"(?m)^([A-Za-z0-9_.-]+)==([^ ;\\]+)", text)
        assert reqs, name
        blocks = re.split(r"(?m)^(?=[A-Za-z0-9_.-]+==)", text)[1:]
        for block in blocks:
            assert "--hash=sha256:" in block, (name, block.splitlines()[0])
    build = dict(re.findall(r"(?m)^([A-Za-z0-9_.-]+)==([^ ;\\]+)", (RELEASE_TOOLS / "requirements-build.txt").read_text()))
    for req in _toml()["build-system"]["requires"]:
        pkg = re.match(r"[A-Za-z0-9_.-]+", req).group(0)
        assert pkg in build, "%s from [build-system] is missing in requirements-build.txt" % pkg
    assert "build" in build
    check = dict(re.findall(r"(?m)^([A-Za-z0-9_.-]+)==([^ ;\\]+)", (RELEASE_TOOLS / "requirements-check.txt").read_text()))
    # Must match what the pinned pypa/gh-action-pypi-publish bundles (see release.yml).
    release = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    m = re.search(r"bundles twine (\S+) \+ packaging (\S+), the versions pinned", release)
    assert m and check["twine"] == m.group(1) and check["packaging"] == m.group(2).rstrip(",")


def test_pyproject_dependencies_match_requirements_txt():
    deps = _toml()["project"]["dependencies"]
    assert sorted(map(_norm_req, deps)) == sorted(map(_norm_req, _requirements()))


def test_version_is_read_from_package_and_is_pep440():
    # hatchling's default version source pattern, applied to the file it is pointed at
    text = (ROOT / "pulsar" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r"""(?i)^(__version__|VERSION) *= *(['"])v?(?P<version>.+?)\2""", text, re.M)
    assert m and m.group("version") == pulsar.__version__
    assert re.match(r"^\d+\.\d+\.\d+((a|b|rc)\d+)?(\.post\d+)?(\.dev\d+)?$", pulsar.__version__)


def test_console_script_target_exists():
    tree = ast.parse((ROOT / "pulsar" / "cli.py").read_text(encoding="utf-8"))
    mains = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"]
    assert mains, "pulsar.cli has no top-level main()"
    args = mains[0].args
    # console scripts call main() with no arguments
    assert len(args.args) == len(args.defaults), "main() must be callable without arguments"


# --------------------------------------------------------------------------- npm wrapper

def test_npm_package_json():
    pkg = json.loads((NPM / "package.json").read_text(encoding="utf-8"))
    assert pkg["name"] == "pulsarcode"
    # npm spells pre-releases the semver way (3.1.0rc1 -> 3.1.0-rc.1); see update_manifests.py
    assert pkg["version"] == _tool("update_manifests").npm_package_version(pulsar.__version__)
    assert pkg["bin"] == {"pulsarcode": "bin/pulsarcode.js"}
    assert pkg["files"] == ["bin/pulsarcode.js"]
    assert pkg["engines"]["node"] == ">=18"
    assert pkg["license"] == "AGPL-3.0-or-later"
    assert pkg["repository"]["url"] == "git+https://github.com/%s.git" % pulsar.REPO
    assert not pkg.get("dependencies"), "the wrapper must stay dependency-free"
    assert (NPM / "bin" / "pulsarcode.js").read_text(encoding="utf-8").startswith("#!/usr/bin/env node\n")
    if os.name != "nt":
        assert os.access(str(NPM / "bin" / "pulsarcode.js"), os.X_OK)


def test_npm_wrapper_uses_the_same_bootstrap():
    assert _js_boot() == pulsar.BOOT


def test_npm_wrapper_never_builds_shell_strings():
    src = (NPM / "bin" / "pulsarcode.js").read_text(encoding="utf-8")
    assert "shell: true" not in src and "execSync" not in src
    assert not re.search(r"(?<![.\w])exec(File)?(Sync)?\s*\(", src), "only spawn() with argv arrays"
    assert "^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}$" in src
    assert "command: 'bash'" not in src and "command: 'powershell.exe'" not in src
    assert "'main'" not in src.split("function defaultRef", 1)[0], "the ref must default to the package version"


def _node_eval(code):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    p = subprocess.run([node, "-e", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
    return json.loads(p.stdout.decode("utf-8"))


def test_npm_version_mapping_round_trips_through_the_wrapper():
    """update_manifests maps PEP 440 to semver; the wrapper maps semver back to the same tag."""
    um = _tool("update_manifests")
    mapped = {v: um.npm_version(v) for v in REL_VERSIONS}
    assert mapped["3.1.0rc1"] == "3.1.0-rc.1" and mapped["3.1.0a1"] == "3.1.0-alpha.1"
    assert mapped["3.1.0.dev3"] == "3.1.0-dev.3" and mapped["3.1.0rc1.dev2"] == "3.1.0-rc.1.dev.2"
    assert um.npm_version("3.1.0.post1") is None and um.npm_package_version("3.1.0.post1") == "3.1.0"
    for v in mapped.values():
        assert um.SEMVER_RE.match(v), v
    code = ("const w=require(%s);const m=%s;const o={};for(const k of Object.keys(m))o[k]=w.tagForVersion(m[k]);"
            "process.stdout.write(JSON.stringify(o))") % (json.dumps(str(NPM / "bin" / "pulsarcode.js")), json.dumps(mapped))
    assert _node_eval(code) == {v: "v" + v for v in REL_VERSIONS}


def test_npm_accepts_the_mapped_versions():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    semver = Path(os.path.realpath(node)).parent.parent / "lib" / "node_modules" / "npm" / "node_modules" / "semver"
    if not semver.is_dir():
        pytest.skip("npm's bundled semver not found next to node")
    um = _tool("update_manifests")
    mapped = [um.npm_version(v) for v in REL_VERSIONS]
    code = "const s=require(%s);process.stdout.write(JSON.stringify(%s.map(v=>s.valid(v))))" % (
        json.dumps(str(semver)), json.dumps(mapped))
    assert _node_eval(code) == mapped


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@scratch_home_only_in_ci                    # the node tests start the wrapper, npm and npx with a fake HOME
def test_npm_wrapper_node_tests():
    p = subprocess.run(["node", "--test", str(NPM / "test" / "pulsarcode.test.js")], cwd=str(ROOT),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
    out = p.stdout.decode("utf-8", "replace")
    assert p.returncode == 0, out[-3000:]


# --------------------------------------------------------------------------- Homebrew / Scoop

def test_homebrew_formula_is_plausible_ruby():
    rb = FORMULA.read_text(encoding="utf-8")
    assert "class Pulsarcode < Formula" in rb
    assert 'depends_on "python@3.12"' in rb
    assert 'license "AGPL-3.0-or-later"' in rb
    assert re.search(r"(?m)^  test do$", rb)
    url = re.search(r'(?m)^  url "([^"]+)"$', rb).group(1)
    sha = re.search(r'(?m)^  sha256 "([^"]+)"$', rb).group(1)
    assert HEX64.match(sha)
    m = re.match(r"^https://github\.com/PulsarOSDevTeam/pulsarcode/releases/download/v([^/]+)/"
                 r"pulsarcode-app-([^/]+)\.tar\.gz$", url)
    assert m and m.group(1) == m.group(2)
    # block structure: every opener has an end
    code = [line.split("#", 1)[0] for line in rb.splitlines()]
    body = "\n".join(line for line in code if not line.strip().startswith("<<~"))
    openers = len(re.findall(r"(?m)^\s*(?:class|def|module)\b|\bdo\s*(?:\|[^|]*\|)?\s*$", body))
    ends = len(re.findall(r"(?m)^\s*end\s*$", body))
    assert openers == ends, (openers, ends)


@pytest.mark.skipif(shutil.which("ruby") is None, reason="ruby not installed")
def test_homebrew_formula_ruby_syntax():
    p = subprocess.run(["ruby", "-c", str(FORMULA)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert p.returncode == 0, p.stdout.decode("utf-8", "replace")


def test_scoop_manifest():
    m = json.loads(SCOOP.read_text(encoding="utf-8"))
    for key in ("version", "description", "homepage", "license", "url", "hash", "extract_dir", "bin",
                "checkver", "autoupdate", "installer"):
        assert key in m, key
    assert HEX64.match(m["hash"])
    v = m["version"]
    assert m["url"] == ("https://github.com/PulsarOSDevTeam/pulsarcode/releases/download/v%s/"
                        "pulsarcode-app-%s.zip" % (v, v))
    assert m["extract_dir"] == "pulsarcode-" + v
    assert m["checkver"]["github"] == "https://github.com/" + pulsar.REPO
    assert "$version" in m["autoupdate"]["url"] and m["autoupdate"]["extract_dir"] == "pulsarcode-$version"
    script = "\n".join(m["installer"]["script"])
    code = "\n".join(line for line in m["installer"]["script"] if not line.lstrip().startswith("#"))
    assert "-m pulsar" not in code
    boot = re.search(r"^\$boot = '((?:[^']|'')*)'$", script, re.M)
    assert boot and boot.group(1).replace("''", "'") == pulsar.BOOT
    # Like install.ps1's launchers: -E so a PYTHONPATH naming the current folder cannot run its
    # sitecustomize.py before the bootstrap, -X utf8 instead of PYTHONUTF8 (which -E ignores).
    shim = [line for line in m["installer"]["script"] if line.startswith("$shim = ")]
    selftest = [line for line in m["installer"]["script"] if line.rstrip().endswith("pulsar version")]
    assert len(shim) == 1 and len(selftest) == 1, (shim, selftest)
    assert "python.exe\" -E -X utf8 -B -c \"" in shim[0] and "PYTHONUTF8" not in shim[0], shim[0]
    assert selftest[0].startswith("& $py -E -X utf8 -B -c $boot "), selftest[0]


def test_homebrew_and_scoop_point_at_the_same_release():
    rb = FORMULA.read_text(encoding="utf-8")
    scoop = json.loads(SCOOP.read_text(encoding="utf-8"))
    assert "/v%s/pulsarcode-app-%s.tar.gz" % (scoop["version"], scoop["version"]) in rb


# --------------------------------------------------------------------------- release tools

def test_update_manifests_rewrites(tmp_path):
    um = _tool("update_manifests")
    rb = FORMULA.read_text(encoding="utf-8")
    new = um.update_formula(rb, "https://example.invalid/x-9.9.9.tar.gz", "a" * 64)
    assert 'url "https://example.invalid/x-9.9.9.tar.gz"' in new and 'sha256 "%s"' % ("a" * 64) in new
    assert new.count("\n") == rb.count("\n")
    data = um.update_scoop(json.loads(SCOOP.read_text(encoding="utf-8")), "9.9.9", "https://u/z.zip", "b" * 64)
    assert (data["version"], data["hash"], data["extract_dir"]) == ("9.9.9", "b" * 64, "pulsarcode-9.9.9")
    sums = tmp_path / "SHA256SUMS"
    sums.write_text("%s  pulsarcode-app-9.9.9.tar.gz\n%s *pulsarcode-app-9.9.9.zip\n" % ("c" * 64, "d" * 64))
    assert um.parse_sums(str(sums)) == {"pulsarcode-app-9.9.9.tar.gz": "c" * 64, "pulsarcode-app-9.9.9.zip": "d" * 64}
    out = tmp_path / "gh_output"
    assert um.check_tag("v" + pulsar.__version__, pulsar.__version__, output=str(out)) == 0
    assert "npm_version=%s\n" % um.npm_version(pulsar.__version__) in out.read_text()
    assert um.check_tag("v0.0.0-not-it", pulsar.__version__, output=str(out)) == 1


@pytest.mark.parametrize("version,npm,publish,tag,pre", [
    ("3.1.0", "3.1.0", "3.1.0", "latest", "false"),
    ("3.1.0rc1", "3.1.0-rc.1", "3.1.0-rc.1", "next", "true"),
    ("3.1.0b2", "3.1.0-beta.2", "3.1.0-beta.2", "next", "true"),
    ("3.1.0.dev4", "3.1.0-dev.4", "3.1.0-dev.4", "next", "true"),
    ("3.1.0.post1", "3.1.0", "", "latest", "false"),
])
def test_check_tag_for_pre_and_post_releases(tmp_path, version, npm, publish, tag, pre):
    um = _tool("update_manifests")
    pkg = tmp_path / "package.json"
    pkg.write_text(json.dumps({"version": npm}))
    out = tmp_path / "out"
    assert um.check_tag("v" + version, version, npm_file=str(pkg), output=str(out)) == 0
    assert out.read_text() == "version=%s\nnpm_version=%s\nnpm_tag=%s\nprerelease=%s\n" % (version, publish, tag, pre)
    pkg.write_text(json.dumps({"version": version}))  # the PEP 440 spelling is not valid for npm
    if version != npm:
        assert um.check_tag("v" + version, version, npm_file=str(pkg), output=str(tmp_path / "o2")) == 1
    assert um.check_tag("v0" + version, "0" + version, npm_file=str(pkg), output=str(tmp_path / "o3")) == 1


def test_manifests_skip_pre_releases(capsys):
    um = _tool("update_manifests")
    before = [FORMULA.read_text(encoding="utf-8"), SCOOP.read_text(encoding="utf-8"), (NPM / "package.json").read_text()]
    assert um.main(["--version", "9.9.9rc1", "--dist", "/nonexistent"]) == 0
    assert "pre-release" in capsys.readouterr().out
    assert before == [FORMULA.read_text(encoding="utf-8"), SCOOP.read_text(encoding="utf-8"),
                      (NPM / "package.json").read_text()]


def test_app_archives_are_complete_and_reproducible(tmp_path, monkeypatch):
    ba = _tool("build_archives")
    files = ba.collect()
    for required in ("pulsarcode", "install.sh", "install.ps1", "requirements.txt", "pulsar/__init__.py",
                     "pulsar/cli.py", "LICENSE", "pulsar/fnplug/.claude-plugin/plugin.json"):
        assert required in files, required
    assert not [f for f in files if "__pycache__" in f or f.endswith(".pyc")]
    # the engine as the wheel ships it: every file git tracks under pulsar/, the Stage pane plugin's own
    # dev tests excepted (pyproject excludes them from the wheel too)
    assert not [f for f in files if f.startswith("pulsar/fnplug/tests/")]
    git = shutil.which("git")
    if git and (ROOT / ".git").exists():
        p = subprocess.run([git, "-C", str(ROOT), "ls-files", "-z", "pulsar"], stdout=subprocess.PIPE, timeout=60)
        if p.returncode == 0:
            tracked = {f for f in p.stdout.decode("utf-8").split("\0") if f and not f.startswith("pulsar/fnplug/tests/")}
            assert tracked <= set(files), sorted(tracked - set(files))
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1767225600")
    a, b = tmp_path / "a", tmp_path / "b"
    assert ba.main(["--out", str(a), "--version", "9.9.9", "--checksums"]) == 0
    assert ba.main(["--out", str(b), "--version", "9.9.9"]) == 0
    for name in ("pulsarcode-app-9.9.9.tar.gz", "pulsarcode-app-9.9.9.zip"):
        assert (a / name).read_bytes() == (b / name).read_bytes(), name
    with tarfile.open(str(a / "pulsarcode-app-9.9.9.tar.gz")) as t:
        members = {m.name: m for m in t.getmembers()}
    shim = members["pulsarcode-9.9.9/pulsarcode"]
    assert shim.mode == 0o755 and shim.uid == 0 and shim.mtime == 1767225600
    assert all(n == "pulsarcode-9.9.9" or n.startswith("pulsarcode-9.9.9/") for n in members)
    with zipfile.ZipFile(str(a / "pulsarcode-app-9.9.9.zip")) as z:
        assert "pulsarcode-9.9.9/pulsar/__init__.py" in z.namelist()
    sums = (a / "SHA256SUMS").read_text().splitlines()
    assert len(sums) == 2 and all(HEX64.match(line.split()[0]) for line in sums)


def _body(n, alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"):
    return "".join(alphabet[(i * 7 + 3) % len(alphabet)] for i in range(n))


def test_secret_scan_detects_and_redacts(tmp_path):
    ss = _tool("secret_scan")
    body = _body(64)
    fake = "nvapi-" + body
    leak = tmp_path / "config.env"
    leak.write_text("KEY=%s\nDOC=nvapi-...\nOK=sk-or-...\nAIza%s # secret-scan: allow\n" % (fake, body[:35]))
    hits = ss.scan([str(leak)], root=str(tmp_path), allow=set())
    assert [(h[1], h[2]) for h in hits] == [(1, "nvidia")]
    assert fake not in hits[0][3] and hits[0][3].startswith("nvapi-")
    # an allowlist entry covers exactly that value in exactly that file
    allow = {("config.env", "nvidia", ss.fingerprint(fake))}
    assert ss.scan([str(leak)], root=str(tmp_path), allow=allow) == []
    other = tmp_path / "other.env"
    other.write_text("KEY=%s\nKEY2=nvapi-%s\n" % (fake, _body(64)[::-1]))
    assert [(h[0], h[1]) for h in ss.scan([str(tmp_path)], root=str(tmp_path), allow=allow, git="/nonexistent")] == [
        ("other.env", 1), ("other.env", 2)]


def test_secret_scan_finds_home_paths_private_addresses_and_secret_files(tmp_path):
    """Leaks that are not credentials: a real user's home folder (macOS, Linux, Windows), a private network
    address outside tests, and files that hold secrets by their nature. Placeholder user names, CIDR ranges,
    loopback and documentation addresses, tests' addresses and example env files pass."""
    ss = _tool("secret_scan")
    user = "jq" + "smith"                       # built here, so this file never holds a home path itself
    files = {
        "docs/setup.md": "cd /" + "Users/" + user + "/work\n",
        "tools/run.sh": "HOME=/" + "home/" + user + "\n",
        "scripts/win.ps1": "$p = 'C:" + "\\Users\\" + user + "\\.local'\n",
        "pulsar/net.py": "HOST = '192." + "168.4.20'\n",
        "deploy/.env": "X=1\n",
        "deploy/server.pem": "x\n",
        "deploy/id_ed25519": "x\n",
        "docs/fine.md": ("/home/dev/x /home/u /Users/you/x $d/home/code /tmp/home/user ~/.local /Users/Shared/x\n"
                         "C:\\Users\\Public 10.0.0.0/8 172.16.0.0/12 127.0.0.1 192.0.2.10 v10.0.12\n"),
        "tests/test_net.py": "IP = '10.1.2.3'\n",
        "packaging/npm/test/net.test.js": "const ip = '192.168.1.5';\n",
        "deploy/.env.example": "X=\n",
    }
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    hits = ss.scan(root=str(tmp_path), allow=set(), git="/nonexistent")
    got = sorted((h[0], h[2]) for h in hits)
    assert got == sorted([("docs/setup.md", "home-path"), ("tools/run.sh", "home-path"),
                          ("scripts/win.ps1", "home-path"), ("pulsar/net.py", "private-address"),
                          ("deploy/.env", "secret-file"), ("deploy/server.pem", "secret-file"),
                          ("deploy/id_ed25519", "secret-file")]), got
    assert not [h for h in hits if user in h[3]], hits            # never printed in full
    # a deliberate case: the inline marker, or an allowlist entry for exactly that value in that file
    (tmp_path / "docs" / "setup.md").write_text("cd /" + "Users/" + user + "/work  # secret-scan: allow\n")
    allow = {("deploy/.env", "secret-file", ss.fingerprint("deploy/.env"))}
    got = sorted((h[0], h[2]) for h in ss.scan(root=str(tmp_path), allow=allow, git="/nonexistent"))
    assert ("docs/setup.md", "home-path") not in got and ("deploy/.env", "secret-file") not in got, got


def _plant_leaks(root):
    """The reviewer's cases: keys in test dirs, in folders named dist/build/venv, and UTF-16."""
    import codecs
    files = {
        "tests/zz_fixture.py": "KEY = 'sk-ant-api03-%s'\n" % _body(95),
        "packaging/npm/test/zz.js": "const t = 'ghp_%s';\n" % _body(36),
        "dist/leak.txt": "nvapi-%s\n" % _body(64),
        "docs/build/leak.txt": "gsk_%s\n" % _body(52),
        "pulsar/venv/leak.py": "AWS = 'AKIA%s'\n" % _body(16, "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"),
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    (root / "docs" / "utf16.txt").write_bytes(codecs.BOM_UTF16_LE + ("x=xai-%s\n" % _body(80)).encode("utf-16-le"))
    (root / "docs" / "utf16-nobom.txt").write_bytes(("hf_%s\n" % _body(34)).encode("utf-16-le"))
    # a real virtualenv (pyvenv.cfg) is skipped by the walk fallback; git never lists one
    (root / "env" / "lib").mkdir(parents=True)
    (root / "env" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (root / "env" / "lib" / "botocore.py").write_text("EXAMPLE = 'AKIA%s'\n" % _body(16, "ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
    return sorted(list(files) + ["docs/utf16-nobom.txt", "docs/utf16.txt"])


def test_secret_scan_covers_tests_build_dirs_and_utf16(tmp_path):
    ss = _tool("secret_scan")
    expected = _plant_leaks(tmp_path)
    hits = ss.scan(root=str(tmp_path), allow=set(), git="/nonexistent")
    assert sorted(h[0] for h in hits) == expected


@pytest.mark.skipif(os.name == "nt", reason="fake git is a shell script")
def test_secret_scan_uses_git_ls_files(tmp_path):
    """With git, exactly the files `git ls-files --cached --others --exclude-standard` lists."""
    ss = _tool("secret_scan")
    _plant_leaks(tmp_path)
    (tmp_path / ".git").mkdir()
    listed = ["tests/zz_fixture.py", "dist/leak.txt", "docs/utf16.txt", "deleted.txt", "env/lib/botocore.py"]
    fake_git = tmp_path / "fake-git"
    fake_git.write_text("#!/bin/sh\n[ \"$1\" = -C ] && [ \"$3\" = ls-files ] || exit 9\nprintf '%s\\0' " +
                        " ".join("'%s'" % n for n in listed) + "\n")
    fake_git.chmod(0o755)
    hits = ss.scan(root=str(tmp_path), allow=set(), git=str(fake_git))
    assert sorted(h[0] for h in hits) == ["dist/leak.txt", "docs/utf16.txt", "env/lib/botocore.py", "tests/zz_fixture.py"]


def test_repository_has_no_committed_secrets():
    ss = _tool("secret_scan")
    errors = []
    hits = ss.scan(errors=errors)
    assert not hits, ["%s:%d %s" % h[:3] for h in hits]
    assert not errors, errors


def test_em_dash_audit_checks_every_text_file(tmp_path):
    ea = _tool("em_dash_audit")
    import codecs
    dash = chr(0x2014)
    for rel in ("pulsar/notify.py", ".github/CODEOWNERS", ".gitignore", "LICENSE", "docs/site.css", "docs/x.mjs",
                "setup.cfg", "docs/bad.md"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("ok\nx %s y\n" % dash, encoding="utf-8")
    (tmp_path / "docs" / "bad.md").write_bytes(b"\xc3\x28 invalid utf-8 " + dash.encode("utf-8"))
    (tmp_path / "docs" / "wide.txt").write_bytes(codecs.BOM_UTF16_BE + ("a %s b" % dash).encode("utf-16-be"))
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\0\0" + dash.encode("utf-8"))
    (tmp_path / "clean.txt").write_text("fine - fine\n")
    hits, errors = ea.audit(root=str(tmp_path), git="/nonexistent")
    assert sorted(h[0] for h in hits) == sorted([".github/CODEOWNERS", ".gitignore", "LICENSE", "docs/bad.md",
                                                 "docs/site.css", "docs/wide.txt", "docs/x.mjs", "pulsar/notify.py",
                                                 "setup.cfg"])
    assert not errors
    assert ea.main([str(tmp_path / "clean.txt")]) == 0
    assert ea.main([str(tmp_path / "LICENSE")]) == 1


def test_repository_has_no_em_dash():
    ea = _tool("em_dash_audit")
    hits, errors = ea.audit()
    assert not hits and not errors, (hits, errors)


def test_smoke_absolutizes_relative_launcher_paths(tmp_path, monkeypatch):
    smoke = _tool("smoke")
    (tmp_path / "packaging" / "npm" / "bin").mkdir(parents=True)
    (tmp_path / "packaging" / "npm" / "bin" / "pulsarcode.js").write_text("")
    (tmp_path / "pulsarcode-3.0.0.tgz").write_text("")
    (tmp_path / "pulsar").mkdir()
    monkeypatch.chdir(tmp_path)
    args = ["packaging/npm/bin/pulsarcode.js", "--yes", "--package", "pulsarcode-3.0.0.tgz",
            "--file=pulsarcode-3.0.0.tgz", "pulsar", "-B", "version", "/abs/x.js"]
    out = smoke.absolutize(args)
    assert out == [str(tmp_path / "packaging" / "npm" / "bin" / "pulsarcode.js"), "--yes", "--package",
                   str(tmp_path / "pulsarcode-3.0.0.tgz"), "--file=" + str(tmp_path / "pulsarcode-3.0.0.tgz"),
                   "pulsar", "-B", "version", "/abs/x.js"]


def test_smoke_script_catches_python_dash_m(tmp_path):
    """The hostile-cwd smoke test passes the installed launchers' argv and fails `python -m pulsar`. Its run
    with PYTHONPATH naming the current folder also fails a launcher that starts Python without -E, whose
    planted sitecustomize.py would run before the bootstrap; pip's console scripts skip that run."""
    pytest.importorskip("httpx")
    smoke = _tool("smoke")  # PULSAR_HOME is already a per-test scratch folder (conftest)
    safe = [sys.executable, "-E", "-B", "-c", pulsar.BOOT, str(ROOT), "pulsar"]
    assert smoke.main(safe) == 0
    unsafe = [sys.executable, "-m", "pulsar"]
    assert smoke.main(unsafe) == 1
    without_e = [sys.executable, "-B", "-c", pulsar.BOOT, str(ROOT), "pulsar"]
    assert smoke.main(without_e) == 1
    assert smoke.main(["--no-pythonpath"] + without_e) == 0


# --------------------------------------------------------------------------- GitHub config

def _load_yaml(path):
    return _yaml().safe_load(path.read_text(encoding="utf-8"))


def _uses(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "uses" and isinstance(v, str):
                yield v
            else:
                for x in _uses(v):
                    yield x
    elif isinstance(obj, list):
        for item in obj:
            for x in _uses(item):
                yield x


@pytest.mark.parametrize("name", ["test.yml", "release.yml", "compat.yml"])
def test_workflows_are_valid_and_pinned(name):
    wf = _load_yaml(WORKFLOWS / name)
    on = wf.get("on", wf.get(True))  # YAML 1.1 reads a bare `on` key as True
    assert on and wf["jobs"]
    assert "permissions" in wf
    for ref in _uses(wf):
        if ref.startswith("./"):
            continue
        action = ref.split("@", 1)[0]
        if action in UNPINNED_ALLOWED:
            continue
        assert SHA_PIN.match(ref), "%s: %s is not pinned to a commit SHA" % (name, ref)
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    for ref in re.findall(r"uses: (\S+@[0-9a-f]{40})", text):
        assert re.search(re.escape(ref) + r" # v\d", text), "%s needs a version comment" % ref


def test_release_workflow_least_privilege():
    wf = _load_yaml(WORKFLOWS / "release.yml")
    assert wf["permissions"] == {}
    on = wf.get("on", wf.get(True))
    assert on["push"]["tags"] == ["v*"]
    jobs = wf["jobs"]
    for job_id, job in jobs.items():
        assert "permissions" in job, job_id
    assert jobs["pypi"]["permissions"] == {"id-token": "write"}
    assert jobs["pypi"]["environment"]["name"] == "pypi"
    assert jobs["build"]["permissions"].get("attestations") == "write"
    assert jobs["github-release"]["permissions"] == {"contents": "write"}
    steps = [s.get("uses", "") for s in jobs["pypi"]["steps"]]
    assert any(s.startswith("pypa/gh-action-pypi-publish@") for s in steps)

    # npm: trusted publishing (OIDC) behind the "npm" environment, no long-lived token at all
    npm = jobs["npm"]
    assert npm["permissions"] == {"contents": "read", "id-token": "write"}
    assert npm["environment"]["name"] == "npm"
    assert "vars.NPM_PUBLISH == 'true'" in npm["if"] and "npm_version != ''" in npm["if"]
    npm_run = "\n".join(s.get("run", "") for s in npm["steps"])
    assert '--provenance' in npm_run and '--tag "$NPM_TAG"' in npm_run
    assert "npm install" not in npm_run, "no ad-hoc installs in the publishing job"
    assert "11.5.1" in npm_run
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert "secrets." not in text and "NPM_TOKEN" not in text and "NODE_AUTH_TOKEN" not in text

    # build: only hash-pinned third-party code, and attest + upload come last
    build_steps = jobs["build"]["steps"]
    build_run = "\n".join(s.get("run", "") for s in build_steps)
    assert "--require-hashes" in build_run and "--no-isolation" in build_run
    assert "wheel_smoke" not in build_run and "twine" not in build_run
    for line in build_run.splitlines():
        if "pip install" in line:
            assert "--require-hashes" in line, line
    names = [s.get("uses", "").split("@")[0] for s in build_steps]
    assert names[-2:] == ["actions/attest-build-provenance", "actions/upload-artifact"]

    # check: the smoke test runs without write or OIDC access, and gates every publish job
    assert jobs["check"]["permissions"] == {"contents": "read"}
    check_run = "\n".join(s.get("run", "") for s in jobs["check"]["steps"])
    assert "wheel_smoke.py" in check_run and "requirements-check.txt" in check_run
    for job_id in ("github-release", "pypi", "npm"):
        needs = jobs[job_id]["needs"]
        assert "check" in ([needs] if isinstance(needs, str) else needs), job_id

    # pre-releases: GitHub pre-release flag and dist-tag from verify, manifests skipped
    assert set(jobs["verify"]["outputs"]) == {"version", "npm_version", "npm_tag", "prerelease"}
    assert jobs["manifests"]["if"] == "needs.verify.outputs.prerelease == 'false'"
    assert "PRERELEASE" in jobs["github-release"]["steps"][-1]["env"]
    manifests_run = "\n".join(s.get("run", "") for s in jobs["manifests"]["steps"])
    assert "gh workflow run test.yml" in manifests_run
    assert jobs["manifests"]["permissions"].get("actions") == "write"


def test_release_never_replaces_files_and_signs_only_when_public():
    """Release files are never replaced (no --clobber: a re-run adds what is missing and fails on a file that
    differs), PyPI and npm publish only after the GitHub release with its SHA256SUMS exists, and build
    attestations and npm provenance, which need a public repository, run only when it is one."""
    wf = _load_yaml(WORKFLOWS / "release.yml")
    jobs = wf["jobs"]
    for name in ("release.yml", "test.yml", "compat.yml"):
        assert "--clobber" not in (WORKFLOWS / name).read_text(encoding="utf-8"), name
    for job_id in ("pypi", "npm"):
        needs = jobs[job_id]["needs"]
        assert "github-release" in ([needs] if isinstance(needs, str) else needs), job_id
    public = "github.event.repository.visibility == 'public'"
    attest = [s for s in jobs["build"]["steps"] if s.get("uses", "").startswith("actions/attest-build-provenance@")]
    assert len(attest) == 1 and attest[0].get("if") == public, attest
    publish = [s for s in jobs["npm"]["steps"] if "npm publish" in s.get("run", "")][0]
    assert publish["env"]["VISIBILITY"] == "${{ github.event.repository.visibility }}"
    assert '--provenance=false' in publish["run"] and '"$VISIBILITY" = public' in publish["run"]
    assert "provenance" not in json.loads((NPM / "package.json").read_text(encoding="utf-8")).get("publishConfig", {})
    upload = jobs["github-release"]["steps"][-1]["run"]
    assert "gh release upload" in upload and "cmp -s" in upload and "::error::" in upload


@pytest.mark.parametrize("name", ["test.yml", "release.yml", "compat.yml"])
def test_no_untrusted_expressions_inside_run(name):
    wf = _load_yaml(WORKFLOWS / name)
    for job in wf["jobs"].values():
        for step in job.get("steps", []):
            run = step.get("run", "")
            for expr in re.findall(r"\$\{\{([^}]*)\}\}", run):
                assert not re.search(r"github\.(ref_name|head_ref|event)|inputs\.|secrets\.", expr), \
                    "pass %r through env, not inline in run:" % expr.strip()


def _matrix_rows(include: str, event: str) -> list:
    """The rows test.yml's matrix expression gives a run triggered by `event`."""
    literals = re.findall(r"'((?:[^']|'')*)'", include)
    rows = [lit for lit in literals if lit.lstrip().startswith("{")]
    extra = [lit for lit in literals if lit.lstrip().startswith(",{")]
    assert len(rows) == 1 and len(extra) == 1, literals
    assert "github.event_name != 'pull_request' && ',{" in include and "|| ''))" in include
    return json.loads("[" + rows[0] + (extra[0] if event != "pull_request" else "") + "]")


def test_test_workflow_matrix():
    """On the private repository macOS bills at ten times the Linux rate: pull requests run ubuntu 3.9, 3.10, 3.11
    and 3.13, ubuntu arm64, one macOS row and Windows (experimental); pushes to main and manual runs add x86_64
    macOS."""
    wf = _load_yaml(WORKFLOWS / "test.yml")
    assert wf["concurrency"]["cancel-in-progress"] is True
    on = wf.get("on", wf.get(True))
    assert on["push"]["branches"] == ["main"] and "pull_request" in on and "workflow_dispatch" in on
    include = wf["jobs"]["tests"]["strategy"]["matrix"]["include"]
    assert include.startswith("${{ fromJSON(format('[{0}{1}]',") and include.endswith(")) }}"), include
    pr = {("ubuntu-latest", "3.9", False), ("ubuntu-latest", "3.10", False), ("ubuntu-latest", "3.11", False),
          ("ubuntu-latest", "3.13", False), ("ubuntu-24.04-arm", "3.12", False), ("macos-latest", "3.12", False),
          ("windows-latest", "3.12", True)}
    for event in ("pull_request", "push", "workflow_dispatch"):
        rows = _matrix_rows(include, event)
        got = {(r["os"], r["python"], r["experimental"]) for r in rows}
        assert len(got) == len(rows), rows
        assert got == (pr if event == "pull_request" else pr | {("macos-15-intel", "3.12", False)}), (event, got)
    assert wf["jobs"]["tests"]["continue-on-error"] == "${{ matrix.experimental }}"


def test_every_python_classifier_runs_on_pull_requests():
    """Every Python version pyproject.toml claims (its classifiers) has a pull-request row in the test matrix."""
    claimed = {c.rsplit(" :: ", 1)[1] for c in _toml()["project"]["classifiers"]
               if re.match(r"^Programming Language :: Python :: 3\.\d+$", c)}
    include = _load_yaml(WORKFLOWS / "test.yml")["jobs"]["tests"]["strategy"]["matrix"]["include"]
    tested = {row["python"] for row in _matrix_rows(include, "pull_request")}
    assert claimed and claimed <= tested, sorted(claimed - tested)


def test_audit_job_runs_the_png_checks_with_pillow():
    """The mascot checks on the PNG posters and stills, and the posterize test, skip without Pillow, and the
    test rows install only the runtime dependencies: the audit job installs Pillow and runs them."""
    steps = _load_yaml(WORKFLOWS / "test.yml")["jobs"]["audit"]["steps"]
    install = [s for s in steps if s.get("name") == "Install tools"][0]["run"]
    assert "pillow" in install.lower().split(), install
    brand = [s.get("run", "") for s in steps if "tests/test_demo_brand.py" in s.get("run", "")]
    assert len(brand) == 1 and "tests/test_brand_assets.py" in brand[0] and "pytest" in brand[0], brand


def test_compat_workflow_runs_weekly_and_tests_the_stage_pane():
    """The compat run is weekly (Mondays) and on demand, not nightly, and it also runs the Stage pane
    plugin's own tests with the Claude Code it installs (the only CI job that has one)."""
    wf = _load_yaml(WORKFLOWS / "compat.yml")
    on = wf.get("on", wf.get(True))
    crons = [s["cron"] for s in on["schedule"]]
    assert len(crons) == 1 and crons[0].split()[2:] == ["*", "*", "1"], crons
    assert "workflow_dispatch" in on
    steps = wf["jobs"]["compat"]["steps"]
    run = "\n".join(s.get("run", "") for s in steps)
    assert 'plugin test pulsar/fnplug' in run and "tests/test_e2e_stage.py" in run
    install = [s for s in steps if "Install Claude Code" in s.get("name", "")][0]
    plugin = [s for s in steps if "plugin test pulsar/fnplug" in s.get("run", "")][0]
    assert steps.index(install) < steps.index(plugin)
    assert "secrets." not in (WORKFLOWS / "compat.yml").read_text(encoding="utf-8")


def test_dependabot_and_issue_templates():
    dep = _load_yaml(ROOT / ".github" / "dependabot.yml")
    ecosystems = {(u["package-ecosystem"], u["directory"]) for u in dep["updates"]}
    assert ecosystems == {("pip", "/"), ("github-actions", "/"), ("npm", "/packaging/npm")}
    templates = ROOT / ".github" / "ISSUE_TEMPLATE"
    bug = _load_yaml(templates / "bug_report.yml")
    labels = " ".join(item.get("attributes", {}).get("label", "") for item in bug["body"])
    for field in ("pulsarcode doctor", "Provider", "Model", "Operating system"):
        assert field in labels, field
    assert _load_yaml(templates / "feature_request.yml")["body"]
    cfg = _load_yaml(templates / "config.yml")
    assert cfg["blank_issues_enabled"] is False
    assert any("discussions" in link["url"] for link in cfg["contact_links"])


# --------------------------------------------------------------------------- hygiene

OWNED = [PYPROJECT, FORMULA, SCOOP, NPM / "package.json", NPM / "README.md", NPM / "bin" / "pulsarcode.js",
         RELEASE_TOOLS / "requirements-build.in", RELEASE_TOOLS / "requirements-build.txt",
         RELEASE_TOOLS / "requirements-check.in", RELEASE_TOOLS / "requirements-check.txt",
         RELEASE_TOOLS / "secret_scan_allow.txt",
         NPM / "test" / "pulsarcode.test.js", ROOT / "packaging" / "README.md", ROOT / "tools" / "github" / "setup.sh",
         ROOT / ".github" / "dependabot.yml", ROOT / ".github" / "pull_request_template.md",
         WORKFLOWS / "test.yml", WORKFLOWS / "release.yml", Path(__file__).resolve()] + \
    sorted(RELEASE_TOOLS.glob("*.py")) + sorted((ROOT / ".github" / "ISSUE_TEMPLATE").glob("*.yml"))


@pytest.mark.parametrize("path", OWNED, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_em_dash(path):
    assert chr(0x2014) not in path.read_text(encoding="utf-8")  # the em dash


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="needs bash")
def test_setup_script_dry_run():
    script = ROOT / "tools" / "github" / "setup.sh"
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0
    # a dry run starts no program at all (not even gh), so the caller's HOME is never read
    p = subprocess.run(["bash", str(script), "--dry-run"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       env={"PATH": "/usr/bin:/bin", "HOME": REAL_HOME}, timeout=60)
    out = p.stdout.decode("utf-8", "replace")
    assert p.returncode == 0, out
    assert "gh repo edit PulsarOSDevTeam/pulsarcode" in out and "--enable-wiki=false" in out
    topics = json.loads(re.search(r'(\{"names":\[.*?\]\})', out).group(1))["names"]
    assert len(topics) == 20 == len(set(topics))
    assert "social.png" in out and "dry run" in out
    # GitHub Pages (and a homepage pointing at it) only with --pages, checked against the visibility first
    assert '"path":"/docs"' not in out and "/pages" not in out and "--homepage" not in out, out
    p = subprocess.run(["bash", str(script), "--dry-run", "--pages"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       env={"PATH": "/usr/bin:/bin", "HOME": REAL_HOME}, timeout=60)
    pages = p.stdout.decode("utf-8", "replace")
    assert p.returncode == 0, pages
    assert "gh repo view PulsarOSDevTeam/pulsarcode --json visibility -q .visibility" in pages
    assert '"path":"/docs"' in pages and "--homepage https://pulsarosdevteam.github.io/pulsarcode/" in pages
    assert pages.index("actions/permissions/workflow") < pages.index('"path":"/docs"'), "hardening comes first"
    for env in ("pypi", "npm"):
        assert "environments/%s/deployment-branch-policies" % env in out
    ruleset = json.loads(re.search(r'(\{"name":"release tags".*?\})\nJSON', out).group(1))
    assert ruleset["target"] == "tag" and ruleset["conditions"]["ref_name"]["include"] == ["refs/tags/v*"]
    assert {r["type"] for r in ruleset["rules"]} == {"creation", "update", "deletion"}
    assert "NPM_TOKEN" not in out and "NPM_PUBLISH" in out


FAKE_GH = r'''#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_GH_LOG"
case "$*" in
    "auth status") exit 0 ;;
    *"repo view"*visibility*) echo "$FAKE_VISIBILITY"; exit 0 ;;
    *"--method POST repos/"*/pages*|*"--method PUT repos/"*/pages*) cat >/dev/null; [ -z "$FAKE_PAGES_FAIL" ]; exit ;;
    "api repos/"*/pages|"api repos/"*/environments/*) exit 1 ;;
esac
case "$*" in *"--input -"*) cat >/dev/null ;; esac
exit 0
'''


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="needs bash")
def test_setup_pages_is_opt_in_and_refused_while_private(tmp_path):
    """A GitHub Pages site is public whatever the repository's visibility: --pages is refused before anything
    changes unless the repository is public. When it runs, Pages comes after every hardening step, and a
    Pages failure (a plan without Pages) is a warning, not the end of the setup."""
    script = ROOT / "tools" / "github" / "setup.sh"
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "gh").write_text(FAKE_GH)
    (stubs / "gh").chmod(0o755)
    runs = []

    def run(*args, **extra):
        log = tmp_path / ("gh-%d.log" % len(runs))
        runs.append(log)
        env = dict({"PATH": str(stubs) + ":/usr/bin:/bin", "FAKE_GH_LOG": str(log)}, **extra)
        p = subprocess.run(["bash", str(script)] + list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           env=env, timeout=60)
        return p.returncode, p.stdout.decode("utf-8", "replace"), log.read_text().splitlines() if log.exists() else []

    rc, out, calls = run("--pages", FAKE_VISIBILITY="PRIVATE")
    assert rc != 0 and "--pages" in out and "PRIVATE" in out, out
    assert not [c for c in calls if "--method" in c or c.startswith("repo edit")], calls      # nothing changed
    rc, out, calls = run(FAKE_VISIBILITY="PRIVATE")                                          # the default
    assert rc == 0, out
    assert not [c for c in calls if "/pages" in c or "--homepage" in c], calls
    rc, out, calls = run("--pages", FAKE_VISIBILITY="PUBLIC", FAKE_PAGES_FAIL="1")
    assert rc == 0 and "warning: could not" in out, out
    pages = [i for i, c in enumerate(calls) if "repos/PulsarOSDevTeam/pulsarcode/pages" in c]
    perms = [i for i, c in enumerate(calls) if "actions/permissions/workflow" in c]
    assert pages and perms and min(pages) > max(perms), calls
    assert any(c.startswith("repo edit") and "--homepage https://pulsarosdevteam.github.io/pulsarcode/" in c
               for c in calls), calls


def test_no_invisible_or_bidi_control_characters_in_source():
    """Trojan Source guard: format/control characters (bidi overrides, zero-width) must be written as escapes."""
    import subprocess
    import unicodedata
    root = Path(__file__).resolve().parents[1]
    try:
        listed = subprocess.run(["git", "ls-files"], cwd=str(root), capture_output=True, text=True, timeout=30).stdout.split()
    except (OSError, subprocess.SubprocessError):
        listed = []
    files = [root / f for f in listed] or [p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts]
    bad = []
    for f in files:
        if f.suffix.lower() in (".png", ".gif", ".jpg", ".ico", ".woff", ".woff2", ".pyc") or not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for i, ch in enumerate(text):
            if ch in "\n\t\r":
                continue
            if unicodedata.category(ch) in ("Cf", "Cc", "Co") or ord(ch) in (0x2028, 0x2029):
                bad.append("%s:%d U+%04X" % (f.relative_to(root), text.count("\n", 0, i) + 1, ord(ch)))
    assert not bad, bad[:20]
