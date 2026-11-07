/**
 * 验证：给 Ink 的 stdout 套一层 proxy，在每次 write 外套 CSI ?2026h / ?2026l。
 *
 * 这是 CC 文档 §1.4 的 P0 第 3 条。Ink 本身不支持，但可以在 stdout 层拦截：
 * log-update 的每次 write 已经是「擦除+重写」的完整一帧，天然适合被包住。
 */
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from '../src/App.js'
import type { BridgeClient } from '../src/bridgeClient.js'
import type { BridgeEvent } from '../src/protocol.js'

const NL = String.fromCharCode(10)
const delay = (ms: number) => new Promise(r => setTimeout(r, ms))

const BSU = '\u001b[?2026h'
const ESU = '\u001b[?2026l'

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

/**
 * 把每次 write 包进同步输出区间。
 * 只包「整帧」类 write（含擦除序列的），避免把单字符写入也包起来。
 */
function synchronizeStdout(stream: CaptureWriteStream) {
  return {
    ...stream,
    get columns() { return stream.columns },
    get rows() { return stream.rows },
    isTTY: true,
    write(chunk: unknown): boolean {
      const data = String(chunk)
      if (data.includes('\u001b[2K')) {
        return stream.write(BSU + data + ESU)
      }
      return stream.write(data)
    },
    on: stream.on.bind(stream),
    off: stream.off.bind(stream),
    removeListener: stream.removeListener.bind(stream),
    emit: stream.emit.bind(stream),
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
  const useSync = process.argv.includes('--sync')
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

  const raw = new CaptureWriteStream()
  const out = useSync ? synchronizeStdout(raw) : raw
  const stderr = new CaptureWriteStream()
  const stdin = new FakeReadStream()

  const instance = render(React.createElement(App, {
    python: 'python',
    bridgeScript: 'bridge.py',
    startBridgeClient,
  }), {
    stdout: out as unknown as NodeJS.WriteStream,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    patchConsole: false,
  })

  try {
    await delay(400)
    const sink = emit as unknown as (e: BridgeEvent) => void
    sink({ type: 'status', status: 'running', taskId: 1 })
    sink({ type: 'user', taskId: 1, text: 'T1' })
    for (let i = 0; i < 40; i += 1) {
      sink({ type: 'assistant_delta', taskId: 1, text: `L-${i} content` + NL })
      await delay(25)
    }
    sink({ type: 'assistant_done', taskId: 1, text: 'DONE' })
    sink({ type: 'status', status: 'idle', taskId: 1 })
    await delay(400)
  } finally {
    instance.unmount()
  }

  const all = raw.chunks.join('')
  const bsu = (all.match(/\u001b\[\?2026h/g) ?? []).length
  const esu = (all.match(/\u001b\[\?2026l/g) ?? []).length
  const erases = (all.match(/\u001b\[2K/g) ?? []).length

  console.log(`mode=${useSync ? 'SYNC' : 'plain'}`)
  console.log(`  BSU 数量      = ${bsu}`)
  console.log(`  ESU 数量      = ${esu}`)
  console.log(`  配对完整      = ${bsu === esu && bsu > 0 ? 'YES' : 'NO'}`)
  console.log(`  2K 擦除总数   = ${erases}`)
  // 检查是否所有擦除都被包住
  const unwrapped = all.split(BSU).slice(1).map(s => s.split(ESU)[0] ?? '')
  const wrappedErases = unwrapped.reduce((a, s) => a + (s.match(/\u001b\[2K/g) ?? []).length, 0)
  console.log(`  被包住的擦除  = ${wrappedErases} / ${erases}`)
}

main().catch(e => { console.error(e); process.exit(1) })
