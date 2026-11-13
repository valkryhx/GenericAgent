/**
 * 探针：打字时原生光标（park）落后输入框反显块几个字符？一帧写多少字节？
 *
 * 判据全部是 stdout 字节里的确定性事实：
 *  1. 出现 "ab" 的那一帧之后，park 写出的列（ESC[<n>G）对应 caret 在 "ab" 之后还是 "a"
 *     之后 —— 落后一字符 ⇒ 这一帧用的是上一次 render 声明的 setPark 规格。
 *  2. 每个输入字符触发多少次 write / 多少字节 —— 整帧重写 vs 局部 diff 的量化证据。
 *
 * 运行： cd frontends/ink/ui && npx tsx probes/caret_lag_probe.ts
 */
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from '../src/App.js'
import { createCursorParkStdout } from '../src/stdoutCursorPark.js'
import type { BridgeClient } from '../src/bridgeClient.js'
import type { BridgeEvent } from '../src/protocol.js'

class CaptureWriteStream extends EventEmitter {
  columns = 100
  rows = 30
  isTTY = true
  chunks: string[] = []
  write(chunk: unknown): boolean {
    this.chunks.push(String(chunk))
    return true
  }
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

const delay = (ms: number) => new Promise<void>(r => setTimeout(r, ms))

type Rec =
  | { kind: 'frame'; text: string; bytes: number }
  | { kind: 'park'; col: number; up: number; bytes: number }
  | { kind: 'unpark'; raw: string; bytes: number }
  | { kind: 'other'; raw: string; bytes: number }

async function main() {
  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
  const stdin = new FakeReadStream()
  const park = createCursorParkStdout(stdout as never)

  const recs: Rec[] = []
  // park 的 Proxy 最终会写到底层 sink；包底层 write 即可拿到终端看到的真实顺序。
  const baseWrite = stdout.write.bind(stdout)
  ;(stdout as unknown as { write: (c: unknown) => boolean }).write = (chunk: unknown) => {
    const s = String(chunk)
    if (s.includes('\u001b[2K')) {
      recs.push({ kind: 'frame', text: stripAnsi(s), bytes: s.length })
    } else if (s.includes('\u001b[?25h')) {
      const par = /^\u001b\[(\d+)A/.exec(s)
      const col = /\u001b\[(\d+)G/.exec(s)
      recs.push({ kind: 'park', col: col ? Number(col[1]) : -1, up: par ? Number(par[1]) : 0, bytes: s.length })
    } else if (s.includes('\u001b[?25l')) {
      recs.push({ kind: 'unpark', raw: JSON.stringify(s), bytes: s.length })
    } else {
      recs.push({ kind: 'other', raw: JSON.stringify(s.slice(0, 40)), bytes: s.length })
    }
    return baseWrite(chunk)
  }

  let emit: ((e: BridgeEvent) => void) | null = null
  const startBridgeClient = (
    _p: string,
    _b: string,
    onEvent: (e: BridgeEvent) => void,
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
    // 进入 running：用户抱怨的正是「回复中打字 / 回退」。
    e({ type: 'status', status: 'running', taskId: 1 })
    await delay(250)

    // 字符间隔 >32ms（典型人手速），使 ink 的 throttle 每次走 leading 分支。
    const markA = recs.length
    stdin.send('a')
    await delay(90)
    const markB = recs.length
    stdin.send('b')
    await delay(90)
    const markC = recs.length

    stdin.send('\u007f')          // Backspace
    await delay(140)
    const markD = recs.length

    seg(recs, markA, markB, "输入 'a'", 'a')
    seg(recs, markB, markC, "输入 'b'", 'ab')
    seg(recs, markC, markD, '回退 (Backspace)', 'a')

    const idleStart = recs.length
    await delay(1000)
    console.log(`\n[idle 1000ms] writes = ${recs.length - idleStart}`)
  } finally {
    instance.unmount()
    await delay(120)
  }
}

function seg(recs: Rec[], from: number, to: number, label: string, expectText: string) {
  const slice = recs.slice(from, to)
  const frames = slice.filter(r => r.kind === 'frame') as Extract<Rec, { kind: 'frame' }>[]
  const parks = slice.filter(r => r.kind === 'park') as Extract<Rec, { kind: 'park' }>[]
  const unparks = slice.filter(r => r.kind === 'unpark')
  const others = slice.filter(r => r.kind === 'other')
  const bytes = slice.reduce((a, r) => a + r.bytes, 0)

  console.log(`\n=== ${label} ===`)
  console.log(`  writes=${slice.length}  frames=${frames.length}  parks=${parks.length}  unparks=${unparks.length}  other=${others.length}  bytes=${bytes}`)
  if (frames.length) {
    const avg = Math.round(frames.reduce((a, f) => a + f.bytes, 0) / frames.length)
    console.log(`  单帧字节 avg=${avg} max=${Math.max(...frames.map(f => f.bytes))}`)
  }
  if (unparks.length) console.log(`  unpark 序列 = ${unparks.slice(0, 6).map(u => u.raw).join(' ')}`)
  console.log(`  park 列序列 = [${parks.map(p => p.col).join(', ')}]  up 序列 = [${parks.map(p => p.up).join(', ')}]`)

  // 找该段内第一条输入行包含期望文本的帧 → 紧接其后的 park 列。
  const idx = recs.findIndex((r, i) => i >= from && i < to && r.kind === 'frame' && inputLine(r.text).includes(`> ${expectText}`))
  if (idx < 0) {
    const lines = frames.map(f => inputLine(f.text)).filter(Boolean)
    console.log(`  (未找到输入行含 "> ${expectText}" 的帧；见过的输入行: ${JSON.stringify([...new Set(lines)])})`)
    return
  }
  const frame = recs[idx] as Extract<Rec, { kind: 'frame' }>
  const parkAfter = recs.slice(idx, to).find(r => r.kind === 'park') as Extract<Rec, { kind: 'park' }> | undefined
  console.log(`  含 "> ${expectText}" 的帧 = ${JSON.stringify(inputLine(frame.text))}`)
  console.log(`  该帧之后的第一个 park 列 = ${parkAfter ? parkAfter.col : '(无)'}  ← caret 在 "> " 之后第 ${expectText.length + 1} 列`)
}

/** 从一帧纯文本里取输入框提示行（含 '>' 的那行）。 */
function inputLine(text: string): string {
  const lines = text.split('\n')
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    const l = lines[i]!
    if (l.trimStart().startsWith('>')) return l.trimEnd()
  }
  return ''
}

main().catch(err => { console.error(err); process.exit(1) })
