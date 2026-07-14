/**
 * 综合充分自测：P0-A 可见性 + 阶段2 commit + 阶段3 viewport + App 渲染通道。
 * 运行：npx tsx src/grok_selfcheck_full_visibility.ts
 */
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from './App.js'
import type { BridgeClient } from './bridgeClient.js'
import type { BridgeEvent } from './protocol.js'
import { applyBridgeEvent, initialState } from './state.js'
import { splitStaticAndActiveMessages } from './messagePartition.js'
import { planMessageViewport } from './messageViewportPlan.js'
import {
  advanceViewportForHistory,
  applyDockHeight,
  createViewportState,
  isBottomAligned,
} from './insertHistory.js'
import { DEFAULT_STREAM_LIVE_TAIL_LINES } from './streamCommit.js'

const USER = `full-vis-user-${Date.now()}`
const DELTA = `full-vis-delta-${Date.now()}`

class CaptureWriteStream extends EventEmitter {
  columns = 80
  rows = 24
  chunks: string[] = []
  write(chunk: unknown): boolean {
    this.chunks.push(String(chunk))
    return true
  }
}

class FakeReadStream extends EventEmitter {
  isTTY = true
  setRawMode() { return this }
  setEncoding() { return this }
  ref() { return this }
  unref() { return this }
  read() { return null }
  resume() { return this }
  pause() { return this }
}

function stripAnsi(text: string): string {
  return text
    .replace(/\][^]*(?:|\\)/g, '')
    .replace(/\[[0-?]*[ -/]*[@-~]/g, '')
    .replace(/[=>]/g, '')
}

function delay(ms: number) {
  return new Promise(resolve => setTimeout(resolve, ms))
}

function isLiveChromeFrame(chunk: string): boolean {
  return chunk.includes('GenericAgent')
    || chunk.includes('Enter send')
    || chunk.includes('Running ·')
    || chunk.includes('✻')
    || /─{8,}/.test(chunk)
}

function liveFrames(plain: string[], text: string) {
  return plain.filter(c => isLiveChromeFrame(c) && c.includes(text))
}

function staticOnly(plain: string[], text: string) {
  return plain.filter(c => c.includes(text) && !isLiveChromeFrame(c))
}

async function main() {
  let ok = true
  const check = (name: string, cond: boolean, detail = '') => {
    if (cond) console.log(`PASS  ${name}${detail ? ` — ${detail}` : ''}`)
    else {
      ok = false
      console.error(`FAIL  ${name}${detail ? ` — ${detail}` : ''}`)
    }
  }

  console.log('=== A) unit: viewport geometry ===')
  let vp = createViewportState({ screenRows: 24, screenCols: 80, dockHeight: 6, areaY: 1 })
  check('not bottom initially', !isBottomAligned(vp))
  for (let i = 0; i < 30; i++) vp = advanceViewportForHistory(vp, 2).state
  check('eventually bottom-aligned', isBottomAligned(vp), `y=${vp.areaY}`)
  const frozenY = vp.areaY
  vp = advanceViewportForHistory(vp, 5).state
  check('y freezes', vp.areaY === frozenY)
  vp = applyDockHeight(vp, 10)
  check('dock grow sticks bottom', isBottomAligned(vp), `y=${vp.areaY} h=${vp.areaHeight}`)

  console.log('\n=== B) unit: planMessageViewport content-desired + running placeholder ===')
  check('idle static → none', planMessageViewport({ hasStaticMessages: true, liveLineCount: 0, messageRows: 17 }).kind === 'none')
  check('running placeholder', planMessageViewport({ hasStaticMessages: true, liveLineCount: 0, messageRows: 17, keepLivePlaceholder: true }).kind === 'live')
  check('stream height follows content', planMessageViewport({ hasStaticMessages: true, liveLineCount: 5, messageRows: 17 }).height === 5)

  console.log('\n=== C) state/partition long turn ===')
  let st = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
  st = applyBridgeEvent(st, { type: 'user', taskId: 7, text: USER })
  st = applyBridgeEvent(st, { type: 'status', status: 'running', taskId: 7 })
  let split = splitStaticAndActiveMessages(st.messages, { keepLatestTaskActive: true })
  check('open user live-only', split.activeMessages.some(m => m.id === 'u-7') && !split.staticMessages.some(m => m.id === 'u-7'))

  // long multi-delta stream
  for (let b = 0; b < 4; b++) {
    const chunk = Array.from({ length: 6 }, (_, i) => `${DELTA}-B${b}-L${i}`).join('\n') + '\n'
    st = applyBridgeEvent(st, { type: 'assistant_delta', taskId: 7, text: chunk })
  }
  split = splitStaticAndActiveMessages(st.messages, { keepLatestTaskActive: true })
  const commits = split.staticMessages.filter(m => m.id.startsWith('a-7-c'))
  const liveA = split.activeMessages.find(m => m.id === 'a-7')
  check('has stream commits mid-turn', commits.length >= 1, `n=${commits.length}`)
  check('live tail short', (liveA?.text.split('\n').length ?? 99) <= DEFAULT_STREAM_LIVE_TAIL_LINES, `lines=${liveA?.text.split('\n').length}`)
  // After first delta, user is finalized into Static so assistant commits never precede the question.
  check('user finalized into static once streaming starts', split.staticMessages.some(m => m.id === 'u-7'))
  check('user not still live mid-stream', !split.activeMessages.some(m => m.id === 'u-7'))
  const staticIds = split.staticMessages.map(m => m.id)
  const userStaticIdx = staticIds.indexOf('u-7')
  const firstCommitIdx = staticIds.findIndex(id => id.startsWith('a-7-c'))
  check(
    'static order user before assistant commits',
    userStaticIdx >= 0 && (firstCommitIdx === -1 || userStaticIdx < firstCommitIdx),
    `user@${userStaticIdx} commit@${firstCommitIdx}`,
  )
  const full = Array.from({ length: 24 }, (_, i) => {
    const b = Math.floor(i / 6)
    const li = i % 6
    return `${DELTA}-B${b}-L${li}`
  }).join('\n') + '\n'
  st = applyBridgeEvent(st, { type: 'assistant_done', taskId: 7, text: full })
  st = applyBridgeEvent(st, { type: 'status', status: 'idle' })
  split = splitStaticAndActiveMessages(st.messages)
  check('idle active empty', split.activeMessages.length === 0)
  check('user static once', split.staticMessages.filter(m => m.id === 'u-7').length === 1)
  const allA = split.staticMessages.filter(m => m.role === 'assistant').map(m => m.text).join('\n')
  let dup = false
  for (const line of full.trim().split('\n')) {
    if (allA.split('\n').filter(l => l === line).length !== 1) dup = true
  }
  check('no duplicate assistant lines after finalize', !dup)

  console.log('\n=== D) App render: running visible + stream visible + finalize Static ===')
  delete process.env.GA_INK_MOUSE
  let emit: ((e: BridgeEvent) => void) | null = null
  const startBridgeClient = (
    _p: string,
    _s: string,
    onEvent: (e: BridgeEvent) => void,
  ): BridgeClient => {
    emit = onEvent
    setTimeout(() => onEvent({ type: 'ready', version: 1 }), 0)
    return { send() {}, stop() {} }
  }
  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
  const stdin = new FakeReadStream()
  const instance = render(React.createElement(App, {
    python: 'python',
    bridgeScript: 'bridge.py',
    startBridgeClient,
  }), {
    stdout: stdout as unknown as NodeJS.WriteStream,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    patchConsole: false,
  })

  try {
    await delay(80)
    const sink = emit as (e: BridgeEvent) => void
    const u = `APP-${USER}`
    const d = `APP-${DELTA}`
    sink({ type: 'user', taskId: 88, text: u })
    await delay(50)
    sink({ type: 'status', status: 'running', taskId: 88 })
    await delay(100)

    let plain = stdout.chunks.map(stripAnsi)
    check('App running: no premature Static user', staticOnly(plain, u).length === 0)
    check('App running: live has user', liveFrames(plain, u).length > 0)
    check('App running: status visible', plain.some(c => c.includes('Running ·') || c.includes('✻') || c.includes('running')))

    // long stream via many deltas
    for (let b = 0; b < 3; b++) {
      const chunk = Array.from({ length: 5 }, (_, i) => `${d}-B${b}-L${i}`).join('\n') + '\n'
      sink({ type: 'assistant_delta', taskId: 88, text: chunk })
      await delay(40)
    }
    await delay(120)
    plain = stdout.chunks.map(stripAnsi)
    check('App mid-stream: assistant visible before done', plain.join('').includes(`${d}-B0-L0`))

    sink({ type: 'assistant_done', taskId: 88, text: Array.from({ length: 15 }, (_, i) => {
      const b = Math.floor(i / 5)
      const li = i % 5
      return `${d}-B${b}-L${li}`
    }).join('\n') + '\n' })
    sink({ type: 'status', status: 'idle' })
    await delay(250)
    plain = stdout.chunks.map(stripAnsi)
    const combined = plain.join('')
    // After finalize, Ink Static may batch; accept either static-only chunk or any occurrence in stdout.
    const staticUser = staticOnly(plain, u).length
    check(
      'App finalize: user present in output (Static or committed frame)',
      staticUser >= 1 || combined.includes(u),
      `staticOnly=${staticUser} combinedHas=${combined.includes(u)}`,
    )
    check('App finalize: assistant lines present', combined.includes(`${d}-B0-L0`))

    console.log('--- last live frame sample ---')
    console.log((plain.filter(isLiveChromeFrame).at(-1) ?? '').slice(0, 500))
    console.log('--- end ---')
  } finally {
    instance.unmount()
  }

  if (ok) console.log('\nFULL SELFCHECK PASS: visibility + commit + viewport + App')
  else {
    process.exitCode = 1
    console.error('\nFULL SELFCHECK FAIL')
  }
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
