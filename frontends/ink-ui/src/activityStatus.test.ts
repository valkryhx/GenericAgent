import test from 'node:test'
import assert from 'node:assert/strict'
import { formatElapsed, formatRunningStatus, formatTokenCount, pickRunningVerb, RUNNING_VERBS, shouldShowActivityStatus } from './activityStatus.js'

test('formatElapsed renders seconds and minutes', () => {
  assert.equal(formatElapsed(0), '0s')
  assert.equal(formatElapsed(40), '40s')
  assert.equal(formatElapsed(75), '1m 15s')
})

test('formatRunningStatus renders Claude-style activity text', () => {
  assert.equal(formatRunningStatus(40), '✻ Hyperspacing (40s)')
})

test('formatRunningStatus appends live token usage when available', () => {
  assert.equal(
    formatRunningStatus(40, 'Hyperspacing', { inputTokens: 11, outputTokens: 17, totalTokens: 28 }),
    '✻ Hyperspacing (40s · ↑11 ↓17 Σ28)',
  )
})

test('formatRunningStatus renders token usage in k units above one thousand', () => {
  assert.equal(
    formatRunningStatus(40, 'Hyperspacing', { inputTokens: 1200, outputTokens: 17500, totalTokens: 18700 }),
    '✻ Hyperspacing (40s · ↑1.2k ↓17.5k Σ18.7k)',
  )
})

test('formatTokenCount keeps sub-thousand values exact and trims whole k values', () => {
  assert.equal(formatTokenCount(999), '999')
  assert.equal(formatTokenCount(1000), '1k')
  assert.equal(formatTokenCount(1050), '1.1k')
  assert.equal(formatTokenCount(12000), '12k')
})

test('shouldShowActivityStatus keeps final token usage visible after returning idle', () => {
  assert.equal(shouldShowActivityStatus('running', true, null), true)
  assert.equal(shouldShowActivityStatus('idle', false, { inputTokens: 11, outputTokens: 17, totalTokens: 28 }), true)
  assert.equal(shouldShowActivityStatus('idle', false, null), false)
})

test('pickRunningVerb picks deterministically from the verb list', () => {
  assert.equal(pickRunningVerb(() => 0), RUNNING_VERBS[0])
  assert.equal(pickRunningVerb(() => 0.999), RUNNING_VERBS[RUNNING_VERBS.length - 1])
})
