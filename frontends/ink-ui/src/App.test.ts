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
    const frame = await waitForFrame(stdout, value => value.includes('█') && value.includes('回答 12'))
    const messageRows = frame.split('\n').slice(1, 18)

    assert.equal(messageRows.length, 17)
    assert.equal(messageRows.every(line => /[│█]$/.test(line)), true)
    assert.equal(frame.split('\n').every(line => stringWidth(line) < stdout.columns), true)
  } finally {
    instance.unmount()
    timers.forEach(timer => clearTimeout(timer))
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
    const idleFrame = await waitForFrame(stdout, frame => frame.includes('>'))
    const expectedBorders = inputBorderRows(idleFrame)
    assert.equal(expectedBorders.length, 2)
    const eventSink = emit as unknown as (event: BridgeEvent) => void

    eventSink({ type: 'status', status: 'running', taskId: 1 })
    eventSink({ type: 'user', taskId: 1, text: '保持输入区稳定' })
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
      if (frame) streamingFrames.push(frame)
    }

    assert.equal(streamingFrames.length, 3)
    for (const frame of streamingFrames) {
      assert.deepEqual(inputBorderRows(frame), expectedBorders)
      assert.equal(frame.split('\n').every(line => stringWidth(line) < 40), true)
    }
  } finally {
    instance.unmount()
  }
})

test('App scrolls a resumed history replacement to its first user message even when row count is unchanged', async () => {
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
    const finalFrame = await waitForFrame(stdout, frame => /> 介绍美国/.test(frame) && /恢复完成：12 轮历史 · session\.json/.test(frame))

    assert.match(finalFrame, /> 介绍美国/)
    assert.match(finalFrame, /恢复完成：12 轮历史 · session\.json/)
    assert.doesNotMatch(finalFrame, /已写入 backend\.history/)
    const transcriptArea = finalFrame.split('Enter send')[0] ?? finalFrame
    assert.doesNotMatch(transcriptArea, /已恢复 12 轮结构化会话/)
  } finally {
    instance.unmount()
    timers.forEach(timer => clearTimeout(timer))
  }
})

test('App renders the first restored frame at the oldest resumed message', async () => {
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
    await delay(300)
    const frames = stdout.chunks.map(stripAnsi).filter(chunk => chunk.includes('GenericAgent'))
    const firstRestoredFrame = frames.find(frame => frame.includes('介绍美国') || frame.includes('恢复问题 12'))

    assert.ok(firstRestoredFrame)
    assert.match(firstRestoredFrame, /> 介绍美国/)
    assert.doesNotMatch(firstRestoredFrame, /已写入 backend\.history/)
    const transcriptArea = firstRestoredFrame.split('Enter send')[0] ?? firstRestoredFrame
    assert.doesNotMatch(transcriptArea, /已恢复 12 轮结构化会话/)
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

    assert.match(stdout.chunks.join(''), /\x1b\[22;4H/)
  } finally {
    instance.unmount()
  }
})

test('App enters the alternate screen before the first Ink frame is written', async () => {
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
    const firstFrameIndex = combined.indexOf('GenericAgent')

    assert.notEqual(alternateScreenIndex, -1)
    assert.ok(alternateScreenIndex < firstFrameIndex)
  } finally {
    instance.unmount()
    timers.forEach(timer => clearTimeout(timer))
  }
})

test('App help documents workflow plan slash command entry', () => {
  assert.match(helpText(), /\/workflow plan \[--manual\] \[--timeout SECONDS\] TASK - plan and run a dynamic workflow/)
})
