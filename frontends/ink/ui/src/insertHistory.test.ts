import test from 'node:test'
import assert from 'node:assert/strict'
import {
  advanceViewportForHistory,
  applyDockHeight,
  createViewportState,
  insertHistorySequence,
  isBottomAligned,
} from './insertHistory.js'

test('createViewportState starts with dock not bottom-aligned when content is short', () => {
  const vp = createViewportState({ screenRows: 24, screenCols: 80, dockHeight: 6 })
  assert.equal(vp.screenRows, 24)
  assert.equal(vp.dockHeight, 6)
  assert.ok(vp.areaY >= 0)
  assert.equal(vp.areaHeight, 6)
  assert.ok(vp.areaY + vp.areaHeight <= 24)
  assert.equal(isBottomAligned(vp), false)
})

test('createViewportState clamps oversized dockHeight to screenRows', () => {
  const vp = createViewportState({ screenRows: 10, screenCols: 40, dockHeight: 99 })
  assert.equal(vp.dockHeight, 10)
  assert.equal(vp.areaHeight, 10)
  assert.equal(vp.areaY, 0)
  assert.equal(isBottomAligned(vp), true)
})

test('createViewportState clamps areaY into valid range', () => {
  const high = createViewportState({ screenRows: 20, screenCols: 80, dockHeight: 5, areaY: 999 })
  assert.equal(high.areaY, 15)
  const neg = createViewportState({ screenRows: 20, screenCols: 80, dockHeight: 5, areaY: -3 })
  assert.equal(neg.areaY, 0)
})

test('advanceViewportForHistory moves dock down until bottom-aligned then freezes y', () => {
  let vp = createViewportState({ screenRows: 20, screenCols: 80, dockHeight: 5, areaY: 2 })
  const first = advanceViewportForHistory(vp, 4)
  assert.equal(first.scrollAmount, 4)
  assert.equal(first.state.areaY, 6)

  const second = advanceViewportForHistory(first.state, 100)
  assert.equal(second.state.areaY, 15)
  assert.equal(second.state.areaY + second.state.areaHeight, 20)
  assert.equal(isBottomAligned(second.state), true)

  const third = advanceViewportForHistory(second.state, 3)
  assert.equal(third.scrollAmount, 0)
  assert.equal(third.state.areaY, 15)
})

test('advanceViewportForHistory with zero lines is a no-op', () => {
  const vp = createViewportState({ screenRows: 20, screenCols: 80, dockHeight: 5, areaY: 3 })
  const next = advanceViewportForHistory(vp, 0)
  assert.equal(next.scrollAmount, 0)
  assert.equal(next.state.areaY, 3)
  assert.equal(next.state, vp)
})

test('advanceViewportForHistory multi-turn simulation reaches bottom then freezes', () => {
  let vp = createViewportState({ screenRows: 24, screenCols: 80, dockHeight: 6, areaY: 0 })
  const ys: number[] = [vp.areaY]
  for (let turn = 0; turn < 20; turn++) {
    const r = advanceViewportForHistory(vp, 3)
    vp = r.state
    ys.push(vp.areaY)
  }
  assert.equal(isBottomAligned(vp), true)
  assert.equal(vp.areaY, 18)
  // Monotonic non-decreasing y until freeze
  for (let i = 1; i < ys.length; i++) {
    assert.ok(ys[i]! >= ys[i - 1]!)
  }
  // Last several freezes identical
  assert.equal(ys.at(-1), ys.at(-2))
})

test('applyDockHeight grows dock and re-sticks to bottom when overflowing', () => {
  const vp = createViewportState({ screenRows: 20, screenCols: 80, dockHeight: 5, areaY: 15 })
  const grown = applyDockHeight(vp, 8)
  assert.equal(grown.areaHeight, 8)
  assert.equal(grown.areaY, 12)
  assert.equal(grown.areaY + grown.areaHeight, 20)
})

test('applyDockHeight shrink does not force bottom when room remains', () => {
  const vp = createViewportState({ screenRows: 20, screenCols: 80, dockHeight: 8, areaY: 5 })
  const shrunk = applyDockHeight(vp, 4)
  assert.equal(shrunk.areaHeight, 4)
  assert.equal(shrunk.areaY, 5)
  assert.equal(isBottomAligned(shrunk), false)
})

test('insertHistorySequence emits scroll-region style markers when moving dock down', () => {
  const vp = createViewportState({ screenRows: 24, screenCols: 80, dockHeight: 6, areaY: 4 })
  const { sequence, state, scrollAmount } = insertHistorySequence(vp, 3)
  assert.ok(sequence.includes('\x1b['), 'should emit CSI sequences')
  assert.ok(state.areaY > vp.areaY)
  assert.equal(scrollAmount, 3)
  assert.ok(sequence.includes('\x1b[s'))
  assert.ok(sequence.includes('\x1b[u'))
  assert.ok(sequence.includes('\x1bM') || sequence.includes('\r\n'))
})

test('insertHistorySequence scrollAmount is 0 when already bottom-aligned', () => {
  const vp = createViewportState({ screenRows: 24, screenCols: 80, dockHeight: 6, areaY: 18 })
  const { state, scrollAmount } = insertHistorySequence(vp, 5)
  assert.equal(scrollAmount, 0)
  assert.equal(state.areaY, 18)
})

test('insertHistorySequence with zero lines returns empty sequence', () => {
  const vp = createViewportState({ screenRows: 24, screenCols: 80, dockHeight: 6, areaY: 4 })
  const { sequence, scrollAmount } = insertHistorySequence(vp, 0)
  assert.equal(sequence, '')
  assert.equal(scrollAmount, 0)
})
