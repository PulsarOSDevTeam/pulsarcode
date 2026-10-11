// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 PulsarOS Intelligence Inc.
// `claude plugin test pulsar/fnplug`: the engine itself judges every code point a Stage raster cell may
// hold. pulsar/stage/raster.py emits printable ASCII and CELL_RANGES only, and one cell the engine
// refuses blanks the whole pane, so every one of them is mounted here as a Raster cell through the
// plugin's own Pane drawing. tests/test_stage_pane.py keeps this table equal to raster.CELL_RANGES.

import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

const CORE = 'http://127.0.0.1:4747'
const TOKEN = `pls_${'k'.repeat(43)}`
const PANE = 'pulsar-stage'
const PLACEHOLDER = 0x25af

// cells:begin (generated from pulsar.stage.raster.CELL_RANGES)
const CELL_RANGES: readonly (readonly [number, number])[] = [
  [0x00a1, 0x00ac], [0x00ae, 0x02ff], [0x0370, 0x0377], [0x037a, 0x037f], [0x0384, 0x038a], [0x038c, 0x038c],
  [0x038e, 0x03a1], [0x03a3, 0x0482], [0x048a, 0x052f], [0x0531, 0x0556], [0x0559, 0x058a], [0x058d, 0x058f],
  [0x05d0, 0x05ea], [0x05ef, 0x05f4], [0x0620, 0x064a], [0x0660, 0x066d], [0x10a0, 0x10c5], [0x10c7, 0x10c7],
  [0x10cd, 0x10cd], [0x10d0, 0x10ff], [0x1e00, 0x1f15], [0x1f18, 0x1f1d], [0x1f20, 0x1f45], [0x1f48, 0x1f4d],
  [0x1f50, 0x1f57], [0x1f59, 0x1f59], [0x1f5b, 0x1f5b], [0x1f5d, 0x1f5d], [0x1f5f, 0x1f7d], [0x1f80, 0x1fb4],
  [0x1fb6, 0x1fc4], [0x1fc6, 0x1fd3], [0x1fd6, 0x1fdb], [0x1fdd, 0x1fef], [0x1ff2, 0x1ff4], [0x1ff6, 0x1ffe],
  [0x2010, 0x2027], [0x2030, 0x205e], [0x2070, 0x2071], [0x2074, 0x208e], [0x2090, 0x209c], [0x20a0, 0x20c0],
  [0x2100, 0x218b], [0x2190, 0x2319], [0x231c, 0x2328], [0x232b, 0x23e8], [0x23ed, 0x23ef], [0x23f1, 0x23f2],
  [0x23f4, 0x2426], [0x2440, 0x244a], [0x2460, 0x25fc], [0x25ff, 0x2613], [0x2616, 0x262f], [0x2638, 0x2647],
  [0x2654, 0x267e], [0x2680, 0x2689], [0x2690, 0x2692], [0x2694, 0x26a0], [0x26a2, 0x26a9], [0x26ac, 0x26bc],
  [0x26bf, 0x26c3], [0x26c6, 0x26cd], [0x26cf, 0x26d3], [0x26d5, 0x26e9], [0x26eb, 0x26f1], [0x26f4, 0x26f4],
  [0x26f6, 0x26f9], [0x26fb, 0x26fc], [0x26fe, 0x2704], [0x2706, 0x2709], [0x270c, 0x2727], [0x2729, 0x274b],
  [0x274d, 0x274d], [0x274f, 0x2752], [0x2756, 0x2756], [0x2758, 0x2794], [0x2798, 0x27af], [0x27b1, 0x27be],
  [0x27c0, 0x2b1a], [0x2b1d, 0x2b4f], [0x2b51, 0x2b54], [0x2b56, 0x2b73], [0x2b76, 0x2b95], [0x2b97, 0x2bff],
]
// cells:end

function everyCell(): number[] {
  const out: number[] = []
  for (let cp = 0x20; cp <= 0x7e; cp++) out.push(cp)
  for (const [lo, hi] of CELL_RANGES) for (let cp = lo; cp <= hi; cp++) out.push(cp)
  return out
}

function cellsOf(cps: readonly number[], n: number): string {
  const words = new Uint32Array(n * 3)
  for (let i = 0; i < n; i++) {
    words[i * 3] = i < cps.length ? (cps[i] ?? 0x20) : 0x20
    words[i * 3 + 1] = i % 2 === 0 ? 0x00ecf6ff : 0x01000000
    words[i * 3 + 2] = 0x0007060c
  }
  const bytes = new Uint8Array(words.buffer)
  let bin = ''
  for (const b of bytes) bin += String.fromCharCode(b)
  return btoa(bin)
}

// A Core that answers every frame with `cps` laid out row by row, in a pane of 512 x 24 cells.
function core(on: On, cps: () => readonly number[]) {
  mock.env(on, { PULSAR_CORE: CORE, PULSAR_SESSION_TOKEN: TOKEN })
  const clock = mock.clock(on, { now: 1_790_000_000_000 })
  on('session.start', () => ({ cwd: '/work' }))
  on('command.register', ($, e) => ({ value: { command: e.name } }))
  on('ui.toast', () => ({ value: undefined }))
  on('ui.log', () => ({ value: undefined }))
  on('ui.panes', () => ({ value: [{ id: PANE, title: PANE, isShown: true, isFocused: false, isPlaced: true }] }))
  on('ui.open', () => ({ value: { isPlaced: true as const } }))
  on('ui.invalidate', () => ({ value: undefined }))
  on('http.fetch', ($, e) => {
    const u = new URL(e.url)
    const meta = { id: 'cells', version: 1_790_000_000, slides: 1, title: 'Cells', kind: 'card' }
    if (u.searchParams.get('meta') === '1') {
      return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify(meta) } }
    }
    const cols = Number(u.searchParams.get('cols'))
    const rows = Number(u.searchParams.get('rows'))
    const cells = cellsOf(cps(), cols * rows)
    const frame = { ...meta, cells, cols, rows, slide: 0, animating: false, slide_title: '' }
    return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify(frame) } }
  })
  return clock
}

const PANE_MOUNT = {
  plugin: 'pulsar-stage',
  surface: 'terminal',
  component: 'Pane',
  requestId: PANE,
  props: { title: 'stage', isFocused: true, bodyColumns: 512, placement: 'dock', scroll: { offset: 0, bodyRows: 25 }, view: {} },
  viewport: { columns: 600, rows: 40, isFullscreen: true },
} as const

test('the table holds the placeholder and fits one pane', () => {
  const all = everyCell()
  expect(all.includes(PLACEHOLDER)).toBe(true)
  expect(new Set(all).size).toBe(all.length)
  expect(all.length).toBeLessThanOrEqual(512 * 24)
  expect(all.every(cp => cp >= 0x20 && cp <= 0xffff && (cp < 0xd800 || cp > 0xdfff))).toBe(true)
})

test('every code point a Stage raster cell may hold mounts as a Raster cell', async ($, on) => {
  let cps = everyCell()
  const refused: string[] = []
  const clock = core(on, () => cps)
  await $.session.start({ cwd: '/work', surface: 'terminal' as const, isInteractive: true })
  await clock.advance(700)
  // on a refusal, name the code point, drop it and mount again, so one run lists every refused one
  let drawn = ''
  for (let round = 0; round < 200; round++) {
    try {
      const ui = await $.ui.mount(PANE_MOUNT)
      expect(await ui.find({ type: 'Raster' })).toBeDefined()
      drawn = JSON.stringify(await ui.drawn())
      break
    } catch (err) {
      const msg = String(err)
      const m = /cell (\d+) holds (U\+[0-9A-F]+)/.exec(msg)
      if (!m) {
        refused.push(msg.slice(0, 300))
        break
      }
      refused.push(m[2] ?? '?')
      const at = Number(m[1])
      cps = cps.filter((_, i) => i !== at)
    }
  }
  expect(refused).toEqual([])
  // the pane drew them all: one Raster 512 cells across
  expect(drawn).toContain('"columns":512')
  expect(drawn).toContain(cellsOf(cps, 512 * 24))
})

test('the oracle bites: a trigram (wide since Unicode 16) is refused', async ($, on) => {
  const clock = core(on, () => [0x41, 0x2630, 0x42])
  await $.session.start({ cwd: '/work', surface: 'terminal' as const, isInteractive: true })
  await clock.advance(700)
  let why = ''
  try {
    await $.ui.mount(PANE_MOUNT)
  } catch (err) {
    why = String(err)
  }
  expect(why).toContain('U+2630')
})
