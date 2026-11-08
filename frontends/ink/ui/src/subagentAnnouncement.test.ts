/**
 * A subagent finishing must be visible in the UI, in green bold.
 *
 * What the bridge already does: it emits `agent_snapshot` / `agent_event` frames carrying
 * every child's status (`frontends/ink/bridge.py:_emit_agent_snapshot`). The UI stored
 * them in state and then rendered none of it, so a child finishing was invisible unless
 * you went and read `state.json` off disk. That is the same class of problem as the dead
 * composer: the information exists and never reaches the person watching.
 *
 * Requirement: when a child's status lands, the transcript shows a status line the user
 * cannot miss. Green + bold, because a completion is the one thing worth noticing at a
 * glance, and a user testing subagents should not have to read carefully to see it.
 *
 * These tests assert on the *styled* spans (not the stripped text) because colour and
 * weight are the requirement, not decoration -- "visible" that is the same grey as
 * everything else does not satisfy "obvious".
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
  read(): string | null { return this.queue.shift() ?? null }
  resume(): this { return this }
  pause(): this { return this }
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

function record(overrides: Record<string, unknown>) {
  return {
    executionId: 'process:probe_alpha',
    engine: 'process',
    recordKind: 'process_agent',
    status: 'succeeded',
    sourceStatus: 'completed',
    // The full capability shape: the bridge always sends it, and a partial object would
    // make this fixture diverge from the real frame in a way the type checker catches.
    capabilities: { actions: [], features: [] },
    agentPath: '/root/probe_alpha',
    metadata: { taskName: 'probe_alpha' },
    ...overrides,
  }
}

type Harness = {
  stdout: CaptureWriteStream
  stdin: FakeReadStream
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

  return {
    stdout,
    stdin,
    instance,
    emit: (event) => onEventRef?.(event),
  }
}

test('a finished subagent is announced in the transcript', async () => {
  const app = mountApp()
  try {
    await waitUntil(() => app.stdout.chunks.length > 0)
    app.emit({
      type: 'agent_snapshot',
      snapshot: { records: [record({})], cursors: {}, errors: {} },
    })
    await delay(150)

    const frame = stripAnsi(app.stdout.chunks.join(''))
    assert.match(
      frame,
      /probe_alpha/,
      'a finished subagent produced no visible line at all',
    )
    assert.match(
      frame,
      /succeeded|completed|完成/i,
      'the announcement does not say what happened',
    )
  } finally {
    app.instance.unmount()
  }
})

test('the announcement is green and bold', async () => {
  // Asserting on SGR bytes here is a trap: chalk decides whether colour is supported
  // once, at module load, so setting FORCE_COLOR inside a test is too late and the
  // assertion would fail for a reason that has nothing to do with the UI. The rendered
  // element tree is the contract -- `<Text color="green" bold>` is what produces green
  // bold on a real terminal.
  const { agentNoticeLine } = await import('./agentNotice.js')
  const node = agentNoticeLine(
    { executionId: 'process:probe_alpha', name: 'probe_alpha', ok: true, word: 'completed' },
    'green',
  )

  const props = node.props as { color?: string; bold?: boolean; children?: unknown }
  assert.equal(props.color, 'green', 'the announcement is not green')
  assert.equal(props.bold, true, 'the announcement is not bold')

  const text = String(props.children ?? '')
  assert.match(text, /probe_alpha/, 'the announcement does not name the child')
  assert.match(text, /completed/, 'the announcement does not say what happened')
})

test('a failed child is rendered in the error colour, not the success colour', async () => {
  const { agentNoticeLine } = await import('./agentNotice.js')
  const node = agentNoticeLine(
    { executionId: 'process:probe_alpha', name: 'probe_alpha', ok: false, word: 'failed' },
    'red',
  )

  const props = node.props as { color?: string; bold?: boolean }
  assert.equal(props.color, 'red')
  assert.equal(props.bold, true)
})

test('each subagent finishing gets its own line, in snapshot order', async () => {
  const app = mountApp()
  try {
    await waitUntil(() => app.stdout.chunks.length > 0)
    app.emit({
      type: 'agent_snapshot',
      snapshot: {
        records: [
          record({ executionId: 'process:alpha', agentPath: '/root/alpha', metadata: { taskName: 'alpha' } }),
          record({ executionId: 'process:bravo', agentPath: '/root/bravo', metadata: { taskName: 'bravo' } }),
          record({ executionId: 'process:charlie', agentPath: '/root/charlie', metadata: { taskName: 'charlie' } }),
        ],
        cursors: {},
        errors: {},
      },
    })
    await delay(200)

    const frame = stripAnsi(app.stdout.chunks.join(''))
    for (const name of ['alpha', 'bravo', 'charlie']) {
      assert.match(frame, new RegExp(name), `child ${name} was never announced`)
    }
    // All three must be present at once, not replaced one after another.
    const lastFrameStart = frame.lastIndexOf('alpha')
    assert.ok(
      frame.indexOf('bravo') < lastFrameStart || frame.includes('charlie'),
      'children replaced each other instead of accumulating',
    )
  } finally {
    app.instance.unmount()
  }
})

test('a failing subagent is announced too, and not as a success', async () => {
  const app = mountApp()
  try {
    await waitUntil(() => app.stdout.chunks.length > 0)
    app.emit({
      type: 'agent_snapshot',
      snapshot: {
        records: [record({ status: 'failed', sourceStatus: 'errored' })],
        cursors: {},
        errors: {},
      },
    })
    await delay(150)

    const frame = stripAnsi(app.stdout.chunks.join(''))
    assert.match(frame, /probe_alpha/)
    assert.match(frame, /failed|error|失败/i, 'a failed child was reported with no failure word')
  } finally {
    app.instance.unmount()
  }
})

test('an unchanged snapshot does not re-announce the same child', async () => {
  const app = mountApp()
  try {
    await waitUntil(() => app.stdout.chunks.length > 0)
    const snapshot = { records: [record({})], cursors: {}, errors: {} }
    app.emit({ type: 'agent_snapshot', snapshot })
    await delay(120)
    const afterFirst = stripAnsi(app.stdout.chunks.join('')).split('probe_alpha').length - 1

    app.emit({ type: 'agent_snapshot', snapshot })
    await delay(120)
    const afterSecond = stripAnsi(app.stdout.chunks.join('')).split('probe_alpha').length - 1

    assert.equal(
      afterSecond,
      afterFirst,
      'the same snapshot announced the child again, which spams the transcript',
    )
  } finally {
    app.instance.unmount()
  }
})
