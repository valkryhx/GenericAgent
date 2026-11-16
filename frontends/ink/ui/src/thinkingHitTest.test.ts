import test from 'node:test'
import assert from 'node:assert/strict'
import { hitTestThinkingRow, thinkingRowsOf } from './thinkingHitTest.js'

// 回归：折叠/展开的三角 `▸` 一直是**死控件** —— 终端把它画成三角，用户自然会
// 去点，但鼠标处理只做文本选区与「跳到底部」药丸，没有任何点击展开的路径。
// 这两个纯函数就是点击命中测试，从 App.tsx 里抽出来才能在没有真终端鼠标上报
// 的情况下测到。

function rows(lines: { id: string; text: string }[]) {
  return thinkingRowsOf(lines)
}

test('collapsed thinking summary row is a header target', () => {
  const lines = [
    { id: 'a-1-thinking-0', text: '▸ ✻ Wait for A first.' },
  ]
  const mapped = rows(lines)
  assert.deepEqual(mapped, [{ row: 0, kind: 'header' }])
  assert.equal(hitTestThinkingRow(mapped, 0), true)
})

test('expanded thinking header and its body lines are all targets', () => {
  const lines = [
    { id: 'a-1-thinking-0', text: '▾ ✻ thinking' },
    { id: 'a-1-thinking-0-line-0', text: '  Wait for A first.' },
    { id: 'a-1-thinking-0-line-1', text: '  Then read it.' },
  ]
  const mapped = rows(lines)
  assert.deepEqual(mapped.map(entry => entry.kind), ['header', 'body', 'body'])
  assert.equal(hitTestThinkingRow(mapped, 0), true)
  assert.equal(hitTestThinkingRow(mapped, 1), true)
  assert.equal(hitTestThinkingRow(mapped, 2), true)
})

test('several blocks each map to their own rows', () => {
  const lines = [
    { id: 'u-1', text: '> question' },
    { id: 'a-1-thinking-0', text: '▸ ✻ one' },
    { id: 'a-1-c0', text: 'prose' },
    { id: 'a-1-thinking-1', text: '▸ ✻ two' },
  ]
  const mapped = rows(lines)
  assert.deepEqual(mapped.map(entry => entry.row), [1, 3])
})

test('a click on ordinary transcript text is not a thinking hit', () => {
  const lines = [
    { id: 'u-1', text: '> question' },
    { id: 'a-1-thinking-0', text: '▸ ✻ reason' },
    { id: 'a-1', text: 'answer' },
  ]
  const mapped = rows(lines)
  assert.equal(hitTestThinkingRow(mapped, 0), false, 'user row must fall through to selection')
  assert.equal(hitTestThinkingRow(mapped, 2), false, 'prose row must fall through to selection')
})

test('a click below the last row is not a hit', () => {
  const mapped = rows([{ id: 'a-1-thinking-0', text: '▸ ✻ reason' }])
  assert.equal(hitTestThinkingRow(mapped, 5), false)
})

test('tool chips that merely start with a triangle are not thinking rows', () => {
  // The id is what decides membership: a tool chip line reuses the triangle glyph
  // in other contexts, and must not become a disclosure target by its text alone.
  const mapped = rows([{ id: 'a-1-c0', text: '▸ ✻ looks like reasoning but is prose' }])
  assert.deepEqual(mapped, [])
})
