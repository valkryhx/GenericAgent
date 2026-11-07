/**
 * 判定：BRAVO 消失是不是因为 ink 走进了
 *   `outputHeight >= stdout.rows` → clearTerminal + fullStaticOutput + output
 * 这个分支？
 *
 * 该分支只在「主帧高度 >= 终端行数」时触发。spacer 让主帧多 1 行，
 * 24 行终端下就容易越线。
 */
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from '../src/App.js'
import { createCursorParkStdout } from '../src/stdoutCursorPark.js'
import { ReflowTerminal } from '../src/resizeReflowModel.js'
import type { BridgeClient } from '../src/bridgeClient.js'
import type { BridgeEvent } from '../src/protocol.js'

const NL = String.fromCharCode(10)
const ESC = String.fromCharCode(27)
const delay = (ms: number) => new Promise(r => setTimeout(r, ms))

class ReflowWriteStream extends EventEmitter {
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

async function main() {
  let emit: ((e: BridgeEvent) => void) | null = null
  const startBridgeClient = (
    _p: string, _s: string, onEvent: (e: BridgeEvent) => void,
  ): BridgeClient => {
    emit = onEvent
    setTimeout(() => onEvent({ type: 'ready', version: 1 }), 0)
    return { send() {}, stop() {} }
  }

  const stdout = new ReflowWriteStream(80, 24)
  const stderr = new ReflowWriteStream(80, 24)
  const stdin = new FakeReadStream()
  const cursorPark = createCursorParkStdout(stdout as unknown as NodeJS.WriteStream)
  const instance = render(React.createElement(App, {
    python: 'python', bridgeScript: 'bridge.py', startBridgeClient, cursorPark,
  }), {
    stdout: cursorPark.stdout,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    patchConsole: false,
  })

  try {
    await delay(200)
    const sink = emit as unknown as (e: BridgeEvent) => void
    sink({ type: 'status', status: 'running', taskId: 1 })
    sink({ type: 'user', taskId: 1, text: 'HIST-PROBE-ALPHA' })
    sink({ type: 'assistant_done', taskId: 1, text: 'HIST-PROBE-BRAVO' })
    sink({ type: 'status', status: 'idle', taskId: 1 })
    await delay(300)

    stdout.columns = 40
    stderr.columns = 40
    stdout.emit('resize')
    await delay(400)
    stdout.columns = 100
    stderr.columns = 100
    stdout.emit('resize')
    await delay(400)

    const mark = stdout.chunks.length
    sink({ type: 'status', status: 'running', taskId: 2 })
    sink({ type: 'user', taskId: 2, text: 'HIST-PROBE-CHARLIE' })
    for (let i = 0; i < 3; i += 1) {
      sink({ type: 'assistant_delta', taskId: 2, text: `STREAM-PROBE-${i}` + NL })
    }
    await delay(400)

    const after = stdout.chunks.slice(mark).join('')
    const CLEAR_TERMINAL = ESC + '[2J' + ESC + '[3J' + ESC + '[H'
    console.log(`轮 2 之后写入 ${stdout.chunks.length - mark} 块`)
    console.log(`包含 clearTerminal 序列: ${after.includes(CLEAR_TERMINAL)}`)
    console.log(`包含 ESC[2J: ${after.includes(ESC + '[2J')}`)
    console.log(`包含 ESC[3J: ${after.includes(ESC + '[3J')}`)
    console.log(`包含 BRAVO: ${after.includes('HIST-PROBE-BRAVO')}`)
    console.log(`包含 ALPHA: ${after.includes('HIST-PROBE-ALPHA')}`)

    // 找出第一次出现 BRAVO 的块与之后是否再被擦
    const idx = stdout.chunks.findIndex((c, i) => i >= mark && c.includes('HIST-PROBE-BRAVO'))
    console.log(`\n轮 2 内 BRAVO 出现在第 ${idx < 0 ? '（无）' : idx - mark} 块`)
  } finally {
    instance.unmount()
  }
}

main().catch(e => { console.error(e); process.exit(1) })
