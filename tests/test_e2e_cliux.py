# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""End-to-end regression tests for the CLI, the terminal screens, the MCP switch, Pilot, the catalog and the
installer."""

import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pulsar import __version__, catalog as C, cli, config as K, fx, providers as P, screens as S

ROOT = Path(__file__).resolve().parent.parent
FAKE = "fake-key-not-real-0123456789abcdef"


@pytest.fixture(autouse=True)
def outside_a_session(monkeypatch):
    """These run as a person's own terminal would: not inside a Claude Code session (tests may be started
    from one, which exports CLAUDECODE) and not under a Claude Code process, so the human gate never stops
    a command under test."""
    for var in cli.SESSION_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli.L, "claude_ancestor", lambda *a, **kw: False)


def _link(raw):
    det = P.detect(raw)
    prov = det.candidates[0]
    return S.Link(prov, det.key, S.profile_id_for(prov, det.base_url), base_url=det.base_url)


# ---------------------------------------------------------------------------
# 1. key -> save_link keeps the detected base URL (Azure, Foundry, Vertex, Bedrock)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, want", [
    ("https://fake-res.openai.azure.com/openai/v1 " + FAKE, "https://fake-res.openai.azure.com/openai/v1"),
    ("https://fake-res.services.ai.azure.com/anthropic " + FAKE, "https://fake-res.services.ai.azure.com/anthropic"),
    ("vertex my-proj-123 europe-west1",
     "https://europe-west1-aiplatform.googleapis.com/v1/projects/my-proj-123/locations/europe-west1"),
    ("bedrock eu-west-3", "https://bedrock-runtime.eu-west-3.amazonaws.com"),
])
def test_save_link_persists_detected_base_url(raw, want):
    K.ensure_home()
    link = _link(raw)
    prof = cli.save_link(K.load(), link)
    again = K.load().profiles[prof.id]
    assert again.base_url == want
    prov = again.resolve()
    assert prov.base_url.rstrip("/") == want and "YOUR-RESOURCE" not in prov.base_url
    assert cli.placeholder_problem(prov) == ""
    if prov.route == "cloud":
        opts = P.cloud_options(prov, again.base_url)
        assert opts.get("region") in ("europe-west1", "eu-west-3")
        assert P.cloud_missing(prov, opts, environ={}) == []


def test_placeholder_provider_is_refused_before_any_request(monkeypatch, capsys):
    st = K.State()
    st.upsert(K.Profile(id="azure", provider="azure", model="gpt-4o"))        # an older save without base_url
    K.save(st)
    K.set_key("azure", FAKE)
    assert "placeholder" in cli.placeholder_problem(st.profiles["azure"].resolve())
    monkeypatch.setattr(cli.L, "find_claude", lambda: "/bin/true")
    monkeypatch.setattr(cli.L, "claude_version", lambda b: "2.1.287")
    monkeypatch.setattr(cli.L, "start_session", lambda *a, **k: pytest.fail("must not start a session"))
    monkeypatch.setattr(cli.L, "exec_claude", lambda *a, **k: pytest.fail("must not launch"))
    monkeypatch.setattr(C, "load_catalog", lambda *a, **k: C.CatalogResult([], "seed"))
    from pulsar.core import spawn
    monkeypatch.setattr(spawn, "attach", lambda *a, **k: pytest.fail("no Core for an unusable profile"))
    monkeypatch.setattr(spawn, "ensure_core", lambda *a, **k: pytest.fail("no Core for an unusable profile"))
    assert cli.cmd_launch(["-p", "hi"]) == 2
    cap = capsys.readouterr()
    assert "placeholder" in cap.out + cap.err


# ---------------------------------------------------------------------------
# 2. switch never binds an unknown id to the active profile
# ---------------------------------------------------------------------------

def _two_profiles():
    st = K.State()
    st.upsert(K.Profile(id="groq", provider="groq", model="openai/gpt-oss-120b"))
    st.upsert(K.Profile(id="xai", provider="xai", model="grok-4.7"))
    st.active = "groq"
    K.save(st)
    K.set_key("groq", "gsk_" + FAKE)
    K.set_key("xai", "xai-" + FAKE)
    C.write_cache("groq", [C.Model("openai/gpt-oss-120b", "groq")])
    return st


def test_switch_fetches_missing_catalog_and_picks_its_owner(monkeypatch, capsys):
    _two_profiles()
    calls = []

    def fake_load(prov, pid, key, refresh=False, network=True, timeout=8.0):
        calls.append(pid)
        return C.CatalogResult([C.Model("grok-build-1", pid)], "live")
    monkeypatch.setattr(C, "load_catalog", fake_load)
    monkeypatch.setattr("pulsar.core.state.live_sessions", lambda reap=False: [])
    assert cli.main(["switch", "grok-build-1"]) == 0
    out = capsys.readouterr().out
    assert calls == ["xai"] and "xAI" in out and "takes effect on the next message" not in out
    st = K.load()
    assert st.active == "xai" and st.profiles["xai"].model == "grok-build-1"
    assert st.profiles["groq"].model == "openai/gpt-oss-120b"


def test_switch_refuses_an_id_no_catalog_lists(monkeypatch, capsys):
    _two_profiles()
    monkeypatch.setattr(C, "load_catalog", lambda *a, **k: C.CatalogResult([], "seed", "offline"))
    monkeypatch.setattr("pulsar.core.state.live_sessions", lambda reap=False: [])
    assert cli.main(["switch", "grok-build-1"]) == 1
    err = capsys.readouterr().err
    assert "provider:model" in err and "pulsarcode models" in err
    st = K.load()
    assert st.active == "groq" and st.profiles["groq"].model == "openai/gpt-oss-120b"
    # an explicit provider prefix still works
    assert cli.main(["switch", "xai:grok-build-1"]) == 0
    assert K.load().profiles["xai"].model == "grok-build-1"


def test_mcp_switch_resolve_refuses_unknown_ids(monkeypatch):
    from pulsar.mcp import server as M
    _two_profiles()
    monkeypatch.setattr(C, "load_catalog", lambda *a, **k: C.CatalogResult([], "seed", "offline"))
    st = K.load()
    assert M._resolve(st, "grok-build-1") is None
    assert M._resolve(st, "openai/gpt-oss-120b")[0].id == "groq"
    assert M._resolve(st, "xai:grok-build-1")[0].id == "xai"


# ---------------------------------------------------------------------------
# 3, 4, 5. models / providers / status output
# ---------------------------------------------------------------------------

def test_models_never_cuts_a_port(capsys, monkeypatch):
    st = K.State()
    st.upsert(K.Profile(id="custom-127-0-0-1-51051", provider="custom", base_url="http://127.0.0.1:51051/v1"))
    K.save(st)
    long_id = "vendor/" + "x" * 90
    C.write_cache("custom-127-0-0-1-51051", [C.Model("fake/m-1", "custom-127-0-0-1-51051"),
                                             C.Model(long_id, "custom-127-0-0-1-51051")])
    monkeypatch.setenv("COLUMNS", "120")
    assert cli.cmd_models([]) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert all("127.0.0.1:51051" in ln for ln in lines)
    assert "127.0.0.1:5105 " not in "\n".join(lines)
    cols = {ln.index("127.0.0.1:51051") for ln in lines}
    assert len(cols) == 1                                  # long ids no longer push the provider column


def test_label_width_and_tab_start():
    assert S._label_width(["127.0.0.1:51051"]) == 15
    assert S._label_width(["x" * 60]) == 24
    tabs = [(None, "All")] + [("p%d" % i, "Provider %d" % i) for i in range(10)]
    start = S._tab_start(tabs, "p9", 40)
    assert start > 0 and sum(len(lab) + 4 for _, lab in tabs[start:10 + 1]) + 2 <= 40


def test_providers_columns_align(capsys):
    assert cli.cmd_providers([]) == 0
    rows = [ln for ln in capsys.readouterr().out.splitlines() if re.match(r"^ \S \S", ln)]
    starts = {re.search(r" (adapter|native|cloud)\s", ln).start() for ln in rows
              if re.search(r" (adapter|native|cloud)\s", ln)}
    assert len(starts) == 1, starts


def test_status_does_not_tick_a_missing_home(capsys, monkeypatch, pulsar_home):
    monkeypatch.setattr(cli.L, "find_claude", lambda: "")
    assert not pulsar_home.exists()
    assert cli.cmd_status([]) == 0
    out = capsys.readouterr().out
    home_line = next(ln for ln in out.splitlines() if "home " in ln)
    prof_line = next(ln for ln in out.splitlines() if "profile " in ln)
    assert "created on first connect" in home_line and "✓" not in home_line and "+ home" not in home_line
    assert "created on first launch" in prof_line and "✓" not in prof_line
    K.ensure_home()
    os.chmod(str(pulsar_home), 0o700)
    cli.cmd_status([])
    home_line = next(ln for ln in capsys.readouterr().out.splitlines() if "home " in ln)
    assert "created on first" not in home_line


# ---------------------------------------------------------------------------
# 6. key with an empty, non-tty stdin
# ---------------------------------------------------------------------------

class _Stdin(io.StringIO):
    def isatty(self):
        return False


def test_key_with_empty_stdin_says_no_key_received(monkeypatch, capsys):
    _two_profiles()
    monkeypatch.setattr(sys, "stdin", _Stdin(""))
    monkeypatch.setattr(S, "uplink", lambda *a, **k: pytest.fail("must not open the uplink"))
    assert cli.cmd_key([]) == 2
    cap = capsys.readouterr()
    out = cap.out + cap.err
    assert "no key received on stdin" in out and "no provider connected" not in out


# ---------------------------------------------------------------------------
# 7, 8. pilot on inside a session; pilot output into a closed pipe
# ---------------------------------------------------------------------------

def test_pilot_on_inside_a_session_refuses_without_asking(monkeypatch, capsys):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setattr(cli, "_stdio_is_tty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("must not ask for I am human"))
    assert cli.main(["pilot", "on"]) == 3
    cap = capsys.readouterr()
    assert cap.out == "" and "Claude Code session" in cap.err


def test_pilot_on_without_a_terminal_refuses_before_the_banner(monkeypatch, capsys):
    from pulsar.pilot import cli as PC
    for v in PC.IN_SESSION_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(sys, "stdin", _Stdin(""))
    assert PC.cmd_on(PC.Out(), []) == 2
    cap = capsys.readouterr()
    assert cap.out == "" and "interactive terminal" in cap.err


def test_pilot_into_a_closed_pipe_exits_quietly(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~"), "PULSAR_HOME": str(tmp_path / "ph"),
           "PYTHONPATH": str(ROOT)}
    code = "import sys; from pulsar import cli; sys.exit(cli.main(['pilot', 'help']))"
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                         cwd=str(tmp_path))
    p.stdout.close()                                    # the reader (head) is gone before the first line
    err = p.stderr.read().decode()
    p.wait(timeout=30)
    assert "Traceback" not in err and "BrokenPipe" not in err and "Exception ignored" not in err
    assert p.returncode in (0, 141)


# ---------------------------------------------------------------------------
# 9, 10. vision awareness and the catalog User-Agent
# ---------------------------------------------------------------------------

def test_catalog_reads_vision_metadata_and_hints():
    prov = P.get("openrouter")
    models = C.parse_models(prov, "openrouter", {"data": [
        {"id": "a/text", "architecture": {"input_modalities": ["text"]}},
        {"id": "a/seeing", "architecture": {"input_modalities": ["text", "image"]}},
        {"id": "a/unknown"}]})
    by = {m.id: m for m in models}
    assert by["a/text"].vision is False and by["a/seeing"].vision is True and by["a/unknown"].vision is None
    assert C.vision_hint("qwen/qwen2.5-vl-72b-instruct") and C.vision_hint("gpt-4o-mini")
    assert C.vision_hint("openai/gpt-oss-120b") is False


def test_learned_no_vision_heal_counts(pulsar_home):
    K.ensure_home()
    (pulsar_home / "cache").mkdir(exist_ok=True)
    (pulsar_home / "cache" / "heals-groq.json").write_text(json.dumps(
        {"gpt-4o": {"no_vision": True, "_t": int(time.time())}}))
    assert C.model_vision("groq", "gpt-4o") is False             # the provider said so, whatever the id hints


def test_pilot_screenshot_refuses_a_text_only_model(monkeypatch):
    from pulsar.pilot import tools as T
    from tests.test_pilot import make_pilot
    monkeypatch.delenv("PULSAR_SESSION", raising=False)
    monkeypatch.setenv("PULSAR_MODEL_VISION", "0")
    p, b = make_pilot()
    with pytest.raises(Exception) as ei:
        p.screenshot({})
    assert "cannot view images" in str(ei.value)
    assert not any(c[0] == "capture" for c in b.calls)               # the screen was never captured
    monkeypatch.delenv("PULSAR_MODEL_VISION")
    out = p.screenshot({})
    text = " ".join(c.get("text", "") for c in out if c.get("type") == "text")
    assert "cannot tell whether the active model views images" in text
    monkeypatch.setenv("PULSAR_MODEL_VISION", "1")
    text = " ".join(c.get("text", "") for c in p.screenshot({}) if c.get("type") == "text")
    assert "cannot tell" not in text
    assert T.active_vision({"PULSAR_MODEL_VISION": "0"}) == (False, "")


def test_pilot_status_shows_vision(monkeypatch):
    from pulsar.pilot import cli as PC
    st = K.State()
    st.upsert(K.Profile(id="groq", provider="groq", model="openai/gpt-oss-120b"))
    K.save(st)
    monkeypatch.delenv("PULSAR_SESSION", raising=False)
    buf = io.StringIO()
    PC._vision_row(PC.Out(buf))
    assert "vision" in buf.getvalue() and "cannot view images" in buf.getvalue()


def test_catalog_user_agent_carries_the_version():
    assert C.UA.startswith("pulsarcode/%s " % __version__)


# ---------------------------------------------------------------------------
# 11-19. TUI layout helpers
# ---------------------------------------------------------------------------

class _UI:
    def __init__(self, u=True):
        self.u = u

    def mark(self):
        return fx.MARK if self.u else "@"


def _row(cv, y):
    return "".join(c[0] for c in cv.cells[y])


@pytest.mark.parametrize("w", [40, 60])
def test_header_right_text_never_overlaps_the_title(w):
    cv = fx.Canvas(w, 3)
    S.header(cv, _UI(), "MODEL", 0.0, right="530 models  ·  4 uplinks")
    row = _row(cv, 0)
    assert "PULSAR CODE" in row
    title_end = row.index("PULSAR CODE") + len("PULSAR CODE")
    assert "COD5" not in row and "CODE530" not in row
    if "530" in row:
        assert row.index("530") > title_end


def test_footer_fits_whole_items_by_priority():
    hints = [("up/dn", "move"), ("type", "search"), ("tab", "provider"), ("enter", "launch"),
             ("ctrl-n", "add key"), ("esc", "keep current")]
    kept = S.fit_hints(hints, 36, u=False)
    keys = [k for k, _ in kept]
    assert "enter" in keys and "esc" in keys
    assert ("esc", "keep current") in kept                                 # never 'keep curre'
    width = sum(len(k) + 1 + len(v) for k, v in kept) + 5 * (len(kept) - 1)
    assert width <= 36
    assert [k for k, _ in S.fit_hints(hints, 200)] == [k for k, _ in hints]
    q = S.fit_hints([("enter", "connect"), ("ctrl-o", "open the NVIDIA key page"), ("esc", "back"),
                     ("ctrl-c", "quit")], 44)
    assert [k for k, _ in q] == ["enter", "esc", "ctrl-c"]


def test_wrap_hard_splits_long_ids():
    mid = "accounts/fireworks/models/" + "a" * 40 + "-instruct-" + "b" * 12
    lines = S.wrap(mid, 32)
    assert all(len(ln) <= 32 for ln in lines) and "".join(lines) == mid
    assert lines[0].endswith("/")
    clipped = S.wrap_clip("x" * 200, 32, 3)
    assert len(clipped) == 3 and clipped[-1].endswith("…") and all(len(ln) <= 32 for ln in clipped)


def test_demo_marker_at_every_size():
    for w, h in ((40, 12), (60, 18), (30, 8), (120, 40)):
        tasks = [S.Task("Claude Code", lambda: ("ok", "ready"))]
        for t in tasks:
            t.status, t.detail = "ok", "ready"
        b = S._Boot(tasks, True, True, __import__("random").Random(1), demo=True)
        cv = b.frame(w, h, 3.0)
        text = "\n".join(_row(cv, y) for y in range(h))
        assert "DEMO" in text, (w, h)


def test_demo_quick_is_quick(monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", lambda d: slept.append(d))

    def fake_boot(ui, tasks, quick=False, demo=None):
        for t in tasks:
            t.fn()
        return "go"
    monkeypatch.setattr(S, "boot", fake_boot)
    monkeypatch.setattr(S, "warp", lambda *a, **k: None)
    cli.cmd_demo(["--quick"])
    quick = sum(slept)
    slept.clear()
    cli.cmd_demo([])
    assert quick < sum(slept) * 0.5


def test_ctrl_o_without_a_browser_shows_the_link(monkeypatch):
    real_has_browser = S._has_browser
    monkeypatch.setattr(S, "_has_browser", lambda environ=None: True)
    monkeypatch.setattr(S.webbrowser, "open", lambda url: False)
    msg, _ = S.open_link("https://build.nvidia.com/x")
    assert msg.startswith("No browser here; open this link: https://build.nvidia.com/x")
    monkeypatch.setattr(S, "_has_browser", lambda environ=None: False)
    monkeypatch.setattr(S.webbrowser, "open", lambda url: pytest.fail("no browser to try"))
    assert S.open_link("https://e.example/")[0].startswith("No browser here")
    assert real_has_browser({"BROWSER": "firefox"}) is True
    if sys.platform.startswith("linux") and "microsoft" not in os.uname().release.lower():
        assert real_has_browser({}) is False                                   # SSH / console: just show it


def test_detection_badge_for_a_custom_endpoint():
    det = P.detect("http://127.0.0.1:18931/v1")
    note = S.detection_note(det, det.best)
    assert note.count("endpoint") == 0 and "OpenAI-compatible" in note and "127.0.0.1:18931" in note


def test_ascii_mode_masks_and_folds():
    shown = S.masked_live("sk-or-v1-" + "a" * 30 + "1234", u=False)
    assert "•" not in shown and "*" in shown and shown.endswith("1234")
    shown = S.masked_live("https://user:pw@h.example/v1 sk-" + "b" * 30, u=False)
    assert all(ord(c) < 128 for c in shown)
    cv = fx.Canvas(80, 3)
    S.header(cv, _UI(u=False), "MODEL", 0.0, right="5 models  ·  1 uplink")
    assert all(ord(c) < 128 for c in _row(cv, 0).replace(fx.MARK, "@"))


@pytest.mark.parametrize("env, want", [
    ({"LANG": "C"}, False), ({"LANG": "POSIX"}, False), ({"LC_ALL": "C", "LANG": "en_US.UTF-8"}, False),
    ({"LC_ALL": "en_US.UTF-8", "LANG": "C"}, True), ({"LC_CTYPE": "en_US.ISO-8859-1"}, False),
    ({"LANG": "C.UTF-8"}, True), ({"LANG": "en_US.utf8"}, True), ({}, True),
    ({"LANG": "C", "PULSAR_ASCII": "0"}, True),
])
def test_lang_c_means_ascii(env, want):
    from pulsar.term import detect_unicode
    assert detect_unicode(env, "utf-8", is_win=False) is want


def test_plain_picker_is_capped_labelled_and_filterable(monkeypatch):
    said = []

    class UI:
        u = False

        def say(self, text="", *a, **k):
            said.append(text)
    prov = P.custom_provider("http://127.0.0.1:18931/v1")
    src = S.Source("custom-127-0-0-1-18931", prov, "127.0.0.1:18931", "")
    src.result = C.CatalogResult([C.Model("m/model-%03d" % i, src.profile_id) for i in range(530)], "live")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    answers = iter(["model-42", "1"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    choice = S._picker_plain(UI(), [src], None, [], src.profile_id)
    rows = [t for t in said if re.match(r"^\s+\*?\s*\d+\. ", t or "")]
    assert len(rows) <= 30 + 30
    assert all("127.0.0.1:18931" in r and "custom-127" not in r for r in rows)
    assert any("more" in t for t in said)
    assert choice is not None and choice.model.startswith("m/model-42")


def test_plain_uplink_lists_one_url_per_line(monkeypatch):
    said = []

    class UI:
        u = True

        def say(self, text="", *a, **k):
            said.append(text)
    import getpass
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "")
    S._uplink_plain(UI(), [])
    assert all(t.count("https://") <= 1 for t in said)
    assert sum(1 for t in said if "https://" in t) >= 2


# ---------------------------------------------------------------------------
# 20-22. installer and uninstall
# ---------------------------------------------------------------------------

def test_uninstall_removes_a_shim_recorded_in_the_marker(monkeypatch, tmp_path, pulsar_home):
    st = K.State()
    st.upsert(K.Profile(id="groq", provider="groq", model="m"))
    K.save(st)
    (pulsar_home / "app").mkdir(exist_ok=True)
    (pulsar_home / "app" / "pulsarcode").write_text("#!/bin/sh\n")
    custom = tmp_path / "off-path-bin"
    custom.mkdir()
    os.symlink(str(pulsar_home / "app" / "pulsarcode"), str(custom / "pulsarcode"))
    marker = pulsar_home / K.MARKER
    marker.write_text(K.MARKER_TEXT + "created 2026-10-01T00:00:00Z\nbin %s\n" % custom)
    assert cli.marker_bin_dirs() == [str(custom)]
    home = tmp_path / "userhome"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))     # your home, as uninstall sees it
    for v in ("PULSAR_BIN_DIR", "PULSARCODE_BIN_DIR", "PULSAR_MANAGED", "ZDOTDIR", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(cli.L, "stop_core", lambda force=False: False)
    assert cli.main(["uninstall", "--yes"]) == 0
    assert not os.path.lexists(str(custom / "pulsarcode"))


def test_installer_records_bin_dir_and_names_deps_correctly():
    text = (ROOT / "install.sh").read_text()
    assert "fastapi" not in text
    assert "starlette, uvicorn, httpx" in text
    assert "record_bin_dir" in text and "printf 'bin %s\\n'" in text
    assert "no answer from github.com" in text and "(HTTP $FETCH_CODE)" not in text


def test_installer_fetch_why_reports_the_tool_exit(tmp_path):
    script = (ROOT / "install.sh").read_text()
    start = script.index("FETCH_CODE=\"000\"")
    end = script.index("sha256_of()")
    snippet = script[start:end]
    probe = tmp_path / "probe.sh"
    probe.write_text("need() { command -v \"$1\" >/dev/null 2>&1; }\nCA_FILE=\nLOG=%s/log\n%s\n"
                     "fetch https://127.0.0.1:9/nothing %s/out 2>>\"$LOG\"; echo \"$(fetch_why)\"\n"
                     % (tmp_path, snippet, tmp_path))
    out = subprocess.run(["bash", str(probe)], capture_output=True, text=True, timeout=120,
                         env={"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~")}).stdout
    assert "no answer from github.com" in out and "exit" in out and "HTTP 000" not in out


def test_lang_c_survives_pythons_locale_coercion():
    from pulsar.term import detect_unicode
    # what os.environ looks like inside Python started with LANG=C (PEP 538 adds LC_CTYPE=C.UTF-8)
    assert detect_unicode({"LANG": "C", "LC_CTYPE": "C.UTF-8"}, "utf-8", is_win=False) is False
    assert detect_unicode({"LANG": "en_US.UTF-8", "LC_CTYPE": "C.UTF-8"}, "utf-8", is_win=False) is True
