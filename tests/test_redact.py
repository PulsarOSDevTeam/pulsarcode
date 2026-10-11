# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""pulsar.redact: keys of any length, fragments from anywhere in the key, escaped forms, never raising.
Every key here is an obvious fake."""

import random
import string
import time
from urllib.parse import quote

import pytest

from pulsar import redact as RD
from pulsar.core.engine import _short
from pulsar.redact import mask, redact

FAKE = "fake-key-not-real-0123456789"
SLASHED = "fake/key+not/real-0123456789"


def fake_key(n, seed=1):
    """An obviously fake key of exactly `n` characters, JWT-like and not periodic."""
    rng = random.Random(seed)
    head = "eyJfake-not-real-"
    return (head + "".join(rng.choice(string.ascii_letters + string.digits + "-_.") for _ in range(n)))[:n]


def esc_u(s):
    return "".join("\\u%04x" % ord(c) for c in s)


def esc_pct(s):
    return "".join("%%%02X" % ord(c) for c in s)


@pytest.mark.parametrize("n", [64, 300, 985, 1000, 1200, 2000, 4000])
def test_keys_of_any_length_are_masked_without_error(n):
    key = fake_key(n)
    RD._patterns.cache_clear()
    for piece in (key, key[: n // 2 + 5], key[-(n // 2 + 5):], key[3:-3]):
        text = "upstream said: invalid token " + piece + " and some more text" * 20
        started = time.time()
        out = redact(text, [key])
        assert time.time() - started < 2.0
        assert piece not in out and mask(key) in out and out.startswith("upstream said: invalid token ")


def test_periodic_jwt_style_key_is_redacted():
    key = "eyJfake-not-real-" + "A1b2C3d4" * 148            # 1201 characters, as in repro_long_key.sh
    out = redact('{"error": {"message": "invalid x-api-key %s"}}' % key, [key])
    assert key not in out and "A1b2C3d4A1b2C3d4" not in out and mask(key) in out


def test_middle_fragments_are_masked_wherever_they_sit():
    for frag in (FAKE[:-3], FAKE[3:], FAKE[2:-2], FAKE[4:24], FAKE[7:21]):
        out = _short('{"error": {"message": "Invalid API key: %s"}}' % frag, secrets=[FAKE])
        assert frag not in out and out == "Invalid API key: " + mask(FAKE)
    assert redact("short fake-key-n here", [FAKE]) == "short fake-key-n here"    # 10 chars: below the threshold
    assert redact("x " + FAKE[4:17] + " y", [FAKE]) == "x " + FAKE[4:17] + " y"   # 13 < max(12, 28 // 2)


@pytest.mark.parametrize("escape", [esc_u, esc_pct, lambda s: s.replace("/", "\\/"), lambda s: s])
def test_escaped_whole_prefix_suffix_and_middle_fragments(escape):
    for frag in (SLASHED, SLASHED[:16], SLASHED[-16:], SLASHED[3:-3], SLASHED[6:22]):
        out = redact("before " + escape(frag) + " after", [SLASHED])
        assert out == "before " + mask(SLASHED) + " after", (frag, out)


def test_mixed_escapes_and_two_copies():
    mixed = "".join(("\\u%04x" % ord(c)) if i % 3 == 0 else c for i, c in enumerate(FAKE))
    assert redact("bad " + mixed, [FAKE]) == "bad " + mask(FAKE)
    assert redact(FAKE + " and " + FAKE, [FAKE]) == mask(FAKE) + " and " + mask(FAKE)
    assert redact("ab%c3cd", [FAKE]) == "ab%c3cd"                    # unrelated escapes are left alone


def test_short_secrets_only_whole():
    assert redact("abc123 and abc12", ["abc123"]) == mask("abc123") + " and abc12"
    assert redact("abc12 x", ["abc12"]) == "abc12 x"                 # under 6 characters: not searched


def test_matcher_agrees_with_brute_force():
    rng = random.Random(7)
    for _ in range(800):
        alpha = rng.choice(["ab", "abc", "A1b2", "abcdef0123"])
        secret = "".join(rng.choice(alpha) for _ in range(rng.randint(6, 36)))
        least = RD.least_fragment(secret)
        pieces = []
        for _ in range(rng.randint(1, 5)):
            if rng.random() < 0.6:
                i = rng.randint(0, len(secret) - 1)
                pieces.append(secret[i:rng.randint(i + 1, len(secret))])
            else:
                pieces.append("".join(rng.choice(alpha + " xyz") for _ in range(rng.randint(0, 10))))
        text = "".join(pieces)
        want = [False] * len(text)
        for a in range(len(text)):
            for b in range(a + least, len(text) + 1):
                if text[a:b] not in secret:
                    break
                for k in range(a, b):
                    want[k] = True
        got = [False] * len(text)
        for a, b in RD._Matcher(secret).spans(text, least):
            for k in range(a, b):
                got[k] = True
        assert got == want, (secret, text)


def test_redact_never_raises(monkeypatch):
    def boom(secret):
        raise RecursionError("maximum recursion depth exceeded")
    monkeypatch.setattr(RD, "_matcher", boom)
    assert redact("Invalid API key: " + FAKE, [FAKE]) == "Invalid API key: " + mask(FAKE)
    assert redact("u=" + quote(SLASHED, safe=""), [SLASHED]) == "u=" + mask(SLASHED)       # percent-encoded form
    assert redact("x \ud800abcdefgh y", ["\ud800abcdefgh"]) == "x " + mask("\ud800abcdefgh") + " y"
    assert redact("", [FAKE]) == "" and redact(None, [FAKE]) == ""
    assert redact("plain", [None, "", 42, FAKE]) == "plain"
