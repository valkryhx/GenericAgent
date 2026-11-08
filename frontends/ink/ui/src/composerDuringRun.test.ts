/**
 * The composer must stay usable while the agent is running.
 *
 * Real complaint: during a `wait_agent` call (up to 180s of synchronous sleep on the
 * Python side) the composer showed no cursor and Enter did nothing. Typing actually
 * reached the state (inputController does not gate printable input on status), but with
 * no caret and no response it looked like the app had hung, so there was no way to tell
 * whether input was even being accepted.
 *
 * Claude Code keeps the native terminal cursor visible during a turn and queues what you
 * type. These tests pin the GA side of that behaviour at the byte/frame level:
 *
 *   1. the parked native cursor is shown (`\x1b[?25h`) while `status === 'running'`
 *   2. the inverse caret block is painted while running, not only when idle
 *   3. Enter while running queues the draft instead of silently discarding the intent
 *   4. the composer tells the user their input was queued
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from './App.js'
import { inkRenderOptions } from './inkRenderOptions.js'
import { createCursorParkStdout } from './stdoutCursorPark.js'
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

async function waitUntil(predicate: () => boolean, timeoutMs = 2000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (predicate()) return
    await delay(10)
  }
}

type Harness = {
  stdout: CaptureWriteStream
  stdin: FakeReadStream
  instance: ReturnType<typeof render>
  /** Drive the App into the running state the way the real bridge does. */
  setRunning: () => Promise<void>
  sent: string[]
  framesSince: (start: number) => string
}

function mountApp(withPark: boolean): Harness {
  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
  const stdin = new FakeReadStream()
  const sent: string[] = []
  let onEventRef: ((event: BridgeEvent) => void) | null = null

  const startBridgeClient = (
    _python: string,
    _bridgeScript: string,
    onEvent: (event: BridgeEvent) => void,
  ): BridgeClient => {
    onEventRef = onEvent
    setTimeout(() => onEvent({ type: 'ready', version: 1 }), 0)
    return {
      send(command: unknown) {
        sent.push(JSON.stringify(command))
      },
      stop() {},
    }
  }

  const cursorPark = withPark ? createCursorParkStdout(stdout as unknown as NodeJS.WriteStream) : undefined
  const instance = render(React.createElement(App, {
    python: 'python',
    bridgeScript: 'bridge.py',
    startBridgeClient,
    ...(cursorPark ? { cursorPark } : {}),
  }), {
    ...inkRenderOptions,
    stdout: (cursorPark ? cursorPark.stdout : stdout) as unknown as NodeJS.WriteStream,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    patchConsole: false,
  })

  return {
    stdout,
    stdin,
    instance,
    setRunning: async () => {
      await waitUntil(() => onEventRef !== null)
      onEventRef!({ type: 'status', status: 'running', taskId: 1 })
      // Wait for the running chrome, not a fixed delay: an assertion that depends on
      // "did the re-render land yet" is flaky, and a flaky RED is not evidence.
      await waitUntil(() =>
        stdout.chunks.some(chunk => stripAnsi(chunk).includes('Running ·')),
      )
    },
    sent,
    framesSince: (start: number) => stdout.chunks.slice(start).join(''),
  }
}

test('the native cursor stays visible while the agent is running', async () => {
  const app = mountApp(true)
  try {
    await waitUntil(() => app.framesSince(0).length > 0)
    await app.setRunning()
    await delay(60)

    const afterRunning = app.framesSince(0)
    // The park writer SHOWs the cursor when it parks on the caret. If the App stops
    // declaring a park while running (the old behaviour), the cursor stays hidden and
    // the user cannot tell whether the composer is accepting input.
    const lastHide = afterRunning.lastIndexOf('\x1b[?25l')
    const lastShow = afterRunning.lastIndexOf('\x1b[?25h')
    assert.ok(
      lastShow > lastHide,
      'native cursor is hidden while running: the composer looks dead',
    )
  } finally {
    app.instance.unmount()
  }
})

test('the caret is drawn while running, not only when idle', async () => {
  // The decision that hid the caret was `showCursor={status !== 'running'}`. Asserting
  // "renderInputLine is status-independent" cannot fail on the old code, because the
  // gating lived at the call site, not in the model. So assert the call site's effect
  // instead: with a real cursor park attached, a running turn must still declare a
  // park on the caret. That is exactly what the old branch suppressed.
  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
  const stdin = new FakeReadStream()
  const cursorPark = createCursorParkStdout(stdout as unknown as NodeJS.WriteStream)
  const parks: Array<{ up: number; col: number } | null> = []
  const realSetPark = cursorPark.setPark.bind(cursorPark)
  cursorPark.setPark = (spec) => {
    parks.push(spec)
    realSetPark(spec)
  }

  let onEvent: ((event: BridgeEvent) => void) | null = null
  const instance = render(React.createElement(App, {
    python: 'python',
    bridgeScript: 'bridge.py',
    cursorPark,
    startBridgeClient: (_p: string, _b: string, cb: (e: BridgeEvent) => void): BridgeClient => {
      onEvent = cb
      setTimeout(() => cb({ type: 'ready', version: 1 }), 0)
      return { send() {}, stop() {} }
    },
  }), {
    ...inkRenderOptions,
    stdout: cursorPark.stdout,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    patchConsole: false,
  })

  try {
    await waitUntil(() => onEvent !== null)
    await waitUntil(() => parks.some(spec => spec !== null))

    const beforeRunning = parks.length
    onEvent!({ type: 'status', status: 'running', taskId: 1 })
    await waitUntil(() =>
      stdout.chunks.some(chunk => stripAnsi(chunk).includes('Running ·')),
    )
    await delay(60)

    const duringRun = parks.slice(beforeRunning)
    assert.ok(duringRun.length > 0, 'the composer stopped re-declaring its caret during a run')
    assert.ok(
      duringRun.some(spec => spec !== null),
      'the native cursor was un-parked during a run: the composer looks read-only',
    )

    // And printable input is not gated on status, which is the premise of queueing.
    stdin.send('y')
    await delay(60)
    assert.match(stripAnsi(stdout.chunks.join('')), /y/, 'typing did not reach the composer')
  } finally {
    instance.unmount()
  }
})

test('Enter while running queues the draft instead of discarding it', async () => {
  const app = mountApp(false)
  try {
    await waitUntil(() => app.framesSince(0).length > 0)
    await app.setRunning()
    await app.stdin.send('do this next')
    await delay(40)
    await app.stdin.send('\r')
    await delay(80)

    const submits = app.sent.filter(command => command.includes('"type":"submit"'))
    assert.equal(submits.length, 0, 'Enter during a run submitted a new turn instead of queueing')

    // Not submitting is only half the requirement -- the current code also "passes"
    // that by doing nothing at all. The draft has to actually reach the queue.
    const queued = app.sent.filter(command => command.includes('"type":"queue_prompt"'))
    assert.equal(queued.length, 1, 'Enter during a run did not queue the draft')
    assert.ok(queued[0]!.includes('do this next'), 'the queued command lost the draft text')
  } finally {
    app.instance.unmount()
  }
})

test('the composer says the input was queued', async () => {
  const app = mountApp(false)
  try {
    await waitUntil(() => app.framesSince(0).length > 0)
    await app.setRunning()
    await app.stdin.send('queued text')
    await delay(40)
    await app.stdin.send('\r')
    await delay(120)

    const frame = stripAnsi(app.framesSince(0))
    assert.match(
      frame,
      /queued|Queued|排队/i,
      'nothing told the user their input was accepted for later',
    )
    // The draft is taken: it now lives in the queue, so leaving it in the composer
    // would make a second Enter look like it queued the same thing twice.
    assert.doesNotMatch(
      frame.split('\n').filter(line => line.includes('>')).at(-1) ?? '',
      /queued text/,
      'the composer kept the draft after queueing it',
    )
  } finally {
    app.instance.unmount()
  }
})
