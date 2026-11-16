import { buildFramePlan } from '../src/framePlan.js'
import { normalizeFrameSelection, selectedFrameText } from '../src/frameSelection.js'
import { screenRowToContentRow, type SelectionSpace } from '../src/selectionSpace.js'

// 照抄 App.tsx 修复后的换算
const CANVAS = 80
const HEADER = 1
const VIEWPORT = 20
const TOTAL = 25
const OFFSET = 0

const space: SelectionSpace = {
  headerRows: HEADER,
  transcriptTotalRows: TOTAL,
  transcriptViewportRows: VIEWPORT,
  transcriptScrollOffset: OFFSET,
  bandRows: 1,
}

const transcriptLines = Array.from({ length: TOTAL }, (_, i) => (
  i === TOTAL - 1 ? 'LAST-LINE' : `line-${i}`
))

const shift = Math.max(0, TOTAL - VIEWPORT)
const contentFrame = buildFramePlan({
  canvasColumns: CANVAS,
  rows: (HEADER + VIEWPORT + 4) + shift,
  headerRows: HEADER,
  header: 'GenericAgent idle',
  transcriptRows: Math.max(TOTAL, 1),
  transcriptLines,
  composerRow: (HEADER + VIEWPORT + 2) + shift,
  composerLines: [{ gutter: '> ', text: '' }],
  bandLines: [''],
  belowLines: ['Enter send'],
})

/** App.tsx: contentPointFromMouse（修复后） */
function contentPointOf(screenRow: number, col: number) {
  if (screenRow < HEADER) return { row: screenRow, col }
  const contentRow = screenRowToContentRow(space, screenRow)
  if (contentRow === null) return { row: screenRow, col }
  return { row: contentRow + HEADER, col }
}

let failures = 0
function check(name: string, ok: boolean, detail: string) {
  if (!ok) failures += 1
  console.log(`${ok ? '✔' : '✖'} ${name}${detail ? ' — ' + detail : ''}`)
}

console.log(`HEADER=${HEADER} VIEWPORT=${VIEWPORT} TOTAL=${TOTAL}`)
console.log(`屏幕最后一行 = ${HEADER + VIEWPORT - 1}\n`)

// ── 1. 行号映射正确 ────────────────────────────────────────────────
console.log('=== 行号映射 ===')
for (const [screenRow, expect] of [[1, 'line-5'], [20, 'LAST-LINE']] as const) {
  const p = contentPointOf(screenRow, 40)
  const actual = contentFrame[p.row]?.text
  console.log(`  屏幕行 ${screenRow} -> frame[${p.row}] = ${JSON.stringify(actual)}（期望 ${JSON.stringify(expect)}）`)
  check(`屏幕行 ${screenRow} 映射正确`, actual === expect, `实际 ${JSON.stringify(actual)}`)
}
console.log()

// ── 2. 从上往下拖，能否选到末行 ────────────────────────────────────
console.log('=== 从上往下拖（anchor 在顶）===')
const anchorTop = contentPointOf(HEADER, 0)
const lastRow = HEADER + VIEWPORT - 1
for (const target of [lastRow - 1, lastRow]) {
  const range = normalizeFrameSelection(anchorTop, contentPointOf(target, 40), contentFrame)
  const text = selectedFrameText(range, contentFrame)
  const hits = text.includes('LAST-LINE')
  console.log(`  拖到屏幕行 ${target} => 含末行=${hits}, 行数=${text.split('\n').length}`)
  if (target === lastRow) {
    check('拖到屏幕最后一行就能选到末行', hits, hits ? '' : '仍需往下拖一格')
  }
}
console.log()

// ── 3. 从下往上拖 ──────────────────────────────────────────────────
console.log('=== 从下往上拖（anchor 在末行行尾）===')
// 「往上选一整屏」的实际操作：从末行末尾按下，向上拖到顶部。
const anchorBottom = contentPointOf(lastRow, 78)
console.log(`  anchor frame[${anchorBottom.row}] 文本=${JSON.stringify(contentFrame[anchorBottom.row]?.text)} col=${anchorBottom.col}`)
const upFocus = contentPointOf(HEADER, 0)
const upRange = normalizeFrameSelection(anchorBottom, upFocus, contentFrame)
console.log(`  anchor=${JSON.stringify(anchorBottom)} focus=${JSON.stringify(upFocus)} range=${JSON.stringify(upRange)}`)
const upText = selectedFrameText(upRange, contentFrame)
console.log(`  拖到顶部 => 行数=${upText.split('\n').length}`)
console.log(`  前 3 行=${JSON.stringify(upText.split('\n').slice(0, 3))}`)
console.log(`  后 3 行=${JSON.stringify(upText.split('\n').slice(-3))}`)
check('从下往上拖涵盖整个视口的内容', upText.includes('LAST-LINE') && upText.includes('line-5'),
  `行数=${upText.split('\n').length}`)
console.log()

console.log(failures === 0 ? '✔ 全部通过' : `✖ ${failures} 项失败`)
process.exit(failures === 0 ? 0 : 1)
