/**
 * 真机验证：拖拽到视口外会不会自动滚动。
 *
 * ## 为什么不用 PTY
 *
 * PTY 路线走过（`tests/probes/python/drag_scroll_probe.py`），失败的形态很明确：
 * Ink 的粘贴层把注进去的字符折叠成 `[Copied text #1 +1 lines]`，鼠标序列
 * 也没走到 GA 的处理函数。抓包证据在 `captures/drag_scroll.raw.txt`。
 *
 * 这里改走**真 App + 假流**：渲染的是真的 `App.tsx`（真 Ink、真键盘/鼠标
 * 处理、真布局），只有 stdin/stdout 是假的。这样能精确注入 SGR 鼠标序列，
 * 又能从 stdout 字节里看到真实渲染结果。
 *
 * ## 判据
 *
 * 拖之前记下屏幕上可见的 `line N` 首行号，一路拖到视口顶部之后再看首行号。
 * 首行号变小 = 真的往历史方向滚了（用户要的「自动随着滑动扩展」）。
 *
 * 用法：npx tsx probes/drag_scroll_real.ts
 */
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from '../src/App.js'
import type { BridgeClient } from '../src/bridgeClient.js'
import type { BridgeEvent } from '../src/protocol.js'

const NL = String.fromCharCode(10)
const delay = (ms: number) => new Promise(r => setTimeout(r, ms))

class CapStream extends EventEmitter {
  columns: number
  rows: number
  isTTY = true
  chunks: string[] = []
  constructor(columns: number, rows: number) {
    super()
    this.columns = columns
    this.rows = rows
  }
  write(chunk: unknown): boolean {
    const s = String(chunk)
    this.chunks.push(s)
    this.onChunk?.(s)
    return true
  }
  onChunk: ((s: string) => void) | null = null
  get text(): string {
    return this.chunks.join('')
  }
  reset(): void {
    this.chunks.length = 0
  }
}

/**
 * 假 stdin。
 *
 * **必须实现 `readable` + `read()` 队列**：Ink 的 `App.handleReadable` 监听的是
 * `readable` 事件、然后循环调 `stdin.read()` 取数据（node_modules/ink/build/
 * components/App.js:131-138）。只 `emit('data', ...)` 是收不到的 —— 探针第一版
 * 就是这样，鼠标序列全都石沉大海。
 */
class FakeReadStream extends EventEmitter {
  isTTY = true
  private queue: Buffer[] = []
  setRawMode() { return this }
  setEncoding() { return this }
  ref() { return this }
  unref() { return this }
  resume() { return this }
  pause() { return this }
  read(): Buffer | null {
    return this.queue.shift() ?? null
  }
  push(chunk: string): void {
    this.queue.push(Buffer.from(chunk, 'utf8'))
    this.emit('readable')
  }
}

/** SGR 鼠标报告（1-based 坐标）。 */
const sgr = (button: number, x: number, y: number, release = false): string =>
  `\u001B[<${button};${x};${y}${release ? 'm' : 'M'}`

/**
 * 最小屏幕仿真：按 ANSI 光标/擦除序列真正维护一块画布。
 *
 * 为什么不能用「把 stdout 尾巴拼起来再看」：Ink 是增量渲染，同一行会被反复
 * 擦写；把多帧拼起来会看到早已被覆盖的内容，于是「滚动了」和「没滚动」都
 * 能从同一段字节里读出来 —— 那就是假验收。必须仿真真实屏幕。
 *
 * 支持的序列（Ink 实际用到的）：
 * - `ESC[<n>A/B/C/D` 光标相对移动
 * - `ESC[<n>G` 列定位、`ESC[<r>;<c>H` 绝对定位
 * - `ESC[2K` / `ESC[K` 擦除整行 / 光标到行尾
 * - `ESC[H` 归位、`ESC[2J` 清屏
 * - SGR / 光标显隐 / 鼠标模式等其余 CSI：忽略
 */
class ScreenBuffer {
  lines: string[] = []
  row = 0
  col = 0

  constructor(private rows: number, private cols: number) {
    for (let i = 0; i < rows; i += 1) this.lines.push('')
  }

  private ensure(row: number): void {
    while (this.lines.length <= row) this.lines.push('')
  }

  private put(ch: string): void {
    this.ensure(this.row)
    const line = this.lines[this.row]!
    const padded = line.padEnd(this.col, ' ')
    this.lines[this.row] = padded.slice(0, this.col) + ch + padded.slice(this.col + 1)
    this.col += 1
  }

  private eraseLine(mode: number): void {
    this.ensure(this.row)
    const line = this.lines[this.row]!
    if (mode === 2) this.lines[this.row] = ''
    else if (mode === 0) this.lines[this.row] = line.slice(0, this.col)
    else if (mode === 1) this.lines[this.row] = ' '.repeat(this.col) + line.slice(this.col)
  }

  feed(chunk: string): void {
    let i = 0
    while (i < chunk.length) {
      const c = chunk[i]!
      if (c === '\u001B') {
        const rest = chunk.slice(i)

        // ESC[<r>;<c>H / f 绝对定位
        let m = /^\u001B\[(\d+);(\d+)[Hf]/.exec(rest)
        if (m) {
          this.row = Number(m[1]) - 1
          this.col = Number(m[2]) - 1
          i += m[0].length
          continue
        }
        // ESC[H 归位
        m = /^\u001B\[H/.exec(rest)
        if (m) {
          this.row = 0
          this.col = 0
          i += m[0].length
          continue
        }
        // ESC[2J 清屏
        m = /^\u001B\[2J/.exec(rest)
        if (m) {
          this.lines = new Array(this.rows).fill('')
          this.row = 0
          this.col = 0
          i += m[0].length
          continue
        }
        // ESC[<n>K 擦除行
        m = /^\u001B\[(\d*)K/.exec(rest)
        if (m) {
          this.eraseLine(m[1] ? Number(m[1]) : 0)
          i += m[0].length
          continue
        }
        // ESC[<n>A/B/C/D 相对移动
        m = /^\u001B\[(\d*)([ABCD])/.exec(rest)
        if (m) {
          const n = m[1] ? Number(m[1]) : 1
          if (m[2] === 'A') this.row = Math.max(0, this.row - n)
          else if (m[2] === 'B') this.row += n
          else if (m[2] === 'C') this.col += n
          else this.col = Math.max(0, this.col - n)
          i += m[0].length
          continue
        }
        // ESC[<n>G 列定位
        m = /^\u001B\[(\d+)G/.exec(rest)
        if (m) {
          this.col = Number(m[1]) - 1
          i += m[0].length
          continue
        }
        // 其余 CSI（SGR / 光标显隐 / 鼠标模式 / 同步输出…）整段跳过
        m = /^\u001B\[[0-9;?]*[A-Za-z]/.exec(rest)
        if (m) {
          i += m[0].length
          continue
        }
        // OSC
        m = /^\u001B\][^\u0007]*\u0007/.exec(rest)
        if (m) {
          i += m[0].length
          continue
        }
        i += 1
        continue
      }
      if (c === '\r') {
        this.col = 0
        i += 1
        continue
      }
      if (c === '\n') {
        this.row += 1
        this.ensure(this.row)
        i += 1
        continue
      }
      if (c >= ' ') {
        this.put(c)
      }
      i += 1
    }
  }

  numbers(): number[] {
    const nums: number[] = []
    for (const line of this.lines) {
      const m = /line (\d+)/.exec(line)
      if (m) nums.push(Number(m[1]))
    }
    return nums
  }
}

async function main() {
  const out: string[] = []
  const say = (s: string) => out.push(s)

  let emit: ((e: BridgeEvent) => void) | null = null
  const startBridgeClient = (
    _p: string,
    _s: string,
    onEvent: (e: BridgeEvent) => void,
  ): BridgeClient => {
    emit = onEvent
    setTimeout(() => onEvent({ type: 'ready', version: 1 }), 0)
    return { send() {}, stop() {} }
  }

  const stdout = new CapStream(100, 30)
  const stderr = new CapStream(100, 30)
  const stdin = new FakeReadStream()
  // 持续把 stdout 的增量喂进屏幕仿真
  const screen = new ScreenBuffer(30, 100)
  stdout.onChunk = chunk => screen.feed(chunk)

  const instance = render(
    React.createElement(App, {
      python: 'python',
      bridgeScript: 'bridge.py',
      startBridgeClient,
    }),
    {
      stdout: stdout as unknown as NodeJS.WriteStream,
      stderr: stderr as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      patchConsole: false,
    },
  )

  const feed = (s: string) => {
    stdin.push(s)
  }

  try {
    await delay(600)
    const sink = emit as unknown as ((e: BridgeEvent) => void) | null
    if (!sink) throw new Error('bridge 没有回传 onEvent，拿不到事件注入通道')

    // 造出足够多的行。必须**逐条 delta**：一次性灌 80 行时 Ink 只渲染尾部，
    // 前面的行不会进入可见帧（探针第一版就踩了这个，抓到 0 行）。
    sink({ type: 'status', status: 'running', taskId: 1 })
    sink({ type: 'user', taskId: 1, text: 'PROMPT' })
    const allText = Array.from({ length: 80 }, (_, i) => `line ${i + 1}`).join(NL)
    for (let i = 1; i <= 80; i += 1) {
      sink({ type: 'assistant_delta', taskId: 1, text: `line ${i}` + NL })
      await delay(40)
    }
    // assistant_done 的 text 是整轮全文，会**覆盖**已流式的内容。
    sink({ type: 'assistant_done', taskId: 1, text: allText })
    sink({ type: 'status', status: 'idle', taskId: 1 })
    await delay(2000)

    await delay(400)
    const before = screen.numbers()
    say('=== 1. 滚动前 ===')
    say('可见 line 号: ' + JSON.stringify([...new Set(before)].sort((a, b) => a - b)))
    if (new Set(before).size < 5) {
      say('!! 可见行太少，无法验证滚动')
      return finish(out, false)
    }

    // 拖拽：视口内按下 → 一路拖到顶部 → 在顶部反复拖 → 释放
    say('')
    say('=== 2. 拖拽（按下 → 拖出视口顶部 → 释放）===')
    feed(sgr(0, 5, 15))          // 在第 15 行按下
    await delay(200)
    for (const y of [12, 9, 6, 3, 1]) {
      feed(sgr(32, 5, y))        // 32 = 左键按住移动
      await delay(150)
      say(`  drag -> y=${y}`)
    }
    for (let i = 0; i < 8; i += 1) {
      feed(sgr(32, 5, 1))
      await delay(150)
    }
    say('  在 y=1 上额外拖了 8 次')
    feed(sgr(0, 5, 1, true))     // 释放
    await delay(800)

    await delay(600)
    const after = screen.numbers()
    say('')
    say('=== 3. 滚动后 ===')
    say('可见 line 号: ' + JSON.stringify([...new Set(after)].sort((a, b) => a - b)))

    const beforeSet = new Set(before)
    const afterSet = new Set(after)
    const minBefore = Math.min(...beforeSet)
    const minAfter = afterSet.size > 0 ? Math.min(...afterSet) : minBefore
    const scrolled = minAfter < minBefore

    say('')
    say('=== 4. 判定 ===')
    say(`滚动前首行 = ${minBefore}, 滚动后首行 = ${minAfter}`)
    say('CHECK 真的滚了   : ' + (scrolled ? 'PASS' : 'FAIL'))
    say('CHECK 朝历史方向 : ' + (scrolled ? 'PASS' : 'FAIL(没看到更早的行)'))

    return finish(out, scrolled)
  } finally {
    instance.unmount()
  }
}

async function finish(out: string[], ok: boolean): Promise<void> {
  const text = out.join(NL)
  process.stdout.write(text + NL)
  process.stderr.write(ok ? 'PASS' + NL : 'FAIL' + NL)

  const fs = await import('node:fs')
  const path = await import('node:path')
  const dir = path.resolve(import.meta.dirname, '../../../../tests/probes/captures')
  fs.mkdirSync(dir, { recursive: true })
  fs.writeFileSync(path.join(dir, 'drag_scroll_real.txt'), text + NL, 'utf8')
}

main().catch(e => {
  console.error(e)
  process.exit(1)
})
