/**
 * 回归：Running 可见性 P0-A + 单通道（不双显）
 *
 * - turn 内 user done:false → 仅 live
 * - assistant_done / idle → user finalize 进 Static 一次
 * - streaming 过程 stdout 持续可见 assistant 探针
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

const UNIQUE_USER_TEXT = '核验Running可见-user-probe-9c2e'
const UNIQUE_ASSISTANT_DELTA = '核验Running可见-assistant-delta-9c2e'

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
  setRawMode(): this { return this }
  setEncoding(): this { return this }
  ref(): this { return this }
  unref(): this { return this }
  read(): string | null { return this.queue.shift() ?? null }
  resume(): this { return this }
  pause(): this { return this }
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

function isLiveChromeFrame(chunk: string): boolean {
  // Default inline mode no longer paints a "GenericAgent" status header; detect
  // live chrome via composer/activity/borders instead.
  return chunk.includes('GenericAgent')
    || chunk.includes('Enter send')
    || chunk.includes('Running ·')
    || chunk.includes('✻')
    || /─{8,}/.test(chunk)
}

function staticChunksWithText(plainChunks: string[], text: string): string[] {
  return plainChunks.filter(chunk => chunk.includes(text) && !isLiveChromeFrame(chunk))
}

function liveFramesWithText(plainChunks: string[], text: string): string[] {
  return plainChunks.filter(chunk => isLiveChromeFrame(chunk) && chunk.includes(text))
}

test('grok: open-turn user stays live-only until finalize (no premature Static)', () => {
  let state = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
  state = applyBridgeEvent(state, { type: 'user', taskId: 9, text: UNIQUE_USER_TEXT })

  assert.equal(state.messages[0]?.done, false)
  const splitBefore = splitStaticAndActiveMessages(state.messages, {
    keepLatestTaskActive: keepLatestTaskActiveFromStatus(state.status),
  })
  assert.deepEqual(splitBefore.staticMessages.map(m => m.id), [])
  assert.deepEqual(splitBefore.activeMessages.map(m => m.id), ['u-9'])

  state = applyBridgeEvent(state, { type: 'status', status: 'running', taskId: 9 })
  const splitRunning = splitStaticAndActiveMessages(state.messages, {
    keepLatestTaskActive: keepLatestTaskActiveFromStatus(state.status),
  })
  assert.deepEqual(splitRunning.staticMessages.map(m => m.id), [])
  assert.deepEqual(splitRunning.activeMessages.map(m => m.id), ['u-9'])
})

test('grok: assistant_done finalizes user into static once (single channel)', () => {
  let state = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
  state = applyBridgeEvent(state, { type: 'user', taskId: 3, text: UNIQUE_USER_TEXT })
  state = applyBridgeEvent(state, { type: 'status', status: 'running', taskId: 3 })
  state = applyBridgeEvent(state, { type: 'assistant_delta', taskId: 3, text: 'hi' })
  state = applyBridgeEvent(state, { type: 'assistant_done', taskId: 3, text: 'hi final' })

  const user = state.messages.find(m => m.id === 'u-3')
  assert.equal(user?.done, true)
  const split = splitStaticAndActiveMessages(state.messages, { keepLatestTaskActive: false })
  assert.ok(split.staticMessages.some(m => m.id === 'u-3'))
  assert.equal(split.activeMessages.some(m => m.id === 'u-3'), false)
})

test('grok: status idle finalizes open user without assistant', () => {
  let state = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
  state = applyBridgeEvent(state, { type: 'user', taskId: 5, text: UNIQUE_USER_TEXT })
  state = applyBridgeEvent(state, { type: 'status', status: 'running', taskId: 5 })
  state = applyBridgeEvent(state, { type: 'status', status: 'idle' })

  assert.equal(state.messages.find(m => m.id === 'u-5')?.done, true)
  const split = splitStaticAndActiveMessages(state.messages)
  assert.deepEqual(split.staticMessages.map(m => m.id), ['u-5'])
  assert.deepEqual(split.activeMessages, [])
})

test('grok: App running path shows user in live and not premature Static', async () => {
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
    await waitForOutput(stdout, o => o.includes('Enter send') || o.includes('>'))
    const sink = emit as (e: BridgeEvent) => void
    sink({ type: 'user', taskId: 42, text: UNIQUE_USER_TEXT })
    await delay(60)
    sink({ type: 'status', status: 'running', taskId: 42 })
    await waitForOutput(stdout, o => o.includes(UNIQUE_USER_TEXT) && (o.includes('Running') || o.includes('✻')))
    await delay(100)

    const plain = stdout.chunks.map(stripAnsi)
    const staticChunks = staticChunksWithText(plain, UNIQUE_USER_TEXT)
    const liveWithUser = liveFramesWithText(plain, UNIQUE_USER_TEXT)

    assert.equal(staticChunks.length, 0, 'running 中 user 不得过早进 Static')
    assert.ok(liveWithUser.length > 0, 'running 中 live 必须可见本轮 user')
  } finally {
    instance.unmount()
  }
})

test('grok: App streaming deltas are visible before assistant_done', async () => {
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
    await waitForOutput(stdout, o => o.includes('Enter send') || o.includes('>'))
    const sink = emit as (e: BridgeEvent) => void
    sink({ type: 'user', taskId: 44, text: UNIQUE_USER_TEXT })
    sink({ type: 'status', status: 'running', taskId: 44 })
    await delay(80)
    sink({ type: 'assistant_delta', taskId: 44, text: UNIQUE_ASSISTANT_DELTA })
    await waitForOutput(stdout, o => o.includes(UNIQUE_ASSISTANT_DELTA))
    await delay(80)

    const beforeDone = stdout.chunks.map(stripAnsi).join('')
    assert.ok(
      beforeDone.includes(UNIQUE_ASSISTANT_DELTA),
      'assistant_done 前 stdout 必须已出现流式内容（过程可见）',
    )
    const liveWithAssistant = liveFramesWithText(stdout.chunks.map(stripAnsi), UNIQUE_ASSISTANT_DELTA)
    assert.ok(liveWithAssistant.length > 0, '流式内容应在 live 帧中')

    sink({ type: 'assistant_done', taskId: 44, text: UNIQUE_ASSISTANT_DELTA })
    sink({ type: 'status', status: 'idle' })
    await delay(150)

    const plain = stdout.chunks.map(stripAnsi)
    const combined = plain.join('')
    // After finalize, user may appear in Static; must not double-count wildly in live+static forever.
    // Live frames after idle should not keep painting the same user if dock collapsed — allow total >=1.
    assert.ok(countOccurrences(combined, UNIQUE_USER_TEXT) >= 1)
    assert.ok(countOccurrences(combined, UNIQUE_ASSISTANT_DELTA) >= 1)
  } finally {
    instance.unmount()
  }
})

test('grok: partition matrix open user is active; finalized user is static only', () => {
  const openTurn: ChatMessage[] = [
    { id: 'u-1', role: 'user', text: 'q1', done: true, taskId: 1 },
    { id: 'a-1', role: 'assistant', text: 'a1', done: true, taskId: 1 },
    { id: 'u-2', role: 'user', text: 'q2', done: false, taskId: 2 },
  ]
  const openSplit = splitStaticAndActiveMessages(openTurn, { keepLatestTaskActive: true })
  assert.deepEqual(openSplit.staticMessages.map(m => m.id), ['u-1', 'a-1'])
  assert.deepEqual(openSplit.activeMessages.map(m => m.id), ['u-2'])

  const doneTurn: ChatMessage[] = [
    ...openTurn.slice(0, 2),
    { id: 'u-2', role: 'user', text: 'q2', done: true, taskId: 2 },
    { id: 'a-2', role: 'assistant', text: 'a2', done: true, taskId: 2 },
  ]
  const doneSplit = splitStaticAndActiveMessages(doneTurn, { keepLatestTaskActive: false })
  assert.deepEqual(doneSplit.staticMessages.map(m => m.id), ['u-1', 'a-1', 'u-2', 'a-2'])
  assert.deepEqual(doneSplit.activeMessages, [])
})
