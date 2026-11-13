/**
 * 探针：setPark 与「帧写入」的先后顺序 —— 判定光标滞后一列的根因。
 *
 * 若某次帧写入发生时，最近一次 setPark 来自**更早的 commit**，则该帧的 park 必然陈旧。
 * 这里给两个事件都打上单调时钟，按时间轴排一次，直接看谁在前。
 *
 * 对照事实（CC 源码 src/ink/ink.tsx:203-216）：
 *   CC 的 scheduleRender = queueMicrotask(onRender)，注释明说「deferring to a microtask
 *   runs onRender AFTER layout effects commit, so the native cursor tracks the caret
 *   without a one-keystroke lag」。GA 用 npm ink@5.2.1，onRender 由 resetAfterCommit
 *   同步触发（leading edge of throttle），**早于** layout effect 里的 setPark。
 *
 * 运行： cd frontends/ink/ui && npx tsx probes/park_ordering.ts
 */
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from '../src/App.js'
import { createCursorParkStdout } from '../src/stdoutCursorPark.js'
import type { BridgeClient } from '../src/bridgeClient.js'
import type { BridgeEvent } from '../src/protocol.js'

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

type Ev =
  | { t: number; kind: 'setPark'; col: number }
  | { t: number; kind: 'frame'; expectCol: number; text: string }
  | { t: number; kind: 'park'; col: number }

async function main() {
  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
  const stdin = new FakeReadStream()
  const park = createCursorParkStdout(stdout as never)

  const events: Ev[] = []
  // 记录 setPark 调用时刻与列号。
  const realSetPark = park.setPark.bind(park)
  let lastSpec: { up: number; col: number } | null = null
  park.setPark = (spec) => {
    lastSpec = spec
    if (spec) events.push({ t: performance.now(), kind: 'setPark', col: spec.col })
    realSetPark(spec)
  }

  let openFrame: Extract<Ev, { kind: 'frame' }> | null = null
  const baseWrite = stdout.write.bind(stdout)
  ;(stdout as unknown as { write: (c: unknown) => boolean }).write = (chunk: unknown) => {
    const s = String(chunk)
    if (s.includes('\u001b[2K')) {
      const idx = s.lastIndexOf('\u001b[0G')
      const body = idx >= 0 ? s.slice(idx + 4) : s
      const lines = stripAnsi(body).split('\n')
      let at = -1
      for (let i = lines.length - 1; i >= 0; i -= 1) {
        if (lines[i]!.indexOf('>') >= 0) { at = i; break }
      }
      if (at >= 0) {
        const raw = lines[at]!
        const gt = raw.indexOf('>')
        const text = raw.slice(gt + 1).replace(/\s+$/, '')
        const ev: Ev = { t: performance.now(), kind: 'frame', expectCol: gt + 1 + text.length + 1, text }
        events.push(ev)
        openFrame = ev
      } else {
        openFrame = null
      }
    } else if (s.includes('\u001b[?25h') && openFrame) {
      const col = /\u001b\[(\d+)G/.exec(s)
      events.push({ t: performance.now(), kind: 'park', col: col ? Number(col[1]) : -1 })
      openFrame = null
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
  void lastSpec

  try {
    await delay(400)
    const e = () => emit as unknown as (ev: BridgeEvent) => void
    e()({ type: 'status', status: 'running', taskId: 1 })

    const mark = events.length
    for (const ch of 'abc'.split('')) { stdin.send(ch); await delay(5) }
    for (let i = 0; i < 3; i += 1) { stdin.send('\u007f'); await delay(5) }
    await delay(400)

    const window = events.slice(mark)
    // 时间轴前 12 条，按时间排序后标注
    console.log(`\n=== 时间轴（前 14 条，按写入顺序）===`)
    for (const ev of window.slice(0, 14)) {
      const label = ev.kind === 'setPark' ? `setPark  col=${ev.col}` : ev.kind === 'frame' ? `frame    expectCol=${ev.expectCol} text=${JSON.stringify(ev.text)}` : `park     col=${ev.col}`
      console.log(`  ${ev.t.toFixed(2).padStart(10)}  ${label}`)
    }

    // 逐帧判定：该帧之前最近的一次 setPark，是否在本帧之后（= 陈旧）
    let stale = 0
    let fresh = 0
    for (let i = 0; i < window.length; i += 1) {
      const f = window[i]!
      if (f.kind !== 'frame') continue
      const prevSet = [...window.slice(0, i)].reverse().find(x => x.kind === 'setPark') as Extract<Ev, { kind: 'setPark' }> | undefined
      const nextSet = window.slice(i + 1).find(x => x.kind === 'setPark') as Extract<Ev, { kind: 'setPark' }> | undefined
      const distPrev = prevSet ? f.t - prevSet.t : Infinity
      const distNext = nextSet ? nextSet.t - f.t : Infinity
      if (distNext < distPrev) stale += 1
      else fresh += 1
    }
    console.log(`\n=== 帧与 setPark 的代次关系 ===`)
    console.log(`  帧写入更接近「上一次 setPark」（陈旧）= ${stale}`)
    console.log(`  帧写入更接近「下一次 setPark」（同代）= ${fresh}`)
    console.log(`\n结论：若陈旧数 > 0，说明部分帧在 layout effect 之前就被写出，`)
    console.log(`      这些帧的 park 规格来自上一次 commit → 光标滞后/超前，随后被 trailing 帧纠正。`)
  } finally {
    instance.unmount()
    await delay(150)
  }
}

main().catch(err => { console.error(err); process.exit(1) })
