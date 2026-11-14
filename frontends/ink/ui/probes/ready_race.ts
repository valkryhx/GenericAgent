/**
 * 探针：ink 测试 harness 的 `ready` / `running` 竞态。
 *
 * 手动跑：`npx tsx probes/ready_race.ts [轮数]`（不参与 npm test）
 *
 * ## 它测什么
 *
 * 两个症状，一个根因。
 *
 * **症状 A** — `composerDuringRun.test.ts` 的
 * `the native cursor stays visible while the agent is running` 偶发超时：
 * 等 `Running ·` 等 15 秒都等不到。
 *
 * **症状 B** — `App.test.ts` 的
 * `App shows a Codex-style completion summary only after each turn ends` 偶发失败：
 * `Worked for 1s` 出现了，但 token 段 `↑12k ↓1k Σ13k` 没了。
 *
 * **根因** — `applyBridgeEvent` 对 `ready` 的处理（src/state.ts:107）会把
 * `status → idle`、`activityLabel → null`、`tokenUsage → null`。而测试 harness 普遍写成
 *
 * ```ts
 * startBridgeClient: (_p, _b, onEvent) => {
 *   setTimeout(() => onEvent({ type: 'ready' }), 0)   // 排后
 *   return { send() {}, stop() {} }
 * }
 * ```
 *
 * 测试却在**捕获到 emit 的瞬间**就发 `running` / `token_usage`。于是「谁后到」决定
 * 结局：晚到的 `ready` 把已建立的状态清掉，屏幕上就再没有 running chrome / token 段。
 *
 * ## 为什么这个探针是确定性的，而不是靠运气采样
 *
 * 两个 dispatch 谁先被 React 处理，取决于事件循环当下有多忙 —— 整套 90 个测试文件
 * 并行跑时才容易触发。直接按真实时序跑，能否复现全看机器负载，那样的探针不能当证据。
 *
 * 所以这里**显式构造两种顺序**：
 *
 * | 模式 | ready 何时发 | 期望 |
 * | --- | --- | --- |
 * | `stale-order` | 先让 running 落地，50ms 后再发 ready | 复现 bug（failures = 轮数） |
 * | `correct-order` | 等 ready 落地，再发 running | 干净（failures = 0） |
 *
 * 前者证明根因，后者证明修法有效。若哪天 `stale-order` 不再失败，说明 `ready`
 * 不再重置状态 —— 那时这两个测试的修法可以简化，这条探针会先告诉你。
 *
 * ## 为什么值得留
 *
 * 表现（"等不到 Running"、"token 没了"）离根因（"ready 重置了状态"）非常远。
 * 只看断言失败信息会一路怀疑渲染、节流、chunk 拆分 —— 全是错的。这个探针把
 * running chrome 与 token 段分别指向同一件事，一眼可辨。
 */
import React from 'react'
import { render } from 'ink'
import { EventEmitter } from 'node:events'
import { App } from '../src/App.js'
import type { BridgeClient } from '../src/bridgeClient.js'
import type { BridgeEvent } from '../src/protocol.js'
import { createCursorParkStdout } from '../src/stdoutCursorPark.js'
import { inkRenderOptions } from '../src/inkRenderOptions.js'

function stripAnsi(text: string): string {
  return text
    .replace(/\u001B\][^\u0007]*(?:\u0007|\u001B\\)/g, '')
    .replace(/\u001B\[[0-?]*[ -/]*[@-~]/g, '')
    .replace(/\u001B[=>]/g, '')
}

const delay = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms))

/** 捕获流：与测试里那份等价，保留每个 chunk 的边界（chunk 拆分本身也是常见误判来源）。 */
class Capture extends EventEmitter {
  columns = 80
  rows = 24
  chunks: string[] = []
  write(chunk: unknown): boolean {
    this.chunks.push(String(chunk))
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

type Order = 'stale-order' | 'correct-order'

type Mount = {
  stdout: Capture
  /** 发 ready 并等它落地（React 已处理）。 */
  emitReady: () => Promise<void>
  /** 取 emit 句柄；`readyDelayMs` 控制 ready 相对于后续事件的时序。 */
  emit: () => ((event: BridgeEvent) => void) | null
  unmount: () => void
}

/**
 * @param withPark 是否挂 cursorPark（症状 A 需要，症状 B 不需要）。
 * @param readyDelayMs `stale-order` 传 50，让 running 先落地；
 *                     `correct-order` 传 0 并自行 await emitReady()。
 */
function mount(withPark: boolean, ready: { mode: Order }): Mount {
  const stdout = new Capture()
  const stderr = new Capture()
  const stdin = new FakeStdin()
  let emit: ((event: BridgeEvent) => void) | null = null
  let readyLanded = false
  let readySent: Promise<void> | null = null
  let resolveReady: (() => void) | null = null
  readySent = new Promise<void>(resolve => { resolveReady = resolve })

  const startBridgeClient = (
    _python: string,
    _bridgeScript: string,
    onEvent: (event: BridgeEvent) => void,
  ): BridgeClient => {
    emit = onEvent
    const sendReady = () => {
      onEvent({ type: 'ready', version: 1 })
      // resolve 在 emit 之后：等它落地就等于等 React 处理完 ready。
      setTimeout(() => { readyLanded = true; resolveReady?.() }, 0)
    }
    if (ready.mode === 'stale-order') {
      // 刻意晚发：给 running 留出落地窗口，复现"晚到的 ready 清掉状态"。
      setTimeout(sendReady, 50)
    } else {
      setTimeout(sendReady, 0)
    }
    return { send() {}, stop() {} }
  }

  const cursorPark = withPark ? createCursorParkStdout(stdout as unknown as NodeJS.WriteStream) : undefined
  const instance = render(React.createElement(App, {
    python: 'python',
    bridgeScript: 'bridge.py',
    startBridgeClient,
    ...(cursorPark ? { cursorPark } : {}),
  }), {
    ...inkRenderOptions,
    stdout: (cursorPark ? cursorPark.stdout : stdout) as unknown as NodeJS.WriteStream,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    patchConsole: false,
  })

  return {
    stdout,
    emit: () => emit,
    emitReady: async () => {
      await readySent
      // 再等一拍，确保 ready 的那次重绘也落了地。
      for (let i = 0; i < 200 && !readyLanded; i += 1) await delay(10)
      await delay(30)
    },
    unmount: () => instance.unmount(),
  }
}

async function untilEmit(app: Mount): Promise<((event: BridgeEvent) => void) | null> {
  for (let i = 0; i < 500 && app.emit() === null; i += 1) await delay(10)
  return app.emit()
}

/** 症状 A：发出 running 后，`Running ·` 到底有没有到过捕获流？ */
async function probeRunningChrome(order: Order, index: number): Promise<string | null> {
  const app = mount(true, { mode: order })
  try {
    const emit = await untilEmit(app)
    if (!emit) return `${index}: onEvent 从未被捕获`
    if (order === 'correct-order') await app.emitReady()

    emit({ type: 'status', status: 'running', taskId: 1 })
    const deadline = Date.now() + 3000
    while (Date.now() < deadline) {
      if (app.stdout.chunks.some(chunk => stripAnsi(chunk).includes('Running ·'))) return null
      await delay(20)
    }
    const plain = stripAnsi(app.stdout.chunks.join(''))
    const hintLine = /^.*(Running ·|Enter send).*$/m.exec(plain.slice(-500))?.[0]?.trim() ?? '(无)'
    return `${index}: 超时 chunks=${app.stdout.chunks.length} `
      + `最终提示行=${JSON.stringify(hintLine)}`
  } finally {
    app.unmount()
    await delay(20)
  }
}

/** 症状 B：idle 之后 token 段是否还在？ */
async function probeCompletionTokens(order: Order, index: number): Promise<string | null> {
  const app = mount(false, { mode: order })
  try {
    const emit = await untilEmit(app)
    if (!emit) return `${index}: onEvent 从未被捕获`
    if (order === 'correct-order') await app.emitReady()

    emit({ type: 'status', status: 'running', taskId: 1 })
    emit({ type: 'activity', label: 'Calculating' })
    emit({ type: 'token_usage', taskId: 1, inputTokens: 12000, outputTokens: 1000, totalTokens: 13000 })
    await delay(1100)
    emit({ type: 'status', status: 'idle', taskId: 1 })
    await delay(400)

    const frames = app.stdout.chunks.map(stripAnsi).filter(chunk => (
      chunk.includes('GenericAgent')
      || chunk.includes('Enter send')
      || chunk.includes('Running ·')
      || /─{8,}/.test(chunk)
    ))
    const last = frames.at(-1) ?? ''
    if (last.includes('↑12k ↓1k Σ13k')) return null

    const workedLine = /Worked for[^\n]*/.exec(last)?.[0] ?? '(无)'
    return `${index}: token 段丢失 chunks=${app.stdout.chunks.length} `
      + `完成摘要=${JSON.stringify(workedLine)}`
  } finally {
    app.unmount()
    await delay(20)
  }
}

async function run(
  label: string,
  order: Order,
  probe: (order: Order, index: number) => Promise<string | null>,
  rounds: number,
): Promise<number> {
  const failures: string[] = []
  for (let i = 0; i < rounds; i += 1) {
    const failure = await probe(order, i)
    if (failure) failures.push(failure)
  }
  console.log(`\n${label} [${order}] failures=${failures.length}/${rounds}`)
  for (const failure of failures.slice(0, 3)) console.log(`  ${failure}`)
  if (failures.length > 3) console.log(`  …另有 ${failures.length - 3} 轮`)
  return failures.length
}

async function main(): Promise<void> {
  const rounds = Number(process.argv[2] ?? 4)
  console.log(`ready/running 竞态探针（每种顺序 ${rounds} 轮）`)
  console.log('期望：stale-order 全红、correct-order 全绿。')

  const staleA = await run('A: running chrome（带 cursorPark）', 'stale-order', probeRunningChrome, rounds)
  const cleanA = await run('A: running chrome（带 cursorPark）', 'correct-order', probeRunningChrome, rounds)
  const staleB = await run('B: 完成摘要的 token 段', 'stale-order', probeCompletionTokens, rounds)
  const cleanB = await run('B: 完成摘要的 token 段', 'correct-order', probeCompletionTokens, rounds)

  console.log('\n结论：')
  console.log(`  stale-order 复现根因：A=${staleA > 0 ? '是' : '否'} B=${staleB > 0 ? '是' : '否'}`)
  console.log(`  correct-order 已修好：A=${cleanA === 0 ? '是' : '否'} B=${cleanB === 0 ? '是' : '否'}`)
  const ok = staleA > 0 && staleB > 0 && cleanA === 0 && cleanB === 0
  console.log(ok
    ? '  ✔ 探针有效：既证明了根因，也证明了修法。'
    : '  ✖ 宿主行为变了，需要复核 src/state.ts:107 的 ready 处理与两个测试的修法。')
}

main().catch(error => {
  console.error(error)
  process.exit(1)
})
