/**
 * 探针：多步回合里每条 thinking 相对它那一步正文的位置。
 *
 * ## 测什么
 *
 * 构造一个 5 步回合（每步 = 一个 thinking 块 + 一段正文），然后断言**行序**：
 * 第 K 条 `✻` 必须落在第 K-1 步正文之后、第 K 步正文之前。也就是原始发生顺序。
 *
 * ## 根因坐标
 *
 * `frontends/ink/ui/src/state.ts` 的 `assistant_thinking` 分支。曾经两种摆法都是错的：
 *
 * - **追加到末尾**：一回合 5 条 thinking 全堆在回合最下面（用户实测报告）。
 * - **插到 live 消息之前**：live 消息 `a-{taskId}` 承载的是**跨多个 thinking 边界
 *   累积**的正文，插到它前面等于插到前面几步的正文之上。
 *
 * 正确做法是在 thinking 边界处把 live 消息切成提交段（复用 `a-N-cK` 的 id 形状，
 * 这样 `committedAssistantPrefix` 仍认得它、`assistant_done` 不会重复渲染），
 * 再追加 thinking 行。
 *
 * ## 为什么是确定性的
 *
 * 纯 reducer + 纯渲染函数，没有计时、没有终端、没有网络。
 *
 * ## 为什么必须留着
 *
 * 这个 bug 只在**多步**回合出现，而单步测试（`state.test.ts` 里那条
 * `appends a done message ahead of the prose`）对两种错误摆法都是绿的 ——
 * 它无法区分「对」和「看起来对」。这条探针是唯一会因摆错而变红的检查。
 *
 * 运行：cd frontends/ink/ui && npx tsx probes/live_thinking_multi_step.ts
 */
import assert from 'node:assert/strict'
import { applyBridgeEvent, initialState } from '../src/state.js'
import { transcriptLines } from '../src/messageWindow.js'
import { GLYPHS } from '../src/glyphs.js'

const STEPS = 5
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

// ── 构造真实多步回合 ────────────────────────────────────────────────
let state = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
state = applyBridgeEvent(state, { type: 'user', taskId: 1, text: '你知道黄仁勋最近的经历吗' })
// The real bridge sends the whole turn body on `assistant_done`, not an empty
// string — passing '' here made `remainingAssistantTextAfterCommits` return '' and
// the reducer dropped the still-open live shell, losing the last step's prose.
// Reproduce the real payload so the probe measures the model, not its own fixture.
const turnBody = Array.from({ length: STEPS }, (_, i) =>
  `\nLLM Running (Step ${i + 1}) ...\nBODY-${i + 1}\n`).join('')
for (let step = 1; step <= STEPS; step += 1) {
  state = applyBridgeEvent(state, { type: 'assistant_thinking', taskId: 1, text: `REASON-${step}` })
  state = applyBridgeEvent(state, {
    type: 'assistant_delta',
    taskId: 1,
    text: `\nLLM Running (Step ${step}) ...\nBODY-${step}\n`,
  })
}
state = applyBridgeEvent(state, { type: 'assistant_done', taskId: 1, text: turnBody })

const rows = transcriptLines(state.messages, { expandedTools: false }).map(row => row.text)
console.log('=== 行序 ===')
rows.forEach((row, index) => { if (row.trim()) console.log(`  ${String(index).padStart(2)}| ${row.trim()}`) })
console.log()

function rowIndexOf(predicate: (row: string) => boolean): number {
  return rows.findIndex(predicate)
}

// ── 主断言：每步 thinking 在它那步正文之前、上一步正文之后 ───────────
check(`全部 ${STEPS} 条 thinking 都渲染出来了`, () => {
  for (let step = 1; step <= STEPS; step += 1) {
    const idx = rowIndexOf(row => row.includes(GLYPHS.star) && row.includes(`REASON-${step}`))
    assert.ok(idx !== -1, `REASON-${step} 没渲染出来: ${JSON.stringify(rows)}`)
  }
})

check('第 K 条 thinking 落在第 K 步正文之前', () => {
  // 最后一步之后没有再产生 thinking，它的正文留在 live 消息里直到 assistant_done；
  // 这里 turn 已经 done，live 消息的正文应当已在行里。
  for (let step = 1; step <= STEPS; step += 1) {
    const thinkIdx = rowIndexOf(row => row.includes(GLYPHS.star) && row.includes(`REASON-${step}`))
    const ownBodyIdx = rowIndexOf(row => row.includes(`BODY-${step}`))
    assert.ok(ownBodyIdx !== -1, `BODY-${step} 没渲染出来: ${JSON.stringify(rows)}`)
    assert.ok(thinkIdx < ownBodyIdx,
      `REASON-${step} 落在它那步正文之后: think=${thinkIdx} body=${ownBodyIdx}`)
  }
})

check('第 K 条 thinking 落在第 K-1 步正文之后（不是被顶到最上面）', () => {
  for (let step = 2; step <= STEPS; step += 1) {
    const thinkIdx = rowIndexOf(row => row.includes(GLYPHS.star) && row.includes(`REASON-${step}`))
    const prevBodyIdx = rowIndexOf(row => row.includes(`BODY-${step - 1}`))
    assert.ok(prevBodyIdx !== -1, `BODY-${step - 1} 没渲染出来`)
    assert.ok(thinkIdx > prevBodyIdx,
      `REASON-${step} 跑到上一步正文前面去了: think=${thinkIdx} prevBody=${prevBodyIdx}`)
  }
})

check('thinking 行严格单调递增（就是原始发生顺序）', () => {
  const positions = []
  for (let step = 1; step <= STEPS; step += 1) {
    positions.push(rowIndexOf(row => row.includes(GLYPHS.star) && row.includes(`REASON-${step}`)))
  }
  for (let i = 1; i < positions.length; i += 1) {
    assert.ok(positions[i]! > positions[i - 1]!,
      `顺序错乱: ${JSON.stringify(positions)}`)
  }
})

check('thinking 文本没有渗进正文', () => {
  const bodyText = rows.filter(row => !row.includes(GLYPHS.star)).join('\n')
  for (let step = 1; step <= STEPS; step += 1) {
    assert.ok(!bodyText.includes(`REASON-${step}`), `REASON-${step} 混进正文了`)
  }
})

// ── 对照：单步回合不能因为切开逻辑多出空消息 ─────────────────────────
let one = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
one = applyBridgeEvent(one, { type: 'user', taskId: 2, text: 'q' })
one = applyBridgeEvent(one, { type: 'assistant_thinking', taskId: 2, text: 'ONLY' })
one = applyBridgeEvent(one, { type: 'assistant_delta', taskId: 2, text: 'answer' })
one = applyBridgeEvent(one, { type: 'assistant_done', taskId: 2, text: 'answer' })
const oneRows = transcriptLines(one.messages, { expandedTools: false }).map(row => row.text)
console.log('=== 对照组：单步回合 ===')
oneRows.forEach((row, index) => { if (row.trim()) console.log(`  ${String(index).padStart(2)}| ${row.trim()}`) })
console.log()

check('对照：单步回合只有一条 thinking，且无空消息', () => {
  assert.equal(oneRows.filter(row => row.includes(GLYPHS.star)).length, 1, JSON.stringify(oneRows))
  // 空格行在对照组里是既有的块分隔（user 块尾 + thinking 与前文之间的边界），
  // 不是这次切开逻辑引入的。断言「没有凭空多行的空消息」用 id 更准：
  // 空 text 的 assistant 消息会渲染成一行占位空格。
  const signalling = one.messages.filter(
    message => message.role === 'assistant' && message.text.trim() === '' && !message.thinking?.length,
  )
  assert.equal(signalling.length, 0, `出现了空 assistant 消息: ${JSON.stringify(signalling.map(m => m.id))}`)
})

console.log(failures === 0 ? '\n✔ 全部通过' : `\n✖ ${failures} 项失败`)
process.exit(failures === 0 ? 0 : 1)
