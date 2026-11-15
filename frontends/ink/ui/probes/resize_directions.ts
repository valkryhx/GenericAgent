/**
 * 探针：resize 的**四种方向**分别会发生什么（用户报「从小放大时内容卡住不显示」）。
 *
 * 手动跑：`npx tsx probes/resize_directions.ts [轮数]`（不参与 npm test）
 *
 * ## 为什么需要它
 *
 * 之前所有 resize 探针（`resize_blank_window` / `resize_delay_sweep` /
 * `resize_repeat`）都**只改 columns、从不改 rows**。用户报的是「终端大小缩放」，
 * 尤其是「从小缩放到大」，那同时牵涉 rows 与 columns，而且方向相反。
 *
 * 已知的可疑点（本探针要验证，不是结论）：
 *
 * 1. `App.tsx` 的 resize 处理里有一行 `if (previous.columns === next.columns) return`
 *    —— 只改 rows 时整屏重置与重挂都不会发生。
 * 2. `cursorPark` 的 `terminalRows` 是 `readonly`，构造时取一次（`stdoutCursorPark.ts:127`）。
 *    行数变化后它仍是旧值，`buildLineDiff` 的「帧高触顶」判断随之失真
 *    （`lineDiff.ts:132`：`next.length + 1 >= terminalRows` 时退化为全量重写）。
 * 3. ink 的 `log-update` 用 `previousLineCount` 做相对擦除，行数变化后该值过期。
 *
 * ## 判据
 *
 * 每个方向跑 N 轮，每一轮检查 resize 之后屏幕是否**恢复正常**：
 *   - 历史探针（HIST-PROBE-ALPHA / BRAVO / CHARLIE）各出现恰好 1 次
 *   - 流式行（STREAM-PROBE-*）仍在
 *   - 记录从 resize 到「屏幕重新有内容」的毫秒数；若始终没有内容 → 卡死
 *
 * 这是「内容被卡住不显示」的直接量化，而不是只看某一行有没有丢。
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

type Direction = {
  name: string
  from: { columns: number; rows: number }
  to: { columns: number; rows: number }
}

const DIRECTIONS: Direction[] = [
  // 用户最在意的：从小放大
  { name: '小→大 (80x24 → 140x50)', from: { columns: 80, rows: 24 }, to: { columns: 140, rows: 50 } },
  // 只放大行（有些终端先改行后改列）
  { name: '只增行 (80x24 → 80x50)', from: { columns: 80, rows: 24 }, to: { columns: 80, rows: 50 } },
  // 只增列
  { name: '只增列 (80x24 → 140x24)', from: { columns: 80, rows: 24 }, to: { columns: 140, rows: 24 } },
  // 对照组：大→小
  { name: '大→小 (140x50 → 80x24)', from: { columns: 140, rows: 50 }, to: { columns: 80, rows: 24 } },
]

type Outcome = {
  /** 三个历史探针在最终屏幕上的出现次数。 */
  hist: { alpha: number; bravo: number; charlie: number }
  /** 流式行在最终屏幕上出现几次。 */
  stream: number
  /** resize 之后屏幕重新出现任意内容行所需的毫秒数；始终空白 = 'never'。 */
  contentBackAt: number | 'never'
  /** 最终屏幕上非空行数。 */
  nonEmptyRows: number
  /** 是否完全卡死（屏幕上一条内容都没有）。 */
  blanked: boolean
}

async function runDirection(direction: Direction): Promise<Outcome> {
  process.env.GA_INK_FULLSCREEN = 'off'
  let emit: ((event: BridgeEvent) => void) | null = null
  let readyEmitted = false
  const startBridgeClient = (
    _python: string,
    _bridgeScript: string,
    onEvent: (event: BridgeEvent) => void,
  ): BridgeClient => {
    emit = onEvent
    setTimeout(() => {
      onEvent({ type: 'ready', version: 1 })
      readyEmitted = true
    }, 0)
    return { send() {}, stop() {} }
  }

  const stdout = new ReflowStream(direction.from.columns, direction.from.rows)
  const stderr = new ReflowStream(direction.from.columns, direction.from.rows)
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
    // 等 ready 真正落地（见 probes/ready_race.ts）。
    for (let i = 0; i < 300 && !readyEmitted; i += 1) await delay(10)
    await delay(80)
    const sink = emit as unknown as (event: BridgeEvent) => void

    // 建立历史
    sink({ type: 'status', status: 'running', taskId: 1 })
    sink({ type: 'user', taskId: 1, text: 'HIST-PROBE-ALPHA' })
    sink({ type: 'assistant_done', taskId: 1, text: 'HIST-PROBE-BRAVO' })
    sink({ type: 'status', status: 'idle', taskId: 1 })
    await delay(300)

    // 开一轮流式输出
    sink({ type: 'status', status: 'running', taskId: 2 })
    sink({ type: 'user', taskId: 2, text: 'HIST-PROBE-CHARLIE' })
    for (let index = 0; index < 3; index += 1) {
      sink({ type: 'assistant_delta', taskId: 2, text: `STREAM-PROBE-${index}` + NL })
    }
    await delay(250)

    // 改变尺寸并触发 resize —— 与真机一致：先设新尺寸，再 emit。
    stdout.columns = direction.to.columns
    stdout.rows = direction.to.rows
    stderr.columns = direction.to.columns
    stderr.rows = direction.to.rows
    const started = Date.now()
    stdout.emit('resize')

    // 观察窗口要盖住 120ms 重挂延迟之后的落地，给到 800ms。
    let contentBackAt: number | 'never' = 'never'
    const observeUntil = started + 800
    while (Date.now() < observeUntil) {
      const screen = stdout.term.screen()
      const hasContent = screen.some(line => line.trim().length > 0
        && (line.includes('HIST-PROBE') || line.includes('STREAM-PROBE')
          || line.includes('GenericAgent') || line.includes('Running ·')))
      if (contentBackAt === 'never' && hasContent) contentBackAt = Date.now() - started
      await delay(5)
    }

    const settled = stdout.term.screen()
    const count = (probe: string) => settled.filter(line => line.includes(probe)).length
    const nonEmptyRows = settled.filter(line => line.trim().length > 0).length
    return {
      hist: {
        alpha: count('HIST-PROBE-ALPHA'),
        bravo: count('HIST-PROBE-BRAVO'),
        charlie: count('HIST-PROBE-CHARLIE'),
      },
      stream: count('STREAM-PROBE'),
      contentBackAt,
      nonEmptyRows,
      blanked: nonEmptyRows === 0,
    }
  } finally {
    instance.unmount()
    await delay(30)
  }
}

async function main(): Promise<void> {
  const rounds = Number(process.argv[2] ?? 4)
  console.log(`resize 四方向探针（每方向 ${rounds} 轮）`)
  console.log('判据：历史三行各 1 次、流式行仍在、resize 后屏幕能恢复内容。\n')

  for (const direction of DIRECTIONS) {
    const outcomes: Outcome[] = []
    for (let round = 0; round < rounds; round += 1) {
      outcomes.push(await runDirection(direction))
    }
    const okCount = outcomes.filter(o => (
      o.hist.alpha === 1 && o.hist.bravo === 1 && o.hist.charlie === 1
      && o.stream > 0 && !o.blanked
    )).length
    const blankCount = outcomes.filter(o => o.blanked).length
    console.log(`\n${direction.name}`)
    console.log(`  正常 ${okCount}/${rounds}   完全空白 ${blankCount}/${rounds}`)
    outcomes.forEach((o, index) => {
      const back = o.contentBackAt === 'never' ? '未恢复' : `t+${o.contentBackAt}ms`
      const verdict = (o.hist.alpha === 1 && o.hist.bravo === 1 && o.hist.charlie === 1
        && o.stream > 0 && !o.blanked) ? '✔' : '✖'
      console.log(
        `    轮${index}: ${verdict} 历史=${o.hist.charlie}/${o.hist.alpha}/${o.hist.bravo} `
        + `流式=${o.stream} 内容恢复于=${back} 可见行=${o.nonEmptyRows}`,
      )
    })
  }
}

main().catch(error => {
  console.error(error)
  process.exit(1)
})
