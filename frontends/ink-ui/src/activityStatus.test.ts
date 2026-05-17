import test from 'node:test'
import assert from 'node:assert/strict'
import { formatElapsed, formatRunningStatus, pickRunningVerb, RUNNING_VERBS } from './activityStatus.js'

test('formatElapsed renders seconds and minutes', () => {
  assert.equal(formatElapsed(0), '0s')
  assert.equal(formatElapsed(40), '40s')
  assert.equal(formatElapsed(75), '1m 15s')
})

test('formatRunningStatus renders Claude-style activity text', () => {
  assert.equal(formatRunningStatus(40), '✻ Hyperspacing (40s)')
})

test('pickRunningVerb picks deterministically from the verb list', () => {
  assert.equal(pickRunningVerb(() => 0), RUNNING_VERBS[0])
  assert.equal(pickRunningVerb(() => 0.999), RUNNING_VERBS[RUNNING_VERBS.length - 1])
})
