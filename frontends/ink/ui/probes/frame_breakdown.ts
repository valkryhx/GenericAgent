/**
 * 探针：逐帧记录「消息区高度」和「底栏高度」，找出到底是哪一个在变。
 * 用真实的 App 组件 + fake stream，不经过终端。
 */
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from '../src/App.js'
import type { BridgeClient } from '../src/bridgeClient.js'
import type { BridgeEvent } from '../src/protocol.js'

const NL = String.fromCharCode(10)
const delay = (ms: number) => new Promise(r => setTimeout(r, ms))

class CaptureWriteStream extends EventEmitter {
  columns = 120
  rows = 40
  chunks: string[] = []
  isTTY = true
  write(chunk: unknown): boolean {
    this.chunks.push(String(chunk))
    return true
  }
}

class FakeReadStream extends EventEmitter {
  isTTY = true
  setRawMode() { return this }
  setEncoding() { return this }
  ref() { return this }
  unref() { return this }
  read() { return null }
  resume() { return this }
  pause() { return this }
}

function stripAnsi(s: string): string {
  return s
    .replace(/\u001b\][^\u0007]*(?:\u0007|\u001b\\)/g, '')
    .replace(/\u001b\[[0-?]*[ -/]*[@-~]/g, '')
}

async function main() {
  let emit: ((e: BridgeEvent) => void) | null = null
  const startBridgeClient = (
    _p: string,
    _s: string,
    onEvent: (e: BridgeEvent) => void,
  ): BridgeClient => {
    emit = onEvent
    setTimeout(() => onEvent({ type: 'ready', version: 1 }), 0)
    return { send() {}, stop() {} }
  }

  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
  const stdin = new FakeReadStream()

  const frames: Array<{ label: string; total: number; bottom: number; hasErr: boolean; hasPanel: boolean }> = []

  const instance = render(React.createElement(App, {
    python: 'python',
    bridgeScript: 'bridge.py',
    startBridgeClient,
  }), {
    stdout: stdout as unknown as NodeJS.WriteStream,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    patchConsole: false,
  })

  const snap = (label: string, before: number) => {
    const chunk = stdout.chunks.slice(before).join('')
    const plain = stripAnsi(chunk)
    const lines = plain.split(NL)
    while (lines.length && lines[lines.length - 1]!.trim() === '') lines.pop()
    // 底栏 = 从最后一个 '─' 顶边框开始到底
    let lastBorder = -1
    lines.forEach((l, i) => { if (/^─{8,}$/.test(l.trim())) lastBorder = i })
    frames.push({
      label,
      total: lines.length,
      bottom: lastBorder >= 0 ? lines.length - lastBorder : -1,
      hasErr: plain.includes('Error') || plain.includes('错误'),
      hasPanel: plain.includes('MCP connecting') || plain.includes('MCP ready'),
    })
  }

  try {
    await delay(400)
    const sink = emit as unknown as (e: BridgeEvent) => void

    sink({ type: 'status', status: 'running', taskId: 1 })
    sink({ type: 'user', taskId: 1, text: 'T1-USER' })
    sink({ type: 'assistant_done', taskId: 1, text: 'T1-ANSWER' })
    sink({ type: 'status', status: 'idle', taskId: 1 })
    await delay(400)

    console.log('=== turn 2 逐帧 ===')
    console.log('阶段'.padEnd(22), '总行', '底栏', 'MCP行')
    const mark0 = stdout.chunks.length
    snap('turn2 开始前', mark0)

    let mark = stdout.chunks.length
    sink({ type: 'status', status: 'running', taskId: 2 })
    await delay(120)
    snap('status=running', mark); mark = stdout.chunks.length

    sink({ type: 'user', taskId: 2, text: 'T2-USER' })
    await delay(120)
    snap('user echo', mark); mark = stdout.chunks.length

    for (let i = 0; i < 10; i += 1) {
      sink({ type: 'assistant_delta', taskId: 2, text: `L${i}` + NL })
      await delay(120)
      snap(`delta ${i}`, mark); mark = stdout.chunks.length
    }

    sink({ type: 'assistant_done', taskId: 2, text: 'T2-DONE' })
    await delay(150)
    snap('assistant_done', mark); mark = stdout.chunks.length

    sink({ type: 'status', status: 'idle', taskId: 2 })
    await delay(200)
    snap('status=idle', mark)

    for (const f of frames) {
      console.log(
        f.label.padEnd(22),
        String(f.total).padStart(4),
        String(f.bottom).padStart(4),
        f.hasPanel ? 'yes' : 'no',
      )
    }
  } finally {
    instance.unmount()
  }
}

main().catch(e => { console.error(e); process.exit(1) })
