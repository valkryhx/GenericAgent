/**
 * 探针：量化「光标每帧被 SHOW/HIDE 几次」+「是否有 DEC 2026 原子提交」+「单字符体量」。
 *
 * 这三个是纯字节事实，直接对应「不如 CC 丝滑」的观感：
 *   A. DECTCEM 翻转频率 —— 每帧 1 次 HIDE + 1 次 SHOW。31fps 时 = 62 次/秒 的光标开合，
 *      肉眼就是光标在抖。CC 只在 mount 隐藏一次，帧内不翻转。
 *   B. 是否有 ESC[?2026h/l（同步输出）—— 没有 ⇒ 终端会把「擦除全段 + 重写全段」的中间态
 *      画出来，即每帧一次「空一下再出现」。
 *   C. 单字符引起的字节 —— 整段重写而非 diff。
 *
 * 运行： cd frontends/ink/ui && npx tsx probes/cursor_toggle_cost.ts
 */
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from '../src/App.js'
import { createCursorParkStdout } from '../src/stdoutCursorPark.js'
import type { BridgeClient } from '../src/bridgeClient.js'
import type { BridgeEvent } from '../src/protocol.js'

const NL = '\n'
const delay = (ms: number) => new Promise<void>(r => setTimeout(r, ms))

class CaptureWriteStream extends EventEmitter {
  columns = Number(process.env.PROBE_COLUMNS ?? 100)
  rows = Number(process.env.PROBE_ROWS ?? 30)
  isTTY = true
  chunks: string[] = []
  write(chunk: unknown): boolean { this.chunks.push(String(chunk)); return true }
}
class FakeReadStream extends EventEmitter {
  isTTY = true
  private queue: string[] = []
  setRawMode() { return this }
  setEncoding() { return this }
  ref() { return this }
  unref() { return this }
  read(): string | null { return this.queue.shift() ?? null }
  resume() { return this }
  pause() { return this }
  send(t: string) { this.queue.push(t); this.emit('readable') }
}

const count = (s: string, re: RegExp) => (s.match(re) ?? []).length

async function main() {
  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
  const stdin = new FakeReadStream()
  const park = createCursorParkStdout(stdout as never)

  let emit: ((e: BridgeEvent) => void) | null = null
  const instance = render(
    React.createElement(App, {
      python: 'python', bridgeScript: 'x', cursorPark: park,
      startBridgeClient: (_p: string, _b: string, onEvent: (e: BridgeEvent) => void): BridgeClient => {
        emit = onEvent
        setTimeout(() => onEvent({ type: 'ready', version: 1 }), 0)
        return { send() {}, stop() {} }
      },
    } as never),
    {
      stdout: park.stdout,
      stderr: stderr as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      patchConsole: false,
      exitOnCtrlC: false,
    } as never,
  )

  const e = () => emit as unknown as (ev: BridgeEvent) => void

  const window = (label: string, from: number, elapsedMs: number) => {
    const all = stdout.chunks.slice(from).join('')
    const frames = count(all, /\u001b\[2K/g)          // 近似帧数（每帧 N 次擦行）
    const hides = count(all, /\u001b\[\?25l/g)
    const shows = count(all, /\u001b\[\?25h/g)
    const bsu = count(all, /\u001b\[\?2026h/g)
    const esu = count(all, /\u001b\[\?2026l/g)
    const parkWrites = count(all, /\u001b\[\?25h$/gm)
    const bytes = all.length
    const secs = elapsedMs / 1000
    console.log(`\n--- ${label}（${elapsedMs}ms）---`)
    console.log(`  帧/擦行次数   = ${frames}`)
    console.log(`  HIDE(?25l)    = ${hides}      → ${(hides / secs).toFixed(1)} 次/秒`)
    console.log(`  SHOW(?25h)    = ${shows}      → ${(shows / secs).toFixed(1)} 次/秒`)
    console.log(`  同步输出 2026h/l = ${bsu} / ${esu}   ${bsu === 0 ? '<<未使用：终端会画出擦除-重写的中间态' : ''}`)
    console.log(`  字节          = ${bytes}      → ${(bytes / secs / 1024).toFixed(1)} KB/s`)
    console.log(`  park 写入次数 = ${parkWrites}`)
  }

  try {
    await delay(400)

    // 1) 空闲：不打字、不流式（应当完全静默）
    let from = stdout.chunks.length
    let t = Date.now()
    await delay(1000)
    window('空闲 1s（应当 0 写入）', from, Date.now() - t)

    // 2) 空闲逐字输入 8 个字符（间隔 120ms）
    from = stdout.chunks.length
    t = Date.now()
    for (const ch of 'abcdefgh'.split('')) { stdin.send(ch); await delay(120) }
    await delay(150)
    window('空闲 · 逐字输入 8 字符', from, Date.now() - t)

    // 3) 运行中流式 2 秒（约 50 delta/s）
    e()({ type: 'status', status: 'running', taskId: 1 })
    e()({ type: 'user', taskId: 1, text: 'stream' })
    await delay(200)
    from = stdout.chunks.length
    t = Date.now()
    const pump = (async () => {
      for (let i = 0; i < 100; i += 1) {
        e()({ type: 'assistant_delta', taskId: 1, text: `line ${i} ` + 'k'.repeat(25) + NL })
        await delay(20)
      }
    })()
    await pump
    await delay(100)
    window('运行中 · 流式输出', from, Date.now() - t)

    // 4) 流式中打字（用户抱怨最明显）
    from = stdout.chunks.length
    t = Date.now()
    const pump2 = (async () => {
      for (let i = 0; i < 40; i += 1) {
        e()({ type: 'assistant_delta', taskId: 1, text: `more ${i}` + NL })
        await delay(20)
      }
    })()
    for (const ch of 'hello'.split('')) { stdin.send(ch); await delay(100) }
    for (let i = 0; i < 3; i += 1) { stdin.send('\u007f'); await delay(100) }
    await pump2
    await delay(150)
    window('运行中 · 流式 + 打字/回退', from, Date.now() - t)

    console.log(`\n=== 对照（Claude Code fork 源码）===`)
    console.log(`  FRAME_INTERVAL_MS = 16（src/ink/constants.ts）  → 上限 62fps`)
    console.log(`  LogUpdate.render 输出 Patch[] diff，只写变化 cell（src/ink/log-update.ts）`)
    console.log(`  writeDiffToTerminal 用 BSU/ESU 包住整帧（src/ink/terminal.ts:203）`)
    console.log(`  原生光标：mount 时 HIDE_CURSOR 一次（components/App.tsx:184），帧内不再翻转`)
    console.log(`\n=== 对照（GA 现装的 ink@5.2.1）===`)
    console.log(`  rootNode.onRender = throttle(onRender, 32)   → 上限 31fps`)
    console.log(`  logUpdate.render = eraseLines(prevN) + output（整段重写，非 diff）`)
    console.log(`  无 BSU/ESU；cursor park writer 每帧 HIDE 一次 + SHOW 一次`)
  } finally {
    instance.unmount()
    await delay(150)
  }
}

main().catch(err => { console.error(err); process.exit(1) })
