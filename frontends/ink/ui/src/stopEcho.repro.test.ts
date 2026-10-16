/**
 * 复现 / 回归：/stop 与 Esc 在 running 时 transcript 只能出现一次 Stop requested。
 *
 * 注意：Ink debug 模式会把整帧重复写到 capture stream，不能用全量 join 计数。
 * 断言最后一帧里的出现次数。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from './App.js'
import type { BridgeClient } from './bridgeClient.js'
import type { BridgeCommand, BridgeEvent } from './protocol.js'

class CaptureWriteStream extends EventEmitter {
  columns = 80
  rows = 24
  chunks: string[] = []
  write(chunk: unknown): boolean {
    this.chunks.push(String(chunk))
    return true
  }
}

class FakeReadStream extends EventEmitter {
  isTTY = true
  private readonly queue: string[] = []
  setRawMode(): this { return this }
  setEncoding(): this { return this }
  ref(): this { return this }
  unref(): this { return this }
  read(): string | null { return this.queue.shift() ?? null }
  resume(): this { return this }
  pause(): this { return this }
  send(text: string): void {
    this.queue.push(text)
    this.emit('readable')
  }
}

function stripAnsi(text: string): string {
  return text
    .replace(/\][^]*(?:|\\)/g, '')
    .replace(/\[[0-?]*[ -/]*[@-~]/g, '')
    .replace(/[=>]/g, '')
}

function delay(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}

function countOccurrences(haystack: string, needle: string): number {
  let count = 0
  let from = 0
  while (true) {
    const idx = haystack.indexOf(needle, from)
    if (idx === -1) return count
    count += 1
    from = idx + needle.length
  }
}

function lastDebugFrame(stdout: CaptureWriteStream): string {
  const frames = stdout.chunks.map(stripAnsi).filter(chunk => (
    chunk.includes('Enter send')
    || chunk.includes('Running ·')
    || /─{8,}/.test(chunk)
  ))
  return frames.at(-1) ?? ''
}

async function waitForFrame(
  stdout: CaptureWriteStream,
  predicate: (frame: string) => boolean,
  timeoutMs = 2500,
): Promise<string> {
  const deadline = Date.now() + timeoutMs
  let frame = ''
  while (Date.now() < deadline) {
    frame = lastDebugFrame(stdout)
    if (predicate(frame)) return frame
    await delay(20)
  }
  return frame
}

function makeRunningBridge(sent: BridgeCommand[]): (
  python: string,
  bridgeScript: string,
  onEvent: (event: BridgeEvent) => void,
) => BridgeClient {
  return (_python, _bridgeScript, onEvent) => {
    setTimeout(() => {
      onEvent({ type: 'ready', version: 1 })
      onEvent({ type: 'status', status: 'running', taskId: 1 })
      onEvent({ type: 'user', taskId: 1, text: 'hello' })
      onEvent({ type: 'assistant_delta', taskId: 1, text: 'working...' })
    }, 0)
    return {
      send(cmd) {
        sent.push(cmd)
        if (cmd.type === 'stop') {
          setTimeout(() => {
            onEvent({ type: 'status', status: 'stopping', taskId: 1 })
            onEvent({ type: 'assistant_done', taskId: 1, text: 'working... aborted' })
            onEvent({ type: 'status', status: 'idle', taskId: 1 })
          }, 10)
        }
      },
      stop() {},
    }
  }
}

test('slash /stop writes Stop requested exactly once in final frame', async () => {
  delete process.env.GA_INK_MOUSE
  const sent: BridgeCommand[] = []
  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
  const stdin = new FakeReadStream()
  const instance = render(React.createElement(App, {
    python: 'python',
    bridgeScript: 'bridge.py',
    startBridgeClient: makeRunningBridge(sent),
  }), {
    stdout: stdout as unknown as NodeJS.WriteStream,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    patchConsole: false,
    debug: true,
  })
  try {
    await waitForFrame(stdout, frame => frame.includes('working...'))
    for (const ch of '/stop') stdin.send(ch)
    await delay(40)
    stdin.send('\r')
    const frame = await waitForFrame(
      stdout,
      value => value.includes('Stop requested') && value.includes('Enter send'),
    )
    await delay(80)
    const finalFrame = lastDebugFrame(stdout)
    const nStop = countOccurrences(finalFrame, 'Stop requested')
    const nCmd = countOccurrences(finalFrame, '/stop')
    assert.equal(
      nStop,
      1,
      `Stop requested count=${nStop} cmd=/stop count=${nCmd} sent=${sent.map(s => s.type).join(',')}\nFRAME:\n${finalFrame || frame}`,
    )
  } finally {
    instance.unmount()
  }
})

test('Esc stop writes Stop requested exactly once in final frame', async () => {
  delete process.env.GA_INK_MOUSE
  const sent: BridgeCommand[] = []
  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
  const stdin = new FakeReadStream()
  const instance = render(React.createElement(App, {
    python: 'python',
    bridgeScript: 'bridge.py',
    startBridgeClient: makeRunningBridge(sent),
  }), {
    stdout: stdout as unknown as NodeJS.WriteStream,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    patchConsole: false,
    debug: true,
  })
  try {
    await waitForFrame(stdout, frame => frame.includes('working...'))
    stdin.send('')
    const frame = await waitForFrame(
      stdout,
      value => value.includes('Stop requested') && value.includes('Enter send'),
    )
    await delay(80)
    const finalFrame = lastDebugFrame(stdout)
    const nStop = countOccurrences(finalFrame, 'Stop requested')
    assert.equal(
      nStop,
      1,
      `Esc Stop requested count=${nStop} sent=${sent.map(s => s.type).join(',')}\nFRAME:\n${finalFrame || frame}`,
    )
  } finally {
    instance.unmount()
  }
})
