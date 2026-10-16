// Probe: how expensive is one Ink render per bridge event during a workflow
// run, and does it scale with transcript length?
//
// Motivation: the "event -> frame throttle (Codex schedule_frame())" backlog
// item assumes many events per second each cost a full render. This probe
// measures that assumption directly.
//
// Findings (2026-10-14, Windows, Node 22):
//   * Ink flushes synchronously per `dispatch`: a burst of N events -> N writes.
//   * <Static> means transcript length does NOT affect per-render cost; only
//     the live chrome (status bar / open panel) is re-rendered.
//   * workflow_progress (status bar): ~1.5 ms/render, ~400 B/frame.
//   * workflow_live (panel open): ~4.6 ms/render, ~590 B/frame.
//   At the observed ~8 events/s that is <= ~4% CPU, so coalescing saves ~1-3%.
//
// Run: npx tsx scripts/_probe_workflow_frame_cost.ts
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from '../src/App.js'
import type { BridgeClient } from '../src/bridgeClient.js'
import type { BridgeCommand, BridgeEvent } from '../src/protocol.js'

class CountingStream extends EventEmitter {
  columns = 120
  rows = 40
  writes = 0
  bytes = 0
  write(chunk: unknown): boolean {
    this.writes += 1
    this.bytes += String(chunk).length
    return true
  }
}

class SinkStream extends EventEmitter {
  columns = 120
  rows = 40
  write(): boolean {
    return true
  }
}

class FakeRead extends EventEmitter {
  isTTY = true
  setRawMode() { return this }
  setEncoding() { return this }
  ref() { return this }
  unref() { return this }
  resume() { return this }
  pause() { return this }
  read() { return null }
}

const delay = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms))

const RUN = {
  runId: 'wf_probe',
  sessionId: 'probe',
  status: 'running',
  metadata: { workflowName: 'probe-flow' },
  jobs: [{ jobId: 'agent_1', status: 'running', metadata: { label: 'scout' } }],
}

async function setup(transcriptMessages: number, openPanel: boolean) {
  const stdout = new CountingStream()
  const stdin = new FakeRead()
  let emit: ((event: BridgeEvent) => void) | null = null
  const startBridgeClient = (
    _python: string,
    _bridgeScript: string,
    onEvent: (event: BridgeEvent) => void,
  ): BridgeClient => {
    emit = onEvent
    return { send(_command: BridgeCommand) {}, stop() {} }
  }
  const instance = render(
    React.createElement(App, { python: 'python', bridgeScript: 'x.py', startBridgeClient }),
    {
      stdout: stdout as unknown as NodeJS.WriteStream,
      stderr: new SinkStream() as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      patchConsole: false,
    },
  )
  await delay(40)
  emit!({ type: 'ready', version: 1 } as unknown as BridgeEvent)
  await delay(40)
  if (transcriptMessages > 0) {
    emit!({
      type: 'history_replace',
      messages: Array.from({ length: transcriptMessages }, (_, k) => ({
        role: k % 2 === 0 ? 'user' : 'assistant',
        text: `message ${k} ` + 'x'.repeat(200),
      })),
    } as unknown as BridgeEvent)
    await delay(150)
  }
  emit!({ type: 'workflow_run', run: RUN } as unknown as BridgeEvent)
  await delay(80)
  if (openPanel) {
    emit!({
      type: 'workflow_detail',
      run: RUN,
      script: 'x',
      events: [],
      draft: null,
      progress: { runId: RUN.runId, status: 'running', workflowProgress: [{ jobId: 'agent_1', label: 'scout', state: 'running' }] },
    } as unknown as BridgeEvent)
    await delay(100)
  }
  return { stdout, instance, emit: emit! }
}

function progressEvent(i: number): BridgeEvent {
  return {
    type: 'workflow_progress',
    progress: {
      runId: RUN.runId,
      status: 'running',
      workflowProgress: [{ jobId: 'agent_1', label: 'scout', state: 'running', lastToolName: `tool_${i}` }],
    },
  } as unknown as BridgeEvent
}

function liveEvent(i: number): BridgeEvent {
  return {
    type: 'workflow_live',
    runId: RUN.runId,
    jobs: [{ jobId: 'agent_1', turn: i, toolCalls: i, lastToolName: `tool_${i}`, tokenUsage: { total_tokens: i * 10 }, elapsedSeconds: i }],
  } as unknown as BridgeEvent
}

async function measure(label: string, make: (i: number) => BridgeEvent, transcriptMessages: number, openPanel: boolean, events = 150) {
  const { stdout, instance, emit } = await setup(transcriptMessages, openPanel)
  const samples: number[] = []
  stdout.writes = 0
  stdout.bytes = 0
  for (let i = 0; i < events; i += 1) {
    const t0 = performance.now()
    emit(make(i))
    samples.push(performance.now() - t0)
  }
  const sorted = [...samples].sort((a, b) => a - b)
  const median = sorted[Math.floor(sorted.length / 2)]
  const result = {
    label,
    transcriptMessages,
    panelOpen: openPanel,
    events,
    renders: stdout.writes,
    rendersPerEvent: +(stdout.writes / events).toFixed(3),
    bytesPerRender: Math.round(stdout.bytes / Math.max(1, stdout.writes)),
    perRenderMs: {
      median: +median.toFixed(2),
      p90: +sorted[Math.floor(sorted.length * 0.9)].toFixed(2),
      max: +sorted[sorted.length - 1].toFixed(2),
    },
    at8EventsPerSecCpuPct: +(median * 8 / 10).toFixed(2),
  }
  instance.unmount()
  await delay(30)
  return result
}

async function main() {
  const rows: unknown[] = []
  rows.push(await measure('workflow_progress / status bar / 20 msgs', progressEvent, 20, false))
  rows.push(await measure('workflow_progress / status bar / 1500 msgs', progressEvent, 1500, false))
  rows.push(await measure('workflow_live / panel open / 1500 msgs', liveEvent, 1500, true))
  console.log(JSON.stringify(rows, null, 2))
}

main().catch(error => {
  console.error(error)
  process.exitCode = 1
})
