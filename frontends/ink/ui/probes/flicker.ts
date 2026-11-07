/**
 * 探针：测「每帧重画量」。这是 flicker（闪）的直接度量。
 *
 * Ink 的 log-update 每帧都 `eraseLines(previousLineCount) + output`，
 * 即擦掉上一帧全部行、重写全部行 —— 哪怕只变了一个字符。
 * 位置不漂移（位置漂移实测为 0）但如果每帧重画 N 行，用户看到的就是「闪」。
 *
 * 输出：每帧的 (擦除行数, 实际变化行数, 重画比)。
 */
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from '../src/App.js'
import type { BridgeClient } from '../src/bridgeClient.js'
import type { BridgeEvent } from '../src/protocol.js'

const NL = String.fromCharCode(10)

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

const delay = (ms: number) => new Promise(r => setTimeout(r, ms))

function stripAnsi(s: string): string {
  return s
    .replace(/\u001b\][^\u0007]*(?:\u0007|\u001b\\)/g, '')
    .replace(/\u001b\[[0-?]*[ -/]*[@-~]/g, '')
}

/** 从一次 write 里解析出「擦除了几行」和「写出的新内容」。 */
function parseWrite(chunk: string): { erased: number; body: string } {
  // ansi-escapes.eraseLines(n) = CSI 2K + n-1 次 (CSI 1A + CSI 2K) + CSI G
  // 也就是说 eraseLines(n) 里有 n 个 CSI 2K
  const erases = (chunk.match(/\u001b\[2K/g) ?? []).length
  const body = stripAnsi(chunk)
  return { erased: erases, body }
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
    await delay(300)
    const sink = emit as unknown as (e: BridgeEvent) => void

    // 第一轮铺一些历史
    sink({ type: 'status', status: 'running', taskId: 1 })
    sink({ type: 'user', taskId: 1, text: 'TURN1-USER' })
    sink({ type: 'assistant_done', taskId: 1, text: 'TURN1-ANSWER' + NL + 'second line' })
    sink({ type: 'status', status: 'idle', taskId: 1 })
    await delay(400)

    // 关键：记录流式开始前的 write 位置
    const mark = stdout.chunks.length

    // 流式：每 40ms 追加一行（模拟真实 token 到达节奏）
    sink({ type: 'status', status: 'running', taskId: 2 })
    sink({ type: 'user', taskId: 2, text: 'TURN2-USER' })
    for (let i = 0; i < 30; i += 1) {
      sink({ type: 'assistant_delta', taskId: 2, text: `LINE-${i}` + NL })
      await delay(40)
    }
    sink({ type: 'assistant_done', taskId: 2, text: 'TURN2-DONE' })
    sink({ type: 'status', status: 'idle', taskId: 2 })
    await delay(300)

    const streamChunks = stdout.chunks.slice(mark)
    console.log(`流式期间共 ${streamChunks.length} 次 write\n`)

    let prevLines: string[] = []
    let totalErased = 0
    let totalUnchanged = 0
    let totalLines = 0
    const samples: string[] = []

    streamChunks.forEach((chunk, idx) => {
      const { erased, body } = parseWrite(chunk)
      const lines = body.split(NL)
      // 有多少行与上一帧同行号内容完全相同（= 本可以跳过）
      let unchanged = 0
      for (let i = 0; i < Math.min(lines.length, prevLines.length); i += 1) {
        if (lines[i] === prevLines[i]) unchanged += 1
      }
      totalErased += erased
      totalUnchanged += unchanged
      totalLines += lines.length
      if (idx < 6 || idx === streamChunks.length - 1) {
        samples.push(
          `  write#${String(idx).padStart(3)}  擦除=${String(erased).padStart(2)} `
          + `总行=${String(lines.length).padStart(2)} 未变行=${String(unchanged).padStart(2)}`,
        )
      }
      prevLines = lines
    })

    console.log('前 6 次 + 最后一次:')
    samples.forEach(s => console.log(s))

    console.log('')
    console.log('=== 汇总 ===')
    console.log(`擦除总行数      = ${totalErased}`)
    console.log(`写出总行数      = ${totalLines}`)
    console.log(`其中内容未变的  = ${totalUnchanged}`)
    const ratio = totalLines ? ((totalUnchanged / totalLines) * 100).toFixed(1) : '0'
    console.log(`白重画比例      = ${ratio}%`)
  } finally {
    instance.unmount()
  }
}

main().catch(e => { console.error(e); process.exit(1) })
