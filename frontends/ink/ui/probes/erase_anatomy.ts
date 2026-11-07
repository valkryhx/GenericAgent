/**
 * 精确核对：每次 write 的 eraseLines 计数 vs 上一帧的实际行数。
 *
 * ink 的 log-update：`write(eraseLines(previousLineCount) + output)`，
 * previousLineCount = 上次 output 的 split('\n').length（= 行数 + 1）。
 * 若我的 spacer 让上一步的 previousLineCount 与实际屏幕占用不符，
 * erase 就会多擦一行 → 吃掉 Static 最后一行。
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

/** 统计一次 write 里的擦除上移次数与写入行数。 */
function anatomy(data: string) {
  const ups = (data.match(/\u001b\[\d*A/g) ?? []).reduce((n, m) => {
    const v = Number(m.slice(2, -1) || '1')
    return n + v
  }, 0)
  const eraseAll = (data.match(/\u001b\[2K/g) ?? []).length
  const body = data
    .replace(/\u001b\][^\u0007]*(?:\u0007|\u001b\\)/g, '')
    .replace(/\u001b\[[0-?]*[ -/]*[@-~]/g, '')
  const lines = body.split(NL).length
  return { ups, eraseAll, lines }
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

  const label = process.argv[2] ?? 'run'

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

    console.log(`===== ${label} =====`)
    console.log('轮 2 各次写入：')
    for (let i = mark; i < stdout.chunks.length; i += 1) {
      const a = anatomy(stdout.chunks[i]!)
      const hasBravo = stdout.chunks[i]!.includes('HIST-PROBE-BRAVO')
      const hasAlpha = stdout.chunks[i]!.includes('HIST-PROBE-ALPHA')
      console.log(
        `  #${i - mark}  上移${String(a.ups).padStart(2)}  擦行${String(a.eraseAll).padStart(2)}`
        + `  写入${String(a.lines).padStart(2)}行`
        + (hasAlpha ? '  [含ALPHA]' : '')
        + (hasBravo ? '  [含BRAVO]' : ''),
      )
    }
    const screen = stdout.term.screen()
    console.log(`\n最终缓冲 BRAVO 计数: ${screen.filter(l => l.includes('HIST-PROBE-BRAVO')).length}`)
    console.log('缓冲区全量：')
    stdout.term.visualRows().forEach((l, i) => console.log(`  ${String(i).padStart(2)} | ${l.slice(0, 70)}`))
  } finally {
    instance.unmount()
  }
}

main().catch(e => { console.error(e); process.exit(1) })
