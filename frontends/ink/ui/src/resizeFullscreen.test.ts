/**
 * 全屏模式下窗口变化后，历史必须还在（用户报「最大化后内容不显示」）。
 *
 * ## 用户症状（2026-11-15）
 *
 * 「小窗口输入，窗口点击全屏后内容不显示；再点击成小窗口，需要鼠标上下滚动卡一会儿
 * 貌似 1 秒才正常显示；再次全屏窗口，仍然不显示。」
 *
 * ## 与已有 resize 测试的区别
 *
 * 现有 `resizeReflow` / `resizeMidStream` 都跑在 **inline 模式**
 * （`GA_INK_FULLSCREEN = 'off'`）。而 GA 默认是**全屏模式**（`resolveFullscreenMode()`
 * 在未设置环境变量时返回 true），两者渲染结构完全不同：
 *
 *   - inline：历史走 ink `<Static>`，live 区只放尾部
 *   - 全屏：`staticTranscript.rows = []`，`<Static>` 完全不参与，历史全靠 live 区
 *     的滚动视口渲染（见 `App.tsx` 里 `fullscreen ? { rows: [], channel: null } : ...`）
 *
 * 所以 inline 模式全绿不代表全屏没问题 —— 用户正是在全屏下遇到它。
 *
 * ## 判据
 *
 * 全屏模式下 resize 之后，历史必须仍然可见。不要求特定字节形状，只要求「屏幕上还有内容」
 * 且「历史行还在」。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from './App.js'
import { inkRenderOptions } from './inkRenderOptions.js'
import { createCursorParkStdout } from './stdoutCursorPark.js'
import { ReflowTerminal } from './resizeReflowModel.js'
import type { BridgeClient } from './bridgeClient.js'
import type { BridgeEvent } from './protocol.js'

const NL = String.fromCharCode(10)
const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

class ReflowStream extends EventEmitter {
  readonly term: ReflowTerminal
  isTTY = true
  constructor(columns: number, rows: number) {
    super()
    this.term = new ReflowTerminal(columns, rows)
  }
  get columns(): number { return this.term.columns }
  set columns(value: number) { this.term.columns = Math.max(1, Math.floor(value)) }
  get rows(): number { return this.term.rows }
  set rows(value: number) { this.term.rows = Math.max(1, Math.floor(value)) }
  write(chunk: unknown): boolean {
    this.term.write(String(chunk))
    return true
  }
}

class FakeReadStream extends EventEmitter {
  isTTY = true
  setRawMode() { return this } setEncoding() { return this } ref() { return this } unref() { return this }
  read() { return null } on() { return this } off() { return this } removeListener() { return this }
}

/** 建立一段会话历史，返回渲染实例供调用方继续操作。 */
async function mountWithHistory(fullscreen: boolean) {
  process.env.GA_INK_FULLSCREEN = fullscreen ? 'on' : 'off'
  let emit: ((event: BridgeEvent) => void) | null = null
  let readyEmitted = false
  const startBridgeClient = (
    _python: string,
    _bridgeScript: string,
    onEvent: (event: BridgeEvent) => void,
  ): BridgeClient => {
    emit = onEvent
    setTimeout(() => {
      onEvent({ type: 'ready', version: 1 })
      readyEmitted = true
    }, 0)
    return { send() {}, stop() {} }
  }

  const stdout = new ReflowStream(80, 24)
  const stderr = new ReflowStream(80, 24)
  const cursorPark = createCursorParkStdout(stdout as unknown as NodeJS.WriteStream)
  const instance = render(React.createElement(App, {
    python: 'python',
    bridgeScript: 'bridge.py',
    startBridgeClient,
    cursorPark,
  }), {
    ...inkRenderOptions,
    stdout: cursorPark.stdout,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: new FakeReadStream() as unknown as NodeJS.ReadStream,
    patchConsole: false,
  })

  for (let i = 0; i < 300 && !readyEmitted; i += 1) await delay(10)
  await delay(60)
  return { stdout, stderr, instance, sink: emit as unknown as (event: BridgeEvent) => void }
}

function seedTurns(sink: (event: BridgeEvent) => void, count: number): void {
  for (let index = 1; index <= count; index += 1) {
    sink({ type: 'status', status: 'running', taskId: index })
    sink({ type: 'user', taskId: index, text: `HIST-${String(index).padStart(2, '0')}` })
    sink({ type: 'assistant_done', taskId: index, text: `回答 ${index}` })
    sink({ type: 'status', status: 'idle', taskId: index })
  }
}

test('fullscreen: history survives a window enlarge', async () => {
  const app = await mountWithHistory(true)
  try {
    seedTurns(app.sink, 12)
    await delay(500)

    const before = app.stdout.term.screen()
    assert.ok(
      before.some(line => line.includes('HIST-')),
      'precondition: history must be visible before the resize' + NL + before.join(NL),
    )

    // 用户「点击全屏」= 终端窗口最大化：宽高同时变大。
    app.stdout.columns = 140
    app.stdout.rows = 50
    app.stderr.columns = 140
    app.stderr.rows = 50
    app.stdout.emit('resize')
    await delay(600)

    const after = app.stdout.term.screen()
    const visible = after.filter(line => line.trim().length > 0).length
    assert.ok(
      after.some(line => line.includes('HIST-')),
      `fullscreen resize left the screen without history (visible rows=${visible}):` + NL + after.join(NL),
    )
  } finally {
    app.instance.unmount()
    await delay(30)
  }
})

test('fullscreen: history survives a shrink then enlarge round-trip', async () => {
  // 用户：「再点击成小窗口…再次全屏窗口，仍然不显示」。来回切必须都能恢复。
  const app = await mountWithHistory(true)
  try {
    seedTurns(app.sink, 12)
    await delay(500)

    // 缩小
    app.stdout.columns = 60
    app.stdout.rows = 18
    app.stderr.columns = 60
    app.stderr.rows = 18
    app.stdout.emit('resize')
    await delay(500)

    // 再放大
    app.stdout.columns = 140
    app.stdout.rows = 50
    app.stderr.columns = 140
    app.stderr.rows = 50
    app.stdout.emit('resize')
    await delay(600)

    const after = app.stdout.term.screen()
    const visible = after.filter(line => line.trim().length > 0).length
    assert.ok(
      after.some(line => line.includes('HIST-')),
      `round-trip resize left the screen without history (visible rows=${visible}):` + NL + after.join(NL),
    )
  } finally {
    app.instance.unmount()
    await delay(30)
  }
})
