/**
 * Real Ink UI E2E: spawn TWO subagents in one turn with a real LLM profile.
 *
 * Guards the historical failure mode where concurrent subagent cold start blew
 * the ~10s startup handshake (`startup_timeout: did not emit process_entry`).
 * Asserts both children register, both reach terminal `succeeded`, their turn
 * windows overlap (real concurrency), both authoritative results are readable,
 * and the UI returns to idle.
 */
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { readFile, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import React from 'react'
import { render } from 'ink'
import { App } from '../src/App.js'
import { startBridge, type BridgeClient } from '../src/bridgeClient.js'
import { createCursorParkStdout } from '../src/stdoutCursorPark.js'
import { applyBridgeEvent, initialState, type AppState } from '../src/state.js'
import type { BridgeCommand, BridgeEvent } from '../src/protocol.js'

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..')
const PYTHON = process.env.PYTHON || 'python'
const BRIDGE_SCRIPT = path.join(REPO, 'frontends', 'ink_bridge.py')
const PROFILE = process.env.GA_MULTI_SUBAGENT_E2E_PROFILE || 'deepseek-v4.1-flash'
const EXPECTED_MODEL = process.env.GA_MULTI_SUBAGENT_E2E_MODEL || `${PROFILE}/deepseek-v4.1-flash`
const START_TIMEOUT_MS = Number(process.env.GA_MULTI_SUBAGENT_E2E_START_TIMEOUT_MS || 300_000)
const TOTAL_TIMEOUT_MS = Number(process.env.GA_MULTI_SUBAGENT_E2E_TIMEOUT_MS || 600_000)
const STAMP = Date.now()
const NAMES = [`ga_multi_probe_a_${STAMP}`, `ga_multi_probe_b_${STAMP}`]
const MARKERS = ['GA_MULTI_SUBAGENT_A_OK_20261003', 'GA_MULTI_SUBAGENT_B_OK_20261003']

class CaptureWriteStream extends EventEmitter {
  columns = 100
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

const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))
const T0 = Date.now()
function progress(label: string, extra: Record<string, unknown> = {}): void {
  const suffix = Object.keys(extra).length > 0 ? ` ${JSON.stringify(extra)}` : ''
  console.error(`[multi-subagent-e2e +${((Date.now() - T0) / 1000).toFixed(1)}s] ${label}${suffix}`)
}
async function waitFor(label: string, predicate: () => boolean, timeoutMs: number): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (predicate()) return
    await delay(100)
  }
  throw new Error(`${label} timed out`)
}
async function typeInput(stdin: FakeReadStream, text: string): Promise<void> {
  for (const char of text) { stdin.send(char); await delay(4) }
  stdin.send(String.fromCharCode(13))
}
function safeError(value: unknown): string {
  return String(value)
    .replace(/Bearer\s+[^\s"']+/gi, 'Bearer [REDACTED]')
    .replace(/sk-[A-Za-z0-9_-]{8,}/g, '[REDACTED_KEY]')
    .slice(0, 800)
}

async function main(): Promise<number> {
  process.env.GA_WORKFLOW_PLANNER_MODE = 'real'
  delete process.env.GA_INK_MOUSE

  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
  const stdin = new FakeReadStream()
  const cursorPark = createCursorParkStdout(stdout as unknown as NodeJS.WriteStream)
  const events: BridgeEvent[] = []
  const commands: BridgeCommand[] = []
  let state: AppState = initialState
  const holder: { client: BridgeClient | null } = { client: null }

  const startBridgeClient = (
    python: string, bridgeScript: string,
    onEvent: (event: BridgeEvent) => void,
    onExit: (code: number | null) => void,
  ): BridgeClient => {
    holder.client = startBridge(python, bridgeScript, event => {
      events.push(event)
      state = applyBridgeEvent(state, event)
      onEvent(event)
    }, onExit)
    return {
      send(command) { commands.push(command); holder.client?.send(command) },
      stop() { holder.client?.stop() },
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
    debug: true,
  })

  const summary: Record<string, unknown> = {
    passed: false,
    profile: PROFILE,
    expectedModel: EXPECTED_MODEL,
    subagents: NAMES,
  }

  try {
    await waitFor('bridge ready', () => events.some(event => event.type === 'ready'), 60_000)
    progress('bridge ready')
    await typeInput(stdin, `/model ${PROFILE}`)
    progress('switching model', { profile: PROFILE })
    await waitFor(`${PROFILE} model selected`, () => (
      events.some(event => event.type === 'model_switch_result' && event.ok)
      && events.some(event => event.type === 'model_status'
        && event.models.some(model => model.current && model.name === EXPECTED_MODEL))
    ), 60_000)
    progress('model selected')

    await typeInput(stdin, '/permissions full')
    await waitFor('full access selected', () => (
      events.some(event => event.type === 'permission_switch_result' && event.mode === 'full_access')
    ), 30_000)
    progress('full access selected')

    const spawnPrompt = [
      '这是一次真实 GA UI 多 subagent 并发 E2E。用户明确要求你使用两个子 agent，必须实际调用工具，不要直接伪造最终答案。',
      '在同一个回合内一次性调用 spawn_agent 两次（不要等待第一个完成再起第二个），不要用 code_run 探查模型索引或文件系统，两个任务分别是：',
      `  1) task_name=${NAMES[0]}，fork_turns="none"，不要传 llm_no（子 agent 默认继承当前模型），message 要求子 agent 不调用工具、不输出 Markdown，只输出这一行精确文本：${MARKERS[0]}。`,
      `  2) task_name=${NAMES[1]}，fork_turns="none"，不要传 llm_no，message 要求子 agent 不调用工具、不输出 Markdown，只输出这一行精确文本：${MARKERS[1]}。`,
      '两个 spawn_agent 都成功启动后，本轮立即返回一行简短 ACK，不要在本轮调用 wait_agent、read_agent_result 或 close_agent。',
    ].join('\n')
    const firstDoneCount = events.filter(event => event.type === 'assistant_done').length
    progress('submitting spawn prompt', { names: NAMES })
    await typeInput(stdin, spawnPrompt)
    await waitFor('spawn submit command', () => commands.some(c => c.type === 'submit' && c.text === spawnPrompt), 20_000)
    await waitFor('spawn turn returned', () => (
      events.filter(event => event.type === 'assistant_done').length > firstDoneCount
      && events.some(event => event.type === 'status' && event.status === 'idle')
    ), START_TIMEOUT_MS)
    progress('spawn turn returned')
    assert.equal(state.error, null, `spawn bridge error: ${state.error}`)

    const spawnErrors = events
      .filter((event): event is Extract<BridgeEvent, { type: 'error' }> => event.type === 'error')
      .filter(event => /spawn|startup|subagent/i.test(`${event.code} ${event.message}`))
    summary.spawnErrors = spawnErrors.map(event => ({ code: event.code, message: safeError(event.message) }))
    assert.equal(spawnErrors.length, 0, `subagent spawn errors were surfaced: ${JSON.stringify(summary.spawnErrors)}`)

    // Refresh the on-demand agent read model until both children are terminal.
    const deadline = Date.now() + TOTAL_TIMEOUT_MS
    let refreshes = 0
    let sawConcurrentRunning = false
    while (Date.now() < deadline) {
      const listCommands = commands.filter(c => c.type === 'workflow_list').length
      const listEvents = events.filter(e => e.type === 'workflow_runs').length
      await typeInput(stdin, '/workflows')
      const remaining = Math.max(1_000, deadline - Date.now())
      await waitFor('workflow list command', () => commands.filter(c => c.type === 'workflow_list').length > listCommands, Math.min(10_000, remaining))
      await waitFor('workflow list event', () => events.filter(e => e.type === 'workflow_runs').length > listEvents, Math.min(60_000, remaining))
      refreshes += 1
      const mine = state.agents.filter(record => (
        record.recordKind === 'process_agent'
        && NAMES.some(name => record.agentPath?.endsWith('/' + name))
      ))
      if (mine.filter(record => record.status === 'running').length >= 2) sawConcurrentRunning = true
      const terminal = NAMES.every(name => mine.some(record => (
        record.agentPath?.endsWith('/' + name)
        && ['succeeded', 'failed', 'error', 'cancelled', 'killed'].includes(record.status)
      )))
      if (terminal) break
      stdin.send('\u001b')
      await delay(200)
    }
    // The loop can exit straight from a /workflows panel; close it before typing the next
    // prompt or the characters land in the panel instead of the composer.
    stdin.send('\u001b')
    await delay(250)

    const records = NAMES.map(name => state.agents.find(record => (
      record.recordKind === 'process_agent' && record.agentPath?.endsWith('/' + name)
    )))
    summary.agentRecords = records.map(record => record ? {
      name: record.agentPath,
      status: record.status,
      sourceStatus: record.sourceStatus,
      runId: record.runId,
      transcriptRef: record.transcriptRef,
      workspace: record.workspace,
    } : null)
    summary.readModelRefreshes = refreshes
    summary.sawConcurrentRunning = sawConcurrentRunning

    for (let index = 0; index < NAMES.length; index += 1) {
      const record = records[index]
      assert.ok(record, `subagent ${NAMES[index]} never appeared in the Ink read model`)
      assert.equal(record!.status, 'succeeded', `subagent ${NAMES[index]} ended ${record!.status}`)
    }
    // The bridge projects terminal state through the agent_snapshot read model; agent_event is
    // only an optimization for live deltas and is not guaranteed to carry every turn boundary.
    for (let index = 0; index < NAMES.length; index += 1) {
      const record = records[index]!
      assert.equal(record.sourceStatus, 'waiting_reply',
        `${NAMES[index]} did not report a completed turn (sourceStatus=${record.sourceStatus})`)
    }

    // Concurrency evidence: the two children turn windows must overlap.
    const windows = await Promise.all(records.map(async record => {
      const taskDir = record?.workspace ? path.resolve(String(record.workspace)) : null
      if (!taskDir) return null
      const rows = await readFile(path.join(taskDir, 'events.jsonl'), 'utf8')
        .then(text => text.split('\n').filter(Boolean).map(line => JSON.parse(line) as Record<string, unknown>))
        .catch(() => [] as Array<Record<string, unknown>>)
      const stamp = (type: string) => {
        const row = rows.find(item => item.type === type)
        return row?.ts ? Date.parse(String(row.ts)) : null
      }
      return { taskDir, started: stamp('turn_started'), completed: stamp('turn_completed') }
    }))
    summary.childWindows = windows
    const [first, second] = windows
    if (first?.started != null && second?.started != null) {
      const startedGapMs = Math.abs(first.started - second.started)
      const overlapped = first.started < (second.completed ?? Infinity) && second.started < (first.completed ?? Infinity)
      summary.startedGapMs = startedGapMs
      summary.overlappedExecution = overlapped
      assert.ok(overlapped, 'the two subagents did not have overlapping execution windows')
    }

    progress('both children terminal')
    const readPrompt = [
      `现在依次调用 wait_agent 等待 ${NAMES[0]} 和 ${NAMES[1]}（timeout_seconds 使用 5），`,
      '然后对每个子 agent 调用 read_agent_result 读取权威最终结果。',
      `最终只输出两行，第一行必须包含 ${MARKERS[0]}，第二行必须包含 ${MARKERS[1]}；不要伪造，不要再次 spawn。`,
    ].join('\n')
    const secondDoneCount = events.filter(event => event.type === 'assistant_done').length
    progress('submitting read prompt')
    await typeInput(stdin, readPrompt)
    await waitFor('read submit command', () => commands.some(c => c.type === 'submit' && c.text === readPrompt), 20_000)
    await waitFor('read turn returned', () => (
      events.filter(event => event.type === 'assistant_done').length > secondDoneCount
      && events.filter(event => event.type === 'status' && event.status === 'idle').length >= 2
    ), TOTAL_TIMEOUT_MS)
    progress('read turn returned')
    const assistantText = events
      .filter((event): event is Extract<BridgeEvent, { type: 'assistant_done' }> => event.type === 'assistant_done')
      .slice(-1).map(event => event.text).join('\n')
    summary.readbackMarkers = MARKERS.map(marker => assistantText.includes(marker))
    assert.ok(assistantText.includes(MARKERS[0]), `readback missing ${MARKERS[0]}`)
    assert.ok(assistantText.includes(MARKERS[1]), `readback missing ${MARKERS[1]}`)
    assert.equal(state.status, 'idle', 'Ink UI did not return to idle after multi-subagent rounds')

    summary.passed = true
    summary.uiReturnedToIdle = true
  } catch (error) {
    summary.error = safeError(String(error).replaceAll(REPO, '<repo>').slice(0, 1200))
    summary.eventTypes = [...new Set(events.map(event => event.type))]
    summary.bridgeErrors = events
      .filter((event): event is Extract<BridgeEvent, { type: 'error' }> => event.type === 'error')
      .map(event => ({ code: event.code, message: safeError(event.message) }))
    summary.uiError = safeError(state.error)
    summary.agentStatuses = state.agents.map(record => ({ kind: record.recordKind, path: record.agentPath, status: record.status }))
    summary.assistantTail = events
      .filter((event): event is Extract<BridgeEvent, { type: 'assistant_done' }> => event.type === 'assistant_done')
      .slice(-1).map(event => safeError(event.text)).join('\n')
  } finally {
    instance.unmount()
    cursorPark.dispose()
    holder.client?.stop()
  }
  const outFile = process.env.GA_MULTI_SUBAGENT_E2E_OUT
  if (outFile) await writeFile(outFile, JSON.stringify(summary, null, 2), 'utf8')
  console.log(JSON.stringify(summary, null, 2))
  return summary.passed === true ? 0 : 1
}

main().then(code => { process.exitCode = code }).catch(error => {
  console.log(JSON.stringify({ passed: false, error: safeError(error) }, null, 2))
  process.exitCode = 1
})
