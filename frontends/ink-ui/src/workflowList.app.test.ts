import test from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
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

  read(): null {
    return null
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

test('App shows workflow_runs as a list first without auto-opening detail', async () => {
  const timers: NodeJS.Timeout[] = []
  const sent: BridgeCommand[] = []
  const stdin = new FakeReadStream()
  const startBridgeClient = (
    _python: string,
    _bridgeScript: string,
    onEvent: (event: BridgeEvent) => void,
  ): BridgeClient => {
    timers.push(setTimeout(() => onEvent({ type: 'ready', version: 1 }), 0))
    timers.push(setTimeout(() => onEvent({
      type: 'workflow_runs',
      runs: [
        {
          runId: 'wf_done_1',
          sessionId: 'session',
          status: 'succeeded',
          metadata: { workflowName: 'prompt-guided-planner-real-e2e-design' },
          jobs: [{ jobId: 'agent_1', status: 'succeeded' }],
        },
        {
          runId: 'wf_waiting',
          sessionId: 'session',
          status: 'awaiting_approval',
          metadata: { workflowName: 'approval-needed' },
          jobs: [],
        },
      ],
    }), 20))
    return {
      send(command: BridgeCommand) {
        sent.push(command)
      },
      stop() {
        timers.forEach(timer => clearTimeout(timer))
      },
    }
  }
  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
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
    const listFrame = await waitForFrame(stdout, frame => frame.includes('Dynamic workflows') && frame.includes('prompt-guided-planner-real-e2e-design'))

    assert.match(listFrame, /Dynamic workflows/)
    assert.match(listFrame, /1 completed/)
    assert.match(listFrame, /› ✓ prompt-guided-planner-real-e2e-design/)
    assert.equal(sent.some(command => command.type === 'workflow_detail'), false)
  } finally {
    instance.unmount()
    timers.forEach(timer => clearTimeout(timer))
  }
})

test('App shows live workflow status bar without opening workflow panel', async () => {
  const timers: NodeJS.Timeout[] = []
  const sent: BridgeCommand[] = []
  const stdin = new FakeReadStream()
  const startBridgeClient = (
    _python: string,
    _bridgeScript: string,
    onEvent: (event: BridgeEvent) => void,
  ): BridgeClient => {
    timers.push(setTimeout(() => onEvent({ type: 'ready', version: 1 }), 0))
    timers.push(setTimeout(() => onEvent({
      type: 'workflow_run',
      run: {
        runId: 'wf_live_status',
        sessionId: 'session',
        status: 'running',
        metadata: { workflowName: 'read-recent-git-commits' },
        jobs: [
          { jobId: 'agent_1', status: 'succeeded' },
          { jobId: 'agent_2', status: 'running' },
        ],
      },
    }), 20))
    return {
      send(command: BridgeCommand) {
        sent.push(command)
      },
      stop() {
        timers.forEach(timer => clearTimeout(timer))
      },
    }
  }
  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
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
    const frame = await waitForFrame(stdout, current => current.includes('read-recent-git-commits') && current.includes('1/2 agents done'))

    assert.match(frame, /Enter view · x stop/)
    assert.match(frame, /read-recent-git-commits/)
    assert.match(frame, /1\/2 agents done/)
    assert.equal(frame.includes('Dynamic workflows'), false)
    assert.equal(sent.some(command => command.type === 'workflow_detail'), false)
  } finally {
    instance.unmount()
    timers.forEach(timer => clearTimeout(timer))
  }
})
