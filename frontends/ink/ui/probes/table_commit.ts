/**
 * 验证：stream commit 把表格切成多个 message 后，表格是否还能渲染。
 *
 * 真机链路：assistant_delta 累积 -> commitStreamingAssistantMessages 把溢出行
 * 提交成 a-N-cK 段 -> 每段独立 renderMarkdownLines。
 * 若表格被切在段边界，每段都不是完整表格 -> 退化回原始竖线。
 */
import { commitStreamingAssistantMessages } from '../src/streamCommit.ts'
import { transcriptLinesWithChannel } from '../src/messageWindow.ts'
import type { ChatMessage } from '../src/protocol.ts'

const RAW = 'LLM Running (Step 1) ...\n\nSummary: test\n\n| 名称 | 延迟 | 说明 |\n| --- | --- | --- |\n| 缓存 | 12ms | 命中率高 |\n| 回源 | 180ms | 首次请求 |'

function simulate(tailLines: number) {
  let messages: ChatMessage[] = [
    { id: 'u-1', role: 'user', text: 'Q', done: true, taskId: 1 },
    { id: 'a-1', role: 'assistant', text: '', done: false, taskId: 1 },
  ]
  // 逐行喂入（模拟流式）
  const lines = RAW.split('\n')
  for (const line of lines) {
    const idx = messages.findIndex(m => m.id === 'a-1')
    if (idx === -1) break
    messages = messages.slice()
    messages[idx] = { ...messages[idx]!, text: messages[idx]!.text + line + '\n' }
    messages = commitStreamingAssistantMessages(messages, 1, tailLines)
  }
  // 收尾
  const idx = messages.findIndex(m => m.id === 'a-1')
  if (idx >= 0) {
    messages = messages.slice()
    messages[idx] = { ...messages[idx]!, done: true }
  }

  console.log(`\n===== tailLines=${tailLines} → ${messages.length} 个 message =====`)
  for (const m of messages) {
    const firstLine = (m.text.split('\n')[0] ?? '').slice(0, 40)
    console.log(`  ${m.id} (${m.text.split('\n').length} 行): ${JSON.stringify(firstLine)}`)
  }

  const out = transcriptLinesWithChannel(messages, { expandedTools: false })
  const joined = out.lines.map(l => l.text).join('\n')
  console.log(`  渲染结果：表格边框 ${joined.includes('┌') ? '有 ✓' : '无 ✗'}`)
  for (const l of out.lines) {
    if (l.text.trim()) console.log(`    ${l.text.slice(0, 70)}`)
  }
}

simulate(8)
simulate(3)
simulate(100)
