// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 PulsarOS Intelligence Inc.
// `claude plugin test pulsar/fnplug`: the Stage pane against a fake Pulsar Core beneath the plugin.

import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

const CORE = 'http://127.0.0.1:4747'
const TOKEN = `pls_${'k'.repeat(43)}`
const PANE = 'pulsar-stage'

type Seen = { url: string; headers: Record<string, string> }

// 40 x 11 blank cells, base64 of 440 little-endian [0x20, 0x01000000, 0x01000000] triplets.
function blankCells(cols: number, rows: number): string {
  const words = new Uint32Array(cols * rows * 3)
  for (let i = 0; i < cols * rows; i++) {
    words[i * 3] = 0x20
    words[i * 3 + 1] = 0x01000000
    words[i * 3 + 2] = 0x01000000
  }
  const bytes = new Uint8Array(words.buffer)
  let bin = ''
  for (const b of bytes) bin += String.fromCharCode(b)
  return btoa(bin)
}

// The world beneath the plugin: a session, a clock, panes, toasts, commands and a fake Core.
type Opts = { refuseFetch?: boolean; reason?: string; empty?: boolean; width?: number }

function world(on: On, env: Record<string, string>, opts: Opts = {}) {
  const seen: Seen[] = []
  const curl: { argv: readonly string[]; stdin: string }[] = []
  const commands: string[] = []
  const toasts: string[] = []
  const opened: string[] = []
  const unplaced: string[] = []
  const openArgs: { focus: boolean; columns: number | undefined }[] = []
  // `width`: the terminal's columns, for the engine's rule that an unasked open (here: no `focus`) is
  // placed only from 144 columns; `empty`: the stage holds no scene yet (Core answers 204).
  const stage = { id: 'd1', version: 1_790_000_000, title: 'Demo', empty: opts.empty === true, width: opts.width ?? 200 }
  mock.env(on, env)
  const clock = mock.clock(on, { now: 1_790_000_000_000 })
  on('session.start', () => ({ cwd: '/work' }))
  on('command.register', ($, e) => {
    commands.push(e.name)
    return { value: { command: e.name } }
  })
  on('ui.toast', ($, e) => {
    toasts.push(e.text)
    return { value: undefined }
  })
  on('ui.log', () => ({ value: undefined }))
  on('ui.panes', () => ({
    value: [
      ...opened.map(id => ({ id, title: id, isShown: true, isFocused: false, isPlaced: true })),
      ...unplaced.map(id => ({ id, title: id, isShown: false, isFocused: false, isPlaced: false })),
    ],
  }))
  on('ui.open', ($, e) => {
    openArgs.push({ focus: e.focus === true, columns: e.columns })
    if (e.focus !== true && stage.width < 144) {
      if (!unplaced.includes(e.id) && !opened.includes(e.id)) unplaced.push(e.id)
      return { value: { isPlaced: false as const, reason: `unasked below 144 columns (${stage.width} now)` } }
    }
    const at = unplaced.indexOf(e.id)
    if (at >= 0) unplaced.splice(at, 1)
    if (!opened.includes(e.id)) opened.push(e.id)
    return { value: { isPlaced: true as const } }
  })
  on('ui.invalidate', () => ({ value: undefined }))
  on('ui.close', ($, e) => {
    const at = opened.indexOf(e.id)
    if (at >= 0) opened.splice(at, 1)
    return { value: undefined }
  })
  const answer = (url: string): { status: number; text: string } => {
    const u = new URL(url)
    if (u.pathname !== '/pulsar/stage/raster') return { status: 404, text: '{}' }
    if (stage.empty && u.searchParams.get('idle') !== '1') return { status: 204, text: '' }
    if (u.searchParams.get('meta') === '1') {
      return {
        status: 200,
        text: JSON.stringify({ ...stage, slides: 3, slide: 0, kind: 'deck', count: 1 }),
      }
    }
    const cols = Number(u.searchParams.get('cols'))
    const rows = Number(u.searchParams.get('rows'))
    return {
      status: 200,
      text: JSON.stringify({
        cells: blankCells(cols, rows), cols, rows, slides: 3, slide: Number(u.searchParams.get('slide')),
        animating: false, title: stage.title, slide_title: 'Intro', kind: 'deck', id: stage.id,
      }),
    }
  }
  on('http.fetch', ($, e) => {
    if (opts.refuseFetch) return { deny: opts.reason ?? 'nonessential network traffic is disabled for this session' }
    seen.push({ url: e.url, headers: { ...(e.init?.headers ?? {}) } })
    const a = answer(e.url)
    return { value: { status: a.status, ok: a.status === 200, headers: {}, text: a.text } }
  })
  on('process.run', ($, e) => {
    curl.push({ argv: e.argv, stdin: e.init?.stdin ?? '' })
    const a = answer(e.argv[e.argv.length - 1] ?? '')
    return { value: { exitCode: 0, stdout: `${a.text}\n${a.status}`, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  return { seen, curl, commands, toasts, opened, unplaced, openArgs, clock, stage }
}

const START = { cwd: '/work', surface: 'terminal' as const, isInteractive: true }

test('does nothing without a Pulsar session token', async ($, on) => {
  const w = world(on, { PULSAR_CORE: CORE, ANTHROPIC_AUTH_TOKEN: 'sk-ant-not-a-pulsar-token' })
  await $.session.start(START)
  await w.clock.advance(2000)
  expect(w.commands).toEqual([])
  expect(w.seen).toEqual([])
})

test('refuses a Core that is not on loopback', async ($, on) => {
  const w = world(on, { PULSAR_CORE: 'http://core.example.com:4747', PULSAR_SESSION_TOKEN: TOKEN })
  await $.session.start(START)
  await w.clock.advance(2000)
  expect(w.commands).toEqual([])
  expect(w.seen).toEqual([])
})

test('polls Core as the session and opens nothing on the main screen', async ($, on) => {
  const w = world(on, { PULSAR_CORE: `${CORE}/`, ANTHROPIC_AUTH_TOKEN: TOKEN })
  await $.session.start(START)
  await w.clock.advance(700)
  expect(w.commands).toEqual(['stage-pane'])
  expect(w.seen.length).toBeGreaterThan(0)
  const first = w.seen[0]
  expect(first?.url).toBe(`${CORE}/pulsar/stage/raster?meta=1`)
  expect(first?.headers.Authorization).toBe(`Bearer ${TOKEN}`)
  expect(first?.headers['X-Pulsar-Viewer']).toBe('pane-standby')
  // the terminal never said it docks panes: a toast points at the command instead of a takeover
  expect(w.opened).toEqual([])
  expect(w.toasts.some(t => t.includes('/stage-pane'))).toBe(true)
})

test('draws the frame Core sends as one Raster under the controls', async ($, on) => {
  const w = world(on, { PULSAR_CORE: CORE, PULSAR_SESSION_TOKEN: TOKEN })
  await $.session.start(START)
  await w.clock.advance(700)
  const ui = await $.ui.mount({
    plugin: 'pulsar-stage',
    surface: 'terminal',
    component: 'Pane',
    requestId: PANE,
    props: { title: 'stage', isFocused: true, bodyColumns: 40, placement: 'dock', scroll: { offset: 0, bodyRows: 12 }, view: {} },
    viewport: { columns: 120, rows: 40, isFullscreen: true },
  })
  const raster = await ui.find({ type: 'Raster' })
  expect(raster).toBeDefined()
  const tree = JSON.stringify(await ui.drawn())
  expect(tree).toContain('"columns":40')
  expect(tree).toContain('"rows":11')
  expect(tree).not.toContain(TOKEN)
  const frameUrl = w.seen.map(s => s.url).find(u => u.includes('cols=40'))
  expect(frameUrl).toContain('rows=11')
  expect(frameUrl).toContain('id=d1')
  await ui.press({ key: 'next' })
  await w.clock.advance(100)
  expect(w.seen.map(s => s.url).some(u => u.includes('slide=1'))).toBe(true)
})

test('falls back to curl with the token on stdin when the host refuses plugin fetches', async ($, on) => {
  const w = world(on, { PULSAR_CORE: CORE, PULSAR_SESSION_TOKEN: TOKEN }, { refuseFetch: true })
  await $.session.start(START)
  await w.clock.advance(1300)
  expect(w.curl.length).toBeGreaterThan(0)
  const call = w.curl[w.curl.length - 1]
  // the system's own curl by its absolute path, never one that a PATH entry finds first
  expect(call?.argv[0]).toBe('/usr/bin/curl')
  // -q first: curl never reads the person's .curlrc (an `include` or `trace-ascii` there)
  expect(call?.argv[1]).toBe('-q')
  expect(call?.argv.join(' ')).toContain('--proto =http')
  expect(call?.argv.join(' ')).not.toContain(TOKEN)
  expect(call?.stdin).toContain(`Authorization: Bearer ${TOKEN}`)
  expect(call?.argv[call.argv.length - 1]).toBe(`${CORE}/pulsar/stage/raster?meta=1`)
})

test('respects an administrator policy that refuses plugin network access', async ($, on) => {
  const w = world(on, { PULSAR_CORE: CORE, PULSAR_SESSION_TOKEN: TOKEN }, { refuseFetch: true, reason: 'Network access from plugins is off by policy' })
  await $.session.start(START)
  await w.clock.advance(1300)
  expect(w.curl).toEqual([])
})

function abovePrompt($: Parameters<Parameters<typeof test>[1]>[0], columns: number, isFullscreen: boolean) {
  return $.ui.mount({
    plugin: 'pulsar-stage',
    surface: 'terminal',
    component: 'AbovePrompt',
    props: { hasSurvey: false, isWorking: false, maxRows: 10, bodyColumns: columns, scroll: { offset: 0, bodyRows: 10 }, view: {} },
    viewport: { columns, rows: 45, isFullscreen },
  })
}

function viewers(w: ReturnType<typeof world>): string[] {
  return w.seen.map(s => s.headers['X-Pulsar-Viewer'] ?? '')
}

test('docks itself for a new scene only where the terminal docks panes, and not after the person closed it', async ($, on) => {
  const w = world(on, { PULSAR_CORE: CORE, PULSAR_SESSION_TOKEN: TOKEN }, { empty: true, width: 160 })
  on('ui.render', { component: 'AbovePrompt' }, ($, e) => {
    const { Box } = $.ui.resolve(e)
    return <Box />
  })
  await $.session.start(START)
  await abovePrompt($, 160, true)
  await w.clock.advance(1300)
  // a fullscreen terminal 160 columns wide: an open of its own would dock, so Core hears pane-auto
  expect(viewers(w).at(-1)).toBe('pane-auto')
  expect(w.opened).toEqual([])
  w.stage.empty = false
  await w.clock.advance(700)
  expect(w.opened).toEqual([PANE])
  await w.clock.advance(700)
  expect(viewers(w).at(-1)).toBe('pane')
  const pane = await $.ui.mount({
    plugin: 'pulsar-stage',
    surface: 'terminal',
    component: 'Pane',
    requestId: PANE,
    props: { title: 'stage', isFocused: true, bodyColumns: 80, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} },
    viewport: { columns: 160, rows: 45, isFullscreen: true },
  })
  await pane.press({ key: 'close' })
  expect(w.opened).toEqual([])
  w.stage.id = 'd2'
  w.stage.title = 'Second'
  w.stage.version += 5
  await w.clock.advance(1300)
  expect(w.opened).toEqual([])
  expect(w.toasts.some(t => t.includes('Second') && t.includes('/stage-pane'))).toBe(true)
  expect(viewers(w).at(-1)).toBe('pane-standby')
})

test('never reports pane-auto where the engine would leave its own open undrawn (under 144 columns)', async ($, on) => {
  const w = world(on, { PULSAR_CORE: CORE, PULSAR_SESSION_TOKEN: TOKEN }, { empty: true, width: 120 })
  on('ui.render', { component: 'AbovePrompt' }, ($, e) => {
    const { Box } = $.ui.resolve(e)
    return <Box />
  })
  await $.session.start(START)
  await abovePrompt($, 120, true)
  await w.clock.advance(1300)
  w.stage.empty = false
  await w.clock.advance(1300)
  w.stage.id = 'd2'
  w.stage.version += 5
  await w.clock.advance(1300)
  // fullscreen but 120 columns: the unasked open waits undrawn, a toast points at the command
  expect(w.opened).toEqual([])
  expect(w.toasts.some(t => t.includes('Widen the terminal') && t.includes('/stage-pane'))).toBe(true)
  expect(viewers(w).length).toBeGreaterThan(3)
  expect(viewers(w).every(v => v === 'pane-standby')).toBe(true)
  // the terminal widens past the floor: the engine seats the waiting pane, Core hears pane
  w.stage.width = 150
  await abovePrompt($, 150, true)
  w.unplaced.splice(0).forEach(id => w.opened.push(id))
  await w.clock.advance(700)
  expect(viewers(w).at(-1)).toBe('pane')
})

test('stays on standby at a width where an open of its own was just left undrawn', async ($, on) => {
  const w = world(on, { PULSAR_CORE: CORE, PULSAR_SESSION_TOKEN: TOKEN }, { empty: true, width: 100 })
  on('ui.render', { component: 'AbovePrompt' }, ($, e) => {
    const { Box } = $.ui.resolve(e)
    return <Box />
  })
  // the plugin's view of the terminal says 150 columns, the engine still refuses: trust the engine
  await $.session.start(START)
  await abovePrompt($, 150, true)
  await w.clock.advance(700)
  expect(viewers(w).at(-1)).toBe('pane-auto')
  w.stage.empty = false
  await w.clock.advance(1300)
  expect(w.opened).toEqual([])
  expect(viewers(w).at(-1)).toBe('pane-standby')
  await abovePrompt($, 170, true)
  w.stage.width = 170
  await w.clock.advance(700)
  expect(viewers(w).at(-1)).toBe('pane-auto')
})

test('speaks of an id with a lone surrogate the way Core does', async ($, on) => {
  const w = world(on, { PULSAR_CORE: CORE, PULSAR_SESSION_TOKEN: TOKEN })
  w.stage.id = 'deck\ud83d'
  await $.session.start(START)
  await w.clock.advance(700)
  const ui = await $.ui.mount({
    plugin: 'pulsar-stage',
    surface: 'terminal',
    component: 'Pane',
    requestId: PANE,
    props: { title: 'stage', isFocused: true, bodyColumns: 30, placement: 'dock', scroll: { offset: 0, bodyRows: 8 }, view: {} },
    viewport: { columns: 160, rows: 40, isFullscreen: true },
  })
  expect(await ui.find({ type: 'Raster' })).toBeDefined()
  const frameUrl = w.seen.map(s => s.url).find(u => u.includes('cols=30'))
  expect(frameUrl).toContain('id=deck%EF%BF%BD')
})

test('/stage-pane docks half the terminal even while AbovePrompt reports only the transcript column', async ($, on) => {
  const w = world(on, { PULSAR_CORE: CORE, PULSAR_SESSION_TOKEN: TOKEN }, { width: 160 })
  on('ui.render', { component: 'AbovePrompt' }, ($, e) => {
    const { Box } = $.ui.resolve(e)
    return <Box />
  })
  await $.session.start(START)
  // a docked pane leaves AbovePrompt the transcript's 79 columns of a 160-column terminal
  await abovePrompt($, 79, true)
  // as the engine stamps it when the person runs the command in a 160-column fullscreen terminal
  const presentation = { isFullscreen: true, columns: 160 }
  const out = await $.command.run({ command: 'stage-pane', args: '', presentation } as never)
  expect(JSON.stringify(out)).toContain('open')
  const asked = w.openArgs.filter(a => a.focus)
  expect(asked.length).toBe(1)
  expect(asked[0]?.columns).toBe(80)
})
