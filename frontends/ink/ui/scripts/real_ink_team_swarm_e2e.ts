import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import React from 'react'
import { render } from 'ink'
import stringWidth from 'string-width'
import { App } from '../src/App.js'
import { startBridge, type BridgeClient } from '../src/bridgeClient.js'
import { createCursorParkStdout } from '../src/stdoutCursorPark.js'
import { applyBridgeEvent, initialState, type AppState } from '../src/state.js'
import type { BridgeCommand, BridgeEvent } from '../src/protocol.js'

// Real-LLM agent-team e2e driven through the Ink input box (see
// docs/20261015-GA-ink-真实LLM-e2e-自测SOP.md). Uses the *current* model
// (default deepseek-flash); no model switch. Types a hard, decomposable task
// and asserts the team really formed (task board + team_mode teammates) and
// produced real artifacts.

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../..')
const PYTHON = process.env.PYTHON || 'python'
const BRIDGE_SCRIPT = path.join(REPO, 'frontends', 'ink', 'bridge.py')
const RUN_ID = process.env.GA_TEAM_E2E_RUNID || String(Date.now())
const ART_DIR = path.join(REPO, 'temp', `team_e2e_${RUN_ID}`)
const MIN_MEMBERS = Number(process.env.GA_TEAM_E2E_MIN_MEMBERS || 2)
const TIMEOUT_MS = Number(process.env.GA_TEAM_E2E_TIMEOUT_MS || 720_000)
const CUSTOM_PROMPT = process.env.GA_TEAM_E2E_PROMPT || ''
const SOFT = process.env.GA_TEAM_E2E_SOFT === '1'

class CaptureWriteStream extends EventEmitter {
  columns = 80
  rows = 24
  isTTY = true
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
  resume(): this { return this }
  pause(): this { return this }
  read(): string | null { return this.queue.shift() ?? null }
  send(text: string): void {
    this.queue.push(text)
    this.emit('readable')
  }
}

function delay(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}

function stripAnsi(text: string): string {
  return text
    .replace(/\u001B\][^\u0007]*(?:\u0007|\u001B\\)/g, '')
    .replace(/\u001B\[[0-?]*[ -/]*[@-~]/g, '')
    .replace(/\u001B[=>]/g, '')
    .replace(/\r/g, '')
}

function chromeFrames(stdout: CaptureWriteStream): string[] {
  return stdout.chunks
    .map(stripAnsi)
    .filter(chunk => (
      chunk.includes('Enter send')
      || chunk.includes('Running ·')
      || chunk.includes('GenericAgent')
      || /─{8,}/.test(chunk)
    ))
}

function validateFrame(frame: string, label: string): void {
  assert.ok(frame, `${label}: no live Ink frame captured`)
  const lines = frame.split('\n')
  assert.equal(
    lines.every(line => stringWidth(line) < 80),
    true,
    `${label}: a rendered line reaches/exceeds terminal width`,
  )
}

async function waitFor(label: string, predicate: () => boolean, timeoutMs: number): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (predicate()) return
    await delay(100)
  }
  throw new Error(`${label} timed out`)
}

async function typeUserInput(stdin: FakeReadStream, text: string): Promise<void> {
  for (const character of text) {
    stdin.send(character)
    await delay(6)
  }
  stdin.send('\r')
}

interface ChildEvidence {
  taskName: string
  teamMode: boolean
  turnStatus: string | null
  processStatus: string | null
  capabilityProfile: string | null
}

function readJson(file: string): any {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'))
  } catch {
    return null
  }
}

function newestTaskBoard(runStartMs: number): { listId: string; tasks: any[] } | null {
  const teamsDir = path.join(REPO, 'temp', 'teams')
  if (!fs.existsSync(teamsDir)) return null
  let best: { listId: string; tasks: any[]; mtime: number } | null = null
  for (const listId of fs.readdirSync(teamsDir)) {
    const file = path.join(teamsDir, listId, 'tasks.json')
    if (!fs.existsSync(file)) continue
    const mtime = fs.statSync(file).mtimeMs
    if (mtime < runStartMs - 5_000) continue
    const data = readJson(file)
    if (!data || !Array.isArray(data.tasks)) continue
    if (!best || mtime > best.mtime) best = { listId, tasks: data.tasks, mtime }
  }
  return best ? { listId: best.listId, tasks: best.tasks } : null
}

function childrenSince(runStartMs: number): ChildEvidence[] {
  const registryFile = path.join(REPO, 'temp', 'subagents', 'registry.json')
  const registry = readJson(registryFile)
  const agents = registry && registry.agents ? registry.agents : {}
  const out: ChildEvidence[] = []
  for (const entry of Object.values<any>(agents)) {
    const createdMs = Date.parse(entry?.created_at || '')
    if (!Number.isFinite(createdMs) || createdMs < runStartMs - 5_000) continue
    const taskDir = entry.task_dir ? String(entry.task_dir) : path.join(REPO, 'temp', String(entry.task_name))
    const state = readJson(path.join(taskDir, 'state.json'))
    const capOptions = state?.capability_options || {}
    out.push({
      taskName: String(entry.task_name),
      teamMode: Boolean(capOptions.team_mode),
      turnStatus: state?.turn_status ?? null,
      processStatus: state?.process_status ?? null,
      capabilityProfile: state?.capability_profile ?? null,
    })
  }
  return out
}

function buildTeamPrompt(): string {
  return [
    '这是一个 agent team 协作 E2E（真实 LLM）。必须用「团队」方式完成，不要自己一个人做完。',
    `工作目录固定为 ${ART_DIR}（只允许在该目录下写文件；仓库源码只读；不要 git commit）。`,
    '步骤：',
    '1) 先用 task_create 建立 3 个可认领任务（subject 分别以 T1/T2/T3 开头）：',
    '   T1 = 统计仓库根目录下 *.py 文件的总数；',
    '   T2 = 统计 tests/ 目录下 test_*.py 文件的数量；',
    '   T3 = 读取 AGENTS.md，统计以 "## " 开头的二级标题行数。',
    `2) 用 spawn_agent 起 3 个 teammate（每个都必须传 team_mode=true），message 里写清对应任务，并要求：从共享任务板认领自己的任务、完成后用 task_update 置 completed、用 send_message 把结果发给 team-lead、并把结果写入 ${ART_DIR}/<你的task_name>.txt。`,
    '3) 用 wait_agent 等待它们完成，再用 read_agent_result 读取它们的权威结果。',
    `4) 你自己把这三个数字汇总写入文件 ${ART_DIR}/report.md，并在最终回答里列出这三个数字。`,
    '这三个数字必须来自 teammate 的真实结果，不要自己另算。',
  ].join('\n')
}

async function main(): Promise<number> {
  delete process.env.GA_INK_MOUSE
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
    realClient = startBridge(
      python,
      bridgeScript,
      event => {
        events.push(event)
        state = applyBridgeEvent(state, event)
        onEvent(event)
      },
      onExit,
    )
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

  const summary: Record<string, unknown> = {
    passed: false,
    runId: RUN_ID,
    artDir: ART_DIR,
    model: null as string | null,
    terminal: { columns: stdout.columns, rows: stdout.rows },
  }

  try {
    await waitFor('bridge ready', () => events.some(event => event.type === 'ready'), 60_000)
    await waitFor('initial composer frame', () => chromeFrames(stdout).length > 0, 10_000)
    validateFrame(chromeFrames(stdout).at(-1) || '', 'initial')

    const modelStatus = events.find((event): event is Extract<BridgeEvent, { type: 'model_status' }> => event.type === 'model_status')
    const current = modelStatus?.models.find(model => model.current)
    summary.model = current?.name ?? null

    await typeUserInput(stdin, '/permissions full')
    await waitFor('full access command', () => commands.some(command => command.type === 'set_permission_mode' && command.mode === 'full_access'), 10_000)
    await waitFor('full access selected', () => events.some(event => event.type === 'permission_switch_result' && event.mode === 'full_access'), 30_000)
    validateFrame(chromeFrames(stdout).at(-1) || '', 'after permission switch')

    const runStartMs = Date.now()
    const prompt = CUSTOM_PROMPT ? CUSTOM_PROMPT.split('{ART_DIR}').join(ART_DIR) : buildTeamPrompt()
    fs.writeFileSync(path.join(ART_DIR, 'prompt.txt'), prompt, { encoding: 'utf8' })
    const beforeDone = events.filter(event => event.type === 'assistant_done').length
    await typeUserInput(stdin, prompt)
    await waitFor('team submit command', () => commands.some(command => command.type === 'submit' && command.text === prompt), 30_000)
    summary.submitSeen = true
    await waitFor('lead run settled', () => (
      events.filter(event => event.type === 'assistant_done').length > beforeDone
      && events.some(event => event.type === 'status' && event.status === 'idle')
    ), TIMEOUT_MS)
    assert.equal(state.error, null, `team run bridge error: ${state.error}`)
    validateFrame(chromeFrames(stdout).at(-1) || '', 'after lead run')
    summary.leadAssistantDone = events.filter(event => event.type === 'assistant_done').length

    // ---- /team panel: real roster through the bridge + rendered frame ----
    const rosterBefore = events.filter(event => event.type === 'team_roster').length
    await typeUserInput(stdin, '/team')
    await waitFor('team roster event', () => events.filter(event => event.type === 'team_roster').length > rosterBefore, 20_000)
    const roster = events.filter((event): event is Extract<BridgeEvent, { type: 'team_roster' }> => event.type === 'team_roster').at(-1)
    await delay(200)
    const teamFrame = chromeFrames(stdout).at(-1) || ''
    validateFrame(teamFrame, 'after /team')
    summary.teamRoster = {
      memberCount: roster?.team?.memberCount ?? null,
      members: roster?.members?.map(member => ({ name: member.name, role: member.role, status: member.status })) ?? [],
      taskCount: roster?.tasks?.length ?? null,
    }
    stdin.send('\u001b')
    await delay(150)

    // ---- disk evidence ----
    const children = childrenSince(runStartMs)
    const teamChildren = children.filter(child => child.teamMode)
    const completedTeammates = teamChildren.filter(child => child.turnStatus === 'completed')
    const board = newestTaskBoard(runStartMs)
    const boardTasks = board?.tasks ?? []
    const completedBoardTasks = boardTasks.filter(task => task.status === 'completed')
    const reportPath = path.join(ART_DIR, 'report.md')
    const reportExists = fs.existsSync(reportPath)
    const reportSize = reportExists ? fs.statSync(reportPath).size : 0
    const artifactFiles = fs.existsSync(ART_DIR) ? fs.readdirSync(ART_DIR) : []

    summary.evidence = {
      childrenSinceRun: children.map(child => child.taskName),
      teamModeChildren: teamChildren.map(child => ({ taskName: child.taskName, turnStatus: child.turnStatus, profile: child.capabilityProfile })),
      taskBoard: board ? { listId: board.listId, count: boardTasks.length, completed: completedBoardTasks.length, subjects: boardTasks.map(task => task.subject) } : null,
      report: { exists: reportExists, size: reportSize },
      artifacts: artifactFiles,
    }

    // NOTE: /team lists *active* members (include_closed=False). The lead closes
    // teammates right after reading their results, so a post-run roster is empty by
    // design; what persists is the shared task board, which /team must still surface.
    summary.teamRosterFrame = teamFrame.slice(0, 600)
    if (SOFT) {
      // 观测模式：只记录证据，不强制团队成立（用于验证自然输入是否自触发 team）
      summary.softMode = true
    } else {
      assert.ok(teamChildren.length >= MIN_MEMBERS, `expected >=${MIN_MEMBERS} team_mode teammates, saw ${teamChildren.length} (of ${children.length} children)`)
      assert.ok(completedTeammates.length >= MIN_MEMBERS, `expected >=${MIN_MEMBERS} teammates with turn_status=completed, saw ${completedTeammates.length}`)
      assert.ok(board && boardTasks.length >= MIN_MEMBERS, `expected a shared task board with >=${MIN_MEMBERS} tasks, saw ${boardTasks.length}`)
      assert.ok(completedBoardTasks.length >= 1, 'expected at least one task marked completed on the shared board')
      assert.ok((roster?.tasks?.length ?? 0) >= MIN_MEMBERS, `/team payload did not surface the shared task board (saw ${roster?.tasks?.length} tasks)`)
      assert.ok(teamFrame.includes('Task board'), `/team panel frame missing "Task board": ${JSON.stringify(teamFrame.slice(0, 200))}`)
      assert.ok(reportExists && reportSize > 0, `expected lead to write report.md in ${ART_DIR}`)
    }

    summary.passed = true
  } catch (error) {
    summary.error = String(error).replaceAll(REPO, '<repo>').slice(0, 1_500)
    summary.lastFrame = (chromeFrames(stdout).at(-1) || '').slice(0, 1_500)
    summary.eventTypes = [...new Set(events.map(event => event.type))]
    summary.state = {
      status: state.status,
      error: state.error,
      agents: state.agents.map(record => ({ kind: record.recordKind, path: record.agentPath, status: record.status })),
    }
  }

  fs.writeFileSync(path.join(ART_DIR, 'summary.json'), JSON.stringify(summary, null, 2), { encoding: 'utf8' })
  instance.unmount()
  cursorPark.dispose()
  console.log(JSON.stringify(summary, null, 2))
  return summary.passed === true ? 0 : 1
}

main().then(code => {
  process.exitCode = code
}).catch(error => {
  console.log(JSON.stringify({ passed: false, error: String(error).slice(0, 1_000) }, null, 2))
  process.exitCode = 1
})
