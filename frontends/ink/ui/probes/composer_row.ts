/**
 * 探针：只看「屏幕已填满之后」的稳定期，输入框行号是否变化。
 *
 * 之前测的是"动态区行数"，但那在 inline 模式下本来就该随内容增长；
 * 真正让用户觉得"输入框在晃"的是：稳定运行期间输入框所在行是否跳动。
 */
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from '../src/App.js'
import { ReflowTerminal } from '../src/resizeReflowModel.js'
import type { BridgeClient } from '../src/bridgeClient.js'
import type { BridgeEvent } from '../src/protocol.js'

const COLS = 80
const ROWS = 24
const NL = String.fromCharCode(10)

class TermWriteStream extends EventEmitter {
  readonly term: ReflowTerminal
  readonly chunks: string[] = []
  isTTY = true
  constructor(columns: number, rows: number) {
    super()
    this.term = new ReflowTerminal(columns, rows)
  }
  get columns(): number { return this.term.columns }
  set columns(v: number) { this.term.columns = Math.max(1, Math.floor(v)) }
  get rows(): number { return this.term.rows }
  set rows(v: number) { this.term.rows = Math.max(1, Math.floor(v)) }
  write(chunk: unknown): boolean {
    this.chunks.push(String(chunk))
    this.term.write(String(chunk))
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

const delay = (ms: number) => new Promise(r => setTimeout(r, ms))

/** 输入框顶边框（'─' 长条）所在行号。 */
function inputFrameRow(screen: string[]): number | null {
  for (let i = screen.length - 1; i >= 0; i -= 1) {
    if (/^─{8,}$/.test(screen[i]!.trim())) return i
  }
  return null
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

  const stdout = new TermWriteStream(COLS, ROWS)
  const stderr = new TermWriteStream(COLS, ROWS)
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

  const readings: Array<{ phase: string; row: number | null }> = []
  const sample = (phase: string) => readings.push({ phase, row: inputFrameRow(stdout.term.screen()) })

  try {
    await delay(250)
    const sink = emit as unknown as (e: BridgeEvent) => void

    // 先灌足够多的历史，让屏幕填满 —— 这样后面测的才是稳定期。
    sink({ type: 'status', status: 'running', taskId: 1 })
    sink({ type: 'user', taskId: 1, text: 'FILL-USER' })
    for (let i = 0; i < 40; i += 1) {
      sink({ type: 'assistant_delta', taskId: 1, text: `FILL-${i}` + NL })
    }
    sink({ type: 'assistant_done', taskId: 1, text: 'FILL-DONE' })
    sink({ type: 'status', status: 'idle', taskId: 1 })
    await delay(400)
    sample('填满后 idle')

    // 第二轮：稳定期内的流式，逐行注入并采样
    sink({ type: 'status', status: 'running', taskId: 2 })
    sink({ type: 'user', taskId: 2, text: 'TURN2-USER' })
    await delay(120)
    sample('turn2 start')

    for (let i = 0; i < 24; i += 1) {
      sink({ type: 'assistant_delta', taskId: 2, text: `T2-${i}` + NL })
      await delay(35)
      sample(`turn2 stream ${i}`)
    }

    sink({ type: 'assistant_done', taskId: 2, text: 'T2-DONE' })
    sink({ type: 'status', status: 'idle', taskId: 2 })
    await delay(400)
    sample('turn2 done idle')
  } finally {
    instance.unmount()
  }

  console.log(`终端 ${ROWS} 行 × ${COLS} 列\n`)
  console.log('阶段'.padEnd(20), '输入框顶边框行号')
  console.log('-'.repeat(40))
  for (const r of readings) {
    console.log(r.phase.padEnd(20), r.row === null ? '(未找到)' : r.row)
  }

  const streamRows = readings
    .filter(r => r.phase.startsWith('turn2 stream'))
    .map(r => r.row)
    .filter((v): v is number => v !== null)
  const uniq = [...new Set(streamRows)].sort((a, b) => a - b)
  console.log('')
  console.log('稳定期（turn2 流式）输入框行号集合:', uniq)
  console.log('种数:', uniq.length)
  console.log('结论:', uniq.length <= 1 ? '稳定 —— 不晃' : `变化 ${uniq.length} 种 —— 会晃`)
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
