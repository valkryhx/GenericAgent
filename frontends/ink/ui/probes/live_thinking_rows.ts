/**
 * 探针：实时 thinking 的消息形状与渲染位置。
 *
 * ## 测什么
 *
 * 1. `assistant_thinking` 事件被 reducer 折成什么形状（一条独立 done 消息）。
 * 2. 渲染出来的行序：`✻ REASON` 必须在正文之上，且 thinking-only 消息不能
 *    多出一个空白正文行。
 * 3. 对照组：不装 thinking 的普通消息，行数必须和以前逐字节一致（回归护栏）。
 *
 * ## 根因坐标（已修）
 *
 * - `genericagent/llm/core.py:_parse_claude_sse` 的 `thinking_delta` 分支只
 *   累加 `current_block`；`_parse_openai_sse` 的 `reasoning_text` 同样只进
 *   blocks。两者都不向调用方 yield 任何东西 —— 而 `chat()` 的 yield 契约是
 *   显示文本，reasoning 本来也不该混进去。
 * - `frontends/ink/ui/src/state.ts` 的 `assistant_thinking` 分支。
 *
 * ## 为什么是确定性的
 *
 * 全程纯函数：喂事件序列，读 `state.messages` 和 `transcriptLines()` 的行。
 * 没有计时、没有终端、没有网络。
 *
 * ## 为什么必须留着
 *
 * 「reasoning 显示在正文之上」这件事依赖一个**顺序**事实：thinking 块在正文
 * 之前完成、且流式正文会被切成 `a-N-cK` 段提交。任何一个环节改顺序，行就会
 * 掉到正文下面，而那在真机上只是「看起来有点怪」，很难立刻归因。这个探针是
 * 那条因果链唯一的可重复证据。
 *
 * 运行：cd frontends/ink/ui && npx tsx probes/live_thinking_rows.ts
 */
import assert from 'node:assert/strict'
import { applyBridgeEvent, initialState } from '../src/state.js'
import { transcriptLines } from '../src/messageWindow.js'
import { GLYPHS } from '../src/glyphs.js'

let failures = 0
function check(name: string, fn: () => void): void {
  try {
    fn()
    console.log(`✔ ${name}`)
  } catch (error) {
    failures += 1
    console.log(`✖ ${name}\n    ${(error as Error).message}`)
  }
}

function rowsOf(state: ReturnType<typeof applyBridgeEvent>): string[] {
  return transcriptLines(state.messages, { expandedTools: false }).map(row => row.text)
}

// ── 主场景：一个 thinking 块 + 一轮正文 ──────────────────────────────
let state = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
state = applyBridgeEvent(state, { type: 'user', taskId: 1, text: '问题' })
state = applyBridgeEvent(state, { type: 'assistant_thinking', taskId: 1, text: '先想一下再答。' })
state = applyBridgeEvent(state, { type: 'assistant_delta', taskId: 1, text: '答案。' })
state = applyBridgeEvent(state, { type: 'assistant_done', taskId: 1, text: '答案。' })
const rows = rowsOf(state)

console.log('=== 折叠态行序列 ===')
rows.forEach((row, index) => console.log(`  ${String(index).padStart(2)}| ${row}`))
console.log()

check('thinking 行在正文之上', () => {
  const thinkIdx = rows.findIndex(row => row.includes(GLYPHS.star) && row.includes('先想一下再答'))
  const bodyIdx = rows.findIndex(row => row.includes('答案。'))
  assert.ok(thinkIdx !== -1, `没渲染出 thinking 行: ${JSON.stringify(rows)}`)
  assert.ok(bodyIdx !== -1, `没渲染出正文: ${JSON.stringify(rows)}`)
  assert.ok(thinkIdx < bodyIdx, `thinking 落在正文下方: think=${thinkIdx} body=${bodyIdx}`)
})

check('thinking-only 消息不多出空白正文行', () => {
  // 正文为空时 formatAssistantText('') || ' ' 会产出一个空格行；那是给流式
  // 占位用的。落在 done 的 thinking 消息上，它就变成 reasoning 与正文之间
  // 的一行纯空白噪声。只数 thinking 行之后的空行——user 块自带的那一行对照
  // 组也有，属于既有的块分隔，不是这次引入的。
  const thinkIdx = rows.findIndex(row => row.includes(GLYPHS.star) && row.includes('先想一下再答'))
  const bodyIdx = rows.findIndex(row => row.includes('答案。'))
  const between = rows.slice(thinkIdx + 1, bodyIdx)
  const blanks = between.filter(row => row.trim() === '')
  // 恰好一个边界空行：与对照组「正文前一个空行」同级。
  assert.equal(blanks.length, 1, `thinking 与正文之间应只有 1 个空行，实际 ${blanks.length}: ${JSON.stringify(between)}`)
})

check('thinking 文本没有渗进正文', () => {
  assert.ok(!rows.join('\n').includes('先想一下再答。\n答案'), '推理被当成正文拼进去了')
  const bodyRow = rows.find(row => row.includes('答案。'))!
  assert.ok(!bodyRow.includes('先想一下'), `正文行里混进了推理: ${bodyRow}`)
})

// ── 对照组：没有 thinking 的普通消息，行数与旧行为一致 ────────────────
let plain = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
plain = applyBridgeEvent(plain, { type: 'user', taskId: 2, text: '问题' })
plain = applyBridgeEvent(plain, { type: 'assistant_delta', taskId: 2, text: '答案。' })
plain = applyBridgeEvent(plain, { type: 'assistant_done', taskId: 2, text: '答案。' })
const plainRows = rowsOf(plain)
console.log('=== 对照组（无 thinking）===')
plainRows.forEach((row, index) => console.log(`  ${String(index).padStart(2)}| ${row}`))
console.log()

check('对照：无 thinking 时不产生 ✻ 行', () => {
  assert.ok(!plainRows.join('\n').includes(GLYPHS.star), `无 thinking 却出现星标: ${JSON.stringify(plainRows)}`)
})

check('对照：正文行内容不受影响', () => {
  assert.ok(plainRows.some(row => row.includes('答案。')), JSON.stringify(plainRows))
})

// ── 展开态：Ctrl+O ──────────────────────────────────────────────────
const expanded = transcriptLines(state.messages, { expandedTools: true }).map(row => row.text)
console.log('=== 展开态行序列 ===')
expanded.forEach((row, index) => console.log(`  ${String(index).padStart(2)}| ${row}`))
console.log()

check('展开态给出 ▾ ✻ thinking 标题', () => {
  const header = expanded.find(row => row.includes(GLYPHS.star) && row.includes('thinking'))
  assert.ok(header, `没有展开标题: ${JSON.stringify(expanded)}`)
  assert.ok(header!.includes(GLYPHS.triangleDown), `展开标记不对: ${header}`)
})

console.log(failures === 0 ? '\n✔ 全部通过' : `\n✖ ${failures} 项失败`)
process.exit(failures === 0 ? 0 : 1)
