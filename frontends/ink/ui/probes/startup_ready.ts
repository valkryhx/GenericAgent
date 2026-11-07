/**
 * 抓 full 模式启动阶段的逐帧时间线：输入框行号 + 是否含 Ready. + 底栏行数。
 *
 * 用户报告：「full 模式下启动时输入框确实在下面，但会被 ready 搞得移动一次」。
 * 帧高恒为 39 说明不是帧高变化，所以要找的是**同一帧内部**输入框相对位置的变化。
 */
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from '../src/App.js'
import type { BridgeClient } from '../src/bridgeClient.js'
import type { BridgeEvent } from '../src/protocol.js'

const NL = String.fromCharCode(10)
const delay = (ms: number) => new Promise(r => setTimeout(r, ms))

class CapWrite extends EventEmitter {
  columns = 110
  rows = 40
  chunks: string[] = []
  isTTY = true
  write(c: unknown): boolean {
    this.chunks.push(String(c))
    return true
  }
}

class FakeRead extends EventEmitter {
  isTTY = true
  private queue: string[] = []
  setRawMode() { return this }
  setEncoding() { return this }
  ref() { return this }
  unref() { return this }
  read(): string | null { return this.queue.shift() ?? null }
  resume() { return this }
  pause() { return this }
  send(s: string) { this.queue.push(s); this.emit('readable') }
}

function stripAnsi(s: string): string {
  return s
    .replace(/\u001b\][^\u0007]*(?:\u0007|\u001b\\)/g, '')
    .replace(/\u001b\[[0-?]*[ -/]*[@-~]/g, '')
}

function frame(data: string): string[] {
  const lines = stripAnsi(data).split(NL)
  while (lines.length > 0 && lines[lines.length - 1]!.trim() === '') lines.pop()
  return lines
}

type Row = { n: number; rows: number; composer: number; hasReady: boolean; hasMcp: boolean; top: string }

async function main() {
  process.env.GA_INK_MOUSE = 'full'

  let emit: ((e: BridgeEvent) => void) | null = null
  const startBridgeClient = (
    _p: string, _s: string, onEvent: (e: BridgeEvent) => void,
  ): BridgeClient => {
    emit = onEvent
    // 模拟真实 bridge：ready 先到，MCP 稍后才报
    setTimeout(() => onEvent({ type: 'ready', version: 1 }), 0)
    return { send() {}, stop() {} }
  }

  const stdout = new CapWrite()
  const history: Row[] = []
  let counter = 0
  const origWrite = stdout.write.bind(stdout)
  stdout.write = (c: unknown): boolean => {
    const r = origWrite(c)
    counter += 1
    const lines = frame(String(c))
    let composer = -1
    for (let i = lines.length - 1; i >= 0; i -= 1) {
      const t = lines[i]!.trim()
      if (t.length >= 8 && [...t].every(ch => ch === '─')) { composer = i; break }
    }
    let top = ''
    for (let i = 0; i < lines.length; i += 1) {
      if (lines[i]!.trim()) { top = lines[i]!.trim(); break }
    }
    history.push({
      n: counter,
      rows: lines.length,
      composer,
      hasReady: lines.some(l => l.trim() === 'Ready.'),
      hasMcp: lines.some(l => l.includes('MCP')),
      top: top.slice(0, 30),
    })
    return r
  }

  const inst = render(React.createElement(App, {
    python: 'python', bridgeScript: 'bridge.py', startBridgeClient,
  }), {
    stdout: stdout as unknown as NodeJS.WriteStream,
    stderr: new CapWrite() as unknown as NodeJS.WriteStream,
    stdin: new FakeRead() as unknown as NodeJS.ReadStream,
    patchConsole: false,
  })

  try {
    await delay(600)
    const startup = history.slice()
    console.log('=== 启动阶段逐帧 ===')
    console.log('  #  帧高  输入框行   Ready?  MCP?   首条内容')
    for (const h of startup) {
      console.log(
        `  ${String(h.n).padStart(2)}  ${String(h.rows).padStart(3)}  ${String(h.composer).padStart(7)}`
        + `   ${h.hasReady ? 'YES  ' : '     '}  ${h.hasMcp ? 'YES ' : '    '}  ${h.top}`,
      )
    }

    const sink = emit as unknown as (e: BridgeEvent) => void
    sink({ type: 'status', status: 'running', taskId: 1 })
    sink({ type: 'user', taskId: 1, text: 'Q' })
    await delay(500)

    const after = history.slice(startup.length)
    console.log('\n=== 提交后 ===')
    for (const h of after) {
      console.log(
        `  ${String(h.n).padStart(2)}  ${String(h.rows).padStart(3)}  ${String(h.composer).padStart(7)}`
        + `   ${h.hasReady ? 'YES  ' : '     '}  ${h.hasMcp ? 'YES ' : '    '}  ${h.top}`,
      )
    }

    console.log('\n=== 输入框行号变化轨迹 ===')
    let prev = -1
    for (const h of history) {
      if (h.composer !== prev) {
        console.log(`  #${h.n}: ${prev === -1 ? '起始' : prev} -> ${h.composer}   (Ready=${h.hasReady}, MCP=${h.hasMcp})`)
        prev = h.composer
      }
    }
  } finally {
    inst.unmount()
    delete process.env.GA_INK_MOUSE
  }
}

main().catch(e => { console.error(e); process.exit(1) })
