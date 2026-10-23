import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from '../src/App.js'
import { createCursorParkStdout } from '../src/stdoutCursorPark.js'
import type { BridgeClient } from '../src/bridgeClient.js'
import type { BridgeEvent } from '../src/protocol.js'

class CaptureWriteStream extends EventEmitter {
  isTTY = true
  rows: number
  columns: number
  chunks: string[] = []
  constructor(columns: number, rows: number) {
    super()
    this.columns = columns
    this.rows = rows
  }
  write(chunk: string) {
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

function bigTurn(taskId: number, lines: number, chunkCount: number): BridgeEvent[] {
  const events: BridgeEvent[] = []
  events.push({ type: 'user', taskId, text: '跑一下 code_run 大量输出' } as BridgeEvent)
  events.push({ type: 'status', status: 'running', taskId } as BridgeEvent)
  const body: string[] = ['我先执行一个脚本。', '']
  for (let i = 0; i < lines; i += 1) body.push(`L${String(i).padStart(3, '0')} stdout payload ........`)
  const text = body.join('\n')
  const size = Math.ceil(text.length / chunkCount)
  for (let off = 0; off < text.length; off += size) {
    events.push({ type: 'assistant_delta', taskId, text: text.slice(off, off + size) } as BridgeEvent)
  }
  events.push({ type: 'assistant_done', taskId, text } as BridgeEvent)
  events.push({ type: 'status', status: 'idle', taskId } as BridgeEvent)
  return events
}

/** 解析每次 write：是 ink 主帧（含 eraseLines）还是 park/unpark 光标序列。 */
function classify(chunk: string) {
  const eraseCount = (chunk.match(/\u001b\[2K/g) ?? []).length
  const isPark = /\u001b\[\?25h$/.test(chunk) && /\u001b\[\d+A/.test(chunk)
  const isUnpark = /^\u001b\[\?25l/.test(chunk) && /\u001b\[\d+B/.test(chunk)
  const upMatch = /\u001b\[(\d+)A/.exec(chunk)
  const downMatch = /\u001b\[(\d+)B/.exec(chunk)
  return {
    eraseCount,
    isPark,
    isUnpark,
    up: upMatch ? Number(upMatch[1]) : null,
    down: downMatch ? Number(downMatch[1]) : null,
    lines: chunk.split('\n').length,
    clearScreen: /\u001b\[2J/.test(chunk),
    clearScrollback: /\u001b\[3J/.test(chunk),
  }
}

async function main() {
  const rows = Number(process.env.PROBE_ROWS ?? 24)
  const columns = Number(process.env.PROBE_COLUMNS ?? 100)
  const lines = Number(process.env.PROBE_LINES ?? 120)
  const chunks = Number(process.env.PROBE_CHUNKS ?? 40)
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
  queue.push(...bigTurn(1, lines, chunks))
  await delay(3000)
  instance.unmount()
  await delay(100)

  const frames: string[] = []
  let prevFrameH: number | null = null
  let prevFrameErase: number | null = null
  for (const chunk of stdout.chunks) {
    const info = classify(chunk)
    if (info.eraseCount > 0 && !info.isPark && !info.isUnpark) {
      // ink 主帧：eraseLines(n) + output + '\n'
      const idx = chunk.lastIndexOf('\u001b[0G')
      const body = idx >= 0 ? chunk.slice(idx + 4) : chunk
      const h = body.split('\n').length - 1
      frames.push(
        `erase=${info.eraseCount} H=${h} prevH=${prevFrameH ?? '-'} prevErase=${prevFrameErase ?? '-'} ` +
          `${prevFrameH !== null && prevFrameErase !== null && prevFrameErase !== prevFrameH + 1 ? '<<ERASE_MISMATCH' : ''}`,
      )
      prevFrameH = h
      prevFrameErase = info.eraseCount
    }
  }
  console.log(JSON.stringify({ rows, columns, totalWrites: stdout.chunks.length, frames: frames.length }, null, 2))
  console.log('--- frames (first 12 / last 12) ---')
  console.log(frames.slice(0, 12).join('\n'))
  console.log('...')
  console.log(frames.slice(-12).join('\n'))
  const mismatches = frames.filter(f => f.includes('ERASE_MISMATCH')).length
  console.log(`ERASE_MISMATCH count = ${mismatches}`)
}

void main()
