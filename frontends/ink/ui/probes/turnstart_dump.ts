/**
 * 逐行 dump：turn 开始前后每一行是什么，定位那 1 行从哪来。
 */
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from '../src/App.js'
import type { BridgeClient } from '../src/bridgeClient.js'
import type { BridgeEvent } from '../src/protocol.js'

const NL = String.fromCharCode(10)
const delay = (ms: number) => new Promise(r => setTimeout(r, ms))

class CaptureWriteStream extends EventEmitter {
  columns = 120
  rows = 40
  chunks: string[] = []
  isTTY = true
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

function lines(data: string): string[] {
  const out = stripAnsi(data).split(NL)
  while (out.length > 0 && out[out.length - 1]!.trim() === '') out.pop()
  return out
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

  const stdout = new CaptureWriteStream()
  const instance = render(React.createElement(App, {
    python: 'python', bridgeScript: 'bridge.py', startBridgeClient,
  }), {
    stdout: stdout as unknown as NodeJS.WriteStream,
    stderr: new CaptureWriteStream() as unknown as NodeJS.WriteStream,
    stdin: new FakeReadStream() as unknown as NodeJS.ReadStream,
    patchConsole: false,
  })

  const snapshots: { name: string; rows: string[] }[] = []
  const snap = (name: string) => {
    snapshots.push({ name, rows: lines(stdout.chunks[stdout.chunks.length - 1] ?? '') })
  }

  try {
    await delay(400)
    snap('1-idle-empty')

    const sink = emit as unknown as (e: BridgeEvent) => void
    // 模拟用户已输入文本但未提交（typed）
    sink({ type: 'status', status: 'running', taskId: 1 })
    sink({ type: 'user', taskId: 1, text: 'Q' })
    await delay(300)
    snap('2-running-after-submit')
  } finally {
    instance.unmount()
  }

  for (const s of snapshots) {
    console.log(`\n===== ${s.name}  (${s.rows.length} 行) =====`)
    s.rows.forEach((r, i) => console.log(`  ${String(i).padStart(2)} | ${r.slice(0, 90)}`))
  }

  console.log('\n===== 差异 =====')
  const a = snapshots[0]!.rows
  const b = snapshots[1]!.rows
  console.log(`  行数: ${a.length} -> ${b.length}  (${b.length - a.length >= 0 ? '+' : ''}${b.length - a.length})`)
  const max = Math.max(a.length, b.length)
  for (let i = 0; i < max; i += 1) {
    const l = (a[i] ?? '<无>').slice(0, 60)
    const r = (b[i] ?? '<无>').slice(0, 60)
    if (l !== r) console.log(`  row ${String(i).padStart(2)}: ${JSON.stringify(l)}  =>  ${JSON.stringify(r)}`)
  }
}

main().catch(e => { console.error(e); process.exit(1) })
