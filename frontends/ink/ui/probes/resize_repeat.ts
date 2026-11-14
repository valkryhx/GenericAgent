/**
 * 探针：resize 之后历史能否恢复，**同一个延迟值**重复跑是否稳定？
 *
 * 手动跑：`npx tsx probes/resize_repeat.ts [延迟ms] [轮数]`（不参与 npm test）
 *
 * ## 为什么要这个探针
 *
 * `resize_delay_sweep.ts` 第一次跑出 `32/48/80/120` 绿、`0/16/64/96` 红，
 * 第二次跑出 `0/16/48/120` 绿、`32/64/80/96` 红 —— 完全不重叠。这种结果只可能意味着
 * 两件事之一：
 *
 *   (a) 探针自身的判定不稳定（采样窗口 / 观察时长不够）；
 *   (b) resize 之后历史能否恢复，**本身就是非确定的**。
 *
 * 两者的后果完全不同：(a) 只需修探针；(b) 说明连 120ms 都不安全，
 * `resizeMidStream.test.ts` 一直是靠运气过的。所以必须先把这个分开。
 *
 * ## 判据
 *
 * 固定一个延迟值跑 N 轮，看 CHARLIE 最终是否恰好出现 1 次：
 * - 全绿 → 该延迟值稳定，扫描的抖动来自探针
 * - 有红有绿 → 这个行为本身非确定，比"选多大的延迟"更严重
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
  charlie: number
  alpha: number
  bravo: number
  /** CHARLIE 首次回到屏幕的毫秒数（相对 resize）；Never 表示没回来。 */
  backAt: number | 'never'
  /** 观察结束时屏幕上可见的行数（判断是不是被滚出视口）。 */
  visibleRows: number
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
      charlie: count('HIST-PROBE-CHARLIE'),
      alpha: count('HIST-PROBE-ALPHA'),
      bravo: count('HIST-PROBE-BRAVO'),
      backAt,
      visibleRows: settled.filter(line => line.trim().length > 0).length,
    }
  } finally {
    instance.unmount()
    await delay(30)
  }
}

async function main(): Promise<void> {
  const delayMs = Number(process.argv[2] ?? 120)
  const rounds = Number(process.argv[3] ?? 8)
  // 观察窗口给足：重挂 + ink 再写一帧 + 余量。
  const observeMs = Math.max(800, delayMs + 600)

  console.log(`resize 历史恢复稳定性：delay=${delayMs}ms，${rounds} 轮，每轮观察 ${observeMs}ms\n`)
  console.log('  轮次   CHARLIE/ALPHA/BRAVO   CHARLIE 回来于   可见行数')

  const results: Outcome[] = []
  for (let index = 0; index < rounds; index += 1) {
    const outcome = await oneRound(delayMs, observeMs)
    results.push(outcome)
    const counts = `${outcome.charlie}/${outcome.alpha}/${outcome.bravo}`
    const back = outcome.backAt === 'never' ? '未回来' : `t+${outcome.backAt}ms`
    console.log(`  ${String(index).padStart(3)}   ${counts.padEnd(20)}   ${back.padEnd(16)}  ${outcome.visibleRows}`)
  }

  const good = results.filter(r => r.charlie === 1 && r.alpha === 1 && r.bravo === 1).length
  const never = results.filter(r => r.backAt === 'never').length
  console.log(`\n结论：${good}/${rounds} 轮最终状态正确（三个探针各 1 次），${never} 轮 CHARLIE 从未回来`)
  if (good === rounds) {
    console.log(`  ✔ delay=${delayMs}ms 稳定。扫描时的抖动来自探针的采样窗口，不是这个延迟值。`)
  } else if (good === 0) {
    console.log(`  ✖ delay=${delayMs}ms 稳定失败。这不是抖动，是该延迟值真的不够。`)
  } else {
    console.log('  ✖ 同一延迟值时红时绿 —— 历史能否恢复本身是非确定的。')
    console.log('    这比「选多大的延迟」严重：连默认值都不安全，需查 ink 尾帧与重挂的耦合。')
  }
}

main().catch(error => {
  console.error(error)
  process.exit(1)
})
