/**
 * 探针：resize 之后那段「空白窗口」到底有多长。
 *
 * 手动跑：`npx tsx probes/resize_blank_window.ts [采样步长ms]`（不参与 npm test）
 *
 * ## 它测什么
 *
 * `App.tsx` 在宽度变化时做两件事：
 *
 * 1. `resetTranscriptViewport()` —— 立刻整屏重置（清屏 + 光标归位）
 * 2. `<Static>` 重挂 —— **推迟 120ms**
 *
 * 推迟是必须的：ink 把 `log` 包成 `throttle(log, 32, {leading:true, trailing:true})`，
 * resize 那一刻节流窗口里挂着一次**延迟执行的尾帧**，它捕获的 output 是 resize 之前的
 * （只有 live 区）。若立刻重挂，尾帧会按旧行数擦一遍、只重画 live，刚重发的历史行被
 * 擦掉且不再重画。实测 2026-10-10 那条 user 行就是这么消失的。
 *
 * 代价是：从重置到重挂落地之间，屏幕上**历史是空的**。这个探针量这段窗口的真实长度，
 * 用来判断 120ms 是不是过长（真机 60fps 下 120ms ≈ 7 帧，用户可能看得见闪一下）。
 *
 * ## 为什么值得留
 *
 * 这段窗口是「等尾帧」策略的直接代价，也是后续优化（拦截尾帧而非等它）唯一的
 * 验收指标。没有基线数字，"优化有效"就是空话。
 *
 * ## 已知的替代思路（留待验证，不是结论）
 *
 * `es-toolkit` 的 `throttle` 返回 `DebouncedFunc`，带 `cancel()` / `flush()`。
 * `ink.js` 把 `log` 包成 `throttle(this.log, undefined, {trailing:true})` 存为
 * `throttledLog` —— **但 ink 没有把它暴露出来**，`new Ink()` 实例上取不到。
 * 所以「resize 时主动 cancel 那个尾帧」只能靠：
 *   (a) 自己接管 stdout 写入，识别并拦掉那段陈旧帧；或
 *   (b) 换掉 ink 的 throttle（fork / patch）。
 * 两条都要先有这个基线数字，才知道值不值得做。
 */
import React from 'react'
import { render } from 'ink'
import { EventEmitter } from 'node:events'
import { App } from '../src/App.js'
import { inkRenderOptions } from '../src/inkRenderOptions.js'
import { createCursorParkStdout } from '../src/stdoutCursorPark.js'
import { ReflowTerminal } from '../src/resizeReflowModel.js'
import type { BridgeClient } from '../src/bridgeClient.js'
import type { BridgeEvent } from '../src/protocol.js'

const NL = String.fromCharCode(10)
const delay = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms))

class ReflowStream extends EventEmitter {
  readonly term: ReflowTerminal
  isTTY = true
  constructor(columns: number, rows: number) {
    super()
    this.term = new ReflowTerminal(columns, rows)
  }
  get columns(): number { return this.term.columns }
  set columns(value: number) { this.term.columns = Math.max(1, Math.floor(value)) }
  get rows(): number { return this.term.rows }
  set rows(value: number) { this.term.rows = Math.max(1, Math.floor(value)) }
  write(chunk: unknown): boolean {
    this.term.write(String(chunk))
    return true
  }
}

class FakeStdin extends EventEmitter {
  isTTY = true
  setRawMode() { return this }
  setEncoding() { return this }
  ref() { return this }
  unref() { return this }
  read() { return null }
  resume() { return this }
  pause() { return this }
}

type Sample = { at: number; charlie: number; alpha: number }

/**
 * 跑一轮：建立历史 → 开一轮流式输出 → 变窄 → 每 `step` ms 采样一次屏幕。
 * 返回从 resize 起的采样序列。
 */
async function measure(step: number, settle: number): Promise<Sample[]> {
  process.env.GA_INK_FULLSCREEN = 'off'
  let emit: ((event: BridgeEvent) => void) | null = null
  const startBridgeClient = (
    _python: string,
    _bridgeScript: string,
    onEvent: (event: BridgeEvent) => void,
  ): BridgeClient => {
    emit = onEvent
    setTimeout(() => onEvent({ type: 'ready', version: 1 }), 0)
    return { send() {}, stop() {} }
  }

  const stdout = new ReflowStream(80, 24)
  const stderr = new ReflowStream(80, 24)
  const cursorPark = createCursorParkStdout(stdout as unknown as NodeJS.WriteStream)
  const instance = render(React.createElement(App, {
    python: 'python',
    bridgeScript: 'bridge.py',
    startBridgeClient,
    cursorPark,
  }), {
    ...inkRenderOptions,
    stdout: cursorPark.stdout,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: new FakeStdin() as unknown as NodeJS.ReadStream,
    patchConsole: false,
  })

  try {
    // ready 落地再发事件（见 probes/ready_race.ts：晚到的 ready 会重置状态）。
    await delay(120)
    const sink = emit as unknown as (event: BridgeEvent) => void

    sink({ type: 'status', status: 'running', taskId: 1 })
    sink({ type: 'user', taskId: 1, text: 'HIST-PROBE-ALPHA' })
    sink({ type: 'assistant_done', taskId: 1, text: 'HIST-PROBE-BRAVO' })
    sink({ type: 'status', status: 'idle', taskId: 1 })
    await delay(300)

    sink({ type: 'status', status: 'running', taskId: 2 })
    sink({ type: 'user', taskId: 2, text: 'HIST-PROBE-CHARLIE' })
    for (let index = 0; index < 3; index += 1) {
      sink({ type: 'assistant_delta', taskId: 2, text: `STREAM-PROBE-${index}` + NL })
    }
    await delay(250)

    const before = stdout.term.screen()
    if (!before.some(line => line.includes('HIST-PROBE-CHARLIE'))) {
      throw new Error('前提不成立：resize 之前 CHARLIE 就已经不在屏幕上了')
    }

    // 真正开始采样。
    stdout.columns = 60
    stderr.columns = 60
    stdout.emit('resize')

    const samples: Sample[] = []
    const started = Date.now()
    while (Date.now() - started < settle) {
      const screen = stdout.term.screen()
      samples.push({
        at: Date.now() - started,
        charlie: screen.filter(line => line.includes('HIST-PROBE-CHARLIE')).length,
        alpha: screen.filter(line => line.includes('HIST-PROBE-ALPHA')).length,
      })
      await delay(step)
    }
    return samples
  } finally {
    instance.unmount()
    await delay(30)
  }
}

/** 从采样序列里取出「历史缺失」的连续区间。 */
function missingWindow(samples: Sample[]): { from: number; to: number } | null {
  const firstMissing = samples.find(sample => sample.charlie === 0)
  if (!firstMissing) return null
  const after = samples.filter(sample => sample.at >= firstMissing.at)
  const firstBack = after.find(sample => sample.charlie > 0)
  return { from: firstMissing.at, to: firstBack ? firstBack.at : Number.POSITIVE_INFINITY }
}

async function main(): Promise<void> {
  const step = Number(process.argv[2] ?? 5)
  const settle = Number(process.argv[3] ?? 600)
  console.log(`resize 空白窗口探针：采样步长 ${step}ms，观察 ${settle}ms\n`)

  const samples = await measure(step, settle)
  const window = missingWindow(samples)

  console.log('时间线（只打印历史行数发生变化的采样点）：')
  let previous = ''
  for (const sample of samples) {
    const key = `${sample.charlie}/${sample.alpha}`
    if (key === previous) continue
    previous = key
    console.log(`  t+${String(sample.at).padStart(3)}ms  CHARLIE=${sample.charlie} ALPHA=${sample.alpha}`)
  }

  const settled = samples.at(-1)
  console.log('\n结论：')
  if (window === null) {
    console.log('  ✔ 没有空白窗口：历史和流式行在 resize 全程都在屏幕上')
    console.log('     → 若真机仍看到闪，先怀疑渲染节流，而不是这段延迟')
  } else if (!Number.isFinite(window.to)) {
    console.log(`  ✖ 历史从 t+${window.from}ms 起就没回来（超出 ${settle}ms 观察窗）—— 这是 bug，不是代价`)
  } else {
    console.log(`  空白窗口 = t+${window.from}ms → t+${window.to}ms，共 ${window.to - window.from}ms`)
    console.log(`  （RESIZE_REMOUNT_DELAY_MS = 120；60fps 下 120ms ≈ 7 帧）`)
    console.log(`  最终状态：CHARLIE=${settled?.charlie} ALPHA=${settled?.alpha}（各应为 1）`)
  }
}

main().catch(error => {
  console.error(error)
  process.exit(1)
})
