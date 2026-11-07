/**
 * 探针：区分「擦除→重写之间的空窗」和「重写内容本身的变化」。
 *
 * 「轻微抖动」的可能来源：
 *  A. Ink 每帧 eraseLines + 全量重写 => 擦除瞬间屏幕上是空的，重写后才有内容
 *     -> 表现为「闪一下」
 *  B. 同一毫秒内多次 write（帧间隔中位数 0ms）=> 多次擦写叠加
 *     -> 表现为「抖」
 *
 * 这个探针统计：每次 write 的「擦除行数」vs「实际内容变化行数」的比值。
 * 比值越高 = 越多无用重画 = 越可能是抖动来源。
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
    for (let i = 0; i < 60; i += 1) {
      sink({ type: 'assistant_delta', taskId: 1, text: `line ${i} of streamed text` + NL })
      await delay(30)
    }
    sink({ type: 'assistant_done', taskId: 1, text: 'DONE' })
    sink({ type: 'status', status: 'idle', taskId: 1 })
    await delay(300)

    const writes = stdout.chunks.slice(mark)
    let prev: string[] = []
    let totalErase = 0
    let totalLines = 0
    let unchanged = 0
    let sameMsBursts = 0
    const rows: string[] = []

    writes.forEach((w, i) => {
      const erase = (w.data.match(/\u001b\[2K/g) ?? []).length
      const body = stripAnsi(w.data)
      const lines = body.split(NL)
      let same = 0
      for (let k = 0; k < Math.min(lines.length, prev.length); k += 1) {
        if (lines[k] === prev[k]) same += 1
      }
      totalErase += erase
      totalLines += lines.length
      unchanged += same
      if (i > 0 && w.t === writes[i - 1]!.t) sameMsBursts += 1
      rows.push(`${String(i).padStart(3)} erase=${String(erase).padStart(2)} lines=${String(lines.length).padStart(2)} same=${String(same).padStart(2)}`)
      prev = lines
    })

    console.log('前 10 帧:')
    rows.slice(0, 10).forEach(r => console.log('  ' + r))
    console.log('')
    console.log('=== 汇总 ===')
    console.log(`write 次数        = ${writes.length}`)
    console.log(`擦除总行数        = ${totalErase}`)
    console.log(`写出总行数        = ${totalLines}`)
    console.log(`内容未变的行      = ${unchanged}`)
    console.log(`无效重画占比      = ${((unchanged / Math.max(1, totalLines)) * 100).toFixed(1)}%`)
    console.log(`同毫秒内多次 write = ${sameMsBursts} 次`)
  } finally {
    instance.unmount()
  }
}

main().catch(e => { console.error(e); process.exit(1) })
