/**
 * 定位 ±1：headless 渲染 App，逐帧量「总帧高」与各层高度。
 *
 * 真机 diff 显示 turn 结束时多出一个空行、总高 +1，终端已满于是滚 1 行。
 * 本探针直接驱动 status running -> idle，逐帧打印帧高，
 * 找出到底哪一层多出来一行。
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

/** 一帧的可见行数（去掉尾部纯空行）。 */
function frameRows(data: string): number {
  const body = stripAnsi(data)
  const lines = body.split(NL)
  while (lines.length > 0 && lines[lines.length - 1]!.trim() === '') lines.pop()
  return lines.length
}

type Snap = { phase: string; frame: number; tail: string[] }

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

  const snaps: Snap[] = []

  function snap(phase: string) {
    const last = stdout.chunks[stdout.chunks.length - 1] ?? ''
    const body = stripAnsi(last)
    const lines = body.split(NL)
    while (lines.length > 0 && lines[lines.length - 1]!.trim() === '') lines.pop()
    snaps.push({
      phase,
      frame: lines.length,
      tail: lines.slice(-6).map(l => l.trimEnd()),
    })
  }

  try {
    await delay(400)
    snap('idle-ready')

    const sink = emit as unknown as (e: BridgeEvent) => void

    sink({ type: 'status', status: 'running', taskId: 1 })
    sink({ type: 'user', taskId: 1, text: 'P' })
    await delay(250)
    snap('running-no-content')

    // 灌 20 行内容
    for (let i = 0; i < 20; i += 1) {
      sink({ type: 'assistant_delta', taskId: 1, text: `R${i}` + NL })
      await delay(30)
    }
    await delay(300)
    snap('running-with-content')

    // turn 结束：done -> idle
    sink({ type: 'assistant_done', taskId: 1, text: '' })
    await delay(120)
    snap('after-done-before-idle')

    sink({ type: 'status', status: 'idle', taskId: 1 })
    await delay(350)
    snap('idle-after-turn')
    await delay(600)
    snap('idle-settled')
  } finally {
    instance.unmount()
  }

  console.log('阶段'.padEnd(26), '帧高', '  底部 6 行')
  console.log('-'.repeat(78))
  for (const s of snaps) {
    console.log(`${s.phase.padEnd(26)} ${String(s.frame).padStart(4)}`)
    for (const l of s.tail) {
      if (l) console.log(`${' '.repeat(31)}| ${l.slice(0, 70)}`)
    }
    console.log('')
  }

  console.log('=== 高度变化 ===')
  for (let i = 1; i < snaps.length; i += 1) {
    const d = snaps[i]!.frame - snaps[i - 1]!.frame
    const mark = d === 0 ? '' : d > 0 ? `  <== +${d}` : `  <== ${d}`
    console.log(`${snaps[i - 1]!.phase} (${snaps[i - 1]!.frame}) -> ${snaps[i]!.phase} (${snaps[i]!.frame})${mark}`)
  }
}

main().catch(e => { console.error(e); process.exit(1) })
