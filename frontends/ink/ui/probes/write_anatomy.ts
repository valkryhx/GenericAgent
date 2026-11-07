/**
 * 探针：看清 Ink 每次 write 的「三连击」结构。
 *
 * 从上个探针看：erase=0 的帧通常伴随 lines=14，然后下一次 erase=14 lines=1。
 * 说明 log-update 的写入被拆成了多次 write：
 *   1) 擦除（erase=N, lines=0/1）
 *   2) 内容（erase=0, lines=N）
 *
 * 如果这两次 write 之间被终端刷新，用户看到的就是「先空、再有内容」= 闪。
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
    for (let i = 0; i < 12; i += 1) {
      sink({ type: 'assistant_delta', taskId: 1, text: `line ${i}` + NL })
      await delay(40)
    }
    await delay(200)

    const writes = stdout.chunks.slice(mark)
    console.log('每次 write 的原始特征（前 16 次）:\n')
    console.log('  #   相对ms  字节  CSI2K  CSI1A  含换行  前 40 字符(可读)')
    const t0 = writes[0]?.t ?? Date.now()
    writes.slice(0, 16).forEach((w, i) => {
      const c2k = (w.data.match(/\u001b\[2K/g) ?? []).length
      const c1a = (w.data.match(/\u001b\[1A/g) ?? []).length
      const nl = (w.data.match(/\n/g) ?? []).length
      const readable = w.data
        .replace(/\u001b\[[0-?]*[ -/]*[@-~]/g, '<E>')
        .replace(/\n/g, '\\n')
        .replace(/\r/g, '')
        .slice(0, 40)
      console.log(
        `  ${String(i).padStart(2)}  ${String(w.t - t0).padStart(5)}  ${String(w.data.length).padStart(4)}`
        + `  ${String(c2k).padStart(4)}  ${String(c1a).padStart(4)}  ${String(nl).padStart(4)}    ${readable}`,
      )
    })
  } finally {
    instance.unmount()
  }
}

main().catch(e => { console.error(e); process.exit(1) })
