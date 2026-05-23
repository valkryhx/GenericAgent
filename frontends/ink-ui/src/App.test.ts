import test from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from './App.js'
import type { BridgeClient } from './bridgeClient.js'
import type { BridgeEvent } from './protocol.js'

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

  setRawMode(): this {
    return this
  }

  setEncoding(): this {
    return this
  }

  ref(): this {
    return this
  }

  unref(): this {
    return this
  }

  read(): null {
    return null
  }
}

function stripAnsi(text: string): string {
  return text
    .replace(/\u001B\][^\u0007]*(?:\u0007|\u001B\\)/g, '')
    .replace(/\u001B\[[0-?]*[ -/]*[@-~]/g, '')
    .replace(/\u001B[=>]/g, '')
}

function delay(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}

async function waitForFrame(stdout: CaptureWriteStream, predicate: (frame: string) => boolean): Promise<string> {
  const deadline = Date.now() + 2000
  let lastFrame = ''
  while (Date.now() < deadline) {
    const frames = stdout.chunks.map(stripAnsi).filter(chunk => chunk.includes('GenericAgent Ink'))
    lastFrame = frames.at(-1) ?? ''
    if (predicate(lastFrame)) return lastFrame
    await delay(20)
  }
  return lastFrame
}

test('App scrolls a resumed history replacement to its first user message even when row count is unchanged', async () => {
  const timers: NodeJS.Timeout[] = []
  const startBridgeClient = (
    _python: string,
    _bridgeScript: string,
    onEvent: (event: BridgeEvent) => void,
  ): BridgeClient => {
    const emitLater = (delayMs: number, event: BridgeEvent) => {
      timers.push(setTimeout(() => onEvent(event), delayMs))
    }
    emitLater(0, { type: 'ready', version: 1 })
    for (let index = 1; index <= 12; index++) {
      emitLater(20, { type: 'user', taskId: index, text: `当前问题 ${index}` })
      emitLater(20, { type: 'assistant_done', taskId: index, text: `当前回答 ${index}` })
    }
    timers.push(setTimeout(() => {
      const messages: BridgeEvent & { type: 'history_replace' } = {
        type: 'history_replace',
        messages: [],
      }
      for (let index = 1; index <= 12; index++) {
        messages.messages.push({ role: 'user', taskId: index, text: index === 1 ? '介绍美国' : `恢复问题 ${index}` })
        messages.messages.push({ role: 'assistant', taskId: index, text: `恢复回答 ${index}` })
      }
      onEvent(messages)
      onEvent({ type: 'system', text: '✅ 已恢复 12 轮结构化会话' })
    }, 90))
    return {
      send() {},
      stop() {
        timers.forEach(timer => clearTimeout(timer))
      },
    }
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
    const finalFrame = await waitForFrame(stdout, frame => /> 介绍美国/.test(frame))

    assert.match(finalFrame, /> 介绍美国/)
  } finally {
    instance.unmount()
    timers.forEach(timer => clearTimeout(timer))
  }
})
