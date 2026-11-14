/**
 * 探针：`RESIZE_REMOUNT_DELAY_MS` 的**安全下限**在哪里。
 *
 * 手动跑：`npx tsx probes/resize_delay_sweep.ts [每档轮数] [延迟列表]`（不参与 npm test）
 *
 * ## 它测什么
 *
 * `App.tsx` 把 `<Static>` 重挂推迟 120ms，为了等 ink 的节流尾帧（32ms 窗口）先落地。
 * 但 120ms 是"留足余量"的经验值，不是测出来的。这个探针扫一遍延迟值，找出
 * **保正确性的最小延迟** —— 也就是空白窗口还能压缩多少。
 *
 * 判据：最终屏幕里 CHARLIE / ALPHA / BRAVO 各出现**恰好一次**（不丢、不重复）。
 * 只满足"最终对"是不够的；历史上把延迟改成 0 也能在最终屏幕上碰巧对，
 * 但中间态会闪。
 *
 * ## 关键教训：每一档必须跑多轮
 *
 * 第一版每档只跑一次，两次运行的结果完全不重叠（`32/48/80/120` 绿 vs
 * `0/16/48/120` 绿）。原因不是延迟值，而是**这个行为在小的延迟值上本身是非确定的**：
 *
 * | 延迟 | 结果 |
 * | --- | --- |
 * | 120ms | 12/12 绿 |
 * | 48ms  | 7/10 绿 |
 * | 64ms  | 4/10 绿 |
 *
 * 单次采样一个随机行为，得到的是垃圾结论。所以这里每档跑 N 轮，报**通过率**。
 *
 * ## 更重要的结论：这个扫描**测不出安全下限**
 *
 * 通过率对延迟值**不单调**（48ms 比 64ms 好，64ms 又比 32ms 差）。如果延迟绝对值是
 * 关键变量，结果应该单调递增。不单调说明真正的变量是**两次写入的相对顺序**，
 * 延迟只是间接影响它。机制由 `resize_write_timeline.ts` 取到了：
 *
 * - 失败轮：含 static 的帧（历史写出）之后，又来一次 `eraseLines(10)` + 只写 live 的帧
 *   → 历史被擦掉且不再重画。
 * - 通过轮：陈旧 tail 帧先落地，含 static 的重挂帧**之后**才发生 → 安全。
 *
 * 所以这张表的正确读法是「**哪些值实测稳定**」，不是「最小的安全值是多少」。
 * 结论只有一条：**小于 120ms 没有可用的值**，要缩短空白窗口必须换机制
 * （拦截/接管陈旧帧写入），不能靠调这个常量。
 *
 * ## 为什么值得留
 *
 * `resize_blank_window.ts` 量出当前空白窗口约 140ms。这个扫描证明这 140ms
 * 不能靠调小延迟来压缩 —— 它是将来任何人想改这个常量时的第一道证据。
 *
 * ## 已知的替代思路（不是结论）
 *
 * `ink.js` 把 `throttledLog = throttle(this.log, undefined, {trailing:true})` 存在
 * 实例内部，**没有暴露**。所以无法从外部 `cancel()` 那个陈旧尾帧，只能：
 *   (a) 自己接管 stdout 写入、识别并拦掉陈旧帧（需要能区分"这是 resize 前的 output"）
 *   (b) fork / patch ink 的 throttle
 * 两条都比"调小延迟"重得多，所以先把延迟扫清楚。
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

type Outcome = {
  /** 三个探针在最终屏幕上各出现几次。 */
  counts: { charlie: number; alpha: number; bravo: number }
  /** 从 resize 起，CHARLIE 首次回到屏幕的毫秒数；没回来就是 'never'。 */
  backAt: number | 'never'
}

async function oneRound(delayMs: number, observeMs: number): Promise<Outcome> {
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
    resizeRemountDelayMs: delayMs,
  }), {
    ...inkRenderOptions,
    stdout: cursorPark.stdout,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: new FakeStdin() as unknown as NodeJS.ReadStream,
    patchConsole: false,
  })

  try {
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

    stdout.columns = 60
    stderr.columns = 60
    const started = Date.now()
    stdout.emit('resize')

    let backAt: number | 'never' = 'never'
    while (Date.now() - started < observeMs) {
      const screen = stdout.term.screen()
      if (backAt === 'never' && screen.some(line => line.includes('HIST-PROBE-CHARLIE'))) {
        backAt = Date.now() - started
      }
      await delay(5)
    }

    const settled = stdout.term.screen()
    const count = (probe: string) => settled.filter(line => line.includes(probe)).length
    return {
      counts: {
        charlie: count('HIST-PROBE-CHARLIE'),
        alpha: count('HIST-PROBE-ALPHA'),
        bravo: count('HIST-PROBE-BRAVO'),
      },
      backAt,
    }
  } finally {
    instance.unmount()
    await delay(30)
  }
}

async function main(): Promise<void> {
  const rounds = Number(process.argv[2] ?? 6)
  const settings = process.argv[3]
    ? process.argv[3].split(',').map(Number)
    : [0, 16, 32, 48, 64, 80, 96, 120]

  console.log(`RESIZE_REMOUNT_DELAY_MS 扫描（每档 ${rounds} 轮；判据：三个探针各恰好 1 次）`)
  console.log('通过率 < 100% 即视为不安全 —— 用户看到的是闪，不是平均值。\n')
  console.log('  延迟     通过率   中位回来时长   空白窗口')

  const summary: Array<{ delayMs: number; passRate: number }> = []
  for (const delayMs of settings) {
    const observeMs = Math.max(800, delayMs + 600)
    const outcomes: Outcome[] = []
    for (let index = 0; index < rounds; index += 1) {
      outcomes.push(await oneRound(delayMs, observeMs))
    }

    const good = outcomes.filter(outcome => (
      outcome.counts.charlie === 1 && outcome.counts.alpha === 1 && outcome.counts.bravo === 1
    )).length
    const returns = outcomes
      .map(outcome => outcome.backAt)
      .filter((value): value is number => typeof value === 'number')
      .sort((a, b) => a - b)
    const median = returns.length > 0 ? returns[Math.floor(returns.length / 2)] : null
    const passRate = good / rounds
    summary.push({ delayMs, passRate })

    console.log(
      `  ${String(delayMs).padStart(3)}ms   ${`${good}/${rounds}`.padStart(6)}   `
      + `${(median === null ? '未回来' : `t+${median}ms`).padStart(12)}   `
      + `${(median === null ? '—' : `${median}ms`).padStart(8)}`,
    )
  }

  const safe = summary.filter(entry => entry.passRate === 1).map(entry => entry.delayMs)
  const unsafe = summary.filter(entry => entry.passRate < 1).map(entry => entry.delayMs)
  console.log('\n结论：')
  console.log(`  本轮全绿：${safe.length > 0 ? safe.map(v => `${v}ms`).join(', ') : '（无）'}`)
  console.log(`  本轮出现过失败：${unsafe.length > 0 ? unsafe.map(v => `${v}ms`).join(', ') : '（无）'}`)
  if (unsafe.length > 0) {
    console.log('\n  ⚠ 通过率对延迟值不单调 —— 不要从这张表里读「最小安全延迟」。')
    console.log('  真实变量是「含 static 的重挂帧」与「只写 live 的陈旧帧」谁后发生，')
    console.log('  延迟只间接影响它。机制见 probes/resize_write_timeline.ts：')
    console.log('    · 失败轮：历史写出后，又有一条 eraseLines(n) + 只写 live 的帧把它擦掉')
    console.log('    · 通过轮：陈旧帧先落地，含 static 的重挂帧之后才发生')
    console.log('\n  可操作的结论只有一条：小于 120ms 没有可靠的值。要缩短那 ~140ms')
    console.log('  空白窗口，必须换机制（拦截/take over 陈旧帧的写入），不能靠调这个常量。')
  }
}

main().catch(error => {
  console.error(error)
  process.exit(1)
})
