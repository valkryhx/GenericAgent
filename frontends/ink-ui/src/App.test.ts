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
    const frames = stdout.chunks.map(stripAnsi).filter(chunk => chunk.includes('GenericAgent'))
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
  return stdout.chunks.slice(start).map(stripAnsi).filter(chunk => chunk.includes('GenericAgent'))
}

function inputBorderRows(frame: string): number[] {
  return frame.split('\n')
    .map((line, index) => ({ index, line: line.trim() }))
    .filter(item => /^─+$/.test(item.line))
    .map(item => item.index)
}

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
    let expectedBorders: number[] | null = null
    const streamingFrames: string[] = []
    for (const text of [
      `第一段 ${'中文'.repeat(20)}`,
      `https://example.test/${'path/'.repeat(20)}`,
      `👨‍💻 e\u0301 **streaming** ${'abcdef'.repeat(20)}`,
    ]) {
      const frameStart = stdout.chunks.length
      eventSink({ type: 'assistant_delta', taskId: 1, text })
      await delay(120)
      const frame = completeFrames(stdout, frameStart).at(-1)
      if (frame) {
        streamingFrames.push(frame)
        expectedBorders ??= inputBorderRows(frame)
      }
    }

    assert.equal(streamingFrames.length, 3)
    assert.ok(expectedBorders)
    for (const frame of streamingFrames) {
      assert.deepEqual(inputBorderRows(frame), expectedBorders)
      assert.equal(frame.split('\n').every(line => stringWidth(line) < 40), true)
    }
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

    assert.match(stdout.chunks.join(''), /\x1b\[6;4H/)
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
    const parkedCaret = combined.indexOf('\x1b[6;4H')
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
    await waitForFrame(stdout, frame => frame.includes('GenericAgent'))
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
    assert.match(combined, /\u001B\[0m\u001B\[(?:5|7)A\r/)
    assert.ok((combined.match(/\u001B\[2K/g) ?? []).length >= 7)
    assert.match(combined, /\u001B\[6A\r/)
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
    await waitForOutput(stdout, output => output.includes('静态回答') && output.includes('活动问题'))
    const chunks = stdout.chunks.map(stripAnsi)
    const staticChunk = chunks.find(chunk => chunk.includes('静态问题') || chunk.includes('静态回答'))
    const liveFrame = chunks.filter(chunk => chunk.includes('GenericAgent')).at(-1) ?? ''

    assert.ok(staticChunk)
    assert.equal(staticChunk.includes('GenericAgent'), false)
    assert.doesNotMatch(liveFrame, /静态问题/)
    assert.doesNotMatch(liveFrame, /静态回答/)
    assert.match(liveFrame, /活动问题/)
  } finally {
    instance.unmount()
    timers.forEach(timer => clearTimeout(timer))
  }
})

test('App help documents workflow plan slash command entry', () => {
  assert.match(helpText(), /\/workflow plan \[--manual\] \[--timeout SECONDS\] TASK - plan and run a dynamic workflow/)
})
