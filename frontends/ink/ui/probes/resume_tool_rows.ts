/**
 * 探针：resume 历史里的工具调用是否渲染成 `● tool(args)` / `  ⎿  result`。
 *
 * 测什么
 * ------
 * 把 bridge 真实产出的 UI 消息（含 toolUses / toolResults）喂给
 * `transcriptLines()`，断言渲染出的行里出现：
 *   - 工具 chip：`● spawn_agent(...)`
 *   - 结果行：`  ⎿  ...`
 * 并与「只有 text」的旧行为对比，统计行数差。
 *
 * 根因坐标
 * --------
 * `genericagent/core/transcripts.py:_message_text_from_blocks` 只收 `type == "text"`，
 * 于是 `load_v2()` 产出的 assistant_text 不含工具；`bridge.py` 又只发 text。
 * 前端 `messageWindow.ts:appendStructuredToolRows` 是本轮新增的消费点。
 *
 * 为什么必须保留
 * --------------
 * 「工具调用在历史里消失」是一个**静默**缺陷：862 个单测全绿、resume 也能跑，
 * 只是内容少了。只有断言「渲染结果里出现工具名」才能发现它复发。
 *
 * 运行：cd frontends/ink/ui && npx tsx probes/resume_tool_rows.ts
 */
import { transcriptLines } from '../src/messageWindow.js'
import type { ChatMessage } from '../src/protocol.js'

const toolUses = [
  {
    id: 'call-a',
    tool: 'spawn_agent',
    input: JSON.stringify({ task_name: 'ga_multi_probe_a', fork_turns: 'none' }),
  },
  {
    id: 'call-b',
    tool: 'mcp__tavily__tavily_search',
    input: JSON.stringify({ query: 'claude code 319' }),
  },
]
const toolResults = [
  { toolUseId: 'call-a', text: '{"status": "started", "pid": 34136}' },
  { toolUseId: 'call-b', text: '{"results": [{"title": "..."}]}' },
]

function render(messages: ChatMessage[]): string[] {
  return transcriptLines(messages, { expandedTools: false }).map(line => line.text)
}

const base: ChatMessage = {
  id: 'a-1',
  role: 'assistant',
  text: '两个子代理已启动。',
  done: true,
  taskId: 1,
}

// 旧行为：只有 text
const withoutTools = render([base])
// 新行为：text + 结构化工具
const withTools = render([{ ...base, toolUses, toolResults }])

console.log('=== 只有 text（旧行为 = 用户看到的）===')
withoutTools.forEach((line, i) => console.log(`  ${i}| ${line}`))

console.log('\n=== text + 结构化工具（修复后）===')
withTools.forEach((line, i) => console.log(`  ${i}| ${line}`))

const joined = withTools.join('\n')
const checks: Array<[string, boolean]> = [
  ['chip: ● spawn_agent', /●\s*spawn_agent/.test(joined)],
  ['chip: ● mcp__tavily__tavily_search', /●\s*mcp__tavily__tavily_search/.test(joined)],
  ['result: ⎿ started', /⎿.*started/.test(joined)],
  ['result: ⎿ results', /⎿.*results/.test(joined)],
  ['旧行为没有工具行', !/●/.test(withoutTools.join('\n'))],
  ['行数确实变多', withTools.length > withoutTools.length],
]

console.log('\n=== 判据 ===')
let allOk = true
for (const [name, ok] of checks) {
  console.log(`  ${ok ? '✔' : '✖'} ${name}`)
  allOk &&= ok
}
console.log(`\n${allOk ? '  OK  工具调用已进入 resume 历史' : '  NG  工具调用仍然缺失'}`)
process.exitCode = allOk ? 0 : 1