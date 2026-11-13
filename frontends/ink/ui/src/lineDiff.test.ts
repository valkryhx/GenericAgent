import test from 'node:test'
import assert from 'node:assert/strict'
import { buildLineDiff, looksLikeFrame, parseFrameRows } from './lineDiff.js'
import { applyToScreen, createScreen, type Screen } from './virtualScreen.js'

const ERASE = '\u001b[2K'
const UP = (n: number) => `\u001b[${n}A`

/** ink 的 eraseLines(N)：N 次 2K，之间用 1A 上移，末尾 \x1b[G。 */
function eraseLines(count: number): string {
  let s = ''
  for (let i = 0; i < count; i++) s += ERASE + (i < count - 1 ? '\u001b[1A' : '')
  if (count) s += '\u001b[G'
  return s
}

/** 一帧的完整字节：eraseLines(prevN) + body + '\n'（ink 的 output 形态）。 */
function inkFrame(body: string, prevLineCount: number): string {
  return eraseLines(prevLineCount) + body + '\n'
}

type Playback = { screen: Screen; cursor: { row: number; col: number } }

/** 从同一初始状态回放一段序列，返回屏幕与光标。 */
function replay(seq: string, startCursor = { row: 0, col: 0 }, setup = ''): Playback {
  const screen = createScreen(80, 60)
  if (setup) applyToScreen(screen, setup, 60)
  screen.row = startCursor.row
  screen.col = startCursor.col
  applyToScreen(screen, seq, 60)
  return { screen, cursor: { row: screen.row, col: screen.col } }
}

function screensEqual(a: Playback, b: Playback): string | null {
  if (a.cursor.row !== b.cursor.row || a.cursor.col !== b.cursor.col) {
    return `cursor (${a.cursor.row},${a.cursor.col}) vs (${b.cursor.row},${b.cursor.col})`
  }
  const n = Math.max(a.screen.rows.length, b.screen.rows.length)
  for (let r = 0; r < n; r += 1) {
    const x = (a.screen.rows[r] ?? '').trimEnd()
    const y = (b.screen.rows[r] ?? '').trimEnd()
    if (x !== y) return `row ${r}: ${JSON.stringify(x)} vs ${JSON.stringify(y)}`
  }
  return null
}

// ── parseFrameRows ─────────────────────────────────────────────────────

test('parseFrameRows extracts the frame body from an ink frame', () => {
  const rows = parseFrameRows(inkFrame('a\nb\nc', 0))
  assert.deepEqual(rows, ['a', 'b', 'c'])
})

test('parseFrameRows handles the trailing newline ink always appends', () => {
  // ink 的 output = body + '\n'，所以 split 后末元素恒为空串，必须 pop 掉。
  assert.deepEqual(parseFrameRows(inkFrame('only', 0)), ['only'])
  // 空 body（整帧无内容）→ 一个空行，不是零行。ink 对空帧写的就是 "\n"。
  assert.deepEqual(parseFrameRows(inkFrame('', 0)), [''])
  assert.deepEqual(parseFrameRows(''), null)
})

test('parseFrameRows keeps blank interior lines', () => {
  assert.deepEqual(parseFrameRows(inkFrame('a\n\nb', 0)), ['a', '', 'b'])
})

test('parseFrameRows rejects clearTerminal frames', () => {
  // 内容 ≥ 视口高度时 ink 走 clearTerminal 路径，几何完全重建 → 不能 diff。
  assert.equal(parseFrameRows('\u001b[2J\u001b[Ha\nb'), null)
  assert.equal(parseFrameRows('\u001b[3J\u001b[Ha'), null)
})

test('parseFrameRows rejects bursts containing more than one erase', () => {
  // log.clear() + <Static> + 主帧：一次突发里两段擦除，帧锚点已被静态区推进。
  const staticThenMain = `${eraseLines(2)}static-line\n${eraseLines(3)}main\nbody`
  assert.equal(parseFrameRows(staticThenMain), null)
})

test('parseFrameRows rejects frames containing a clearTerminal or extra erases', () => {
  // 「是否像一帧」由 looksLikeFrame 判断；parseFrameRows 只负责拆解一帧的内部结构。
  // 它的拒绝条件是「结构不可 diff」：
  assert.equal(parseFrameRows('\u001b[2J\u001b[Ha'), null)          // clearTerminal
  assert.equal(parseFrameRows(eraseLines(2) + 'x\n' + eraseLines(1) + 'y\n'), null)  // 多段擦除
  assert.equal(parseFrameRows(''), null)                          // 空输入
})

test('looksLikeFrame gates first frames vs debug output', () => {
  // 首帧：eraseLines(0) 空前缀，但 ink 的 output 恒以 \n 结尾。
  assert.equal(looksLikeFrame(inkFrame('a\nb', 0)), true)
  // 常规帧：带擦除前缀。
  assert.equal(looksLikeFrame(inkFrame('a', 3)), true)
  // debug 输出：既无擦除、也无帧尾换行 → 不是帧，调用方应原样透传。
  assert.equal(looksLikeFrame('debug text without terminator'), false)
  assert.equal(looksLikeFrame(''), false)
})

// ── buildLineDiff：语义 ────────────────────────────────────────────────

test('buildLineDiff rewrites only the changed rows', () => {
  const diff = buildLineDiff(['a', 'b', 'c'], ['a', 'B', 'c'])
  assert.ok(diff !== null)
  // 只出现一次擦除：只有中间那行变了。
  assert.equal(diff.split(ERASE).length - 1, 1)
  assert.ok(diff.includes('B'))
  // 未变化的行不该被写出。
  assert.ok(!diff.includes('a\r') && !diff.includes('c'))
})

test('buildLineDiff emits nothing when the frame is identical', () => {
  const diff = buildLineDiff(['a', 'b', 'c'], ['a', 'b', 'c'])
  assert.equal(diff, '\r')
})

test('buildLineDiff always resets style before erasing', () => {
  // `ESC[2K` 会用当前背景色填充，带背景样式的行必须先归零再擦，
  // 否则擦完会留下一整行彩色背景。
  const diff = buildLineDiff(['a'], ['b'])!
  const eraseAt = diff.indexOf(ERASE)
  const resetAt = diff.indexOf('\u001b[0m')
  assert.ok(resetAt >= 0, 'must reset before erasing')
  assert.ok(resetAt < eraseAt, 'reset must precede the erase')
})

test('buildLineDiff moves up with CSI A and never rolls the screen', () => {
  // 行数相同时，向上移动是安全的（CSI A 不会滚动）。
  const diff = buildLineDiff(['a', 'b', 'c'], ['a', 'c', 'd'])!
  assert.ok(diff.includes('\u001b[1A') || diff.includes('\u001b[2A'), 'upward move must use CSI A')
  // 关键：绝不能靠 `\n` 向下建行 —— 到视口底部会滚动屏幕，而全量重写不滚动。
  assert.ok(!diff.includes('\r\n'), 'must not use newline to build rows')
})

test('buildLineDiff returns null when the row count changes (grow or shrink)', () => {
  // 变矮：删掉的行在帧内容之下，相对移动无法可靠清除。
  assert.equal(buildLineDiff(['a', 'b', 'c'], ['a', 'b']), null)
  assert.equal(buildLineDiff(['a', 'b', 'c', 'd'], ['a']), null)
  assert.equal(buildLineDiff(['a'], []), null)
  // 变高：唯一的下建行手段（\n）会滚动屏幕，CSI B 会钳位 —— 两者都不等价于全量重写。
  // 这一条是被 probes/line_diff_acceptance.ts 的 28/34 帧不一致打出来的。
  assert.equal(buildLineDiff(['a', 'b'], ['a', 'b', 'c']), null)
  assert.equal(buildLineDiff([], ['a', 'b']), null)
})

// ── buildLineDiff：回放正确性（虚拟终端）────────────────────────────────

type Case = { name: string; prev: string[]; next: string[] }

const CASES: Case[] = [
  { name: '等高·中间行变化', prev: ['a', 'b', 'c', 'd'], next: ['a', 'B', 'c', 'd'] },
  { name: '等高·首行变化', prev: ['a', 'b', 'c'], next: ['A', 'b', 'c'] },
  { name: '等高·末行变化', prev: ['a', 'b', 'c'], next: ['a', 'b', 'C'] },
  { name: '等高·全部变化', prev: ['a', 'b'], next: ['X', 'Y'] },
  { name: '完全不变', prev: ['a', 'b', 'c'], next: ['a', 'b', 'c'] },
  { name: '长行变短行', prev: ['x'.repeat(70)], next: ['short'] },
  { name: '带 ANSI 样式', prev: ['\u001b[31mred\u001b[0m', 'p'], next: ['\u001b[31mRED\u001b[0m', 'p'] },
  { name: '空行穿插', prev: ['a', '', 'c'], next: ['a', 'B', 'c'] },
  { name: '首尾都变', prev: ['a', 'b', 'c'], next: ['X', 'b', 'Z'] },
]

for (const c of CASES) {
  test(`diff replay matches full rewrite: ${c.name}`, () => {
    // 同一个初始状态：屏幕上先有上一帧，光标在其帧底。
    const setup = c.prev.join('\n') + (c.prev.length ? '\n' : '')
    const start = { row: c.prev.length, col: 0 }

    // 全量重写：eraseLines(prevN + 1) + body + '\n'
    // prevN + 1 是 ink 的 previousLineCount（output 以 \n 结尾，多一格）。
    const full = replay(eraseLines(c.prev.length + 1) + c.next.join('\n') + '\n', start, setup)

    const diffBody = buildLineDiff(c.prev, c.next)
    assert.ok(diffBody !== null, 'these cases must not need fallback')
    const diff = replay(diffBody!, start, setup)

    const mismatch = screensEqual(full, diff)
    assert.equal(mismatch, null, mismatch ?? '')
  })
}

test('diff replay matches full rewrite across a multi-frame sequence', () => {
  // 连续多帧：每帧都以上一帧的终点为起点，验证误差不累积。
  // 行数恒定 —— 行数变化一律退化（见 buildLineDiff 的 §为什么不处理变高）。
  const frames = [
    ['a', 'b', 'c'],
    ['a', 'B', 'c'],
    ['a', 'B', 'C'],
    ['A', 'B', 'C'],
    ['A', 'B', 'C'],
  ]

  const fullScreen = createScreen(60, 40)
  const diffScreen = createScreen(60, 40)
  let prev: string[] = []

  for (let i = 0; i < frames.length; i += 1) {
    const next = frames[i]!
    // 全量：从帧底擦 prevN+1 行再重写。
    const fullSeq = eraseLines(prev.length + 1) + next.join('\n') + '\n'
    applyToScreen(fullScreen, fullSeq, 40)

    // diff：同一帧底出发。行数不变时必然是合法序列；
    // 行数变化（含首帧，prev 为空）必须返回 null，此时写的就是全量。
    const diffSeq = buildLineDiff(prev, next)
    if (i === 0) {
      assert.equal(diffSeq, null, 'the very first frame has no known previous rows → must fall back')
    } else {
      assert.ok(diffSeq !== null, `equal-length frame must be diffable: ${JSON.stringify(next)}`)
    }
    applyToScreen(diffScreen, (diffSeq ?? fullSeq), 40)

    assert.equal(fullScreen.row, diffScreen.row, 'cursor row must track')
    for (let r = 0; r < Math.max(fullScreen.rows.length, diffScreen.rows.length); r += 1) {
      assert.equal(
        (fullScreen.rows[r] ?? '').trimEnd(),
        (diffScreen.rows[r] ?? '').trimEnd(),
        `row ${r} after frame ${JSON.stringify(next)}`,
      )
    }
    prev = next
  }
})

test('fallback is signalled for any row-count change', () => {
  // 调用方的契约：拿到 null 就必须走全量重写，不能当作「无变化」跳过。
  assert.equal(buildLineDiff(['a', 'b'], ['a']), null)          // 变矮
  assert.equal(buildLineDiff(['a', 'b'], ['a', 'b', 'c']), null) // 变高
  // 行数不变 → 合法序列（非 null），否则调用方会白白全量重写。
  assert.notEqual(buildLineDiff(['a', 'b'], ['a', 'B']), null)
})
