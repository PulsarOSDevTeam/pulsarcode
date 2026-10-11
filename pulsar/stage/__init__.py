# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Pulsar Stage: a presentation surface the model drives.

The model calls MCP tools (stage_present, stage_show, ...) with a scene; Core
broadcasts the scene; viewers render it: an animated terminal viewer
(`pulsarcode stage`, auto-opened in a split when possible) and a live web
view served by Core. Scenes can also be exported as a standalone HTML deck.

  schema.py   scene model, normalization, limits, sanitizing (shared contract)
"""
