/**
 * 精确归因 ±1：逐帧打印 three-layer 高度（viewport / bottomRows / frame）。
 *
 * 已定位：MessageViewport 的 height 在 turn 边界会从 1 变 0（kind 'none'），
 * 这正是 ±1 的来源。本探针把 plan.kind / height / bottomRows 都打出来，
 * 让这条链路可复算，而不是靠推测。
 */
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from '../src/App.js'
import { computeLayoutMetrics } from '../src/layoutMetrics.js'
import { planMessageViewport } from '../src/messageViewportPlan.js'
import type { BridgeClient } from '../src/bridgeClient.js'
import type { BridgeEvent } from '../src/protocol.js'

const NL = String.fromCharCode(10)
const delay = (ms: number) => new Promise(r => setTimeout(r, ms))

class CaptureWriteStream extends EventEmitter {
  columns = 120
  rows = 40
  chunks: string[] = []
  isTTY = true
  write(chunk: unknown): boolean {
    this.chunks.push(String(chunk))
    return true
  }
}

class FakeReadStream extends EventEmitter {
  isTTY = true
  setRawMode() { return this }
  setEncoding() { return this }
  ref() { return this }
  unref() { return this }
  read() { return null }
  resume() { return this }
  pause() { return this }
}

function stripAnsi(s: string): string {
  return s
    .replace(/\u001b\][^\u0007]*(?:\u0007|\u001b\\)/g, '')
    .replace(/\u001b\[[0-?]*[ -/]*[@-~]/g, '')
}

function frameLines(data: string): string[] {
  const lines = stripAnsi(data).split(NL)
  while (lines.length > 0 && lines[lines.length - 1]!.trim() === '') lines.pop()
  return lines
}

type Row = {
  phase: string
  frame: number
  planKind: string
  planHeight: number
  bottomRows: number
  viewportPlusBottom: number
  hasReady: boolean
}

async function main() {
  let emit: ((e: BridgeEvent) => void) | null = null
  const startBridgeClient = (
    _p: string,
    _s: string,
    onEvent: (e: BridgeEvent) => void,
  ): BridgeClient => {
    emit = onEvent
    setTimeout(() => onEvent({ type: 'ready', version: 1 }), 0)
    return { send() {}, stop() {} }
  }

  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
  const stdin = new FakeReadStream()

  const instance = render(React.createElement(App, {
    python: 'python',
    bridgeScript: 'bridge.py',
    startBridgeClient,
  }), {
    stdout: stdout as unknown as NodeJS.WriteStream,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    patchConsole: false,
  })

  const rows: Row[] = []

  function snap(phase: string, liveLineCount: number, hasStatic: boolean, running: boolean) {
    const lines = frameLines(stdout.chunks[stdout.chunks.length - 1] ?? '')
    const plan = planMessageViewport({
      hasStaticMessages: hasStatic,
      liveLineCount,
      messageRows: 34,
      maxLiveRows: 34,
      keepLivePlaceholder: running && liveLineCount === 0,
    })
    const planHeight = plan.kind === 'none' ? 0 : plan.height
    const m = computeLayoutMetrics({
      rows: 40,
      columns: 120,
      hasActivity: true,
      hasError: false,
      hasPanel: false,
      hasSlashSuggestions: false,
      inputRows: 1,
      goalRows: 0,
      headerRows: 0,
    })
    rows.push({
      phase,
      frame: lines.length,
      planKind: plan.kind,
      planHeight,
      bottomRows: m.bottomRows,
      viewportPlusBottom: planHeight + m.bottomRows,
      hasReady: lines.some(l => l.trim() === 'Ready.'),
    })
  }

  try {
    await delay(400)
    snap('A idle-ready', 0, false, false)

    const sink = emit as unknown as (e: BridgeEvent) => void

    sink({ type: 'status', status: 'running', taskId: 1 })
    sink({ type: 'user', taskId: 1, text: 'P' })
    await delay(300)
    snap('B running, no stream yet', 0, false, true)

    for (let i = 0; i < 20; i += 1) {
      sink({ type: 'assistant_delta', taskId: 1, text: `R${i}` + NL })
      await delay(25)
    }
    await delay(300)
    snap('C running, 20 lines streamed', 0, false, true)

    sink({ type: 'assistant_done', taskId: 1, text: '' })
    await delay(150)
    snap('D after done, still running', 0, true, true)

    sink({ type: 'status', status: 'idle', taskId: 1 })
    await delay(400)
    snap('E idle after turn', 0, true, false)
  } finally {
    instance.unmount()
  }

  console.log(
    '阶段'.padEnd(28),
    '帧高',
    'plan'.padEnd(12),
    '视口',
    '底栏',
    '视口+底栏',
    'Ready?',
  )
  console.log('-'.repeat(88))
  for (const r of rows) {
    console.log(
      r.phase.padEnd(28),
      String(r.frame).padStart(4),
      `${r.planKind}/${r.planHeight}`.padEnd(12),
      String(r.planHeight).padStart(4),
      String(r.bottomRows).padStart(4),
      String(r.viewportPlusBottom).padStart(9),
      r.hasReady ? 'YES' : '',
    )
  }

  console.log('')
  console.log('=== 帧高相邻变化 ===')
  for (let i = 1; i < rows.length; i += 1) {
    const d = rows[i]!.frame - rows[i - 1]!.frame
    if (d === 0) continue
    console.log(
      `${rows[i - 1]!.phase} (${rows[i - 1]!.frame}) -> ${rows[i]!.phase} (${rows[i]!.frame})`
      + `  ${d > 0 ? '+' : ''}${d}`
      + `   [视口 ${rows[i - 1]!.planHeight}->${rows[i]!.planHeight},`
      + ` 底栏 ${rows[i - 1]!.bottomRows}->${rows[i]!.bottomRows}]`,
    )
  }

  console.log('')
  console.log('=== turn 边界是否 ±1 ===')
  const d = rows[3]!.planHeight - rows[4]!.planHeight
  console.log(
    `D(视口 ${rows[3]!.planHeight}) -> E(视口 ${rows[4]!.planHeight})  `
    + `视口变化 ${d >= 0 ? '+' : ''}${d}；底栏不变（${rows[4]!.bottomRows}）`,
  )
  if (d !== 0) {
    console.log('=> 确认：turn 结束时 MessageViewport 高度变化，导致整帧高度 ±' + Math.abs(d))
  }
}

main().catch(e => { console.error(e); process.exit(1) })
