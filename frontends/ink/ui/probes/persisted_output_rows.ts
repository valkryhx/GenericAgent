/**
 * 探针：`<persisted-output>` 信封在 resume 后占多少行。
 *
 * ## 测什么
 *
 * 喂一条真实形状的 resume 消息（工具调用 + 存盘信封），数渲染行数，并断言
 * 信封只贡献一行摘要、元数据与 JSON 预览都不漏进正文。
 *
 * ## 用户实测症状
 *
 * 「【结果太大保存到文件了】等多次重复，而且没看到正常内容了」。信封是九行：
 *
 *     <persisted-output>
 *     Output too large (74859 chars). Full output saved to: D:\...\call-1.json
 *     artifactPath: tool-results/session/call-1.json
 *     originalChars: 74859
 *     truncated: false
 *     sha256: ee1c8763...
 *     Preview:
 *     {"status":"success","_truncated":true,...}
 *     </persisted-output>
 *
 * 逐行铺开时两个信封吃掉 18 行。终端 24-40 行，于是真正文被挤出屏幕，用户看到的
 * 就是「同一句提示反复出现」。
 *
 * ## 根因坐标（已修）
 *
 * `frontends/ink/ui/src/messageWindow.ts:appendStructuredResultRows` —— 原本把整块
 * 结果按行铺开。现在先过 `parsePersistedEnvelope()`，命中就折叠成一行。
 * 信封格式的事实来源是 `genericagent/core/tool_results/budget.py:_envelope()`；
 * 那边改字段，这里的解析和测试要一起改。
 *
 * ## 为什么是确定性的
 *
 * 纯渲染函数 + 手工构造的信封，没有网络、没有计时、没有终端。
 *
 * ## 为什么必须留着
 *
 * 折叠是「丢了信息也不该丢」的取舍：真正的大结果在磁盘文件里，屏幕上留的只是
 * 指路信息。这条因果链一旦被改回逐行铺开，症状是「看不到内容」而不是崩溃，
 * 没有断言就发现不了。
 *
 * 运行：cd frontends/ink/ui && npx tsx probes/persisted_output_rows.ts
 */
import assert from 'node:assert/strict'
import { transcriptLines } from '../src/messageWindow.js'
import { parsePersistedEnvelope } from '../src/messageWindow.js'
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

const ENVELOPE = [
  '<persisted-output>',
  'Output too large (74859 chars). Full output saved to: D:\\repo\\tool-results\\call-1.json',
  'artifactPath: tool-results/session/call-1.json',
  'originalChars: 74859',
  'truncated: false',
  'sha256: ee1c8763216d894f12f78dc252134e523af9671585d4261241af701745adc036',
  'Preview:',
  '{"status":"success","_truncated":true,"_note":"truncated by the host"}',
  '</persisted-output>',
].join('\n')

function render(resultText: string): string[] {
  const message: ChatMessage = {
    id: 'a-1',
    role: 'assistant',
    text: '',
    done: true,
    taskId: 1,
    blocks: [
      { kind: 'tool_use', id: 'c1', tool: 'tavily_search', input: '{"query":"x"}' },
      { kind: 'tool_result', toolUseId: 'c1', text: resultText },
    ],
  }
  return transcriptLines([message], { expandedTools: false }).map(row => row.text)
}

console.log('=== 信封渲染 ===')
const rows = render(ENVELOPE)
rows.forEach((row, index) => { if (row.trim()) console.log(`  ${index}| ${row}`) })
console.log()

check('信封只占一行摘要（原本九行）', () => {
  const nonBlank = rows.filter(row => row.trim() !== '')
  assert.equal(nonBlank.length, 2, `应为 chip + 一行摘要，实际 ${nonBlank.length} 行: ${JSON.stringify(rows)}`)
})

check('摘要说出三件可行动的事：存盘、大小、路径', () => {
  const summary = rows[rows.length - 1]!
  assert.ok(summary.includes('saved to file'), `没表达「存到文件」: ${summary}`)
  assert.ok(summary.includes('74859'), `没有字符数: ${summary}`)
  assert.ok(summary.includes('tool-results/session/call-1.json'), `没有可用路径: ${summary}`)
})

check('元数据不占独立行', () => {
  const joined = rows.join('\n')
  for (const noise of ['<persisted-output>', '</persisted-output>', 'sha256', 'originalChars', 'artifactPath:']) {
    assert.ok(!joined.includes(noise), `元数据漏出来了: ${noise} ← ${joined}`)
  }
})

check('JSON 预览不显示', () => {
  assert.ok(!rows.join('\n').includes('_truncated'), JSON.stringify(rows))
})

check('truncated=true 时说明只存了前面部分', () => {
  const truncated = render(ENVELOPE.replace('truncated: false', 'truncated: true'))
  const summary = truncated[truncated.length - 1]!
  assert.ok(summary.includes('first portion only'), summary)
})

check('信封解析只认完整包裹（避免误吞普通输出）', () => {
  assert.equal(parsePersistedEnvelope('普通结果'), null)
  assert.equal(parsePersistedEnvelope('<persisted-output>没有闭合'), null)
  assert.equal(parsePersistedEnvelope('前缀 <persisted-output>\nx\n</persisted-output>'), null)
  assert.ok(parsePersistedEnvelope(ENVELOPE), '完整信封应被识别')
})

// ── 对照组：普通多行结果行为不变 ──────────────────────────────────────
const plainRows = render('line one\nline two')
console.log('=== 对照组：普通结果 ===')
plainRows.forEach((row, index) => { if (row.trim()) console.log(`  ${index}| ${row}`) })
console.log()

check('对照：普通结果仍按行展开', () => {
  assert.ok(plainRows.some(row => row.includes('line one')), JSON.stringify(plainRows))
  assert.ok(plainRows.some(row => row.includes('line two')), JSON.stringify(plainRows))
})

console.log(failures === 0 ? '\n✔ 全部通过' : `\n✖ ${failures} 项失败`)
process.exit(failures === 0 ? 0 : 1)
