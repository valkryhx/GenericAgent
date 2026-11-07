// 量化：GA 的动态帧高度在不同流式状态下是否恒定。
// 这是"输入框上下抖"的根因判据 —— 只要 dynamicFrameHeight 变，屏幕就会重排。
import { computeLayoutMetrics } from '../src/layoutMetrics.js'
import { planMessageViewport } from '../src/messageViewportPlan.js'

const rows = 40
const columns = 120
const inputRows = 1

const bottom = computeLayoutMetrics({
  rows,
  columns,
  hasActivity: false,
  hasError: false,
  hasPanel: false,
  hasSlashSuggestions: false,
  inputRows,
  headerRows: 0,
})

console.log('terminal rows        =', rows)
console.log('bottomRows           =', bottom.bottomRows)
console.log('messageRows          =', bottom.messageRows)
console.log('headerRows           =', bottom.headerRows)
console.log('')

const cases: Array<[string, number, boolean, boolean]> = [
  ['ready (no history)', 0, false, false],
  ['idle + static', 0, true, false],
  ['streaming 1 line', 1, true, true],
  ['streaming 4 lines', 4, true, true],
  ['streaming 12 lines', 12, true, true],
  ['streaming 40 lines', 40, true, true],
  ['idle + static (again)', 0, true, false],
]

const heights = new Set<number>()
for (const [label, live, hasStatic, keepPlaceholder] of cases) {
  const plan = planMessageViewport({
    hasStaticMessages: hasStatic,
    liveLineCount: live,
    messageRows: bottom.messageRows,
    keepLivePlaceholder: keepPlaceholder,
  })
  const vp = plan.kind === 'none' ? 0 : plan.height
  const dynamic = bottom.headerRows + vp + bottom.bottomRows
  heights.add(dynamic)
  console.log(
    label.padEnd(22),
    'plan=' + plan.kind.padEnd(6),
    'viewport=' + String(vp).padStart(2),
    'dynamicFrameHeight=' + dynamic,
    dynamic === rows ? '(FULL)' : '(short by ' + (rows - dynamic) + ')',
  )
}

console.log('')
console.log('出现过的帧高度数量 =', heights.size)
console.log('结论:', heights.size === 1 ? '恒定（不抖）' : '会变（就是抖动根源）')
