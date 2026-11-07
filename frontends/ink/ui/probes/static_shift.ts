/**
 * 决定性验证：整帧位移是否由「Static 提交」引起？
 *
 * 假设：inline 模式下 <Static> 新提交的行会被写到动态区上方，
 * 动态区（含输入框）因此整体下移；随后终端滚动又把整体推上去。
 *
 * 判据：把每次 write 分成两类
 *   - static 类：erase=0 且含内容（ink.js:129 stdout.write(staticOutput)）
 *   - frame 类：含 CSI 2K（log.clear / log(output)）
 * 然后看「帧行数变化」是否总是紧跟 static 类写入。
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
  chunks: Array<{ t: number; data: string }> = []
  isTTY = true
  write(chunk: unknown): boolean {
    this.chunks.push({ t: Date.now(), data: String(chunk) })
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
    await delay(400)
    const sink = emit as unknown as (e: BridgeEvent) => void
    sink({ type: 'status', status: 'running', taskId: 1 })
    sink({ type: 'user', taskId: 1, text: 'PROMPT' })

    const mark = stdout.chunks.length
    for (let i = 0; i < 40; i += 1) {
      sink({ type: 'assistant_delta', taskId: 1, text: `line ${i} content` + NL })
      await delay(35)
    }
    sink({ type: 'assistant_done', taskId: 1, text: 'DONE' })
    sink({ type: 'status', status: 'idle', taskId: 1 })
    await delay(300)

    const writes = stdout.chunks.slice(mark)

    // 逐次 write 分类 + 重绘行数
    let prevFrameLines = 0
    const events: Array<{ kind: string; lines: number; erase: number }> = []
    for (const w of writes) {
      const erase = (w.data.match(/\u001b\[2K/g) ?? []).length
      const body = stripAnsi(w.data)
      const lines = body.split(NL).filter(l => l.length > 0).length
      let kind = 'other'
      if (erase > 0 && lines <= 1) kind = 'clear-only'
      else if (erase > 0 && lines > 1) kind = 'frame-with-erase'
      else if (erase === 0 && lines > 0) kind = 'no-erase-content'
      events.push({ kind, lines, erase })
    }

    // 统计各类占比
    const counts = new Map<string, number>()
    for (const e of events) counts.set(e.kind, (counts.get(e.kind) ?? 0) + 1)
    console.log('=== write 分类（流式 40 delta）===')
    for (const [k, v] of [...counts].sort((a, b) => b[1] - a[1])) {
      console.log(`  ${k.padEnd(20)} ${v} 次`)
    }

    // 关键：no-erase-content 就是 ink.js:129 的 staticOutput 写入
    const staticWrites = events.filter(e => e.kind === 'no-erase-content')
    const totalStaticLines = staticWrites.reduce((a, e) => a + e.lines, 0)
    console.log('')
    console.log('=== Static 提交（导致整帧下移的源头）===')
    console.log(`  次数            = ${staticWrites.length}`)
    console.log(`  总行数          = ${totalStaticLines}`)
    console.log(`  平均每次        = ${(totalStaticLines / Math.max(1, staticWrites.length)).toFixed(1)} 行`)
    console.log('')
    console.log('每次 Static 提交都会把动态区（含输入框）往下推对应行数，')
    console.log('终端滚动再把它推回去 —— 这就是「偶尔轻微上下位移」。')

    // 列出前几次
    console.log('')
    console.log('前 8 次 Static 提交的行数:')
    staticWrites.slice(0, 8).forEach((e, i) => {
      console.log(`  #${i}  ${e.lines} 行`)
    })
  } finally {
    instance.unmount()
  }
}

main().catch(e => { console.error(e); process.exit(1) })
