/**
 * 验证：把同一 tick 内的多次 stdout.write 合并成一次，是否可行。
 *
 * 根因：ink.js:127-131 在 hasStaticOutput 时执行三次独立 write
 *        log.clear() -> stdout.write(staticOutput) -> log(output)
 *   Windows ConPTY 每次 write 是独立一次管道传输，中间终端可能刷新 -> 闪。
 *
 * 方案：stdout proxy，用 queueMicrotask 收集同一 tick 的写入，一次 flush。
 *   优点：不改 node_modules，不动 ?2026h（ConPTY 会破坏），可验证。
 */
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from '../src/App.js'
import type { BridgeClient } from '../src/bridgeClient.js'
import type { BridgeEvent } from '../src/protocol.js'

const NL = String.fromCharCode(10)
const delay = (ms: number) => new Promise(r => setTimeout(r, ms))

class UnderlyingStream extends EventEmitter {
  columns = 120
  rows = 40
  writes: Array<{ t: number; len: number }> = []
  isTTY = true
  write(chunk: unknown): boolean {
    this.writes.push({ t: Date.now(), len: String(chunk).length })
    return true
  }
}

/** 把同一 tick 内的多次 write 合并成一次底层 write。 */
function coalesce(target: UnderlyingStream) {
  let pending: string[] = []
  let scheduled = false
  const flush = () => {
    scheduled = false
    if (pending.length === 0) return
    const merged = pending.join('')
    pending = []
    target.write(merged)
  }
  return {
    get columns() { return target.columns },
    get rows() { return target.rows },
    isTTY: true,
    write(chunk: unknown): boolean {
      pending.push(String(chunk))
      if (!scheduled) {
        scheduled = true
        queueMicrotask(flush)
      }
      return true
    },
    on: target.on.bind(target),
    off: target.off.bind(target),
    removeListener: target.removeListener.bind(target),
    emit: target.emit.bind(target),
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

async function run(useCoalesce: boolean) {
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

  const underlying = new UnderlyingStream()
  const out = useCoalesce ? coalesce(underlying) : underlying
  const stderr = new UnderlyingStream()
  const stdin = new FakeReadStream()

  const instance = render(React.createElement(App, {
    python: 'python',
    bridgeScript: 'bridge.py',
    startBridgeClient,
  }), {
    stdout: out as unknown as NodeJS.WriteStream,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    patchConsole: false,
  })

  try {
    await delay(400)
    const sink = emit as unknown as (e: BridgeEvent) => void
    sink({ type: 'status', status: 'running', taskId: 1 })
    sink({ type: 'user', taskId: 1, text: 'PROMPT' })
    const mark = underlying.writes.length
    for (let i = 0; i < 60; i += 1) {
      sink({ type: 'assistant_delta', taskId: 1, text: `line ${i} of text` + NL })
      await delay(30)
    }
    sink({ type: 'assistant_done', taskId: 1, text: 'DONE' })
    sink({ type: 'status', status: 'idle', taskId: 1 })
    await delay(400)

    const w = underlying.writes.slice(mark)
    // 同一毫秒内多次 write = 一帧被拆成多次传输
    const byMs = new Map<number, number>()
    for (const x of w) byMs.set(x.t, (byMs.get(x.t) ?? 0) + 1)
    const splitFrames = [...byMs.values()].filter(n => n > 1).length
    const maxPerMs = Math.max(...byMs.values())
    return {
      total: w.length,
      msBuckets: byMs.size,
      splitFrames,
      maxPerMs,
      mode: useCoalesce ? 'coalesce' : 'plain',
    }
  } finally {
    instance.unmount()
  }
}

async function main() {
  const plain = await run(false)
  const merged = await run(true)

  console.log('=== 对照 ===')
  console.log('指标                      plain      coalesce')
  console.log(`底层 write 次数            ${String(plain.total).padStart(6)}     ${String(merged.total).padStart(6)}`)
  console.log(`不同毫秒桶数               ${String(plain.msBuckets).padStart(6)}     ${String(merged.msBuckets).padStart(6)}`)
  console.log(`一帧被拆成多次的桶数        ${String(plain.splitFrames).padStart(6)}     ${String(merged.splitFrames).padStart(6)}`)
  console.log(`单毫秒内最多 write 次数     ${String(plain.maxPerMs).padStart(6)}     ${String(merged.maxPerMs).padStart(6)}`)
  console.log('')
  const better = merged.maxPerMs < plain.maxPerMs || merged.splitFrames < plain.splitFrames
  console.log(better ? '=> coalesce 有效：帧被合并，减少中间空窗' : '=> coalesce 无明显效果')
}

main().catch(e => { console.error(e); process.exit(1) })
