/**
 * 探针：resize 之后 CHARLIE 行到底长什么样 —— 是**真丢了**，还是**被折行**了？
 *
 * 手动跑：`npx tsx probes/resize_screen_dump.ts [延迟ms]`（不参与 npm test）
 *
 * ## 为什么要这个探针
 *
 * `resize_repeat.ts` 报 `CHARLIE 回来于 t+70ms`，但最终计数是 0。同一轮里"回来了"
 * 又说"最终没有"，只可能有两种解释：
 *
 *   (a) 真的回来了，随后又被某个尾帧擦掉；
 *   (b) 行还在，但**被折行**成两行，于是 `line.includes('HIST-PROBE-CHARLIE')`
 *       匹配不到 —— 探针在产假红。
 *
 * 这两个后果完全相反：(a) 是真 bug，(b) 是我的断言写错了。`resize_repeat` 里
 * 那些 `0/1/1` 的轮次，可见行数总是少 1（10 vs 11），正好像"一行被折成两行"。
 *
 * 所以这里把 resize 后的完整屏幕（带行号、带宽度）原样打印，不做任何 includes 判断，
 * 用眼睛区分这两种情况。
 */
import React from 'react'
import { render } from 'ink'
import { EventEmitter } from 'node:events'
import { App } from '../src/App.js'
import { inkRenderOptions } from '../src/inkRenderOptions.js'
import { createCursorParkStdout } from '../src/stdoutCursorPark.js'
import { ReflowTerminal } from '../src/resizeReflowModel.js'
import type { BridgeClient } from '../src/bridgeClient.js'
import type { BridgeEvent } from '../src/protocol.js'

const NL = String.fromCharCode(10)
const delay = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms))

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

class FakeStdin extends EventEmitter {
  isTTY = true
  setRawMode() { return this }
  setEncoding() { return this }
  ref() { return this }
  unref() { return this }
  read() { return null }
  resume() { return this }
  pause() { return this }
}

function dump(label: string, screen: string[]): void {
  console.log(`\n--- ${label} ---（共 ${screen.length} 行，宽 ${60}）`)
  screen.forEach((line, index) => {
    const trimmed = line.trimEnd()
    if (!trimmed) {
      console.log(`  ${String(index).padStart(2)} |`)
      return
    }
    console.log(`  ${String(index).padStart(2)} | ${trimmed}`)
  })
}

async function main(): Promise<void> {
  const delayMs = Number(process.argv[2] ?? 48)
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
    resizeRemountDelayMs: delayMs,
  }), {
    ...inkRenderOptions,
    stdout: cursorPark.stdout,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: new FakeStdin() as unknown as NodeJS.ReadStream,
    patchConsole: false,
  })

  try {
    await delay(120)
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

    dump('resize 之前（80 列）', stdout.term.screen())

    stdout.columns = 60
    stderr.columns = 60
    stdout.emit('resize')

    await delay(Math.max(400, delayMs + 300))
    dump(`resize 之后稳定态（60 列，delay=${delayMs}ms）`, stdout.term.screen())

    // 用一个"不依赖单行完整匹配"的判据再数一遍：把所有可见行拼起来找子串。
    const joined = stdout.term.screen().join(NL)
    console.log('\n判据对比：')
    console.log(`  逐行 includes('HIST-PROBE-CHARLIE') = `
      + `${stdout.term.screen().filter(line => line.includes('HIST-PROBE-CHARLIE')).length} 行`)
    console.log(`  拼接后 includes('HIST-PROBE-CHARLIE') = ${joined.includes('HIST-PROBE-CHARLIE')}`)
    console.log(`  拼接后 includes('HIST-PROBE-') 出现次数 = `
      + `${(joined.match(/HIST-PROBE-/g) ?? []).length}`)
    console.log(`  'CHARLIE' 在拼接后出现次数 = ${(joined.match(/CHARLIE/g) ?? []).length}`)
    console.log('\n若「拼接后 true 但逐行 0 行」，说明行被折行 —— resize_repeat 的假红来自断言写法。')
  } finally {
    instance.unmount()
    await delay(30)
  }
}

main().catch(error => {
  console.error(error)
  process.exit(1)
})
