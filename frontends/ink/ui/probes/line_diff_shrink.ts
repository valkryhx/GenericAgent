/**
 * 直接验证行级 diff 在「变矮」边界上是否需要退化 —— 不经过 App，纯合成帧。
 *
 * App 的动态区高度受 layoutMetrics 约束，在 80x12 下不会真的变矮（实测恒为 11 行），
 * 所以上一个探针的「变矮」场景其实是空跑。这里用合成帧把边界测实：
 *
 *   case 1: 变高（N -> N+2）
 *   case 2: 变矮（N+2 -> N），必须退化或被正确处理
 *   case 3: 中间行删除（首尾不变、中间少一行）
 *   case 4: 变矮到只剩 1 行
 *
 * 判据：diff 序列回放出的网格 + 光标 == 全量重写序列回放出的网格 + 光标。
 *
 * 运行： cd frontends/ink/ui && npx tsx probes/line_diff_shrink.ts
 */
import { applyToScreen, createScreen } from '../src/virtualScreen.js'

const NL = '\n'
const COLUMNS = 40

function eraseLines(count: number): string {
  let s = ''
  for (let i = 0; i < count; i++) s += '\u001b[2K' + (i < count - 1 ? '\u001b[1A' : '')
  if (count) s += '\u001b[G'
  return s
}

/**
 * 行级 diff（严格版）：只处理「本帧不矮于上一帧」的情况；变矮返回 null → 调用方退化。
 *
 * 变矮为什么难：ink 擦除序列不同（`clear(N)` + `cursorMove(0,-1)`），而且删掉的行
 * 位于帧内容之下的屏幕区域，靠相对移动「向上重画」无法可靠清除。宁可退化。
 */
function buildLineDiff(prev: string[], next: string[]): string | null {
  if (next.length < prev.length) return null   // 变矮：退化
  let out = ''
  let cursor = prev.length
  for (let row = 0; row < next.length; row += 1) {
    const before = row < prev.length ? prev[row]! : ''
    if (before === next[row]) continue
    const dy = row - cursor
    if (dy !== 0) out += dy < 0 ? `\u001b[${-dy}A` : `\u001b[${dy}B`
    out += '\r\u001b[2K\u001b[0m' + next[row]!
    cursor = row
  }
  const back = next.length - cursor
  if (back !== 0) out += back < 0 ? `\u001b[${-back}A` : `\u001b[${back}B`
  out += '\r'
  return out
}

type Case = { name: string; prev: string[]; next: string[] }

const cases: Case[] = [
  { name: '变高 N -> N+2', prev: ['a', 'b', 'c'], next: ['a', 'b', 'c', 'd', 'e'] },
  { name: '变矮 N+2 -> N', prev: ['a', 'b', 'c', 'd', 'e'], next: ['a', 'b', 'c'] },
  { name: '变矮到 1 行', prev: ['a', 'b', 'c', 'd'], next: ['a'] },
  { name: '等高，中间行变化', prev: ['a', 'b', 'c', 'd'], next: ['a', 'B', 'c', 'd'] },
  { name: '等高，末行变化', prev: ['a', 'b', 'c'], next: ['a', 'b', 'C'] },
  { name: '等高，首行变化', prev: ['a', 'b', 'c'], next: ['A', 'b', 'c'] },
  { name: '从空到 3 行', prev: [], next: ['a', 'b', 'c'] },
  { name: '完全不变', prev: ['a', 'b', 'c'], next: ['a', 'b', 'c'] },
  { name: '带 ANSI 样式的行', prev: ['\u001b[31mred\u001b[0m', 'plain'], next: ['\u001b[31mRED\u001b[0m', 'plain'] },
]

let failures = 0
console.log(`\n=== 行级 diff 合成帧边界（${COLUMNS} 列）===`)
for (const c of cases) {
  const sFull = createScreen(60, COLUMNS)
  const sDiff = createScreen(60, COLUMNS)

  // 两屏先处于同一初始状态（上一帧）。
  const setup = c.prev.join(NL) + NL
  applyToScreen(sFull, setup, COLUMNS)
  applyToScreen(sDiff, setup, COLUMNS)

  const fullSeq = eraseLines(c.prev.length + 1) + c.next.join(NL) + NL
  const diffBody = buildLineDiff(c.prev, c.next)
  const degraded = diffBody === null
  const diffSeq = degraded ? fullSeq : diffBody

  applyToScreen(sFull, fullSeq, COLUMNS)
  applyToScreen(sDiff, diffSeq, COLUMNS)

  let ok = sFull.row === sDiff.row && sFull.col === sDiff.col
  let detail = ''
  if (ok) {
    for (let r = 0; r < Math.max(sFull.rows.length, sDiff.rows.length); r += 1) {
      const a = (sFull.rows[r] ?? '').trimEnd()
      const b = (sDiff.rows[r] ?? '').trimEnd()
      if (a !== b) { ok = false; detail = `row ${r}: ${JSON.stringify(a)} vs ${JSON.stringify(b)}`; break }
    }
  } else {
    detail = `cursor (${sFull.row},${sFull.col}) vs (${sDiff.row},${sDiff.col})`
  }
  if (!ok) failures += 1
  const tag = degraded ? '退化全量' : '行级 diff'
  console.log(`  ${ok ? '✅' : '❌'} ${c.name.padEnd(22)} [${tag}]  ${detail}`)
}

console.log(`\n  失败 = ${failures} / ${cases.length}   ${failures === 0 ? '✅ 全部正确（含退化路径）' : '❌'}`)
process.exit(failures === 0 ? 0 : 1)
