/**
 * 前端探针：把「选区跨视口」和「跳动图标」渲染出来，核对真实观感。
 *
 * 单测只保证状态机与坐标换算，看不到屏幕。这个探针抓两件事：
 * 1. 活动行的图标在连续帧里是否真的变了（用户要的「明显知道在跑」）
 * 2. 选区拖出视口时，高亮是否贴在视口边缘（而不是画到 composer 上）
 *
 * 用法：npx tsx probes/selection_and_spinner.ts
 */
import { spinnerFrameAt, runningStatusSegments } from '../src/activityStatus.js'
import { buildFramePlan } from '../src/framePlan.js'
import { frameSelectionSpans, normalizeFrameSelection } from '../src/frameSelection.js'
import {
  autoScrollDirection,
  focusContentRowAtEdge,
  projectRangeToScreen,
  type SelectionSpace,
} from '../src/selectionSpace.js'

const NL = String.fromCharCode(10)
const out: string[] = []
const say = (s: string) => out.push(s)

// ---- 1. 跳动图标 ----

say('===== spinner frames (running status line) =====')
for (let frame = 0; frame < 6; frame += 1) {
  const segs = runningStatusSegments(168, 'Nucleating', { inputTokens: 12300, outputTokens: 400, totalTokens: 12700 } as never, frame)
  say(`frame ${frame}: ${segs.map(s => s.text).join('')}`)
}
say('')
say('首字符序列: ' + Array.from({ length: 6 }, (_, i) => spinnerFrameAt(i)).join(' → '))
say('')

// ---- 2. 选区跨视口 ----

say('===== selection across the viewport =====')
const space: SelectionSpace = {
  headerRows: 1,
  transcriptTotalRows: 200,
  transcriptViewportRows: 10,
  transcriptScrollOffset: 0,
  bandRows: 1,
}

const transcript = Array.from({ length: 200 }, (_, i) => `line ${i}`)
const makeFrame = (offset: number) => {
  const end = 200 - offset
  const start = Math.max(0, end - 10)
  return buildFramePlan({
    canvasColumns: 40,
    rows: 14,
    headerRows: 1,
    header: 'GenericAgent running',
    transcriptRows: 10,
    transcriptLines: transcript.slice(start, end),
    composerRow: 12,
    composerLines: [{ gutter: '> ', text: '' }],
    bandLines: [runningStatusSegments(168, 'Nucleating', null, 0).map(s => s.text).join('')],
  })
}

// 从视口内往外拖
const anchor = { row: 190, col: 0 } // 内容行 190
let focus = { row: 195, col: 5 }
say(`drag inside: anchor=content ${anchor.row}, focus=content ${focus.row}`)
say('  autoScroll: ' + String(autoScrollDirection(space, 5)))

// 拖到视口上方（想选更早的内容）
say(`drag above viewport (screen row 0): autoScroll=${autoScrollDirection(space, 0)}`)
const scrolledUp: SelectionSpace = { ...space, transcriptScrollOffset: 20 }
say(`  after scrolling up 20: window start = ${200 - 20 - 10}, focus row = ${focusContentRowAtEdge(scrolledUp, 'up')}`)

// 投影：滚出去的部分贴边
const projected = projectRangeToScreen(space, {
  start: { row: 50, col: 0 },   // 远在窗口之上
  end: { row: 195, col: 5 },    // 窗口内
})
say(`project range [50..195] -> screen [${projected.start.row}..${projected.end.row}] (viewport is 1..10)`)

const frame = makeFrame(0)
const range = normalizeFrameSelection(projected.start, projected.end, frame)
const spans = frameSelectionSpans(range, frame)
say('')
say('rendered frame with highlight (█ = selected):')
spans.forEach((span, index) => {
  if (index >= frame.length) return
  const text = frame[index]!.text
  let marked: string
  if (!span) {
    marked = text
  } else {
    const from = Math.min(span.from, text.length)
    const to = Math.min(span.to, text.length)
    marked = text.slice(0, from) + '█'.repeat(Math.max(0, to - from)) + text.slice(to)
  }
  say(`  ${String(index).padStart(2)} | ${marked}`)
})

const result = out.join(NL)
process.stdout.write(result + NL)

const fs = await import('node:fs')
const path = await import('node:path')
const dir = path.resolve(import.meta.dirname, '../../../../tests/probes/captures')
fs.mkdirSync(dir, { recursive: true })
fs.writeFileSync(path.join(dir, 'selection_and_spinner.txt'), result, 'utf8')
process.stderr.write(`wrote ${path.join(dir, 'selection_and_spinner.txt')}${NL}`)
