import test from 'node:test'
import assert from 'node:assert/strict'
import { transcriptScrollbar } from './transcriptScrollbar.js'

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
