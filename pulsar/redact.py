# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Scrub secrets from any text that leaves Pulsar (notices, events, council answers, errors).

Upstreams sometimes echo the key back ("Invalid API key provided: sk-...").
That text would otherwise land in Claude Code's transcript, be sent to the next
provider in a relay chain, and stream out on the event bus.

A configured secret is found with a suffix automaton of the secret, scanned over the text once as written
and once with JSON \\uXXXX, "\\/" and %XX escapes decoded. That finds the whole secret and any long
fragment of it (prefix, suffix or middle) in time linear in the text, with no regular expression whose
size or nesting grows with the secret, so a secret of any length (a multi-kilobyte JWT) is fine.
"""

from __future__ import annotations

import functools
import re
from typing import Dict, Iterable, List, Sequence, Tuple
from urllib.parse import quote

# Well-known key shapes, kept loose: a false positive only masks a word. The registry's underscore prefixes
# (Novita sk_, Vercel vck_, Venice vapi_, Chutes cpk_, Cerebras csk_, Together tgp_) only count at the start
# of a word, so "task_..." or "disk_..." in an error stays readable. Opaque keys with no prefix (32 or 64 hex,
# 40 alphanumerics) cannot be told from other text: those are masked only where the key itself is known.
_SHAPES = re.compile(
    r"(sk-ant-[A-Za-z0-9_\-]{8,}|sk-(?:proj-|svcacct-|admin-|or-v1-|or-)?[A-Za-z0-9_\-]{12,}|nvapi-[A-Za-z0-9_\-]{8,}|"
    r"gsk_[A-Za-z0-9]{8,}|xai-[A-Za-z0-9]{8,}|AIza[0-9A-Za-z_\-]{20,}|AQ\.[A-Za-z0-9_\-]{20,}|hf_[A-Za-z0-9]{12,}|"
    r"csk-[A-Za-z0-9]{8,}|fw_[A-Za-z0-9]{8,}|tgp_v1_[A-Za-z0-9_\-]{8,}|pplx-[A-Za-z0-9]{8,}|"
    r"ABSK[A-Za-z0-9+/=]{16,}|bedrock-api-key-[A-Za-z0-9+/=_\-]{8,}|"
    r"(?<![A-Za-z0-9])(?:sk|vck|vapi|cpk|csk|tgp)_[A-Za-z0-9_.\-]{12,}|"
    r"(?<![A-Za-z0-9])[0-9a-f]{32}\.[A-Za-z0-9]{10,24}(?![A-Za-z0-9])|ya29\.[A-Za-z0-9_\-]{20,}|"
    r"pl[sav]_[A-Za-z0-9_\-]{8,})")
_BEARER = re.compile(r"(?i)\b(bearer|api[-_]?key|x-api-key|authorization)([\"'\s:=]+)([A-Za-z0-9._~+/=\-]{12,})")
# One escaped character as upstreams write it: a JSON \uXXXX escape, JSON's optional "\/", URL percent-encoding.
_ESCAPE = re.compile(r"\\u[0-9a-fA-F]{4}|\\/|%[0-9a-fA-F]{2}")
_MIN_SECRET = 6         # shorter secrets are not searched for (they would mask ordinary words)
_FRAGMENTS_FROM = 16    # secrets this long also have their long fragments masked


def mask(secret: str) -> str:
    s = secret or ""
    if len(s) <= 8:
        return "•" * len(s)
    return s[:4] + "…" + s[-4:]


def least_fragment(secret: str) -> int:
    """The shortest piece of `secret` that is masked: the whole secret below 16 characters, else
    max(12, len // 2) characters from anywhere in it."""
    n = len(secret or "")
    return n if n < _FRAGMENTS_FROM else max(12, n // 2)


class _Matcher:
    """Suffix automaton of one secret: every substring of the secret is a path from the root.

    Built and scanned iteratively (no recursion, no regular expression), in time linear in the secret and
    in the text."""

    __slots__ = ("nxt", "link", "length")

    def __init__(self, secret: str) -> None:
        nxt: List[Dict[str, int]] = [{}]
        link: List[int] = [-1]
        length: List[int] = [0]
        last = 0
        for c in secret:
            cur = len(nxt)
            nxt.append({})
            link.append(0)
            length.append(length[last] + 1)
            p = last
            while p != -1 and c not in nxt[p]:
                nxt[p][c] = cur
                p = link[p]
            if p != -1:
                q = nxt[p][c]
                if length[p] + 1 == length[q]:
                    link[cur] = q
                else:
                    clone = len(nxt)
                    nxt.append(dict(nxt[q]))
                    link.append(link[q])
                    length.append(length[p] + 1)
                    while p != -1 and nxt[p].get(c) == q:
                        nxt[p][c] = clone
                        p = link[p]
                    link[q] = clone
                    link[cur] = clone
            last = cur
        self.nxt = nxt
        self.link = link
        self.length = length

    def spans(self, text: Sequence[str], least: int) -> List[Tuple[int, int]]:
        """[start, end) index ranges of `text` covered by substrings of the secret at least `least` long.

        For every end position the longest substring of the secret ending there is tracked (matching
        statistics), so any such fragment, wherever it sits in the secret, is inside one of the ranges."""
        nxt, link, length = self.nxt, self.link, self.length
        out: List[Tuple[int, int]] = []
        v = run = 0
        for i, c in enumerate(text):
            while v and c not in nxt[v]:
                v = link[v]
                run = length[v]
            step = nxt[v].get(c)
            if step is None:
                v = run = 0
                continue
            v = step
            run += 1
            if run >= least:
                a = i + 1 - run
                if out and a < out[-1][1]:
                    out[-1] = (out[-1][0], i + 1)
                else:
                    out.append((a, i + 1))
        return out


@functools.lru_cache(maxsize=32)
def _matcher(secret: str) -> _Matcher:
    return _Matcher(secret)


_patterns = _matcher    # the former name; callers use _patterns.cache_clear()


def _decoded(text: str) -> Tuple[List[str], List[int]]:
    """`text` with each escaped character decoded, and where each decoded character starts in `text`
    (one extra entry: the end)."""
    chars: List[str] = []
    starts: List[int] = []
    pos = 0
    for m in _ESCAPE.finditer(text):
        for k in range(pos, m.start()):
            chars.append(text[k])
            starts.append(k)
        tok = m.group(0)
        if tok[1] == "u":
            chars.append(chr(int(tok[2:], 16)))
        elif tok[0] == "%":
            chars.append(chr(int(tok[1:], 16)))
        else:
            chars.append("/")
        starts.append(m.start())
        pos = m.end()
    for k in range(pos, len(text)):
        chars.append(text[k])
        starts.append(k)
    starts.append(len(text))
    return chars, starts


def _union(spans: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    out: List[Tuple[int, int]] = []
    for a, b in sorted(spans):
        if out and a < out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _mask_secret(text: str, secret: str) -> str:
    least = least_fragment(secret)
    if len(text) < least:
        return text
    m = _matcher(secret)
    found = list(m.spans(text, least))                       # as written
    if _ESCAPE.search(text):                                 # and with escapes decoded
        chars, starts = _decoded(text)
        found += [(starts[a], starts[b]) for a, b in m.spans(chars, least)]
    if not found:
        return text
    hidden = mask(secret)
    parts: List[str] = []
    pos = 0
    for a, b in _union(found):
        parts.append(text[pos:a])
        parts.append(hidden)
        pos = b
    parts.append(text[pos:])
    return "".join(parts)


def _plain(text: str, secrets: Sequence[str]) -> str:
    """The fail-safe path: exact and percent-encoded forms of each secret, by plain replacement."""
    out = text
    for s in secrets:
        forms = {s}
        try:
            forms |= {quote(s, safe=""), quote(s)}
        except Exception:       # a secret that is not valid UTF-8 has no percent-encoded form
            pass
        for form in sorted(forms, key=len, reverse=True):
            out = out.replace(form, mask(s))
    return out


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """`text` with every given secret, every Bearer/api-key value and every known key shape masked.

    A given secret is masked whatever escaping the upstream used (JSON \\uXXXX, "\\/", percent-encoding),
    and for secrets of 16 or more characters any echoed fragment of at least max(12, len // 2) characters
    is masked too, wherever it sits in the secret. Never raises: if masking a secret fails, its plain and
    percent-encoded forms are still replaced.
    """
    if not text:
        return text or ""
    out = str(text)
    wanted = [s for s in (secrets or ()) if isinstance(s, str) and len(s) >= _MIN_SECRET]
    out = _plain(out, wanted)
    for s in wanted:
        try:
            out = _mask_secret(out, s)
        except Exception:       # RecursionError, MemoryError...: never let redaction fail a request
            pass
    try:
        out = _BEARER.sub(lambda m: m.group(1) + m.group(2) + mask(m.group(3)), out)
        return _SHAPES.sub(lambda m: mask(m.group(0)), out)
    except Exception:
        return out
