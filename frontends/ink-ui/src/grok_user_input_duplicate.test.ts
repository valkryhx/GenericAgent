/**
 * 回归：默认 inline scrollback 下用户输入不得双显（Codex 对齐）。
 *
 * 生产路径事件顺序：user → status:running
 * 期望：user 只 commit 到 Static 一次；live 不重画 done user。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from './App.js'
import type { BridgeClient } from './bridgeClient.js'
import type { BridgeEvent, ChatMessage } from './protocol.js'
import { splitStaticAndActiveMessages } from './messagePartition.js'
import { applyBridgeEvent, initialState } from './state.js'

const UNIQUE_USER_TEXT = '核验重复用户输入-grok-probe-7f3a'

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
}

function stripAnsi(text: string): string {
  return text
    .replace(/\][^]*(?:|\\)/g, '')
    .replace(/\[[0-?]*[ -/]*[@-~]/g, '')
    .replace(/[=>]/g, '')
}

function delay(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}

async function waitForOutput(
  stdout: CaptureWriteStream,
  predicate: (output: string) => boolean,
  timeoutMs = 2500,
): Promise<string> {
  const deadline = Date.now() + timeoutMs
  let output = ''
  while (Date.now() < deadline) {
    output = stdout.chunks.map(stripAnsi).join('')
    if (predicate(output)) return output
    await delay(20)
  }
  return output
}

function countOccurrences(haystack: string, needle: string): number {
  if (!needle) return 0
  let count = 0
  let from = 0
  while (true) {
    const idx = haystack.indexOf(needle, from)
    if (idx === -1) return count
    count += 1
    from = idx + needle.length
  }
}

function keepLatestTaskActiveFromStatus(status: string): boolean {
  return status === 'running' || status === 'stopping'
}

function staticChunksWithText(plainChunks: string[], text: string): string[] {
  return plainChunks.filter(chunk => chunk.includes(text) && !chunk.includes('GenericAgent'))
}

function liveFramesWithText(plainChunks: string[], text: string): string[] {
  return plainChunks.filter(chunk => chunk.includes('GenericAgent') && chunk.includes(text))
}

test('grok: production order keeps done user in static before and after status:running', () => {
  let state = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
  assert.equal(state.status, 'idle')

  state = applyBridgeEvent(state, { type: 'user', taskId: 9, text: UNIQUE_USER_TEXT })

  const keepBeforeRunning = keepLatestTaskActiveFromStatus(state.status)
  assert.equal(keepBeforeRunning, false)
  assert.equal(state.messages.length, 1)
  assert.equal(state.messages[0]?.role, 'user')
  assert.equal(state.messages[0]?.done, true)
  assert.equal(state.messages[0]?.taskId, 9)

  const splitBeforeRunning = splitStaticAndActiveMessages(state.messages, {
    keepLatestTaskActive: keepBeforeRunning,
  })

  assert.deepEqual(
    splitBeforeRunning.staticMessages.map(message => message.id),
    ['u-9'],
    'user 提交后应立即进入 static（Codex：立即 commit scrollback）',
  )
  assert.deepEqual(
    splitBeforeRunning.activeMessages.map(message => message.id),
    [],
    'done user 不得进入 active',
  )

  state = applyBridgeEvent(state, { type: 'status', status: 'running', taskId: 9 })
  const keepAfterRunning = keepLatestTaskActiveFromStatus(state.status)
  assert.equal(keepAfterRunning, true)
  assert.equal(state.messages.filter(message => message.role === 'user').length, 1)

  const splitAfterRunning = splitStaticAndActiveMessages(state.messages, {
    keepLatestTaskActive: keepAfterRunning,
  })
  assert.deepEqual(
    splitAfterRunning.staticMessages.map(message => message.id),
    ['u-9'],
    'status:running 后 done user 仍应留在 static',
  )
  assert.deepEqual(
    splitAfterRunning.activeMessages.map(message => message.id),
    [],
    'status:running 后 done user 不得进入 active（否则会双显）',
  )
})

test('grok: reverse event order also keeps done user only in static', () => {
  let state = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
  state = applyBridgeEvent(state, { type: 'status', status: 'running', taskId: 3 })
  state = applyBridgeEvent(state, { type: 'user', taskId: 3, text: UNIQUE_USER_TEXT })

  const keep = keepLatestTaskActiveFromStatus(state.status)
  assert.equal(keep, true)

  const split = splitStaticAndActiveMessages(state.messages, { keepLatestTaskActive: keep })
  assert.deepEqual(split.staticMessages.map(message => message.id), ['u-3'])
  assert.deepEqual(split.activeMessages.map(message => message.id), [])
  assert.equal(state.messages.filter(message => message.role === 'user').length, 1)
})

test('grok: App production order commits user to Static once and never to live', async () => {
  const previousMouseMode = process.env.GA_INK_MOUSE
  delete process.env.GA_INK_MOUSE

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
    await waitForOutput(stdout, output => output.includes('GenericAgent'))
    const eventSink = emit as unknown as (event: BridgeEvent) => void

    eventSink({ type: 'user', taskId: 42, text: UNIQUE_USER_TEXT })
    await delay(80)
    eventSink({ type: 'status', status: 'running', taskId: 42 })

    await waitForOutput(
      stdout,
      output => output.includes(UNIQUE_USER_TEXT) && output.includes('running'),
    )
    await delay(120)

    const plainChunks = stdout.chunks.map(stripAnsi)
    const staticChunks = staticChunksWithText(plainChunks, UNIQUE_USER_TEXT)
    const liveWithUser = liveFramesWithText(plainChunks, UNIQUE_USER_TEXT)

    assert.ok(
      staticChunks.length > 0,
      '生产顺序下 Static scrollback 应写入用户文本一次',
    )
    assert.equal(
      liveWithUser.length,
      0,
      'status:running 后 live viewport 不得再绘制同一用户文本',
    )
  } finally {
    instance.unmount()
    if (previousMouseMode === undefined) {
      delete process.env.GA_INK_MOUSE
    } else {
      process.env.GA_INK_MOUSE = previousMouseMode
    }
  }
})

test('grok: App reverse order commits user to Static and keeps live free of user', async () => {
  const previousMouseMode = process.env.GA_INK_MOUSE
  delete process.env.GA_INK_MOUSE

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
    await waitForOutput(stdout, output => output.includes('GenericAgent'))
    const eventSink = emit as unknown as (event: BridgeEvent) => void
    const reverseText = `${UNIQUE_USER_TEXT}-reverse`

    eventSink({ type: 'status', status: 'running', taskId: 43 })
    await delay(40)
    eventSink({ type: 'user', taskId: 43, text: reverseText })
    await waitForOutput(stdout, output => output.includes(reverseText))
    await delay(120)

    const plainChunks = stdout.chunks.map(stripAnsi)
    const staticChunks = staticChunksWithText(plainChunks, reverseText)
    const liveWithUser = liveFramesWithText(plainChunks, reverseText)

    assert.ok(
      staticChunks.length > 0,
      'running→user 顺序下用户文本应 commit 到 Static',
    )
    assert.equal(
      liveWithUser.length,
      0,
      'running→user 顺序下 live 不得包含用户文本',
    )
  } finally {
    instance.unmount()
    if (previousMouseMode === undefined) {
      delete process.env.GA_INK_MOUSE
    } else {
      process.env.GA_INK_MOUSE = previousMouseMode
    }
  }
})

test('grok: partition matrix keeps done user in static when keepLatestTaskActive', () => {
  const loneUser: ChatMessage[] = [
    { id: 'u-1', role: 'user', text: 'q1', done: true, taskId: 1 },
    { id: 'a-1', role: 'assistant', text: 'a1', done: true, taskId: 1 },
    { id: 'u-2', role: 'user', text: 'q2', done: true, taskId: 2 },
  ]

  const idleSplit = splitStaticAndActiveMessages(loneUser, { keepLatestTaskActive: false })
  assert.deepEqual(idleSplit.staticMessages.map(message => message.id), ['u-1', 'a-1', 'u-2'])
  assert.deepEqual(idleSplit.activeMessages.map(message => message.id), [])

  const runningSplit = splitStaticAndActiveMessages(loneUser, { keepLatestTaskActive: true })
  assert.deepEqual(runningSplit.staticMessages.map(message => message.id), ['u-1', 'a-1', 'u-2'])
  assert.deepEqual(runningSplit.activeMessages.map(message => message.id), [])
})

test('grok: streaming keeps user in static and assistant in active', () => {
  const messages: ChatMessage[] = [
    { id: 'u-1', role: 'user', text: 'q1', done: true, taskId: 1 },
    { id: 'a-1', role: 'assistant', text: 'a1', done: true, taskId: 1 },
    { id: 'u-2', role: 'user', text: UNIQUE_USER_TEXT, done: true, taskId: 2 },
    { id: 'a-2', role: 'assistant', text: 'partial', done: false, taskId: 2 },
  ]

  const split = splitStaticAndActiveMessages(messages, { keepLatestTaskActive: true })
  assert.deepEqual(split.staticMessages.map(message => message.id), ['u-1', 'a-1', 'u-2'])
  assert.deepEqual(split.activeMessages.map(message => message.id), ['a-2'])
  assert.equal(countOccurrences(split.staticMessages.map(m => m.text).join('\n'), UNIQUE_USER_TEXT), 1)
})
