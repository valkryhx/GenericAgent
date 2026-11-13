/**
 * 探针：park 规格是否与「它紧跟的那一帧」的内容一致？（一帧延迟 / 陈旧 DOM 的决定性判据）
 *
 * 对每一帧做配对：frame(k) 紧跟 park(k)。
 *   期望列（1-based）：输入框提示行 `>` 之后、caret 字符右边那一列。
 *   实测列：park(k) 里的 ESC[<n>G。
 * 两者不等 ⇒ park 用的是**另一次 render** 的 caret（陈旧规格）。
 *   实测 < 期望 ⇒ park 落后（光标停在 caret 左边）
 *   实测 > 期望 ⇒ park 超前（光标停在 caret 右边，甚至越过行尾/下侧线）
 *
 * 运行： cd frontends/ink/ui && npx tsx probes/park_staleness.ts
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
  send(t: string) { this.queue.push(t); this.emit('readable') }
}

const stripAnsi = (t: string) => t
  .replace(/\u001B\][^\u0007]*(?:\u0007|\u001B\\)/g, '')
  .replace(/\u001B\[[0-?]*[ -/]*[@-~]/g, '')
  .replace(/\u001B[=>]/g, '')

/** 从一帧正文取输入行里 `>` 之后的内容、caret 期望列（1-based 终端列号）与 `>` 所在行号。 */
function caretExpectation(body: string): { text: string; expectCol: number; promptRow: number; rows: number } | null {
  const lines = stripAnsi(body).split(NL)
  const rows = Math.max(0, lines.length - 1)
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    const raw = lines[i]!
    const idx = raw.indexOf('>')
    if (idx >= 0) {
      // 输入行形如 " > hello"；`>` 本身占 1 列，其后紧跟输入文本。
      const after = raw.slice(idx + 1)
      const text = after.replace(/\s+$/, '')
      // 终端列号 1-based：`>` 在 1-based 列 idx+1，caret 在其后第 text.length+1 列。
      const expectCol = idx + 1 + text.length + 1
      return { text, expectCol, promptRow: i, rows }
    }
  }
  return null
}

type Pair = {
  frameBody: string
  text: string
  expectCol: number
  promptRow: number
  rows: number
  /** park 落到的绝对行（相对本帧内容顶）= rows - up */
  parkRow: number | null
  parkCol: number | null
  parkUp: number | null
  frameBytes: number
}

async function main() {
  const stdout = new CaptureWriteStream(Number(process.env.PROBE_COLUMNS ?? 100), Number(process.env.PROBE_ROWS ?? 30))
  const stderr = new CaptureWriteStream(100, 30)
  const stdin = new FakeReadStream()
  const park = createCursorParkStdout(stdout as never)

  const pairs: Pair[] = []
  let open: Pair | null = null

  const baseWrite = stdout.write.bind(stdout)
  ;(stdout as unknown as { write: (c: unknown) => boolean }).write = (chunk: unknown) => {
    const s = String(chunk)
    if (s.includes('\u001b[2K')) {
      const idx = s.lastIndexOf('\u001b[0G')
      const body = idx >= 0 ? s.slice(idx + 4) : s
      const exp = caretExpectation(body)
      if (exp) {
        open = {
          frameBody: body, text: exp.text, expectCol: exp.expectCol,
          promptRow: exp.promptRow, rows: exp.rows,
          parkRow: null, parkCol: null, parkUp: null, frameBytes: s.length,
        }
        pairs.push(open)
      } else {
        open = null
      }
    } else if (s.includes('\u001b[?25h') && open && open.parkCol === null) {
      const col = /\u001b\[(\d+)G/.exec(s)
      const up = /^\u001b\[(\d+)A/.exec(s)
      open.parkCol = col ? Number(col[1]) : null
      open.parkUp = up ? Number(up[1]) : 0
      // park 上移 up 行 → 落点行号 = rows - up（rows 之后是帧底那一空行）
      open.parkRow = open.parkUp === null ? null : open.rows - open.parkUp
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

  const summarize = (label: string, from: number) => {
    const slice = pairs.slice(from).filter(p => p.parkCol !== null)
    const exact = slice.filter(p => p.parkCol === p.expectCol)
    const behind = slice.filter(p => p.parkCol! < p.expectCol!)
    const ahead = slice.filter(p => p.parkCol! > p.expectCol!)
    const rowBad = slice.filter(p => p.parkRow !== p.promptRow)
    console.log(`\n--- ${label} ---  配对帧=${slice.length}  列一致=${exact.length}  列落后=${behind.length}  列超前=${ahead.length}  行不符=${rowBad.length}`)
    for (const p of [...behind, ...ahead, ...rowBad].slice(0, 8)) {
      console.log(`   text=${JSON.stringify(p.text)} expectCol=${p.expectCol} parkCol=${p.parkCol} promptRow=${p.promptRow} parkRow=${p.parkRow} rows=${p.rows} up=${p.parkUp}  ${p.parkCol! < p.expectCol! ? '<<列落后' : p.parkCol! > p.expectCol! ? '<<列超前' : ''} ${p.parkRow !== p.promptRow ? '<<行不符' : ''}`)
    }
    if (!behind.length && !ahead.length && !rowBad.length && slice.length) {
      const sample = slice.slice(-3)
      for (const p of sample) console.log(`   OK text=${JSON.stringify(p.text)} expectCol=${p.expectCol} parkCol=${p.parkCol} promptRow=${p.promptRow} parkRow=${p.parkRow}`)
    }
  }

  try {
    await delay(400)
    let mark = pairs.length
    stdin.send('hello')
    await delay(300)
    summarize('A 空闲逐字输入 hello', mark)

    // 关键场景：流式输出中逐字输入
    e()({ type: 'status', status: 'running', taskId: 1 })
    e()({ type: 'user', taskId: 1, text: 'go' })
    await delay(120)
    mark = pairs.length
    for (const ch of 'abcde'.split('')) {
      stdin.send(ch)
      await delay(45)          // 人手速，接近 ink 的 32ms 帧间隔
      e()({ type: 'assistant_delta', taskId: 1, text: 'streaming line' + NL })
      await delay(45)
    }
    await delay(200)
    summarize('B 流式中逐字输入 abcde（44ms/字符 → 接近 32ms 节流窗口）', mark)

    // 极快连打（一个事件轮次里 3 个字符）
    mark = pairs.length
    stdin.send('x'); stdin.send('y'); stdin.send('z')
    await delay(250)
    summarize('C 快速连打 xyz（同一轮次内）', mark)

    // 回退
    mark = pairs.length
    stdin.send('\u007f'); await delay(80)
    stdin.send('\u007f'); await delay(80)
    stdin.send('\u007f'); await delay(200)
    summarize('D 连续三次回退', mark)

    // 流式高峰：不停喂 delta，同时打字
    mark = pairs.length
    const pump = (async () => {
      for (let i = 0; i < 20; i += 1) {
        e()({ type: 'assistant_delta', taskId: 1, text: `hi-${i}` + NL })
        await delay(16)
      }
    })()
    for (const ch of 'qwert'.split('')) { stdin.send(ch); await delay(50) }
    await pump
    await delay(250)
    summarize('E 流式高峰（16ms/delta）中打字 qwert', mark)

    const all = pairs.filter(p => p.parkCol !== null)
    const bad = all.filter(p => p.parkCol !== p.expectCol)
    const badRow = all.filter(p => p.parkRow !== p.promptRow)
    console.log(`\n=== 汇总 ===`)
    console.log(`  配对帧 ${all.length}  列不一致 ${bad.length}  (${all.length ? ((bad.length / all.length) * 100).toFixed(0) : 0}%)  行不一致 ${badRow.length}`)
  } finally {
    instance.unmount()
    await delay(150)
  }
}

main().catch(err => { console.error(err); process.exit(1) })
