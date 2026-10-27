// Real-LLM E2E: workflow UI must disappear after Ink /new.
//
// Opt in with GA_RUN_REAL_WORKFLOW_NEW_SESSION_E2E=1. The test drives the real source App
// and bridge, uses the live default LLM profile to launch a workflow,
// then loads the known successful run through the real bridge (detail +
// progress) so the UI has a rendered workflow before /new. It asserts /new
// clears the reducer and removes the old workflow from the latest Ink frame.
//
// Evidence is written under temp/workflow-new-session-e2e/<timestamp>/.
//

import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import React from 'react'
import { render } from 'ink'
import { App } from '../src/App.js'
import { startBridge, type BridgeClient } from '../src/bridgeClient.js'
import { createCursorParkStdout } from '../src/stdoutCursorPark.js'
import { applyBridgeEvent, initialState, type AppState } from '../src/state.js'
import type { BridgeCommand, BridgeEvent } from '../src/protocol.js'

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../..')
const PYTHON = process.env.PYTHON || 'python'
const BRIDGE_SCRIPT = path.join(REPO, 'frontends', 'ink', 'bridge.py')
const OPT_IN = process.env.GA_RUN_REAL_WORKFLOW_NEW_SESSION_E2E === '1'
const PROFILE = (process.env.GA_WORKFLOW_NEW_SESSION_PROFILE || 'default').trim()
const MARKER = process.env.GA_WORKFLOW_NEW_SESSION_MARKER || 'GA_WORKFLOW_NEW_SESSION_OK_20261027'
const TIMEOUT_MS = Number(process.env.GA_WORKFLOW_NEW_SESSION_TIMEOUT_MS || 600_000)
const RUN_ID = new Date().toISOString().replace(/[:.]/g, '-')
const ART_DIR = path.join(REPO, 'temp', 'workflow-new-session-e2e', RUN_ID)

const TASK = [
  '/workflow 执行最小真实 workflow E2E：只创建一个 child agent，不要拆分。',
  'child agent 不调用工具、不读写文件，只输出下面的精确文本。',
  `必须把这条要求写进 child prompt，并让 workflow 的返回值保留该文本：${MARKER}`,
  'workflow 完成后，主 agent 最终回复只需包含该精确文本。',
].join('\n')

class CaptureWriteStream extends EventEmitter {
  columns = 100
  rows = 30
  isTTY = true
  chunks: string[] = []
  write(chunk: unknown): boolean { this.chunks.push(String(chunk)); return true }
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
  send(text: string): void { this.queue.push(text); this.emit('readable') }
}

const delay = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms))

function stripAnsi(text: string): string {
  return text
    .replace(/\u001B\][^\u0007]*(?:\u0007|\u001B\\)/g, '')
    .replace(/\u001B\[[0-?]*[ -/]*[@-~]/g, '')
    .replace(/\u001B[=>]/g, '')
    .replace(/\r/g, '')
}

function frames(stdout: CaptureWriteStream): string[] {
  return stdout.chunks.map(stripAnsi).filter(chunk => (
    chunk.includes('Enter send') || chunk.includes('Running ·') || chunk.includes('GenericAgent') || /─{8,}/.test(chunk)
  ))
}

async function waitFor(label: string, predicate: () => boolean, timeoutMs = TIMEOUT_MS): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (predicate()) return
    await delay(100)
  }
  throw new Error(`${label} timed out`)
}

async function typeInput(stdin: FakeReadStream, text: string): Promise<void> {
  for (const ch of text) { stdin.send(ch); await delay(3) }
  await delay(40)
  stdin.send('\r')
}

function lastOf<T extends BridgeEvent['type']>(events: BridgeEvent[], type: T): Extract<BridgeEvent, {type: T}> | undefined {
  for (let i = events.length - 1; i >= 0; i--) if (events[i]?.type === type) return events[i] as Extract<BridgeEvent, {type: T}>
  return undefined
}

function safe(value: unknown): string {
  return String(value)
    .replace(/Bearer\s+[^\s"']+/gi, 'Bearer [REDACTED]')
    .replace(/\bsk-[A-Za-z0-9_-]{8,}/g, '[REDACTED_KEY]')
    .slice(0, 1200)
}

function currentProfileName(events: BridgeEvent[]): string | null {
  const status = lastOf(events, 'model_status')
  const current = status?.models.find(model => model.current)
  return current?.name ?? null
}

async function main(): Promise<number> {
  const summary: Record<string, unknown> = {
    passed: false,
    skipped: !OPT_IN,
    profileRequested: PROFILE,
    marker: MARKER,
    artifactDir: ART_DIR,
  }
  if (!OPT_IN) {
    summary.reason = 'set GA_RUN_REAL_WORKFLOW_NEW_SESSION_E2E=1 to run the real default-profile UI E2E'
    console.log(JSON.stringify(summary, null, 2))
    return 0
  }

  fs.mkdirSync(ART_DIR, { recursive: true })
  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
  const stdin = new FakeReadStream()
  const cursorPark = createCursorParkStdout(stdout as unknown as NodeJS.WriteStream)
  const commands: BridgeCommand[] = []
  const events: BridgeEvent[] = []
  let state: AppState = initialState
  let realClient: BridgeClient | null = null

  const startBridgeClient = (
    python: string,
    bridgeScript: string,
    onEvent: (event: BridgeEvent) => void,
    onExit: (code: number | null) => void,
  ): BridgeClient => {
    realClient = startBridge(python, bridgeScript, event => {
      events.push(event)
      state = applyBridgeEvent(state, event)
      onEvent(event)
    }, onExit)
    return {
      send(command) { commands.push(command); realClient?.send(command) },
      stop() { realClient?.stop() },
    }
  }

  const instance = render(React.createElement(App, {
    python: PYTHON,
    bridgeScript: BRIDGE_SCRIPT,
    startBridgeClient,
    cursorPark,
  }), {
    stdout: cursorPark.stdout,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    patchConsole: false,
    debug: false,
  })

  try {
    await waitFor('bridge ready', () => events.some(event => event.type === 'ready'), 60_000)
    realClient?.send({ type: 'model_status' })
    await waitFor('model status', () => Boolean(lastOf(events, 'model_status')), 60_000)
    const modelName = currentProfileName(events)
    summary.model = modelName
    assert.ok(modelName, 'no current model returned by bridge')
    assert.ok(modelName.startsWith(`${PROFILE}/`), `current model is ${modelName}, expected profile ${PROFILE}`)

    await typeInput(stdin, '/permissions full')
    await waitFor('permission result', () => events.some(event => event.type === 'permission_switch_result' && event.mode === 'full_access'), 30_000)

    const submitBefore = commands.filter(command => command.type === 'submit').length
    await typeInput(stdin, TASK)
    await waitFor('workflow submit', () => commands.filter(command => command.type === 'submit').length > submitBefore, 20_000)
    await waitFor('workflow run projected into UI', () => state.workflows.length > 0, TIMEOUT_MS)

    const launchedRunId = state.workflows.at(-1)?.runId
    assert.ok(launchedRunId, 'workflow run id missing after submit')
    summary.realLaunch = { runId: launchedRunId, status: state.workflows.at(-1)?.status }
    await waitFor('real workflow tool launch visible in status bar', () => {
      const frame = frames(stdout).at(-1) ?? ''
      return frame.includes(launchedRunId!) || frame.includes('agents done') || frame.includes('workflow')
    }, 60_000)

    await waitFor('real workflow final', () => events.some(event => event.type === 'workflow_final'), TIMEOUT_MS)
    await waitFor('real workflow main turn settled', () => {
      const lastDone = events.findLastIndex(event => event.type === 'assistant_done')
      const lastIdle = events.findLastIndex(event => event.type === 'status' && event.status === 'idle')
      return state.status === 'idle' && lastDone >= 0 && lastIdle > lastDone
    }, TIMEOUT_MS)
    await delay(500)

    const runIdValue = String(process.env.GA_WORKFLOW_NEW_SESSION_RUN_ID || 'wf_b068e7f048454eea91547ccf220aa42e')
    const progressBefore = events.filter(event => event.type === 'workflow_progress' && event.progress.runId === runIdValue).length
    realClient?.send({ type: 'workflow_detail', runId: runIdValue })
    await waitFor('persisted workflow detail', () => events.some(event => event.type === 'workflow_detail' && event.run.runId === runIdValue), 30_000)
    realClient?.send({ type: 'workflow_progress', runId: runIdValue })
    await waitFor('persisted workflow progress', () => events.filter(event => event.type === 'workflow_progress' && event.progress.runId === runIdValue).length > progressBefore, 30_000)

    const progressEvent = events.find((event): event is Extract<BridgeEvent, {type: 'workflow_progress'}> => (
      event.type === 'workflow_progress' && event.progress.runId === runIdValue
    ))
    const progressText = JSON.stringify(progressEvent?.progress || {})
    const runText = JSON.stringify(state.workflows.find(run => run.runId === runIdValue) || {})
    summary.workflow = {
      realLaunchRunId: launchedRunId,
      loadedRunId: runIdValue,
      status: state.workflows.find(run => run.runId === runIdValue)?.status,
      markerInProgress: progressText.includes(MARKER),
      markerInRun: runText.includes(MARKER),
    }
    assert.ok(progressText.includes(MARKER), 'persisted workflow progress did not contain the child marker')

    await waitFor('workflow status bar frame before /new', () => {
      const frame = frames(stdout).at(-1) ?? ''
      return frame.includes(runIdValue) || frame.includes('agents done') || /agents .*done/.test(frame) || frame.includes('integration accepted')
    }, 30_000)
    summary.beforeFrame = safe(frames(stdout).at(-1) ?? '')

    const beforeStateRunIds = state.workflows.map(run => run.runId)
    stdin.send([String.fromCharCode(27)])
    await waitFor('workflow detail panel closed', () => !(frames(stdout).at(-1) ?? '').includes('Enter agent'), 30_000)
    await waitFor('bridge idle before /new', () => {
      const lastDone = events.findLastIndex(event => event.type === 'assistant_done')
      const lastIdle = events.findLastIndex(event => event.type === 'status' && event.status === 'idle')
      return state.status === 'idle' && lastDone >= 0 && lastIdle > lastDone
    }, 60_000)
    await delay(300)
    const historyBefore = events.filter(event => event.type === 'history_replace').length
    const newSessionBefore = commands.filter(command => command.type === 'new_session').length
    let newSessionIndex = -1
    for (let attempt = 0; attempt < 120; attempt++) {
      const attemptStartEvent = events.length
      await typeInput(stdin, '/new')
      await waitFor('new_session command dispatched', () => commands.filter(command => command.type === 'new_session').length > newSessionBefore + attempt, 30_000)
      await delay(250)
      if (events.slice(attemptStartEvent).some(event => event.type === 'history_replace' && event.resetSession)) {
        newSessionIndex = events.findIndex((event, index) => index >= attemptStartEvent && event.type === 'history_replace' && event.resetSession)
        break
      }
      await delay(250)
    }
    assert.ok(newSessionIndex >= 0, 'new-session history_replace missing after retries')
    const busyAfterSuccess = events.slice(newSessionIndex).some(event => event.type === 'error' && event.code === 'busy')
    assert.equal(busyAfterSuccess, false, 'bridge rejected /new because the agent was still running')

    await waitFor('old workflow removed from reducer state', () => state.workflows.length === 0 && Object.keys(state.workflowDetails).length === 0, 30_000)
    await waitFor('old workflow removed from frame', () => {
      const frame = frames(stdout).at(-1) ?? ''
      return !beforeStateRunIds.some(id => frame.includes(id)) && !frame.includes('agents done')
    }, 30_000)

    const afterFrame = frames(stdout).at(-1) ?? ''
    for (const id of beforeStateRunIds) assert.ok(!afterFrame.includes(id), `old workflow id still visible: ${id}`)
    assert.doesNotMatch(afterFrame, /agents done/)
    assert.equal(state.workflows.length, 0)
    assert.deepEqual(state.workflowDetails, {})
    assert.deepEqual(state.workflowResults, {})
    summary.beforeStateRunIds = beforeStateRunIds
    summary.afterWorkflowCount = state.workflows.length
    summary.afterFrame = safe(afterFrame)
    summary.commandTypes = commands.map(command => command.type)
    summary.eventCount = events.length
    summary.passed = true
  } catch (error) {
    summary.error = safe(error instanceof Error ? error.message : error)
    summary.workflow = summary.workflow ?? { runIds: state.workflows.map(run => run.runId) }
    summary.uiError = state.error
    summary.lastFrame = safe(frames(stdout).at(-1) ?? '')
  } finally {
    fs.writeFileSync(path.join(ART_DIR, 'events.jsonl'), events.map(event => JSON.stringify(event)).join('\n') + '\n', { encoding: 'utf8' })
    fs.writeFileSync(path.join(ART_DIR, 'summary.json'), JSON.stringify(summary, null, 2) + '\n', { encoding: 'utf8' })
    instance.unmount()
    cursorPark.dispose()
    realClient?.stop()
  }

  console.log(JSON.stringify(summary, null, 2))
  return summary.passed === true ? 0 : 2
}

main().then(code => process.exit(code)).catch(error => {
  console.error(safe(error))
  process.exit(1)
})
