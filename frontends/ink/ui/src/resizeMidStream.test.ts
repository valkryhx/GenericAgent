/**
 * 宽度变化后，正在流式输出的那一轮，开头那条 user 行不能消失。
 *
 * ## 真机 bug（2026-10-10 起，2026-11-14 定位根因，2026-11-15 修好）
 *
 * `resizeReflow.test.ts` 红了一个多月：变窄之后，正在流式输出的那一轮，开头那条
 * user 行（探针里的 `HIST-PROBE-CHARLIE`）从屏幕上整条消失。
 *
 * 根因是 ink 的 **两个** log 入口，App 只处理得了一个：
 *
 * 1. ink 用 `log-update` 记着「上一帧占了几行」，下一帧据此 `eraseLines(n)` 先擦再画。
 *    宽度一变，这个计数是**旧宽度**下的值 —— 我们的整屏重置只清了屏幕，ink 不知道。
 * 2. ink 还把 `log` 包成 `throttle(log, 32, {leading: true, trailing: true})`。
 *    resize 那一刻，节流窗口里挂着一次**延迟执行的尾帧**，它捕获的 output 是 resize
 *    之前的（只有 live 区，不含 static）。`log.clear()` 取消不掉这个尾帧。
 *
 * 于是顺序变成：重置 → ink 写完整帧（含 static）→ 32ms 后尾帧按旧计数擦 10 行、
 * 只重画 live 区 ⇒ static 那段被擦掉且不再重画。实测 CHARLIE 在 t+0ms 就没了。
 *
 * 修复要两条一起做：先 `ink.clear()` 把计数归零，再把 `<Static>` 的重挂推到节流
 * 窗口之后，让尾帧先落地。
 *
 * ## 为什么不用 resizeReflow.test.ts 代替
 *
 * 那个测试断言的是最终屏幕，能覆盖结果；这条测试把「t+0ms 就丢了」这个**时序**
 * 也钉住 —— 若将来有人把延迟改回 0，最终屏幕可能碰巧还对，但中间态会闪一下，
 * 而这正是用户会看到的东西（真机 60fps 下 32ms 是两帧）。
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

const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

/** Rows on screen that mention `probe`. */
function rowsMentioning(screen: string[], probe: string): number[] {
  return screen.map((line, index) => (line.includes(probe) ? index : -1)).filter(index => index >= 0)
}

test('narrowing mid-stream keeps the open turn header, at every point in time', async () => {
  process.env.GA_INK_FULLSCREEN = 'off'
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

  const stdout = new ReflowStream(80, 24)
  const stderr = new ReflowStream(80, 24)
  const cursorPark = createCursorParkStdout(stdout as unknown as NodeJS.WriteStream)
  const instance = render(React.createElement(App, {
    python: 'python',
    bridgeScript: 'bridge.py',
    startBridgeClient,
    cursorPark,
    // Pin the default explicitly. The safe window is not a pure function of this value
    // (probes/resize_delay_sweep.ts shows pass rate is non-monotonic: 48ms < 64ms < 120ms),
    // so a test that silently inherits the constant cannot tell "someone tuned it" from
    // "the ordering race changed". Naming it here makes a change deliberate.
    resizeRemountDelayMs: 120,
  }), {
    ...inkRenderOptions,
    stdout: cursorPark.stdout,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: new FakeReadStream() as unknown as NodeJS.ReadStream,
    patchConsole: false,
  })

  try {
    await delay(200)
    const sink = emit as unknown as (event: BridgeEvent) => void
    sink({ type: 'status', status: 'running', taskId: 1 })
    sink({ type: 'user', taskId: 1, text: 'HIST-PROBE-ALPHA' })
    sink({ type: 'assistant_done', taskId: 1, text: 'HIST-PROBE-BRAVO' })
    sink({ type: 'status', status: 'idle', taskId: 1 })
    await delay(300)

    sink({ type: 'status', status: 'running', taskId: 2 })
    sink({ type: 'user', taskId: 2, text: 'HIST-PROBE-CHARLIE' })
    for (let index = 0; index < 3; index += 1) {
      sink({ type: 'assistant_delta', taskId: 2, text: `STREAM-PROBE-${index}` + NL })
    }
    await delay(250)
    assert.ok(
      rowsMentioning(stdout.term.screen(), 'HIST-PROBE-CHARLIE').length > 0,
      'the header was already missing before the resize',
    )

    stdout.columns = 60
    stderr.columns = 60
    stdout.emit('resize')

    // A short blank window is expected and fine: the remount is deliberately pushed
    // past ink's 32ms throttle so the stale tail frame lands first. What must never
    // happen is arriving at a *settled* screen without the header -- that was the bug.
    await delay(200)
    const settled = stdout.term.screen()
    assert.ok(
      rowsMentioning(settled, 'HIST-PROBE-CHARLIE').length > 0,
      'the header was lost for good after the resize' + NL + settled.join(NL),
    )
    assert.ok(
      rowsMentioning(settled, 'HIST-PROBE-ALPHA').length > 0,
      'history was lost for good after the resize' + NL + settled.join(NL),
    )

    // And exactly once: the full repaint must not duplicate it either.
    assert.equal(rowsMentioning(settled, 'HIST-PROBE-CHARLIE').length, 1, settled.join(NL))
    assert.equal(rowsMentioning(settled, 'HIST-PROBE-ALPHA').length, 1, settled.join(NL))
    assert.equal(rowsMentioning(settled, 'HIST-PROBE-BRAVO').length, 1, settled.join(NL))

    // Widening again must also settle correctly.
    stdout.columns = 100
    stderr.columns = 100
    stdout.emit('resize')
    await delay(250)
    const widened = stdout.term.screen()
    assert.equal(rowsMentioning(widened, 'HIST-PROBE-CHARLIE').length, 1, widened.join(NL))
    assert.equal(rowsMentioning(widened, 'HIST-PROBE-ALPHA').length, 1, widened.join(NL))
  } finally {
    instance.unmount()
  }
})
