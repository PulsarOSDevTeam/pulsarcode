#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc. / Collapse Technologies Inc.
"""Interactive arrow-key picker over the PulsarOS API Sonar catalog.

The picker groups NVIDIA NIM models into operator-facing tiers
(LOCAL_PULSAR, CODING, GENERAL, LIGHTWEIGHT, OTHER), renders a banner-style
TUI matching the pulsarcode design register, and persists the operator's
selection to ~/.pulsarcode/active_model so subsequent launches inherit it.

This module is the Rung 1 core for the first-launch wizard. It is also
invoked by the `pulsarcode pick` subcommand and by the `/model` Claude Code
custom slash command (Rung 2) when the operator wants to switch live.

Doctrine:
  - PulsarOS Pillar 14 (Omnipresent API Sonar) and Pillar 15 (NIM as
    dumb-compute payload), with the operator never exposed to backend
    model id syntax. Aliases only.
  - CLAUDE.md RL-3: zero em dashes in any user-facing string.
  - CLAUDE.md RL-11: zero internal terminology in user-facing strings.
  - feedback_decide_dont_interview.md: decide, never prompt the operator
    with a question the catalog already answers.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

try:
    from proxy.nim_api_sonar import (
        DEFAULT_API_BASE,
        DEFAULT_MODEL,
        DEFAULT_PUBLIC_MODEL,
        NIMModelRecord,
        runtime_catalog,
    )
except ImportError:
    # Allow execution as `python proxy/nim_sonar_picker.py` from the project root.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from proxy.nim_api_sonar import (
        DEFAULT_API_BASE,
        DEFAULT_MODEL,
        DEFAULT_PUBLIC_MODEL,
        NIMModelRecord,
        runtime_catalog,
    )


# ---------------------------------------------------------------------------
# Tier classification (derived from upstream model id; see derivation notes).
# ---------------------------------------------------------------------------

TIER_LOCAL_PULSAR = "LOCAL_PULSAR"
TIER_CODING = "CODING"
TIER_GENERAL = "GENERAL"
TIER_LIGHTWEIGHT = "LIGHTWEIGHT"
TIER_OTHER = "OTHER"

TIER_ORDER: Tuple[str, ...] = (
    TIER_LOCAL_PULSAR,
    TIER_CODING,
    TIER_GENERAL,
    TIER_LIGHTWEIGHT,
    TIER_OTHER,
)

TIER_HEADERS: Dict[str, str] = {
    TIER_LOCAL_PULSAR: "LOCAL PULSAR  sovereign, airplane mode, your patents",
    TIER_CODING: "CODING TIER  largest coding-tuned routes",
    TIER_GENERAL: "GENERAL TIER  mid to large general-purpose routes",
    TIER_LIGHTWEIGHT: "LIGHTWEIGHT TIER  small, fast, low-cost",
    TIER_OTHER: "OTHER  uncategorized public NIM routes",
}

_CODING_HINTS = (
    "kimi-k2",
    "coder",
    "deepseek-v4",
    "gpt-oss-120b",
    "qwen3-coder",
)
_LIGHTWEIGHT_HINTS = (
    "nano",
    "mini",
    "small",
    "phi-4",
    "gemma-2-2b",
    "llama-3.2-1b",
    "llama-3.2-3b",
    "llama-3.1-8b",
    "magistral-small",
    "mistral-7b",
    "codegemma",
    "gliner",
    "nemoguard",
)
_GENERAL_HINTS = (
    "glm",
    "minimax",
    "magistral",
    "llama-3.3",
    "mixtral",
    "gemma-7b",
    "stockmark",
    "nemotron",
    "qwen3-next",
    "qwen3-5",
    "sarvam",
    "step-3-5",
    "solar",
    "llama2-70b",
    "llama-3.1-70b",
    "llama-3.1-nemotron",
    "mistral-nemotron",
    "seed-oss",
    "dracarys",
)


import re as _re


def _hint_in(model: str, hint: str) -> bool:
    """Letter-boundary substring match. Hint must not be preceded or followed
    by another letter. Digits and dots are permitted on either side because
    they are model-version markers (e.g. 'glm' matches 'glm5.1' but 'mini'
    does NOT match 'minimax'). Avoids the 2026-04-17 P1b-class bug where
    substring overlap caused a destructive misclassification."""
    pattern = r"(?<![a-z])" + _re.escape(hint) + r"(?![a-z])"
    return _re.search(pattern, model) is not None


def tier_of(record: NIMModelRecord) -> str:
    upstream = record.upstream_id.lower()
    model = record.model.lower()
    if upstream.startswith("local-pulsar/"):
        return TIER_LOCAL_PULSAR
    for hint in _CODING_HINTS:
        if _hint_in(model, hint):
            return TIER_CODING
    for hint in _LIGHTWEIGHT_HINTS:
        if _hint_in(model, hint):
            return TIER_LIGHTWEIGHT
    for hint in _GENERAL_HINTS:
        if _hint_in(model, hint):
            return TIER_GENERAL
    return TIER_OTHER


# Coding-tier intra-tier order. Lower = earlier.
_CODING_ORDER_HINTS: Tuple[Tuple[str, int], ...] = (
    ("kimi-k2.6", 0),
    ("kimi-k2-thinking", 1),
    ("kimi-k2-instruct", 2),
    ("qwen3-coder-480b", 3),
    ("qwen2.5-coder-32b", 4),
    ("qwen2.5-coder-7b", 5),
    ("deepseek-v4-pro", 6),
    ("deepseek-v4-flash", 7),
    ("gpt-oss-120b", 8),
)


def _within_tier_sort_key(record: NIMModelRecord) -> Tuple[int, str]:
    model = record.model.lower()
    for hint, score in _CODING_ORDER_HINTS:
        if hint in model:
            return (score, model)
    return (1_000, model)


def group_by_tier(records: Sequence[NIMModelRecord]) -> Dict[str, List[NIMModelRecord]]:
    groups: Dict[str, List[NIMModelRecord]] = {tier: [] for tier in TIER_ORDER}
    for record in records:
        groups[tier_of(record)].append(record)
    for tier, items in groups.items():
        items.sort(key=_within_tier_sort_key)
    return groups


# ---------------------------------------------------------------------------
# Persistence: ~/.pulsarcode/active_model holds the chosen alias only.
# ---------------------------------------------------------------------------

DEFAULT_PULSAR_HOME = Path(os.path.expanduser(os.environ.get("PULSAR_HOME", "~/.pulsarcode")))


def active_model_path(pulsar_home: Path = DEFAULT_PULSAR_HOME) -> Path:
    return pulsar_home / "active_model"


def read_active_model(pulsar_home: Path = DEFAULT_PULSAR_HOME) -> Optional[str]:
    path = active_model_path(pulsar_home)
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def write_active_model(alias: str, pulsar_home: Path = DEFAULT_PULSAR_HOME) -> None:
    pulsar_home.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(pulsar_home, 0o700)
    except OSError:
        pass
    path = active_model_path(pulsar_home)
    path.write_text(alias.strip() + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# ANSI primitives. Gated by NO_COLOR env var and TTY check.
# ---------------------------------------------------------------------------

def _ansi_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stderr.isatty():
        return False
    return True


_EMBER = "\x1b[38;5;208m"  # PulsarOS ember accent
_DIM = "\x1b[38;5;240m"
_GREEN = "\x1b[38;5;46m"
_RESET = "\x1b[0m"
_BOLD = "\x1b[1m"
_HIDE_CURSOR = "\x1b[?25l"
_SHOW_CURSOR = "\x1b[?25h"


def _paint(text: str, color: str) -> str:
    if not _ansi_enabled():
        return text
    return color + text + _RESET


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Row:
    """A flat-list row in the TUI. Either a tier header or a model."""
    kind: str           # "header" or "model"
    label: str          # display text for header rows
    record: Optional[NIMModelRecord] = None


def build_rows(groups: Dict[str, List[NIMModelRecord]]) -> List[Row]:
    rows: List[Row] = []
    for tier in TIER_ORDER:
        items = groups.get(tier, [])
        if not items:
            continue
        rows.append(Row(kind="header", label=TIER_HEADERS[tier]))
        for record in items:
            rows.append(Row(kind="model", label=record.alias, record=record))
    return rows


def first_selectable(rows: Sequence[Row]) -> int:
    for index, row in enumerate(rows):
        if row.kind == "model":
            return index
    return 0


def find_alias(rows: Sequence[Row], alias: Optional[str]) -> Optional[int]:
    if alias is None:
        return None
    for index, row in enumerate(rows):
        if row.kind == "model" and row.record is not None and row.record.alias == alias:
            return index
    return None


def _describe(record: NIMModelRecord) -> str:
    bits: List[str] = []
    if record.context_tokens:
        bits.append(f"ctx={record.context_tokens // 1000}K")
    if "default" in record.tags:
        bits.append("default")
    if "authorized" == record.confidence:
        bits.append("key-visible")
    elif record.confidence == "public":
        bits.append("public-source")
    return "  ".join(bits)


def _ascii_top() -> str:
    return "+" + "-" * 76 + "+"


# Default viewport size for the windowed picker. Overridable via the
# PULSAR_PICKER_VIEWPORT env var. Smaller terminals can set it to 5; larger
# screens can set it to 15. The number includes both header rows and model
# rows in the visible slice.
DEFAULT_VIEWPORT = int(os.environ.get("PULSAR_PICKER_VIEWPORT", "10"))


def _render_row(row: Row, is_cursor: bool, is_current: bool) -> str:
    if row.kind == "header":
        return "  " + _paint(row.label, _BOLD)
    marker_cursor = ">" if is_cursor else " "
    marker_state = "[x]" if is_current else "[ ]"
    alias_text = row.label
    upstream = row.record.upstream_id if row.record else ""
    meta = _describe(row.record) if row.record else ""
    line = f"  {marker_cursor} {marker_state}  {alias_text:36}  {upstream:50}  {meta}"
    if is_cursor:
        return _paint(line, _EMBER)
    if is_current:
        return _paint(line, _GREEN)
    return line


def render(
    rows: Sequence[Row],
    cursor: int,
    current_alias: Optional[str],
    window_top: Optional[int] = None,
    viewport: Optional[int] = None,
) -> List[str]:
    """Render the picker. If window_top + viewport are given, render only the
    visible slice; otherwise render the whole list (used by non-TTY fallback)."""
    lines: List[str] = []
    lines.append("")
    lines.append(_paint(_ascii_top(), _EMBER))
    title = "| Local Pulsar  /  API Sonar  /  pick a model"
    pad = 78 - len(title) - 1
    lines.append(_paint(title + (" " * pad) + "|", _EMBER))
    lines.append(_paint(_ascii_top(), _EMBER))
    lines.append("")
    lines.append(_paint("  arrows up / down to move,  enter to select,  esc to keep current", _DIM))
    lines.append("")

    if window_top is None or viewport is None:
        # Render every row (legacy / non-TTY mode).
        start, end = 0, len(rows)
    else:
        start = max(0, min(window_top, len(rows)))
        end = min(start + viewport, len(rows))

    if start > 0:
        lines.append("  " + _paint(f"({start} more above)", _DIM))

    for index in range(start, end):
        row = rows[index]
        is_cursor = index == cursor
        is_current = row.kind == "model" and row.record is not None and row.record.alias == current_alias
        lines.append(_render_row(row, is_cursor, is_current))

    if end < len(rows):
        lines.append("  " + _paint(f"({len(rows) - end} more below)", _DIM))

    lines.append("")
    lines.append(_paint(_ascii_top(), _EMBER))
    return lines


# ---------------------------------------------------------------------------
# Raw-key input loop (Unix). No third-party deps.
# ---------------------------------------------------------------------------

def _tty_available() -> bool:
    """True iff raw-TTY arrow-key input is supported on this platform."""
    if not sys.stdin.isatty():
        return False
    try:
        import termios  # noqa: F401
        import tty  # noqa: F401
    except ImportError:
        return False
    return True


class _RawTTY:
    """Context manager that puts stdin into cbreak mode (Unix only)."""

    def __init__(self) -> None:
        self._fd: Optional[int] = None
        self._saved = None

    def __enter__(self) -> "_RawTTY":
        if not sys.stdin.isatty():
            raise RuntimeError("stdin is not a tty")
        import termios
        import tty
        self._fd = sys.stdin.fileno()
        self._saved = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fd is None or self._saved is None:
            return
        import termios
        termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)


def _read_key() -> str:
    """Return a token like 'UP', 'DOWN', 'ENTER', 'ESC', 'q', 'CTRL-C'.

    Notes on the escape-sequence handling: terminals deliver arrow keys as a
    three-byte CSI sequence (ESC '[' direction). On the FIRST keypress after
    entering cbreak mode, the bytes can arrive in two chunks several tens of
    milliseconds apart on some terminals (slow TTY paths, focus-change
    delivery, GPU-accelerated terminals on macOS). A short inter-byte timeout
    caused the bug where a first arrow press was treated as plain ESC and
    cancelled the picker. We wait up to 200ms between bytes, which is
    imperceptible to a human pressing ESC but plenty of time for any CSI
    sequence to fully arrive.
    """
    ch = sys.stdin.read(1)
    if ch == "\x1b":
        import select
        # Wait up to 200ms for the next byte of a possible CSI sequence.
        ready, _, _ = select.select([sys.stdin], [], [], 0.20)
        if not ready:
            return "ESC"
        ch2 = sys.stdin.read(1)
        if ch2 != "[":
            return "ESC"
        # And up to 200ms for the third byte.
        ready, _, _ = select.select([sys.stdin], [], [], 0.20)
        if not ready:
            return "ESC"
        ch3 = sys.stdin.read(1)
        if ch3 == "A":
            return "UP"
        if ch3 == "B":
            return "DOWN"
        if ch3 == "C":
            return "RIGHT"
        if ch3 == "D":
            return "LEFT"
        # Some terminals send numeric CSI sequences (e.g., for Page Up / Down,
        # Home, End). Drain trailing bytes up to the terminator '~' so the
        # next read does not see garbage.
        if ch3.isdigit():
            for _ in range(8):
                ready, _, _ = select.select([sys.stdin], [], [], 0.05)
                if not ready:
                    break
                terminator = sys.stdin.read(1)
                if terminator == "~":
                    break
            return "ESC"
        return "ESC"
    if ch in ("\r", "\n"):
        return "ENTER"
    if ch == "\x03":
        return "CTRL-C"
    if ch == "\t":
        return "TAB"
    return ch


# ---------------------------------------------------------------------------
# The picker entry point.
# ---------------------------------------------------------------------------

def _pick_numbered_fallback(
    rows: Sequence[Row],
    current_alias: Optional[str],
    out: "object" = sys.stderr,
) -> Optional[str]:
    """Numbered-list fallback for platforms without raw TTY (Windows, etc.).

    Same persistence semantics as pick_model: on selection, writes the alias
    to ~/.pulsarcode/active_model and returns it.
    """
    write = lambda text: out.write(text + "\n")  # noqa: E731
    model_rows: List[Tuple[int, Row]] = [
        (number, row) for number, row in enumerate(
            (r for r in rows if r.kind == "model"), start=1
        )
    ]
    # Re-render with numbers and tier headers, but skip the cursor highlight.
    write("")
    write(_paint(_ascii_top(), _EMBER))
    title = "| Local Pulsar  /  API Sonar  /  numbered picker (no arrow keys here)"
    pad = 78 - len(title) - 1
    write(_paint(title + (" " * pad) + "|", _EMBER))
    write(_paint(_ascii_top(), _EMBER))
    write("")
    write(_paint("  Type the number of the model you want, then Enter. Empty input keeps current.", _DIM))
    write("")
    current_tier: Optional[str] = None
    for number, row in model_rows:
        if row.record is None:
            continue
        tier = tier_of(row.record)
        if tier != current_tier:
            current_tier = tier
            write("")
            write("  " + _paint(TIER_HEADERS[tier], _BOLD))
            write("  " + _paint("-" * min(len(TIER_HEADERS[tier]), 70), _DIM))
        marker = "[x]" if row.record.alias == current_alias else "[ ]"
        meta = _describe(row.record)
        line = f"  {number:3d}.  {marker}  {row.record.alias:36}  {row.record.upstream_id:50}  {meta}"
        if row.record.alias == current_alias:
            line = _paint(line, _GREEN)
        write(line)
    write("")
    write(_paint(_ascii_top(), _EMBER))
    write("")
    try:
        raw = input("  Pick number (or Enter to keep current): ").strip()
    except (EOFError, KeyboardInterrupt):
        write("")
        return None
    if not raw:
        return None
    try:
        choice = int(raw)
    except ValueError:
        write(_paint("  Local Pulsar: not a number, keeping current.", _DIM))
        return None
    if choice < 1 or choice > len(model_rows):
        write(_paint(f"  Local Pulsar: number out of range (1 to {len(model_rows)}), keeping current.", _DIM))
        return None
    record = model_rows[choice - 1][1].record
    if record is None:
        return None
    write_active_model(record.alias)
    write("")
    write(_paint(f"  Local Pulsar: active model set to {record.alias}.", _GREEN))
    return record.alias


def pick_model(
    records: Sequence[NIMModelRecord],
    current_alias: Optional[str],
    out: "object" = sys.stderr,
) -> Optional[str]:
    """Run the interactive picker. Return the selected alias, or None on cancel.

    Side effect: on selection, persist the alias to ~/.pulsarcode/active_model.
    Falls back to numbered-list input on platforms without raw TTY support
    (Windows without WSL, CI, or any stdin without termios).
    """
    write = lambda text: out.write(text + "\n")  # noqa: E731

    if not records:
        write("Local Pulsar: empty NIM catalog. Run `pulsarcode /api` to set your key.")
        return None

    groups = group_by_tier(records)
    rows = build_rows(groups)
    if not any(row.kind == "model" for row in rows):
        write("Local Pulsar: no model rows in catalog after grouping.")
        return None

    if not sys.stdin.isatty():
        # Non-interactive shell (CI, piped runs): dump grouped catalog and exit.
        for line in render(rows, cursor=-1, current_alias=current_alias):
            write(line)
        write("")
        write("Local Pulsar: non-interactive shell. Run `pulsarcode pick` in a fresh terminal tab to use the picker.")
        return None

    if not _tty_available():
        # Interactive shell, but no termios (Windows native, etc.).
        # GAP-D fix per audit 2026-05-20: fall back to numbered list + integer input.
        return _pick_numbered_fallback(rows, current_alias, out=out)

    cursor = find_alias(rows, current_alias)
    if cursor is None:
        cursor = first_selectable(rows)

    viewport = max(3, int(os.environ.get("PULSAR_PICKER_VIEWPORT", str(DEFAULT_VIEWPORT))))
    # Start the window so the cursor is visible. If cursor sits near the top
    # of the list, anchor the window at zero; otherwise center the cursor in
    # the window for first paint.
    if cursor < viewport:
        window_top = 0
    else:
        window_top = max(0, cursor - viewport // 2)

    last_paint_lines = 0

    def _move(delta: int) -> int:
        new = cursor + delta
        bound_low = 0
        bound_high = len(rows) - 1
        while bound_low <= new <= bound_high and rows[new].kind != "model":
            new += delta
        if new < bound_low or new > bound_high:
            return cursor
        return new

    def _adjust_window(c: int, top: int) -> int:
        # Keep the cursor inside the visible window. Scroll one step at a
        # time so the user sees an incremental scroll behavior (one row
        # appears at the bottom, one disappears at the top).
        if c < top:
            return c
        if c >= top + viewport:
            return c - viewport + 1
        return top

    if _ansi_enabled():
        out.write(_HIDE_CURSOR)
        out.flush()

    try:
        with _RawTTY():
            while True:
                # Clear previous paint
                if last_paint_lines and _ansi_enabled():
                    out.write(f"\x1b[{last_paint_lines}F")
                    out.write("\x1b[J")

                lines = render(rows, cursor, current_alias, window_top=window_top, viewport=viewport)
                for line in lines:
                    out.write(line + "\n")
                out.flush()
                last_paint_lines = len(lines)

                key = _read_key()

                if key == "UP":
                    cursor = _move(-1)
                    window_top = _adjust_window(cursor, window_top)
                    continue
                if key == "DOWN":
                    cursor = _move(+1)
                    window_top = _adjust_window(cursor, window_top)
                    continue
                if key == "ENTER":
                    record = rows[cursor].record
                    if record is None:
                        continue
                    write_active_model(record.alias)
                    return record.alias
                if key in {"ESC", "q", "CTRL-C"}:
                    return None
                # ignore everything else
    finally:
        if _ansi_enabled():
            out.write(_SHOW_CURSOR)
            out.flush()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="PulsarOS API Sonar interactive model picker.")
    parser.add_argument("--api-base", default=os.environ.get("NVIDIA_NIM_API_BASE", DEFAULT_API_BASE))
    parser.add_argument("--public-model", default=os.environ.get("PULSAR_NIM_PUBLIC_MODEL", DEFAULT_PUBLIC_MODEL))
    parser.add_argument("--no-network", action="store_true", help="Use cached or static catalog only.")
    parser.add_argument("--print", action="store_true", help="Print catalog and exit, do not enter picker.")
    parser.add_argument("--current", default=None, help="Override the current alias detection.")
    args = parser.parse_args()

    key = (
        os.environ.get("NVIDIA_NIM_API_KEY")
        or os.environ.get("NVIDIA_API_KEY")
        or os.environ.get("NVIDIA_NIM_KEY")
        or ""
    )
    if args.no_network:
        os.environ["PULSAR_NIM_SONAR_NETWORK"] = "0"
    records = runtime_catalog(api_base=args.api_base, api_key=key, public_default=args.public_model)

    current = args.current or read_active_model() or DEFAULT_PUBLIC_MODEL

    if args.print:
        groups = group_by_tier(records)
        rows = build_rows(groups)
        for line in render(rows, cursor=-1, current_alias=current):
            print(line)
        return 0

    chosen = pick_model(records, current)
    if chosen is None:
        print("Local Pulsar: selection unchanged. Active model stays at " + (current or "default") + ".", file=sys.stderr)
        return 1
    print("Local Pulsar: active model set to " + chosen + ".", file=sys.stderr)
    print(chosen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
