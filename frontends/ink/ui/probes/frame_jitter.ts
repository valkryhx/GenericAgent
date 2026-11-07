/**
 * 决定性测量：模拟一轮完整对话，逐帧记录 Ink 实际写出的「动态区行数」。
 *
 * 判据：只要这个数字在帧间变化，终端就必须重排，输入框就会在屏幕上跳。
 * 不依赖任何"看起来如何"的主观判断。
 *
 * 运行：npx tsx .tmp_frame_jitter.ts
 */
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from '../src/App.js'
import type { BridgeClient } from '../src/bridgeClient.js'
import type { BridgeEvent } from '../src/protocol.js'

class CaptureWriteStream extends EventEmitter {
  columns = 120
  rows = 40
  chunks: string[] = []
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

function stripAnsi(text: string): string {
  return text
    .replace(/\u001b\][^\u0007]*(?:\u0007|\u001b\\)/g, '')
    .replace(/\u001b\[[0-?]*[ -/]*[@-~]/g, '')
    .replace(/\u001b[=>]/g, '')
}

const delay = (ms: number) => new Promise(r => setTimeout(r, ms))

/** 取某一个完整帧的可见行（去尾部空行）。 */
function frameLines(frame: string): string[] {
  const lines = frame.split('\n')
  while (lines.length && lines[lines.length - 1]!.trim() === '') lines.pop()
  return lines
}

/**
 * Ink 用 log-update 做增量：每次 write 是一个"擦除 + 重画"批次。
 * 这里按 write 批次切开，取每次写完之后屏幕上应占的行数。
 */
function dynamicHeightOf(chunk: string): number | null {
  const plain = stripAnsi(chunk)
  if (!plain.includes('>')) return null
  const lines = frameLines(plain)
  if (!lines.length) return null
  return lines.length
}

async function main() {
  const mode = process.env.GA_INK_MOUSE === 'full' ? 'full(alt-screen)' : 'inline(default)'
  console.log(`=== 模式: ${mode} · 终端 40x120 ===\n`)

  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
  const stdin = new FakeReadStream()

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

  const instance = render(
    React.createElement(App, {
      python: 'python',
      bridgeScript: 'bridge.py',
      startBridgeClient,
    } as never),
    {
      stdout: stdout as unknown as NodeJS.WriteStream,
      stderr: stderr as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      patchConsole: false,
      // 关键：必须 false。debug:true 时 ink 每帧写 fullStaticOutput + output，
      // 行数里混进了所有已提交的 Static 历史，测出来的不是"动态区高度"。
      debug: false,
    },
  )

  const readings: Array<{ label: string; height: number }> = []

  const snapshot = (label: string) => {
    const last = stdout.chunks.at(-1)
    if (!last) return
    const h = dynamicHeightOf(last)
    if (h !== null) readings.push({ label, height: h })
  }

  try {
    await delay(300)
    snapshot('startup')

    if (!emit) throw new Error('bridge not ready')
    const e = emit as (ev: BridgeEvent) => void

    e({ type: 'user', taskId: 1, text: 'count 1..20' })
    await delay(150)
    snapshot('user echoed')

    // 模拟流式：逐行追加
    for (const n of [1, 2, 4, 8, 12, 20]) {
      e({ type: 'assistant_delta', taskId: 1, text: Array.from({ length: n }, (_, i) => `line ${i + 1}`).join('\n') })
      await delay(120)
      snapshot(`streaming ${n} lines`)
    }

    e({ type: 'assistant_done', taskId: 1, text: 'done' })
    e({ type: 'status', status: 'idle' })
    await delay(250)
    snapshot('idle after turn')
  } finally {
    instance.unmount()
  }

  console.log('阶段'.padEnd(24), '动态区行数')
  console.log('-'.repeat(40))
  for (const r of readings) {
    console.log(r.label.padEnd(24), r.height)
  }

  const heights = [...new Set(readings.map(r => r.height))].sort((a, b) => a - b)
  console.log('\n出现过的行数:', heights)
  console.log('种数:', heights.length)
  console.log('结论:', heights.length <= 1 ? '恒定 —— 不抖' : `变化 ${heights.length} 种 —— 就是抖动根因`)
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
