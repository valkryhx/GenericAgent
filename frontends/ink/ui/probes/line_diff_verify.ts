/**
 * 验证探针：行级 diff 是否**正确**（不只是省字节），以及在真实流式负载下的收益。
 *
 * 上一版 `line_diff_potential.ts` 测出可省 97%，但两个疑点必须先排除：
 *   1. 「平均每帧变化 0.19 行」低得可疑 —— 因为 GA 的 transcript 走 <Static>（只追加），
 *      动态区其实只有 composer + 状态行。这会让收益数字虚高。
 *   2. 只算了字节，没验证「用 diff 写出的屏幕」和「用全量重写写出的屏幕」是否一致。
 *
 * 本探针用 `virtualScreen.applyToScreen` 把两种策略分别回放成字符网格，逐帧比对。
 * 网格一致才算正确。
 *
 * 运行： cd frontends/ink/ui && npx tsx probes/line_diff_verify.ts
 */
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from '../src/App.js'
import { createCursorParkStdout } from '../src/stdoutCursorPark.js'
import { applyToScreen, createScreen } from '../src/virtualScreen.js'
import type { BridgeClient } from '../src/bridgeClient.js'
import type { BridgeEvent } from '../src/protocol.js'

const NL = '\n'
const DELAY = (ms: number) => new Promise<void>(r => setTimeout(r, ms))
const COLUMNS = Number(process.env.PROBE_COLUMNS ?? 100)
const ROWS = Number(process.env.PROBE_ROWS ?? 30)

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

const BSU = '\u001b[?2026h'
const ESU = '\u001b[?2026l'
const GUTTER = 0

function eraseLines(count: number): string {
  let s = ''
  for (let i = 0; i < count; i++) s += '\u001b[2K' + (i < count - 1 ? '\u001b[1A' : '')
  if (count) s += '\u001b[G'
  return s
}

/** 从一次写出里取出「内容行」与「park 段」。 */
function splitChunk(chunk: string): { rows: string[]; park: string } | null {
  const inner = chunk.split(BSU).join('').split(ESU).join('')
  if (!inner.includes('\u001b[2K')) return null
  // ink@5.2.1 的 eraseLines 以 `\x1b[G`（无参数，等价列 0）收尾，**不是** `\x1b[0G`。
  // 内容 = 最后一个 eraseLines 收尾之后，到帧内容的末尾（以 \n 结束）。
  const zeroG = inner.lastIndexOf('\u001b[G')
  if (zeroG < 0) return null
  const rest = inner.slice(zeroG + '\u001b[G'.length)
  const parkM = /\u001b\[\d*A?\u001b\[\d+G$/.exec(rest)
  const park = parkM ? parkM[0] : ''
  const content = parkM ? rest.slice(0, parkM.index) : rest
  const rows = content.split(NL)
  if (rows.at(-1) === '') rows.pop()
  return { rows, park }
}

/**
 * 行级 diff 生成器：给定上一帧行、本帧行，产出「从上一帧帧底出发」的写入序列。
 *
 * 关键不变量：写完后光标必须回到**本帧帧底**（row = next.length, col 0），
 * 这样后续 park 段的相对移动才仍然成立。
 */
function buildLineDiff(prev: string[], next: string[]): string {
  let out = ''
  let cursor = prev.length          // 起始：上一帧帧底
  const max = Math.max(prev.length, next.length)

  for (let row = 0; row < max; row += 1) {
    const before = row < prev.length ? prev[row]! : ''
    const after = row < next.length ? next[row]! : ''
    const gone = row >= next.length
    if (!gone && before === after) continue

    // 移动到该行（相对当前光标）。
    const dy = row - cursor
    if (dy !== 0) out += dy < 0 ? `\u001b[${-dy}A` : `\u001b[${dy}B`
    out += '\r\u001b[2K'      // 列 0 + 擦整行
    if (!gone) {
      // 行首归零：该行若带样式但结尾没 reset，不补就会漏到右边。
      out += '\u001b[0m'
      out += next[row]!
    }
    cursor = row
  }

  // 回到本帧帧底。
  const back = next.length - cursor
  if (back !== 0) out += back < 0 ? `\u001b[${-back}A` : `\u001b[${back}B`
  out += '\r'
  return out
}

async function main() {
  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
  const stdin = new FakeReadStream()
  const park = createCursorParkStdout(stdout as never)

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

  try {
    await DELAY(400)
    e()({ type: 'status', status: 'running', taskId: 1 })
    e()({ type: 'user', taskId: 1, text: 'ok' })
    await DELAY(200)

    // 场景 A：流式（transcript 走 Static，动态区变化小）
    for (let i = 0; i < 30; i += 1) {
      e()({ type: 'assistant_delta', taskId: 1, text: `stream ${i}` + NL })
      await DELAY(20)
    }
    await DELAY(150)
    const markA = stdout.chunks.length

    // 场景 B：打字 + 回退（composer 行每次都变）
    for (const ch of 'hello world') { stdin.send(ch); await DELAY(70) }
    for (let i = 0; i < 5; i += 1) { stdin.send('\u007f'); await DELAY(70) }
    await DELAY(150)
    const markB = stdout.chunks.length

    // 场景 C：多行输入（composer 行长变化）
    stdin.send('\u0015'); await DELAY(60)
    stdin.send('l1'); await DELAY(60)
    stdin.send('\u001b\r'); await DELAY(60)
    stdin.send('l2'); await DELAY(60)
    stdin.send('\u001b\r'); await DELAY(60)
    stdin.send('l3'); await DELAY(150)
    const markC = stdout.chunks.length

    // ── 逐帧比对两种策略的屏幕 ──────────────────────────────────────────
    const screenFull = createScreen(ROWS + 200, COLUMNS)
    const screenDiff = createScreen(ROWS + 200, COLUMNS)

    let fullBytes = 0
    let diffBytes = 0
    let prevRows: string[] = []
    let mismatches = 0
    let framesCompared = 0
    const mismatchSamples: string[] = []

    let frameIndex = 0
    for (const chunk of stdout.chunks) {
      const parsed = splitChunk(chunk)
      if (!parsed) continue
      const { rows, park: parkSeq } = parsed
      frameIndex += 1

      // 每帧都从一个已知起点开始：上一帧 park 之后，光标停在 caret。
      // 因此两种策略都必须先补上 unpark（把光标从 caret 相对移回帧底），
      // 这一步在真实 chunk 里由包裹流插入，我在对比时必须显式建模，否则两者起点不同。
      const upM = /^\u001b\[(\d+)A/.exec(parkSeq)
      const parkUp = upM ? Number(upM[1]) : 0
      const unpark = parkUp > 0 ? `\u001b[${parkUp}B\r` : '\r'

      // 策略 1（现状）：unpark + 擦除上一帧 + 全量重写 + park。
      //
      // 关键：ink 的 previousLineCount 是 `output.split('\n').length`，而 output 以 \n 结尾，
      // 所以对 N 行内容这里是 **N+1**（多出来的是「最后一行之后」那一格）。少算这一项，
      // 虚拟终端每帧就会漂一行 —— 我第一版就是这么错估的。
      const full = unpark + eraseLines(prevRows.length + 1) + rows.join(NL) + NL + parkSeq
      fullBytes += full.length
      applyToScreen(screenFull, full, COLUMNS)

      // 策略 2（行级 diff）：unpark + 只写变化行 + park。
      const diffSeq = unpark + buildLineDiff(prevRows, rows) + parkSeq
      diffBytes += diffSeq.length
      applyToScreen(screenDiff, diffSeq, COLUMNS)

      framesCompared += 1
      // 比对光标落点（Grid 全部内容）—— 差异行数直接反映正确性。
      const rowsEqual = screenFull.rows.length === screenDiff.rows.length
 differenceCheck: {
        if (!rowsEqual) { mismatches += 1; break differenceCheck }
        for (let r = 0; r < screenFull.rows.length; r += 1) {
          const a = screenFull.rows[r]!
          const b = screenDiff.rows[r]!
          if (a.trimEnd() !== b.trimEnd()) {
            mismatches += 1
            if (mismatchSamples.length < 3) {
              mismatchSamples.push(`frame ${frameIndex} row ${r}: full=${JSON.stringify(a.trimEnd())} diff=${JSON.stringify(b.trimEnd())}`)
            }
            break
          }
        }
        // 光标位置也必须一致（否则下一帧的相对移动会漂）。
        if (screenFull.row !== screenDiff.row || screenFull.col !== screenDiff.col) {
          mismatches += 1
          if (mismatchSamples.length < 3) {
            mismatchSamples.push(`frame ${frameIndex} cursor: full=(${screenFull.row},${screenFull.col}) diff=(${screenDiff.row},${screenDiff.col})`)
          }
        }
        void GUTTER
      }
      prevRows = rows
    }

    console.log(`\n=== 行级 diff 正确性（${framesCompared} 帧回放比对）===`)
    console.log(`  屏幕/光标不一致的帧 = ${mismatches}`)
    console.log(`  ${mismatches === 0 ? '✅ 网格与光标完全一致' : '❌ 存在不一致'}`)
    for (const s of mismatchSamples) console.log(`     ${s}`)

    console.log(`\n=== 字节（含 park 段）===`)
    console.log(`  现状（eraseLines + 全量） = ${fullBytes}`)
    console.log(`  行级 diff                = ${diffBytes}`)
    console.log(`  可省                     = ${((1 - diffBytes / Math.max(1, fullBytes)) * 100).toFixed(1)}%`)

    console.log(`\n=== 分场景平均每帧字节 ===`)
    const seg = (from: number, to: number, label: string) => {
      let f = 0
      let d = 0
      let n = 0
      let p: string[] = []
      for (let i = 0; i < Math.min(to, stdout.chunks.length); i += 1) {
        const parsed = splitChunk(stdout.chunks[i]!)
        if (!parsed) continue
        if (i >= from) {
          f += eraseLines(p.length + 1).length + parsed.rows.join(NL).length
          d += buildLineDiff(p, parsed.rows).length
          n += 1
        }
        p = parsed.rows
      }
      if (n > 0) console.log(`  ${label.padEnd(18)} 现状 ${Math.round(f / n)}  B/帧   diff ${Math.round(d / n)} B/帧`)
    }
    seg(0, markA, '流式输出')
    seg(markA, markB, '打字 + 回退')
    seg(markB, markC, '多行输入')
  } finally {
    instance.unmount()
    await DELAY(200)
  }
}

main().catch(err => { console.error(err); process.exit(1) })
