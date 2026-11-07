/** 直接打印 loading 阶段摘要行的实际内容与长度。 */
import { mcpStartupStatusRows } from '../src/mcpPanel.ts'

const rows = mcpStartupStatusRows({
  loading: true,
  servers: [
    { name: 'exa', status: 'connected', tool_count: 3 },
    { name: 'context7', status: 'pending', tool_count: 0 },
  ],
  tools: [],
})

console.log('行数:', rows.length)
rows.forEach((r, i) => {
  console.log(`  [${i}] len=${r.length}`)
  console.log(`      ${JSON.stringify(r)}`)
})
