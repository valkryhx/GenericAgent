/** 验证：streaming 状态下表格是否被破坏（流式时表格还没写完）。 */
import { transcriptLinesWithChannel } from '../src/messageWindow.ts'
import type { ChatMessage } from '../src/protocol.ts'

const FULL = '| 名称 | 延迟 | 说明 |\n| --- | --- | --- |\n| 缓存 | 12ms | 命中率高 |\n| 回源 | 180ms | 首次请求 |'

function render(text: string, done: boolean, label: string) {
  const msg: ChatMessage = { id: 'a-1', role: 'assistant', text, done, taskId: 1 }
  const out = transcriptLinesWithChannel([msg], { expandedTools: false })
  const joined = out.lines.map(l => l.text).join('\n')
  console.log(`\n=== ${label} (done=${done}) ===`)
  for (const l of out.lines) console.log(`  ${l.text}`)
  console.log(`  表格边框: ${joined.includes('┌') ? '有 ✓' : '无 ✗'}`)
}

// 完整 + done
render(FULL, true, '完整表格')
// 完整但仍在流式
render(FULL, false, '完整表格但流式中')
// 流式中间态：只有表头行
render('| 名称 | 延迟 | 说明 |', false, '流式：只有表头')
// 流式中间态：表头+分隔
render('| 名称 | 延迟 | 说明 |\n| --- | --- | --- |', false, '流式：表头+分隔')
