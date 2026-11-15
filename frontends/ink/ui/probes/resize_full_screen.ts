/**
 * 探针：**内容撑满终端**时的 resize（用户报「从小放大内容卡住不显示」）。
 *
 * 手动跑：`npx tsx probes/resize_full_screen.ts [轮数]`（不参与 npm test）
 *
 * ## 为什么需要它（前一版探针为什么没复现）
 *
 * `probes/resize_directions.ts` 测了四个方向全部正常 —— 但它的内容只有 11 行，
 * 终端有 24/50 行，**从来没接近视口高度**。而这道题的全部难点恰好在那条边界上：
 *
 *   - `lineDiff.ts:132`：`next.length + 1 >= terminalRows` 时**必须**退化为全量重写。
 *     原因写在源码里：帧高触顶时全量重写末尾的换行会触底滚动，而 diff 只重写变化的行、
 *     不写空行、不滚动 → 两路屏幕逐渐错开。
 *   - `stdoutCursorPark.ts:127`：`terminalRows` 是 **readonly**，构造时从 `base.rows`
 *     取一次。终端尺寸一变，它仍是旧值。
 *
 * 于是有两种可疑组合：
 *   - 终端**变小**、`terminalRows` 仍是大值 → diff 被允许，但视口装不下帧 → 不滚动 vs 该滚动
 *   - 终端**变大**、`terminalRows` 仍是小值 → diff 被过度禁止（安全，但慢）
 *
 * 真机上用户是在一个有大量历史的会话里拖窗口的，所以必须用撑满屏幕的内容来测。
 *
 * ## 判据
 *
 * 每一轮在 resize 之后持续采样，检查：
 *   1. 屏幕是否**卡死在空白**（一条内容都没有）—— 用户说的「卡住不显示」
 *   2. 内容是否恢复，以及恢复用了多久
 *   3. 历史/流式探针是否还在（不丢、不重复）
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
  { name: '小→大 (80x24 → 140x50)', from: { columns: 80, rows: 24 }, to: { columns: 140, rows: 50 } },
  { name: '只增行 (80x24 → 80x50)', from: { columns: 80, rows: 24 }, to: { columns: 80, rows: 50 } },
  { name: '只增列 (80x24 → 140x24)', from: { columns: 80, rows: 24 }, to: { columns: 140, rows: 24 } },
  { name: '大→小 (140x50 → 80x24)', from: { columns: 140, rows: 50 }, to: { columns: 80, rows: 24 } },
]

/** 撑满视口所需的历史消息条数：每条 1 行，足够超出最大视口(50)。 */
const HISTORY_MESSAGES = 60

type Outcome = {
  /** resize 之后屏幕连续为空白的**最长**毫秒数。 */
  longestBlankMs: number
  /** resize 后内容首次回来耗时；始终空白 = 'never'。 */
  contentBackAt: number | 'never'
  /** 最终屏幕非空行数。 */
  nonEmptyRows: number
  /** 最终屏幕上是否出现了流式/提示等活内容。 */
  hasLiveChrome: boolean
  /** 历史上是否存在重复（同一行出现多次）。 */
  duplicates: number
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
    for (let i = 0; i < 300 && !readyEmitted; i += 1) await delay(10)
    await delay(80)
    const sink = emit as unknown as (event: BridgeEvent) => void

    // 建立**撑满视口**的历史：每条一轮，共 HISTORY_MESSAGES 条。
    for (let index = 1; index <= HISTORY_MESSAGES; index += 1) {
      sink({ type: 'status', status: 'running', taskId: index })
      sink({ type: 'user', taskId: index, text: `HIST-${String(index).padStart(2, '0')}-ALPHA` })
      sink({ type: 'assistant_done', taskId: index, text: `HIST-${String(index).padStart(2, '0')}-BRAVO` })
      sink({ type: 'status', status: 'idle', taskId: index })
    }
    await delay(1200)

    // 再开一轮流式输出，让底部有活内容。
    sink({ type: 'status', status: 'running', taskId: 999 })
    sink({ type: 'user', taskId: 999, text: 'HIST-LIVE-CHARLIE' })
    for (let index = 0; index < 4; index += 1) {
      sink({ type: 'assistant_delta', taskId: 999, text: `STREAM-PROBE-${index}` + NL })
    }
    await delay(400)

    // 确认 resize 之前确实撑满了（否则这一轮不算数）。
    const before = stdout.term.screen()
    const beforeFilled = before.filter(line => line.trim().length > 0).length
    if (beforeFilled < Math.min(direction.from.rows - 2, 12)) {
      throw new Error(`前提不成立：resize 前只有 ${beforeFilled} 行有内容（视口 ${direction.from.rows}）`)
    }

    stdout.columns = direction.to.columns
    stdout.rows = direction.to.rows
    stderr.columns = direction.to.columns
    stderr.rows = direction.to.rows
    const started = Date.now()
    stdout.emit('resize')

    let longestBlankMs = 0
    let blankStartedAt: number | null = null
    let contentBackAt: number | 'never' = 'never'
    const observeUntil = started + 1500
    while (Date.now() < observeUntil) {
      const screen = stdout.term.screen()
      const nonEmpty = screen.filter(line => line.trim().length > 0).length
      if (nonEmpty === 0) {
        if (blankStartedAt === null) blankStartedAt = Date.now()
        longestBlankMs = Math.max(longestBlankMs, Date.now() - blankStartedAt)
      } else {
        blankStartedAt = null
        if (contentBackAt === 'never') contentBackAt = Date.now() - started
      }
      await delay(5)
    }

    const settled = stdout.term.screen()
    const nonEmptyRows = settled.filter(line => line.trim().length > 0).length
    const hasLiveChrome = settled.some(line => (
      line.includes('Running ·') || line.includes('STREAM-PROBE') || line.includes('CHARLIE')
    ))
    // 重复检测：完全相同的非空内容行出现多次即为重复。
    const seen = new Map<string, number>()
    for (const line of settled) {
      const trimmed = line.trim()
      if (!trimmed) continue
      seen.set(trimmed, (seen.get(trimmed) ?? 0) + 1)
    }
    const duplicates = [...seen.values()].filter(count => count > 1).length

    return { longestBlankMs, contentBackAt, nonEmptyRows, hasLiveChrome, duplicates }
  } finally {
    instance.unmount()
    await delay(30)
  }
}

async function main(): Promise<void> {
  const rounds = Number(process.argv[2] ?? 3)
  console.log(`resize 撑满屏幕探针（每方向 ${rounds} 轮，历史 ${HISTORY_MESSAGES} 条）`)
  console.log('重点看「最长空白时长」——那就是用户说的「卡住不显示」。\n')

  for (const direction of DIRECTIONS) {
    const outcomes: Outcome[] = []
    for (let round = 0; round < rounds; round += 1) {
      try {
        outcomes.push(await runDirection(direction))
      } catch (error) {
        console.log(`  ${direction.name} 轮${round} 抛错: ${(error as Error).message}`)
      }
    }
    if (outcomes.length === 0) continue

    const worstBlank = Math.max(...outcomes.map(o => o.longestBlankMs))
    const blankedOut = outcomes.filter(o => o.contentBackAt === 'never').length
    console.log(`\n${direction.name}`)
    console.log(`  最长空白 ${worstBlank}ms   彻底卡死 ${blankedOut}/${outcomes.length}`)
    outcomes.forEach((o, index) => {
      const back = o.contentBackAt === 'never' ? '未恢复' : `t+${o.contentBackAt}ms`
      const verdict = o.contentBackAt === 'never' || o.longestBlankMs > 400 ? '✖' : '✔'
      console.log(
        `    轮${index}: ${verdict} 最长空白=${o.longestBlankMs}ms 恢复于=${back} `
        + `可见行=${o.nonEmptyRows} 活内容=${o.hasLiveChrome ? 'Y' : 'N'} 重复行=${o.duplicates}`,
      )
    })
  }
}

main().catch(error => {
  console.error(error)
  process.exit(1)
})
