/**
 * 探针：resize 之后的**写入时间线** —— 尾帧到底在什么时刻把历史擦掉的。
 *
 * 手动跑：`npx tsx probes/resize_write_timeline.ts [延迟ms] [轮数]`（不参与 npm test）
 *
 * ## 为什么要这个探针
 *
 * `resize_delay_sweep.ts` 的结果**不单调**：48ms 有时全绿、64ms 反而更差、120ms 稳定。
 * 如果延迟值本身是关键变量，结果应当单调（越大越安全）。不单调说明真正的变量是
 * **某两件事撞在了一起**，而不是延迟的绝对值。
 *
 * 已知的两个嫌疑：
 *
 * 1. resize 会让 App `setTerminalSize` → 一次 React render → 走 ink 的**节流**
 *    `onRender`（leading 立刻发，32ms 内的后续渲染合并成一次 tail）。
 * 2. 重挂 `<Static>` 在 D 毫秒后触发**又一次** render；这次带 static output，
 *    走 `log.clear() → write(static) → log(output)`，绕过节流。
 *
 * 若一次 tail 帧恰好落在 D 之后，它就会按旧行数擦、只重画 live → 历史消失。
 * 这个探针把每次 `stdout.write` 的时间戳、长度、以及「是否含 HIST-PROBE」打出来，
 * 直接看是谁擦的、在何时。
 *
 * ## 判据
 *
 * 对比一个失败率高的延迟（48）与稳定的延迟（120），看写入序列差在哪里。
 * 若失败轮里出现「内容写入之后，又有一条不含历史的长写入」→ 尾帧覆盖，机制坐实。
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

type WriteRecord = {
  at: number
  bytes: number
  hasStatic: boolean
  /** 是否含 CSI 2J / 3J（整屏重置）。 */
  hasReset: boolean
  /** 擦除行数（eraseLines 的 up 次数，粗略）。 */
  eraseUps: number
  head: string
}

/** 带记录的终端：既喂给模型，也记录每次写入的元信息。 */
class RecordingStream extends EventEmitter {
  readonly term: ReflowTerminal
  isTTY = true
  records: WriteRecord[] = []
  private epoch = 0
  constructor(columns: number, rows: number) {
    super()
    this.term = new ReflowTerminal(columns, rows)
  }
  get columns(): number { return this.term.columns }
  set columns(value: number) { this.term.columns = Math.max(1, Math.floor(value)) }
  get rows(): number { return this.term.rows }
  set rows(value: number) { this.term.rows = Math.max(1, Math.floor(value)) }

  markEpoch(): void { this.epoch = Date.now() }

  write(chunk: unknown): boolean {
    const text = String(chunk)
    this.records.push({
      at: this.epoch === 0 ? 0 : Date.now() - this.epoch,
      bytes: text.length,
      hasStatic: text.includes('HIST-PROBE'),
      hasReset: text.includes('\u001B[2J') || text.includes('\u001B[3J'),
      eraseUps: (text.match(/\u001B\[\d*A/g) ?? []).length,
      head: text.replace(/\u001B\[[0-?]*[ -/]*[@-~]/g, '').replace(/\s+/g, ' ').trim().slice(0, 60),
    })
    this.term.write(text)
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

type Round = {
  ok: boolean
  charlie: number
  records: WriteRecord[]
}

async function oneRound(delayMs: number): Promise<Round> {
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

  const stdout = new RecordingStream(80, 24)
  const stderr = new RecordingStream(80, 24)
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

    // 只保留 resize 之后的记录。
    stdout.records = []
    stdout.columns = 60
    stderr.columns = 60
    stdout.markEpoch()
    stdout.emit('resize')

    await delay(Math.max(500, delayMs + 400))
    const screen = stdout.term.screen()
    return {
      ok: screen.filter(line => line.includes('HIST-PROBE-CHARLIE')).length === 1,
      charlie: screen.filter(line => line.includes('HIST-PROBE-CHARLIE')).length,
      records: stdout.records.slice(),
    }
  } finally {
    instance.unmount()
    await delay(30)
  }
}

function printTimeline(label: string, round: Round): void {
  console.log(`\n--- ${label}（CHARLIE=${round.charlie}，${round.ok ? '通过' : '失败'}）---`)
  console.log('   t(ms)  bytes  static  reset  upA  head')
  for (const record of round.records) {
    console.log(
      `  ${String(record.at).padStart(5)}  ${String(record.bytes).padStart(5)}  `
      + `${record.hasStatic ? '   Y  ' : '   .  '}  ${record.hasReset ? '  Y  ' : '  .  '}  `
      + `${String(record.eraseUps).padStart(3)}  ${record.head}`,
    )
  }
}

async function main(): Promise<void> {
  const delayMs = Number(process.argv[2] ?? 48)
  const rounds = Number(process.argv[3] ?? 6)

  console.log(`resize 写入时间线：delay=${delayMs}ms，${rounds} 轮`)
  let firstFailure: Round | null = null
  let firstSuccess: Round | null = null

  for (let index = 0; index < rounds; index += 1) {
    const round = await oneRound(delayMs)
    if (!round.ok && !firstFailure) firstFailure = round
    if (round.ok && !firstSuccess) firstSuccess = round
    console.log(`  轮 ${index}: CHARLIE=${round.charlie} 写入 ${round.records.length} 次 ${round.ok ? '✔' : '✖'}`)
  }

  if (firstFailure) printTimeline(`失败样本 delay=${delayMs}`, firstFailure)
  if (firstSuccess) printTimeline(`通过样本 delay=${delayMs}`, firstSuccess)
  if (!firstFailure) console.log('\n本次没有失败样本，延迟值可能偏大 —— 换更小的延迟再跑。')

  if (firstFailure && firstSuccess) {
    // 失败样本里「含 static 的最后一次写入」之后，是否还有不含 static 的写入？
    const lastStatic = [...firstFailure.records].reverse().find(r => r.hasStatic)
    const after = lastStatic
      ? firstFailure.records.filter(r => r.at > lastStatic.at && !r.hasStatic && r.bytes > 20)
      : []
    console.log('\n判据：')
    console.log(`  失败样本里，最后一次含 static 的写入在 t+${lastStatic?.at ?? '—'}ms`)
    console.log(`  其后不含 static 的实质写入（>20B）：${after.length} 次`)
    for (const record of after) {
      console.log(`    t+${record.at}ms  ${record.bytes}B  擦除 upA=${record.eraseUps}  ${record.head}`)
    }
    console.log(after.length > 0
      ? '  → 历史写出之后仍有覆盖写入：尾帧覆盖机制坐实。'
      : '  → 未观察到覆盖写入，需看更早的序列。')
  }
}

main().catch(error => {
  console.error(error)
  process.exit(1)
})
