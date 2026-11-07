/**
 * 探针：固定 live 槽位取多少行才不会把小终端上的历史挤出可视区？
 *
 * 背景：改成固定高度后，`resizeReflow.test.ts` 里 24 行终端的
 * "HIST-PROBE-BRAVO 应可见一次" 变成了 0 次 —— 历史被 12 行固定槽挤出了屏幕。
 * 这是真实代价，不是测试噪音，所以要量出边界。
 */
import { computeLayoutMetrics } from '../src/layoutMetrics.js'

for (const rows of [24, 30, 40, 50]) {
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
  console.log(
    `终端 ${String(rows).padStart(2)} 行 → messageRows=${String(m.messageRows).padStart(2)}  bottomRows=${m.bottomRows}`,
  )
  for (const slot of [6, 7, 8, 9, 10, 12]) {
    const capped = Math.min(slot, m.messageRows)
    const share = Math.round((capped / m.messageRows) * 100)
    const visibleHistory = m.messageRows - capped
    console.log(
      `    槽=${String(slot).padStart(2)} → 实际 ${String(capped).padStart(2)} 行`
      + ` (占消息区 ${String(share).padStart(3)}%)`
      + `  可见历史 ${String(visibleHistory).padStart(2)} 行`,
    )
  }
  console.log('')
}
