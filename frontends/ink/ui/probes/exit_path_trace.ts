/**
 * 探针：Ctrl+C 两次之后，退出链路到底走到哪一步。
 *
 * 测什么
 * ------
 * headless 跑真 App，发两次 `\x03`（间隔 1.2s，在 3000ms 确认窗内），记录：
 *   - 收到几次 Ctrl+C 决策（arm / exit）
 *   - `cleanupTerminalOnce` 有没有写退出序列
 *   - stdout 上有没有出现 `ESC[?1049l` / `ESC[2J`
 *
 * 为什么需要
 * ----------
 * 真机探针（`_probe_exit_screen_residue.py`）显示两次 Ctrl+C 后进程**没有退出**，
 * `ESC[?1049l` 出现 0 次，屏幕上仍留着 "Press Ctrl+C again to exit"。
 * headless 能排除「终端/ConPTY 干扰」，直接看 App 侧的决策与写入。
 *
 * 运行：cd frontends/ink/ui && npx tsx probes/exit_path_trace.ts
 */
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from '../src/App.js'
import { createCursorParkStdout } from '../src/stdoutCursorPark.js'
import type { BridgeClient } from '../src/bridgeClient.js'
import type { BridgeEvent } from '../src/protocol.js'

const delay = (ms: number) => new Promise<void>(r => setTimeout(r, ms))

class Cap extends EventEmitter {
  columns: number
  rows: number
  isTTY = true
  chunks: string[] = []
  constructor(c: number, r: number) { super(); this.columns = c; this.rows = r }
  write(chunk: unknown): boolean { this.chunks.push(String(chunk)); return true }
}
class FakeRead extends EventEmitter {
  isTTY = true
  private q: string[] = []
  setRawMode() { return this }
  setEncoding() { return this }
  ref() { return this }
  unref() { return this }
  read(): string | null { return this.q.shift() ?? null }
  resume() { return this }
  pause() { return this }
  send(t: string) { this.q.push(t); this.emit('readable') }
}

async function main() {
  process.env.GA_INK_FULLSCREEN = 'on'
  const base = new Cap(120, 40)
  const stderr = new Cap(120, 40)
  const stdin = new FakeRead()
  const park = createCursorParkStdout(base as never, { terminalRows: 40, syncOutput: true })

  let emit: ((e: BridgeEvent) => void) | null = null
  let unmounted = false

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
  instance.waitUntilExit().then(() => { unmounted = true }).catch(() => {})

  try {
    await delay(600)
    stdin.send('a')
    await delay(200)

    console.log('第 1 次 Ctrl+C …')
    stdin.send('\u0003')
    await delay(1200)

    console.log('第 2 次 Ctrl+C …')
    stdin.send('\u0003')
    await delay(1800)

    const all = base.chunks.join('')
    console.log(`\nunmount 是否完成 = ${unmounted}`)
    for (const [name, seq] of [
      ['ESC[?1049h 进备用屏', '\u001b[?1049h'],
      ['ESC[?1049l 回主屏', '\u001b[?1049l'],
      ['ESC[2J 清屏', '\u001b[2J'],
      ['ESC[3J 清scrollback', '\u001b[3J'],
    ] as const) {
      console.log(`  ${name}: ${all.split(seq).length - 1} 次`)
    }
    console.log(`\n  尾部 300 字符:\n  ${JSON.stringify(all.slice(-300))}`)
    const verdict = unmounted && all.includes('\u001b[?1049l')
    console.log(`\n${verdict ? '  OK  走完了退出链路' : '  NG  退出链路没走完（进程未退出或没写回主屏序列）'}`)
  } finally {
    try { instance.unmount() } catch {}
    await delay(150)
  }
}

main().catch(e => { console.error(e); process.exit(1) })
