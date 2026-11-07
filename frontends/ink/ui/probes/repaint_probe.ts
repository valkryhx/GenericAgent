/**
 * 探针：流式期间「每帧重画量」+「空闲时是否仍在重画」。
 *
 * 「轻微抖动」的两个候选来源：
 *   A. 流式每来一个 delta 就擦除+重写整帧（Ink log-update 的固有行为）
 *   B. 空闲时计时器每秒 setNow -> 整帧重画（即使内容没变）
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
  chunks: Array<{ t: number; data: string }> = []
  isTTY = true
  write(chunk: unknown): boolean {
    this.chunks.push({ t: Date.now(), data: String(chunk) })
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

/** 一次 write 里 eraseLines(n) 会产生 n 个 CSI 2K。 */
function eraseCount(data: string): number {
  return (data.match(/\u001b\[2K/g) ?? []).length
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

  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
  const stdin = new FakeReadStream()

  const instance = render(React.createElement(App, {
    python: 'python',
    bridgeScript: 'bridge.py',
    startBridgeClient,
  }), {
    stdout: stdout as unknown as NodeJS.WriteStream,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    patchConsole: false,
  })

  try {
    await delay(400)
    const sink = emit as unknown as (e: BridgeEvent) => void

    sink({ type: 'status', status: 'running', taskId: 1 })
    sink({ type: 'user', taskId: 1, text: 'T1' })
    sink({ type: 'assistant_done', taskId: 1, text: 'T1-ANSWER' })
    sink({ type: 'status', status: 'idle', taskId: 1 })
    await delay(500)

    // ── 测量 1：流式期间 ──────────────────────────────────────────
    const markA = stdout.chunks.length
    sink({ type: 'status', status: 'running', taskId: 2 })
    sink({ type: 'user', taskId: 2, text: 'T2' })
    const streamStart = Date.now()
    // 模拟真实 token 节奏：每 25ms 一行，持续 3 秒 = 120 行
    for (let i = 0; i < 120; i += 1) {
      sink({ type: 'assistant_delta', taskId: 2, text: `LINE-${i} some content here` + NL })
      await delay(25)
    }
    const streamDur = (Date.now() - streamStart) / 1000
    const streamWrites = stdout.chunks.slice(markA)
    sink({ type: 'assistant_done', taskId: 2, text: 'DONE' })
    sink({ type: 'status', status: 'idle', taskId: 2 })
    await delay(400)

    // ── 测量 2：空闲 3 秒（无任何事件）─────────────────────────────
    const markB = stdout.chunks.length
    await delay(3000)
    const idleWrites = stdout.chunks.slice(markB)

    const sum = (arr: Array<{ data: string }>) => arr.reduce((a, c) => a + c.data.length, 0)

    console.log('=== 测量 1：流式 120 行 / %.1fs ==='.replace('%.1f', streamDur.toFixed(1)))
    console.log(`  write 次数        = ${streamWrites.length}`)
    console.log(`  平均帧率          = ${(streamWrites.length / streamDur).toFixed(1)} fps`)
    console.log(`  总输出字节        = ${sum(streamWrites)}`)
    console.log(`  平均每次 write    = ${Math.round(sum(streamWrites) / Math.max(1, streamWrites.length))} 字节`)
    const erases = streamWrites.map(w => eraseCount(w.data)).filter(n => n > 0)
    if (erases.length) {
      console.log(`  平均擦除行数      = ${(erases.reduce((a, b) => a + b, 0) / erases.length).toFixed(1)}`)
      console.log(`  最大擦除行数      = ${Math.max(...erases)}`)
    }
    // 间隔分布
    const gaps = streamWrites.slice(1).map((w, i) => w.t - streamWrites[i]!.t)
    if (gaps.length) {
      gaps.sort((a, b) => a - b)
      console.log(`  帧间隔 中位数     = ${gaps[Math.floor(gaps.length / 2)]} ms`)
      console.log(`  帧间隔 最大       = ${gaps[gaps.length - 1]} ms`)
    }

    console.log('')
    console.log('=== 测量 2：空闲 3 秒（应无输出）===')
    console.log(`  write 次数        = ${idleWrites.length}`)
    console.log(`  总输出字节        = ${sum(idleWrites)}`)
    if (idleWrites.length > 0) {
      console.log('  ⚠️ 空闲时仍在重画 —— 每秒计时器触发的整帧重写')
      const sample = idleWrites.slice(0, 3).map(w => ({
        bytes: w.data.length,
        erase: eraseCount(w.data),
      }))
      console.log('  前 3 次:', JSON.stringify(sample))
    }
  } finally {
    instance.unmount()
  }
}

main().catch(e => { console.error(e); process.exit(1) })
