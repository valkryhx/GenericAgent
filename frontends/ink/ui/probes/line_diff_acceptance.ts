/**
 * 行级 diff 的正确性验收（单次运行 + 双回放）。
 *
 * ── 前几版为什么都不可信 ────────────────────────────────────────────
 *
 * 1. 跨两次运行比较 → `runningLabel` 随机（已用固定 Math.random 修），
 *    但**活动行里的 `(0s)` 计时是 wall-clock**，两次运行必然出现差一拍。
 *    跨运行比对的是随机数 + 时间，不是 diff 正确性。
 * 2. `virtualScreen.applyToScreen` 不模拟滚动，帧高触顶时与真实终端不符。
 *
 * ── 本版做法 ───────────────────────────────────────────────────────
 *
 * **只跑一次**，抓 ink 的原始帧。然后逐帧做双回放：
 *   - 屏幕 A：每帧用 ink 原字节（全量重写）
 *   - 屏幕 B：每帧用 buildLineDiff 的输出（行级 diff），失败则退化
 * 两个屏幕从同一状态出发，喂的是**同一份输入**，因此差异只可能来自 diff 本身。
 * 用带滚动的虚拟终端（视口 = 真实终端高度）回放。
 *
 * 运行： cd frontends/ink/ui && npx tsx probes/line_diff_acceptance.ts
 */
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from '../src/App.js'
import { createCursorParkStdout } from '../src/stdoutCursorPark.js'
import { buildLineDiff, looksLikeFrame, parseFrameRows } from '../src/lineDiff.js'
import type { BridgeClient } from '../src/bridgeClient.js'
import type { BridgeEvent } from '../src/protocol.js'

const NL = '\n'
const COLUMNS = Number(process.env.PROBE_COLUMNS ?? 100)
const ROWS = Number(process.env.PROBE_ROWS ?? 30)
const delay = (ms: number) => new Promise<void>(r => setTimeout(r, ms))

class CaptureWriteStream extends EventEmitter {
  columns = COLUMNS
  rows = ROWS
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

const CSI = '\u001b['

/** 带滚动的虚拟终端（视口 ROWS 行；越界换行将顶行推入 scrollback）。 */
class ScrollScreen {
  rows: string[]
  row = 0
  col = 0
  scrolled = 0

  constructor(private readonly height: number, private readonly width: number) {
    this.rows = Array.from({ length: height }, () => ' '.repeat(width))
  }

  private lineFeed(): void {
    if (this.row >= this.height - 1) {
      this.rows.shift()
      this.rows.push(' '.repeat(this.width))
      this.scrolled += 1
    } else {
      this.row += 1
    }
    this.col = 0
  }

  private put(ch: string): void {
    if (this.col >= this.width) { this.col = 0; this.lineFeed() }
    const line = this.rows[this.row]!
    this.rows[this.row] = line.slice(0, this.col) + ch + line.slice(this.col + 1)
    this.col += 1
  }

  feed(chunk: string): void {
    let i = 0
    while (i < chunk.length) {
      if (chunk.startsWith(CSI, i)) {
        const m = /^\u001b\[([0-9;?]*)([A-Za-z])/.exec(chunk.slice(i))
        if (!m) { i += 1; continue }
        const params = m[1] ?? ''
        const cmd = m[2]!
        i += m[0].length
        if (params.startsWith('?')) continue
        const first = Number(params.split(';')[0] || '0')
        const n = first === 0 ? 1 : first
        if (cmd === 'A') this.row = Math.max(0, this.row - n)
        else if (cmd === 'B') this.row = Math.min(this.height - 1, this.row + n)
        else if (cmd === 'C') this.col = Math.min(this.width - 1, this.col + n)
        else if (cmd === 'D') this.col = Math.max(0, this.col - n)
        else if (cmd === 'G') this.col = Math.max(0, (first === 0 ? 1 : first) - 1)
        else if (cmd === 'H' || cmd === 'f') {
          const parts = params.split(';')
          this.row = Math.max(0, Math.min(this.height - 1, Number(parts[0] || '1') - 1))
          this.col = Math.max(0, Number(parts[1] || '1') - 1)
        } else if (cmd === 'J') {
          if (first === 2 || first === 0) {
            for (let r = 0; r < this.height; r += 1) this.rows[r] = ' '.repeat(this.width)
            this.row = 0
            this.col = 0
          }
        } else if (cmd === 'K') {
          const line = this.rows[this.row]!
          this.rows[this.row] = first === 2
            ? ' '.repeat(this.width)
            : first === 0
              ? line.slice(0, this.col) + ' '.repeat(this.width - this.col)
              : ' '.repeat(this.col) + line.slice(this.col)
        }
        continue
      }
      if (chunk.startsWith('\u001b]', i)) {
        const bel = chunk.indexOf('\u0007', i)
        const st = chunk.indexOf('\u001b\\', i)
        const end = bel === -1 ? st : st === -1 ? bel : Math.min(bel, st)
        i = end === -1 ? chunk.length : end + (end === bel ? 1 : 2)
        continue
      }
      if (chunk[i] === '\u001b') { i += 1; continue }
      const ch = chunk[i]!
      i += 1
      if (ch === '\r') { this.col = 0; continue }
      if (ch === '\n') { this.lineFeed(); continue }
      if (ch === '\t') { this.col = Math.min(this.width - 1, (Math.floor(this.col / 8) + 1) * 8); continue }
      if (ch < ' ') continue
      if (this.row >= this.height) this.row = this.height - 1
      this.put(ch)
    }
  }

  snapshot(): { rows: string[]; row: number; col: number; scrolled: number } {
    return { rows: this.rows.map(r => r.trimEnd()), row: this.row, col: this.col, scrolled: this.scrolled }
  }
}

/** 抓一次真实运行的每个「帧写入」（拆掉 BSU/ESU 后的原始字节）。 */
async function captureFrames(): Promise<string[]> {
  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
  const stdin = new FakeReadStream()
  // 关掉 diff：我们只想要 ink 的原始字节。
  const park = createCursorParkStdout(stdout as never, { lineDiff: false })

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
    { stdout: park.stdout, stderr: stderr as unknown as NodeJS.WriteStream, stdin: stdin as unknown as NodeJS.ReadStream, patchConsole: false, exitOnCtrlC: false } as never,
  )
  const e = () => emit as unknown as (ev: BridgeEvent) => void

  try {
    await delay(450)
    e()({ type: 'status', status: 'running', taskId: 1 })
    e()({ type: 'user', taskId: 1, text: 'ok' })
    await delay(200)
    for (let i = 0; i < 20; i += 1) {
      e()({ type: 'assistant_delta', taskId: 1, text: `stream ${i} ` + 'x'.repeat(20) + NL })
      await delay(18)
    }
    await delay(150)
    for (const ch of 'hello') { stdin.send(ch); await delay(70) }
    for (let i = 0; i < 3; i += 1) { stdin.send('\u007f'); await delay(70) }
    await delay(150)
    e()({ type: 'error', message: 'upstream 502' } as never)
    await delay(200)
    e()({ type: 'error', message: '' } as never)
    await delay(200)
    stdin.send('\u0015'); await delay(60)
    stdin.send('l1'); await delay(60)
    stdin.send('\u001b\r'); await delay(60)
    stdin.send('l2'); await delay(120)
    e()({ type: 'assistant_done', taskId: 1, text: 'done' } as never)
    e()({ type: 'status', status: 'idle', taskId: 1 })
    await delay(250)
  } finally {
    instance.unmount()
    await delay(200)
  }
  // 拆掉同步输出包络，只保留内容。
  return stdout.chunks.map(c => c.split('\u001b[?2026h').join('').split('\u001b[?2026l').join(''))
}

/**
 * 把 writer 的输出拆成 [unpark 前缀][ink 原始帧][park 后缀]。
 *
 * 必须三段都保留：只替换中间那段才是 writer 的真实行为。
 * （我先前整块替换成 diff，把 park 段弄丢了，比对结果全是光标错位 —— 探针的错。）
 */
function splitParkWrap(chunk: string): { prefix: string; frame: string; suffix: string } {
  const prefixM = /^(?:\u001b\[\?25l)?(?:\u001b\[\d+B)?\r/.exec(chunk)
  const prefix = prefixM ? prefixM[0] : ''
  const rest = chunk.slice(prefix.length)
  const suffixM = /(?:\u001b\[\d+A)?\u001b\[\d+G(?:\u001b\[\?25h)?$/.exec(rest)
  if (!suffixM) return { prefix, frame: rest, suffix: '' }
  return { prefix, frame: rest.slice(0, suffixM.index), suffix: suffixM[0] }
}

async function main() {
  console.log(`\n抓一次真实运行的帧（${COLUMNS}x${ROWS}）…`)
  const chunks = await captureFrames()
  console.log(`  共 ${chunks.length} 次写入`)

  const screenFull = new ScrollScreen(ROWS, COLUMNS)
  const screenDiff = new ScrollScreen(ROWS, COLUMNS)

  let prev: string[] | null = null
  let diffed = 0
  let fellBack = 0
  let mismatch = 0
  const samples: string[] = []

  for (let i = 0; i < chunks.length; i += 1) {
    const chunk = chunks[i]!
    // A 路：永远写原始字节。
    screenFull.feed(chunk)

    // B 路：只替换「帧内容」那一段，前后缀原样保留。
    const { prefix, frame, suffix } = splitParkWrap(chunk)
    let emitted = chunk
    if (looksLikeFrame(frame)) {
      const rows = parseFrameRows(frame)
      if (rows && prev !== null) {
        const d = buildLineDiff(prev, rows, ROWS)
        if (d === null) fellBack += 1
        else { diffed += 1; emitted = prefix + d + suffix }
        prev = rows
      } else if (rows) {
        prev = rows
      } else {
        prev = null
      }
    } else {
      prev = null
    }
    screenDiff.feed(emitted)

    const a = screenFull.snapshot()
    const b = screenDiff.snapshot()
    let bad = ''
    if (a.row !== b.row || a.col !== b.col) bad = `cursor (${a.row},${a.col}) vs (${b.row},${b.col})`
    else {
      for (let r = 0; r < ROWS; r += 1) {
        if ((a.rows[r] ?? '') !== (b.rows[r] ?? '')) { bad = `row ${r}: ${JSON.stringify(a.rows[r] ?? '')} vs ${JSON.stringify(b.rows[r] ?? '')}`; break }
      }
    }
    if (bad) {
      mismatch += 1
      if (samples.length < 4) samples.push(`#${i}: ${bad}`)
    }
  }

  const bytesFull = chunks.join('').length
  let bytesDiff = 0
  {
    // 重算 B 路字节量（与上面同一策略）
    prev = null
    for (const chunk of chunks) {
      const { prefix, frame, suffix } = splitParkWrap(chunk)
      if (looksLikeFrame(frame)) {
        const rows = parseFrameRows(frame)
        if (rows && prev !== null) {
          const d = buildLineDiff(prev, rows, ROWS)
          bytesDiff += (d === null ? chunk : prefix + d + suffix).length
          prev = rows
          continue
        }
        if (rows) { prev = rows; bytesDiff += chunk.length; continue }
        prev = null
      } else prev = null
      bytesDiff += chunk.length
    }
  }

  console.log(`\n=== 结果 ===`)
  console.log(`  diff 生效帧 = ${diffed}   退化帧 = ${fellBack}`)
  console.log(`  字节：全量 ${bytesFull} → diff ${bytesDiff}   节省 ${((1 - bytesDiff / bytesFull) * 100).toFixed(1)}%`)
  console.log(`  屏幕不一致 = ${mismatch} / ${chunks.length}`)
  for (const s of samples) console.log(`     ${s}`)
  console.log(`\n  ${mismatch === 0 ? '✅ 行级 diff 等价于全量重写' : '❌ 存在不一致'}`)
}

main().catch(err => { console.error(err); process.exit(1) })
