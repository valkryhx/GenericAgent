import test from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import stringWidth from 'string-width'
import { App, helpText } from './App.js'
import type { BridgeClient } from './bridgeClient.js'
import type { BridgeEvent } from './protocol.js'

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

  setRawMode(): this {
    return this
  }

  setEncoding(): this {
    return this
  }

  ref(): this {
    return this
  }

  unref(): this {
    return this
  }

  read(): string | null {
    return this.queue.shift() ?? null
  }

  resume(): this {
    return this
  }

  pause(): this {
    return this
  }

  send(text: string): void {
    this.queue.push(text)
    this.emit('readable')
  }
}

function stripAnsi(text: string): string {
  return text
    .replace(/\u001B\][^\u0007]*(?:\u0007|\u001B\\)/g, '')
    .replace(/\u001B\[[0-?]*[ -/]*[@-~]/g, '')
    .replace(/\u001B[=>]/g, '')
}

function delay(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}

async function waitForFrame(stdout: CaptureWriteStream, predicate: (frame: string) => boolean): Promise<string> {
  const deadline = Date.now() + 2000
  let lastFrame = ''
  while (Date.now() < deadline) {
    // Live frames used to always include a "GenericAgent" header; default inline
    // mode no longer paints that header, so detect chrome via composer/hint instead.
    const frames = stdout.chunks.map(stripAnsi).filter(chunk => (
      chunk.includes('GenericAgent')
      || chunk.includes('Enter send')
      || chunk.includes('Running ·')
      || /─{8,}/.test(chunk)
    ))
    lastFrame = frames.at(-1) ?? ''
    if (predicate(lastFrame)) return lastFrame
    await delay(20)
  }
  return lastFrame
}

async function waitForOutput(stdout: CaptureWriteStream, predicate: (output: string) => boolean): Promise<string> {
  const deadline = Date.now() + 2000
  let output = ''
  while (Date.now() < deadline) {
    output = stdout.chunks.map(stripAnsi).join('')
    if (predicate(output)) return output
    await delay(20)
  }
  return output
}

function readyBridge(
  _python: string,
  _bridgeScript: string,
  onEvent: (event: BridgeEvent) => void,
): BridgeClient {
  setTimeout(() => onEvent({ type: 'ready', version: 1 }), 0)
  return { send() {}, stop() {} }
}

function completeFrames(stdout: CaptureWriteStream, start = 0): string[] {
  return stdout.chunks.slice(start).map(stripAnsi).filter(chunk => (
    chunk.includes('GenericAgent')
    || chunk.includes('Enter send')
    || chunk.includes('Running ·')
    || /─{8,}/.test(chunk)
  ))
}

function inputBorderRows(frame: string): number[] {
  return frame.split('\n')
    .map((line, index) => ({ index, line: line.trim() }))
    .filter(item => /^─+$/.test(item.line))
    .map(item => item.index)
}

/** Empty live rows between last non-chrome content and the activity/hint chrome. */
function emptyLiveGapAboveChrome(frame: string): number {
  const lines = frame.split('\n')
  const chromeIdx = lines.findIndex(line => (
    line.includes('Enter send')
    || line.includes('Running ·')
    || line.includes('✻')
  ))
  if (chromeIdx < 0) return Number.POSITIVE_INFINITY
  // activity placeholder is usually one row above the hint line
  const activityIdx = Math.max(0, chromeIdx - 1)
  let empty = 0
  for (let row = activityIdx - 1; row >= 0; row -= 1) {
    if ((lines[row] ?? '').trim().length === 0) empty += 1
    else break
  }
  return empty
}

test('App default inline mode does not paint a GenericAgent status header above the transcript', async () => {
  // Status belongs in the bottom activity chrome; a live header after <Static>
  // looks like "GenericAgent running" jammed into the middle of output.
  delete process.env.GA_INK_MOUSE
  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
  const stdin = new FakeReadStream()
  const instance = render(React.createElement(App, {
    python: 'python',
    bridgeScript: 'bridge.py',
    startBridgeClient: readyBridge,
  }), {
    stdout: stdout as unknown as NodeJS.WriteStream,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    patchConsole: false,
  })
  try {
    await waitForFrame(stdout, frame => frame.includes('>'))
    const live = stdout.chunks.map(stripAnsi).filter(c => c.includes('Enter send') || c.includes('>')).at(-1) ?? ''
    assert.doesNotMatch(live, /GenericAgent\s+(idle|running|connecting)/)
  } finally {
    instance.unmount()
  }
})

test('App soft-wraps long input without writing into the physical final column', async () => {
  const longInput = `${'0123456789'.repeat(13)}尾部`

  for (const columns of [40, 80, 120]) {
    const stdout = new CaptureWriteStream()
    stdout.columns = columns
    const stderr = new CaptureWriteStream()
    stderr.columns = columns
    const stdin = new FakeReadStream()
    const instance = render(React.createElement(App, {
      python: 'python',
      bridgeScript: 'bridge.py',
      startBridgeClient: readyBridge,
    }), {
      stdout: stdout as unknown as NodeJS.WriteStream,
      stderr: stderr as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      patchConsole: false,
    })

    try {
      await waitForFrame(stdout, frame => frame.includes('>'))
      stdin.send(longInput)
      const frame = await waitForFrame(stdout, value => value.includes('0123456789'))
      const inputRows = frame.split('\n').filter(line => /0123456789|尾部/.test(line))

      assert.equal(frame.split('\n').every(line => stringWidth(line) < columns), true)
      assert.match(frame, /尾部/)
      assert.ok(inputRows.length >= 2)
      if (columns === 80) assert.match(frame, /Enter send · Alt\+Enter newline · PgUp\/PgDn · Ctrl\+O tools · Ctrl\+C exit/)
    } finally {
      instance.unmount()
    }
  }
})

test('App renders one continuous safe-canvas scrollbar cell per message viewport row', async () => {
  const previousMouseMode = process.env.GA_INK_MOUSE
  process.env.GA_INK_MOUSE = 'full'
  const timers: NodeJS.Timeout[] = []
  const startBridgeClient = (
    _python: string,
    _bridgeScript: string,
    onEvent: (event: BridgeEvent) => void,
  ): BridgeClient => {
    timers.push(setTimeout(() => {
      onEvent({ type: 'ready', version: 1 })
      for (let index = 1; index <= 12; index += 1) {
        onEvent({ type: 'user', taskId: index, text: `问题 ${index} ${'中文'.repeat(10)}` })
        onEvent({ type: 'assistant_done', taskId: index, text: `回答 ${index} ${'abcdefghijklmnopqrstuvwxyz'.repeat(3)}` })
      }
    }, 0))
    return { send() {}, stop() { timers.forEach(timer => clearTimeout(timer)) } }
  }
  const stdout = new CaptureWriteStream()
  stdout.columns = 40
  const stderr = new CaptureWriteStream()
  stderr.columns = 40
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
    const frame = await waitForFrame(stdout, value => value.includes('▐') && value.includes('回答 12'))
    const messageRows = frame.split('\n').slice(1, 18)

    assert.equal(messageRows.length, 17)
    assert.equal(messageRows.every(line => /▐$/.test(line)), true)
    assert.equal(frame.split('\n').every(line => stringWidth(line) < stdout.columns), true)
  } finally {
    instance.unmount()
    timers.forEach(timer => clearTimeout(timer))
    if (previousMouseMode === undefined) {
      delete process.env.GA_INK_MOUSE
    } else {
      process.env.GA_INK_MOUSE = previousMouseMode
    }
  }
})

test('App reflows long input when the terminal is resized', async () => {
  const longInput = `${'0123456789'.repeat(13)}尾部`
  const stdout = new CaptureWriteStream()
  stdout.columns = 80
  const stderr = new CaptureWriteStream()
  stderr.columns = 80
  const stdin = new FakeReadStream()
  const instance = render(React.createElement(App, {
    python: 'python',
    bridgeScript: 'bridge.py',
    startBridgeClient: readyBridge,
  }), {
    stdout: stdout as unknown as NodeJS.WriteStream,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    patchConsole: false,
    debug: true,
  })

  try {
    await waitForFrame(stdout, frame => frame.includes('>'))
    stdin.send(longInput)
    const before = await waitForFrame(stdout, frame => frame.includes('尾部'))
    const beforeRows = before.split('\n').filter(line => /0123456789|尾部/.test(line)).length
    const resizeChunk = stdout.chunks.length

    stdout.columns = 40
    stderr.columns = 40
    stdout.emit('resize')
    await delay(200)
    const after = completeFrames(stdout, resizeChunk).at(-1) ?? ''
    const afterRows = after.split('\n').filter(line => /0123456789|尾部/.test(line)).length

    assert.ok(after)
    assert.match(after, /尾部/)
    assert.ok(afterRows > beforeRows)
    assert.equal(after.split('\n').every(line => stringWidth(line) < 40), true)
  } finally {
    instance.unmount()
  }
})

test('App keeps input chrome fixed while long mixed-width output streams', async () => {
  let emit: ((event: BridgeEvent) => void) | null = null
  const startBridgeClient = (
    _python: string,
    _bridgeScript: string,
    onEvent: (event: BridgeEvent) => void,
  ): BridgeClient => {
    emit = onEvent
    setTimeout(() => onEvent({ type: 'ready', version: 1 }), 0)
    return { send() {}, stop() {} }
  }
  const stdout = new CaptureWriteStream()
  stdout.columns = 40
  const stderr = new CaptureWriteStream()
  stderr.columns = 40
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
    await waitForFrame(stdout, frame => frame.includes('>'))
    const eventSink = emit as unknown as (event: BridgeEvent) => void

    eventSink({ type: 'status', status: 'running', taskId: 1 })
    eventSink({ type: 'user', taskId: 1, text: '保持输入区稳定' })
    const streamingFrames: string[] = []
    for (const text of [
      `第一段 ${'中文'.repeat(20)}`,
      `https://example.test/${'path/'.repeat(20)}`,
      `👨‍💻 é **streaming** ${'abcdef'.repeat(20)}`,
    ]) {
      const frameStart = stdout.chunks.length
      eventSink({ type: 'assistant_delta', taskId: 1, text })
      await delay(120)
      const frame = completeFrames(stdout, frameStart).at(-1)
      if (frame) streamingFrames.push(frame)
    }

    assert.equal(streamingFrames.length, 3)
    // Content-desired: live height may grow with stream lines (Codex-like), so absolute
    // border row indices can increase — but the input chrome block size (border pair gap)
    // stays 2, and lines stay within the safe canvas width.
    for (const frame of streamingFrames) {
      const borders = inputBorderRows(frame)
      assert.ok(borders.length >= 2)
      assert.equal(borders[1]! - borders[0]!, 2)
      assert.equal(frame.split('\n').every(line => stringWidth(line) < 40), true)
    }
    const lastBorders = inputBorderRows(streamingFrames.at(-1)!)
    const midBorders = inputBorderRows(streamingFrames[1]!)
    assert.ok(lastBorders[0]! >= midBorders[0]!)
  } finally {
    instance.unmount()
  }
})

test('App appends a resumed history replacement into static terminal scrollback', async () => {
  const timers: NodeJS.Timeout[] = []
  const startBridgeClient = (
    _python: string,
    _bridgeScript: string,
    onEvent: (event: BridgeEvent) => void,
  ): BridgeClient => {
    const emitLater = (delayMs: number, event: BridgeEvent) => {
      timers.push(setTimeout(() => onEvent(event), delayMs))
    }
    emitLater(0, { type: 'ready', version: 1 })
    for (let index = 1; index <= 12; index++) {
      emitLater(20, { type: 'user', taskId: index, text: `当前问题 ${index}` })
      emitLater(20, { type: 'assistant_done', taskId: index, text: `当前回答 ${index}` })
    }
    timers.push(setTimeout(() => {
      const messages: BridgeEvent & { type: 'history_replace' } = {
        type: 'history_replace',
        messages: [],
      }
      for (let index = 1; index <= 12; index++) {
        messages.messages.push({ role: 'user', taskId: index, text: index === 1 ? '介绍美国' : `恢复问题 ${index}` })
        messages.messages.push({ role: 'assistant', taskId: index, text: `恢复回答 ${index}` })
      }
      onEvent(messages)
      onEvent({ type: 'system', text: '✅ 已恢复 12 轮结构化会话（session.json）\n(已写入 backend.history，可直接继续)' })
    }, 90))
    return {
      send() {},
      stop() {
        timers.forEach(timer => clearTimeout(timer))
      },
    }
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
    const output = await waitForOutput(stdout, value => value.includes('介绍美国') && value.includes('恢复完成：12 轮历史 · session.json'))
    const chunks = stdout.chunks.map(stripAnsi)
    const staticChunk = chunks.find(chunk => chunk.includes('介绍美国'))

    assert.match(output, /介绍美国/)
    assert.match(output, /恢复完成：12 轮历史 · session\.json/)
    assert.ok(staticChunk)
  } finally {
    instance.unmount()
    timers.forEach(timer => clearTimeout(timer))
  }
})

test('App replays a restored history replacement when the visible row count stays the same', async () => {
  const timers: NodeJS.Timeout[] = []
  const startBridgeClient = (
    _python: string,
    _bridgeScript: string,
    onEvent: (event: BridgeEvent) => void,
  ): BridgeClient => {
    timers.push(setTimeout(() => onEvent({ type: 'ready', version: 1 }), 0))
    timers.push(setTimeout(() => {
      const messages: BridgeEvent & { type: 'history_replace' } = {
        type: 'history_replace',
        messages: [],
      }
      for (let index = 1; index <= 12; index++) {
        messages.messages.push({ role: 'user', taskId: index, text: index === 1 ? '介绍美国' : `恢复问题 ${index}` })
        messages.messages.push({ role: 'assistant', taskId: index, text: `恢复回答 ${index}` })
      }
      onEvent(messages)
      onEvent({ type: 'system', text: '✅ 已恢复 12 轮结构化会话（session.json）\n(已写入 backend.history，可直接继续)' })
    }, 50))
    return {
      send() {},
      stop() {
        timers.forEach(timer => clearTimeout(timer))
      },
    }
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
    const output = await waitForOutput(stdout, value => value.includes('介绍美国') && value.includes('恢复完成：12 轮历史 · session.json'))

    assert.match(output, /介绍美国/)
    assert.match(output, /恢复完成：12 轮历史 · session\.json/)
  } finally {
    instance.unmount()
    timers.forEach(timer => clearTimeout(timer))
  }
})

test('App parks the native terminal cursor on the visible input caret for IME', async () => {
  const startBridgeClient = (
    _python: string,
    _bridgeScript: string,
    onEvent: (event: BridgeEvent) => void,
  ): BridgeClient => {
    setTimeout(() => onEvent({ type: 'ready', version: 1 }), 0)
    return {
      send() {},
      stop() {},
    }
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
    await waitForFrame(stdout, frame => frame.includes('>'))

    // Content-desired ready (height=1), no header: activity+hint+border = row 5 (1-based).
    assert.match(stdout.chunks.join(''), /\x1b\[5;4H/)
  } finally {
    instance.unmount()
  }
})

test('App restores the renderer cursor before redrawing after parking the input caret', async () => {
  const startBridgeClient = (
    _python: string,
    _bridgeScript: string,
    onEvent: (event: BridgeEvent) => void,
  ): BridgeClient => {
    setTimeout(() => onEvent({ type: 'ready', version: 1 }), 0)
    return {
      send() {},
      stop() {},
    }
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
    await waitForFrame(stdout, frame => frame.includes('>'))
    stdin.send('x')
    await waitForFrame(stdout, frame => frame.includes('> x'))

    const combined = stdout.chunks.join('')
    // Cursor parks on content-desired ready caret row without header (messageRows live = 1).
    const parkedCaret = combined.indexOf('\x1b[5;4H')
    assert.notEqual(parkedCaret, -1)
    const nextInkErase = combined.indexOf('\x1b[2K', parkedCaret + 1)
    assert.notEqual(nextInkErase, -1)
    const between = combined.slice(parkedCaret, nextInkErase)
    assert.match(between, /\x1b\[u/)
  } finally {
    instance.unmount()
  }
})

test('App does not enter the alternate screen before the first Ink frame is written', async () => {
  const timers: NodeJS.Timeout[] = []
  const startBridgeClient = (
    _python: string,
    _bridgeScript: string,
    onEvent: (event: BridgeEvent) => void,
  ): BridgeClient => {
    timers.push(setTimeout(() => onEvent({ type: 'ready', version: 1 }), 20))
    return {
      send() {},
      stop() {
        timers.forEach(timer => clearTimeout(timer))
      },
    }
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
    await waitForFrame(stdout, frame => frame.includes('>') || frame.includes('Enter send'))
    const combined = stdout.chunks.join('')
    const alternateScreenIndex = combined.indexOf('\u001B[?1049h')

    assert.equal(alternateScreenIndex, -1)
  } finally {
    instance.unmount()
    timers.forEach(timer => clearTimeout(timer))
  }
})

test('App clears the inline live input block before exiting on Ctrl+C', async () => {
  const sentCommands: unknown[] = []
  const startBridgeClient = (
    _python: string,
    _bridgeScript: string,
    onEvent: (event: BridgeEvent) => void,
  ): BridgeClient => {
    setTimeout(() => onEvent({ type: 'ready', version: 1 }), 0)
    return {
      send(command) {
        sentCommands.push(command)
      },
      stop() {},
    }
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
    await waitForFrame(stdout, frame => frame.includes('>'))
    stdin.send('你好')
    await waitForFrame(stdout, frame => frame.includes('你好'))
    stdin.send('\u0003')
    await delay(50)

    const combined = stdout.chunks.join('')
    assert.deepEqual(sentCommands.at(-1), { type: 'shutdown' })
    // No header: liveViewportRows = msg(1)+bottom(5) = 6.
    // Parked caret ~row 4; unparked uses rows as cursorRow → 6A up, 6×2K, 5A back.
    assert.match(combined, /\u001B\[0m\u001B\[(?:4|6)A\r/)
    assert.ok((combined.match(/\u001B\[2K/g) ?? []).length >= 6)
    assert.match(combined, /\u001B\[5A\r/)
  } finally {
    instance.unmount()
  }
})

test('App writes completed transcript to static terminal scrollback instead of the live viewport', async () => {
  const timers: NodeJS.Timeout[] = []
  const startBridgeClient = (
    _python: string,
    _bridgeScript: string,
    onEvent: (event: BridgeEvent) => void,
  ): BridgeClient => {
    timers.push(setTimeout(() => {
      onEvent({ type: 'ready', version: 1 })
      onEvent({ type: 'user', taskId: 1, text: '静态问题' })
      onEvent({ type: 'assistant_done', taskId: 1, text: '静态回答' })
      onEvent({ type: 'status', status: 'running', taskId: 2 })
      onEvent({ type: 'user', taskId: 2, text: '活动问题' })
    }, 0))
    return {
      send() {},
      stop() {
        timers.forEach(timer => clearTimeout(timer))
      },
    }
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
    // P0-A: completed turn commits to Static; open-turn user stays in live only (single channel).
    await waitForOutput(stdout, output => output.includes('静态回答') && output.includes('活动问题'))
    const chunks = stdout.chunks.map(stripAnsi)
    const staticOutput = chunks.filter(chunk => !chunk.includes('活动问题') && (chunk.includes('静态问题') || chunk.includes('静态回答'))).join('\n')
    const liveFrame = chunks.filter(chunk => chunk.includes('活动问题') || chunk.includes('Running ·') || chunk.includes('running')).at(-1) ?? ''

    assert.match(staticOutput, /静态问题/)
    assert.match(staticOutput, /静态回答/)
    assert.doesNotMatch(staticOutput, /活动问题/)
    assert.doesNotMatch(liveFrame, /静态问题/)
    assert.doesNotMatch(liveFrame, /静态回答/)
    assert.match(liveFrame, /活动问题/)
  } finally {
    instance.unmount()
    timers.forEach(timer => clearTimeout(timer))
  }
})


test('App places slash suggestions below the input frame', async () => {
  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
  const stdin = new FakeReadStream()
  const instance = render(React.createElement(App, {
    python: 'python',
    bridgeScript: 'bridge.py',
    startBridgeClient: readyBridge,
  }), {
    stdout: stdout as unknown as NodeJS.WriteStream,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    patchConsole: false,
  })

  try {
    await waitForFrame(stdout, frame => frame.includes('>'))
    stdin.send('/')
    const frame = await waitForFrame(stdout, f => f.includes('/help') || f.includes('/clear') || f.includes('/model'))
    const lines = frame.split('\n')
    const borders = inputBorderRows(frame)
    assert.ok(borders.length >= 2, `input borders missing: ${frame.slice(0, 500)}`)
    // Composer body sits between the two border rows; slash popup must be after the bottom border.
    const inputLine = borders[0] + 1
    const bottomBorder = borders[1]
    const slashLine = lines.findIndex((line, index) => index > bottomBorder && /\/(help|clear|model|workflow)/.test(line))
    assert.ok(slashLine > bottomBorder, `expected slash below input border@${bottomBorder}, got slash@${slashLine}; frame=${frame.slice(0, 500)}`)
    assert.ok(inputLine < slashLine)
  } finally {
    instance.unmount()
  }
})

test('App sticks composer near static history without a full-height empty live slot', async () => {
  const bridge = { emit: null as null | ((event: BridgeEvent) => void) }
  const startBridgeClient = (
    _python: string,
    _bridgeScript: string,
    onEvent: (event: BridgeEvent) => void,
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
    const readyFrame = await waitForFrame(stdout, frame => frame.includes('>'))
    // Ready: compact 1-row cue only — no full-height empty live spacer.
    assert.ok(
      emptyLiveGapAboveChrome(readyFrame) <= 2,
      `ready live slot still full-height overfix: gap=${emptyLiveGapAboveChrome(readyFrame)}`,
    )
    const readyBorders = inputBorderRows(readyFrame)
    assert.ok(readyBorders.length >= 2)

    if (!bridge.emit) throw new Error('bridge emit not ready')
    bridge.emit({ type: 'user', taskId: 1, text: '长历史锚点探针' })
    bridge.emit({ type: 'assistant_done', taskId: 1, text: '这是一段足够长的助手回答，用来写入 Static scrollback。' })
    bridge.emit({ type: 'status', status: 'idle' })
    await delay(150)

    // Static history is not part of live frames; wait for idle composer chrome only.
    // debug:true may still include Static lines in the same frame, so measure the empty
    // gap above activity/hint chrome rather than first-non-empty → border.
    const idleFrame = await waitForFrame(stdout, frame => (
      frame.includes('>')
      && frame.includes('Enter send')
      && !frame.includes('Running ·')
    ))
    const idleBorders = inputBorderRows(idleFrame)
    assert.ok(idleBorders.length >= 2)
    const idleGap = emptyLiveGapAboveChrome(idleFrame)
    // Idle + Static: live kind none → no tall empty live gap above chrome.
    assert.ok(idleGap <= 1, `idle still has tall empty live gap: ${idleGap}`)
  } finally {
    instance.unmount()
  }
})

test('App help documents workflow plan slash command entry', () => {
  assert.match(helpText(), /\/workflow plan \[--manual\] \[--timeout SECONDS\] TASK - plan and run a dynamic workflow/)
})
