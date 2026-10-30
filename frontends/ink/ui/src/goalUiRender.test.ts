/**
 * Real-render goal UI tests.
 *
 * These actually mount <App> with a fake bridge client and capture painted ANSI
 * frames (same technique as App.test.ts / the real-ui e2e). That proves the goal
 * card, terminal badge, passive nudge, proposal panel and /goal completion
 * really reach the screen - not just that pure model functions return strings.
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import stringWidth from 'string-width'
import { App } from './App.js'
import type { BridgeClient } from './bridgeClient.js'
import type { BridgeCommand, BridgeEvent } from './protocol.js'

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
  private queue: string[] = []
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
    .replace(/\r/g, '')
}

function delay(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}

function chromeFrames(stdout: CaptureWriteStream): string[] {
  return stdout.chunks.map(stripAnsi).filter(chunk => (
    chunk.includes('Enter send')
    || chunk.includes('Running ·')
    || chunk.includes('GenericAgent')
    || /─{8,}/.test(chunk)
  ))
}

async function waitForFrame(
  stdout: CaptureWriteStream,
  predicate: (frame: string) => boolean,
  timeoutMs = 2500,
): Promise<string> {
  const deadline = Date.now() + timeoutMs
  let last = ''
  while (Date.now() < deadline) {
    const frames = chromeFrames(stdout)
    last = frames.at(-1) ?? ''
    if (predicate(last)) return last
    await delay(20)
  }
  return last
}

async function waitForText(
  stdout: CaptureWriteStream,
  predicate: (text: string) => boolean,
  timeoutMs = 2500,
): Promise<string> {
  const deadline = Date.now() + timeoutMs
  let out = ''
  while (Date.now() < deadline) {
    out = stdout.chunks.map(stripAnsi).join('')
    if (predicate(out)) return out
    await delay(20)
  }
  return out
}

type Harness = {
  stdout: CaptureWriteStream
  stdin: FakeReadStream
  sent: BridgeCommand[]
  instance: { unmount: () => void }
}

function mount(script: (onEvent: (e: BridgeEvent) => void) => void, columns = 80): Harness {
  const stdout = new CaptureWriteStream()
  stdout.columns = columns
  const stderr = new CaptureWriteStream()
  stderr.columns = columns
  const stdin = new FakeReadStream()
  const sent: BridgeCommand[] = []
  const startBridgeClient = (
    _python: string,
    _bridgeScript: string,
    onEvent: (event: BridgeEvent) => void,
  ): BridgeClient => {
    setTimeout(() => {
      onEvent({ type: 'ready', version: 1 })
      script(onEvent)
    }, 0)
    return {
      send(command) { sent.push(command) },
      stop() {},
    }
  }
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
  return { stdout, stdin, sent, instance }
}

const GOAL_META = (over: Partial<Record<string, unknown>> = {}) => ({
  type: 'goal_metadata' as const,
  goal: {
    condition: 'finish the migration',
    iterations: 2,
    set_at: (Date.now() - 125_000) / 1000,
    tokens_at_start: 100,
    tokens_spent: 1500,
    met: false,
    ...over,
  },
})

test('active goal card renders on screen with counter fields', async () => {
  const h = mount(onEvent => { onEvent(GOAL_META()) })
  try {
    const frame = await waitForFrame(h.stdout, f => f.includes('Goal active'))
    assert.match(frame, /◎ Goal active · running \d+/)
    assert.match(frame, /2 turns/)
    assert.match(frame, /1\.5k tokens/)
    assert.match(frame, /Goal: finish the migration/)
  } finally {
    h.instance.unmount()
  }
})

test('goal card shows last check reason', async () => {
  const h = mount(onEvent => {
    onEvent(GOAL_META({ iterations: 1, last_reason: 'not yet: tests still red' }))
  })
  try {
    const frame = await waitForFrame(h.stdout, f => f.includes('Last check'))
    assert.match(frame, /1 turn\b/)
    assert.match(frame, /Last check: not yet: tests still red/)
  } finally {
    h.instance.unmount()
  }
})

test('long goal condition does not overflow the terminal width', async () => {
  const columns = 48
  const h = mount(onEvent => {
    onEvent(GOAL_META({ condition: 'verify every subcommand returns zero and prints the expected banner text without wrapping errors' }))
  }, columns)
  try {
    const frame = await waitForFrame(h.stdout, f => f.includes('Goal active'))
    for (const line of frame.split('\n')) {
      assert.ok(stringWidth(line) < columns, `line too wide (${stringWidth(line)} >= ${columns}): ${JSON.stringify(line)}`)
    }
  } finally {
    h.instance.unmount()
  }
})

test('terminal met badge renders and active card is gone', async () => {
  const h = mount(onEvent => {
    onEvent({ type: 'goal_metadata', goal: null, terminal: { met: true, durationMs: 65_000, iterations: 2, tokens: 1500 } })
  })
  try {
    const frame = await waitForFrame(h.stdout, f => f.includes('Goal met'))
    assert.match(frame, /✓ Goal met · 1m 5s · 2 turns · 1\.5k tokens/)
    assert.doesNotMatch(frame, /Goal active/)
  } finally {
    h.instance.unmount()
  }
})

test('terminal failed badge renders with cross icon', async () => {
  const h = mount(onEvent => {
    onEvent({ type: 'goal_metadata', goal: null, terminal: { met: false, failed: true } })
  })
  try {
    const frame = await waitForFrame(h.stdout, f => f.includes('Goal failed'))
    assert.match(frame, /✗ Goal failed/)
  } finally {
    h.instance.unmount()
  }
})

test('a running turn hides the terminal badge until idle', async () => {
  const h = mount(onEvent => {
    onEvent({ type: 'status', status: 'running', taskId: 1 })
    onEvent({ type: 'goal_metadata', goal: null, terminal: { met: true, tokens: 10 } })
  })
  try {
    await waitForFrame(h.stdout, f => f.includes('Running ·'))
    const runningFrame = chromeFrames(h.stdout).at(-1) ?? ''
    assert.doesNotMatch(runningFrame, /Goal met/)
  } finally {
    h.instance.unmount()
  }
})

test('passive goal nudge renders in the footer when idle', async () => {
  const h = mount(onEvent => {
    onEvent({ type: 'goal_nudge', text: 'no active goal - type /goal to set one' })
  })
  try {
    const frame = await waitForFrame(h.stdout, f => f.includes('✦'))
    assert.match(frame, /✦ no active goal - type \/goal to set one/)
  } finally {
    h.instance.unmount()
  }
})

test('goal proposal panel renders and Enter approves', async () => {
  const h = mount(onEvent => {
    onEvent({ type: 'goal_proposal_request', requestId: 'r1', condition: 'ship the release build' })
  })
  try {
    const out = await waitForText(h.stdout, s => s.includes('建议设定目标'))
    assert.match(out, /ship the release build/)
    assert.match(out, /设定目标 \(approve\)/)
    assert.match(out, /不设定 \(reject\)/)
    assert.match(out, /Enter 确认 · Esc 不设定/)
    // default selection = approve; Enter responds approve
    h.stdin.send('\r')
    await delay(80)
    const resp = h.sent.find(c => c.type === 'goal_proposal_response')
    assert.ok(resp, 'expected a goal_proposal_response command')
    assert.equal((resp as any).requestId, 'r1')
    assert.equal((resp as any).decision, 'approve')
  } finally {
    h.instance.unmount()
  }
})

test('goal proposal panel Esc rejects', async () => {
  const h = mount(onEvent => {
    onEvent({ type: 'goal_proposal_request', requestId: 'r2', condition: 'delete old logs' })
  })
  try {
    await waitForText(h.stdout, s => s.includes('建议设定目标'))
    h.stdin.send('\u001b')
    await delay(80)
    const resp = h.sent.find(c => c.type === 'goal_proposal_response')
    assert.ok(resp)
    assert.equal((resp as any).requestId, 'r2')
    assert.equal((resp as any).decision, 'reject')
  } finally {
    h.instance.unmount()
  }
})

test('goal proposal panel Down then Enter rejects', async () => {
  const h = mount(onEvent => {
    onEvent({ type: 'goal_proposal_request', requestId: 'r3', condition: 'rotate keys' })
  })
  try {
    await waitForText(h.stdout, s => s.includes('建议设定目标'))
    h.stdin.send('\u001b[B') // down arrow -> select reject
    await delay(60)
    h.stdin.send('\r')
    await delay(80)
    const resp = h.sent.find(c => c.type === 'goal_proposal_response')
    assert.ok(resp)
    assert.equal((resp as any).decision, 'reject')
  } finally {
    h.instance.unmount()
  }
})

test('/(goal) slash completion is available in the menu', async () => {
  const h = mount(() => {})
  try {
    await waitForFrame(h.stdout, f => f.includes('>'))
    h.stdin.send('/go')
    const out = await waitForText(h.stdout, s => s.includes('/goal'))
    assert.match(out, /\/goal/)
  } finally {
    h.instance.unmount()
  }
})
