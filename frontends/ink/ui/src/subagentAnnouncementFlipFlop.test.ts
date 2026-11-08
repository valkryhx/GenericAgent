/**
 * A child is announced once, and its final word is the final word.
 *
 * The bug: the same child appeared twice in the transcript, first as
 * `✗ Subagent probe_fast failed` and then as `✓ Subagent probe_fast completed`. The
 * dedupe key was `${executionId}:${word}`, so a *changed word* looked like a new child.
 *
 * Two separate things were wrong, and both have to be fixed:
 *
 * 1. Announcing a non-final state at all. A child's status legitimately moves
 *    (`running` -> `closed`/`succeeded` -> `completed`) as its turn ends and as its
 *    process is reclaimed, so a snapshot taken mid-flight carries a word that later
 *    changes. Announcing every intermediate word produces the flip-flop the user saw.
 *
 * 2. Not correcting a wrong announcement. If an early word was wrong (the earlier
 *    `sourceStatus` bug announced `failed` for finished children), the corrected
 *    `completed` has to replace that line -- not sit next to it. Leaving both means the
 *    user cannot tell which one is true, which is worse than either alone.
 *
 * These tests drive the real reducer path (`applyBridgeEvent`) plus the App's
 * announcement bookkeeping, so a fixture cannot pass while the app still duplicates.
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from './App.js'
import { inkRenderOptions } from './inkRenderOptions.js'
import type { BridgeClient } from './bridgeClient.js'
import type { BridgeEvent } from './protocol.js'
import { paintedFrame } from './virtualScreen.js'

class CaptureWriteStream extends EventEmitter {
  columns = 100
  rows = 30
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
  resume(): this { return this }
  pause(): this { return this }
  read(): string | null { return this.queue.shift() ?? null }
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

async function waitUntil(predicate: () => boolean, timeoutMs = 3000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (predicate()) return
    await delay(10)
  }
}

/**
 * The lines actually painted on the final frame.
 *
 * Reading the accumulated chunk stream is the trap here: it holds *every* frame ever
 * written, so a line from an earlier frame and its replacement from the current one both
 * appear, and a correct implementation looks like it duplicated. Stripping ANSI before
 * splitting makes it worse, because the split separators are themselves escape
 * sequences. So replay the raw stream through the virtual screen and read what is on the
 * screen now -- the same thing the user sees and the same approach the composer-selection
 * end-to-end test uses.
 */
function paintedLines(stdout: CaptureWriteStream): string[] {
  return paintedFrame(stdout.chunks.join(''), 30, 100)
}

function linesMentioning(stdout: CaptureWriteStream, name: string): string[] {
  return paintedLines(stdout).filter(line => line.includes(name))
}

function child(overrides: Record<string, unknown> = {}) {
  return {
    executionId: 'process-agent:run_000001:%2Froot%2Fprobe_fast',
    engine: 'process',
    recordKind: 'process_agent',
    status: 'succeeded',
    sourceStatus: 'waiting_reply',
    capabilities: { actions: [], features: [] },
    agentPath: '/root/probe_fast',
    metadata: { taskName: 'probe_fast', turnStatus: 'completed' },
    ...overrides,
  }
}

type Harness = {
  stdout: CaptureWriteStream
  instance: ReturnType<typeof render>
  emit: (event: BridgeEvent) => void
}

function mountApp(): Harness {
  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
  const stdin = new FakeReadStream()
  let onEventRef: ((event: BridgeEvent) => void) | null = null

  const startBridgeClient = (
    _python: string,
    _bridgeScript: string,
    onEvent: (event: BridgeEvent) => void,
  ): BridgeClient => {
    onEventRef = onEvent
    setTimeout(() => onEvent({ type: 'ready', version: 1 }), 0)
    return { send() {}, stop() {} }
  }

  const instance = render(React.createElement(App, {
    python: 'python',
    bridgeScript: 'bridge.py',
    startBridgeClient,
  }), {
    ...inkRenderOptions,
    stdout: stdout as unknown as NodeJS.WriteStream,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    patchConsole: false,
  })

  return { stdout, instance, emit: event => onEventRef?.(event) }
}

function snapshot(records: Array<Record<string, unknown>>): BridgeEvent {
  return { type: 'agent_snapshot', snapshot: { records, cursors: {}, errors: {} } } as BridgeEvent
}

test('a child that is announced once is not announced again when its word changes', async () => {
  // The exact reported sequence: an early snapshot says failed, a later one says
  // completed. The user must end up with ONE line for this child, saying completed.
  const app = mountApp()
  try {
    await waitUntil(() => app.stdout.chunks.length > 0)

    // Mid-flight: no work verdict yet, process reclaimed -> the bridge projects `closed`.
    app.emit(snapshot([child({
      status: 'closed',
      sourceStatus: 'exited',
      metadata: { taskName: 'probe_fast' },
    })]))
    await delay(120)

    // Finished: turn completed.
    app.emit(snapshot([child({})]))
    await delay(160)

    const lines = linesMentioning(app.stdout, 'probe_fast')
    assert.equal(
      lines.length,
      1,
      `probe_fast was announced ${lines.length} times:\n${lines.join('\n')}`,
    )
    assert.match(lines[0]!, /completed/, 'the surviving line does not say the child completed')
    assert.doesNotMatch(lines[0]!, /failed/, 'a completed child still reads as failed')
  } finally {
    app.instance.unmount()
  }
})

test('a wrong early announcement is replaced, not duplicated, by the correction', async () => {
  // If an interim word was wrong -- which is exactly what the `sourceStatus` bug did to
  // finished children -- the corrected word must take its place. Two contradictory lines
  // for one child leave the user unable to tell which is true.
  const app = mountApp()
  try {
    await waitUntil(() => app.stdout.chunks.length > 0)

    app.emit(snapshot([child({
      status: 'failed',
      sourceStatus: 'killed',
      metadata: { taskName: 'probe_fast', turnStatus: 'errored' },
    })]))
    await delay(120)

    app.emit(snapshot([child({})]))
    await delay(160)

    const lines = linesMentioning(app.stdout, 'probe_fast')
    assert.equal(
      lines.length,
      1,
      `probe_fast has ${lines.length} contradictory lines:\n${lines.join('\n')}`,
    )
    assert.match(lines[0]!, /completed/, 'the correction did not replace the wrong verdict')
  } finally {
    app.instance.unmount()
  }
})

test('three children finishing land as three separate lines, one each', async () => {
  // The reason the dedupe exists at all: the bridge re-emits the whole snapshot on every
  // change, so three children must not multiply into more than three lines.
  const app = mountApp()
  try {
    await waitUntil(() => app.stdout.chunks.length > 0)

    const names = ['alpha', 'bravo', 'charlie']
    const records = names.map(name => child({
      executionId: `process-agent:run_1:%2Froot%2F${name}`,
      agentPath: `/root/${name}`,
      metadata: { taskName: name, turnStatus: 'completed' },
    }))

    app.emit(snapshot(records))
    await delay(160)
    // The same snapshot again, which is what the bridge does whenever anything changes.
    app.emit(snapshot(records))
    await delay(160)

    const frame = paintedLines(app.stdout)
    for (const name of names) {
      const lines = frame.filter(line => line.includes(name))
      assert.equal(lines.length, 1, `${name} was announced ${lines.length} times:\n${lines.join('\n')}`)
    }
  } finally {
    app.instance.unmount()
  }
})

test('a child\'s word changing from failed to completed does not leave both on screen', async () => {
  // Same as the first test but through three transitions, matching a real child's life:
  // running -> closed -> succeeded. Only the final one may be announced.
  const app = mountApp()
  try {
    await waitUntil(() => app.stdout.chunks.length > 0)

    // `running` is not terminal, so nothing is announced yet.
    app.emit(snapshot([child({
      status: 'running',
      sourceStatus: 'alive',
      metadata: { taskName: 'probe_fast', turnStatus: 'running' },
    })]))
    await delay(100)
    assert.equal(
      linesMentioning(app.stdout, 'probe_fast').length,
      0,
      'a still-running child was announced',
    )

    app.emit(snapshot([child({})]))
    await delay(160)

    const lines = linesMentioning(app.stdout, 'probe_fast')
    assert.equal(lines.length, 1, `expected one line, got ${lines.length}:\n${lines.join('\n')}`)
    assert.match(lines[0]!, /completed/)
  } finally {
    app.instance.unmount()
  }
})
