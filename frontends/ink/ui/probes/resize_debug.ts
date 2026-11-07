/**
 * 复现 resizeReflow 第三段断言失败，并 dump 完整缓冲区，看 BRAVO 去哪了。
 * 这是 .tmp_ 探针，不改测试本身。
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

function dump(label: string, term: ReflowTerminal, probes: string[]) {
  const all = term.visualRows()
  const screen = term.screen()
  console.log(`\n===== ${label} =====`)
  console.log(`  缓冲区可视行总数 ${all.length}，屏幕 ${term.rows} 行`)
  console.log(`  探针计数: ${probes.map(p => `${p}=${countVisibleRows(screen, p)}`).join('  ')}`)
  console.log('  --- 屏幕最后 26 行 ---')
  const start = Math.max(0, screen.length - 26)
  screen.slice(start).forEach((l, i) => {
    if (l.trim() === '' && i < 6) return
    console.log(`   ${String(start + i).padStart(2)} | ${l.slice(0, 88)}`)
  })
  console.log('  --- 缓冲区全量（前 40 行）---')
  all.slice(0, 40).forEach((l, i) => console.log(`   ${String(i).padStart(2)} | ${l.slice(0, 88)}`))
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

  const idleProbes = ['HIST-PROBE-ALPHA', 'HIST-PROBE-BRAVO', 'Enter send', 'Worked for']
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
    dump('1. 轮 1 完成（80 宽）', stdout.term, idleProbes)

    stdout.columns = 40
    stderr.columns = 40
    stdout.emit('resize')
    await delay(400)
    dump('2. 变窄到 40', stdout.term, idleProbes)

    stdout.columns = 100
    stderr.columns = 100
    stdout.emit('resize')
    await delay(400)
    dump('3. 变宽到 100', stdout.term, idleProbes)

    sink({ type: 'status', status: 'running', taskId: 2 })
    sink({ type: 'user', taskId: 2, text: 'HIST-PROBE-CHARLIE' })
    for (let i = 0; i < 3; i += 1) {
      sink({ type: 'assistant_delta', taskId: 2, text: `STREAM-PROBE-${i}` + NL })
    }
    await delay(300)
    dump('4. 轮 2 流式（失败点）', stdout.term, streamingProbes)
  } finally {
    instance.unmount()
  }
}

main().catch(e => { console.error(e); process.exit(1) })
