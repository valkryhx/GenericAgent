/**
 * Real-render tests for goal x multi-agent surfaces.
 *
 * These mount the REAL <App> with a fake bridge client and capture painted ANSI
 * frames, so we prove the goal card and the workflow status bar / team panel
 * actually reach the screen together (not merely that pure models return data).
 *
 * Covered:
 *  - active goal card + live workflow status bar coexist (secondary info band)
 *  - workflow status bar keeps the /workflows entry point while a goal is active
 *  - the /team overlay paints and hides the goal card (overlay owns the bottom)
 *  - once the team overlay closes, the goal card comes back
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
  columns = 100
  rows = 30
  chunks: string[] = []
  write(chunk: unknown): boolean { this.chunks.push(String(chunk)); return true }
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
  send(text: string): void { this.queue.push(text); this.emit('readable') }
}
function stripAnsi(text: string): string {
  return text
    .replace(/\u001B\][^\u0007]*(?:\u0007|\u001B\\)/g, '')
    .replace(/\u001B\[[0-?]*[ -/]*[@-~]/g, '')
    .replace(/\u001B[=>]/g, '')
    .replace(/\r/g, '')
}
function delay(ms: number): Promise<void> { return new Promise(r => setTimeout(r, ms)) }
function chrome(out: CaptureWriteStream): string[] {
  return out.chunks.map(stripAnsi).filter(c => (
    c.includes('Enter send') || c.includes('Running ·') || c.includes('GenericAgent') || /─{8,}/.test(c)
  ))
}
async function waitFor(out: CaptureWriteStream, pred: (t: string) => boolean, ms = 3000): Promise<string> {
  const end = Date.now() + ms
  let last = ''
  while (Date.now() < end) {
    last = out.chunks.map(stripAnsi).join('')
    if (pred(last)) return last
    await delay(20)
  }
  return last
}
type Harness = { stdout: CaptureWriteStream; stdin: FakeReadStream; sent: BridgeCommand[]; instance: { unmount: () => void } }
function mount(script: (onEvent: (e: BridgeEvent) => void) => void, columns = 100): Harness {
  const stdout = new CaptureWriteStream(); stdout.columns = columns
  const stderr = new CaptureWriteStream(); stderr.columns = columns
  const stdin = new FakeReadStream(); const sent: BridgeCommand[] = []
  const startBridgeClient = (_p: string, _b: string, onEvent: (e: BridgeEvent) => void): BridgeClient => {
    setTimeout(() => { onEvent({ type: 'ready', version: 1 }); script(onEvent) }, 0)
    return { send(c) { sent.push(c) }, stop() {} }
  }
  const instance = render(React.createElement(App, { python: 'python', bridgeScript: 'bridge.py', startBridgeClient }), {
    stdout: stdout as unknown as NodeJS.WriteStream,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    patchConsole: false,
  })
  return { stdout, stdin, sent, instance }
}

const GOAL = (over: Record<string, unknown> = {}) => ({
  type: 'goal_metadata' as const,
  goal: { condition: 'ship the release', iterations: 1, set_at: (Date.now() - 60_000) / 1000, tokens_at_start: 0, tokens_spent: 800, met: false, ...over },
})
const WF_RUN = {
  type: 'workflow_run' as const,
  run: {
    runId: 'wf_live1', sessionId: 'session', status: 'running',
    metadata: { workflowName: 'release-build' },
    jobs: [
      { jobId: 'agent_1', status: 'succeeded', metadata: { label: 'plan' } },
      { jobId: 'agent_2', status: 'running', metadata: { label: 'build' } },
    ],
  },
}
const TEAM = {
  type: 'team_roster' as const,
  team: { teamName: 'root', leadAgentId: 'a0', leadName: 'lead', memberCount: 2 },
  members: [
    { agentId: 'a0', name: 'lead', status: 'running', role: 'lead', taskCount: 1, currentTask: 'coordinate' },
    { agentId: 'a1', name: 'worker', status: 'running', role: 'teammate', taskCount: 1, currentTask: 'build' },
  ],
  tasks: [{ id: 't1', subject: 'build release', status: 'in_progress', owner: '/root/worker' }],
}

test('goal card and live workflow status bar paint together', async () => {
  const h = mount(onEvent => { onEvent(GOAL()); onEvent(WF_RUN) })
  try {
    const out = await waitFor(h.stdout, s => s.includes('Goal active') && s.includes('release-build'))
    const frame = chrome(h.stdout).at(-1) ?? out
    assert.match(frame, /Goal active/)
    assert.match(frame, /release-build/)
    assert.match(frame, /1\/2 agents done/)
    assert.match(frame, /\/workflows/)
  } finally { h.instance.unmount() }
})

test('goal+workflow frames never overflow the terminal width', async () => {
  const columns = 64
  const h = mount(onEvent => { onEvent(GOAL()); onEvent(WF_RUN) }, columns)
  try {
    await waitFor(h.stdout, s => s.includes('Goal active') && s.includes('release-build'))
    for (const frame of chrome(h.stdout)) {
      for (const line of frame.split('\n')) {
        assert.ok(stringWidth(line) < columns, `line too wide (${stringWidth(line)}): ${JSON.stringify(line)}`)
      }
    }
  } finally { h.instance.unmount() }
})

test('team overlay collapses the goal card to one summary line; closing restores it', async () => {
  const h = mount(onEvent => { onEvent(GOAL()); onEvent(TEAM) })
  try {
    await waitFor(h.stdout, s => s.includes('Task board') && s.includes('worker'))
    const overlay = chrome(h.stdout).at(-1) ?? ''
    assert.match(overlay, /Team root/)
    assert.match(overlay, /Task board/)
    // Overlay owns the bottom band: the full two-line card collapses to the
    // single header line so goal context is not lost, but the second
    // (Goal:/Last check) line is not rendered inside the overlay.
    assert.match(overlay, /◎ Goal active/)
    assert.doesNotMatch(overlay, /Goal: ship the release/)
    for (const line of overlay.split(String.fromCharCode(10))) {
      assert.ok(stringWidth(line) < 100, `line too wide (${stringWidth(line)}): ${JSON.stringify(line)}`)
    }
    // Esc closes the overlay -> the full goal card (with the Goal: line) returns.
    h.stdin.send(String.fromCharCode(27))
    const out = await waitFor(h.stdout, s => s.includes('Goal: ship the release'))
    assert.match(out, /◎ Goal active/)
  } finally { h.instance.unmount() }
})

test('/workflows overlay collapses the goal card to one summary line; closing restores it', async () => {
  const WF_RUNS = {
    type: 'workflow_runs' as const,
    savedWorkflows: [] as string[],
    runs: [
      { runId: 'wf_list1', sessionId: 'session', status: 'running', metadata: { workflowName: 'release-build' } },
    ],
  }
  const h = mount(onEvent => { onEvent(GOAL()); onEvent(WF_RUNS) })
  try {
    await waitFor(h.stdout, s => s.includes('Dynamic workflows'))
    const overlay = chrome(h.stdout).at(-1) ?? ''
    assert.match(overlay, /Dynamic workflows/)
    assert.match(overlay, /◎ Goal active/)
    assert.doesNotMatch(overlay, /Goal: ship the release/)
    for (const line of overlay.split(String.fromCharCode(10))) {
      assert.ok(stringWidth(line) < 100, `line too wide (${stringWidth(line)}): ${JSON.stringify(line)}`)
    }
    h.stdin.send(String.fromCharCode(27))
    const out = await waitFor(h.stdout, s => s.includes('Goal: ship the release'))
    assert.match(out, /◎ Goal active/)
  } finally { h.instance.unmount() }
})

test('/team request opens the panel even while a goal is active', async () => {
  const h = mount(onEvent => {
    onEvent(GOAL())
    onEvent(WF_RUN)
    onEvent(TEAM)
  })
  try {
    await waitFor(h.stdout, s => s.includes('Task board'))
    assert.ok(h.stdout.chunks.map(stripAnsi).join('').includes('Team root'))
  } finally { h.instance.unmount() }
})

test('goal stays visible while the slash-completion popup is open', async () => {
  const h = mount(onEvent => { onEvent(GOAL()) })
  try {
    await waitFor(h.stdout, s => s.includes('Goal active'))
    h.stdin.send('/')
    // Slash suggestions take over the bottom band; the full card is replaced by
    // the single summary line so the goal is not lost while typing a command.
    await waitFor(h.stdout, s => /\/(help|clear|model|workflow)/.test(s) && s.includes('◎ Goal active'))
    const frame = chrome(h.stdout).at(-1) ?? ''
    assert.match(frame, /◎ Goal active/)
    assert.doesNotMatch(frame, /Goal: ship the release/)
  } finally { h.instance.unmount() }
})

test('goal reached while the team overlay is open still shows a badge', async () => {
  const h = mount(onEvent => {
    onEvent(GOAL())
    onEvent(TEAM)
    setTimeout(() => onEvent({ type: 'goal_metadata', goal: null, terminal: { met: true, durationMs: 65_000, iterations: 2, tokens: 900 } }), 120)
  })
  try {
    await waitFor(h.stdout, s => s.includes('Goal met'))
    const frame = chrome(h.stdout).at(-1) ?? ''
    assert.match(frame, /✓ Goal met/)
    assert.match(frame, /Task board/)
    assert.doesNotMatch(frame, /Goal active/)
  } finally { h.instance.unmount() }
})
