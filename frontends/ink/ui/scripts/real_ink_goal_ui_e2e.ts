/**
 * Real Ink goal-UI E2E: real bridge + real model + real Ink render + real input.
 *
 * Proves the goal card / terminal badge actually paint from real bridge events
 * driven by real LLM turns (not just synthetic events). The goal is set and the
 * model is asked to satisfy it, then evaluated to a met verdict so the badge
 * appears. Sized as a "medium" task (create + write a file).
 *
 * Env: GA_GOAL_UI_PROFILE (default 'default'), GA_GOAL_UI_TIMEOUT ms (default 300000).
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
const PROFILE = process.env.GA_GOAL_UI_PROFILE || 'default'
const TURN_TIMEOUT = Number(process.env.GA_GOAL_UI_TIMEOUT || '300000')
const TARGET = path.join('D:/test20260416', `_goal_ui_${Date.now()}`)
const DATA = path.join(REPO, 'temp', 'goal_ui_e2e', `data_${Date.now()}`)

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
function stripAnsi(text: string): string {
  return text
    .replace(/\u001B\][^\u0007]*(?:\u0007|\u001B\\)/g, '')
    .replace(/\u001B\[[0-?]*[ -/]*[@-~]/g, '')
    .replace(/\u001B[=>]/g, '')
    .replace(/\r/g, '')
}
function delay(ms: number): Promise<void> { return new Promise(r => setTimeout(r, ms)) }
function allFrames(stdout: CaptureWriteStream): string[] {
  return stdout.chunks.map(stripAnsi).filter(c => (
    c.includes('Enter send') || c.includes('Running ·') || /─{8,}/.test(c)
  ))
}
async function waitFor(label: string, pred: () => boolean, timeoutMs: number): Promise<void> {
  const end = Date.now() + timeoutMs
  while (Date.now() < end) {
    if (pred()) return
    await delay(100)
  }
  throw new Error(`timeout: ${label}`)
}
async function typeInput(stdin: FakeReadStream, text: string): Promise<void> {
  for (const ch of text) { stdin.send(ch); await delay(6) }
  stdin.send('\r')
}

async function main(): Promise<number> {
  await fs.mkdir(TARGET, { recursive: true })
  Object.assign(process.env, {
    GA_PROJECT_ROOT: TARGET,
    GA_DATA_DIR: DATA,
    GA_WORKFLOW_WORKSPACE_ROOT: TARGET,
    GA_LAUNCH_CWD: TARGET,
    GA_ORIGINAL_CWD: TARGET,
    GA_LLM_CONFIG: path.join(REPO, 'llm.yaml'),
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
  let lastFrame = ''
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
    python: PYTHON, bridgeScript: BRIDGE_SCRIPT, startBridgeClient, cursorPark,
  }), {
    stdout: cursorPark.stdout,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    patchConsole: false,
    debug: true,
  })

  const framesSince = (n: number) => allFrames(stdout).slice(n)
  const waitFrame = async (label: string, pred: (f: string) => boolean, ms: number) => {
    const end = Date.now() + ms
    while (Date.now() < end) {
      const frames = allFrames(stdout)
      lastFrame = frames.at(-1) ?? ''
      if (frames.some(pred)) { lastFrame = frames.filter(pred).at(-1) ?? lastFrame; return }
      await delay(100)
    }
    throw new Error(`timeout waiting frame: ${label}. last=${JSON.stringify(lastFrame.slice(-300))}`)
  }
  const waitIdle = async (label: string, fromDoneCount: number, ms: number) => {
    await waitFor(label, () => (
      events.filter(e => e.type === 'assistant_done').length > fromDoneCount
      && events.some(e => e.type === 'status' && e.status === 'idle')
    ), ms)
  }

  try {
    await waitFor('bridge ready', () => events.some(e => e.type === 'ready'), 60_000)
    await waitFrame('initial composer', f => f.includes('Enter send'), 15_000)

    if (PROFILE) {
      await typeInput(stdin, `/model ${PROFILE}`)
      await waitFor('model switch ok', () => events.some(e => e.type === 'model_switch_result' && (e as any).ok), 30_000)
      await waitFrame('after model switch', f => f.includes('Enter send'), 15_000)
    }
    await typeInput(stdin, '/permissions full')
    await waitFor('permissions full', () => events.some(e => e.type === 'permission_switch_result' && (e as any).mode === 'full_access'), 30_000)

    const marker = 'GOAL_UI_OK_20261030'
    const goalText = `目录 ${TARGET} 下必须存在文件 goal_ui_done.txt 且其内容包含 ${marker}`

    // 1) set goal via the real /goal command -> real goal_metadata -> card paints
    const done0 = events.filter(e => e.type === 'assistant_done').length
    await typeInput(stdin, `/goal ${goalText}`)
    await waitIdle('set goal turn', done0, TURN_TIMEOUT)
    await waitFrame('goal card painted', f => f.includes('Goal active') && f.includes('Goal:'), 20_000)
    assert.ok(allFrames(stdout).some(f => f.includes('Goal active')), 'goal card never painted')
    rec('goal card renders after real /goal', allFrames(stdout).some(f => f.includes('Goal active')))
    rec('goal metadata carries the condition',
      events.some(e => e.type === 'goal_metadata' && (e as any).goal?.condition?.includes('goal_ui_done.txt')))

    // 2) medium real turn: model creates + writes the file
    const done1 = events.filter(e => e.type === 'assistant_done').length
    await typeInput(stdin, `请在 ${TARGET} 下创建文件 goal_ui_done.txt，内容写 ${marker}，然后结束本轮。`)
    await waitIdle('work turn', done1, TURN_TIMEOUT)
    const filePath = path.join(TARGET, 'goal_ui_done.txt')
    let exists = false
    try { exists = (await fs.readFile(filePath, 'utf-8')).includes(marker) } catch { exists = false }
    rec('model created file with marker', exists, filePath)

    // 3) evaluate -> met -> terminal badge
    const done2 = events.filter(e => e.type === 'assistant_done').length
    await typeInput(stdin, '目标是否已达成？如已达成请结束目标。')
    await waitIdle('evaluate turn', done2, TURN_TIMEOUT)
    await waitFrame('terminal badge painted', f => f.includes('Goal met'), 25_000)
    const badgeSeen = allFrames(stdout).some(f => f.includes('Goal met'))
    rec('terminal met badge renders after goal met', badgeSeen)
    rec('goal_metadata terminal.met true observed',
      events.some(e => e.type === 'goal_metadata' && (e as any).terminal?.met === true))

    // layout sanity across the frames we captured
    const frames = allFrames(stdout)
    const overflow = frames.find(f => f.split('\n').some(line => stringWidth(line) >= stdout.columns))
    rec('no frame overflows terminal width', !overflow, overflow ? overflow.slice(0, 120) : '')

    const passed = checks.every(c => c.ok)
    process.stdout.write(`RESULT ${checks.filter(c => c.ok).length}/${checks.length} checks passed\n`)
    return passed ? 0 : 1
  } finally {
    instance.unmount()
    realClient?.stop()
  }
}

main().then(code => process.exit(code)).catch(err => {
  process.stderr.write(`E2E ERROR: ${err?.stack || err}\n`)
  process.exit(1)
})
