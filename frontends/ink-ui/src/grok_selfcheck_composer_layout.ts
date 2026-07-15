/**
 * 独立自测探针：content-desired 输入框贴内容 + slash 在 input 下。
 * 运行：npx tsx src/grok_selfcheck_composer_layout.ts
 */
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from './App.js'
import type { BridgeClient } from './bridgeClient.js'
import type { BridgeEvent } from './protocol.js'
import { inputChromeSections } from './inputLayout.js'
import { planMessageViewport } from './messageViewportPlan.js'
import { inputCursorPosition } from './terminalCursor.js'
import { computeLayoutMetrics } from './layoutMetrics.js'

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
  private readonly queue: string[] = []
  setRawMode() { return this }
  setEncoding() { return this }
  ref() { return this }
  unref() { return this }
  read() { return this.queue.shift() ?? null }
  resume() { return this }
  pause() { return this }
  send(text: string) {
    this.queue.push(text)
    this.emit('readable')
  }
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

function inputBorderRows(frame: string): number[] {
  return frame.split('\n')
    .map((line, index) => ({ index, line: line.trim() }))
    .filter(item => /^─+$/.test(item.line))
    .map(item => item.index)
}

function isLiveChromeFrame(chunk: string): boolean {
  return chunk.includes('GenericAgent')
    || chunk.includes('Enter send')
    || chunk.includes('Running ·')
    || chunk.includes('✻')
    || /─{8,}/.test(chunk)
}

function lastContentTopIndex(lines: string[]): number {
  return lines.findIndex(line => line.trim().length > 0)
}

async function waitForFrame(stdout: CaptureWriteStream, pred: (f: string) => boolean): Promise<string> {
  const deadline = Date.now() + 2500
  let last = ''
  while (Date.now() < deadline) {
    const frames = stdout.chunks.map(stripAnsi).filter(isLiveChromeFrame)
    last = frames.at(-1) ?? ''
    if (pred(last)) return last
    await delay(20)
  }
  return last
}


function emptyLiveGapAboveChrome(frame: string): number {
  const lines = frame.split('\n')
  const chromeIdx = lines.findIndex(line => (
    line.includes('Enter send')
    || line.includes('Running ·')
    || line.includes('✻')
  ))
  if (chromeIdx < 0) return Number.POSITIVE_INFINITY
  const activityIdx = Math.max(0, chromeIdx - 1)
  let empty = 0
  for (let row = activityIdx - 1; row >= 0; row -= 1) {
    if ((lines[row] ?? '').trim().length === 0) empty += 1
    else break
  }
  return empty
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

  console.log('=== 1) unit: planMessageViewport content-desired ===')
  const idle = planMessageViewport({ hasStaticMessages: true, liveLineCount: 0, messageRows: 17 })
  const stream = planMessageViewport({ hasStaticMessages: true, liveLineCount: 4, messageRows: 17 })
  const ready = planMessageViewport({ hasStaticMessages: false, liveLineCount: 0, messageRows: 17 })
  const capped = planMessageViewport({ hasStaticMessages: true, liveLineCount: 40, messageRows: 17, maxLiveRows: 12 })
  check('idle+static → none（贴内容，无满高 spacer）', idle.kind === 'none', JSON.stringify(idle))
  check('streaming height = live 行数', stream.kind === 'live' && stream.height === 4, JSON.stringify(stream))
  check('ready height = 1', ready.kind === 'ready' && ready.height === 1, JSON.stringify(ready))
  check('stream 触顶后 capped', capped.kind === 'live' && capped.height === 12, JSON.stringify(capped))
  check('idle 与 stream 不同高（废弃一期满高同高）', !(idle.kind !== 'none' && stream.kind !== 'none' && 'height' in idle && idle.height === stream.height))

  console.log('\n=== 2) unit: slash/panel 在 input 下 ===')
  const slashSecs = inputChromeSections({ hasError: false, hasPanel: false, hasSlashSuggestions: true })
  check('slash 在 input 后', slashSecs.indexOf('slashSuggestions') > slashSecs.indexOf('input'), slashSecs.join('>'))

  console.log('\n=== 3) unit: ready 光标（content-desired messageRows=1）===')
  const metrics = computeLayoutMetrics({
    rows: 24, columns: 80, hasActivity: false, hasError: false, hasPanel: false, hasSlashSuggestions: false,
    headerRows: 0,
  })
  const readyCursor = inputCursorPosition({
    headerRows: metrics.headerRows,
    messageRows: 1,
    activityRows: 1,
    errorRows: 0,
    panelRows: 0,
    hintRows: 1,
    inputBorderTopRows: 1,
    inputPaddingLeftColumns: 1,
    inputGutterColumns: 2,
    inputCursorLine: 0,
    inputCursorColumn: 0,
  })
  check('ready 光标 row=4 → CUP 5;4H (no header)', readyCursor.row === 4, `row=${readyCursor.row}`)

  console.log('\n=== 4) App: idle+Static 矮槽贴内容（非满高）===')
  delete process.env.GA_INK_MOUSE
  const bridge = { emit: null as null | ((e: BridgeEvent) => void) }
  const startBridgeClient = (
    _p: string,
    _s: string,
    onEvent: (e: BridgeEvent) => void,
  ): BridgeClient => {
    bridge.emit = onEvent
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
    debug: true,
  })

  try {
    const readyFrame = await waitForFrame(stdout, f => f.includes('>'))
    const readyGap = emptyLiveGapAboveChrome(readyFrame)
    const readyBorders = inputBorderRows(readyFrame)
    console.log(`ready: borders=${JSON.stringify(readyBorders)} emptyGap=${readyGap}`)
    check('ready live 槽矮（empty gap ≤2，非满高）', readyGap <= 2, `gap=${readyGap}`)

    if (!bridge.emit) throw new Error('bridge emit not ready')
    bridge.emit({ type: 'user', taskId: 1, text: '自测-composer-static-user' })
    bridge.emit({ type: 'assistant_done', taskId: 1, text: '自测-composer-static-assistant 长回答用于写入 Static。' })
    bridge.emit({ type: 'status', status: 'idle' })
    await delay(180)

    const idleFrame = await waitForFrame(stdout, f => (
      f.includes('>')
      && f.includes('Enter send')
      && !f.includes('Running ·')
    ))
    const idleGap = emptyLiveGapAboveChrome(idleFrame)
    const idleBorders = inputBorderRows(idleFrame)
    console.log(`idle+static: borders=${JSON.stringify(idleBorders)} emptyGap=${idleGap}`)
    // debug frames may include Static lines; measure empty gap above activity/hint only.
    check('idle+static live 槽矮（empty gap ≤1，贴内容）', idleGap <= 1, `gap=${idleGap}`)

    const combined = stdout.chunks.join('')
    // No status header: ready caret is row 5 (1-based CUP 5;4H).
    check('stdout 含 ready 光标 CUP ESC[5;4H', combined.includes('\x1b[5;4H'))

    console.log('\n=== 5) App: slash 在输入框边框下 ===')
    stdin.send('/')
    const slashFrame = await waitForFrame(stdout, f => f.includes('/help') || f.includes('/clear') || f.includes('/model'))
    const slashLines = slashFrame.split('\n')
    const slashBorders = inputBorderRows(slashFrame)
    console.log('--- slash frame tail ---')
    console.log(slashLines.slice(Math.max(0, slashLines.length - 16)).join('\n'))
    console.log('--- end ---')
    check('slash 帧有边框', slashBorders.length >= 2, `borders=${JSON.stringify(slashBorders)}`)
    if (slashBorders.length >= 2) {
      const bottomBorder = slashBorders[1]!
      const slashLine = slashLines.findIndex((line, i) => i > bottomBorder && /\/(help|clear|model|workflow)/.test(line))
      check('slash 在底边框下', slashLine > bottomBorder, `bottom=${bottomBorder} slash=${slashLine}`)
    }
  } finally {
    instance.unmount()
  }

  if (ok) {
    console.log('\nSELFCHECK PASS: content-desired 贴内容 + slash 在下 + ready 矮槽')
  } else {
    process.exitCode = 1
    console.error('\nSELFCHECK FAIL')
  }
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
