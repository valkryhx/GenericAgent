import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from '../src/App.js'
import { createCursorParkStdout } from '../src/stdoutCursorPark.js'
import type { BridgeClient } from '../src/bridgeClient.js'
import type { BridgeEvent } from '../src/protocol.js'
import { ReflowTerminal } from '../src/resizeReflowModel.js'

class CaptureWriteStream extends EventEmitter {
  columns: number
  rows: number
  chunks: string[] = []
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

const delay = (ms: number) => new Promise<void>(r => setTimeout(r, ms))

function bigToolTurn(taskId: number, lines: number): BridgeEvent[] {
  const events: BridgeEvent[] = []
  events.push({ type: 'user', taskId, text: '跑一下 code_run 大量输出' } as BridgeEvent)
  events.push({ type: 'status', status: 'running', taskId } as BridgeEvent)
  const body: string[] = []
  body.push('我先执行一个脚本。\n')
  for (let i = 0; i < lines; i += 1) {
    body.push(`L${String(i).padStart(3, '0')} code_run stdout line payload ........`)
  }
  const text = body.join('\n')
  // stream in 40 chunks
  const chunkSize = Math.ceil(text.length / 40)
  for (let off = 0; off < text.length; off += chunkSize) {
    events.push({ type: 'assistant_delta', taskId, text: text.slice(off, off + chunkSize) } as BridgeEvent)
  }
  events.push({ type: 'assistant_done', taskId, text } as BridgeEvent)
  events.push({ type: 'status', status: 'idle', taskId } as BridgeEvent)
  return events
}

async function main() {
  const rows = Number(process.env.PROBE_ROWS ?? 24)
  const columns = Number(process.env.PROBE_COLUMNS ?? 100)
  const lines = Number(process.env.PROBE_LINES ?? 120)
  const stdout = new CaptureWriteStream(columns, rows)
  const stdin = new FakeReadStream()
  const park = createCursorParkStdout(stdout as never)
  const queue: BridgeEvent[] = []
  const startBridgeClient = (_p: string, _b: string, onEvent: (e: BridgeEvent) => void): BridgeClient => {
    setTimeout(() => onEvent({ type: 'ready', version: 1 } as BridgeEvent), 0)
    const timer = setInterval(() => {
      const next = queue.shift()
      if (next) onEvent(next)
    }, 15)
    return { send() {}, stop() { clearInterval(timer) } }
  }
  const instance = render(
    React.createElement(App, { python: 'python', bridgeScript: 'x', startBridgeClient, cursorPark: park } as never),
    { stdout: park.stdout as never, stdin: stdin as never, exitOnCtrlC: false } as never,
  )
  await delay(200)
  queue.push(...bigToolTurn(1, lines))
  await delay(2500)
  // second turn, short
  queue.push({ type: 'user', taskId: 2, text: '第二轮' } as BridgeEvent)
  queue.push({ type: 'status', status: 'running', taskId: 2 } as BridgeEvent)
  queue.push({ type: 'assistant_delta', taskId: 2, text: '第二轮简短回复\n' } as BridgeEvent)
  queue.push({ type: 'assistant_done', taskId: 2, text: '第二轮简短回复\n' } as BridgeEvent)
  queue.push({ type: 'status', status: 'idle', taskId: 2 } as BridgeEvent)
  await delay(1200)
  instance.unmount()
  await delay(100)

  const raw = stdout.chunks.join('')
  const vt = new ReflowTerminal(columns, rows)
  let clear2 = 0
  let clear3 = 0
  let eraseLineUp = 0
  let firstClearAt = -1
  stdout.chunks.forEach((chunk, i) => {
    const c2 = chunk.split('\u001B[2J').length - 1
    const c3 = chunk.split('\u001B[3J').length - 1
    if (c2 > 0 || c3 > 0) {
      if (firstClearAt < 0) firstClearAt = i
      clear2 += c2
      clear3 += c3
    }
    eraseLineUp += chunk.split('\u001B[1A').length - 1
  })
  vt.write(raw.replace(/\u001B\[\?25[lh]/g, ''))
  const screen = vt.screen()

  const plain = raw.replace(/\u001B\][^\u0007]*(?:\u0007|\u001B\\)/g, '').replace(/\u001B\[[0-?]*[ -/]*[@-~]/g, '')
  const counts = [0, 1, 2, 3, 10, 11, 20, 40, 80, 119].map(n => {
    const needle = `L${String(n).padStart(3, '0')}`
    return `${needle}:${(plain.split(needle).length - 1)}`
  })

  console.log(JSON.stringify({ rows, columns, chunks: stdout.chunks.length, clear2, clear3, firstClearAt, lineMarkers: counts }, null, 2))
  console.log('--- final screen ---')
  console.log(screen.join('\n'))
  console.log('--- tail of scrollback buffer (last 30 visual rows) ---')
  console.log(vt.visualRows().slice(-30).join('\n'))
}

void main()
