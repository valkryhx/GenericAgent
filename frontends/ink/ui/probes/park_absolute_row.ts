/**
 * 探针：park 落到的那一行，在**真实屏幕**上是哪一行？是不是输入框的框内行？
 *
 * 这是「光标下穿到输入框下侧线」的唯一权威判据，且完全不依赖 GA 自己的布局模型：
 *   1. 用 virtualScreen 把墨水的写入串回放成字符网格（ground truth）；
 *   2. 在网格里定位输入框：含 `>` 的那一行是输入首行，其上/下的一行应是边框（─）；
 *   3. 把 park 的相对上移换算成绝对行，检查该行是否落在
 *      [borderTop, borderBottom] 之间（框内），还是落在 borderBottom 上/之外（下穿）。
 *
 * 覆盖会让 bottom 行数变化的全部状态：error / notice / retry / goal / 多行输入 / 面板。
 *
 * 运行： cd frontends/ink/ui && npx tsx probes/park_absolute_row.ts
 */
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from '../src/App.js'
import { createCursorParkStdout } from '../src/stdoutCursorPark.js'
import { applyToScreen, createScreen, type Screen } from '../src/virtualScreen.js'
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

type Verdict = {
  label: string
  frameTopRow: number
  parkAbsRow: number
  composerTop: number
  composerBottom: number
  caretRowInFrame: number
  actualRows: number
  verdict: 'inside' | 'on-bottom-border' | 'below-composer' | 'no-composer' | 'no-park'
  rowText: string
}

async function main() {
  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
  const stdin = new FakeReadStream()
  const park = createCursorParkStdout(stdout as never)

  // 屏幕与「帧顶绝对行」的追踪：ink 每帧从帧顶开始重绘，帧顶 = 上一帧的帧顶。
  const screen: Screen = createScreen(ROWS + 60, COLUMNS)
  let frameTop = 0
  let frameOpen: { topRow: number; rows: number } | null = null
  const verdicts: Verdict[] = []
  let label = 'startup'

  const baseWrite = stdout.write.bind(stdout)
  ;(stdout as unknown as { write: (c: unknown) => boolean }).write = (chunk: unknown) => {
    const s = String(chunk)
    if (s.includes('\u001b[2K')) {
      // 一帧：先数擦了多少行（= 上一帧高度），据此确定本帧帧顶。
      const eraseCount = (s.match(/\u001b\[2K/g) ?? []).length
      const idx = s.lastIndexOf('\u001b[0G')
      const body = idx >= 0 ? s.slice(idx + 4) : s
      const rows = Math.max(0, body.split(NL).length - 1)
      // 擦除从「当前光标所在行」往上 eraseCount 行。
      const bottomBefore = screen.row
      frameTop = Math.max(0, bottomBefore - eraseCount + 1)
      screen.row = frameTop
      screen.col = 0
      applyToScreen(screen, body, COLUMNS)
      frameOpen = { topRow: frameTop, rows }
    } else if (s.includes('\u001b[?25h') && frameOpen) {
      const upM = /^\u001b\[(\d+)A/.exec(s)
      const up = upM ? Number(upM[1]) : 0
      const frameBottomRow = frameOpen.topRow + frameOpen.rows
      const parkAbsRow = frameBottomRow - up
      const v = analyze(label, frameOpen.topRow, frameOpen.rows, parkAbsRow, up, screen)
      verdicts.push(v)
      frameOpen = null
    }
    return baseWrite(chunk)
  }

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

  const run = async (name: string, fn: () => Promise<void>) => {
    label = name
    await fn()
    await delay(180)
  }

  try {
    await delay(400)
    await run('1 空闲', async () => { stdin.send('hi') })
    await run('2 运行中', async () => { e()({ type: 'status', status: 'running', taskId: 1 }) })
    await run('3 流式输出', async () => {
      for (let i = 0; i < 8; i += 1) {
        e()({ type: 'assistant_delta', taskId: 1, text: `out ${i}` + NL })
        await delay(30)
      }
    })
    await run('4 流式中打字+回退', async () => {
      stdin.send('a'); await delay(70)
      stdin.send('\u007f'); await delay(70)
    })
    await run('5 出错（error 行占 1 行）', async () => {
      e()({ type: 'error', message: 'upstream 502' } as never)
    })
    await run('6 清除错误', async () => {
      e()({ type: 'error', message: '' } as never)
    })
    await run('7 多行输入', async () => {
      stdin.send('\u0015'); await delay(60)
      stdin.send('l1')
      await delay(60)
      stdin.send('\u001b\r')
      await delay(60)
      stdin.send('l2')
    })
    await run('8 斜杠面板', async () => {
      stdin.send('\u0015'); await delay(60)
      stdin.send('/')
    })
    await run('9 关闭面板', async () => {
      stdin.send('\u001b')
      await delay(60)
      stdin.send('\u0015')
    })
    await run('10 流式高峰中打字', async () => {
      const p = (async () => {
        for (let i = 0; i < 25; i += 1) { e()({ type: 'assistant_delta', taskId: 1, text: `p${i}` + NL }); await delay(16) }
      })()
      stdin.send('w'); await delay(60)
      stdin.send('x'); await delay(60)
      await p
    })
    // 关键场景：底部带（bottom band）行数变化的瞬间 —— 内容整体上移一格，
    // 若 park 规格落后一帧，原生光标就会正好停在输入框的下边框上。
    await run('12 子代理通知出现（bottom +1 行）', async () => {
      e()({ type: 'agent_snapshot', snapshot: { records: [{ executionId: 'x1', name: 'child', status: 'succeeded' }] } } as never)
    })
    await run('13 再补一条通知（bottom +1 行）', async () => {
      e()({ type: 'agent_snapshot', snapshot: { records: [
        { executionId: 'x1', name: 'child', status: 'succeeded' },
        { executionId: 'x2', name: 'child2', status: 'succeeded' },
      ] } } as never)
    })
    await run('14 错误行出现（bottom +1 行）', async () => {
      e()({ type: 'error', message: 'upstream 502' } as never)
    })
    await run('15 错误行消失（bottom -1 行）', async () => {
      e()({ type: 'error', message: '' } as never)
    })
    await run('11 回合结束', async () => {
      e()({ type: 'assistant_done', taskId: 1, text: 'done' } as never)
      e()({ type: 'status', status: 'idle', taskId: 1 })
    })

    // ── 按状态汇总 ────────────────────────────────────────────────────────
    console.log(`\n终端 ${COLUMNS}x${ROWS}  总配对帧=${verdicts.length}\n`)
    const counts: Record<string, number> = {}
    for (const v of verdicts) {
      counts[v.verdict] = (counts[v.verdict] ?? 0) + 1
      if (v.verdict !== 'inside') {
        console.log(`  <<${v.verdict}>>  [${v.label}]`)
        console.log(`     帧顶=${v.frameTopRow} 高度=${v.actualRows} 光标绝对行=${v.parkAbsRow} 框内=[${v.composerTop}..${v.composerBottom}]`)
        console.log(`     该行内容 = ${JSON.stringify(v.rowText)}`)
      }
    }
    console.log(`\n=== 结论分布 ===`)
    for (const [k, n] of Object.entries(counts).sort((a, b) => b[1] - a[1])) {
      console.log(`  ${k.padEnd(18)} ${n}`)
    }
    const bad = counts['on-bottom-border'] ?? 0
    const below = counts['below-composer'] ?? 0
    console.log(`\n  下穿（落在下边框 / 更下）= ${bad + below} / ${verdicts.length}`)
  } finally {
    instance.unmount()
    await delay(150)
  }
}

/** 在屏幕网格里找输入框并判定 park 行。 */
function analyze(
  label: string,
  topRow: number,
  rows: number,
  parkAbsRow: number,
  up: number,
  screen: Screen,
): Verdict {
  const promptRow = findPromptRow(screen, topRow, rows)
  const rowText = (screen.rows[parkAbsRow] ?? '').trimEnd()
  if (promptRow < 0) {
    return { label, frameTopRow: topRow, parkAbsRow, composerTop: -1, composerBottom: -1, caretRowInFrame: rows - up, actualRows: rows, verdict: 'no-composer', rowText }
  }
  // 输入框：prompt 行之上最近的一条边框行 = top；之下最近的一条 = bottom。
  let top = promptRow
  while (top > 0 && isBorder(screen.rows[top - 1] ?? '')) top -= 1
  let bottom = promptRow
  const limit = topRow + rows
  while (bottom < limit - 1 && !isBorder(screen.rows[bottom + 1] ?? '')) bottom += 1
  const composerBottom = isBorder(screen.rows[bottom + 1] ?? '') ? bottom + 1 : bottom

  let verdict: Verdict['verdict'] = 'inside'
  if (parkAbsRow === composerBottom) verdict = 'on-bottom-border'
  else if (parkAbsRow > composerBottom) verdict = 'below-composer'

  return {
    label, frameTopRow: topRow, parkAbsRow,
    composerTop: top, composerBottom,
    caretRowInFrame: rows - up, actualRows: rows,
    verdict, rowText,
  }
}

function findPromptRow(screen: Screen, topRow: number, rows: number): number {
  const limit = Math.min(screen.rows.length, topRow + rows)
  for (let i = limit - 1; i >= Math.max(0, topRow); i -= 1) {
    const l = screen.rows[i] ?? ''
    if (l.trimStart().startsWith('>')) return i
  }
  return -1
}

function isBorder(line: string): boolean {
  const t = line.trim()
  return t.length > 0 && /^[─═━-]+$/.test(t)
}

main().catch(err => { console.error(err); process.exit(1) })
