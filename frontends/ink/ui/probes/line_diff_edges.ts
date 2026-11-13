/**
 * 验证：行级 diff 在两类危险边界上是否还成立？
 *
 * 前面的 `line_diff_verify.ts` 只测了「动态区平滑增长」这一种。真正会让行级 diff
 * 翻车的是两处：
 *
 *   A. 内容高度 >= 终端行数 —— ink 这时走的是
 *      `clearTerminal + fullStaticOutput + output`（不是 eraseLines），
 *      包裹层若仍按行级 diff 处理，会与 <Static> 的 scrollback 打架。
 *   B. 动态区变矮（面板关闭、通知消失、输入框回缩）—— 需要删行，
 *      而删行与「相对光标移动」交互复杂。
 *
 * 判据：网格 + 光标是否与全量重写一致。不一致的帧说明该边界必须退化为全量重写。
 *
 * 运行： cd frontends/ink/ui && PROBE_ROWS=12 npx tsx probes/line_diff_edges.ts
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
const COLUMNS = Number(process.env.PROBE_COLUMNS ?? 80)
const ROWS = Number(process.env.PROBE_ROWS ?? 12)

class W extends EventEmitter {
  columns = COLUMNS
  rows = ROWS
  isTTY = true
  chunks: string[] = []
  write(c: unknown): boolean { this.chunks.push(String(c)); return true }
}
class R extends EventEmitter {
  isTTY = true
  private q: string[] = []
  setRawMode() { return this }
  setEncoding() { return this }
  ref() { return this }
  unref() { return this }
  read(): string | null { return this.q.shift() ?? null }
  resume() { return this }
  pause() { return this }
  send(t: string) { this.q.push(t); this.emit('readable') }
}

const BSU = '\u001b[?2026h'
const ESU = '\u001b[?2026l'

function eraseLines(count: number): string {
  let s = ''
  for (let i = 0; i < count; i++) s += '\u001b[2K' + (i < count - 1 ? '\u001b[1A' : '')
  if (count) s += '\u001b[G'
  return s
}

type Kind = 'erase' | 'clearTerminal' | 'other'

function classify(chunk: string): { kind: Kind; rows: string[]; park: string; fullPrefix: string } {
  const inner = chunk.split(BSU).join('').split(ESU).join('')
  if (/\u001b\[2J/.test(inner)) {
    // clearTerminal 路径：整段含 2J，必须原样透传。
    return { kind: 'clearTerminal', rows: [], park: '', fullPrefix: inner }
  }
  if (!inner.includes('\u001b[2K')) {
    return { kind: 'other', rows: [], park: '', fullPrefix: inner }
  }
  const at = inner.lastIndexOf('\u001b[G')
  const rest = at >= 0 ? inner.slice(at + 2) : inner
  const parkM = /\u001b\[\d*A?\u001b\[\d+G$/.exec(rest)
  const park = parkM ? parkM[0] : ''
  const content = parkM ? rest.slice(0, parkM.index) : rest
  const rows = content.split(NL)
  if (rows.at(-1) === '') rows.pop()
  return { kind: 'erase', rows, park, fullPrefix: '' }
}

/** 行级 diff；返回 null 表示「此帧不应 diff，必须退化全量重写」。 */
function buildLineDiff(prev: string[], next: string[]): string | null {
  // 变矮时 ink 的擦除几何另有分支，行级 diff 不安全 → 退化。
  if (next.length < prev.length) return null
  let out = ''
  let cursor = prev.length
  for (let row = 0; row < next.length; row += 1) {
    const before = row < prev.length ? prev[row]! : ''
    if (before === next[row]) continue
    const dy = row - cursor
    if (dy !== 0) out += dy < 0 ? `\u001b[${-dy}A` : `\u001b[${dy}B`
    out += '\r\u001b[2K\u001b[0m' + next[row]!
    cursor = row
  }
  const back = next.length - cursor
  if (back !== 0) out += back < 0 ? `\u001b[${-back}A` : `\u001b[${back}B`
  out += '\r'
  return out
}

async function main() {
  const stdout = new W()
  const stderr = new W()
  const stdin = new R()
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

    // 撑满视口：远超终端行数
    for (let i = 0; i < 40; i += 1) {
      e()({ type: 'assistant_delta', taskId: 1, text: `row ${i} ` + 'z'.repeat(30) + NL })
      await DELAY(15)
    }
    await DELAY(200)
    const markTall = stdout.chunks.length

    // 动态区增长：子代理通知 +1 行、+2 行
    e()({ type: 'agent_snapshot', snapshot: { records: [{ executionId: 'a', name: 'c1', status: 'succeeded' }] } } as never)
    await DELAY(200)
    e()({ type: 'agent_snapshot', snapshot: { records: [
      { executionId: 'a', name: 'c1', status: 'succeeded' },
      { executionId: 'b', name: 'c2', status: 'succeeded' },
    ] } } as never)
    await DELAY(200)
    const markGrow = stdout.chunks.length

    // 动态区变矮：错误行出现再消失
    e()({ type: 'error', message: 'boom' } as never)
    await DELAY(200)
    const markErrOn = stdout.chunks.length
    e()({ type: 'error', message: '' } as never)
    await DELAY(200)
    const markErrOff = stdout.chunks.length

    // 多行输入 -> 单行（输入框变矮）
    stdin.send('a'); await DELAY(80)
    stdin.send('\u001b\r'); await DELAY(80)
    stdin.send('b'); await DELAY(80)
    stdin.send('\u001b\r'); await DELAY(80)
    stdin.send('c'); await DELAY(150)
    const markMulti = stdout.chunks.length
    stdin.send('\u007f'); stdin.send('\u007f'); stdin.send('\u007f'); stdin.send('\u007f'); stdin.send('\u007f')
    await DELAY(200)
    const markShrink = stdout.chunks.length

    // ── 回放比对 ────────────────────────────────────────────────────────
    const screenFull = createScreen(ROWS + 400, COLUMNS)
    const screenDiff = createScreen(ROWS + 400, COLUMNS)

    type Stat = { frames: number; diffs: number; fallbacks: number; mismatch: number; fullBytes: number; diffBytes: number }
    const newStat = (): Stat => ({ frames: 0, diffs: 0, fallbacks: 0, mismatch: 0, fullBytes: 0, diffBytes: 0 })
    const stats: Record<string, Stat> = {
      '撑满视口': newStat(),
      '动态区增长': newStat(),
      '错误行出现': newStat(),
      '错误行消失(变矮)': newStat(),
      '多行输入': newStat(),
      '输入框回缩(变矮)': newStat(),
    }
    const bounds: Array<[number, number, string]> = [
      [0, markTall, '撑满视口'],
      [markTall, markGrow, '动态区增长'],
      [markGrow, markErrOn, '错误行出现'],
      [markErrOn, markErrOff, '错误行消失(变矮)'],
      [markErrOff, markMulti, '多行输入'],
      [markMulti, markShrink, '输入框回缩(变矮)'],
    ]

    let prevRows: string[] = []
    let prevParkUp = 0
    const idxOf = (i: number) => bounds.findIndex(([a, b]) => i >= a && i < b)

    for (let i = 0; i < stdout.chunks.length; i += 1) {
      const chunk = stdout.chunks[i]!
      const info = classify(chunk)
      const segIdx = idxOf(i)
      const stat = segIdx >= 0 ? stats[bounds[segIdx]![2]]! : null

      if (info.kind === 'other') { continue }

      if (info.kind === 'clearTerminal') {
        // 必须原样透传：两种策略都写同一份。
        applyToScreen(screenFull, info.fullPrefix, COLUMNS)
        applyToScreen(screenDiff, info.fullPrefix, COLUMNS)
        if (stat) { stat.frames += 1; stat.fallbacks += 1; stat.fullBytes += info.fullPrefix.length; stat.diffBytes += info.fullPrefix.length }
        prevRows = []
        prevParkUp = 0
        continue
      }

      const unpark = prevParkUp > 0 ? `\u001b[${prevParkUp}B\r` : '\r'
      const full = unpark + eraseLines(prevRows.length + 1) + info.rows.join(NL) + NL + info.park
      const diffBody = buildLineDiff(prevRows, info.rows)
      // null = 该帧不安全，退化全量。
      const diff = diffBody === null ? full : unpark + diffBody + info.park

      applyToScreen(screenFull, full, COLUMNS)
      applyToScreen(screenDiff, diff, COLUMNS)

      if (stat) {
        stat.frames += 1
        if (diffBody === null) stat.fallbacks += 1
        else stat.diffs += 1
        stat.fullBytes += full.length
        stat.diffBytes += diff.length
      }

      // 一致性
      let bad = false
      if (screenFull.rows.length !== screenDiff.rows.length) bad = true
      else {
        for (let r = 0; r < screenFull.rows.length; r += 1) {
          if (screenFull.rows[r]!.trimEnd() !== screenDiff.rows[r]!.trimEnd()) { bad = true; break }
        }
        if (!bad && (screenFull.row !== screenDiff.row || screenFull.col !== screenDiff.col)) bad = true
      }
      if (bad && stat) stat.mismatch += 1

      prevRows = info.rows
      const upM = /\u001b\[(\d+)A/.exec(info.park)
      prevParkUp = upM ? Number(upM[1]) : 0
    }

    console.log(`\n=== 行级 diff 在各边界的表现（终端 ${COLUMNS}x${ROWS}）===`)
    console.log(`  ${'场景'.padEnd(20)} ${'帧'.padStart(4)} ${'diff'.padStart(5)} ${'退化'.padStart(5)} ${'不一致'.padStart(7)} ${'字节省'.padStart(7)}`)
    for (const [label, s] of Object.entries(stats)) {
      if (s.frames === 0) continue
      const saved = s.fullBytes > 0 ? ((1 - s.diffBytes / s.fullBytes) * 100).toFixed(0) + '%' : '-'
      console.log(`  ${label.padEnd(20)} ${String(s.frames).padStart(4)} ${String(s.diffs).padStart(5)} ${String(s.fallbacks).padStart(5)} ${String(s.mismatch).padStart(7)} ${saved.padStart(7)}`)
    }
    const totalMismatch = Object.values(stats).reduce((a, s) => a + s.mismatch, 0)
    console.log(`\n  总不一致帧 = ${totalMismatch}   ${totalMismatch === 0 ? '✅ 全部一致' : '❌ 需在对应边界退化全量重写'}`)
  } finally {
    instance.unmount()
    await DELAY(200)
  }
}

main().catch(err => { console.error(err); process.exit(1) })
