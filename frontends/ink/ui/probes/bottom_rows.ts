/** 探针：turn 期间「消息区以外」的部分高度是否变化？ */
import { computeLayoutMetrics } from '../src/layoutMetrics.js'

console.log('layoutMetrics 里 bottomRows 的组成:')
console.log('  baseBottomRows = 3 + inputRows')
console.log('  activityRows   = 1 + goalRows')
console.log('  errorRows      = hasError ? 1 : 0')
console.log('  panelRows      = 面板/slash/MCP 状态')
console.log('')

const cases: Array<[string, Parameters<typeof computeLayoutMetrics>[0]]> = [
  ['启动（无活动）', {
    rows: 40, columns: 120, hasActivity: false, hasError: false,
    hasPanel: false, hasSlashSuggestions: false, inputRows: 1, headerRows: 0,
  }],
  ['运行中（有活动行）', {
    rows: 40, columns: 120, hasActivity: true, hasError: false,
    hasPanel: false, hasSlashSuggestions: false, inputRows: 1, headerRows: 0,
  }],
  ['运行中 + MCP 状态行', {
    rows: 40, columns: 120, hasActivity: true, hasError: false,
    hasPanel: true, hasSlashSuggestions: false, inputRows: 1, headerRows: 0,
  }],
  ['运行中 + 错误行', {
    rows: 40, columns: 120, hasActivity: true, hasError: true,
    hasPanel: false, hasSlashSuggestions: false, inputRows: 1, headerRows: 0,
  }],
  ['输入框换行成 2 行', {
    rows: 40, columns: 120, hasActivity: true, hasError: false,
    hasPanel: false, hasSlashSuggestions: false, inputRows: 2, headerRows: 0,
  }],
]

for (const [label, input] of cases) {
  const m = computeLayoutMetrics(input)
  console.log(
    `${label.padEnd(22)} bottomRows=${m.bottomRows}  messageRows=${m.messageRows}`,
  )
}
