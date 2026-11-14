/**
 * 探针：delay=0 时，每次 `stdout.write` 的**完整字节**（转义后可见）。
 *
 * 手动跑：`npx tsx probes/resize_write_dump.ts [延迟ms] [轮数]`（不参与 npm test）
 *
 * ## 为什么要这个探针
 *
 * `resize_write_timeline.ts` 只打印每次写入的前 60 个可见字符和擦除次数，够用来
 * 判断"谁擦掉了历史"，但不够用来设计修复：要把「整屏重置」与「整屏重绘」合成
 * 一次写出，必须先看清每次写入里**到底有哪些控制序列、按什么顺序**。
 *
 * 具体要回答：
 *   - 含 static 的那次写入，是不是本来就包含 `log.clear()` 的擦除 + static + live？
 *   - 之后那次只用 live 的写入，它的擦除计数对应多少行？和历史行有没有重叠？
 *   - reset 那次写入之后，光标状态让后续 eraseLines 从哪里开始擦？
 *
 * ## 判据
 *
 * 同一次运行的失败轮与通过轮对比：若失败轮的「含 static 写入」之后紧跟一次
 * 擦除计数 > live 行数的写入 → 修复方向是「让 reset 与含 static 的写入原子化，
 * 并丢弃其间的 live-only 帧」。
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
const ESC = String.fromCharCode(27)
const delay = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms))

/** 把控制序列转成可读形式，同时保留文本内容。 */
function visualize(text: string): string {
  return text
    .replace(new RegExp(ESC + '\\[([0-9;?]*)([A-Za-z])', 'g'), (_m, p, f) => `<CSI ${p}${f}>`)
    .replace(new RegExp(ESC + '\\]104;255\\x07', 'g'), '<OSC104>')
    .replace(/\r/g, '<CR>')
    .replace(/\n/g, '<LF>' + NL + '        ')
}

class ReflowStream extends EventEmitter {
  readonly term: ReflowTerminal
  isTTY = true
  records: Array<{ at: number; text: string }> = []
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
    this.records.push({ at: this.epoch === 0 ? 0 : Date.now() - this.epoch, text })
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

type Round = { ok: boolean; charlie: number; records: Array<{ at: number; text: string }> }

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
    await delay(140)
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

function dump(label: string, round: Round): void {
  console.log(`\n===== ${label}（CHARLIE=${round.charlie}，${round.ok ? '通过' : '失败'}）=====`)
  round.records.forEach((record, index) => {
    console.log(`\n[${index}] t+${record.at}ms  ${record.text.length}B`)
    console.log('        ' + visualize(record.text))
  })
}

async function main(): Promise<void> {
  const delayMs = Number(process.argv[2] ?? 0)
  const rounds = Number(process.argv[3] ?? 6)

  console.log(`resize 完整写入转储：delay=${delayMs}ms，${rounds} 轮`)
  let failure: Round | null = null
  let success: Round | null = null
  for (let index = 0; index < rounds; index += 1) {
    const round = await oneRound(delayMs)
    if (!round.ok && !failure) failure = round
    if (round.ok && !success) success = round
    console.log(`  轮 ${index}: CHARLIE=${round.charlie} 写入 ${round.records.length} 次 ${round.ok ? '✔' : '✖'}`)
  }
  if (failure) dump('失败样本', failure)
  if (success) dump('通过样本', success)
}

main().catch(error => {
  console.error(error)
  process.exit(1)
})
