import test from 'node:test'
import assert from 'node:assert/strict'
import { scrollTranscriptBy, transcriptScrollStep } from './transcriptScroll.js'

test('transcriptScrollStep uses half the viewport with a one-row minimum', () => {
  assert.equal(transcriptScrollStep(20), 10)
  assert.equal(transcriptScrollStep(3), 1)
  assert.equal(transcriptScrollStep(0), 1)
})

test('scrollTranscriptBy clamps offsets at the sticky bottom', () => {
  assert.equal(scrollTranscriptBy(0, 5), 5)
  assert.equal(scrollTranscriptBy(5, -2), 3)
  assert.equal(scrollTranscriptBy(5, -99), 0)
})
