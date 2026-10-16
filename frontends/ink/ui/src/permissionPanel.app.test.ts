import test from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from './App.js'
import type { BridgeClient } from './bridgeClient.js'
import type { BridgeCommand, BridgeEvent } from './protocol.js'

// App 级回归：/permissions 打开三档面板，Full Access 走二次确认，只有确认后才发命令。
// 面板/确认的纯逻辑在 permissionPanel.test.ts；这里验证 App 把事件、按键、命令粘合对了。

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

  read(): string | null {
    return this.queue.shift() ?? null
  }

  resume(): this {
    return this
  }

  pause(): this {
    return this
  }

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

async function waitForOutput(stdout: CaptureWriteStream, predicate: (output: string) => boolean): Promise<string> {
  const deadline = Date.now() + 2000
  let output = ''
  while (Date.now() < deadline) {
    output = stdout.chunks.map(stripAnsi).join('')
    if (predicate(output)) return output
    await delay(20)
  }
  return output
}

const UP_ARROW = '[A'
const DOWN_ARROW = '[B'
const ENTER = '\r'

function renderApp() {
  const sent: BridgeCommand[] = []
  let emit: ((event: BridgeEvent) => void) | null = null
  const startBridgeClient = (
    _python: string,
    _bridgeScript: string,
    onEvent: (event: BridgeEvent) => void,
  ): BridgeClient => {
    emit = onEvent
    setTimeout(() => onEvent({ type: 'ready', version: 1 }), 0)
    return {
      send(command: BridgeCommand) {
        sent.push(command)
      },
      stop() {},
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
    debug: true,
  })
  return { sent, stdin, stdout, instance, emit: () => emit as unknown as (event: BridgeEvent) => void }
}

test('App /permissions requests status then applying read_only sends command immediately', async () => {
  const app = renderApp()
  try {
    await waitForOutput(app.stdout, output => output.includes('>'))
    app.stdin.send('/permissions')
    app.stdin.send(ENTER)
    // /permissions 只请求状态，不直接切档
    await waitForOutput(app.stdout, () => app.sent.some(command => command.type === 'permission_status'))
    assert.equal(app.sent.some(command => command.type === 'permission_status'), true)

    // 后端回状态（当前 full_access），面板渲染三档
    app.emit()({ type: 'permission_status', mode: 'full_access', default: 'full_access', modes: ['read_only', 'ask', 'full_access'] })
    await waitForOutput(app.stdout, output => output.includes('Read Only') && output.includes('Full Access'))

    // 面板初始选中 current(full_access, index 2)。上移两次到 read_only。
    app.stdin.send(UP_ARROW)
    app.stdin.send(UP_ARROW)
    app.stdin.send(ENTER)
    await waitForOutput(app.stdout, () => app.sent.some(c => c.type === 'set_permission_mode'))
    const readOnlyCmd = app.sent.find(c => c.type === 'set_permission_mode')
    assert.deepEqual(readOnlyCmd, { type: 'set_permission_mode', mode: 'read_only' })
  } finally {
    app.instance.unmount()
  }
})

test('App switching to Full Access from read_only shows confirm and applies only after second Enter', async () => {
  const app = renderApp()
  try {
    await waitForOutput(app.stdout, output => output.includes('>'))
    app.stdin.send('/permissions')
    app.stdin.send(ENTER)
    await waitForOutput(app.stdout, () => app.sent.some(command => command.type === 'permission_status'))

    // 当前 read_only：面板选中 read_only(index 0)
    app.emit()({ type: 'permission_status', mode: 'read_only', default: 'full_access', modes: ['read_only', 'ask', 'full_access'] })
    await waitForOutput(app.stdout, output => output.includes('Full Access'))

    // 下移两次到 full_access，Enter → 进入二次确认（不发命令）
    app.stdin.send(DOWN_ARROW)
    app.stdin.send(DOWN_ARROW)
    app.stdin.send(ENTER)
    await waitForOutput(app.stdout, output => /confirm|确认|再按/i.test(output))
    assert.equal(app.sent.some(c => c.type === 'set_permission_mode'), false)

    // 再 Enter → 确认，发 full_access
    app.stdin.send(ENTER)
    await waitForOutput(app.stdout, () => app.sent.some(c => c.type === 'set_permission_mode'))
    const cmd = app.sent.find(c => c.type === 'set_permission_mode')
    assert.deepEqual(cmd, { type: 'set_permission_mode', mode: 'full_access' })
  } finally {
    app.instance.unmount()
  }
})
