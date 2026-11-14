/**
 * 选区坐标空间转换（屏幕行 ↔ 内容行）。
 *
 * 这批测试锁的是 2026-11-14 用户实测报的问题：
 * 「无法选出屏幕外需要滑动的区域，也就是无法自动随着滑动来上下扩展屏幕」。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import {
  autoScrollDirection,
  contentRowToScreenRow,
  focusContentRowAtEdge,
  projectRangeToScreen,
  screenRowToContentRow,
  transcriptWindowStart,
  visibleTranscriptContentRows,
  type SelectionSpace,
} from './selectionSpace.js'

const base = (over: Partial<SelectionSpace> = {}): SelectionSpace => ({
  headerRows: 1,
  transcriptTotalRows: 100,
  transcriptViewportRows: 10,
  transcriptScrollOffset: 0,
  bandRows: 2,
  ...over,
})

test('at the tail, the window shows the last viewport rows', () => {
  const s = base()
  assert.equal(transcriptWindowStart(s), 90) // 100 - 10
})

test('scrolling up moves the window toward history', () => {
  const s = base({ transcriptScrollOffset: 6 })
  assert.equal(transcriptWindowStart(s), 84) // 100 - 6 - 10
})

test('scroll offset is clamped: never before row 0', () => {
  const s = base({ transcriptScrollOffset: 9999 })
  assert.equal(transcriptWindowStart(s), 0)
})

test('content shorter than the viewport pins to the top', () => {
  const s = base({ transcriptTotalRows: 4 })
  assert.equal(transcriptWindowStart(s), 0)
  assert.equal(visibleTranscriptContentRows(s), 4, '只有 4 行内容，不该假装有 10 行')
})

// ---- 屏幕行 → 内容行 ----

test('header rows map to themselves', () => {
  const s = base()
  assert.equal(screenRowToContentRow(s, 0), 0)
})

test('the first viewport row maps to the window start, not to the header', () => {
  const s = base()
  // 屏幕行 1 = 视口首行 = 内容行 90
  assert.equal(screenRowToContentRow(s, 1), 90)
  assert.equal(screenRowToContentRow(s, 10), 99)
})

test('the same screen row maps to a different content row after scrolling', () => {
  // 这正是「滚动之后选区指向了别的文本」的根因：屏幕行是不变的，
  // 内容行变了。存屏幕行就等于存了一个会漂移的锚点。
  const atTail = base()
  const scrolled = base({ transcriptScrollOffset: 6 })
  assert.equal(screenRowToContentRow(atTail, 1), 90)
  assert.equal(screenRowToContentRow(scrolled, 1), 84, '滚动后同一屏幕行是不同内容')
})

test('rows below the viewport continue as band and composer', () => {
  const s = base()
  // header(1) + viewport(10) = 11 → 屏幕行 11 是 transcript 之后的第一个内容行
  assert.equal(screenRowToContentRow(s, 11), 101) // 1 + 100 + 0
  assert.equal(screenRowToContentRow(s, 12), 102)
})

test('padding rows inside a partly-filled viewport have no content', () => {
  // 内容只有 4 行、视口 10 行：屏幕行 1..4 是内容，5..10 是空白填充
  const s = base({ transcriptTotalRows: 4 })
  assert.equal(screenRowToContentRow(s, 1), 0)
  assert.equal(screenRowToContentRow(s, 4), 3)
  assert.equal(screenRowToContentRow(s, 5), null, '空白填充行不该对应内容')
})

// ---- 内容行 → 屏幕行 ----

test('a content row in the current window has a screen row', () => {
  const s = base()
  assert.equal(contentRowToScreenRow(s, 90), 1)
  assert.equal(contentRowToScreenRow(s, 99), 10)
})

test('content scrolled out of the window has no screen row', () => {
  const s = base()
  assert.equal(contentRowToScreenRow(s, 89), null, '滚出去的内容不该有屏幕行')
  // 内容行 1 是 transcript 的第一行（内容行 0 是 header，它一直在屏幕上）
  assert.equal(contentRowToScreenRow(s, 1), null, 'transcript 开头早已滚出视口')
})

test('band content maps below the viewport, not below the whole transcript', () => {
  const s = base()
  // 内容行 101 = transcript 之后第一行 → 屏幕行 11
  assert.equal(contentRowToScreenRow(s, 101), 11)
})

// ---- 拖到边缘自动滚动 ----

test('dragging above the viewport asks to scroll up', () => {
  const s = base()
  assert.equal(autoScrollDirection(s, 0), 'up')
  assert.equal(autoScrollDirection(s, -3), 'up')
})

test('dragging below the viewport asks to scroll down', () => {
  const s = base()
  assert.equal(autoScrollDirection(s, 11), 'down')
  assert.equal(autoScrollDirection(s, 40), 'down')
})

test('dragging inside the viewport does not scroll', () => {
  const s = base()
  assert.equal(autoScrollDirection(s, 5), null)
})

test('no auto-scroll when the content fits on screen', () => {
  const s = base({ transcriptTotalRows: 5 })
  assert.equal(autoScrollDirection(s, 0), null, '没得滚就不该滚')
  assert.equal(autoScrollDirection(s, 40), null)
})

test('no auto-scroll down when already at the tail', () => {
  // 贴底时再往右下拉不应把焦点丢到一个不存在的行上
  const s = base({ transcriptScrollOffset: 0 })
  assert.equal(autoScrollDirection(s, 11), 'down', '方向本身可以给出')
  // 但焦点行必须落在有效范围内
  const row = focusContentRowAtEdge(s, 'down')
  assert.ok(row <= 99, `焦点行 ${row} 超出 transcript 末尾`)
})

test('focus after scrolling up is the NEW window start', () => {
  // 关键：滚完之后焦点要落在滚完的窗口首行，否则每滚一次慢一行
  const before = base({ transcriptScrollOffset: 6 })
  const after = base({ transcriptScrollOffset: 10 })
  assert.equal(focusContentRowAtEdge(after, 'up'), 80) // 100 - 10 - 10
  assert.notEqual(
    focusContentRowAtEdge(after, 'up'),
    focusContentRowAtEdge(before, 'up'),
    '焦点必须跟着滚动走',
  )
})

test('focus after scrolling down is the new window last row', () => {
  const after = base({ transcriptScrollOffset: 4 })
  assert.equal(focusContentRowAtEdge(after, 'down'), 95) // 86 + 10 - 1
})

test('focus at the very top of history is row 0', () => {
  const top = base({ transcriptScrollOffset: 90 })
  assert.equal(transcriptWindowStart(top), 0)
  assert.equal(focusContentRowAtEdge(top, 'up'), 0)
})

// ---- 内容坐标 → 屏幕坐标投影（画高亮用）----
//
// 选区存内容坐标，渲染时要投影回屏幕。屏幕外的部分收拢到视口边缘，
// 这样用户拖出屏幕时看得见「高亮贴边」的反馈。

test('a range fully inside the window projects row-for-row', () => {
  const s = base()
  // 内容行 = header(1) + transcript 内部行号 → 内容行 92 是内部行 91，
  // 窗口从内部行 90 开始，所以落在屏幕行 1 + (91-90) = 2
  const p = projectRangeToScreen(s, { start: { row: 92, col: 0 }, end: { row: 95, col: 4 } })
  assert.deepEqual(p.start, { row: 2, col: 0 })
  assert.deepEqual(p.end, { row: 5, col: 4 })
})

test('a range extending above the window collapses to the first viewport row', () => {
  const s = base()
  const p = projectRangeToScreen(s, { start: { row: 10, col: 3 }, end: { row: 92, col: 0 } })
  assert.deepEqual(p.start, { row: 1, col: 0 }, '滚出去的上半段贴到视口首行')
  assert.deepEqual(p.end, { row: 2, col: 0 })
})

test('a range extending below the window collapses to the last viewport row', () => {
  const s = base()
  // 内容行 100 是 transcript 最后一行（内部 99），仍在窗口内 → 屏幕 10
  const inside = projectRangeToScreen(s, { start: { row: 98, col: 0 }, end: { row: 100, col: 2 } })
  assert.deepEqual(inside.start, { row: 8, col: 0 })
  assert.deepEqual(inside.end, { row: 10, col: 2 })

  // 窗口上滚后，末尾就落到视口之外了
  const scrolled = base({ transcriptScrollOffset: 5 })
  const below = projectRangeToScreen(scrolled, { start: { row: 97, col: 0 }, end: { row: 100, col: 2 } })
  assert.equal(below.end.row, 10, '滚出去的下半段贴到视口末行')
})

test('a range covering the whole transcript fills the viewport', () => {
  const s = base()
  const p = projectRangeToScreen(s, { start: { row: 1, col: 0 }, end: { row: 100, col: 0 } })
  assert.equal(p.start.row, 1)
  assert.equal(p.end.row, 10)
})

test('content after the transcript (composer) projects above where it would naively land', () => {
  const s = base()
  // 内容行 101 是 transcript 之后第一行 → 屏幕行 11
  const p = projectRangeToScreen(s, { start: { row: 101, col: 0 }, end: { row: 101, col: 5 } })
  assert.deepEqual(p.start, { row: 11, col: 0 })
  assert.deepEqual(p.end, { row: 11, col: 5 })
})

test('header rows are unaffected by the projection', () => {
  const s = base({ headerRows: 2 })
  // headerRows=2 时行 0..1 都是 header，整段原样投影
  const p = projectRangeToScreen(s, { start: { row: 0, col: 0 }, end: { row: 1, col: 2 } })
  assert.deepEqual(p.start, { row: 0, col: 0 })
  assert.deepEqual(p.end, { row: 1, col: 2 })
})
