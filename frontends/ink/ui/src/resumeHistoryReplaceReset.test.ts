import test from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from './App.js'
import { createCursorParkStdout } from './stdoutCursorPark.js'
import { resetViewportSequence } from './terminalCleanup.js'
import type { BridgeClient } from './bridgeClient.js'
import type { BridgeEvent } from './protocol.js'

class CaptureWriteStream extends EventEmitter {
  columns = 80
  rows = 24
  chunks: string[] = []
  isTTY = true
  write(chunk: unknown): boolean {
    this.chunks.push(String(chunk))
    return true
  }
}

class FakeReadStream extends EventEmitter {
  isTTY = true
  setRawMode(): this { return this }
  setEncoding(): this { return this }
  ref(): this { return this }
  unref(): this { return this }
  read(): string | null { return null }
  resume(): this { return this }
  pause(): this { return this }
}

function delay(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}

test('resume history replacement resets the terminal viewport before reprinting history', async () => {
  delete process.env.GA_INK_MOUSE
  let emit: ((event: BridgeEvent) => void) | null = null
  const startBridgeClient = (
    _python: string,
    _bridgeScript: string,
    onEvent: (event: BridgeEvent) => void,
  ): BridgeClient => {
    emit = onEvent
    setTimeout(() => onEvent({ type: 'ready', version: 1 }), 0)
    return { send() {}, stop() {} }
  }
  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
  const cursorPark = createCursorParkStdout(stdout as unknown as NodeJS.WriteStream)
  const instance = render(React.createElement(App, {
    python: 'python',
    bridgeScript: 'bridge.py',
    startBridgeClient,
    cursorPark,
  }), {
    stdout: cursorPark.stdout,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: new FakeReadStream() as unknown as NodeJS.ReadStream,
    patchConsole: false,
  })

  try {
    await delay(80)
    const sink = emit as unknown as (event: BridgeEvent) => void
    sink({ type: 'user', taskId: 1, text: 'CURRENT-HISTORY-PROBE' })
    sink({ type: 'assistant_done', taskId: 1, text: 'CURRENT-ANSWER-PROBE' })
    sink({ type: 'status', status: 'idle', taskId: 1 })
    await delay(120)

    const mark = stdout.chunks.length
    sink({
      type: 'history_replace',
      messages: [
        { role: 'user', taskId: 1, text: 'RESTORED-HISTORY-PROBE-1' },
        { role: 'assistant', taskId: 1, text: 'RESTORED-ANSWER-PROBE-1' },
        { role: 'user', taskId: 2, text: 'RESTORED-HISTORY-PROBE-2' },
        { role: 'assistant', taskId: 2, text: 'RESTORED-ANSWER-PROBE-2' },
      ],
    })
    await delay(160)

    const afterReplacement = stdout.chunks.slice(mark).join('')
    const reset = resetViewportSequence()
    assert.ok(
      afterReplacement.includes(reset),
      'history_replace must clear the screen and scrollback before reprinting restored history',
    )
    const resetIndex = afterReplacement.indexOf(reset)
    assert.ok(
      afterReplacement.indexOf('RESTORED-HISTORY-PROBE-1') > resetIndex,
      'restored history must be printed after the viewport reset',
    )
  } finally {
    instance.unmount()
  }
})
