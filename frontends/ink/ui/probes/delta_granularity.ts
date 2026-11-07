/**
 * 验证：提交频率与 delta 粒度强相关。
 *
 * 假设：state.ts:289 在【每个】assistant_delta 里调 commitStreamingAssistantMessages。
 * 如果 relay 的 delta 很小（几字符），提交就会非常碎 —— 每次都推一次动态区。
 *
 * 对照两种 delta 粒度：
 *   A. 每次 1 行（真实流式）
 *   B. 每次 5 行（攒得多一点）
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

async function run(deltaLines: number, label: string) {
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

  try {
    await delay(350)
    const sink = emit as unknown as (e: BridgeEvent) => void
    sink({ type: 'status', status: 'running', taskId: 1 })
    sink({ type: 'user', taskId: 1, text: 'P' })
    const mark = stdout.chunks.length

    // 总量固定 40 行，但每次 delta 带 deltaLines 行
    const total = 40
    for (let i = 0; i < total; i += deltaLines) {
      const chunk = Array.from(
        { length: Math.min(deltaLines, total - i) },
        (_, k) => `L${i + k}`,
      ).join(NL) + NL
      sink({ type: 'assistant_delta', taskId: 1, text: chunk })
      await delay(35)
    }
    sink({ type: 'assistant_done', taskId: 1, text: 'D' })
    sink({ type: 'status', status: 'idle', taskId: 1 })
    await delay(300)

    const writes = stdout.chunks.slice(mark)
    let staticWrites = 0
    let staticLines = 0
    for (const data of writes) {
      const erase = (data.match(/\u001b\[2K/g) ?? []).length
      const body = stripAnsi(data)
      const lines = body.split(NL).filter(l => l.length > 0).length
      if (erase === 0 && lines > 0) {
        staticWrites += 1
        staticLines += lines
      }
    }
    console.log(
      `${label.padEnd(22)} Static 提交=${String(staticWrites).padStart(2)} 次`
      + `  总行数=${String(staticLines).padStart(3)}`,
    )
  } finally {
    instance.unmount()
  }
}

async function main() {
  console.log('=== 固定总量 40 行，改变每次 delta 的粒度 ===\n')
  await run(1, 'delta=1 行/次 (40 次)')
  await run(2, 'delta=2 行/次 (20 次)')
  await run(5, 'delta=5 行/次 (8 次)')
  await run(10, 'delta=10 行/次 (4 次)')
  console.log('')
  console.log('提交次数越多 => 动态区被推开次数越多 => 用户感到「偶尔上下位移」')
}

main().catch(e => { console.error(e); process.exit(1) })
