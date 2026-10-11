// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 PulsarOS Intelligence Inc.
//
// Pulsar Stage, live inside Claude Code: a pane that shows what the model
// presents (decks, charts, diagrams, tables, code) as Pulsar Core draws it.
//
// Core does the drawing: GET /pulsar/stage/raster answers a frame of cells
// (the terminal viewer's own renderer) for the pane's exact size, the slide
// and the seconds since the slide appeared. This module only polls, keeps
// the slide position in $.state, draws one Raster under a row of controls,
// and blits the next frames at ~15 fps while the intro still plays.
//
// Safety: it talks to one URL, the loopback Core named in PULSAR_CORE, as
// this session (PULSAR_SESSION_TOKEN, or ANTHROPIC_AUTH_TOKEN when it is a
// Pulsar session token); every request is time-bounded; every failure is
// swallowed into a quiet line in the pane; the token is never shown or logged.

import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { StageScene } from '../types'

const PANE = 'pulsar-stage'
const TITLE = '◉ PULSAR STAGE'
const RASTER = 'stage'
const COMMAND = 'stage-pane'
const FOCUS_HINT = 'ctrl+x tab to control'

const IDLE_MS = 600 // version check while nothing moves
const FRAME_MS = 66 // ~15 fps while an intro plays
const META_TIMEOUT_MS = 1500
const FRAME_TIMEOUT_MS = 2500
const STALE_S = 120 // a scene already this old at first sight is shown quietly, never popped open
// The engine places a pane the plugin opens on its own only from this many terminal columns, or from
// ASKED_FLOOR for an id the person opened before (and has not closed by hand since); see PaneOpenArgs.
const UNASKED_FLOOR = 144
const ASKED_FLOOR = 110

const BEAM = '#36E2FF'
const FLARE = '#FF2E88'
const PLASMA = '#9D4DFF'
const EMBER = '#FF6A00'
const ICE = '#ECF6FF'
const ASH = '#9692AC'
const DUST = '#58546C'

const sceneAtom = atom({ plugin: 'pulsar-stage', key: 'scene' } as const, null)
const slideAtom = atom({ plugin: 'pulsar-stage', key: 'slide' } as const, 0)
const shownAtom = atom({ plugin: 'pulsar-stage', key: 'shownAt' } as const, 0)
const dismissedAtom = atom({ plugin: 'pulsar-stage', key: 'dismissed' } as const, false)
const statusAtom = atom({ plugin: 'pulsar-stage', key: 'status' } as const, '')
const askedAtom = atom({ plugin: 'pulsar-stage', key: 'asked' } as const, false)

type Core = { base: string; token: string }
type Frame = {
  cells: string
  cols: number
  rows: number
  slides: number
  slide: number
  animating: boolean
  title: string
  slideTitle: string
}
type Meta = { id: string; version: number; slides: number; title: string; kind: string }
type Reply = { status: number; body: unknown }
type Mount = { cols: number; rows: number; id: string; slide: number }
type Cancel = { cancel: () => void }
type Step = number | 'first' | 'last' | 'replay'

const KIND_ICON: Record<string, string> = {
  deck: '◧',
  card: '◈',
  markdown: '¶',
  code: '❮❯',
  table: '▦',
  chart: '▥',
  diagram: '◇',
  metrics: '◔',
  timeline: '┆',
  progress: '◌',
}

// Facts and handles of this load, not drawing state (that lives in $.state): the session's Core and
// token (read again at every load), the layout the terminal reported, and the timers this load runs.
let core: Core | undefined
let isFullscreen: boolean | undefined
let columns = 0
// The terminal width at which the engine last left an unasked open of the pane undrawn: no claim that
// the pane opens itself (`pane-auto`) until the terminal is wider than that.
let unplacedAt = 0
let polling = false
let animGen = 0
let animTimer: Cancel | undefined
let animBusy = false
let retryArmed = false
let transport: 'fetch' | 'curl' | 'none' = 'fetch'
let curlFailures = 0
// curl from the system's own folders, never whatever a PATH entry finds first: a project's
// node_modules/.bin or an empty PATH entry could hold a `curl` that would then get the session token.
const CURLS = ['/usr/bin/curl', '/bin/curl', 'C:\\Windows\\System32\\curl.exe']
let curlAt = 0 // the entry of CURLS that ran last

// The loopback Core origin, or undefined: http only, 127.0.0.1 / localhost / ::1, no credentials.
function loopback(raw: string | undefined): string | undefined {
  if (!raw || raw.length > 200) return undefined
  let u: URL
  try {
    u = new URL(raw.trim())
  } catch {
    return undefined
  }
  const host = u.hostname.toLowerCase()
  const isLoop = host === '127.0.0.1' || host === 'localhost' || host === '[::1]' || host === '::1'
  if (u.protocol !== 'http:' || !isLoop || u.username !== '' || u.password !== '') return undefined
  return `${u.protocol}//${u.host}`
}

function cleanToken(raw: string | undefined): string | undefined {
  const tok = (raw ?? '').trim()
  return /^[A-Za-z0-9._~+/=-]{8,512}$/.test(tok) ? tok : undefined
}

function printable(s: unknown, limit = 120): string {
  const text = typeof s === 'string' ? s : ''
  let out = ''
  for (const ch of text) {
    const cp = ch.codePointAt(0) ?? 0x20
    const control = cp < 0x20 || (cp >= 0x7f && cp <= 0x9f) || (cp >= 0xd800 && cp <= 0xdfff)
    // bidi overrides and isolates, zero-width and other format characters never reach Ink
    const format = (cp >= 0x200b && cp <= 0x200f) || (cp >= 0x2028 && cp <= 0x202e) || (cp >= 0x2060 && cp <= 0x206f)
    out += control || format || cp === 0xfeff || cp === 0x061c || cp === 0x180e ? ' ' : ch
    if (out.length >= limit) break
  }
  return out.replace(/\s+/g, ' ').trim()
}

// A lone surrogate half (encodeURIComponent throws on one) becomes U+FFFD, as Core spells such an id.
function wellFormed(s: string): string {
  return s.replace(/[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/g, '\ufffd')
}

function num(v: unknown, fallback: number): number {
  return typeof v === 'number' && Number.isFinite(v) ? v : fallback
}

function asFrame(v: unknown): Frame | undefined {
  if (!v || typeof v !== 'object') return undefined
  const f = v as Record<string, unknown>
  const cols = num(f.cols, 0)
  const rows = num(f.rows, 0)
  const cells = typeof f.cells === 'string' ? f.cells : ''
  if (cols < 1 || cols > 512 || rows < 1 || rows > 256) return undefined
  if (cells.length !== Math.ceil((cols * rows * 12) / 3) * 4) return undefined
  return {
    cells,
    cols,
    rows,
    slides: Math.max(1, Math.floor(num(f.slides, 1))),
    slide: Math.max(0, Math.floor(num(f.slide, 0))),
    animating: f.animating === true,
    title: printable(f.title),
    slideTitle: printable(f.slide_title),
  }
}

function asMeta(v: unknown): Meta | undefined {
  if (!v || typeof v !== 'object') return undefined
  const m = v as Record<string, unknown>
  if (typeof m.id !== 'string' || m.id === '') return undefined
  return {
    id: wellFormed(m.id.slice(0, 64)),
    version: num(m.version, 0),
    slides: Math.max(1, Math.floor(num(m.slides, 1))),
    title: printable(m.title),
    kind: printable(m.kind, 16),
  }
}

function frameQuery(m: Mount, t: number): string {
  return (
    `/pulsar/stage/raster?cols=${m.cols}&rows=${m.rows}&slide=${m.slide}` +
    `&t=${t.toFixed(3)}&id=${encodeURIComponent(wellFormed(m.id))}`
  )
}

// Half the terminal for a docked pane, within 56..160 columns.
function dockColumns(term: number): number | undefined {
  return term > 0 ? Math.max(56, Math.min(160, Math.round(term * 0.5))) : undefined
}

type Settled<T> = { ok: true; value: T } | { ok: false; error: string }

// A promise that settles by `ms`, never rejecting: the value, or why not (a timeout included).
function settle<T>($: EngineInterface, work: Promise<T>, ms: number): Promise<Settled<T>> {
  return new Promise(resolve => {
    let done = false
    let timer: Cancel | undefined
    const finish = (out: Settled<T>) => {
      if (done) return
      done = true
      try {
        timer?.cancel()
      } catch {
        // gone already
      }
      resolve(out)
    }
    try {
      timer = $.clock.after(ms, () => finish({ ok: false, error: 'timeout' }))
    } catch {
      timer = undefined
    }
    work.then(
      value => finish({ ok: true, value }),
      err => finish({ ok: false, error: String(err) }),
    )
  })
}

function trace($: EngineInterface, text: string): void {
  try {
    $.ui.log(`pulsar-stage: ${text}`, { to: 'debug' })
  } catch {
    // the debug log is optional
  }
}

function parseReply(status: number, text: string): Reply | undefined {
  if (status !== 200) return { status, body: null }
  try {
    return { status: 200, body: JSON.parse(text) as unknown }
  } catch {
    return undefined
  }
}

// GET from Core through the host's fetch. `undefined` when it fails; switches the session to the curl
// transport when this session refuses plugin network access (CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC).
async function viaFetch($: EngineInterface, c: Core, path: string, viewer: string, ms: number): Promise<Reply | undefined> {
  let pending: Promise<{ status: number; text: string }>
  try {
    pending = $.http.fetch(c.base + path, {
      method: 'GET',
      headers: { Authorization: `Bearer ${c.token}`, Accept: 'application/json', 'X-Pulsar-Viewer': viewer },
    })
  } catch (err) {
    pending = Promise.reject(err)
  }
  const res = await settle($, pending, ms)
  if (res.ok) return parseReply(res.value.status, res.value.text)
  // Only the launcher's CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC switches transports; an administrator's
  // "Network access from plugins" policy, or another plugin's deny, is respected: the pane stays quiet.
  if (/nonessential network traffic is disabled/i.test(res.error) && transport === 'fetch') {
    transport = 'curl'
    trace($, 'host fetch refused for plugins in this session; using curl on loopback instead')
  }
  return undefined
}

// GET from Core with curl, for sessions whose host refuses plugin network access. The token never
// reaches argv (other local users can read argv): curl reads the headers from stdin (-H @-). `-q` must
// come first: it keeps curl from reading the person's ~/.curlrc ($CURL_HOME/.curlrc), whose options
// (`include`, `trace-ascii`, `output`, a proxy) would garble the answer or write the token to disk.
async function viaCurl($: EngineInterface, c: Core, path: string, viewer: string, ms: number): Promise<Reply | undefined> {
  const secs = Math.max(1, Math.ceil(ms / 1000))
  const stdin = `Authorization: Bearer ${c.token}\nAccept: application/json\nX-Pulsar-Viewer: ${viewer}\n`
  let res: Settled<{ exitCode: number; stdout: string }> = { ok: false, error: 'no curl' }
  // the curl that ran last first, then the others: a system without /usr/bin/curl finds its own once
  for (let i = 0; i < CURLS.length; i++) {
    const at = (curlAt + i) % CURLS.length
    const curl = CURLS[at] || '/usr/bin/curl'
    const argv = [
      curl, '-q', '-sS', '--proto', '=http', '--noproxy', '*', '--max-time', String(secs),
      '-H', '@-', '-w', '\n%{http_code}', c.base + path,
    ]
    let pending: Promise<{ exitCode: number; stdout: string }>
    try {
      pending = $.process.run(argv, { stdin, timeoutMs: ms + 1000 })
    } catch (err) {
      pending = Promise.reject(err)
    }
    res = await settle($, pending, ms + 1500)
    if (res.ok || res.error === 'timeout') {
      curlAt = at
      break
    }
  }
  if (!res.ok) {
    curlFailures += 1
    if (curlFailures >= 5 && transport === 'curl') {
      transport = 'none'
      trace($, 'curl does not run here either; the pane cannot reach Pulsar Core')
    }
    return undefined
  }
  curlFailures = 0
  if (res.value.exitCode !== 0) return undefined
  const out = res.value.stdout
  const cut = out.lastIndexOf('\n')
  const status = Number(out.slice(cut + 1).trim())
  if (cut < 0 || !Number.isInteger(status) || status < 100) return undefined
  return parseReply(status, out.slice(0, cut))
}

async function ask($: EngineInterface, path: string, viewer: string, ms: number): Promise<Reply | undefined> {
  const c = core
  if (!c || transport === 'none') return undefined
  if (transport === 'fetch') {
    const reply = await viaFetch($, c, path, viewer, ms)
    if (reply || transport === 'fetch') return reply
  }
  return viaCurl($, c, path, viewer, ms)
}

async function setStatus($: EngineInterface, text: string): Promise<void> {
  try {
    if ((await read($, statusAtom)) !== text) await update($, statusAtom, () => text)
  } catch {
    // state is best effort
  }
}

async function paneState($: EngineInterface): Promise<{ isOpen: boolean; isPlaced: boolean }> {
  try {
    const pane = (await $.ui.panes()).find(p => p.id === PANE)
    return { isOpen: pane !== undefined, isPlaced: pane?.isPlaced === true }
  } catch {
    return { isOpen: false, isPlaced: false }
  }
}

// `term`: the terminal's width as the caller knows it. A command passes its `presentation.columns`:
// AbovePrompt reports only the transcript's column while a pane is docked (79 of 160), and a redraw
// can land between the command and this open.
async function openPane($: EngineInterface, asked: boolean, term = columns): Promise<boolean> {
  const cols = dockColumns(term)
  const size = cols ? { rows: 26, columns: cols } : { rows: 26 }
  try {
    const opened = asked
      ? await $.ui.open({ id: PANE, title: TITLE, focus: true, ...size })
      : await $.ui.open({ id: PANE, title: TITLE, ...size })
    if (asked) {
      await update($, askedAtom, () => true)
    } else if (opened.isPlaced) {
      unplacedAt = 0
      // placed unasked below the 144-column floor: the engine remembers the person opened it before
      if (term > 0 && term < UNASKED_FLOOR) await update($, askedAtom, () => true)
    } else {
      unplacedAt = Math.max(term, 1)
    }
    return opened.isPlaced
  } catch {
    return false
  }
}

// Would a pane this plugin opens on its own be placed now? Fullscreen (it docks as a sidebar), wide
// enough for the engine's floor, not closed by the person, and not just left undrawn at this width.
async function opensItself($: EngineInterface): Promise<boolean> {
  if (isFullscreen !== true || columns <= 0 || columns <= unplacedAt) return false
  if (await read($, dismissedAtom)) return false
  return columns >= ((await read($, askedAtom)) ? ASKED_FLOOR : UNASKED_FLOOR)
}

async function announce($: EngineInterface, meta: Meta, quiet: boolean): Promise<void> {
  const { isOpen, isPlaced } = await paneState($)
  if ((isOpen && isPlaced) || quiet) return
  const label = meta.title || meta.kind || 'a new scene'
  if (!(await read($, dismissedAtom)) && isFullscreen === true) {
    if (await openPane($, false)) return
    $.ui.toast(`◉ Pulsar Stage: ${label}. Widen the terminal or run /${COMMAND} to watch it here.`)
    return
  }
  $.ui.toast(`◉ Pulsar Stage: ${label}. Run /${COMMAND} to watch it here.`)
}

async function poll($: EngineInterface): Promise<void> {
  if (!core || polling) return
  polling = true
  try {
    const { isPlaced } = await paneState($)
    const viewer = isPlaced ? 'pane' : (await opensItself($)) ? 'pane-auto' : 'pane-standby'
    const reply = await ask($, '/pulsar/stage/raster?meta=1', viewer, META_TIMEOUT_MS)
    if (!reply || (reply.status !== 200 && reply.status !== 204)) {
      const why = reply?.status === 404 ? 'This Pulsar Core has no pane route yet.' : 'Waiting for Pulsar Core…'
      await setStatus($, why)
      return
    }
    await setStatus($, '')
    const current = await read($, sceneAtom)
    if (reply.status === 204) {
      if (current !== null) {
        await update($, sceneAtom, () => null)
        await update($, slideAtom, () => 0)
      }
      return
    }
    const meta = asMeta(reply.body)
    if (!meta) return
    if (current && current.id === meta.id && current.version === meta.version) return
    const scene: StageScene = {
      id: meta.id,
      version: meta.version,
      title: meta.title,
      kind: meta.kind,
      slides: meta.slides,
    }
    const isNew = !current || current.id !== meta.id
    const now = await $.clock.now()
    const stale = meta.version > 0 && now / 1000 - meta.version > STALE_S
    if (isNew) {
      await update($, slideAtom, () => 0)
      await update($, shownAtom, () => (stale ? 0 : now))
    } else {
      await update($, slideAtom, n => Math.max(0, Math.min(meta.slides - 1, n ?? 0)))
    }
    await update($, sceneAtom, () => scene)
    if (isNew) await announce($, meta, stale && current === null)
  } catch {
    // never break Claude Code over the stage
  } finally {
    polling = false
  }
}

function stopAnimation(): void {
  animGen += 1
  try {
    animTimer?.cancel()
  } catch {
    // already gone
  }
  animTimer = undefined
  animBusy = false
}

async function tick($: EngineInterface, m: Mount, gen: number, count: number): Promise<void> {
  if (animBusy || gen !== animGen) return
  animBusy = true
  try {
    const shownAt = await read($, shownAtom)
    const now = await $.clock.now()
    const t = shownAt > 0 ? Math.max(0, (now - shownAt) / 1000) : 99
    const reply = await ask($, frameQuery(m, t), 'pane', FRAME_TIMEOUT_MS)
    if (gen !== animGen) return
    const f = reply?.status === 200 ? asFrame(reply.body) : undefined
    if (!f || f.cols !== m.cols || f.rows !== m.rows) {
      if (count > 40 || reply?.status === 204) stopAnimation()
      return
    }
    const blit = await $.ui.blit({ requestId: PANE, key: RASTER, cells: f.cells, columns: f.cols, rows: f.rows })
    if (gen !== animGen) return
    if ('deny' in blit || !f.animating || count > 400) {
      stopAnimation()
      // the drawing still holds its first frame: draw it again settled, so a re-mount never shows the intro's start
      $.ui.invalidate('ui.render')
    }
  } catch {
    stopAnimation()
  } finally {
    if (gen === animGen) animBusy = false
  }
}

function startAnimation($: EngineInterface, m: Mount): void {
  stopAnimation()
  const gen = animGen
  let count = 0
  try {
    animTimer = $.clock.every(FRAME_MS, () => {
      count += 1
      void tick($, m, gen, count)
    })
  } catch {
    animTimer = undefined
  }
}

function retrySoon($: EngineInterface): void {
  if (retryArmed) return
  retryArmed = true
  try {
    $.clock.after(1500, () => {
      retryArmed = false
      try {
        $.ui.invalidate('ui.render')
      } catch {
        // nothing to redraw
      }
    })
  } catch {
    retryArmed = false
  }
}

async function go($: EngineInterface, step: Step): Promise<void> {
  try {
    const scene = await read($, sceneAtom)
    const n = Math.max(1, scene?.slides ?? 1)
    const now = await $.clock.now()
    if (step === 'replay') {
      await update($, shownAtom, () => now)
      return
    }
    let moved = false
    await update($, slideAtom, cur => {
      const from = Math.max(0, Math.min(n - 1, cur ?? 0))
      let to = from
      if (step === 'first') to = 0
      else if (step === 'last') to = n - 1
      else to = Math.max(0, Math.min(n - 1, from + step))
      moved = to !== from
      return to
    })
    if (moved) await update($, shownAtom, () => now)
  } catch {
    // ignore
  }
}

async function jump($: EngineInterface, slide: number): Promise<void> {
  try {
    const scene = await read($, sceneAtom)
    const n = Math.max(1, scene?.slides ?? 1)
    const now = await $.clock.now()
    await update($, slideAtom, () => Math.max(0, Math.min(n - 1, slide)))
    await update($, shownAtom, () => now)
  } catch {
    // ignore
  }
}

async function dismiss($: EngineInterface): Promise<void> {
  try {
    stopAnimation()
    await update($, dismissedAtom, () => true)
    await $.ui.close({ id: PANE })
  } catch {
    // already closed
  }
}

async function start($: EngineInterface): Promise<void> {
  const base = loopback(await $.env.get('PULSAR_CORE'))
  let token = cleanToken(await $.env.get('PULSAR_SESSION_TOKEN'))
  if (!token) {
    const auth = cleanToken(await $.env.get('ANTHROPIC_AUTH_TOKEN'))
    token = auth && auth.startsWith('pls_') ? auth : undefined
  }
  if (!base || !token) return
  core = { base, token }
  await $.command.register({
    name: COMMAND,
    description: 'Show Pulsar Stage (slides, charts, diagrams) in a pane',
    argumentHint: '[next|prev|first|last|replay|close|<slide>]',
    immediate: true,
  })
  let ticks = 0
  $.clock.every(IDLE_MS, () => {
    ticks += 1
    // over curl (a process per request) the idle check runs at half the rate
    if (transport === 'curl' && ticks % 2 === 1) return
    void poll($)
  })
  void poll($)
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    try {
      await start($)
    } catch {
      core = undefined
    }
    return started
  })

  // The terminal says once per session whether it docks panes (fullscreen), and how wide it is: the
  // whole terminal while no pane is docked, the transcript's column while one is (then this pane is
  // placed and the width only matters again once it closes, when this redraws at the full width).
  on('ui.render', { component: 'AbovePrompt' }, ($, e, next) => {
    try {
      if (e.surface === 'terminal' && e.viewport) {
        if (typeof e.viewport.isFullscreen === 'boolean') isFullscreen = e.viewport.isFullscreen
        if (e.viewport.columns > 0) columns = e.viewport.columns
      }
    } catch {
      // a fact we can live without
    }
    return next(e)
  })

  on('command.run', { command: COMMAND }, async ($, e) => {
    if (!core) return { text: 'Pulsar Stage needs a Pulsar Code session (run pulsarcode).' }
    try {
      const shown = e.presentation as Partial<typeof e.presentation> | undefined
      if (typeof shown?.isFullscreen === 'boolean') isFullscreen = shown.isFullscreen
      const term = (shown?.columns ?? 0) > 0 ? (shown?.columns ?? 0) : columns
      const arg = (e.args ?? '').trim().toLowerCase()
      if (arg === 'close' || arg === 'off') {
        await dismiss($)
        return { text: 'Pulsar Stage pane closed.' }
      }
      await update($, dismissedAtom, () => false)
      if (arg === 'next' || arg === 'n') await go($, 1)
      else if (arg === 'prev' || arg === 'p' || arg === 'previous') await go($, -1)
      else if (arg === 'first') await go($, 'first')
      else if (arg === 'last') await go($, 'last')
      else if (arg === 'replay' || arg === 'r') await go($, 'replay')
      else if (/^\d{1,3}$/.test(arg)) await jump($, Number(arg) - 1)
      const placed = await openPane($, true, term)
      void poll($)
      if (!placed) return { text: 'Pulsar Stage pane is waiting for room on screen.' }
      return { text: 'Pulsar Stage pane is open: p/n slides, r replay, x close (ctrl+x tab focuses it).' }
    } catch {
      return { text: 'Pulsar Stage pane could not open here.' }
    }
  })

  on('ui.close', ($, e, next) => {
    try {
      if (e.id === PANE) {
        stopAnimation()
        if (e.origin.kind === 'person') {
          void update($, dismissedAtom, () => true)
          void update($, askedAtom, () => false) // a close by hand resets the engine's 110-column floor
        }
      }
    } catch {
      // ignore
    }
    return next(e)
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const scene = await read($, sceneAtom)
    const slide = await read($, slideAtom)
    const shownAt = await read($, shownAtom)
    const status = await read($, statusAtom)
    const width = Math.max(1, Math.min(512, Math.floor(e.props.bodyColumns || 80)))
    const bodyRows = Math.floor(e.props.scroll?.bodyRows ?? 0)
    const room = bodyRows > 2 ? bodyRows : Math.max(8, Math.floor((e.viewport?.rows ?? 30) / 3))
    const height = Math.max(1, Math.min(256, room - 1))
    const total = Math.max(1, scene?.slides ?? 1)
    const at = Math.max(0, Math.min(total - 1, slide))
    const icon = KIND_ICON[scene?.kind ?? ''] ?? '◉'
    const title = scene ? scene.title || scene.kind : ''
    // An unfocused pane (docked on its own, focus still on the prompt) hears no hotkeys: p, n and x would
    // type into the prompt. It says how to reach it instead of advertising keys that do not work yet.
    const focused = e.props.isFocused !== false

    const controls = (heading: string) => (
      <Box flexDirection="row" width={width} height={1}>
        <Box flexShrink={0}>
          <Text color={BEAM} bold>
            {'\u25c9 STAGE'}
          </Text>
        </Box>
        <Box flexGrow={1} flexShrink={1} marginLeft={2} overflow="hidden">
          {scene ? (
            <Text wrap="truncate-end">
              <Text color={PLASMA}>{`${icon} `}</Text>
              <Text color={ICE}>{heading}</Text>
            </Text>
          ) : (
            <Text color={DUST} wrap="truncate-end">
              {status || 'awaiting transmission'}
            </Text>
          )}
        </Box>
        {!focused ? (
          <Box flexShrink={0} marginLeft={3} marginRight={3} gap={1}>
            {scene && total > 1 ? (
              <Text>
                <Text color={FLARE} bold>{`${at + 1}`}</Text>
                <Text color={DUST}>{`/${total}`}</Text>
              </Text>
            ) : null}
            <Text color={DUST}>{FOCUS_HINT}</Text>
          </Box>
        ) : null}
        {focused && scene && total > 1 ? (
          <Box flexShrink={0} marginLeft={3} gap={1}>
            <Button key="prev" plain hotkey="p" label={'\u2039'} dimColor={at === 0} onPress={() => void go($, -1)} />
            <Text>
              <Text color={FLARE} bold>{`${at + 1}`}</Text>
              <Text color={DUST}>{`/${total}`}</Text>
            </Text>
            <Button key="next" plain hotkey="n" label={'\u203a'} dimColor={at >= total - 1} onPress={() => void go($, 1)} />
          </Box>
        ) : null}
        {focused && scene ? (
          <Box flexShrink={0} marginLeft={2}>
            <Button key="replay" plain hotkey="r" label="replay" dimColor onPress={() => void go($, 'replay')} />
          </Box>
        ) : null}
        {focused ? (
          <Box flexShrink={0} marginLeft={2} marginRight={3}>
            <Button key="close" plain hotkey="x" role="dismiss" label="close" dimColor onPress={() => void dismiss($)} />
          </Box>
        ) : null}
      </Box>
    )

    if (!core) {
      return (
        <Box flexDirection="column">
          {controls(title)}
          <Text color={DUST}>Pulsar Stage needs a Pulsar Code session.</Text>
        </Box>
      )
    }
    if (!scene) {
      stopAnimation()
      if (e.surface === 'terminal' && !status) {
        // the stage's own idle screen, drawn by Core like any slide
        const idle = await ask($, `/pulsar/stage/raster?idle=1&cols=${width}&rows=${height}`, 'pane', FRAME_TIMEOUT_MS)
        const f = idle?.status === 200 ? asFrame(idle.body) : undefined
        if (f) {
          const { Raster } = $.ui.resolve(e)
          return (
            <Box flexDirection="column">
              {controls(title)}
              <Raster key={RASTER} columns={f.cols} rows={f.rows} cells={f.cells} />
            </Box>
          )
        }
      }
      return (
        <Box flexDirection="column">
          {controls(title)}
          <Box flexDirection="column" paddingX={2} paddingY={1}>
            <Text color={ICE} bold>
              Awaiting transmission
            </Text>
            <Text color={ASH}>{status || 'Slides, charts and diagrams the model presents appear here, live.'}</Text>
            <Text color={DUST}>{'Try: \u201cpresent the architecture of this repo on Pulsar Stage\u201d'}</Text>
          </Box>
        </Box>
      )
    }
    if (e.surface !== 'terminal') {
      return (
        <Box flexDirection="column">
          {controls(title)}
          <Text color={ASH}>{`${title}: open the live stage with \`pulsarcode stage --web\`.`}</Text>
        </Box>
      )
    }

    const now = await $.clock.now()
    const t = shownAt > 0 ? Math.max(0, (now - shownAt) / 1000) : 99
    const m: Mount = { cols: width, rows: height, id: scene.id, slide: at }
    const reply = await ask($, frameQuery(m, t), 'pane', FRAME_TIMEOUT_MS)
    const f = reply?.status === 200 ? asFrame(reply.body) : undefined
    if (!f) {
      stopAnimation()
      retrySoon($)
      const why = reply?.status === 204 ? 'The stage was cleared.' : 'Waiting for Pulsar Core…'
      return (
        <Box flexDirection="column">
          {controls(title)}
          <Text color={EMBER}>{why}</Text>
        </Box>
      )
    }
    const { Raster } = $.ui.resolve(e)
    if (f.animating) startAnimation($, { cols: f.cols, rows: f.rows, id: scene.id, slide: f.slide })
    else stopAnimation()
    return (
      <Box flexDirection="column">
        {controls(f.slideTitle && f.slideTitle !== title ? `${title}  \u203a  ${f.slideTitle}` : title)}
        <Raster key={RASTER} columns={f.cols} rows={f.rows} cells={f.cells} />
      </Box>
    )
  })
}
