/**
 * 决定性验证：resizeReflow 的失败是不是「24 行窗口」尺寸效应。
 *
 * 完全照搬 src/resizeReflow.test.ts 第三段场景，只把终端行数参数化。
 * 若 spacer 在 40 行通过、只在 24 行失败，说明是简化模型在窄窗口下的
 * 边界行为，不是内容真丢失（真机 24/40 行均未复现丢失）。
 */
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from '../src/App.js'
import { createCursorParkStdout } from '../src/stdoutCursorPark.js'
import { ReflowTerminal, countVisibleRows } from '../src/resizeReflowModel.js'
import type { BridgeClient } from '../src/bridgeClient.js'
import type { BridgeEvent } from '../src/protocol.js'

const NL = String.fromCharCode(10)
const ROWS = Number(process.argv[2] ?? 24)
const LABEL = process.argv[3] ?? `rows=${ROWS}`
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

  const stdout = new ReflowWriteStream(80, ROWS)
  const stderr = new ReflowWriteStream(80, ROWS)
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

  const streamingProbes = [
    'HIST-PROBE-ALPHA', 'HIST-PROBE-BRAVO', 'HIST-PROBE-CHARLIE',
    'STREAM-PROBE-0', 'STREAM-PROBE-1', 'STREAM-PROBE-2', 'Enter keeps draft',
  ]

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

    sink({ type: 'status', status: 'running', taskId: 2 })
    sink({ type: 'user', taskId: 2, text: 'HIST-PROBE-CHARLIE' })
    for (let i = 0; i < 3; i += 1) {
      sink({ type: 'assistant_delta', taskId: 2, text: `STREAM-PROBE-${i}` + NL })
    }
    await delay(400)

    const screen = stdout.term.screen()
    const counts = streamingProbes.map(p => `${p.replace('HIST-PROBE-', '').replace('STREAM-PROBE-', 'S')}=${countVisibleRows(screen, p)}`)
    const bravo = countVisibleRows(screen, 'HIST-PROBE-BRAVO')
    console.log(`${LABEL}: BRAVO=${bravo}   ${counts.join('  ')}`)
    process.exitCode = bravo === 1 ? 0 : 1
  } finally {
    instance.unmount()
  }
}

main().catch(e => { console.error(e); process.exit(2) })
