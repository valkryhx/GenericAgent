import test from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from './App.js'
import { createCursorParkStdout } from './stdoutCursorPark.js'
import { ReflowTerminal, countVisibleRows } from './resizeReflowModel.js'
import { resetViewportSequence } from './terminalCleanup.js'
import type { BridgeClient } from './bridgeClient.js'
import type { BridgeEvent } from './protocol.js'

const NL = String.fromCharCode(10)

/**
 * 真机 bug 复现 / 回归：终端放大缩小后 GA Ink UI 出现重复信息。
 * 截图证据：屏幕截图 2026-10-10 092005.png（"Worked for 9s ..." 与提示行重复叠加）。
 *
 * 这里用 resizeReflowModel 的 reflow 感知虚拟终端驱动真实的 <App/>：终端宽度一变，
 * 缓冲区就按新宽度重排（= 真机行为），于是 ink 的相对 eraseLines 是否擦干净可以被断言。
 * 不变量：一次宽度变化之后，屏幕上每条内容行只应出现一次。
 */

class ReflowWriteStream extends EventEmitter {
  readonly term: ReflowTerminal
  readonly chunks: string[] = []
  isTTY = true

  constructor(columns: number, rows: number) {
    super()
    this.term = new ReflowTerminal(columns, rows)
  }

  get columns(): number {
    return this.term.columns
  }

  set columns(value: number) {
    this.term.columns = Math.max(1, Math.floor(value))
  }

  get rows(): number {
    return this.term.rows
  }

  set rows(value: number) {
    this.term.rows = Math.max(1, Math.floor(value))
  }

  write(chunk: unknown): boolean {
    this.chunks.push(String(chunk))
    this.term.write(String(chunk))
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

function delay(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}

function describeScreen(screen: string[]): string {
  return screen
    .map((line, index) => `${String(index).padStart(2, ' ')}| ${line}`)
    .join(NL)
}

test('reflow model: widening/narrowing re-wraps the same logical lines', () => {
  const term = new ReflowTerminal(20, 6)
  term.write('12345678901234567890')
  term.write(String.fromCharCode(10))
  term.write('short')
  assert.deepEqual(term.visualRows(), ['12345678901234567890', 'short'])

  term.columns = 10
  assert.deepEqual(term.visualRows(), ['1234567890', '1234567890', 'short'])
  term.columns = 40
  assert.deepEqual(term.visualRows(), ['12345678901234567890', 'short'])
})

test('reflow model: relative erase after a reflow misses the reflowed top rows (mechanism)', () => {
  const term = new ReflowTerminal(20, 8)
  // 一帧：两行短内容 + 一行长边框，然后光标停在最后一行
  term.write('head')
  term.write(String.fromCharCode(10))
  term.write('tail')
  term.write(String.fromCharCode(10))
  term.write('--------------------')
  term.write(String.fromCharCode(10))
  assert.deepEqual(term.screen().slice(-4), ['head', 'tail', '--------------------', ''])

  // 终端变窄：长边框折成 2 行，这一帧实际占用 4 行
  term.columns = 10
  const before = term.screen()
  assert.equal(countVisibleRows(before, 'head'), 1)

  // ink 仍按改尺寸前的 3 行擦除（CSI 2K + 2x 上移擦除）→ 顶行残留
  const erase = String.fromCharCode(27) + '[2K' + (String.fromCharCode(27) + '[1A' + String.fromCharCode(27) + '[2K').repeat(2)
  term.write(erase)
  assert.equal(countVisibleRows(term.screen(), 'head'), 1, 'head 行被相对擦除漏掉了（这正是真机残留的来源）')
})

test('App: terminal width change must not duplicate the live frame (resize reflow)', async () => {
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

  const stdout = new ReflowWriteStream(80, 24)
  const stderr = new ReflowWriteStream(80, 24)
  const stdin = new FakeReadStream()
  const cursorPark = createCursorParkStdout(stdout as unknown as NodeJS.WriteStream)
  const instance = render(React.createElement(App, {
    python: 'python',
    bridgeScript: 'bridge.py',
    startBridgeClient,
    cursorPark,
  }), {
    stdout: cursorPark.stdout,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    patchConsole: false,
  })

  const assertSingleOccurrence = (label: string, probes: string[]) => {
    const screen = stdout.term.screen()
    for (const probe of probes) {
      assert.equal(
        countVisibleRows(screen, probe),
        1,
        `${label}: 屏幕上 "${probe}" 应只出现一次` + NL + describeScreen(screen),
      )
    }
  }
  const idleProbes = ['HIST-PROBE-ALPHA', 'HIST-PROBE-BRAVO', 'Enter send', 'Worked for']
  const streamingProbes = [
    'HIST-PROBE-ALPHA',
    'HIST-PROBE-BRAVO',
    'HIST-PROBE-CHARLIE',
    'STREAM-PROBE-0',
    'STREAM-PROBE-1',
    'STREAM-PROBE-2',
    'Enter keeps draft',
  ]

  try {
    await delay(200)
    const sink = emit as unknown as (event: BridgeEvent) => void
    sink({ type: 'status', status: 'running', taskId: 1 })
    sink({ type: 'user', taskId: 1, text: 'HIST-PROBE-ALPHA' })
    sink({ type: 'assistant_done', taskId: 1, text: 'HIST-PROBE-BRAVO' })
    sink({ type: 'status', status: 'idle', taskId: 1 })
    await delay(300)
    assertSingleOccurrence('resize 之前', idleProbes)

    // 终端变窄：真机会 reflow 已写出的帧，ink 的相对 eraseLines 依据的行数随之失效。
    const resizeMark = stdout.chunks.length
    stdout.columns = 40
    stderr.columns = 40
    stdout.emit('resize')
    await delay(400)
    assertSingleOccurrence('变窄之后', idleProbes)

    // 字节级契约（playbook 手段二）：宽度变化必须整屏重置，且重置之后不得再写出旧宽度的帧
    // ——「不猜 reflow 后的几何」正是 Claude Code / Codex 的做法。
    const postResize = stdout.chunks.slice(resizeMark).join('')
    const resetSequence = resetViewportSequence()
    assert.ok(postResize.includes(resetSequence), '宽度变化后必须写整屏重置序列')
    const afterReset = postResize.slice(postResize.indexOf(resetSequence) + resetSequence.length)
    assert.equal(
      afterReset.includes('─'.repeat(79)),
      false,
      '整屏重置之后不得再写出旧宽度（79 列画布）的帧',
    )

    // 再变宽一次（reflow 反方向）也不能留下残影。
    stdout.columns = 100
    stderr.columns = 100
    stdout.emit('resize')
    await delay(400)
    assertSingleOccurrence('变宽之后', idleProbes)

    // 流式进行中（活动帧更高、光标未 park）resize 同样不能残留。
    sink({ type: 'status', status: 'running', taskId: 2 })
    sink({ type: 'user', taskId: 2, text: 'HIST-PROBE-CHARLIE' })
    for (let index = 0; index < 3; index += 1) {
      sink({ type: 'assistant_delta', taskId: 2, text: `STREAM-PROBE-${index}` + NL })
    }
    await delay(200)
    assertSingleOccurrence('流式 resize 之前', streamingProbes)

    stdout.columns = 60
    stderr.columns = 60
    stdout.emit('resize')
    await delay(400)
    assertSingleOccurrence('流式 resize 之后', streamingProbes)
  } finally {
    instance.unmount()
  }
})
