/**
 * Real Ink UI e2e: goal + background workflow + agent-team surfaces together.
 *
 * Real bridge + real model (default/deepseek-v4.1-flash) + real Ink render +
 * real stdin. Drives one session so that an active GOAL, a running WORKFLOW and
 * a live TEAM subagent all exist, then captures painted ANSI frames while it
 * switches overlays:
 *
 *   frame A: goal card + workflow status bar coexist (secondary bands)
 *   frame B: /team overlay paints, goal card yields (overlay owns the bottom)
 *   frame C: Esc closes team -> goal card returns
 *   frame D: /workflows overlay paints, goal card yields
 *   frame E: Esc closes workflows -> goal card returns
 *
 * Plus: every captured frame must fit the terminal width, and the workflow/
 * team surfaces must actually paint from REAL bridge events (not synthetic).
 *
 * Env: GA_COMBO_UI_PROFILE (default 'default'), GA_COMBO_UI_TIMEOUT ms.
 */
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { promises as fs } from 'node:fs'
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

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../..')
const PYTHON = process.env.PYTHON || 'python'
const BRIDGE_SCRIPT = path.join(REPO, 'frontends', 'ink', 'bridge.py')
const PROFILE = process.env.GA_COMBO_UI_PROFILE || 'default'
const TURN_TIMEOUT = Number(process.env.GA_COMBO_UI_TIMEOUT || '300000')
const RUN = String(Date.now())
const TARGET = path.join('D:/test20260416', `_goal_combo_ui_${RUN}`)
const DATA = path.join(REPO, 'temp', 'goal_combo_ui', `data_${RUN}`)

class CaptureWriteStream extends EventEmitter {
  columns = 100
  rows = 32
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
function delay(ms: number): Promise<void> { return new Promise(r => setTimeout(r, ms)) }
function allFrames(stdout: CaptureWriteStream): string[] {
  return stdout.chunks.map(stripAnsi).filter(c => (
    c.includes('Enter send') || c.includes('Running ·') || /─{8,}/.test(c) || c.includes('GenericAgent')
  ))
}
async function waitFor(label: string, pred: () => boolean, ms: number): Promise<void> {
  const end = Date.now() + ms
  while (Date.now() < end) { if (pred()) return; await delay(80) }
  throw new Error(`timeout: ${label}`)
}
async function typeInput(stdin: FakeReadStream, text: string): Promise<void> {
  for (const ch of text) { stdin.send(ch); await delay(4) }
  stdin.send('\r')
}

function stripAnsi(text: string): string {
  return text
    .replace(/\u001B\][^\u0007]*(?:\u0007|\u001B\\)/g, '')
    .replace(/\u001B\[[0-?]*[ -/]*[@-~]/g, '')
    .replace(/\u001B[=>]/g, '')
    .replace(/\r/g, '')
}


// Convert a Windows path to forward slashes without any literal backslash
// in the source (avoids heredoc/JS escaping pitfalls).
const toPosix = (p: string): string => p.split(String.fromCharCode(92)).join('/')

async function main(): Promise<number> {
  await fs.mkdir(TARGET, { recursive: true })
  await fs.mkdir(path.join(TARGET, 'nested'), { recursive: true })
  const mwf = path.join(TARGET, 'marker_wf.txt')
  const mteam = path.join(TARGET, 'marker_team.txt')
  const wfJs = path.join(TARGET, 'nested', 'slow_wf.js')
  await fs.writeFile(wfJs,
    "export const meta = { name: 'combo-ui-wf', description: 'slow write marker_wf' }\n" +
    "phase('write')\n" +
    "const a = await agent(`Use code_run to run exactly this ONE command, then stop: " +
    `python -c \\"import time;time.sleep(40);open(r'${toPosix(mwf)}','w').write('WF_OK')\\"` +
    "`, { label: 'slowwf' })\n" +
    "return { summary: a.summary }\n", 'utf-8')

  Object.assign(process.env, {
    GA_PROJECT_ROOT: TARGET, GA_DATA_DIR: DATA,
    GA_WORKFLOW_WORKSPACE_ROOT: TARGET, GA_LAUNCH_CWD: TARGET,
    GA_ORIGINAL_CWD: TARGET, GA_LLM_CONFIG: path.join(REPO, 'llm.yaml'),
  })
  for (const k of ['GA_PROJECT_KEY','GA_PROJECT_DATA_DIR','GA_SESSION_DATA_DIR',
    'GA_PARENT_SESSION_ID','GA_TASK_ROOT','GA_TEAM_KEY']) delete process.env[k]
  delete process.env.GA_INK_MOUSE

  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
  const stdin = new FakeReadStream()
  const cursorPark = createCursorParkStdout(stdout as unknown as NodeJS.WriteStream)
  const commands: BridgeCommand[] = []
  const events: BridgeEvent[] = []
  let state: AppState = initialState
  let realClient: BridgeClient | null = null
  const checks: { name: string; ok: boolean; detail?: string }[] = []
  const rec = (name: string, ok: boolean, detail = '') => {
    checks.push({ name, ok, detail })
    process.stdout.write(`${ok ? 'PASS' : 'FAIL'} ${name}${detail ? ' - ' + detail : ''}\n`)
  }
  const startBridgeClient = (python: string, bridgeScript: string,
    onEvent: (e: BridgeEvent) => void, onExit: (c: number | null) => void): BridgeClient => {
    realClient = startBridge(python, bridgeScript, ev => {
      events.push(ev); state = applyBridgeEvent(state, ev); onEvent(ev)
    }, onExit)
    return { send(c) { commands.push(c); realClient?.send(c) }, stop() { realClient?.stop() } }
  }
  const instance = render(React.createElement(App, {
    python: PYTHON, bridgeScript: BRIDGE_SCRIPT, startBridgeClient, cursorPark,
  }), {
    stdout: cursorPark.stdout, stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream, patchConsole: false, debug: true,
  })
  const waitFrame = async (label: string, pred: (f: string) => boolean, ms: number) => {
    const end = Date.now() + ms
    while (Date.now() < end) {
      const f = allFrames(stdout).at(-1) ?? ''
      if (pred(f)) return f
      await delay(80)
    }
    throw new Error(`timeout frame: ${label}. last=${JSON.stringify((allFrames(stdout).at(-1) ?? '').slice(-240))}`)
  }
  const idleCount = () => events.filter(e => e.type === 'assistant_done').length
  const waitIdle = async (label: string, fromDone: number, ms: number) => {
    await waitFor(label, () => idleCount() > fromDone
      && events.some(e => e.type === 'status' && e.status === 'idle'), ms)
  }
  try {
    await waitFor('ready', () => events.some(e => e.type === 'ready'), 60_000)
    await waitFrame('composer', f => f.includes('Enter send'), 15_000)
    if (PROFILE) {
      await typeInput(stdin, `/model ${PROFILE}`)
      await waitFor('model', () => events.some(e => e.type === 'model_switch_result' && (e as any).ok), 30_000)
    }
    await typeInput(stdin, '/permissions full')
    await waitFor('perm', () => events.some(e => e.type === 'permission_switch_result' && (e as any).mode === 'full_access'), 30_000)

    // 1) goal
    const d0 = idleCount()
    await typeInput(stdin, `/goal 目录 ${TARGET} 下同时存在 marker_wf.txt(WF_OK) 与 marker_team.txt(TEAM_OK)`)
    await waitIdle('set goal', d0, TURN_TIMEOUT)
    await waitFrame('goal card', f => f.includes('Goal active'), 20_000)
    rec('goal card paints (real /goal)', allFrames(stdout).some(f => f.includes('Goal active')))

    // 2) spawn SLOW team subagent via model
    const d1 = idleCount()
    await typeInput(stdin,
      "这是一个 agent team 场景。请：1) task_create 创建任务 subject='write marker_team'；" +
      "2) spawn_agent(task_name='teammate1', team_mode=true, team_task_id=<该id>, background=true)，" +
      `message 要求它用 code_run 执行 python -c "import time;time.sleep(120);open(r'${toPosix(mteam)}','w').write('TEAM_OK')" 后结束；` +
      "3) 完成后只回报 task id 并结束本轮，不要等待、不要自己写文件。")
    await waitIdle('spawn team', d1, TURN_TIMEOUT)

    // 3) launch SLOW workflow via model (scriptPath)
    const d2 = idleCount()
    await typeInput(stdin,
      `请使用 workflow 工具在后台启动 scriptPath="${toPosix(wfJs)}" 的脚本，启动后只回报 run id 并结束本轮，不要等待。`)
    await waitIdle('launch wf', d2, TURN_TIMEOUT)
    await waitFrame('workflow status bar', f => f.includes('agents done') || f.includes('combo-ui-wf'), 25_000)
    const sawWfBar = allFrames(stdout).some(f => f.includes('agents done') || f.includes('combo-ui-wf'))
    rec('workflow status bar paints (real workflow)', sawWfBar)

    // frame A: goal card + workflow status bar together
    const frameA = allFrames(stdout).filter(f => f.includes('Goal active') && (f.includes('agents done') || f.includes('combo-ui-wf'))).at(-1) || ''
    rec('goal card + workflow status bar coexist in one frame', Boolean(frameA))

    // 4) /team overlay (goal card must yield)
    await typeInput(stdin, '/team')
    await waitFor('team_roster after /team', () => events.filter(e => e.type === 'team_roster').length >= 1, 30_000)
    await delay(700)
    const frameB = allFrames(stdout).at(-1) || ''
    const teamPainted = /Team .*lead/.test(frameB) || frameB.includes('Task board')
    rec('/team overlay paints (Team/Task board)', teamPainted, frameB.slice(0, 80))
    rec('team overlay hides the goal card (overlay owns bottom)', !frameB.includes('Goal active'))

    // Esc closes team -> goal card returns
    stdin.send('\u001b'); await delay(500)
    const frameC = await waitFrame('goal back after team', f => f.includes('Goal active'), 10_000)
    rec('goal card returns after closing /team', frameC.includes('Goal active'))

    // 5) /workflows overlay (goal card must yield)
    await typeInput(stdin, '/workflows')
    await waitFor('workflow list event', () => events.some(e => e.type === 'workflow_runs' || e.type === 'workflow_run'), 30_000)
    await delay(700)
    const frameD = allFrames(stdout).at(-1) || ''
    const wfListPainted = frameD.includes('Dynamic workflows')
    rec('/workflows overlay paints', wfListPainted, frameD.slice(0, 80))
    rec('workflows overlay hides the goal card', !frameD.includes('Goal active'))
    stdin.send('\u001b'); await delay(500)
    const frameE = await waitFrame('goal back after workflows', f => f.includes('Goal active'), 10_000)
    rec('goal card returns after closing /workflows', frameE.includes('Goal active'))

    // width sanity over all captured frames
    const over = allFrames(stdout).find(f => f.split('\n').some(l => stringWidth(l) >= stdout.columns))
    rec('no captured frame overflows terminal width', !over, over ? JSON.stringify(over.split('\n').find(l => stringWidth(l) >= stdout.columns)) : '')

    // Persist evidence: captured chrome frames + a check summary.
    try {
      await fs.mkdir(DATA, { recursive: true })
      const frames = allFrames(stdout)
      const SEP = String.fromCharCode(10) + '=====FRAME=====' + String.fromCharCode(10)
      await fs.writeFile(path.join(DATA, 'frames.txt'), frames.join(SEP), 'utf-8')
      await fs.writeFile(path.join(DATA, 'summary.json'), JSON.stringify({ target: TARGET, checks }, null, 2), 'utf-8')
    } catch {}

    const passed = checks.every(c => c.ok)
    process.stdout.write(`RESULT ${checks.filter(c => c.ok).length}/${checks.length} checks passed\n`)
    return passed ? 0 : 1
  } finally {
    instance.unmount(); realClient?.stop()
  }
}
main().then(c => process.exit(c)).catch(err => {
  process.stderr.write(`E2E ERROR: ${err?.stack || err}\n`); process.exit(1)
})
