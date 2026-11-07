/**
 * 判定：spacer 是否让主帧高度越过 `outputHeight >= stdout.rows` 阈值，
 * 从而进入 clearTerminal 分支（该分支不重发 Static，之前的内容就丢了）。
 *
 * ink.js:117-121:
 *   if (outputHeight >= this.options.stdout.rows) {
 *       write(clearTerminal + fullStaticOutput + output); return;   // 不发 staticOutput 之外的
 *   }
 *
 * 注意 clearTerminal = ESC[2J ESC[3J ESC[H，会清掉 scrollback。
 * 若这条分支在 resize 之后被触发，历史行会被清空 —— 与本测试现象一致。
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

    const CLEAR = ESC + '[2J' + ESC + '[3J' + ESC + '[H'
    console.log(`===== ${label} =====`)
    let cleared = 0
    for (let i = 0; i < stdout.chunks.length; i += 1) {
      if (stdout.chunks[i]!.includes(CLEAR)) {
        cleared += 1
        console.log(`  #${i}: 含 clearTerminal (${i >= mark ? '轮2内' : '更早'})`)
      }
    }
    console.log(`  clearTerminal 次数: ${cleared}`)

    // 每次写入后的主帧高度（去掉 clearTerminal 前缀后数行）
    console.log('\n  轮 2 各次写入的主帧高度：')
    for (let i = mark; i < stdout.chunks.length; i += 1) {
      const raw = stdout.chunks[i]!
      const stripped = raw
        .replace(/\u001b\][^\u0007]*(?:\u0007|\u001b\\)/g, '')
        .replace(/\u001b\[[0-?]*[ -/]*[@-~]/g, '')
      const lines = stripped.split(NL)
      while (lines.length > 0 && lines[lines.length - 1] === '') lines.pop()
      console.log(`    #${i - mark}: ${lines.length} 行`)
    }

    const screen = stdout.term.screen()
    console.log(`\n  最终 BRAVO: ${screen.filter(l => l.includes('HIST-PROBE-BRAVO')).length}`)
  } finally {
    instance.unmount()
  }
}

main().catch(e => { console.error(e); process.exit(1) })
