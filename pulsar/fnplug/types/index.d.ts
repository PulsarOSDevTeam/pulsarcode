// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 PulsarOS Intelligence Inc.
// The pulsar-stage plugin's state contract: what the Stage pane draws from.

/** The scene the stage shows now, as Core's raster route describes it (no content, no cells). */
export type StageScene = {
  /** The scene's id on Core's stage. */
  id: string
  /** When Core accepted this state of the scene (seconds since the epoch): a new value redraws. */
  version: number
  /** The scene's title, printable text. */
  title: string
  /** deck, chart, diagram, table, ... */
  kind: string
  /** Slides in a deck; 1 for every other kind. */
  slides: number
}

declare module 'claude-code' {
  interface PluginState {
    'pulsar-stage': {
      /** The newest scene of this session's stage, or null while the stage is empty. */
      scene: StageScene | null
      /** The slide the pane shows (0-based). */
      slide: number
      /** When the scene or slide appeared ($.clock.now() milliseconds): drives the intro animation. */
      shownAt: number
      /** The person closed the pane: new scenes toast instead of opening it again. */
      dismissed: boolean
      /** The person opened the pane (and has not closed it by hand since): the engine then places an
       * unasked open from 110 terminal columns instead of 144. */
      asked: boolean
      /** A short line about Core's reachability, empty while all is well. */
      status: string
    }
  }
}
