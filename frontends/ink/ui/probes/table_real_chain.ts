/** 用真机抓到的原文走完整渲染链路，找出表格为什么没渲染。 */
import { formatAssistantText } from '../src/messageFormat.ts'
import { renderMarkdownLines } from '../src/markdownRender.ts'
import { marked } from 'marked'

const RAW = '\n\n**LLM Running (Step 1) ...**\n\n<summary>用户要求原样输出表格，直接呈现</summary>\n\n| 名称 | 延迟 | 说明 |\n| --- | --- | --- |\n| 缓存 | 12ms | 命中率高 |\n| 回源 | 180ms | 首次请求 |\n\n`````\n[Info] Final response to user.\n`````\n'

console.log('=== 1. formatAssistantText 输出 ===')
const formatted = formatAssistantText(RAW)
console.log(JSON.stringify(formatted))

console.log('\n=== 2. 逐行 ===')
formatted.split('\n').forEach((l, i) => console.log(`  ${String(i).padStart(2)} | ${JSON.stringify(l)}`))

console.log('\n=== 3. marked.lexer token ===')
for (const t of marked.lexer(formatted, { gfm: true })) {
  console.log(`  type=${t.type}  raw=${JSON.stringify((t as {raw?: string}).raw?.slice(0, 60))}`)
}

console.log('\n=== 4. renderMarkdownLines ===')
for (const line of renderMarkdownLines(formatted)) {
  console.log(`  ${line.text}`)
}
