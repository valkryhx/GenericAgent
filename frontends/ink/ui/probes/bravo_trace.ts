/**
 * 逐块 dump：resize 前后每一次写入后，缓冲区变成什么样。
 * 目标是找出 BRAVO 在哪一步消失，而不是继续猜。
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

  // 每次写入后记录缓冲区快照
  const history: { n: number; bravo: number; rows: string[] }[] = []
  let counter = 0
  const origWrite = stdout.write.bind(stdout)
  stdout.write = (chunk: unknown): boolean => {
    const r = origWrite(chunk)
    counter += 1
    const rows = stdout.term.visualRows()
    history.push({
      n: counter,
      bravo: rows.filter(l => l.includes('HIST-PROBE-BRAVO')).length,
      rows: rows.map(l => l.replace(/\s+$/, '')),
    })
    return r
  }

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
    const afterTurn1 = counter
    console.log(`轮 1 结束：写入 ${afterTurn1} 块，BRAVO=${history[history.length - 1]?.bravo}`)
    console.log('缓冲区：')
    history[history.length - 1]?.rows.forEach((l, i) => { if (l) console.log(`  ${String(i).padStart(2)} | ${l.slice(0, 70)}`) })

    stdout.columns = 40
    stderr.columns = 40
    stdout.emit('resize')
    await delay(400)
    console.log(`\n变窄到 40 后：BRAVO=${history[history.length - 1]?.bravo}`)

    stdout.columns = 100
    stderr.columns = 100
    stdout.emit('resize')
    await delay(400)
    console.log(`变宽到 100 后：BRAVO=${history[history.length - 1]?.bravo}`)
    console.log('缓冲区：')
    history[history.length - 1]?.rows.forEach((l, i) => { if (l) console.log(`  ${String(i).padStart(2)} | ${l.slice(0, 70)}`) })

    console.log('\n=== 每次写入后 BRAVO 计数变化 ===')
    let prev = -1
    for (const h of history) {
      if (h.bravo !== prev) {
        console.log(`  #${h.n}: BRAVO ${prev} -> ${h.bravo}`)
        prev = h.bravo
      }
    }
    // 打印 BRAVO 消失那一步前后的缓冲区
    const lossIdx = history.findIndex((h, i) => i > 0 && history[i - 1]!.bravo > 0 && h.bravo === 0)
    if (lossIdx > 0) {
      console.log(`\n=== BRAVO 在 #${history[lossIdx]!.n} 消失，前后对比 ===`)
      const before = history[lossIdx - 1]!
      const after = history[lossIdx]!
      const max = Math.max(before.rows.length, after.rows.length)
      for (let i = 0; i < max; i += 1) {
        const b = (before.rows[i] ?? '<无>').slice(0, 50)
        const a = (after.rows[i] ?? '<无>').slice(0, 50)
        if (b !== a) console.log(`  row ${String(i).padStart(2)}: ${JSON.stringify(b)}\n        => ${JSON.stringify(a)}`)
      }
    } else {
      console.log('\nBRAVO 从未归零（或被 resize 重置直接清掉）')
    }
  } finally {
    instance.unmount()
  }
}

main().catch(e => { console.error(e); process.exit(1) })
