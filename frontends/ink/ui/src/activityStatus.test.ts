import test from 'node:test'
import assert from 'node:assert/strict'
import {
  completedStatusSegments,
  formatCompletedStatus,
  formatElapsed,
  formatRunningStatus,
  formatTokenCount,
  pickRunningVerb,
  runningStatusSegments,
  RUNNING_VERBS,
  shouldShowActivityStatus,
} from './activityStatus.js'

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

test('formatCompletedStatus renders duration and local completion date/time', () => {
  const completedAt = new Date(2026, 9, 5, 13, 41, 0)
  assert.equal(formatCompletedStatus(131, completedAt), 'Worked for 2m 11s • Oct 5 at 13:41')
})

test('formatCompletedStatus keeps token usage visible after a turn completes', () => {
  const completedAt = new Date(2026, 9, 5, 13, 41, 0)
  assert.equal(
    formatCompletedStatus(131, completedAt, { inputTokens: 12000, outputTokens: 1000, totalTokens: 13000 }),
    'Worked for 2m 11s • Oct 5 at 13:41 • ↑12k ↓1k Σ13k',
  )
})

test('formatCompletedStatus renders sub-second and hour-long turns in Codex style', () => {
  const completedAt = new Date(2026, 9, 5, 13, 41, 0)
  assert.equal(formatCompletedStatus(0, completedAt), 'Worked for <1s • Oct 5 at 13:41')
  assert.equal(formatCompletedStatus(3661, completedAt), 'Worked for 1h 1m 1s • Oct 5 at 13:41')
})

test('formatRunningStatus renders cached input tokens when available', () => {
  assert.equal(
    formatRunningStatus(40, 'Hyperspacing', {
      inputTokens: 100,
      outputTokens: 20,
      totalTokens: 120,
      cachedTokens: 70,
      cacheReadTokens: 70,
      cacheCreationTokens: 0,
    }),
    '✻ Hyperspacing (40s · ↑100 ↓20 Σ120 cache 70)',
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
  assert.equal(shouldShowActivityStatus('idle', false, null, true), true)
})

test('pickRunningVerb picks deterministically from the verb list', () => {
  assert.equal(pickRunningVerb(() => 0), RUNNING_VERBS[0])
  assert.equal(pickRunningVerb(() => 0.999), RUNNING_VERBS[RUNNING_VERBS.length - 1])
})

test('runningStatusSegments joins into the same text as formatRunningStatus', () => {
  // The segment API is the rendering path; the string API is kept for callers and
  // tests. They must never drift apart.
  const usage = { inputTokens: 1200, outputTokens: 17500, totalTokens: 18700 }
  assert.equal(
    runningStatusSegments(40, 'Hyperspacing', usage).map(segment => segment.text).join(''),
    formatRunningStatus(40, 'Hyperspacing', usage),
  )
  assert.equal(
    runningStatusSegments(40).map(segment => segment.text).join(''),
    formatRunningStatus(40),
  )
})

test('runningStatusSegments marks only the token usage for emphasis', () => {
  const segments = runningStatusSegments(40, 'Hyperspacing', {
    inputTokens: 1200,
    outputTokens: 17500,
    totalTokens: 18700,
  })
  const emphasised = segments.filter(segment => segment.emphasis)
  assert.equal(emphasised.length, 1)
  assert.equal(emphasised[0]!.text, '↑1.2k ↓17.5k Σ18.7k')
  // The elapsed label and the wrapping parens stay normal weight.
  assert.ok(segments.some(segment => !segment.emphasis && segment.text.startsWith('✻')))
})

test('runningStatusSegments emphasises cache reads when present', () => {
  const segments = runningStatusSegments(40, 'Hyperspacing', {
    inputTokens: 100,
    outputTokens: 20,
    totalTokens: 120,
    cacheReadTokens: 70,
  })
  const emphasised = segments.filter(segment => segment.emphasis).map(segment => segment.text)
  assert.deepEqual(emphasised, ['↑100 ↓20 Σ120 cache 70'])
})

test('runningStatusSegments emits no emphasis segment without usage', () => {
  const segments = runningStatusSegments(40, 'Hyperspacing', null)
  assert.equal(segments.some(segment => segment.emphasis), false)
  assert.equal(segments.map(segment => segment.text).join(''), '✻ Hyperspacing (40s)')
})

test('completedStatusSegments keeps the trailing separator and emphasises only usage', () => {
  const completedAt = new Date(2026, 9, 5, 13, 41, 0)
  const segments = completedStatusSegments(131, completedAt, {
    inputTokens: 12000,
    outputTokens: 1000,
    totalTokens: 13000,
  })
  assert.equal(
    segments.map(segment => segment.text).join(''),
    formatCompletedStatus(131, completedAt, { inputTokens: 12000, outputTokens: 1000, totalTokens: 13000 }),
  )
  const emphasised = segments.filter(segment => segment.emphasis).map(segment => segment.text)
  assert.deepEqual(emphasised, ['↑12k ↓1k Σ13k'])
})
