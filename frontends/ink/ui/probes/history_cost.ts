/**
 * 探针：固定 live 槽位在 24 行终端上，到底把多少历史挤出可视区？
 *
 * resizeReflow.test.ts 在 24 行终端里断言 HIST-PROBE-ALPHA 可见。
 * 改成固定高度后它被挤出屏幕 —— 需要量化这是"1 行之差"还是"大面积丢失"。
 */
import { computeLayoutMetrics } from '../src/layoutMetrics.js'
import { inlineLiveViewportRows } from '../src/messageViewportPlan.js'

const rows = 24
const m = computeLayoutMetrics({
  rows,
  columns: 80,
  hasActivity: true,
  hasError: false,
  hasPanel: false,
  hasSlashSuggestions: false,
  inputRows: 1,
  headerRows: 0,
})

const slot = inlineLiveViewportRows({ messageRows: m.messageRows })
console.log(`终端 ${rows} 行`)
console.log(`  messageRows   = ${m.messageRows}`)
console.log(`  bottomRows    = ${m.bottomRows}`)
console.log(`  固定槽位      = ${slot} 行`)
console.log(`  剩余给历史    = ${m.messageRows - slot} 行`)
console.log('')
console.log('对照：改动前是 content-desired（idle 时 0 行，流式时 1~9 行）')
console.log('      → idle 时历史可用 18 行，改成固定后只有 ' + (m.messageRows - slot) + ' 行')
