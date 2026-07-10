import test from 'node:test'
import assert from 'node:assert/strict'
import {
  isScrollbarColumn,
  shouldHandleScrollbarDrag,
  scrollOffsetForScrollbarClick,
  transcriptScrollbar,
  transcriptScrollbarLines,
} from './transcriptScrollbar.js'

test('transcriptScrollbar sizes the thumb from the complete transcript history', () => {
  assert.deepEqual(transcriptScrollbar({ totalRows: 100, viewportRows: 10, scrollOffset: 0 }), {
    thumbStart: 9,
    thumbSize: 1,
    trackRows: 10,
    visible: true,
  })
})

test('transcriptScrollbar moves to the top when scrolled to the oldest rows', () => {
  assert.deepEqual(transcriptScrollbar({ totalRows: 100, viewportRows: 10, scrollOffset: 90 }), {
    thumbStart: 0,
    thumbSize: 1,
    trackRows: 10,
    visible: true,
  })
})

test('transcriptScrollbar hides when all history fits in the viewport', () => {
  assert.deepEqual(transcriptScrollbar({ totalRows: 8, viewportRows: 10, scrollOffset: 0 }), {
    thumbStart: 0,
    thumbSize: 10,
    trackRows: 10,
    visible: false,
  })
})

test('transcriptScrollbarLines returns exactly one cell for every viewport row', () => {
  const lines = transcriptScrollbarLines({ totalRows: 100, viewportRows: 10, scrollOffset: 45 })

  assert.equal(lines.length, 10)
  assert.equal(lines.every(line => line === '│' || line === '█'), true)
})

test('scrollOffsetForScrollbarClick maps top and bottom track clicks to transcript edges', () => {
  assert.equal(scrollOffsetForScrollbarClick({ totalRows: 100, viewportRows: 10, y: 2, viewportTop: 2 }), 90)
  assert.equal(scrollOffsetForScrollbarClick({ totalRows: 100, viewportRows: 10, y: 11, viewportTop: 2 }), 0)
})

test('scrollOffsetForScrollbarClick maps middle track clicks proportionally', () => {
  assert.equal(scrollOffsetForScrollbarClick({ totalRows: 100, viewportRows: 10, y: 6, viewportTop: 2 }), 50)
})

test('scrollOffsetForScrollbarClick ignores clicks when no scrollbar is visible', () => {
  assert.equal(scrollOffsetForScrollbarClick({ totalRows: 8, viewportRows: 10, y: 2, viewportTop: 2 }), null)
})

test('isScrollbarColumn accepts the visual scrollbar and a one-column tolerance', () => {
  assert.equal(isScrollbarColumn(80, 80), true)
  assert.equal(isScrollbarColumn(79, 80), true)
  assert.equal(isScrollbarColumn(78, 80), false)
})

test('safe-canvas scrollbar hit testing ignores the physical final column', () => {
  assert.equal(isScrollbarColumn(79, 79), true)
  assert.equal(isScrollbarColumn(80, 79), false)
  assert.equal(isScrollbarColumn(78, 79), true)
})

test('shouldHandleScrollbarDrag keeps tracking after the initial press leaves the scrollbar column', () => {
  assert.equal(shouldHandleScrollbarDrag({ kind: 'press', x: 80, columns: 80, dragging: false }), true)
  assert.equal(shouldHandleScrollbarDrag({ kind: 'drag', x: 76, columns: 80, dragging: true }), true)
  assert.equal(shouldHandleScrollbarDrag({ kind: 'drag', x: 80, columns: 80, dragging: false }), false)
})
