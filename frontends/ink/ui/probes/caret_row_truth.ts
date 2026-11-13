/**
 * 探针：park 声明的行 vs 屏幕上真实 caret 行的偏差（「光标下穿到下侧线」的量化证据）。
 *
 * ground truth 的取法不靠模型：在这一帧的纯文本里找到输入框提示行（`>` 行）的
 * **行号索引**，park 应落在那一行（单行输入时 caret 就在 `>` 行）。
 *   declaredUp  = App 声明的 relative-up
 *   actualRows  = ink 本帧写出的内容行数（帧底 = actualRows）
 *   expectedUp  = actualRows - promptLineIndex
 *   up > expectedUp  ⇒ caret 落在 `>` 行上方（偏上）
 *   up < expectedUp  ⇒ caret 落在 `>` 行下方、甚至越过帧底（下穿 / 压到下侧线）
 *
 * 运行： cd frontends/ink/ui && npx tsx probes/caret_row_truth.ts
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

type Row = {
  tag: string
  actualRows: number
  promptIndex: number
  declaredUp: number | null
  expectedUp: number | null
  delta: number | null
  hiddenShown: boolean
}

async function main() {
  const COLUMNS = Number(process.env.PROBE_COLUMNS ?? 100)
  const ROWS = Number(process.env.PROBE_ROWS ?? 30)
  const stdout = new CaptureWriteStream(COLUMNS, ROWS)
  const stderr = new CaptureWriteStream(COLUMNS, ROWS)
  const stdin = new FakeReadStream()
  const park = createCursorParkStdout(stdout as never)

  const rows: Row[] = []
  let pending: Row | null = null
  let hideCount = 0
  let showCount = 0

  const baseWrite = stdout.write.bind(stdout)
  ;(stdout as unknown as { write: (c: unknown) => boolean }).write = (chunk: unknown) => {
    const s = String(chunk)
    hideCount += (s.match(/\u001b\[\?25l/g) ?? []).length
    showCount += (s.match(/\u001b\[\?25h/g) ?? []).length
    if (s.includes('\u001b[2K')) {
      const idx = s.lastIndexOf('\u001b[0G')
      const body = idx >= 0 ? s.slice(idx + 4) : s
      const lines = body.split(NL)
      const actualRows = Math.max(0, lines.length - 1)
      const plain = stripAnsi(body).split(NL)
      let promptIndex = -1
      for (let i = plain.length - 1; i >= 0; i -= 1) {
        if (plain[i]!.trimStart().startsWith('>')) { promptIndex = i; break }
      }
      // caret 在**最后一条输入行**上，而非 `>` 所在行（多行输入时二者不同）。
      // 输入块的上界是 `>` 行上方最近的边框行，下界是下方最近的边框行。
      let caretRow = promptIndex
      if (promptIndex >= 0) {
        for (let i = promptIndex + 1; i < plain.length; i += 1) {
          const t = plain[i]!.trim()
          if (t === '' || /^[─═━-]+$/.test(t)) break
          caretRow = i
        }
      }
      pending = {
        tag: 'frame',
        actualRows,
        promptIndex,
        declaredUp: null,
        expectedUp: promptIndex >= 0 ? actualRows - caretRow : null,
        delta: null,
        hiddenShown: false,
      }
      rows.push(pending)
    } else if (s.includes('\u001b[?25h') && pending) {
      const upM = /^\u001b\[(\d+)A/.exec(s)
      pending.declaredUp = upM ? Number(upM[1]) : 0
      pending.delta = pending.expectedUp === null ? null : pending.declaredUp - pending.expectedUp
      pending.hiddenShown = true
      pending = null
    }
    return baseWrite(chunk)
  }

  let emit: ((e: BridgeEvent) => void) | null = null
  const instance = render(
    React.createElement(App, {
      python: 'python',
      bridgeScript: 'x',
      cursorPark: park,
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
    await delay(400)
    const report = (label: string) => {
      const perf = rows.filter(r => r.declaredUp !== null && r.expectedUp !== null)
      const bad = perf.filter(r => r.delta !== 0)
      console.log(`\n--- ${label} ---  帧=${perf.length}  偏差帧=${bad.length}`)
      const byDelta = new Map<number, number>()
      for (const r of bad) byDelta.set(r.delta!, (byDelta.get(r.delta!) ?? 0) + 1)
      if (byDelta.size) console.log(`  delta 分布 = ${[...byDelta].map(([d, n]) => `${d > 0 ? '+' : ''}${d}:${n}`).join('  ')}`)
      for (const r of perf.slice(-4)) {
        console.log(`  rows=${String(r.actualRows).padStart(2)} promptIdx=${String(r.promptIndex).padStart(2)} declared=${String(r.declaredUp).padStart(2)} expected=${String(r.expectedUp).padStart(2)} delta=${r.delta === 0 ? '0 ' : (r.delta! > 0 ? '+' : '') + r.delta}`)
      }
    }

    // A) 空闲单行
    stdin.send('hey')
    await delay(150)
    report('A 空闲 · 单行输入')

    // B) 运行中流式（用户抱怨的场景）
    e()({ type: 'status', status: 'running', taskId: 1 })
    for (let i = 0; i < 12; i += 1) {
      e()({ type: 'assistant_delta', taskId: 1, text: `stream ${i} ` + 'y'.repeat(30) + NL })
      await delay(40)
    }
    report('B 运行中 · 流式输出')

    // C) 运行中打字 + 回退
    stdin.send('a')
    await delay(90)
    stdin.send('b')
    await delay(90)
    stdin.send('\u007f')
    await delay(90)
    report('C 运行中 · 打字与回退')

    // D) 斜杠面板（slash suggestions，占 bottom 行但不设 activePanel）
    stdin.send('\u0015') // Ctrl+U clear
    await delay(80)
    stdin.send('/')
    await delay(200)
    report('D 斜杠候选面板展开')

    // E) 多行输入
    stdin.send('\u0015')
    await delay(60)
    stdin.send('line1')
    await delay(60)
    stdin.send('\u001b\r') // Alt+Enter newline
    await delay(60)
    stdin.send('line2')
    await delay(120)
    report('E 多行输入')

    // F) 子代理通知（noticeRows 影响 bottomRows）
    e()({ type: 'agent_snapshot', snapshot: { records: [{ executionId: 'x1', name: 'child', status: 'succeeded' }] } } as never)
    await delay(200)
    report('F subagent 通知')

    console.log(`\n=== 光标可见性抖动 ===`)
    console.log(`  HIDE(\\x1b[?25l) 次数 = ${hideCount}   SHOW(\\x1b[?25h) 次数 = ${showCount}`)
    console.log(`  → 每帧都 HIDE 再 SHOW 一次；流式时以帧率重复，肉眼即「光标一直闪」`)
  } finally {
    instance.unmount()
    await delay(150)
  }
}

main().catch(err => { console.error(err); process.exit(1) })
