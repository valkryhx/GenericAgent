/** 走完整链路：formatAssistantText -> renderMarkdownLines，看表格 token 到底是什么。 */
import { formatAssistantText } from '../src/messageFormat.ts'
import { renderMarkdownLines } from '../src/markdownRender.ts'
import { marked } from 'marked'

const raw = `| 名称 | 延迟 | 说明 |
| --- | --- | --- |
| 缓存 | 12ms | 命中率高 |
| 回源 | 180ms | 首次请求 |`

console.log('=== 1. 原始 marked.lexer ===')
for (const t of marked.lexer(raw, { gfm: true })) {
  console.log(`  type=${t.type}`)
}

console.log('\n=== 2. formatAssistantText 之后 ===')
const formatted = formatAssistantText(raw)
console.log(JSON.stringify(formatted))

console.log('\n=== 3. formatted 的 marked.lexer ===')
for (const t of marked.lexer(formatted, { gfm: true })) {
  console.log(`  type=${t.type}`)
}

console.log('\n=== 4. 最终 renderMarkdownLines ===')
for (const line of renderMarkdownLines(formatted)) {
  console.log(`  ${line.text}`)
}

console.log('\n=== 5. 直接 render 原始（不经 format） ===')
for (const line of renderMarkdownLines(raw)) {
  console.log(`  ${line.text}`)
}
