/** 完整复刻 resizeReflow.test.ts 的时序，打印每个 phase 的 24 行屏幕。 */
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from '../src/App.js'
import { ReflowTerminal } from '../src/resizeReflowModel.js'
import { createCursorParkStdout } from '../src/stdoutCursorPark.js'
import type { BridgeClient } from '../src/bridgeClient.js'
import type { BridgeEvent } from '../src/protocol.js'

const NL = String.fromCharCode(10)
const delay = (ms: number) => new Promise(r => setTimeout(r, ms))

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

  const stdout = new TermWriteStream(80, 24)
  const stderr = new TermWriteStream(80, 24)
  const stdin = new FakeReadStream()
  const cursorPark = createCursorParkStdout(stdout as unknown as NodeJS.WriteStream)

  const instance = render(React.createElement(App, {
    python: 'python',
    bridgeScript: 'bridge.py',
    startBridgeClient,
    cursorPark,
  }), {
    stdout: cursorPark.stdout,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    patchConsole: false,
  })

  const dump = (label: string) => {
    const screen = stdout.term.screen()
    const has = (s: string) => screen.filter(l => l.includes(s)).length
    console.log(`\n===== ${label} (cols=${stdout.term.columns}) =====`)
    console.log(`  ALPHA=${has('HIST-PROBE-ALPHA')} BRAVO=${has('HIST-PROBE-BRAVO')} CHARLIE=${has('HIST-PROBE-CHARLIE')}`)
    screen.forEach((line, i) => {
      if (line.trim()) console.log(`${String(i).padStart(2)}| ${line}`)
    })
  }

  try {
    await delay(200)
    const sink = emit as unknown as (e: BridgeEvent) => void
    sink({ type: 'status', status: 'running', taskId: 1 })
    sink({ type: 'user', taskId: 1, text: 'HIST-PROBE-ALPHA' })
    sink({ type: 'assistant_done', taskId: 1, text: 'HIST-PROBE-BRAVO' })
    sink({ type: 'status', status: 'idle', taskId: 1 })
    await delay(300)
    dump('1. resize 之前')

    stdout.columns = 40; stderr.columns = 40
    stdout.emit('resize')
    await delay(400)
    dump('2. 变窄 40 之后')

    stdout.columns = 100; stderr.columns = 100
    stdout.emit('resize')
    await delay(400)
    dump('3. 变宽 100 之后')

    sink({ type: 'status', status: 'running', taskId: 2 })
    sink({ type: 'user', taskId: 2, text: 'HIST-PROBE-CHARLIE' })
    for (let i = 0; i < 3; i += 1) {
      sink({ type: 'assistant_delta', taskId: 2, text: `STREAM-PROBE-${i}` + NL })
    }
    await delay(200)
    dump('4. turn2 流式（失败点）')
  } finally {
    instance.unmount()
  }
}

main().catch(e => { console.error(e); process.exit(1) })
