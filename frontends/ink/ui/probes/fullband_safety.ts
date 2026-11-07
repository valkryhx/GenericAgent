/**
 * 验证「把 band 钉满高」会不会触发 ink 的 clearTerminal 分支。
 *
 * ink.js:117-121
 *   if (outputHeight >= stdout.rows) { write(clearTerminal + fullStaticOutput + output); return }
 *
 * computeLayoutMetrics: rows = terminalRows - 1，messageRows = rows - headerRows - bottomRows
 * 所以 band=messageRows 时 outputHeight = messageRows + bottomRows = terminalRows - 1 < terminalRows。
 * 本探针把这个推算变成实测：band 满高时，写入里是否出现 clearTerminal。
 */
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from '../src/App.js'
import { computeLayoutMetrics } from '../src/layoutMetrics.js'
import type { BridgeClient } from '../src/bridgeClient.js'
import type { BridgeEvent } from '../src/protocol.js'

const NL = String.fromCharCode(10)
const ESC = String.fromCharCode(27)
const delay = (ms: number) => new Promise(r => setTimeout(r, ms))

class CaptureStream extends EventEmitter {
  columns: number
  rows: number
  chunks: string[] = []
  isTTY = true
  constructor(columns: number, rows: number) {
    super()
    this.columns = columns
    this.rows = rows
  }
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

function frameRows(data: string): number {
  const lines = stripAnsi(data).split(NL)
  while (lines.length > 0 && lines[lines.length - 1]!.trim() === '') lines.pop()
  return lines.length
}

async function main() {
  const TERM_ROWS = 40
  const TERM_COLS = 120

  const m = computeLayoutMetrics({
    rows: TERM_ROWS,
    columns: TERM_COLS,
    hasActivity: true,
    hasError: false,
    hasPanel: false,
    hasSlashSuggestions: false,
    inputRows: 1,
    goalRows: 0,
    headerRows: 0,
  })
  console.log('=== 布局预算 ===')
  console.log(`  终端 ${TERM_ROWS} 行`)
  console.log(`  metrics.rows       = ${m.rows}   (= 终端 - 1)`)
  console.log(`  metrics.messageRows= ${m.messageRows}`)
  console.log(`  metrics.bottomRows = ${m.bottomRows}`)
  console.log(`  band 满高时 outputHeight = ${m.messageRows + m.bottomRows} < ${TERM_ROWS} ? ${m.messageRows + m.bottomRows < TERM_ROWS}`)
  console.log('')

  let emit: ((e: BridgeEvent) => void) | null = null
  const startBridgeClient = (
    _p: string, _s: string, onEvent: (e: BridgeEvent) => void,
  ): BridgeClient => {
    emit = onEvent
    setTimeout(() => onEvent({ type: 'ready', version: 1 }), 0)
    return { send() {}, stop() {} }
  }

  const stdout = new CaptureStream(TERM_COLS, TERM_ROWS)
  const stderr = new CaptureStream(TERM_COLS, TERM_ROWS)
  const stdin = new FakeReadStream()

  const instance = render(React.createElement(App, {
    python: 'python', bridgeScript: 'bridge.py', startBridgeClient,
  }), {
    stdout: stdout as unknown as NodeJS.WriteStream,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    patchConsole: false,
  })

  const CLEAR = ESC + '[2J' + ESC + '[3J' + ESC + '[H'

  try {
    await delay(400)
    const startupRows = frameRows(stdout.chunks[stdout.chunks.length - 1] ?? '')
    console.log('=== 启动时 ===')
    console.log(`  首帧高度 ${startupRows} 行`)

    const sink = emit as unknown as (e: BridgeEvent) => void
    sink({ type: 'status', status: 'running', taskId: 1 })
    sink({ type: 'user', taskId: 1, text: 'Q' })
    for (let i = 0; i < 10; i += 1) {
      sink({ type: 'assistant_delta', taskId: 1, text: `L${i}` + NL })
      await delay(30)
    }
    sink({ type: 'assistant_done', taskId: 1, text: '' })
    sink({ type: 'status', status: 'idle', taskId: 1 })
    await delay(400)

    const all = stdout.chunks.join('')
    const clears = (all.match(new RegExp(CLEAR.replace(/[[]/g, '\\[').replace(/[\] ]/g, m2 => '\\' + m2), 'g')) ?? []).length
    console.log('')
    console.log('=== 全流程 ===')
    console.log(`  写入块 ${stdout.chunks.length} 个`)
    console.log(`  clearTerminal 出现: ${all.includes(CLEAR)}`)
    console.log(`  含 ESC[3J（清 scrollback）: ${all.includes(ESC + '[3J')}`)
    console.log(`  最终帧高 ${frameRows(stdout.chunks[stdout.chunks.length - 1] ?? '')} 行`)
  } finally {
    instance.unmount()
  }
}

main().catch(e => { console.error(e); process.exit(1) })
