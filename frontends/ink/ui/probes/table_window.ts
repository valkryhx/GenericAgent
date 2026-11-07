/**
 * 真机链路：messageWindow.appendAssistantBodyRows 走的是 body.split('\n') 逐行扫描。
 * 若中途遇到 [Info]/[Stdout]/工具标记，plain 会被 flush，表格就被拆散成多段。
 *
 * 这里用真机原文 + 真实 appendAssistantBodyRows 验证。
 */
import { formatAssistantText } from '../src/messageFormat.ts'
import { transcriptLinesWithChannel } from '../src/messageWindow.ts'
import type { ChatMessage } from '../src/protocol.ts'

const RAW = '\n\n**LLM Running (Step 1) ...**\n\n<summary>用户要求原样输出表格，直接呈现</summary>\n\n| 名称 | 延迟 | 说明 |\n| --- | --- | --- |\n| 缓存 | 12ms | 命中率高 |\n| 回源 | 180ms | 首次请求 |\n\n`````\n[Info] Final response to user.\n`````\n'

const msg: ChatMessage = {
  id: 'a-1',
  role: 'assistant',
  text: RAW,
  done: true,
  taskId: 1,
}

console.log('=== transcriptLinesWithChannel（真机同路径）===')
const out = transcriptLinesWithChannel([msg], { expandedTools: false })
for (const line of out.lines) {
  console.log(`  ${line.text}`)
}

console.log('\n=== 是否渲染出表格边框 ===')
const text = out.lines.map(l => l.text).join('\n')
console.log(`  ┌: ${text.includes('┌')}   │: ${text.includes('│')}   └: ${text.includes('└')}`)
