/**
 * 探针：「park 规格用了旧一次 commit 的 caret」这一竞态的量化（已知 ink 5.2.1 的结构性成因）。
 *
 * 成因链（源码事实）：
 *   ink@5.2.1 reconciler.js: resetAfterCommit → rootNode.onRender()
 *   onRender = throttle(onRender, 32, {leading:true, trailing:true})
 *   → leading 边**同步**触发，此时 React 尚未跑 layout effect（App 的 setPark 在那里）。
 *   于是这一帧的**内容**是本次 commit 的，park 规格却是上一次 commit 的 → 光标错位一帧，
 *   最多偏 1~N 个字符；32ms 后 trailing 边再写一帧把它纠正回来。
 *
 * CC 修的就是这一点：scheduleRender = queueMicrotask(onRender)，让 onRender 在 layout
 * effect 之后跑，因此「内容与 park 规格永远同代」（src/ink/ink.tsx:212）。
 *
 * 判据：逐帧比较「该帧内容推出的 caret 列」与「紧跟它的 park 列」。不等即为 stale。
 *
 * 运行： cd frontends/ink/ui && npx tsx probes/park_stale_race.ts
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

const stripAnsi = (t: string) => t
  .replace(/\u001B\][^\u0007]*(?:\u0007|\u001B\\)/g, '')
  .replace(/\u001B\[[0-?]*[ -/]*[@-~]/g, '')
  .replace(/\u001B[=>]/g, '')

type Frame = {
  /** 正文推出的期望 park 列（1-based） */
  expectCol: number
  /** 正文里输入文本 */
  text: string
  /** 实际 park 列；null = 本帧没 park */
  parkCol: number | null
  /** 本帧写出时刻 */
  at: number
  bytes: number
}

async function main() {
  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
  const stdin = new FakeReadStream()
  const park = createCursorParkStdout(stdout as never)

  const frames: Frame[] = []
  let open: Frame | null = null

  const baseWrite = stdout.write.bind(stdout)
  ;(stdout as unknown as { write: (c: unknown) => boolean }).write = (chunk: unknown) => {
    const s = String(chunk)
    if (s.includes('\u001b[2K')) {
      const idx = s.lastIndexOf('\u001b[0G')
      const body = idx >= 0 ? s.slice(idx + 4) : s
      const lines = stripAnsi(body).split(NL)
      let at = -1
      for (let i = lines.length - 1; i >= 0; i -= 1) {
        const raw = lines[i]!
        const gt = raw.indexOf('>')
        if (gt >= 0) { at = i; break }
      }
      if (at >= 0) {
        const raw = lines[at]!
        const gt = raw.indexOf('>')
        // `>` 之后恒有 1 个 gutter 空格，然后是输入文本（可能为空）。
        // 必须跳过这个 gutter 空格再 trimEnd，否则空输入时会把 gutter 也 trim 掉，
        // 算出比真实 caret 少 1 的列 —— 那是探针的错，不是产品的错。
        const text = raw.slice(gt + 2).replace(/\s+$/, '')
        const expectCol = gt + 3 + text.length
        open = { expectCol, text, parkCol: null, at: performance.now(), bytes: s.length }
        frames.push(open)
      } else {
        open = null
      }
    } else if (s.includes('\u001b[?25h') && open && open.parkCol === null) {
      const col = /\u001b\[(\d+)G/.exec(s)
      open.parkCol = col ? Number(col[1]) : null
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

  try {
    await delay(400)
    e()({ type: 'status', status: 'running', taskId: 1 })
    e()({ type: 'user', taskId: 1, text: 'go' })
    await delay(150)

    // 高频连打：间隔 5ms，一定会落在同一个 32ms 节流窗口内 → 暴露 leading 边用旧规格。
    for (let round = 0; round < 12; round += 1) {
      for (const ch of 'abcde'.split('')) { stdin.send(ch); await delay(5) }
      for (let i = 0; i < 5; i += 1) { stdin.send('\u007f'); await delay(5) }
      await delay(70)
    }
    await delay(300)

    const parked = frames.filter(f => f.parkCol !== null)
    const stale = parked.filter(f => f.parkCol !== f.expectCol)
    const ahead = stale.filter(f => f.parkCol! > f.expectCol)
    const behind = stale.filter(f => f.parkCol! < f.expectCol)
    const maxOff = stale.reduce((a, f) => Math.max(a, Math.abs(f.parkCol! - f.expectCol)), 0)

    console.log(`\n=== park 规格陈旧（内容与 park 不同代）===`)
    console.log(`  写出的帧 = ${frames.length}   带 park 的帧 = ${parked.length}`)
    console.log(`  stale 帧 = ${stale.length}  (${((stale.length / Math.max(1, parked.length)) * 100).toFixed(1)}%)`)
    console.log(`    光标超前 = ${ahead.length}   光标落后 = ${behind.length}   最大偏移 = ${maxOff} 列`)
    for (const f of stale.slice(0, 10)) {
      console.log(`   text=${JSON.stringify(f.text)} expect=${f.expectCol} park=${f.parkCol} 偏移=${f.parkCol! - f.expectCol > 0 ? '+' : ''}${f.parkCol! - f.expectCol}`)
    }

    // 同代帧的节流间隔分布
    const gaps: number[] = []
    for (let i = 1; i < frames.length; i += 1) gaps.push(frames[i]!.at - frames[i - 1]!.at)
    gaps.sort((a, b) => a - b)
    const q = (p: number) => Math.round(gaps[Math.floor(gaps.length * p)] ?? 0)
    console.log(`\n=== 帧间隔 ===`)
    console.log(`  n=${gaps.length}  p10=${q(0.1)}ms  p50=${q(0.5)}ms  p90=${q(0.9)}ms  max=${Math.round(gaps.at(-1) ?? 0)}ms`)
    console.log(`  <20ms 的帧对 = ${gaps.filter(g => g < 20).length}（同一窗口内 leading+trailing 双写 → 光标被移动两次）`)
  } finally {
    instance.unmount()
    await delay(150)
  }
}

main().catch(err => { console.error(err); process.exit(1) })
