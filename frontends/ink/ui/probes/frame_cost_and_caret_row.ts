/**
 * 探针：把「卡顿」和「光标下穿」两个现象变成可断言的数字。
 *
 * 1) 卡顿 —— 1 秒流式期间的帧数 / 字节数（ink 5.2.1 的 onRender 被 throttle 到 32ms，
 *    且每帧整帧重写 eraseLines + 全量 output，而非屏幕 diff）。
 * 2) 下穿 —— 把 App 声明的 park 行（模型推导的 liveViewportRows - inputCursor.row）
 *    与 ink 实际写出的行数（ground truth）逐帧对比；再用虚拟终端把 park 落到绝对行，
 *    看它是否落在输入框下侧线（帧内最后一行）或之外。
 *
 * 运行： cd frontends/ink/ui && npx tsx probes/frame_cost_and_caret_row.ts
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
  send(text: string) { this.queue.push(text); this.emit('readable') }
}

const stripAnsi = (t: string) => t
  .replace(/\u001B\][^\u0007]*(?:\u0007|\u001B\\)/g, '')
  .replace(/\u001B\[[0-?]*[ -/]*[@-~]/g, '')
  .replace(/\u001B[=>]/g, '')

// ── 最小虚拟终端：只追踪光标行与「擦除/写入」的行数 ─────────────────────────
type FrameRec = {
  /** ink 实际写出的内容行数（不含末尾换行造成的空行） */
  actualRows: number
  /** park 声明的「从帧底上移几行」 */
  up: number | null
  /** park 的绝对行（相对本帧内容顶行，0-based） */
  caretRowInFrame: number | null
  bytes: number
  /** 帧内出现过的、被当作输入框提示行的文本 */
  prompt: string
}

function countErase(chunk: string): number {
  return (chunk.match(/\u001b\[2K/g) ?? []).length
}

async function main() {
  const COLUMNS = Number(process.env.PROBE_COLUMNS ?? 100)
  const ROWS = Number(process.env.PROBE_ROWS ?? 30)

  const stdout = new CaptureWriteStream(COLUMNS, ROWS)
  const stderr = new CaptureWriteStream(COLUMNS, ROWS)
  const stdin = new FakeReadStream()
  const park = createCursorParkStdout(stdout as never)

  const frames: FrameRec[] = []
  let pendingFrame: FrameRec | null = null

  const baseWrite = stdout.write.bind(stdout)
  ;(stdout as unknown as { write: (c: unknown) => boolean }).write = (chunk: unknown) => {
    const s = String(chunk)
    const erase = countErase(s)
    if (erase > 0) {
      // ink 主帧：eraseLines(prevN) + body + '\n'
      const idx = s.lastIndexOf('\u001b[0G')
      const body = idx >= 0 ? s.slice(idx + 4) : s
      const lines = body.split(NL)
      // ink 的 output = body + '\n'：末元素是最后一行之后的空串
      const actualRows = Math.max(0, lines.length - 1)
      const rec: FrameRec = {
        actualRows,
        up: null,
        caretRowInFrame: null,
        bytes: s.length,
        prompt: promptLine(body),
      }
      frames.push(rec)
      pendingFrame = rec
    } else if (s.includes('\u001b[?25h')) {
      // park：可选 ESC[nA + ESC[mG + SHOW
      const upM = /^\u001b\[(\d+)A/.exec(s)
      const up = upM ? Number(upM[1]) : 0
      if (pendingFrame) {
        pendingFrame.up = up
        // 帧底 = actualRows（内容行号 0..actualRows-1，帧底是下一个空行）
        pendingFrame.caretRowInFrame = pendingFrame.actualRows - up
      }
    }
    return baseWrite(chunk)
  }

  let emit: ((e: BridgeEvent) => void) | null = null
  const startBridgeClient = (
    _p: string, _b: string, onEvent: (e: BridgeEvent) => void,
  ): BridgeClient => {
    emit = onEvent
    setTimeout(() => onEvent({ type: 'ready', version: 1 }), 0)
    return { send() {}, stop() {} }
  }

  const instance = render(
    React.createElement(App, { python: 'python', bridgeScript: 'x', startBridgeClient, cursorPark: park } as never),
    {
      stdout: park.stdout,
      stderr: stderr as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      patchConsole: false,
      exitOnCtrlC: false,
    } as never,
  )

  try {
    await delay(400)
    const e = emit as unknown as (ev: BridgeEvent) => void

    // ── 场景 1：流式输出 1 秒，量帧数与字节 ────────────────────────────────
    e({ type: 'status', status: 'running', taskId: 1 })
    e({ type: 'user', taskId: 1, text: '批量输出' })
    await delay(150)
    const streamStart = frames.length
    const t0 = Date.now()
    for (let i = 0; i < 100; i += 1) {
      e({ type: 'assistant_delta', taskId: 1, text: `L${i} ${'x'.repeat(40)}` + NL })
      await delay(10)   // 100 次 / 1000ms = 100 帧/秒的输入速率
    }
    const elapsed = Date.now() - t0
    const streamFrames = frames.slice(streamStart)
    console.log(`\n=== 场景 1：流式 ${elapsed}ms，输入 100 次 delta（≈100/s）===`)
    console.log(`  ink 实际写出帧数 = ${streamFrames.length}   → ${(streamFrames.length / (elapsed / 1000)).toFixed(1)} 帧/秒`)
    const totalBytes = streamFrames.reduce((a, f) => a + f.bytes, 0)
    console.log(`  总字节 = ${totalBytes}   单帧 avg = ${Math.round(totalBytes / Math.max(1, streamFrames.length))}  max = ${Math.max(0, ...streamFrames.map(f => f.bytes))}`)
    console.log(`  （对照：Claude Code fork 用 FRAME_INTERVAL_MS=16 的 throttle，且只写屏幕 diff）`)

    // ── 场景 2：流式中打字 + 回退，检查 park 行是否落在帧内 ────────────────
    const typeStart = frames.length
    stdin.send('a')
    await delay(80)
    stdin.send('b')
    await delay(80)
    stdin.send('\u007f')
    await delay(120)

    console.log(`\n=== 场景 2：流式中输入 "ab" + Backspace ===`)
    const typed = frames.slice(typeStart)
    for (const f of typed) {
      if (f.up === null) continue
      const inside = f.caretRowInFrame !== null && f.caretRowInFrame >= 0 && f.caretRowInFrame < f.actualRows
      console.log(`  actualRows=${String(f.actualRows).padStart(2)}  up=${String(f.up).padStart(2)}  caretRowInFrame=${String(f.caretRowInFrame).padStart(2)}  ${inside ? 'OK(帧内)' : '<<越界：落在帧底之外/下穿'}  prompt=${JSON.stringify(f.prompt)}`)
    }

    // ── 场景 3：加 goal card + subagent notices（bottom 行数变化）──────────
    const goalStart = frames.length
    e({ type: 'goal_metadata', metadata: { status: 'active', condition: '写完测试', turnsUsed: 1, maxTurns: 20 } } as never)
    await delay(200)
    e({ type: 'assistant_delta', taskId: 1, text: 'goal 之后继续输出' + NL })
    await delay(200)
    console.log(`\n=== 场景 3：goal card 出现后 ===`)
    for (const f of frames.slice(goalStart)) {
      if (f.up === null) continue
      const inside = f.caretRowInFrame !== null && f.caretRowInFrame >= 0 && f.caretRowInFrame < f.actualRows
      console.log(`  actualRows=${String(f.actualRows).padStart(2)}  up=${String(f.up).padStart(2)}  caretRow=${String(f.caretRowInFrame).padStart(2)}  ${inside ? 'OK' : '<<越界'}`)
    }

    // ── 汇总 ──────────────────────────────────────────────────────────────
    const parked = frames.filter(f => f.up !== null)
    const outOfFrame = parked.filter(f => f.caretRowInFrame !== null && (f.caretRowInFrame < 0 || f.caretRowInFrame >= f.actualRows))
    console.log(`\n=== 汇总 ===`)
    console.log(`  有 park 的帧 = ${parked.length}   越界（下穿/上穿）= ${outOfFrame.length}`)
  } finally {
    instance.unmount()
    await delay(150)
  }
}

function promptLine(body: string): string {
  const lines = stripAnsi(body).split(NL)
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    const l = lines[i]!
    if (l.trimStart().startsWith('>')) return l.trimEnd()
  }
  return ''
}

main().catch(err => { console.error(err); process.exit(1) })
