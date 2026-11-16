/**
 * 探针：headless 下 park 是否真的发生？（App.setPark → writer.flush 的决策链）
 *
 * ⚠️ **本探针的结论与真机相反，保留作「headless ≠ 真机」的证据。**
 *
 * headless（CaptureWriteStream）下每帧都有 `ESC[4A ESC[7G` park 段，看起来完全正常；
 * 而同一份代码在真 PTY 上，序列也对、可见性却失效（整个会话只 SHOW 一次）。原因：
 * 2026-11-16 之后仓库默认 **fullscreen/alt-screen**（`mouseWheel.ts:112` 恒 true），
 * 而 ConPTY 在 BSU/ESU 事务边界会重置 DECTCEM —— 这一层 headless 永远看不到。
 *
 * 所以在光标可见性问题上，**headless 探针给不出结论**，权威判据是
 * `tests/probes/python/_probe_park_config_matrix.py`（真 PTY）。本探针的价值只在于
 * 「App 的 spec 计算与 writer 的 park 段拼接是正确的」这一层。
 *
 * 测什么
 * ------
 * 用真实 App + 真实 `createCursorParkStdout`，接管 sink 记录每次 flush 时：
 *   - 实际写出的字节里有没有 `CSI nA` + `CSI mG` + `?25h`（park 段）
 *   - `?25l` / `?25h` 各出现几次
 *
 * 为什么必须保留
 * --------------
 * 它是「park 几何正确」这一层的稳定回归点；同时是「headless 绿 ≠ 真机绿」的活证据。
 *
 * 运行：cd frontends/ink/ui && npx tsx probes/park_never_fires.ts
 *      （PROBE_FULLSCREEN=off 可切回 inline 对比）
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
  columns: number
  rows: number
  isTTY = true
  chunks: string[] = []
  constructor(columns: number, rows: number) { super(); this.columns = columns; this.rows = rows }
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

type Rec = {
  tag: string
  bytes: number
  hasSpec: boolean
  hasPark: boolean
  hasShow: boolean
  hasHide: boolean
  parkedUp: number | null
  cursorShown: boolean
  tail: string
}

async function main() {
  process.env.GA_INK_FULLSCREEN = process.env.PROBE_FULLSCREEN ?? 'on'
  const COLUMNS = 120
  const ROWS = 40
  const base = new CaptureWriteStream(COLUMNS, ROWS)
  const stderr = new CaptureWriteStream(COLUMNS, ROWS)
  const stdin = new FakeReadStream()
  const park = createCursorParkStdout(base as never, { terminalRows: ROWS, syncOutput: true })

  const recs: Rec[] = []
  let tag = 'startup'

  // 真实 sink 在 writer 内部；这里通过包裹底层 base.write 观察最终字节。
  const baseWrite = base.write.bind(base)
  ;(base as unknown as { write: (c: unknown) => boolean }).write = (chunk: unknown) => {
    const s = String(chunk)
    // flush 一定以 BSU 开头（syncOutput 强制 true），据此记录一次
    recs.push({
      tag,
      bytes: s.length,
      // 下面两个由 flush 统计后回填
      hasSpec: false,
      hasPark: false,
      hasShow: /\[?25h/.test(s),
      hasHide: /\[?25l/.test(s),
      parkedUp: null,
      cursorShown: false,
      tail: s.slice(-160),
    })
    return baseWrite(chunk)
  }

  // 直接在 writer 层打点：createCursorParkStdout 内部 new 了 writer，
  // 拿不到实例，所以从 setPark 入口回填「本帧是否有 spec」。
  let lastSpec: { up: number; col: number } | null = null
  const realSetPark = park.setPark
  ;(park as { setPark: typeof realSetPark }).setPark = (spec) => {
    lastSpec = spec
    realSetPark(spec)
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

  const step = async (name: string, fn: () => Promise<void>) => {
    tag = name
    await fn()
    await delay(250)
  }

  try {
    await delay(500)
    await step('1 启动空闲', async () => {})
    await step('2 输入 abc', async () => {
      stdin.send('a'); await delay(80)
      stdin.send('b'); await delay(80)
      stdin.send('c')
    })
    await step('3 输入后空闲', async () => {})
    await step('4 运行中', async () => { e()({ type: 'status', status: 'running', taskId: 1 }) })
    await step('5 流式输出', async () => {
      for (let i = 0; i < 6; i += 1) {
        e()({ type: 'assistant_delta', taskId: 1, text: `x${i}` })
        await delay(40)
      }
    })
    await step('6 回合结束', async () => {
      e()({ type: 'assistant_done', taskId: 1, text: 'done' } as never)
      e()({ type: 'status', status: 'idle', taskId: 1 })
    })
    await step('7 结束空闲', async () => {})
  } finally {
    instance.unmount()
    await delay(200)
  }

  // 回填并统计：一次「帧」= 一次 base.write（原子提交保证一突发一次写）
  console.log(`\n终端 ${COLUMNS}x${ROWS}  写入次数=${recs.length}  最后一帧 spec=${JSON.stringify(lastSpec)}\n`)
  const withShow = recs.filter(r => r.hasShow).length
  console.log(`  含 ?25h（SHOW）的写入 = ${withShow} / ${recs.length}`)
  console.log(`  含 ?25l（HIDE）的写入 = ${recs.filter(r => r.hasHide).length} / ${recs.length}`)
  const last = recs.slice(-6)
  console.log('\n  最后 6 次写入（尾部 160 字节，转义可见）：')
  for (const r of last) {
    const parkSeg = /\u001b\[(\d+)A\u001b\[(\d+)G/.exec(r.tail)
    console.log(`   [${r.tag}] ${r.bytes}B  show=${r.hasShow} hide=${r.hasHide} park段=${parkSeg ? parkSeg[0].replace(/\u001b/g, 'ESC') : '无'}`)
    console.log(`      ${JSON.stringify(r.tail.slice(-90))}`)
  }
  const verdict = withShow > 0
  console.log(`\n${verdict ? '  OK  至少有一帧 park 到 caret' : '  NG  全程没有任何 SHOW/park —— 原生光标停在帧底，输入法必然锚在屏幕右下角'}`)
}

main().catch(err => { console.error(err); process.exit(1) })
