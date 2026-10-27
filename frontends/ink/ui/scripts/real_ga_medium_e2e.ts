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
const RUN_ID = process.env.GA_MEDIUM_E2E_RUNID || String(Date.now())
const ART_DIR = path.join(REPO, 'temp', `ga_medium_e2e_${RUN_ID}`)
const TIMEOUT_MS = Number(process.env.GA_MEDIUM_E2E_TIMEOUT_MS || 900_000)
const EXPECTED_MODEL = process.env.GA_MEDIUM_E2E_MODEL || 'default/deepseek-v4.1-flash'

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
async function waitFor(label: string, predicate: () => boolean, timeoutMs = TIMEOUT_MS): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (predicate()) return
    await delay(100)
  }
  throw new Error(`${label} timed out`)
}
async function typeInput(stdin: FakeReadStream, text: string): Promise<void> {
  for (const char of text) { stdin.send(char); await delay(3) }
  stdin.send(String.fromCharCode(13))
}
function safeError(value: unknown): string {
  return String(value)
    .replace(/Bearer\s+[^\s"']+/gi, 'Bearer [REDACTED]')
    .replace(/sk-[A-Za-z0-9_-]{8,}/g, '[REDACTED_KEY]')
    .replace(/([?&](?:api[_-]?key|token)=)[^&\s]+/gi, '$1[REDACTED]')
    .slice(0, 1_500)
}
function readJson(file: string): any {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')) } catch { return null }
}
function latestModel(events: BridgeEvent[]): string | null {
  const status = [...events].reverse().find((event): event is Extract<BridgeEvent, {type: 'model_status'}> => event.type === 'model_status')
  return status?.models.find(model => model.current)?.name || null
}

async function main(): Promise<number> {
  fs.mkdirSync(ART_DIR, { recursive: true })
  delete process.env.GA_INK_MOUSE
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
    debug: true,
  })

  const summary: Record<string, any> = {
    passed: false,
    runId: RUN_ID,
    artDir: ART_DIR,
    expectedModel: EXPECTED_MODEL,
    checks: [] as Array<{name: string; ok: boolean; detail?: unknown}>,
  }
  const check = (name: string, ok: boolean, detail: unknown = '') => {
    summary.checks.push({ name, ok: Boolean(ok), detail })
    console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${detail ? ' - ' + String(detail).slice(0, 500) : ''}`)
  }
  const eventErrors = () => events.filter((event): event is Extract<BridgeEvent, {type: 'error'}> => event.type === 'error')
  const nonFatalErrors = () => eventErrors().filter(event => event.code !== 'bridge_stderr')

  try {
    await waitFor('bridge ready', () => events.some(event => event.type === 'ready'), 60_000)
    check('real App bridge ready', true)

    const modelBefore = events.filter(event => event.type === 'model_status').length
    await typeInput(stdin, '/model ?')
    await waitFor('model status refresh', () => events.filter(event => event.type === 'model_status').length > modelBefore, 30_000)
    const model = latestModel(events)
    stdin.send(String.fromCharCode(27))
    await delay(150)
    check('default model is active', model === EXPECTED_MODEL, model)

    await typeInput(stdin, '/permissions full')
    await waitFor('full access selected', () => events.some(event => event.type === 'permission_switch_result' && event.mode === 'full_access'), 30_000)
    check('full access permission selected', true)

    realClient?.send({ type: 'mcp_watch_start' })
    await typeInput(stdin, '/mcp')
    await waitFor('MCP discovery completed', () => events.some((event) => {
      if (event.type !== 'mcp_status' && event.type !== 'mcp_progress') return false
      return event.loading === false && event.tools.some(tool => tool.function?.name === 'mcp__fetch__fetch_markdown')
    }), 180_000)
    const mcpEvents = events.filter((event): event is Extract<BridgeEvent, {type: 'mcp_status' | 'mcp_progress'}> => event.type === 'mcp_status' || event.type === 'mcp_progress')
    const mcp = [...mcpEvents].reverse().find(event => event.loading === false && event.tools.some(tool => tool.function?.name === 'mcp__fetch__fetch_markdown')) || mcpEvents.at(-1)
    const mcpTools = (mcp?.tools || []).map(tool => tool.function?.name || '').filter(Boolean)
    check('MCP tools discovered', mcpTools.includes('mcp__fetch__fetch_markdown'), mcpTools.slice(0, 20))
    summary.mcpServers = mcp?.servers?.map(server => ({ name: server.name, status: server.status })) || []
    summary.mcpTools = mcpTools
    stdin.send(String.fromCharCode(27))
    await delay(150)

    const notes = path.join(ART_DIR, 'ordinary.md')
    const ordinaryPrompt = [
      '这是一次真实 GA Ink 普通会话 E2E。必须实际调用工具，不要伪造。',
      `1) 用 file_write 在 ${notes} 写入两行：第一行 GA_MEDIUM_ORDINARY_OK，第二行 default model source test。`,
      `2) 用 file_read 读回 ${notes} 验证内容。`,
      '3) 不调用其他工具。最终只回复 ordinary_ok。',
    ].join('\n')
    const assistantBefore = events.filter(event => event.type === 'assistant_done').length
    await typeInput(stdin, ordinaryPrompt)
    await waitFor('ordinary turn finished', () => events.filter(event => event.type === 'assistant_done').length > assistantBefore && state.status === 'idle')
    const ordinaryDone = events.filter((event): event is Extract<BridgeEvent, {type: 'assistant_done'}> => event.type === 'assistant_done').at(-1)
    check('ordinary assistant turn returned', Boolean(ordinaryDone?.text?.trim()), ordinaryDone?.text)
    check('ordinary file write/read landed', fs.existsSync(notes) && fs.readFileSync(notes, 'utf8').includes('GA_MEDIUM_ORDINARY_OK'), notes)
    summary.ordinary = { prompt: ordinaryPrompt, done: ordinaryDone?.text || null, notes }

    const subA = `ga_medium_a_${RUN_ID}`.replace(/[^a-z0-9_]/g, '_').slice(0, 60)
    const subB = `ga_medium_b_${RUN_ID}`.replace(/[^a-z0-9_]/g, '_').slice(0, 60)
    const markerA = `GA_MEDIUM_SUB_A_${RUN_ID}`
    const markerB = `GA_MEDIUM_SUB_B_${RUN_ID}`
    const spawnPrompt = [
      '这是真实 GA Ink 双 subagent E2E。必须在同一回合并发调用两次 spawn_agent，不要自己完成任务。',
      `1) task_name=${subA}，fork_turns="none"，message 要求子代理只输出精确文本 ${markerA}，不要调用工具。`,
      `2) task_name=${subB}，fork_turns="none"，message 要求子代理只输出精确文本 ${markerB}，不要调用工具。`,
      '两次 spawn 都成功启动后，本轮立即回复 spawned，不要在本轮 wait/read/close。',
    ].join('\n')
    const subDoneBefore = events.filter(event => event.type === 'assistant_done').length
    await typeInput(stdin, spawnPrompt)
    await waitFor('subagent spawn turn finished', () => events.filter(event => event.type === 'assistant_done').length > subDoneBefore && state.status === 'idle')
    const spawnTurn = events.filter((event): event is Extract<BridgeEvent, {type: 'assistant_done'}> => event.type === 'assistant_done').at(-1)
    check('subagent spawn turn returned', Boolean(spawnTurn?.text?.trim()), spawnTurn?.text)
    const spawnErrors = eventErrors().filter(event => /spawn|startup|subagent/i.test(`${event.code} ${event.message}`))
    check('no subagent spawn error', spawnErrors.length === 0, spawnErrors.map(event => safeError(event.message)))

    const subDeadline = Date.now() + TIMEOUT_MS
    let snapshot: any = null
    let durableStates: any[] = []
    const scanDurableSubagents = () => {
      const dataRoot = path.join(process.env.LOCALAPPDATA || '', 'GenericAgent', 'projects')
      if (!fs.existsSync(dataRoot)) return []
      const rows: any[] = []
      for (const project of fs.readdirSync(dataRoot)) {
        const projectDir = path.join(dataRoot, project)
        if (!fs.statSync(projectDir).isDirectory()) continue
        for (const session of fs.readdirSync(projectDir)) {
          const tasks = path.join(projectDir, session, 'subagents', 'tasks')
          if (!fs.existsSync(tasks)) continue
          for (const task of fs.readdirSync(tasks)) {
            if (task !== subA && task !== subB) continue
            const statePath = path.join(tasks, task, 'state.json')
            const data = readJson(statePath)
            if (data) rows.push({ ...data, statePath })
          }
        }
      }
      return rows
    }
    let subagentsConverged = false
    let convergenceDeadline = 0
    while (Date.now() < subDeadline) {
      snapshot = [...events].reverse().find(event => event.type === 'agent_snapshot')?.snapshot || snapshot
      durableStates = scanDurableSubagents()
      const records = snapshot?.records || []
      const current = records.filter((record: any) => String(record.agentPath || '').includes(subA) || String(record.agentPath || '').includes(subB))
      const durableDone = durableStates.length >= 2 && durableStates.every((state: any) => state.turn_status === 'completed')
      if (durableDone && convergenceDeadline === 0) convergenceDeadline = Date.now() + 30_000
      if (durableDone && current.length >= 2 && current.every((record: any) => record.status === 'succeeded')) {
        subagentsConverged = true
        break
      }
      if (convergenceDeadline > 0 && Date.now() > convergenceDeadline) break
      await delay(250)
    }
    const records = snapshot?.records || []
    const ours = records.filter((record: any) => String(record.agentPath || '').includes(subA) || String(record.agentPath || '').includes(subB))
    check('two subagents appeared in read model', ours.length >= 2, ours.map((record: any) => ({ path: record.agentPath, status: record.status })))
    check('both subagents completed durably', durableStates.length >= 2 && durableStates.every(state => state.turn_status === 'completed'), durableStates.map(state => ({ name: state.task_name, turn: state.turn_status, process: state.process_status, error: state.last_error })))
    check('both subagents succeeded in read model', subagentsConverged, ours.map((record: any) => record.status))
    summary.subagents = ours.map((record: any) => ({ path: record.agentPath, status: record.status, transcriptRef: record.transcriptRef, metadata: record.metadata }))
    summary.subagentDurableStates = durableStates.map(state => ({ taskName: state.task_name, turnStatus: state.turn_status, processStatus: state.process_status, finalOutputPath: state.final_output_path, lastError: state.last_error }))
    summary.markers = { a: markerA, b: markerB }
    const readPrompt = [
      `现在依次调用 wait_agent 等 ${subA} 和 ${subB} 完成（timeout_seconds=5），`,
      '再分别调用 read_agent_result 读取它们的权威结果，并把两个 marker 原样写进最终回复。',
    ].join('\n')
    const readBefore = events.filter(event => event.type === 'assistant_done').length
    await typeInput(stdin, readPrompt)
    await waitFor('subagent readback turn finished', () => events.filter(event => event.type === 'assistant_done').length > readBefore && state.status === 'idle')
    const readDone = events.filter((event): event is Extract<BridgeEvent, {type: 'assistant_done'}> => event.type === 'assistant_done').at(-1)
    const readText = String(readDone?.text || '')
    check('subagent result A readable', readText.includes(markerA), readText)
    check('subagent result B readable', readText.includes(markerB), readText)
    summary.subagentReadback = readText

    const workflowBefore = events.filter(event => event.type === 'assistant_done').length
    const workflowPrompt = [
      '用户明确选择 dynamic workflow，请用 workflow 工具实际运行，不要自己直接完成。',
      '写一个最短的真实 workflow：使用 parallel() 并行启动 2 个 agent；',
      'agent A 只回复 WORKFLOW_A_OK；agent B 只回复 WORKFLOW_B_OK；',
      '然后串行启动 agent C，把 A/B 的两个结果汇总，返回字符串 WORKFLOW_MEDIUM_OK。',
      '脚本必须以 export const meta = { name: \'ga-medium-e2e\', description: \'medium e2e\' } 开始。',
      '等待 workflow 完成后，只回复 workflow_submitted。',
    ].join('\n')
    await typeInput(stdin, workflowPrompt)
    await waitFor('workflow turn or final observed', () => (
      events.filter(event => event.type === 'assistant_done').length > workflowBefore
      || state.workflows.some(run => run.status === 'running')
      || events.some(event => event.type === 'workflow_final')
    ), TIMEOUT_MS)
    await waitFor('workflow run entered Ink state', () => state.workflows.length > 0, TIMEOUT_MS)
    let wf = state.workflows.at(-1)
    // A model may first submit an invalid script and retry with a corrected
    // one. Select the successful run when present; otherwise retain the first
    // observed run so a genuine failure is reported as such.
    const wfRunIdsAtStart = new Set(state.workflows.map(run => run.runId))
    try {
      await waitFor('successful workflow run in Ink', () => state.workflows.some(run => run.status === 'succeeded'), TIMEOUT_MS)
    } catch {}
    wf = [...state.workflows].reverse().find(run => run.status === 'succeeded')
      || state.workflows.find(run => wfRunIdsAtStart.has(run.runId))
      || state.workflows.at(-1)
    check('workflow run projected to Ink', Boolean(wf?.runId), wf)
    if (wf?.runId) {
      await waitFor('workflow final in Ink', () => events.some(event => event.type === 'workflow_final' && event.runId === wf.runId), TIMEOUT_MS)
      await waitFor('workflow returned to idle', () => state.status === 'idle', 120_000)
    }
    const finalWf = state.workflows.find(run => run.runId === wf?.runId) || wf
    check('workflow succeeded', finalWf?.status === 'succeeded', finalWf)
    check('workflow artifact dir exists', Boolean(finalWf?.artifactDir && fs.existsSync(finalWf.artifactDir)), finalWf?.artifactDir)
    if (finalWf?.artifactDir) {
      const artifactDir = finalWf.artifactDir
      check('workflow persisted script/state/journal', ['script.js', 'state.json', 'journal.jsonl'].every(name => fs.existsSync(path.join(artifactDir, name))), artifactDir)
      const runJson = readJson(path.join(artifactDir, 'run.json'))
      const jobs = runJson?.jobs || []
      check('workflow ran >=3 jobs', jobs.length >= 3, jobs.map((job: any) => ({ id: job.jobId, status: job.status, label: job.metadata?.label })))
      check('all workflow jobs succeeded', jobs.length >= 3 && jobs.every((job: any) => ['succeeded', 'cached'].includes(job.status)), jobs.map((job: any) => job.status))
      summary.workflow = { runId: finalWf.runId, status: finalWf.status, artifactDir, jobs: jobs.map((job: any) => ({ status: job.status, label: job.metadata?.label })) }
    }

    await typeInput(stdin, '/workflows')
    await waitFor('/workflows rendered a list', () => events.some(event => event.type === 'workflow_runs'), 30_000)
    const workflowsEvent = [...events].reverse().find((event): event is Extract<BridgeEvent, {type: 'workflow_runs'}> => event.type === 'workflow_runs')
    check('/workflows contains this run', Boolean(workflowsEvent?.runs.some(run => run.runId === finalWf?.runId)), workflowsEvent?.runs.map(run => run.runId))
    stdin.send(String.fromCharCode(27))
    await delay(150)

    await typeInput(stdin, '/team')
    await waitFor('/team returned roster', () => events.some(event => event.type === 'team_roster'), 30_000)
    const roster = [...events].reverse().find((event): event is Extract<BridgeEvent, {type: 'team_roster'}> => event.type === 'team_roster')
    check('/team rendered roster payload', Boolean(roster), roster)
    stdin.send(String.fromCharCode(27))
    await delay(150)

    const fatal = nonFatalErrors().filter(event => !/provider|429|rate.?limit/i.test(event.message))
    check('no fatal bridge error', fatal.length === 0, fatal.map(event => ({ code: event.code, message: safeError(event.message) })))
    check('Ink returned to idle', state.status === 'idle', state.status)
    check('rendered Ink frames captured', stdout.chunks.length > 0 && stdout.chunks.some(chunk => chunk.includes('GenericAgent') || chunk.includes('Enter send')), stdout.chunks.length)
    summary.passed = summary.checks.every((item: any) => item.ok)
  } catch (error) {
    summary.error = safeError(error)
    summary.eventTypes = [...new Set(events.map(event => event.type))]
    summary.bridgeErrors = nonFatalErrors().map(event => ({ code: event.code, message: safeError(event.message) }))
    summary.state = { status: state.status, error: safeError(state.error), workflows: state.workflows.map(run => ({runId: run.runId, status: run.status, artifactDir: run.artifactDir, error: safeError(run.error)})) }
    summary.lastChunk = String(stdout.chunks.at(-1) || '').slice(0, 1200)
    summary.passed = false
  } finally {
    fs.writeFileSync(path.join(ART_DIR, 'summary.json'), JSON.stringify(summary, null, 2), { encoding: 'utf8' })
    instance.unmount()
    cursorPark.dispose()
    realClient?.stop()
  }
  console.log(JSON.stringify(summary, null, 2))
  return summary.passed === true ? 0 : 1
}

main().then(code => { process.exitCode = code }).catch(error => {
  console.log(JSON.stringify({ passed: false, error: safeError(error) }, null, 2))
  process.exitCode = 1
})
