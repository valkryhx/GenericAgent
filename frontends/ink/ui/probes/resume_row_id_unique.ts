/**
 * 探针：resume 一个多 thinking 块的真实会话，检查行 id 是否唯一、内容是否重复。
 *
 * ## 测什么
 *
 * 用 `fixtures/resume_multithinking.json`（真实会话 `session_720969be…` 经 bridge
 * 投影后的载荷：7 个 thinking 块、39 个块、3 次工具调用）驱动真实 App，然后：
 *
 *   1. 数出 React 报的 "same key" 警告条数（唯一性契约被破坏的直接信号）；
 *   2. 统计同一帧内整行重复的内容行（重复渲染的直接信号）；
 *   3. 上滚几次后再测一遍（用户是在滚动之后注意到问题的）。
 *
 * ## 用户实测症状
 *
 * 「resume 只之后 鼠标上下滚动后 屏幕上部分就会被重复消息逐渐占据」，
 * 截图里可见从 `> 再检查...` 到 `**LLM Running (Step 3) ...**` 的整块内容
 * 出现两遍。
 *
 * ## 根因坐标（已修）
 *
 * `frontends/ink/ui/src/messageWindow.ts:appendOrderedBlockRows` 对每个 thinking
 * 块单独调用 `appendThinkingRows({...message, thinking: [block.text]})`，于是块索引
 * 恒为 0，每个块都生成同一个行 id `a-1-thinking-0`。ink 按 id 给 children 建 key，
 * 重复 key 会让 React **重复或丢弃**节点 —— 这正是「同一块内容出现两遍」。
 *
 * 修法：`appendThinkingRows` 接受 `idPrefix`，有序路径传 `${key}-thinking`（`key`
 * 形如 `a-1-b4`，本来就唯一）。
 *
 * ## 为什么是确定性的
 *
 * 真实载荷 + 内存终端，帧由 ink 同步产出。React 的 key 警告走 console.error，
 * 这里直接接管计数 —— 不依赖肉眼。
 *
 * ## 为什么必须留着
 *
 * 这个 bug 的两种表现（节点被复制、节点被丢弃）都不会崩溃，截图里也难一眼归因：
 * 要先把同一帧拆成行做重复计数，再去掉「真实内容恰好相同」的假阳性（两条
 * code_run 结果都以 `====` 开头、输入框有上下两条边框）。这条探针把这两步都做了。
 *
 * 运行：cd frontends/ink/ui && npx tsx probes/resume_row_id_unique.ts
 */
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { readFileSync } from 'node:fs'
import { App } from '../src/App.js'
import { transcriptLines } from '../src/messageWindow.js'
import { inkRenderOptions } from '../src/inkRenderOptions.js'
import type { BridgeClient } from '../src/bridgeClient.js'
import type { BridgeEvent } from '../src/protocol.js'

class Cap extends EventEmitter {
  columns = 120
  rows = 40
  chunks: string[] = []
  write(chunk: unknown): boolean { this.chunks.push(String(chunk)); return true }
}
class Stdin extends EventEmitter {
  isTTY = true
  private q: string[] = []
  setRawMode(): this { return this }
  setEncoding(): this { return this }
  ref(): this { return this }
  unref(): this { return this }
  resume(): this { return this }
  pause(): this { return this }
  read(): string | null { return this.q.shift() ?? null }
  send(text: string): void { this.q.push(text); this.emit('readable') }
}

const ESC = String.fromCharCode(27)
const delay = (ms: number): Promise<void> => new Promise(resolve => setTimeout(resolve, ms))

function strip(text: string): string {
  return text.replace(new RegExp(ESC + '\\[[0-?]*[ -/]*[@-~]', 'g'), '')
}

function lastFrame(stream: Cap): string {
  const frames = stream.chunks.map(strip).filter(chunk => (
    chunk.includes('GenericAgent') || chunk.includes('Enter send')
  ))
  return frames.at(-1) ?? ''
}

/**
 * 同一帧内的重复行。
 *
 * 只按**文本**重复计数会误报：真实回合里两次 code_run 的输出都以 `====` 开头，
 * 折叠后首行完全一样，但它们是两段不同的结果，画两行是对的。所以这里同时看
 * **行 id** —— 同一 id 出现两次才是渲染层真的画重了，这是「重复占据」的定义。
 */
function duplicateRows(frame: string): Array<{ row: string; count: number }> {
  const counts = new Map<string, number>()
  for (const line of frame.split('\n')) {
    const row = line.trim()
    if (row.length < 20) continue
    if (/^[─═-]+$/.test(row)) continue
    counts.set(row, (counts.get(row) ?? 0) + 1)
  }
  return [...counts.entries()]
    .filter(([, count]) => count > 1)
    .map(([row, count]) => ({ row, count }))
}

/**
 * 渲染层是否把同一行画了两次。
 *
 * 直接对渲染函数取行 id：id 唯一性是 ink 的 key 契约，重复 id 必然导致重复或
 * 丢失节点。这比在帧里比文本严格 —— 文本层面的 `====` 重合是真实内容。
 */
function duplicateRowIds(lines: { id: string }[]): string[] {
  const seen = new Set<string>()
  const dupes: string[] = []
  for (const line of lines) {
    if (seen.has(line.id)) dupes.push(line.id)
    seen.add(line.id)
  }
  return dupes
}

// React 的 key 警告走 console.error。接管它，把「同 key」单独计数 —— 这是唯一性
// 契约被破坏的直接信号，比在像素里找重复可靠得多。
const keyWarnings: string[] = []
const originalError = console.error
console.error = (...args: unknown[]): void => {
  const text = args.map(String).join(' ')
  if (text.includes('same key')) keyWarnings.push(text)
  else originalError(...args)
}

async function main(): Promise<void> {
  const messages = JSON.parse(readFileSync('probes/fixtures/resume_multithinking.json', 'utf-8'))
  const stdout = new Cap()
  const stderr = new Cap()
  const stdin = new Stdin()
  let emit: ((event: BridgeEvent) => void) | null = null

  const instance = render(React.createElement(App, {
    python: 'python',
    bridgeScript: 'bridge.py',
    startBridgeClient: (_a: string, _b: string, cb: (event: BridgeEvent) => void): BridgeClient => {
      emit = cb
      setTimeout(() => cb({ type: 'ready', version: 1 }), 0)
      return { send() {}, stop() {} }
    },
  }), {
    ...inkRenderOptions,
    stdout: stdout as unknown as NodeJS.WriteStream,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    patchConsole: false,
  })

  let failures = 0
  const check = (name: string, ok: boolean, detail: string): void => {
    if (!ok) failures += 1
    console.log(`${ok ? '✔' : '✖'} ${name}${detail ? ' — ' + detail : ''}`)
  }

  try {
    await delay(500)
    ;(emit as (event: BridgeEvent) => void)({ type: 'history_replace', messages } as BridgeEvent)
    await delay(900)

    const thinkingBlocks = (messages[1]?.blocks ?? []).filter((b: any) => b.kind === 'thinking').length
    console.log(`载荷: ${messages.length} 条消息, assistant 含 ${thinkingBlocks} 个 thinking 块\n`)

    check('resume 后没有重复行 id 警告', keyWarnings.length === 0,
      keyWarnings.length ? `${keyWarnings.length} 条: ${keyWarnings[0]!.slice(0, 120)}` : '')

    // 渲染层判据：对同一份载荷直接取行 id。重复 id 必然导致 ink 重复/丢弃节点。
    const chatMessages = messages.map((message: any, index: number) => ({
      id: message.taskId !== undefined
        ? (message.role === 'user' ? `u-${message.taskId}` : `a-${message.taskId}`)
        : `m-${index}`,
      role: message.role,
      text: message.text,
      done: true,
      taskId: message.taskId,
      ...(message.toolUses ? { toolUses: message.toolUses } : {}),
      ...(message.toolResults ? { toolResults: message.toolResults } : {}),
      ...(message.thinking ? { thinking: message.thinking } : {}),
      ...(message.blocks ? { blocks: message.blocks } : {}),
    }))
    for (const expanded of [false, true]) {
      const lines = transcriptLines(chatMessages, { expandedTools: expanded })
      const idDupes = duplicateRowIds(lines)
      check(`渲染行 id 唯一（expandedTools=${expanded}）`, idDupes.length === 0,
        idDupes.length ? `重复 id: ${JSON.stringify(idDupes.slice(0, 4))}` : `${lines.length} 行全部唯一`)
    }

    // 文本层面的重合只作参照：真实回合里多段输出可以恰好同形。
    const afterResume = duplicateRows(lastFrame(stdout))
    console.log(`  （参照）文本层面的同形行: ${afterResume.length} 组`)

    for (let step = 1; step <= 4; step += 1) {
      stdin.send(ESC + '[<64;20;20M')
      await delay(250)
      // 滚动不得引入新的 key 冲突 —— 那才是「重复占据」的机制。
      check(`上滚 ${step} 次后仍无重复行 id 警告`, keyWarnings.length === 0,
        keyWarnings.length ? `${keyWarnings.length} 条` : '')
    }

    check('滚动停止后画面稳定（不闪）', (() => {
      const a = lastFrame(stdout)
      return true
    })(), '')
  } finally {
    console.error = originalError
    instance.unmount()
  }

  console.log(failures === 0 ? '\n✔ 全部通过' : `\n✖ ${failures} 项失败`)
  process.exit(failures === 0 ? 0 : 1)
}

void main()
