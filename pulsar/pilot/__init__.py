# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Pulsar Pilot: computer use for the model, with the animated pulsar cursor.

  backends.py   shared types, coordinate mapping, key parsing, detection
  x11.py        Linux/X11 via libX11 + libXtst (ctypes)
  macos.py      CoreGraphics events (ctypes), screencapture + sips
  windows.py    user32 SendInput, gdi32 BitBlt (ctypes)
  png.py        pure-python resampling + PNG encoder
  overlay.py    the pulsar cursor (tkinter, separate process, click-through)
  safety.py     off by default, failsafe corner, rate limit, audit log
  tools.py      MCP tool specs (pilot_screenshot, pilot_click, ...)
  cli.py        `pulsarcode pilot on|off|status|tour|log`

Importing this package (or any module above except the OS backends, which
are loaded lazily) never touches a native library, on any platform.
"""

__all__ = ["backends", "png", "safety", "tools", "overlay", "cli"]
