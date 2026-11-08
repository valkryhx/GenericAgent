/**
 * 真实 Ink 端到端：在输入框里用鼠标左键拖拽，能不能选中/复制输入框里的文字。
 *
 * 为什么必须做真机验证：这个 bug 的全部内容是「鼠标坐标 → 渲染帧」的几何映射。
 * 单元测试能证明 framePlan 把 composer 放在锚点行、frameSelection 能切出那段文字，
 * 但只有真的跑一遍 Ink（真的布局、真的 wrap、真的 caret 行号）才能证明这两层被
 * 喂进了对的值。历史上本仓库出现过「探针自己重算产品公式 → 全绿但产品是错的」，
 * 所以这里断言的是**用户可见的结果**：抛给剪贴板的字节里必须出现输入框里那串字，
 * 且必须带一个只存在于本次运行的一次性 token（防止把 transcript 里的同名字符串
 * 误当成功）。
 *
 * 不走真实剪贴板：远端会话判定强制打开，走 OSC 52，字节直接落进捕获流，可逐字比对，
 * 也不会污染本机剪贴板。
 */
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import React from 'react'
import { render } from 'ink'
import { App } from '../src/App.js'
import { createCursorParkStdout } from '../src/stdoutCursorPark.js'
import { applyBridgeEvent, initialState, type AppState } from '../src/state.js'
import type { BridgeClient } from '../src/bridgeClient.js'
import type { BridgeCommand, BridgeEvent } from '../src/protocol.js'
import { applyToScreen, createScreen, frameRow, paintedFrame, screenLines } from '../src/virtualScreen.js'

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../..')

class CaptureWriteStream extends EventEmitter {
  columns = 100
  rows = 30
  isTTY = true
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
  resume(): this { return this }
  pause(): this { return this }
  read(): string | null { return this.queue.shift() ?? null }
  send(text: string): void {
    this.queue.push(text)
    this.emit('readable')
  }
}

function delay(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}

async function waitFor(label: string, predicate: () => boolean, timeoutMs = 8000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (predicate()) return
    await delay(50)
  }
  throw new Error(`${label} timed out`)
}

/** SGR mouse report: SGR is 1-based, uppercase M = press, lowercase m = release. */
function sgr(button: number, x: number, y: number, release = false): string {
  return `\u001B[<${button};${x};${y}${release ? 'm' : 'M'}`
}

/** One decodable OSC 52 clipboard write. */
function decodedOsc52(chunks: string[]): string[] {
  const raw = chunks.join('')
  const out: string[] = []
  for (const match of raw.matchAll(/\u001B\]52;c;([A-Za-z0-9+/=]+)\u0007/g)) {
    out.push(Buffer.from(match[1]!, 'base64').toString('utf-8'))
  }
  return out
}

/**
 * Find the absolute screen row of the composer, read out of the *painted screen*.
 *
 * Deliberately not recomputed from layout constants: the whole point is that the app's
 * idea of where the composer is matches where it painted it. The virtual screen replays
 * the terminal output, so this reads what a user would see -- the same thing Claude Code
 * reads out of `frontFrame.screen`.
 *
 * `debug: false` on the Ink render is required for this to work at all: in debug mode Ink
 * prints each frame sequentially without cursor addressing, so there is no screen to
 * reconstruct.
 */
function composerScreenRow(raw: string, columns: number, rows: number): { row: number; borderRow: number; lines: string[] } {
  const lines = paintedFrame(raw, rows, columns)
  const composerRow = frameRow(lines, /^\s*> /)
  if (composerRow === -1) {
    throw new Error(
      'composer row not found on the painted screen; non-empty lines:\n' +
        lines.filter(line => line.length > 0).map((line, index) => `${index}|${line}`).join('\n'),
    )
  }
  // The top border is the rule directly above the composer.
  let borderRow = -1
  for (let row = composerRow - 1; row >= 0; row -= 1) {
    if (/^─{8,}\s*$/.test(lines[row]!)) { borderRow = row; break }
  }
  return { row: composerRow, borderRow, lines }
}

function stripAnsi(text: string): string {
  return text
    .replace(/\u001B\][^\u0007]*(?:\u0007|\u001B\\)/g, '')
    .replace(/\u001B\[[0-?]*[ -/]*[@-~]/g, '')
    .replace(/\u001B[=>]/g, '')
    .replace(/\r/g, '')
}

async function main(): Promise<number> {
  // OSC 52 through the captured stdout: byte-checkable, and it cannot touch the real
  // machine clipboard.
  process.env.GA_CLIPBOARD_REMOTE = '1'
  delete process.env.GA_CLIPBOARD_WRITE
  // Full mouse capture, which is the default and the mode under test.
  delete process.env.GA_INK_MOUSE
  delete process.env.GA_INK_FULLSCREEN

  // A token that exists only in this run, so a hit in the clipboard payload cannot be a
  // coincidence from the transcript or a stale frame.
  const token = `COMPOSERSEL${Date.now().toString(36).toUpperCase()}`
  const composerText = `hello ${token}`

  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
  const stdin = new FakeReadStream()
  const cursorPark = createCursorParkStdout(stdout as unknown as NodeJS.WriteStream)

  // A bridge stub: this test is about geometry, not about the agent. It has to be a real
  // BridgeClient shape so App's effects wire up normally. `onEvent` is how App receives
  // state, so the stub feeds a turn into the transcript the same way the real bridge
  // would -- that is what puts real content above the composer and makes an off-by-N
  // row error visible.
  const commands: BridgeCommand[] = []
  let emit: ((event: BridgeEvent) => void) | null = null
  const startBridgeClient = (
    _python: string,
    _script: string,
    onEvent: (event: BridgeEvent) => void,
  ): BridgeClient => {
    emit = onEvent
    return {
      send(command) { commands.push(command) },
      stop() {},
    }
  }
  const instance = render(React.createElement(App, {
    python: 'python',
    bridgeScript: 'unused',
    startBridgeClient,
    cursorPark,
  }), {
    stdout: cursorPark.stdout,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    patchConsole: false,
    // Cursor-addressed rendering is what makes the painted screen reconstructible; debug
    // mode prints frames one after another with no addressing.
    debug: false,
  })

  await waitFor('initial frame', () => stdout.chunks.length > 0)

  // Seed a turn into the transcript through the real event path, so there is content
  // above the composer. Without it the composer sits directly under the header and an
  // off-by-N row error would still land somewhere plausible.
  const seed = `TRANSCRIPT_SEED_${token}`
  emit?.({ type: 'ready', version: 1 })
  emit?.({ type: 'user', taskId: 1, text: `earlier turn ${seed}` })
  emit?.({ type: 'assistant_done', taskId: 1, text: `first answer ${seed}` })
  emit?.({ type: 'user', taskId: 2, text: `second turn ${seed}` })
  emit?.({ type: 'assistant_done', taskId: 2, text: `second answer ${seed}` })
  await delay(200)

  // Type into the composer without submitting: the text stays in the input box.
  for (const character of composerText) {
    stdin.send(character)
    await delay(4)
  }
  await waitFor('composer repaint', () => {
    const plain = stripAnsi(stdout.chunks.slice(-20).join(''))
    return plain.includes(token.slice(0, 6))
  })
  await delay(150)

  const { row: composerRow, borderRow } = composerScreenRow(stdout.chunks.join(''), 100, 30)
  assert.ok(composerRow > borderRow, 'composer row must be below the top border')

  // Drag across the composer text: press on the 'h', drag right, release. Columns are
  // absolute canvas columns; the row paints as " > hello ..." -- one column of box
  // padding, then the '>' gutter, then a space -- so the text starts at column 4 (1-based).
  //
  // The end column is exclusive (a selection is a half-open column span), so it is
  // startColumn + length, not startColumn + length - 1. Using the latter dropped the last
  // character, which is exactly the kind of off-by-one a token check catches.
  const startColumn = 4
  const endColumn = startColumn + composerText.length
  stdin.send(sgr(0, startColumn, composerRow + 1))
  await delay(30)
  stdin.send(sgr(32, endColumn, composerRow + 1))
  await delay(30)
  stdin.send(sgr(0, endColumn, composerRow + 1, true))
  await delay(500)

  const payloads = decodedOsc52(stdout.chunks)
  const joined = payloads.join('\n---\n')

  if (!joined.includes(token)) {
    console.error('FAIL: no clipboard payload contained the composer token')
    console.error('composer row (0-based):', composerRow, 'borders:', borderRow)
    console.error('payloads:', JSON.stringify(payloads))
    const plain = stripAnsi(stdout.chunks.slice(-6).join(''))
    console.error('last frame (plain):\n' + plain.split('\n').map((line, index) => `${String(index).padStart(2)}|${line}`).join('\n'))
    instance.unmount()
    return 1
  }
  assert.ok(
    joined.includes(composerText),
    `clipboard payload should contain the whole composer line, got: ${JSON.stringify(payloads)}`,
  )
  console.log('OK composer selection copied:', JSON.stringify(payloads.at(-1)))
  instance.unmount()
  return 0
}

main().then(code => {
  process.exitCode = code
}).catch(error => {
  console.error('FAIL', error)
  process.exitCode = 1
})
