import test from 'node:test'
import assert from 'node:assert/strict'
import {
  formatGoalDuration,
  formatGoalTokens,
  goalCardLines,
  goalElapsedMs,
  goalCompactLine,
  goalSummaryLine,
  goalStatusLine,
  goalTerminalLines,
  type GoalMetadata,
} from './goalCard.js'

const meta = (over: Partial<GoalMetadata> = {}): GoalMetadata => ({
  condition: 'all tests pass',
  iterations: 2,
  set_at: 1000,
  tokens_at_start: 100,
  tokens_spent: 1500,
  met: false,
  ...over,
})

test('formatGoalDuration', () => {
  assert.equal(formatGoalDuration(5_000), '5s')
  assert.equal(formatGoalDuration(65_000), '1m 5s')
  assert.equal(formatGoalDuration(3_725_000), '1h 2m')
})

test('formatGoalTokens', () => {
  assert.equal(formatGoalTokens(900), '900')
  assert.equal(formatGoalTokens(1500), '1.5k')
  assert.equal(formatGoalTokens(2000), '2k')
})

test('goalElapsedMs', () => {
  assert.equal(goalElapsedMs(meta({ set_at: 1000 }), 1_100_000), 100_000)
  assert.equal(goalElapsedMs(meta({ set_at: 0 }), 5000), 0)
})

test('goalCardLines has running header and goal', () => {
  const lines = goalCardLines(meta(), 1_120_000)
  assert.equal(lines.length, 2)
  assert.match(lines[0], /Goal active · running 2m 0s · 2 turns · 1\.5k tokens/)
  assert.equal(lines[1], 'Goal: all tests pass')
})

test('goalCardLines singular turn and last check', () => {
  const lines = goalCardLines(meta({ iterations: 1, last_reason: 'not yet\nstill going' }), 1_030_000)
  assert.match(lines[0], /1 turn/)
  assert.match(lines[1], /Last check: not yet still going/)
})

test('goalCardLines empty without goal', () => {
  assert.deepEqual(goalCardLines(null, 0), [])
})

test('goalStatusLine and goalSummaryLine share the header line', () => {
  const lines = goalCardLines(meta(), 1_120_000)
  assert.equal(goalStatusLine(meta(), 1_120_000), lines[0])
  assert.equal(goalSummaryLine(meta(), 1_120_000), lines[0])
})

test('goalSummaryLine is a single line and null without a goal', () => {
  const summary = goalSummaryLine(meta({ last_reason: 'still going' }), 1_120_000)
  assert.equal(typeof summary, 'string')
  assert.equal(summary!.includes(String.fromCharCode(10)), false)
  assert.match(summary!, /Goal active/)
  // It must not leak the second (Goal:/Last check) card line.
  assert.doesNotMatch(summary!, /Last check/)
  assert.equal(goalSummaryLine(null, 0), null)
})

test('goalCompactLine prefers active status, falls back to terminal badge', () => {
  assert.equal(goalCompactLine(meta(), null, 1_120_000), goalStatusLine(meta(), 1_120_000))
  assert.equal(goalCompactLine(null, { met: true, durationMs: 65_000 }, 0), '✓ Goal met · 1m 5s')
  assert.equal(goalCompactLine(null, { met: false, failed: true }, 0), '✗ Goal failed')
  assert.equal(goalCompactLine(null, null, 0), null)
})

test('goalTerminalLines badge', () => {
  assert.deepEqual(goalTerminalLines({ met: true, durationMs: 65_000, iterations: 2, tokens: 1500 }), [
    '✓ Goal met · 1m 5s · 2 turns · 1.5k tokens',
  ])
  assert.deepEqual(goalTerminalLines({ met: false, failed: true }), ['✗ Goal failed'])
  assert.deepEqual(goalTerminalLines(null), [])
})
