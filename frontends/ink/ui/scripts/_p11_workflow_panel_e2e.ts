import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import React from 'react'
import { render } from 'ink'
import stringWidth from 'string-width'
import { App } from '../src/App.js'
import { startBridge, type BridgeClient } from '../src/bridgeClient.js'
import { createCursorParkStdout } from '../src/stdoutCursorPark.js'
import { applyBridgeEvent, initialState, type AppState } from '../src/state.js'
import type { BridgeEvent } from '../src/protocol.js'

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../..')
const PYTHON = process.env.PYTHON || 'python'
const BRIDGE_SCRIPT = path.join(REPO, 'frontends', 'ink', 'bridge.py')
const COLUMNS = 100

class CaptureWriteStream extends EventEmitter {
  columns = COLUMNS
  rows = 30
  isTTY = true
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
const delay = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms))
function stripAnsi(text: string): string {
  const esc = String.fromCharCode(27)
  const bs = String.fromCharCode(92)
  const csi = new RegExp(esc + bs + '[[0-?]*[ -/]*[@-~]', 'g')
  return text.replace(csi, '').replace(new RegExp(esc + '[=>]', 'g'), '')
}

function frames(stream: CaptureWriteStream): string[] {
  return stream.chunks.map(stripAnsi).filter(chunk => chunk.includes('Enter send') || chunk.includes('Dynamic workflows') || chunk.includes('Enter view') || chunk.includes('Enter agent') || chunk.includes('Workflow '))
}
function validateFrame(frame: string, label: string): void {
  assert.ok(frame, `${label}: no frame`)
  assert.equal(frame.split('\n').every(line => stringWidth(line) < COLUMNS), true, `${label}: line overflow`)
}
async function waitFor(label: string, predicate: () => boolean, timeoutMs: number): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) { if (predicate()) return; await delay(100) }
  throw new Error(`${label} timed out`)
}
async function typeInput(stdin: FakeReadStream, text: string): Promise<void> {
  for (const char of text) { stdin.send(char); await delay(8) }
  stdin.send('\r')
}
async function main(): Promise<number> {
  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
  const stdin = new FakeReadStream()
  const cursorPark = createCursorParkStdout(stdout as unknown as NodeJS.WriteStream)
  const events: BridgeEvent[] = []
  let state: AppState = initialState
  let client: BridgeClient | null = null
  const startBridgeClient = (python: string, bridgeScript: string, onEvent: (event: BridgeEvent) => void, onExit: (code: number | null) => void): BridgeClient => {
    client = startBridge(python, bridgeScript, event => { events.push(event); state = applyBridgeEvent(state, event); onEvent(event) }, onExit)
    return { send(command) { client?.send(command) }, stop() { client?.stop() } }
  }
  const instance = render(React.createElement(App, { python: PYTHON, bridgeScript: BRIDGE_SCRIPT, startBridgeClient, cursorPark }), {
    stdout: cursorPark.stdout,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    patchConsole: false,
    debug: true,
  })
  const summary: Record<string, unknown> = { passed: false, columns: COLUMNS, rows: 30 }
  try {
    await waitFor('bridge ready', () => events.some(event => event.type === 'ready'), 60_000)
    const draftScript = [
      "export const meta = { name: 'p11-ui-detail', description: 'UI detail panel smoke' }",
      "return { ok: true }",
    ].join(String.fromCharCode(10))
    client?.send({ type: 'workflow_draft', script: draftScript })
    await waitFor('workflow draft event', () => events.some(event => event.type === 'workflow_draft'), 60_000)
    const before = events.filter(event => event.type === 'workflow_runs').length
    await typeInput(stdin, '/workflows')
    await waitFor('workflow list event', () => events.filter(event => event.type === 'workflow_runs').length > before, 60_000)
    await delay(400)
    const listFrame = frames(stdout).at(-1) || ''
    validateFrame(listFrame, 'workflow list')
    assert.ok(listFrame.includes('Dynamic workflows'), 'list frame missing Dynamic workflows')
    assert.ok(listFrame.includes('Enter view'), 'list frame missing Enter view')
    const listEvent = events.filter((event): event is Extract<BridgeEvent, {type:'workflow_runs'}> => event.type === 'workflow_runs').at(-1)!
    assert.ok(listEvent.runs.length > 0, 'workflow list is empty')
    summary.list = { runCount: listEvent.runs.length, selected: listEvent.runs[0].runId, status: listEvent.runs[0].status, frame: listFrame.slice(0, 700) }
    const detailBefore = events.filter(event => event.type === 'workflow_detail').length
    stdin.send('\r')
    await waitFor('workflow detail event', () => events.filter(event => event.type === 'workflow_detail').length > detailBefore, 60_000)
    await delay(400)
    const detailFrame = frames(stdout).at(-1) || ''
    validateFrame(detailFrame, 'workflow detail')
    assert.ok(detailFrame.includes('agents ·') || detailFrame.includes('Enter agent'), `detail frame missing overview markers: ${JSON.stringify(detailFrame.slice(0, 300))}`)
    const detailEvent = events.filter((event): event is Extract<BridgeEvent, {type:'workflow_detail'}> => event.type === 'workflow_detail').at(-1)!
    summary.detail = { runId: detailEvent.run.runId, status: detailEvent.run.status, jobs: detailEvent.run.jobs?.length ?? 0, frame: detailFrame.slice(0, 1000) }
    summary.passed = true
  } catch (error) {
    summary.error = String(error).replaceAll(REPO, '<repo>').slice(0, 1200)
    summary.eventTypes = [...new Set(events.map(event => event.type))]
    summary.lastFrame = (frames(stdout).at(-1) || '').slice(0, 1000)
    summary.state = { status: state.status, error: state.error, workflows: state.workflows.map(run => ({ runId: run.runId, status: run.status })) }
  } finally {
    instance.unmount()
    cursorPark.dispose()
    client?.stop()
  }
  console.log(JSON.stringify(summary, null, 2))
  return summary.passed === true ? 0 : 1
}
main().then(code => { process.exitCode = code }).catch(error => { console.log(JSON.stringify({ passed: false, error: String(error).slice(0, 1000) }, null, 2)); process.exitCode = 1 })
