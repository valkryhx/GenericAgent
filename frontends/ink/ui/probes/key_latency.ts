/**
 * 探针：一次按键到「光标真正落到 caret」要多久？每个按键写多少字节？
 *
 * 判据（全部可断言）：
 *   latency = 从 stdin 事件到 park 写入完成的时间（ms）
 *   bytes   = 该次按键引起的所有 stdout 字节（unpark + 整帧 + park）
 * 对比：空闲 vs 流式输出中（后者是用户抱怨「回退更卡」的场景）。
 *
 * 运行： cd frontends/ink/ui && npx tsx probes/key_latency.ts
 */
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from '../src/App.js'
import { createCursorParkStdout } from '../src/stdoutCursorPark.js'
import type { BridgeClient } from '../src/bridgeClient.js'
import type { BridgeEvent } from '../src/protocol.js'

const NL = '\n'
const delay = (ms: number) => new Promise<void>(r => setTimeout(r, ms))

class CaptureWriteStream extends EventEmitter {
  columns = Number(process.env.PROBE_COLUMNS ?? 100)
  rows = Number(process.env.PROBE_ROWS ?? 30)
  isTTY = true
  chunks: string[] = []
  write(chunk: unknown): boolean { this.chunks.push(String(chunk)); return true }
}
class FakeReadStream extends EventEmitter {
  isTTY = true
  private queue: string[] = []
  setRawMode() { return this }
  setEncoding() { return this }
  ref() { return this }
  unref() { return this }
  read(): string | null { return this.queue.shift() ?? null }
  resume() { return this }
  pause() { return this }
  send(t: string) { this.queue.push(t); this.emit('readable') }
}

type Sample = { latency: number; bytes: number; writes: number }

async function main() {
  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
  const stdin = new FakeReadStream()
  const park = createCursorParkStdout(stdout as never)

  // 一次「按键处理」= 从 send() 到该次发生的最后一个 park 写入。
  let tracking: { startedAt: number; bytes: number; writes: number; done: (s: Sample) => void } | null = null
  let t0 = 0

  const baseWrite = stdout.write.bind(stdout)
  ;(stdout as unknown as { write: (c: unknown) => boolean }).write = (chunk: unknown) => {
    const s = String(chunk)
    if (tracking) {
      tracking.bytes += s.length
      tracking.writes += 1
      if (s.includes('\u001b[?25h')) {
        const t = tracking
        tracking = null
        // 下一 tick 结算，确保没有后续写入被漏算
        setImmediate(() => t.done({ latency: performance.now() - t.startedAt, bytes: t.bytes, writes: t.writes }))
      }
    }
    return baseWrite(chunk)
  }

  let emit: ((e: BridgeEvent) => void) | null = null
  const instance = render(
    React.createElement(App, {
      python: 'python', bridgeScript: 'x', cursorPark: park,
      startBridgeClient: (_p: string, _b: string, onEvent: (e: BridgeEvent) => void): BridgeClient => {
        emit = onEvent
        setTimeout(() => onEvent({ type: 'ready', version: 1 }), 0)
        return { send() {}, stop() {} }
      },
    } as never),
    {
      stdout: park.stdout,
      stderr: stderr as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      patchConsole: false,
      exitOnCtrlC: false,
    } as never,
  )

  const e = () => emit as unknown as (ev: BridgeEvent) => void

  const press = (key: string): Promise<Sample> => new Promise(resolve => {
    tracking = { startedAt: performance.now(), bytes: 0, writes: 0, done: resolve }
    t0 = performance.now()
    stdin.send(key)
    // 兜底：某些按键（如无变化）可能不产生 park
    setTimeout(() => {
      if (tracking) {
        const t = tracking; tracking = null
        resolve({ latency: performance.now() - t.startedAt, bytes: t.bytes, writes: t.writes })
      }
    }, 3000)
  })
  void t0

  const stats = (label: string, s: Sample[]) => {
    const lat = s.map(x => x.latency).sort((a, b) => a - b)
    const by = s.map(x => x.bytes)
    const p = (q: number) => lat.length ? Math.round(lat[Math.min(lat.length - 1, Math.floor(lat.length * q))]!) : 0
    console.log(`\n--- ${label} ---  样本=${s.length}`)
    console.log(`  延迟 ms: p50=${p(0.5)}  p90=${p(0.9)}  max=${Math.round(lat.at(-1) ?? 0)}`)
    console.log(`  字节/按键: avg=${Math.round(by.reduce((a, b) => a + b, 0) / Math.max(1, by.length))}  max=${Math.max(0, ...by)}`)
    console.log(`  写入次数/按键: avg=${(s.reduce((a, x) => a + x.writes, 0) / Math.max(1, s.length)).toFixed(1)}`)
  }

  try {
    await delay(400)

    // A) 空闲打字
    const a: Sample[] = []
    for (const ch of 'abcdefgh'.split('')) { a.push(await press(ch)); await delay(120) }
    stats('A 空闲 · 逐字输入', a)

    // B) 回退（空闲）
    const b: Sample[] = []
    for (let i = 0; i < 4; i += 1) { b.push(await press('\u007f')); await delay(120) }
    stats('B 空闲 · 回退', b)

    // C) 运行中流式输出（持续喂 delta），期间打字
    e()({ type: 'status', status: 'running', taskId: 1 })
    e()({ type: 'user', taskId: 1, text: 'stream' })
    let streaming = true
    const pump = (async () => {
      let i = 0
      while (streaming) {
        e()({ type: 'assistant_delta', taskId: 1, text: `chunk-${i++} ` + 'z'.repeat(30) + NL })
        await delay(20)
      }
    })()
    await delay(300)
    const c: Sample[] = []
    for (const ch of 'qwert'.split('')) { c.push(await press(ch)); await delay(100) }
    stats('C 运行中 · 流式输出中打字', c)

    // D) 运行中回退
    const d: Sample[] = []
    for (let i = 0; i < 4; i += 1) { d.push(await press('\u007f')); await delay(100) }
    stats('D 运行中 · 流式输出中回退', d)

    streaming = false
    await pump

    console.log(`\n=== 说明 ===`)
    console.log(`  ink 5.2.1 的 onRender 被 throttle 到 32ms（上限 31fps）；每帧整块重写。`)
    console.log(`  Claude Code fork: FRAME_INTERVAL_MS=16（62fps），且只写屏幕 diff + BSU/ESU 原子提交。`)
  } finally {
    instance.unmount()
    await delay(150)
  }
}

main().catch(err => { console.error(err); process.exit(1) })
