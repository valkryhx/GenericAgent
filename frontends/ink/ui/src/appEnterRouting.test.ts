/**
 * End-to-end key routing through the real `App`: who gets Enter?
 *
 * The two unit-level files (`workflowPanelEnterSteering`, `workflowStatusBarEnterSteering`)
 * assert a predicate they define themselves, so they cannot catch a regression that
 * leaves them untouched while `App.tsx` stops honouring the rule. This file drives the
 * actual component with a fake stdin and inspects the commands the bridge receives.
 *
 * The reported bug: during a workflow run the user queues a steering follow-up and
 * presses Enter; the panel/status bar consumed it, so the message was never sent.
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from './App.js'
import type { BridgeCommand, BridgeEvent } from './protocol.js'

class CaptureWriteStream extends EventEmitter {
  columns = 100
  rows = 40
  chunks: string[] = []
  write(chunk: string) {
    this.chunks.push(String(chunk))
    return true
  }
}

class FakeReadStream extends EventEmitter {
  isTTY = true
  private readonly queue: string[] = []
  setRawMode() { return this }
  setEncoding() { return this }
  ref() { return this }
  unref() { return this }
  resume() { return this }
  pause() { return this }
  read() { return this.queue.shift() ?? null }
  send(text: string) {
    this.queue.push(text)
    this.emit('readable')
    this.emit('data', text)
  }
}

const stripAnsi = (value: string) => value.replace(/\x1b\[[0-9;?]*[a-zA-Z]/g, '')

async function waitFor(stdout: CaptureWriteStream, predicate: (frame: string) => boolean, timeoutMs = 4000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const frame = stripAnsi(stdout.chunks.join(''))
    if (predicate(frame)) return frame
    await new Promise(resolve => setTimeout(resolve, 10))
  }
  throw new Error(`timed out; last frame:\n${stripAnsi(stdout.chunks.join('')).slice(-2000)}`)
}

/**
 * A bridge that records what App sends and can inject events on demand.
 * `ready` fires asynchronously so App's handshake path is exercised.
 */
function recordingBridge(sent: BridgeCommand[]) {
  let emit: ((event: BridgeEvent) => void) | null = null
  const client = {
    send(command: BridgeCommand) {
      sent.push(command)
    },
    stop() {},
  }
  const factory = (
    _python: string,
    _bridgeScript: string,
    onEvent: (event: BridgeEvent) => void,
  ) => {
    emit = onEvent
    setTimeout(() => onEvent({ type: 'ready', version: 1 }), 0)
    return client
  }
  return { factory, fire: (event: BridgeEvent) => emit?.(event) }
}

function mount(sent: BridgeCommand[], fire: (event: BridgeEvent) => void) {
  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
  const stdin = new FakeReadStream()
  const bridge = recordingBridge(sent)
  const instance = render(
    React.createElement(App, {
      python: 'python',
      bridgeScript: 'bridge.py',
      startBridgeClient: bridge.factory as never,
    }),
    {
      stdout: stdout as unknown as NodeJS.WriteStream,
      stderr: stderr as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      patchConsole: false,
      exitOnCtrlC: false,
    },
  )
  return { instance, stdout, stdin, fire: bridge.fire }
}

const runningWorkflow = {
  type: 'workflow_run' as const,
  run: { runId: 'wf_probe', sessionId: 'session', status: 'running', jobs: [] },
}

/** A live turn: this is the event that puts App into `running` (`state.ts`). */
const turnRunning = { type: 'status' as const, status: 'running' as const }

test('Enter on a steering draft sends the message instead of opening the workflow panel', async () => {
  const sent: BridgeCommand[] = []
  const { instance, stdout, stdin, fire } = mount(sent, () => {})
  try {
    await waitFor(stdout, frame => frame.includes('>'))
    // Both preconditions of the reported bug: a running turn (so the old code
    // considered Enter "free to lend") AND a live workflow (so the status bar has an
    // Enter binding at all).
    fire(runningWorkflow)
    fire(turnRunning)
    await waitFor(stdout, frame => /workflow/i.test(frame))

    stdin.send('please also update the changelog')
    await waitFor(stdout, frame => frame.includes('changelog'))

    sent.length = 0
    stdin.send('\r')
    await new Promise(resolve => setTimeout(resolve, 250))

    // The bug being fixed: Enter was consumed by the panel/status bar and the draft
    // was never sent. With a live turn the correct command is `queue_prompt`; `submit`
    // is also accepted so the test keeps working if the status machine changes.
    const opened = sent.filter(c => c.type === 'workflow_detail')
    const delivered = sent.filter(c => c.type === 'submit' || c.type === 'queue_prompt')
    assert.equal(
      opened.length,
      0,
      `Enter opened the workflow panel while a steering draft was pending: ${JSON.stringify(sent)}`,
    )
    assert.equal(
      delivered.length,
      1,
      `the steering draft was never sent; bridge received: ${JSON.stringify(sent)}`,
    )
    assert.equal((delivered[0] as { text?: string }).text, 'please also update the changelog')
    assert.equal(
      (delivered[0] as { type: string }).type,
      'queue_prompt',
      'a steering message typed during a running turn must be queued into that turn',
    )
  } finally {
    instance.unmount()
  }
})

test('Enter on an empty composer still opens the workflow panel from the status bar', async () => {
  const sent: BridgeCommand[] = []
  const { instance, stdout, stdin, fire } = mount(sent, () => {})
  try {
    await waitFor(stdout, frame => frame.includes('>'))
    fire(runningWorkflow)
    await waitFor(stdout, frame => /workflow/i.test(frame))

    sent.length = 0
    stdin.send('\r')
    await waitFor(stdout, () => sent.length > 0, 300)

    assert.equal(
      sent.filter(c => c.type === 'workflow_detail').length,
      1,
      `an empty composer must still open the panel; bridge received: ${JSON.stringify(sent)}`,
    )
  } finally {
    instance.unmount()
  }
})