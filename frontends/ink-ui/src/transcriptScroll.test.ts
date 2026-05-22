import test from 'node:test'
import assert from 'node:assert/strict'
import {
  clampTranscriptScrollOffset,
  maxTranscriptScrollOffset,
  preserveTranscriptScrollOnContentChange,
  scrollTranscriptBy,
  transcriptScrollStep,
  transcriptWheelStep,
} from './transcriptScroll.js'

test('maxTranscriptScrollOffset allows the first long answer to scroll inside the transcript viewport', () => {
  assert.equal(maxTranscriptScrollOffset(40, 10), 30)
  assert.equal(maxTranscriptScrollOffset(8, 10), 0)
})

test('scrollTranscriptBy clamps offsets at transcript bounds', () => {
  assert.equal(scrollTranscriptBy(0, 5, 20, 10), 5)
  assert.equal(scrollTranscriptBy(5, -2, 20, 10), 3)
  assert.equal(scrollTranscriptBy(5, -99, 20, 10), 0)
  assert.equal(scrollTranscriptBy(8, 99, 20, 10), 10)
})

test('preserveTranscriptScrollOnContentChange keeps the same historical rows visible when new output arrives', () => {
  assert.equal(preserveTranscriptScrollOnContentChange(6, 40, 45, 10), 11)
})

test('preserveTranscriptScrollOnContentChange remains sticky at the bottom', () => {
  assert.equal(preserveTranscriptScrollOnContentChange(0, 40, 45, 10), 0)
})

test('transcript scroll helpers use stable page and wheel increments', () => {
  assert.equal(transcriptScrollStep(20), 10)
  assert.equal(transcriptScrollStep(3), 1)
  assert.equal(transcriptScrollStep(0), 1)
  assert.equal(transcriptWheelStep(), 3)
  assert.equal(clampTranscriptScrollOffset(99, 10, 4), 6)
})
