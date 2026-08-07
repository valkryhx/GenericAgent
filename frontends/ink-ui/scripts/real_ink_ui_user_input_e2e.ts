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
import type { BridgeCommand, BridgeEvent } from '../src/protocol.js'

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..')
const PYTHON = process.env.PYTHON || 'python'
const BRIDGE_SCRIPT = path.join(REPO, 'frontends', 'ink_bridge.py')
const EXPECTED_MODEL = 'luna/gpt-5.6-luna'
const EXPECTED_PROVIDER = 'gpt-super-responses'
const SUBAGENT_MARKER = 'GA_UI_SUBAGENT_CHILD_OK_20260807'
const WORKFLOW_MARKER = 'GA_UI_WORKFLOW_CHILD_OK_20260807'
const SUBAGENT_NAME = `ga_ui_subagent_${Date.now()}`

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

  read(): string | null {
    return this.queue.shift() ?? null
  }

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

function inputBorderRows(frame: string): number[] {
  return frame.split('\n')
    .map((line, index) => ({ index, line: line.trim() }))
    .filter(item => /^─+$/.test(item.line))
    .map(item => item.index)
}

function validateFrame(frame: string, label: string): { borders: number[]; caretRow: number } {
  assert.ok(frame, `${label}: no live Ink frame captured`)
  const lines = frame.split('\n')
  assert.equal(
    lines.every(line => stringWidth(line) < 80),
    true,
    `${label}: a rendered line reaches/exceeds terminal width`,
  )
  const borders = inputBorderRows(frame)
  assert.ok(borders.length >= 2, `${label}: input composer borders missing: ${JSON.stringify(borders)}`)
  const caretRow = lines.findIndex((line, index) => index > borders[0]! && index < borders[1]! && line.includes('>'))
  assert.ok(caretRow > borders[0]! && caretRow < borders[1]!, `${label}: caret is outside composer: ${JSON.stringify({ borders, caretRow })}`)
  return { borders, caretRow }
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
    await delay(8)
  }
  stdin.send('\r')
}

function lastEventOfType<T extends BridgeEvent['type']>(
  events: BridgeEvent[],
  type: T,
): Extract<BridgeEvent, { type: T }> | undefined {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    if (events[index]?.type === type) return events[index] as Extract<BridgeEvent, { type: T }>
  }
  return undefined
}

function assertCursorProtocol(stdout: CaptureWriteStream): void {
  const raw = stdout.chunks.join('')
  assert.doesNotMatch(raw, /\u001B\[\d+;\d+H/, 'legacy absolute CUP cursor placement was emitted')
  assert.doesNotMatch(raw, /\u001B\[[su]/, 'legacy SCO save/restore cursor placement was emitted')

  const tokens = raw.match(/\u001B\[\?25[hl]/g) || []
  assert.ok(tokens.length > 0, 'no native cursor visibility transition was emitted')
  let visible = false
  let sawShow = false
  for (const token of tokens) {
    if (token.endsWith('h')) {
      assert.equal(visible, false, 'cursor was shown twice without a preceding hide')
      visible = true
      sawShow = true
    } else {
      // Ink may hide the terminal cursor before the first cursor-park SHOW;
      // after parking starts, every HIDE must pair with the preceding SHOW.
      if (sawShow) assert.equal(visible, true, 'cursor was hidden while already hidden')
      visible = false
    }
  }
}

async function main(): Promise<number> {
  // Force the planner itself onto the requested real profile; child binding still
  // follows the live /model selection through GenericAgentBridge.
  process.env.GA_WORKFLOW_PLANNER_MODE = 'real'

  delete process.env.GA_INK_MOUSE
  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
  const stdin = new FakeReadStream()
  const cursorPark = createCursorParkStdout(stdout as unknown as NodeJS.WriteStream)
  const commands: BridgeCommand[] = []
  const events: BridgeEvent[] = []
  let state: AppState = initialState
  let selectedModelIndex: number | null = null
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
        if (event.type === 'model_status') {
          const selected = event.models.find(model => model.current && model.name === EXPECTED_MODEL)
          if (selected) selectedModelIndex = selected.index
        }
        onEvent(event)
      },
      onExit,
    )
    return {
      send(command) {
        commands.push(command)
        realClient?.send(command)
      },
      stop() {
        realClient?.stop()
      },
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
    profile: 'luna',
    model: EXPECTED_MODEL,
    provider: EXPECTED_PROVIDER,
    terminal: { columns: stdout.columns, rows: stdout.rows },
  }

  try {
    await waitFor('bridge ready', () => events.some(event => event.type === 'ready'), 60_000)
    await waitFor('initial composer frame', () => chromeFrames(stdout).length > 0, 10_000)
    validateFrame(chromeFrames(stdout).at(-1) || '', 'initial')

    await typeUserInput(stdin, '/model luna')
    await waitFor('model switch command', () => commands.some(command => command.type === 'model_switch' && command.selector === 'luna'), 10_000)
    await waitFor('luna model selected', () => (
      events.some(event => event.type === 'model_switch_result' && event.ok)
      && events.some(event => event.type === 'model_status' && event.models.some(model => model.current && model.name === EXPECTED_MODEL))
    ), 60_000)
    assert.notEqual(selectedModelIndex, null, 'luna model index was not returned by model_status')
    validateFrame(chromeFrames(stdout).at(-1) || '', 'after model switch')

    await typeUserInput(stdin, '/permissions full')
    await waitFor('full access command', () => commands.some(command => command.type === 'set_permission_mode' && command.mode === 'full_access'), 10_000)
    await waitFor('full access selected', () => events.some(event => event.type === 'permission_switch_result' && event.mode === 'full_access'), 30_000)
    validateFrame(chromeFrames(stdout).at(-1) || '', 'after permission switch')

    if (process.env.GA_UI_SKIP_SUBAGENT !== '1') {
      const spawnPrompt = [
        '这是一次真实 GA UI subagent E2E。用户明确要求你使用子 agent，必须实际调用工具，不要直接伪造最终答案。',
        `调用 spawn_agent，task_name 必须是 ${SUBAGENT_NAME}，fork_turns 使用 "none"，llm_no 必须使用当前 model_status 返回的 luna index ${selectedModelIndex}，不要使用 0 或其他模型 index。`,
        'agent_type 字段必须省略，不要传 default 或任何不存在的角色；不要调用 code_run 或其他探测工具。',
        `message 必须要求子 agent 不调用工具、不输出 Markdown，只输出这一行精确文本：${SUBAGENT_MARKER}。`,
        'spawn_agent 成功后本轮立即返回一行简短 ACK，不要在本轮调用 wait_agent、read_agent_result 或 close_agent。',
      ].join('\n')
      const firstAssistantDoneCount = events.filter(event => event.type === 'assistant_done').length
      await typeUserInput(stdin, spawnPrompt)
      await waitFor('subagent spawn submit command', () => commands.some(command => command.type === 'submit' && command.text === spawnPrompt), 20_000)
      await waitFor('subagent spawn turn returned', () => (
        events.filter(event => event.type === 'assistant_done').length > firstAssistantDoneCount
        && events.some(event => event.type === 'status' && event.status === 'idle')
      ), 180_000)
      assert.equal(state.error, null, `subagent spawn bridge error: ${state.error}`)
      validateFrame(chromeFrames(stdout).at(-1) || '', 'after subagent spawn')

      // The bridge exposes agent snapshots on demand. Refresh that read model
      // until the child is terminal; one early /workflows response may still
      // legitimately observe the child while it is running.
      const snapshotDeadline = Date.now() + 180_000
      let snapshotRefreshes = 0
      while (Date.now() < snapshotDeadline) {
        const workflowListCommandCount = commands.filter(command => command.type === 'workflow_list').length
        const workflowListEventCount = events.filter(event => event.type === 'workflow_runs').length
        await typeUserInput(stdin, '/workflows')
        const remainingMs = Math.max(1_000, snapshotDeadline - Date.now())
        await waitFor('workflow list command after subagent', () => commands.filter(command => command.type === 'workflow_list').length > workflowListCommandCount, Math.min(10_000, remainingMs))
        await waitFor('workflow list panel event after subagent', () => events.filter(event => event.type === 'workflow_runs').length > workflowListEventCount, Math.min(60_000, remainingMs))
        snapshotRefreshes += 1
        validateFrame(chromeFrames(stdout).at(-1) || '', `workflow list after subagent refresh ${snapshotRefreshes}`)
        if (state.agents.some(record => (
          record.recordKind === 'process_agent'
          && record.agentPath?.endsWith(`/${SUBAGENT_NAME}`)
          && record.status === 'succeeded'
        ))) break
        stdin.send('\u001b')
        await delay(150)
        validateFrame(chromeFrames(stdout).at(-1) || '', `after closing workflow list refresh ${snapshotRefreshes}`)
        await delay(250)
      }
      assert.ok(state.agents.some(record => (
        record.recordKind === 'process_agent'
        && record.agentPath?.endsWith(`/${SUBAGENT_NAME}`)
        && record.status === 'succeeded'
      )), `subagent child did not reach succeeded in ${snapshotRefreshes} read-model refreshes`)
      assert.ok(state.agentEvents.some(event => (
        event.type === 'turn_completed'
        && event.executionId.includes(SUBAGENT_NAME)
      )), 'subagent turn_completed event was not projected into UI state')
      validateFrame(chromeFrames(stdout).at(-1) || '', 'workflow list after subagent')
      stdin.send('\u001b')
      await delay(120)
      validateFrame(chromeFrames(stdout).at(-1) || '', 'after closing workflow list')

      const readPrompt = [
        `现在调用 wait_agent 等待 ${SUBAGENT_NAME} 的状态（timeout_seconds 使用 5），然后调用 read_agent_result 读取它的权威最终结果。`,
        `最终只输出一行，必须包含精确文本 ${SUBAGENT_MARKER}；不要伪造，不要再次 spawn。`,
      ].join('\n')
      const secondAssistantDoneCount = events.filter(event => event.type === 'assistant_done').length
      await typeUserInput(stdin, readPrompt)
      await waitFor('subagent read submit command', () => commands.some(command => command.type === 'submit' && command.text === readPrompt), 20_000)
      await waitFor('subagent read turn returned', () => (
        events.filter(event => event.type === 'assistant_done').length > secondAssistantDoneCount
        && events.filter(event => event.type === 'status' && event.status === 'idle').length >= 2
      ), 180_000)
      const assistantDoneText = events
        .filter((event): event is Extract<BridgeEvent, { type: 'assistant_done' }> => event.type === 'assistant_done')
        .slice(-1)
        .map(event => event.text)
        .join('\n')
      assert.match(assistantDoneText, new RegExp(SUBAGENT_MARKER), 'subagent marker was not in assistant final output')
      assert.match(assistantDoneText, /read_agent_result|subagent/i, 'assistant final output did not report subagent result collection')
      validateFrame(chromeFrames(stdout).at(-1) || '', 'after subagent read')
    }

    let workflowSummary: Record<string, unknown> = { skipped: true }
    if (process.env.GA_UI_SKIP_WORKFLOW !== '1') {
      const workflowTask = [
        '执行一个最小 workflow UI E2E，只安排一个 child agent，不要拆分成多个 agent。',
        `child agent 不要调用工具、不修改文件，只输出这一行精确文本：${WORKFLOW_MARKER}。`,
        `必须把这条精确输出要求写入 child prompt，并让 workflow 最终结果保留该文本：${WORKFLOW_MARKER}。`,
        '这是规划/验证任务，不要执行代码，不要读写敏感文件，不要 git commit。',
      ].join(' ')
      await typeUserInput(stdin, `/workflow ${workflowTask}`)
      await waitFor('workflow plan command', () => commands.some(command => command.type === 'workflow_plan' && command.taskText === workflowTask), 20_000)
      await waitFor('workflow final event', () => events.some(event => event.type === 'workflow_final'), 420_000)

      const finalEvent = lastEventOfType(events, 'workflow_final')
      assert.ok(finalEvent && finalEvent.type === 'workflow_final')
      const finalRunId = finalEvent.runId
      const run = state.workflows.find(item => item.runId === finalRunId)
      const finalResultText = JSON.stringify(finalEvent.result)
      const workflowDetail = state.workflowDetails[finalRunId]
      const progressText = JSON.stringify(workflowDetail?.progress || {})
      const workflowEventsText = JSON.stringify(state.workflowEvents.filter(event => event.runId === finalRunId))
      const workflowBridgeArtifacts = events
        .filter(event => (
          event.type === 'workflow_final'
          || event.type === 'workflow_progress'
          || event.type === 'workflow_event'
          || event.type === 'workflow_detail'
        ))
        .map(event => JSON.stringify(event))
        .join('\n')
      const allWorkflowText = `${finalResultText}\n${JSON.stringify(state.workflowResults[finalRunId])}\n${progressText}\n${workflowEventsText}\n${workflowBridgeArtifacts}`
      assert.match(allWorkflowText, new RegExp(WORKFLOW_MARKER), 'workflow child marker was not returned through UI bridge artifacts')
      assert.equal(run?.status, 'succeeded', `workflow status was ${run?.status}`)
      assert.equal(state.error, null, `workflow bridge error: ${state.error}`)
      assert.ok(state.agents.some(record => record.recordKind === 'workflow_child'), 'workflow child record was not projected into UI state')
      assert.ok(state.agents.some(record => record.recordKind === 'workflow_run'), 'workflow run record was not projected into UI state')
      assert.ok(state.agentEvents.length > 0, 'workflow/subagent common agent events were not projected into UI state')
      validateFrame(chromeFrames(stdout).at(-1) || '', 'after workflow')
      workflowSummary = {
        runId: finalRunId,
        status: run?.status,
        marker: WORKFLOW_MARKER,
        childRecord: state.agents.some(record => record.recordKind === 'workflow_child'),
        commonEventCount: state.agentEvents.length,
      }
    }

    assertCursorProtocol(stdout)
    summary.passed = true
    summary.commands = commands.map(command => command.type)
    summary.eventCount = events.length
    summary.eventTypes = [...new Set(events.map(event => event.type))]
    summary.subagent = process.env.GA_UI_SKIP_SUBAGENT === '1'
      ? { skipped: true }
      : {
        marker: SUBAGENT_MARKER,
        processRecord: state.agents.some(record => (
          record.recordKind === 'process_agent'
          && record.agentPath?.endsWith(`/${SUBAGENT_NAME}`)
          && record.status === 'succeeded'
        )),
      }
    summary.workflow = workflowSummary
    summary.ui = {
      chromeFrameCount: chromeFrames(stdout).length,
      everyRenderedLineBelowTerminalWidth: true,
      composerCaretBetweenBorders: true,
      cursorProtocol: 'relative park/unpark with SHOW after park and HIDE before redraw',
    }
  } catch (error) {
    summary.error = String(error).replaceAll(REPO, '<repo>').slice(0, 1_000)
    summary.eventTypes = [...new Set(events.map(event => event.type))]
    summary.commands = commands.map(command => command.type)
    summary.lastFrame = (chromeFrames(stdout).at(-1) || '').slice(0, 1_000)
    summary.state = {
      status: state.status,
      error: state.error,
      workflowStatuses: state.workflows.map(run => ({ runId: run.runId, status: run.status })),
      agentKinds: state.agents.map(record => record.recordKind),
    }
    return printSummary(summary, instance, cursorPark)
  }

  return printSummary(summary, instance, cursorPark)
}

function printSummary(summary: Record<string, unknown>, instance: { unmount: () => void }, cursorPark: { dispose: () => void }): number {
  instance.unmount()
  cursorPark.dispose()
  console.log(JSON.stringify(summary, null, 2))
  return summary.passed === true ? 0 : 1
}

main().then(code => {
  process.exitCode = code
}).catch(error => {
  console.log(JSON.stringify({
    passed: false,
    profile: 'luna',
    model: EXPECTED_MODEL,
    provider: EXPECTED_PROVIDER,
    error: String(error).replaceAll(REPO, '<repo>').slice(0, 1_000),
  }, null, 2))
  process.exitCode = 1
})
