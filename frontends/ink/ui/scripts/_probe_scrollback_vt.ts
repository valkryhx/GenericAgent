/**
 * Scrollback-faithful virtual terminal for the long-stdout probe.
 *
 * The earlier probes used a growable buffer and allowed CSI up / erase to reach
 * rows that a real terminal has already moved into scrollback. That can report
 * false "missing" rows. Here the visible screen is fixed-height and only the
 * screen is erasable; lines scrolled off the top are immutable history.
 */
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from '../src/App.js'
import { createCursorParkStdout } from '../src/stdoutCursorPark.js'

class W extends EventEmitter {
  isTTY = true
  chunks: string[] = []
  constructor(public columns: number, public rows: number) { super() }
  write(s: string) { this.chunks.push(String(s)); return true }
}
class R extends EventEmitter {
  isTTY = true
  setRawMode() { return this } setEncoding() { return this } ref() { return this }
  unref() { return this } read() { return null } resume() { return this } pause() { return this }
}
const delay = (ms: number) => new Promise<void>(r => setTimeout(r, ms))

class ScrollbackTerminal {
  history: string[] = []
  screen: string[]
  row = 0
  col = 0
  constructor(public columns: number, public rows: number) {
    this.screen = Array.from({ length: rows }, () => '')
  }
  private scroll() {
    const top = this.screen.shift() ?? ''
    this.history.push(top)
    this.screen.push('')
  }
  private put(ch: string) {
    if (this.col >= this.columns) {
      this.col = 0
      if (this.row === this.rows - 1) this.scroll()
      else this.row += 1
    }
    const line = this.screen[this.row] ?? ''
    this.screen[this.row] = line.padEnd(this.col, ' ').slice(0, this.col) + ch + line.slice(this.col + 1)
    this.col += 1
  }
  write(input: string) {
    for (let i = 0; i < input.length; i++) {
      const ch = input[i]!
      if (ch === '\x1b') {
        const m = /^\x1b\[([0-9;?]*)([A-Za-z])/.exec(input.slice(i))
        if (m) {
          this.csi(m[1] ?? '', m[2]!)
          i += m[0].length - 1
          continue
        }
        const osc = /^\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/.exec(input.slice(i))
        if (osc) { i += osc[0].length - 1; continue }
        i += 1
        continue
      }
      if (ch === '\r') { this.col = 0; continue }
      if (ch === '\n') {
        this.col = 0
        if (this.row === this.rows - 1) this.scroll()
        else this.row += 1
        continue
      }
      this.put(ch)
    }
  }
  private csi(raw: string, final: string) {
    if (raw.startsWith('?')) return
    const vals = raw.split(';').map(v => (v === '' ? NaN : Number(v)))
    const arg = (i: number, fallback: number) => {
      const v = vals[i]
      return v !== undefined && Number.isFinite(v) ? v : fallback
    }
    switch (final) {
      case 'A': this.row = Math.max(0, this.row - arg(0, 1)); return
      case 'B': this.row = Math.min(this.rows - 1, this.row + arg(0, 1)); return
      case 'C': this.col = Math.min(this.columns, this.col + arg(0, 1)); return
      case 'D': this.col = Math.max(0, this.col - arg(0, 1)); return
      case 'G': this.col = Math.max(0, Math.min(this.columns - 1, arg(0, 1) - 1)); return
      case 'H':
      case 'f':
        this.row = Math.max(0, Math.min(this.rows - 1, arg(0, 1) - 1))
        this.col = Math.max(0, Math.min(this.columns - 1, arg(1, 1) - 1))
        return
      case 'K': this.screen[this.row] = ''; return
      case 'J': {
        const mode = arg(0, 0)
        if (mode === 2) this.screen = Array.from({ length: this.rows }, () => '')
        if (mode === 3) this.history = []
        return
      }
      default: return
    }
  }
  allRows(): string[] {
    return [...this.history, ...this.screen].map(l => l.trimEnd())
  }
}

const queue: any[] = []
const sbc = (_p: any, _b: any, onEvent: any) => {
  setTimeout(() => onEvent({ type: 'ready', version: 1 }), 0)
  const t = setInterval(() => { const n = queue.shift(); if (n) onEvent(n) }, 15)
  return { send() {}, stop() { clearInterval(t) } }
}
const parkEnabled = process.env.PARK !== '0'
const w = new W(100, 24)
const park = createCursorParkStdout(w as any)
const inst = render(
  React.createElement(App, { python: 'python', bridgeScript: 'x', startBridgeClient: sbc, cursorPark: parkEnabled ? park : undefined } as any),
  { stdout: parkEnabled ? park.stdout as any : w as any, stdin: new R() as any, exitOnCtrlC: false } as any,
)
await delay(200)
queue.push({ type: 'user', taskId: 1, text: '跑大量输出' })
queue.push({ type: 'status', status: 'running', taskId: 1 })
const body = ['我先执行一个脚本。', '[Status] Exit Code: 0', '[Stdout]', '']
for (let i = 0; i < 120; i++) body.push('L' + String(i).padStart(3, '0') + ' stdout payload ........')
const text = body.join('\n'); const size = Math.ceil(text.length / 40)
for (let o = 0; o < text.length; o += size) queue.push({ type: 'assistant_delta', taskId: 1, text: text.slice(o, o + size) })
queue.push({ type: 'assistant_done', taskId: 1, text })
queue.push({ type: 'status', status: 'idle', taskId: 1 })
await delay(3000)
inst.unmount()
await delay(100)

const vt = new ScrollbackTerminal(100, 24)
for (const c of w.chunks) vt.write(c)
const rows = vt.allRows()
const missing: string[] = []
const dup: string[] = []
for (let i = 0; i < 120; i++) {
  const tag = 'L' + String(i).padStart(3, '0') + ' stdout payload'
  const n = rows.filter(l => l.includes(tag)).length
  if (n === 0) missing.push(tag.slice(0, 4))
  if (n > 1) dup.push(tag.slice(0, 4) + 'x' + n)
}
console.log(JSON.stringify({ park: parkEnabled, chunks: w.chunks.length, history: vt.history.length, screen: vt.screen.length, missing, dup }, null, 2))
console.log('last 6 history:')
vt.history.slice(-6).forEach((l, i) => console.log(i, '|' + l.trimEnd()))
console.log('screen:')
vt.screen.forEach((l, i) => console.log(i, '|' + l.trimEnd()))
