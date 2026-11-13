/**
 * 探针：「不改 ink 源码，只在 stdout 包裹层做行级 diff」能省多少字节？
 *
 * 背景：ink@5.2.1 的 renderer 内部用 `Output` 建了完整 cell 网格，但 `output.get()`
 * 立刻把它摊平成「以 \n 连接的字符串」，log-update 只认识
 * `eraseLines(prevN) + output`。于是 diff 在 ink 里没有挂点。
 *
 * 但 GA 已经拥有 stdout 包裹层（stdoutCursorPark）。它每帧能看到完整帧字符串，
 * 因此可以自己保留上一帧、只重写变化的行 —— 不需要改 ink。
 *
 * 本探针量化这条路的上限：
 *   fullBytes   = 现状（eraseLines(N) + 全量 output）
 *   diffBytes   = 行级 diff（只重写变化的行 + 行间光标移动）
 *   savings     = 1 - diffBytes / fullBytes
 *
 * 附带核对行级 diff 的前提：每行是否自带样式收尾（以 SGR reset 结束）。
 * 若某行结尾遗留颜色，直接重写该行会把颜色漏到右侧 —— 需要在行首补 \x1b[0m。
 *
 * 运行： cd frontends/ink/ui && npx tsx probes/line_diff_potential.ts
 */
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from '../src/App.js'
import { createCursorParkStdout } from '../src/stdoutCursorPark.js'
import type { BridgeClient } from '../src/bridgeClient.js'
import type { BridgeEvent } from '../src/protocol.js'

const NL = '\n'
const DELAY = (ms: number) => new Promise<void>(r => setTimeout(r, ms))

class CaptureWriteStream extends EventEmitter {
  columns = Number(process.env.PROBE_COLUMNS ?? 100)
  rows = Number(process.env.PROBE_ROWS ?? 30)
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

/** eraseLines(N)：ink 用的擦除序列，也是我们要替代的那部分。 */
function eraseLines(count: number): string {
  let s = ''
  for (let i = 0; i < count; i++) s += '\u001b[2K' + (i < count - 1 ? '\u001b[1A' : '')
  if (count) s += '\u001b[G'
  return s
}

type Frame = { rows: string[] }

function parseFrame(chunk: string): Frame | null {
  const inner = chunk.split(BSU).join('').split(ESU).join('')
  if (!inner.includes('\u001b[2K')) return null
  // 帧尾的 park 段（ESC[nA + ESC[mG）不属于内容，切掉。
  const body = inner.slice(inner.lastIndexOf('\u001b[0G') + 4)
  const parkAt = body.search(/\u001b\[\d*A?\u001b\[\d+G$/)
  const content = parkAt >= 0 ? body.slice(0, parkAt) : body
  const rows = content.split(NL)
  if (rows.at(-1) === '') rows.pop()          // ink 的 output 以 \n 结尾
  return { rows }
}

/**
 * 行级 diff 的理想写法：从帧底出发，上移到第一个变化行，逐行重写变化的行。
 * 返回该策略需要写出的字节数。
 */
function lineDiffBytes(prev: string[], next: string[]): { bytes: number; changed: number } {
  let out = ''
  let cursorRow = prev.length          // 现状：写入前光标在上一帧帧底
  let changed = 0
  const max = Math.max(prev.length, next.length)
  for (let row = 0; row < max; row += 1) {
    const before = prev[row] ?? ''
    const after = next[row] ?? ''
    if (before === after) continue
    changed += 1
    const dy = row - cursorRow
    // 行间移动：上行用 CSI A，下行用 CSI B（不可越过帧底时的换行另计，这里取保守估计）。
    out += dy === 0 ? '' : dy < 0 ? `\u001b[${-dy}A` : `\u001b[${dy}B`
    out += '\r\u001b[2K'                 // 回到列 0 并擦掉整行
    out += '\u001b[0m'                   // 行首归零，防止上一行遗留样式漏过来
    out += after
    cursorRow = row
  }
  // 帧变矮：清掉多出来的行。
  for (let row = next.length; row < prev.length; row += 1) {
    out += `\u001b[${cursorRow - row < 0 ? row - cursorRow : 1}B`.replace(/\u001b\[1B/, '\u001b[1B')
    out += '\r\u001b[2K'
    cursorRow = row
  }
  // 回到帧底，让下一帧的相对移动保持不变。
  const backToBottom = next.length - cursorRow
  if (backToBottom > 0) out += `\u001b[${backToBottom}B`
  out += '\r'
  return { bytes: out.length, changed }
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

    // 流式：每帧只有最后一行在变（典型的长 transcript + 单行新增）。
    for (let i = 0; i < 50; i += 1) {
      e()({ type: 'assistant_delta', taskId: 1, text: `streaming line ${i}` + NL })
      await DELAY(20)
    }
    await DELAY(200)

    // 流式中打字
    for (const ch of 'hello') { stdin.send(ch); await DELAY(80) }
    for (let i = 0; i < 3; i += 1) { stdin.send('\u007f'); await DELAY(80) }
    await DELAY(200)

    const frames = stdout.chunks.map(parseFrame).filter((f): f is Frame => f !== null)

    let fullTotal = 0
    let diffTotal = 0
    let changedTotal = 0
    let rowsTotal = 0
    let unbalancedLines = 0
    let totalLines = 0
    for (let i = 0; i < frames.length; i += 1) {
      const cur = frames[i]!
      const prev = i > 0 ? frames[i - 1]! : []
      // 现状：擦除上一帧全部行 + 全量重写本帧。
      fullTotal += eraseLines(prev.length).length + cur.rows.join(NL).length
      const d = lineDiffBytes(prev, cur.rows)
      diffTotal += d.bytes
      changedTotal += d.changed
      rowsTotal += cur.rows.length
      for (const row of cur.rows) {
        totalLines += 1
        // 行级重写安全的前提：行内样式自洽（结尾回到默认样式）。
        // 判据：行末不是「开着颜色却没 reset」。这里用简单近似——最后 10 字符内是否有 reset。
        if (row.includes('\u001b[') && !/\u001b\[0m$|\u001b\[39m$|\u001b\[22m$/.test(row)) unbalancedLines += 1
      }
    }

    console.log(`\n=== 行级 diff 的字节上限（${frames.length} 帧，终端 ${stdout.columns}x${stdout.rows}）===`)
    console.log(`  现状（eraseLines + 全量重写） = ${fullTotal} 字节`)
    console.log(`  行级 diff（只写变化行）        = ${diffTotal} 字节`)
    console.log(`  可省                          = ${((1 - diffTotal / Math.max(1, fullTotal)) * 100).toFixed(1)}%`)
    console.log(`  平均每帧变化行                = ${(changedTotal / Math.max(1, frames.length)).toFixed(2)} / ${(rowsTotal / Math.max(1, frames.length)).toFixed(1)} 行`)
    console.log(`\n=== 行级重写的前提核对 ===`)
    console.log(`  带样式但行末未见 reset 的行 = ${unbalancedLines} / ${totalLines}  (${((unbalancedLines / Math.max(1, totalLines)) * 100).toFixed(1)}%)`)
    console.log(
      unbalancedLines === 0
        ? '  → 行内样式自洽，可直接按行重写'
        : '  → 有行遗留样式，行首必须补 \\x1b[0m（本探针的估算已含这 4 字节）',
    )
    console.log(`\n=== 对照 CC ===`)
    console.log(`  CC 在 fork 的 ink 内部做 cell 级 diff（Patch[]）+ DECSTBM 滚动优化 + 静态区 blit，`)
    console.log(`  行级 diff 拿不到「只改一行里的几个字符」与「滚动整屏用硬件滚动」这两项收益。`)
  } finally {
    instance.unmount()
    await DELAY(200)
  }
}

main().catch(err => { console.error(err); process.exit(1) })
