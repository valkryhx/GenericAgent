/** 验证：MCP 状态行出现/消失时 bottomRows 跳变多少。 */
import { computeLayoutMetrics } from '../src/layoutMetrics.js'

const base = {
  rows: 40,
  columns: 120,
  hasActivity: true,
  hasError: false,
  hasSlashSuggestions: false,
  inputRows: 1,
  headerRows: 0,
}

console.log('40 行终端，MCP 状态行出现前后的底栏高度：')
for (const [label, hasPanel, panelRows] of [
  ['MCP 连上后（正常）', false, undefined],
  ['MCP 连接中（状态行）', true, 1],
  ['MCP 连接中（默认 8 行）', true, undefined],
] as const) {
  const m = computeLayoutMetrics({ ...base, hasPanel, panelRows })
  console.log(`  ${label.padEnd(24)} bottomRows=${String(m.bottomRows).padStart(2)}  messageRows=${m.messageRows}`)
}
