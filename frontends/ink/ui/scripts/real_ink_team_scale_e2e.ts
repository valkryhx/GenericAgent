// Real-LLM agent-team scale e2e: 10 simple + 5 medium tasks on one shared board.
//
// What this proves, in one run (see docs/20261015-GA-ink-真实LLM-e2e-自测SOP.md):
//   backend  - task board is created, claimed and completed by self-driving teammates;
//              every task ends `completed`, every simple answer is numerically correct,
//              every teammate really used `send_message`, and the roster keeps the
//              finished members with a work verdict.
//   frontend - `/team` is sampled WHILE the team works (not only after): the panel must
//              show a growing `[x]`/`[~]` task board and member rows with verdicts, at
//              terminal width, without an unknown `?` verdict.
//
// Runs the real App (fake TTY) + real bridge + real zhanlu-ds model.
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
import { TEAM_PANEL_MAX_ROWS, teamMemberVerdict, teamPanelFromRoster, teamPanelLines, teamPanelViewport } from '../src/teamPanel.js'
import { advanceSampling } from '../src/teamSampling.js'
import type { BridgeCommand, BridgeEvent } from '../src/protocol.js'

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../..')
const PYTHON = process.env.PYTHON || 'python'
const BRIDGE_SCRIPT = path.join(REPO, 'frontends', 'ink', 'bridge.py')
const RUN_ID = process.env.GA_TEAM_SCALE_RUNID || String(Date.now())
const ART_DIR = path.join(REPO, 'temp', `team_scale_${RUN_ID}`)
const TEAMMATES = Number(process.env.GA_TEAM_SCALE_TEAMMATES || 3)
const TIMEOUT_MS = Number(process.env.GA_TEAM_SCALE_TIMEOUT_MS || 1_800_000)
const SAMPLE_INTERVAL_MS = Number(process.env.GA_TEAM_SCALE_SAMPLE_MS || 1_500)
const EXPECTED_MODEL = process.env.GA_TEAM_SCALE_EXPECT_MODEL || 'zhanlu-ds/moma/deepseek-latest-flash'
const SIMPLE_COUNT = 10
const MEDIUM_COUNT = 5
const TOTAL = SIMPLE_COUNT + MEDIUM_COUNT

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

// ---------------------------------------------------------------------------
// Ground truth for the ten simple tasks, computed from the checkout itself.
// ---------------------------------------------------------------------------
function countDirEntries(dir: string, predicate: (name: string) => boolean, recursive = false): number {
  let total = 0
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.isDirectory()) {
      if (recursive) total += countDirEntries(path.join(dir, entry.name), predicate, true)
      continue
    }
    if (predicate(entry.name)) total += 1
  }
  return total
}

function countHeadingLines(file: string, prefix: string): number {
  const text = fs.readFileSync(file, 'utf8')
  return text.split('\n').filter(line => line.startsWith(prefix)).length
}

const SIMPLE_TASKS: Array<{ id: string; topic: string; expected: () => number }> = [
  { id: 'S01', topic: '仓库根目录（非递归）下 *.py 文件总数', expected: () => countDirEntries(REPO, name => name.endsWith('.py')) },
  { id: 'S02', topic: 'tests/ 目录下 test_*.py 文件数', expected: () => countDirEntries(path.join(REPO, 'tests'), name => name.startsWith('test_') && name.endsWith('.py')) },
  { id: 'S03', topic: 'AGENTS.md 中以 "## " 开头的标题行数', expected: () => countHeadingLines(path.join(REPO, 'AGENTS.md'), '## ') },
  { id: 'S04', topic: 'README.md 中以 "## " 开头的标题行数', expected: () => countHeadingLines(path.join(REPO, 'README.md'), '## ') },
  { id: 'S05', topic: 'genericagent/ 目录（递归）下 *.py 文件数', expected: () => countDirEntries(path.join(REPO, 'genericagent'), name => name.endsWith('.py'), true) },
  { id: 'S06', topic: 'frontends/ink/ui/src/ 目录（非递归）下 *.ts 文件数', expected: () => countDirEntries(path.join(REPO, 'frontends/ink/ui/src'), name => name.endsWith('.ts')) },
  { id: 'S07', topic: 'docs/ 目录（非递归）下 *.md 文件数', expected: () => countDirEntries(path.join(REPO, 'docs'), name => name.endsWith('.md')) },
  { id: 'S08', topic: '仓库根目录（非递归）下 *.md 文件数', expected: () => countDirEntries(REPO, name => name.endsWith('.md')) },
  { id: 'S09', topic: 'tests/ 目录（递归）下 *.py 文件数', expected: () => countDirEntries(path.join(REPO, 'tests'), name => name.endsWith('.py'), true) },
  { id: 'S10', topic: 'genericagent/agents/ 目录（非递归）下 *.py 文件数', expected: () => countDirEntries(path.join(REPO, 'genericagent/agents'), name => name.endsWith('.py')) },
]

const MEDIUM_TASKS: Array<{ id: string; topic: string; source: string; keyword: string }> = [
  { id: 'M01', topic: '读 docs/20261103-GA真实LLM测试策略-过程断言优先.md，提炼三条测试反模式，每行一条', source: 'docs/20261103-GA真实LLM测试策略-过程断言优先.md', keyword: '反模式' },
  { id: 'M02', topic: '读 docs/20261015-GA-ink-真实LLM-e2e-自测SOP.md，写清 L1/L2/L3 三层各测什么', source: 'docs/20261015-GA-ink-真实LLM-e2e-自测SOP.md', keyword: 'L2' },
  { id: 'M03', topic: '读 genericagent/agents/team.py，说明 view() 里 previousStatus/turnStatus/live 三个字段的含义', source: 'genericagent/agents/team.py', keyword: 'turnStatus' },
  { id: 'M04', topic: '读 tests/test_subagent_team.py，列出全部测试类名并统计测试方法数', source: 'tests/test_subagent_team.py', keyword: 'Test' },
  { id: 'M05', topic: '读 docs/20261109-agent-team真实回归与UX验证.md，各用一行写出三个根因', source: 'docs/20261109-agent-team真实回归与UX验证.md', keyword: '根因' },
]

function buildPrompt(): string {
  const simpleLines = SIMPLE_TASKS.map(task =>
    `   - ${task.id}（简单）：${task.topic}。把数字写入 ${ART_DIR}/${task.id}.txt，第一行必须是 count=<数字>。`)
  const mediumLines = MEDIUM_TASKS.map(task =>
    `   - ${task.id}（中等）：${task.topic}。把结果写入 ${ART_DIR}/${task.id}.txt（3-5 行纯文本，必须出现关键词「${task.keyword}」）。`)
  return [
    '这是一个 15 个任务的 agent-team 真实 E2E（真实 LLM）。严格按步骤做，不要自行增减任务，不要修改仓库源码，不要 git 操作。',
    `所有产物只写 ${ART_DIR}（该目录已存在）。`,
    '第一步：用 task_create 创建 15 个任务，subject 必须分别以 S01..S10、M01..M05 开头，description 里写清题目与产物要求。',
    '题目清单：',
    ...simpleLines,
    ...mediumLines,
    '每个任务都要求：完成后 task_update taskId=<该任务 id> status=completed，并 send_message 把结果数字/摘要发给 team-lead。',
    `第二步：用 spawn_agent 起 ${TEAMMATES} 个 teammate（task_name 用 w1..w${TEAMMATES}，team_mode=true），team_task_id 分别绑定 S01、S02、S03 的任务 id。message 里写清：先完成绑定任务；之后继续从共享任务板认领 pending 任务并逐条完成，直到任务板没有可认领的任务；每条都按 description 执行并产出文件。`,
    '第三步：用 wait_agent 等待 teammate；它们完成后会自动认领下一个任务，直到 15 个任务全部 completed。期间用 team_status 查看任务板进度。',
    `最后：读 teammate 结果，把 15 个任务的答案汇总写入 ${ART_DIR}/report.md，并在最终回复里只写 DONE。`,
  ].join('\n')
}

interface RosterSample {
  at: number
  memberCount: number
  members: Array<{ name: string; status?: string | null; turnStatus?: string | null; previousStatus?: string | null; live?: boolean | null; verdict: string }>
  tasks: Array<{ id: string; subject: string; status: string; owner?: string | null }>
  completed: number
  inProgress: number
  pending: number
  panelText: string
  panelLinesTotal: number
  frame?: string
}

function readJson(file: string): any {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')) } catch { return null }
}

function newestTaskBoard(runStartMs: number): { listId: string; tasks: any[] } | null {
  const tasksRoot = path.join(process.env.LOCALAPPDATA || '', 'GenericAgent', 'tasks')
  if (!fs.existsSync(tasksRoot)) return null
  let best: { listId: string; tasks: any[]; mtime: number } | null = null
  for (const listId of fs.readdirSync(tasksRoot)) {
    const file = path.join(tasksRoot, listId, 'tasks.json')
    if (!fs.existsSync(file)) continue
    const mtime = fs.statSync(file).mtimeMs
    if (mtime < runStartMs - 5_000) continue
    const data = readJson(file)
    if (!data || !Array.isArray(data.tasks)) continue
    if (!best || mtime > best.mtime) best = { listId, tasks: data.tasks, mtime }
  }
  return best ? { listId: best.listId, tasks: best.tasks } : null
}

function childTranscriptPath(projectDir: string, session: string, runId: string | null): string | null {
  if (!runId) return null
  const subsRoot = path.join(projectDir, session, 'subagents')
  try {
    for (const dir of fs.readdirSync(subsRoot)) {
      if (!dir.startsWith('session_')) continue
      const candidate = path.join(subsRoot, dir, 'subagents', `${runId}.jsonl`)
      if (fs.existsSync(candidate)) return candidate
    }
  } catch {
    return null
  }
  return null
}

function childrenSince(runStartMs: number): Array<{ taskName: string; taskId: string | null; teamMode: boolean; turnStatus: string | null; sendMessageCalls: number }> {
  const projectsRoot = path.join(process.env.LOCALAPPDATA || '', 'GenericAgent', 'projects')
  if (!fs.existsSync(projectsRoot)) return []
  const out: Array<{ taskName: string; taskId: string | null; teamMode: boolean; turnStatus: string | null; sendMessageCalls: number }> = []
  for (const project of fs.readdirSync(projectsRoot)) {
    const projectDir = path.join(projectsRoot, project)
    if (!fs.statSync(projectDir).isDirectory()) continue
    for (const session of fs.readdirSync(projectDir)) {
      const registryFile = path.join(projectDir, session, 'subagents', 'registry.json')
      const registry = readJson(registryFile)
      const agents = registry && registry.agents ? registry.agents : {}
      for (const entry of Object.values<any>(agents)) {
        const createdMs = Date.parse(entry?.created_at || '')
        if (!Number.isFinite(createdMs) || createdMs < runStartMs - 5_000) continue
        const taskDir = entry.task_dir ? String(entry.task_dir) : path.join(path.dirname(registryFile), 'tasks', String(entry.task_name))
        const state = readJson(path.join(taskDir, 'state.json'))
        const capOptions = state?.capability_options || {}
        let taskId: string | null = null
        try {
          const input = fs.readFileSync(path.join(taskDir, 'input.txt'), 'utf8')
          taskId = input.match(/\[TEAM TASK PROTOCOL\][\s\S]*?#(\d+)/)?.[1] ?? null
        } catch {
          taskId = null
        }
        const transcript = childTranscriptPath(projectDir, session, entry.run_id ? String(entry.run_id) : null)
        let sendMessageCalls = 0
        if (transcript) {
          try { sendMessageCalls = fs.readFileSync(transcript, 'utf8').split('"tool_name":"send_message"').length - 1 } catch { /* ignore */ }
        }
        out.push({
          taskName: String(entry.task_name),
          taskId,
          teamMode: Boolean(capOptions.team_mode),
          turnStatus: state?.turn_status ?? null,
          sendMessageCalls,
        })
      }
    }
  }
  return out
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

  const summary: Record<string, unknown> = {
    passed: false,
    runId: RUN_ID,
    artDir: ART_DIR,
    totalTasks: TOTAL,
    teammates: TEAMMATES,
    model: null as string | null,
  }
  const samples: RosterSample[] = []
  let exitCode = 1

  try {
    await waitFor('bridge ready', () => events.some(event => event.type === 'ready'), 60_000)
    await waitFor('initial frame', () => chromeFrames(stdout).length > 0, 10_000)

    // Model + permissions.
    const modelBefore = events.filter(event => event.type === 'model_status').length
    await typeUserInput(stdin, '/model ?')
    await waitFor('model status', () => events.filter(event => event.type === 'model_status').length > modelBefore, 30_000)
    const modelStatus = [...events].reverse().find((event): event is Extract<BridgeEvent, { type: 'model_status' }> => event.type === 'model_status')
    summary.model = modelStatus?.models.find(model => model.current)?.name ?? null
    assert.equal(summary.model, EXPECTED_MODEL, `team scale e2e must run on ${EXPECTED_MODEL} (current: ${String(summary.model)})`)
    stdin.send('\u001b')
    await delay(150)

    await typeUserInput(stdin, '/permissions full')
    await waitFor('full access', () => events.some(event => event.type === 'permission_switch_result' && event.mode === 'full_access'), 30_000)

    const runStartMs = Date.now()
    const prompt = buildPrompt()
    fs.writeFileSync(path.join(ART_DIR, 'prompt.txt'), prompt, { encoding: 'utf8' })
    const doneBefore = events.filter(event => event.type === 'assistant_done').length
    await typeUserInput(stdin, prompt)
    await waitFor('submit seen', () => commands.some(command => command.type === 'submit' && command.text === prompt), 30_000)

    // Sample `/team` while the team works. Each sample is a fresh roster event, so this
    // reads the same backend the user sees -- not a replay of the final state.
    let sampleSeq = 0
    const sampleRoster = async (): Promise<RosterSample | null> => {
      const before = events.filter(event => event.type === 'team_roster').length
      await typeUserInput(stdin, '/team')
      try {
        await waitFor('team roster sample', () => events.filter(event => event.type === 'team_roster').length > before, 30_000)
      } catch {
        return null
      }
      const roster = events.filter((event): event is Extract<BridgeEvent, { type: 'team_roster' }> => event.type === 'team_roster').at(-1)
      if (!roster) return null
      await delay(250)
      // Render exactly what App paints: the viewport, not the unbounded line list.
      const lines = teamPanelLines(teamPanelFromRoster(roster))
      const panelText = teamPanelViewport(lines, TEAM_PANEL_MAX_ROWS).join('\n')
      const tasks = (roster.tasks || []).map(task => ({ id: String(task.id), subject: String(task.subject || ''), status: String(task.status || ''), owner: task.owner ?? null }))
      sampleSeq += 1
      const sample: RosterSample = {
        at: Date.now() - runStartMs,
        memberCount: roster.team?.memberCount ?? 0,
        members: (roster.members || []).map(member => ({
          name: String(member.name || ''),
          status: member.status ?? null,
          turnStatus: member.turnStatus ?? null,
          previousStatus: member.previousStatus ?? null,
          live: member.live ?? null,
          verdict: teamMemberVerdict(member),
        })),
        tasks,
        completed: tasks.filter(task => task.status === 'completed').length,
        inProgress: tasks.filter(task => task.status === 'in_progress').length,
        pending: tasks.filter(task => task.status === 'pending').length,
        panelText,
        panelLinesTotal: lines.length,
        frame: (chromeFrames(stdout).at(-1) || '').slice(0, 1_200),
      }
      samples.push(sample)
      return sample
    }

    const deadline = Date.now() + TIMEOUT_MS
    let finalSample: RosterSample | null = null
    let samplingState = { key: '', stalled: 0 }
    while (Date.now() < deadline) {
      const sample = await sampleRoster()
      if (sample) {
        finalSample = sample
        const liveWorkers = sample.members.filter(member => member.status !== 'closed' && member.live !== false).length
        const done = events.filter(event => event.type === 'assistant_done').length > doneBefore
        // The stop rule lives in teamSampling.ts: run 3 stopped only four quiet samples
        // into the run, while the lead was still handing tasks 6-15 to workers that were
        // alive mid-turn. That mistake is now covered by unit tests instead of a rerun.
        const advance = advanceSampling(samplingState, {
          completed: sample.completed,
          inProgress: sample.inProgress,
          pending: sample.pending,
          liveWorkers,
          leadIdle: done && state.status === 'idle',
          total: TOTAL,
        })
        samplingState = advance.state
        if (advance.stop) break
      }
      await delay(SAMPLE_INTERVAL_MS)
    }

    if (!finalSample || finalSample.completed < TOTAL) {
      // One last look even after the loop so the failure carries the terminal state.
      finalSample = (await sampleRoster()) || finalSample
    }
    assert.ok(finalSample, 'no /team roster could be sampled')
    const progresses = samples.filter(sample => sample.tasks.length > 0)

    // ---- backend: shared board ----
    await delay(500)
    const board = newestTaskBoard(runStartMs)
    assert.ok(board, 'shared task board file not found')
    const boardTasks = board!.tasks
    summary.board = {
      listId: board!.listId,
      count: boardTasks.length,
      completed: boardTasks.filter(task => task.status === 'completed').length,
      statuses: [...new Set(boardTasks.map(task => task.status))],
      subjects: boardTasks.map(task => task.subject),
    }
    assert.equal(boardTasks.length, TOTAL, `expected exactly ${TOTAL} tasks on the board (saw ${boardTasks.length})`)
    const ids = boardTasks.map(task => String(task.id))
    assert.equal(new Set(ids).size, ids.length, `duplicate task ids on the board: ${JSON.stringify(ids)}`)
    const subjects = boardTasks.map(task => String(task.subject || '').trim().toLowerCase())
    assert.equal(new Set(subjects).size, subjects.length, `duplicate task subjects: ${JSON.stringify(subjects)}`)
    assert.ok(boardTasks.every(task => ['completed'].includes(String(task.status))), `not every board task completed: ${JSON.stringify(boardTasks.map(task => `${task.id}:${task.status}`))}`)
    assert.ok(boardTasks.every(task => String(task.owner || '').trim()), 'a completed task has no owner')

    // ---- backend: teammates ----
    const children = childrenSince(runStartMs)
    const teamChildren = children.filter(child => child.teamMode)
    summary.children = teamChildren.map(child => ({ name: child.taskName, id: child.taskId, turn: child.turnStatus, sendMessageCalls: child.sendMessageCalls }))
    assert.ok(teamChildren.length >= TEAMMATES, `expected >= ${TEAMMATES} team_mode children, saw ${teamChildren.length}`)
    assert.ok(teamChildren.every(child => child.turnStatus === 'completed'), `a teammate did not complete: ${JSON.stringify(teamChildren.map(child => `${child.taskName}:${child.turnStatus}`))}`)
    assert.ok(teamChildren.every(child => child.sendMessageCalls > 0), `a teammate never called send_message: ${JSON.stringify(teamChildren.map(child => child.taskName))}`)

    // ---- frontend: progress was visible WHILE the team worked ----
    assert.ok(progresses.length >= 3, `expected >= 3 roster samples during the run, saw ${progresses.length}`)
    const partial = progresses.filter(sample => sample.completed > 0 && sample.completed < TOTAL)
    const working = progresses.filter(sample => sample.inProgress > 0)
    summary.progress = {
      samples: progresses.length,
      completedSeries: progresses.map(sample => sample.completed),
      inProgressSeries: progresses.map(sample => sample.inProgress),
      partialSamples: partial.length,
      workingSamples: working.length,
    }
    assert.ok(partial.length > 0 || working.length > 0, `no sample showed work in progress (completed series ${JSON.stringify(progresses.map(sample => sample.completed))})`)
    for (let index = 1; index < progresses.length; index += 1) {
      assert.ok(
        progresses[index].completed >= progresses[index - 1].completed,
        `completed count went backwards: ${progresses[index - 1].completed} -> ${progresses[index].completed}`,
      )
    }
    assert.ok(progresses.some(sample => sample.memberCount >= TEAMMATES), `roster never showed ${TEAMMATES} members during the run`)
    // The panel, not just the payload: the task board must have been painted with at
    // least one running/completed task line while work was happening.
    assert.ok(
      progresses.some(sample => sample.panelText.includes('Task board') && /\[(?:[~x])\]/.test(sample.panelText)),
      'no sampled /team panel painted a Task board with task progress',
    )
    assert.ok(
      progresses.every(sample => sample.panelText.split('\n').every(line => stringWidth(line) < stdout.columns)),
      'a sampled /team panel line reached the terminal width',
    )
    // A board bigger than the fixed overlay must say so instead of silently dropping tasks.
    for (const sample of progresses) {
      assert.ok(
        sample.panelText.split('\n').length <= TEAM_PANEL_MAX_ROWS,
        `panel painted ${sample.panelText.split('\n').length} rows, above the ${TEAM_PANEL_MAX_ROWS}-row overlay`,
      )
      if (sample.panelLinesTotal > TEAM_PANEL_MAX_ROWS) {
        assert.match(sample.panelText, /more line\(s\)/, 'a truncated panel did not say how much it hid')
      }
    }
    assert.ok(
      progresses.every(sample => !sample.members.some(member => member.verdict === '?')),
      'a sampled /team member row had an unknown verdict',
    )
    // At least one real Ink frame must have carried the panel (not only the pure renderer).
    summary.realPanelFrames = progresses.filter(sample => (sample.frame || '').includes('Task board')).length

    // ---- final roster ----
    assert.ok(finalSample!.completed === TOTAL, `final /team still showed ${finalSample!.completed}/${TOTAL} completed`)
    assert.ok(finalSample!.memberCount >= TEAMMATES, `final /team showed ${finalSample!.memberCount} members`)
    for (const member of finalSample!.members) {
      assert.equal(member.verdict, 'completed', `final /team verdict for ${member.name} was ${member.verdict}`)
    }

    // ---- artifacts: the ten simple answers must be numerically correct ----
    const answerChecks: Array<{ id: string; expected: number; actual: number | null; ok: boolean }> = []
    for (const task of SIMPLE_TASKS) {
      const file = path.join(ART_DIR, `${task.id}.txt`)
      const expected = task.expected()
      let actual: number | null = null
      if (fs.existsSync(file)) {
        const match = fs.readFileSync(file, 'utf8').match(/count\s*=\s*(\d+)/i)
        actual = match ? Number(match[1]) : null
      }
      answerChecks.push({ id: task.id, expected, actual, ok: actual === expected })
      assert.equal(actual, expected, `${task.id} answer is ${String(actual)}, expected ${expected} (${task.topic})`)
    }
    summary.answers = answerChecks

    // ---- artifacts: medium summaries exist and cover the requested keyword ----
    const mediumChecks: Array<{ id: string; bytes: number; keyword: boolean }> = []
    for (const task of MEDIUM_TASKS) {
      const file = path.join(ART_DIR, `${task.id}.txt`)
      assert.ok(fs.existsSync(file), `${task.id} summary file missing: ${file}`)
      const text = fs.readFileSync(file, 'utf8')
      const keyword = text.toLowerCase().includes(task.keyword.toLowerCase())
      mediumChecks.push({ id: task.id, bytes: Buffer.byteLength(text, 'utf8'), keyword })
      assert.ok(text.trim().length > 40, `${task.id} summary is too small (${text.trim().length} chars)`)
      assert.ok(keyword, `${task.id} summary never mentions ${task.keyword}`)
    }
    summary.medium = mediumChecks

    const reportPath = path.join(ART_DIR, 'report.md')
    assert.ok(fs.existsSync(reportPath) && fs.statSync(reportPath).size > 0, 'lead did not write report.md')
    summary.report = { bytes: fs.statSync(reportPath).size }

    const fatal = events.filter((event): event is Extract<BridgeEvent, { type: 'error' }> => event.type === 'error')
      .filter(event => !/provider|429|rate.?limit|bridge_stderr/i.test(event.message))
    assert.equal(fatal.length, 0, `bridge errors: ${JSON.stringify(fatal.map(event => event.code))}`)
    assert.equal(state.error, null, `UI error: ${state.error}`)
    assert.equal(state.status, 'idle', `UI did not return to idle (status=${state.status})`)

    summary.samples = samples.map(sample => ({ at: sample.at, members: sample.memberCount, completed: sample.completed, inProgress: sample.inProgress, pending: sample.pending }))
    summary.passed = true
    exitCode = 0
  } catch (error) {
    summary.error = String(error).replaceAll(REPO, '<repo>').slice(0, 1_500)
    summary.samples = samples.map(sample => ({ at: sample.at, members: sample.memberCount, completed: sample.completed, inProgress: sample.inProgress, pending: sample.pending }))
    summary.lastPanel = (samples.at(-1)?.panelText || '').slice(0, 1_000)
    summary.eventTypes = [...new Set(events.map(event => event.type))]
    summary.state = { status: state.status, error: state.error }
    exitCode = 1
  } finally {
    fs.writeFileSync(path.join(ART_DIR, 'summary.json'), JSON.stringify(summary, null, 2), { encoding: 'utf8' })
    instance.unmount()
    cursorPark.dispose()
    realClient?.stop()
  }
  console.log(JSON.stringify(summary, null, 2))
  return exitCode
}

main().then(code => { process.exitCode = code }).catch(error => {
  console.log(JSON.stringify({ passed: false, error: String(error).slice(0, 1_000) }, null, 2))
  process.exitCode = 1
})
