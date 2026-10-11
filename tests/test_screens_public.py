# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Public lists and demo labelling: what the screens advertise, and how demo mode says so."""
import io
import random
import re
import sys
from pathlib import Path

from pulsar import providers as P
from pulsar import screens as S

ROOT = Path(__file__).resolve().parents[1]
DOT = chr(0xB7)


def text_of(cv):
    return "\n".join("".join(ch for ch, _, _ in row) for row in cv.cells)


def test_claude_subscription_is_unadvertised_but_still_works():
    names = [p.name for p in S.advertised_providers()]
    assert "Claude subscription" not in names
    assert len(names) == len(P.PROVIDERS) - len(S.UNADVERTISED)
    # the profile and its detection stay in the code for people who already use it
    assert "claude" in P.BY_ID
    det = P.detect("sk-ant-oat01-fake-key-not-real")
    assert det.best is not None and det.best.id == "claude"


def test_uplink_works_with_list_uses_advertised_providers():
    src = (ROOT / "pulsar" / "screens.py").read_text(encoding="utf-8")
    assert "works = advertised_providers()" in src


def test_site_lists_and_counts_match_the_advertised_providers():
    html = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    n = len(S.advertised_providers())
    assert "Claude subscription" not in html
    assert len(re.findall(r'class="pn"', html)) == n
    counts = [int(c) for c in re.findall(r'<span class="count">(\d+)</span>', html)]
    assert sum(counts) == n
    for m in re.findall(r"(\d+) providers", html):
        assert int(m) == n


def test_social_card_count_is_computed():
    sys.path.insert(0, str(ROOT / "tools" / "brand"))
    try:
        import make_assets as M
        html = M.social_html()              # imports capture from tools/brand too, so still on sys.path
    finally:
        sys.path.pop(0)
    n = len(S.advertised_providers())
    assert "%d providers" % n in html
    assert "<span>+%d</span>" % (n - len(M.SOCIAL_CHIPS)) in html


def _demo_tasks():
    out = []
    for label, detail in (("Uplink", "demo"), ("Model catalog", "312 models " + DOT + " live")):
        t = S.Task(label, lambda d=detail: ("ok", d))
        t.status, t.detail = t.fn()
        out.append(t)
    return out


def test_demo_detail_never_claims_live():
    assert S.demo_detail("312 models " + DOT + " live") == "312 models " + DOT + " demo"
    assert S.demo_detail("delivery") == "delivery"
    assert S.demo_detail("") == ""


def test_demo_boot_frame_is_labelled():
    for w, h in ((100, 30), (60, 18), (44, 13)):
        b = S._Boot(_demo_tasks(), True, True, random.Random(1), "minimal", calm=True, demo=True)
        cv = b.frame(w, h, 0.0)
        cv = b.frame(w, h, 1.0)
        txt = text_of(cv)
        assert S.DEMO_TAG in txt, (w, h)
        assert "live" not in txt, (w, h)
    b = S._Boot(_demo_tasks(), True, True, random.Random(1), "minimal", calm=True)
    assert S.DEMO_TAG not in text_of(b.frame(100, 30, 1.0))


class _PlainCaps:
    def __init__(self):
        self.out = io.StringIO()
        self.tty, self.color, self.unicode, self.motion = False, 0, False, False
        self.reduced, self.fx, self.ci = False, "auto", False
        self.size = (80, 24)


def test_plain_demo_boot_says_demo_mode():
    caps = _PlainCaps()
    ui = S.UI(caps)
    assert S.boot(ui, _demo_tasks(), quick=True, demo=True) == "go"
    out = caps.out.getvalue()
    assert S.DEMO_TAG in out and "live" not in out and all(ord(c) < 128 for c in out)


def test_demo_is_detected_from_the_command_line(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["pulsar", "demo", "--quick"])
    assert S._demo_invocation()
    monkeypatch.setattr(sys, "argv", ["pulsar", "--", "demo"])
    assert not S._demo_invocation()
    monkeypatch.setattr(sys, "argv", ["pulsar"])
    assert not S._demo_invocation()


def test_key_chooser_leaves_out_unadvertised_providers():
    det = P.detect("fakekeynotreal1234567890")
    assert not det.certain
    # the unfiltered keyed list (what the chooser used to append) still holds the unadvertised route
    assert any(p.id in S.UNADVERTISED for p in P.PROVIDERS if p.needs_key)
    for everything in (False, True):
        ids = [p.id for p in S.key_choices(det, everything=everything)]
        assert ids and not (set(ids) & S.UNADVERTISED)
    full = S.key_choices(det, everything=True)
    assert len(full) == len(set(p.id for p in full))
    assert set(p.id for p in full) == set(p.id for p in S.advertised_providers() if p.needs_key)


def test_uplink_chooser_and_plain_list_use_key_choices(monkeypatch):
    src = (ROOT / "pulsar" / "screens.py").read_text(encoding="utf-8")
    assert "choices = key_choices(det, everything=True)" in src
    assert "options = key_choices(det)" in src
    # drive the plain uplink with an unrecognised key and capture the numbered list it prints
    said = []

    class FakeUI:
        def say(self, text="", *a, **k):
            said.append(text)

    import getpass
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "fakekeynotreal1234567890")

    def no_choice(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", no_choice)
    assert S._uplink_plain(FakeUI(), []) is None
    listed = [t for t in said if re.match(r"^\s+\d+\. ", t or "")]
    assert listed
    joined = "\n".join(listed)
    for p in P.PROVIDERS:
        if p.id in S.UNADVERTISED:
            assert p.name not in joined
