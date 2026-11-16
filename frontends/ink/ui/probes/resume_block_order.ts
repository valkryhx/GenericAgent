/**
 * 探针：resume 回来后，同一回合内 thinking / text / tool_use 的排列顺序。
 *
 * ## 测什么
 *
 * 直接喂一条带 `blocks` 的 resume 消息（真实会话的块序列：两轮
 * `thinking → text → tool_use` + 各自的 `tool_result`），渲染成行，然后断言
 * 每块出现的行号符合原始交错顺序。
 *
 * ## 用户实测症状
 *
 * 「实时顺序正确，resume 后所有 thinking 都在最前，所有 toolcall 都在最后」。
 * 根因是 `load_v2` 把同回合的块按**类型**压平成三个数组，前端只能按固定顺序
 * （thinking → prose → tools）画一遍。单步回合看不出问题，多步必然错。
 *
 * ## 为什么是确定性的
 *
 * 纯渲染函数 + 手工构造的消息，没有网络、没有计时、没有终端。
 *
 * ## 为什么必须留着
 *
 * 现存的 resume 测试断言的是「块在不在」（条数对不对），对**顺序**全绿——
 * 三个数组各自都完整。只有把三类块放在同一个行序里比较才能发现错位。
 * 对照组（不带 `blocks` 的老消息）钉住兼容性：没有顺序信息时行为不变。
 *
 * 运行：cd frontends/ink/ui && npx tsx probes/resume_block_order.ts
 */
import assert from 'node:assert/strict'
import { transcriptLines } from '../src/messageWindow.js'
import { GLYPHS } from '../src/glyphs.js'
import type { ChatMessage } from '../src/protocol.js'

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

/** 真实会话的块形状：thinking → text → tool_use → tool_result，重复两轮。 */
const resumed: ChatMessage = {
  id: 'a-1',
  role: 'assistant',
  text: '先搜一下。\n再查一次。',
  done: true,
  taskId: 1,
  thinking: ['THINK-1', 'THINK-2'],
  toolUses: [
    { id: 'c1', tool: 'web_search', input: '{"q":"x"}' },
    { id: 'c2', tool: 'web_fetch', input: '{"url":"y"}' },
  ],
  toolResults: [
    { toolUseId: 'c1', text: 'RESULT-1' },
    { toolUseId: 'c2', text: 'RESULT-2' },
  ],
  blocks: [
    { kind: 'thinking', text: 'THINK-1' },
    { kind: 'text', text: '先搜一下。' },
    { kind: 'tool_use', id: 'c1', tool: 'web_search', input: '{"q":"x"}' },
    { kind: 'tool_result', toolUseId: 'c1', text: 'RESULT-1' },
    { kind: 'thinking', text: 'THINK-2' },
    { kind: 'text', text: '再查一次。' },
    { kind: 'tool_use', id: 'c2', tool: 'web_fetch', input: '{"url":"y"}' },
    { kind: 'tool_result', toolUseId: 'c2', text: 'RESULT-2' },
  ],
}

const rows = transcriptLines([resumed], { expandedTools: false }).map(row => row.text)
console.log('=== 有序 resume 渲染 ===')
rows.forEach((row, index) => { if (row.trim()) console.log(`  ${String(index).padStart(2)}| ${row.trim()}`) })
console.log()

function rowIndexOf(predicate: (row: string) => boolean): number {
  return rows.findIndex(predicate)
}
const at = (needle: string) => rowIndexOf(row => row.includes(needle))

check('两轮 thinking 都渲染出来', () => {
  assert.ok(at('THINK-1') !== -1, `THINK-1 缺失: ${JSON.stringify(rows)}`)
  assert.ok(at('THINK-2') !== -1, `THINK-2 缺失: ${JSON.stringify(rows)}`)
})

check('两轮 tool chip 都渲染出来', () => {
  assert.ok(at('web_search') !== -1, `web_search 缺失: ${JSON.stringify(rows)}`)
  assert.ok(at('web_fetch') !== -1, `web_fetch 缺失: ${JSON.stringify(rows)}`)
})

check('第 1 轮：thinking → prose → tool chip 顺序正确', () => {
  const think = at('THINK-1')
  const prose = at('先搜一下')
  const tool = at('web_search')
  assert.ok(think < prose, `THINK-1(${think}) 应在正文(${prose}) 之前`)
  assert.ok(prose < tool, `正文(${prose}) 应在 web_search(${tool}) 之前`)
})

check('第 2 轮：thinking 在第 1 轮工具之后、第 2 轮正文之前', () => {
  const firstRoundResult = at('RESULT-1')
  const think2 = at('THINK-2')
  const prose2 = at('再查一次')
  const tool2 = at('web_fetch')
  assert.ok(think2 > firstRoundResult, `THINK-2(${think2}) 应在前一轮结果(${firstRoundResult}) 之后`)
  assert.ok(think2 < prose2, `THINK-2(${think2}) 应在第 2 轮正文(${prose2}) 之前`)
  assert.ok(prose2 < tool2, `第 2 轮正文(${prose2}) 应在 web_fetch(${tool2}) 之前`)
})

check('所有 thinking 不是全部堆在最前面', () => {
  // 这是用户报的症状。THINK-2 必须出现在第 1 轮的工具之后。
  const think2 = at('THINK-2')
  const firstTool = at('web_search')
  assert.ok(think2 > firstTool, `THINK-2(${think2}) 跑到第一个工具(${firstTool}) 前面了 —— 就是「所有 thinking 在最前」`)
})

check('所有工具不是全部堆在最后面', () => {
  const first = at('web_search')
  const think2 = at('THINK-2')
  assert.ok(first < think2, `web_search(${first}) 应在 THINK-2(${think2}) 之前 —— 否则就是「所有工具在最后」`)
})

check('每个工具结果只出现一次（不在 chip 下和独立块里画两遍）', () => {
  const firstCount = rows.filter(row => row.includes('RESULT-1')).length
  assert.equal(firstCount, 1, `RESULT-1 出现 ${firstCount} 次: ${JSON.stringify(rows)}`)
})

// ── 对照组：没有 blocks 的老消息，行为必须不变 ────────────────────────
const legacy: ChatMessage = {
  id: 'a-2',
  role: 'assistant',
  text: '老消息正文',
  done: true,
  taskId: 2,
  thinking: ['OLD-THINK'],
  toolUses: [{ id: 'c9', tool: 'code_run', input: '{"code":"1"}' }],
  toolResults: [{ toolUseId: 'c9', text: 'OLD-RESULT' }],
}
const legacyRows = transcriptLines([legacy], { expandedTools: false }).map(row => row.text)
console.log('=== 对照组：无 blocks（老消息）===')
legacyRows.forEach((row, index) => { if (row.trim()) console.log(`  ${String(index).padStart(2)}| ${row.trim()}`) })
console.log()

check('对照：无 blocks 时仍按 类型分组 渲染（行为不变）', () => {
  assert.ok(legacyRows.some(row => row.includes(GLYPHS.star)), '老消息丢了 thinking')
  assert.ok(legacyRows.some(row => row.includes('code_run')), '老消息丢了工具')
  assert.ok(legacyRows.some(row => row.includes('OLD-RESULT')), '老消息丢了工具结果')
})

console.log(failures === 0 ? '\n✔ 全部通过' : `\n✖ ${failures} 项失败`)
process.exit(failures === 0 ? 0 : 1)
