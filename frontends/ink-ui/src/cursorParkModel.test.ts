import test from 'node:test'
import assert from 'node:assert/strict'
import { CursorTracker, eraseLines, inkFrame, simulateFrames } from './cursorParkModel.js'

// 3 行帧：row0 hint / row1 边框 / row2 "> " caret 行。caret 在第 2 行、第 2 列。
const BODY = ['hint line', '─────────', '> ']
const CARET_ROW = 2
const CARET_COL = 2

test('eraseLines matches ink@5 ansi-escapes byte sequence', () => {
  // 实测自 node_modules/ink 依赖 ansi-escapes：相对上移逐行擦，最后回列 0。
  assert.equal(eraseLines(1), '\x1b[2K\x1b[G')
  assert.equal(eraseLines(3), '\x1b[2K\x1b[1A\x1b[2K\x1b[1A\x1b[2K\x1b[G')
  assert.equal(eraseLines(0), '')
})

test('inkFrame appends trailing newline and counts lines like log-update', () => {
  const { seq, lineCount } = inkFrame(BODY.join('\n'), 0)
  assert.ok(seq.endsWith('> \n'))
  // output = body + '\n' → 'a\nb\nc\n'.split('\n') = ['a','b','c',''] = 4
  assert.equal(lineCount, 4)
})

test('CursorTracker interprets relative moves and records erased rows', () => {
  const t = new CursorTracker()
  t.feed('abc')            // col 0→3
  assert.deepEqual(t.cursor, { row: 0, col: 3 })
  t.feed('\n')             // row 0→1, col stays (bare LF)
  assert.equal(t.cursor.row, 1)
  t.feed('\x1b[2A')        // up 2 → row -1 (越顶)
  assert.equal(t.cursor.row, -1)
  t.feed('\x1b[3;5H')      // CUP → (2,4) 0-based
  assert.deepEqual(t.cursor, { row: 2, col: 4 })
  t.feed('\x1b[2K')        // erase current row 2
  assert.deepEqual(t.erasedRows, [2])
})

test('baseline "none": cursor stays at bottom, eraseLines always erases correct rows', () => {
  // ink 理想状态——不放 caret，光标留在内容底部，每帧 eraseLines 擦对行。
  const { ok } = simulateFrames('none', 5, BODY, CARET_ROW, CARET_COL)
  assert.ok(ok, 'baseline must keep the erase invariant')
})

test('REPRO current SCO-park races ink: eraseLines erases the WRONG rows (ghost root cause)', () => {
  // 现状复现：帧末 \x1b[s + 绝对 CUP 到 caret，下一帧 ink 写入前光标仍在 caret
  // （effect 复位与 throttledLog 不同步）。eraseLines 从 caret（中部）往上擦 →
  // 擦不到上一帧内容顶行 → ghost composer / 整框漂移。此测试锁定 bug 存在。
  const { ok, detail } = simulateFrames('current-sco', 5, BODY, CARET_ROW, CARET_COL)
  assert.equal(ok, false, `current SCO park should FAIL the invariant; detail:\n${detail.join('\n')}`)
})

test('FIX wrapper-relative: reset caret→bottom before each frame keeps erase invariant', () => {
  // 路径 A：包装器在下一帧 ink 写入前，先把光标从 caret 相对下移复位到底部，
  // 让 ink 的 eraseLines 始终从底部往上擦对行；帧末再相对上移到 caret。
  // 内容写入与光标定位由同一 writer 串行，永不竞态。
  const { ok, detail } = simulateFrames('wrapper-relative', 5, BODY, CARET_ROW, CARET_COL)
  assert.ok(ok, `wrapper-relative must keep the erase invariant; detail:\n${detail.join('\n')}`)
})
