/**
 * 绕开 winpty，直接验证滚轮 → 滚动 offset 的完整分发链路。
 *
 * 背景：PTY 抓包显示 winpty 会拦截鼠标序列（ESC[<64;10;10M 被完全吞掉，
 * ESC[M 被剥成 3 个载荷字节），所以真机探针无法裁决。改用 Ink 的 FakeReadStream
 * 直接投喂输入，看 App 是否真的滚动。
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
  columns = 100
  rows = 30
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

function visible(data: string): string[] {
  const lines = stripAnsi(data).split(NL)
  while (lines.length > 0 && lines[lines.length - 1]!.trim() === '') lines.pop()
  return lines
}

async function main() {
  const prev = process.env.GA_INK_MOUSE
  process.env.GA_INK_MOUSE = 'full'

  let emit: ((e: BridgeEvent) => void) | null = null
  const startBridgeClient = (
    _p: string, _s: string, onEvent: (e: BridgeEvent) => void,
  ): BridgeClient => {
    emit = onEvent
    setTimeout(() => onEvent({ type: 'ready', version: 1 }), 0)
    return { send() {}, stop() {} }
  }

  const stdout = new CapWrite()
  const stderr = new CapWrite()
  const stdin = new FakeRead()

  const inst = render(React.createElement(App, {
    python: 'python', bridgeScript: 'bridge.py', startBridgeClient,
  }), {
    stdout: stdout as unknown as NodeJS.WriteStream,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    patchConsole: false,
  })

  const lastFrame = () => visible(stdout.chunks[stdout.chunks.length - 1] ?? '')
  const bodyFirst = (lines: string[]) => {
    for (let i = 1; i < lines.length; i += 1) if (lines[i]!.trim()) return lines[i]!
    return ''
  }

  try {
    await delay(300)
    const sink = emit as unknown as (e: BridgeEvent) => void
    sink({ type: 'status', status: 'running', taskId: 1 })
    sink({ type: 'user', taskId: 1, text: 'Q' })
    // 灌 60 行，保证可滚动
    for (let i = 1; i <= 60; i += 1) {
      sink({ type: 'assistant_delta', taskId: 1, text: `LINE-${i}` + NL })
    }
    sink({ type: 'assistant_done', taskId: 1, text: '' })
    sink({ type: 'status', status: 'idle', taskId: 1 })
    await delay(600)

    const base = lastFrame()
    console.log(`基线：帧 ${base.length} 行`)
    console.log(`基线首条内容: ${JSON.stringify(bodyFirst(base).trim().slice(0, 40))}`)

    // --- 滚轮上 ---
    stdin.send('\u001b[<64;10;10M')
    await delay(400)
    const afterWheel = lastFrame()
    const wheelMoved = bodyFirst(base) !== bodyFirst(afterWheel)
    console.log(`\n滚轮上 1 格后：`)
    console.log(`  首条内容: ${JSON.stringify(bodyFirst(afterWheel).trim().slice(0, 40))}`)
    console.log(`  是否滚动: ${wheelMoved}`)

    // 再滚 5 格
    for (let i = 0; i < 5; i += 1) {
      stdin.send('\u001b[<64;10;10M')
      await delay(60)
    }
    await delay(400)
    const afterWheel6 = lastFrame()
    console.log(`滚轮再 5 格后首条内容: ${JSON.stringify(bodyFirst(afterWheel6).trim().slice(0, 40))}`)

    // --- End 跳回底部 ---
    stdin.send('\u001b[F')
    await delay(500)
    const afterEnd = lastFrame()
    const backToBottom = bodyFirst(afterEnd) === bodyFirst(base)
    console.log(`\nEnd 之后：`)
    console.log(`  首条内容: ${JSON.stringify(bodyFirst(afterEnd).trim().slice(0, 40))}`)
    console.log(`  是否回到基线（底部）: ${backToBottom}`)

    // --- PgUp / PgDn 对照 ---
    stdin.send('\u001b[5~')
    await delay(400)
    const afterPgUp = lastFrame()
    console.log(`\nPgUp 之后首条内容: ${JSON.stringify(bodyFirst(afterPgUp).trim().slice(0, 40))}`)
    console.log(`  PgUp 有效: ${bodyFirst(afterPgUp) !== bodyFirst(afterEnd)}`)

    console.log('\n=== 判定 ===')
    console.log(`  滚轮滚动历史: ${wheelMoved ? '✓' : '✗'}`)
    console.log(`  End 跳回底部: ${backToBottom ? '✓' : '✗'}`)
  } finally {
    inst.unmount()
    if (prev === undefined) delete process.env.GA_INK_MOUSE
    else process.env.GA_INK_MOUSE = prev
  }
}

main().catch(e => { console.error(e); process.exit(1) })
