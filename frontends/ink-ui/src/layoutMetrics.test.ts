import test from 'node:test'
import assert from 'node:assert/strict'
import { computeLayoutMetrics, terminalRows } from './layoutMetrics.js'

test('terminalRows uses real narrow heights without forcing a tall minimum', () => {
  assert.equal(terminalRows(8), 8)
  assert.equal(terminalRows(1), 1)
  assert.equal(terminalRows(undefined), 24)
})

test('computeLayoutMetrics reserves one terminal row so Ink does not full-clear on each render', () => {
  assert.deepEqual(computeLayoutMetrics({
    rows: 24,
    columns: 80,
    hasActivity: false,
    hasError: false,
    hasPanel: false,
    hasSlashSuggestions: false,
  }), {
    rows: 23,
    columns: 80,
    headerRows: 1,
    bottomRows: 5,
    messageRows: 17,
  })
})

test('computeLayoutMetrics does not change bottom height when activity toggles', () => {
  const idle = computeLayoutMetrics({
    rows: 24,
    columns: 80,
    hasActivity: false,
    hasError: false,
    hasPanel: false,
    hasSlashSuggestions: false,
  })
  const running = computeLayoutMetrics({
    rows: 24,
    columns: 80,
    hasActivity: true,
    hasError: false,
    hasPanel: false,
    hasSlashSuggestions: false,
  })

  assert.equal(idle.bottomRows, running.bottomRows)
  assert.equal(idle.messageRows, running.messageRows)
})

test('computeLayoutMetrics reserves extra bottom rows for multiline input', () => {
  const metrics = computeLayoutMetrics({
    rows: 24,
    columns: 80,
    hasActivity: false,
    hasError: false,
    hasPanel: false,
    hasSlashSuggestions: false,
    inputRows: 3,
  })

  assert.equal(metrics.bottomRows, 7)
  assert.equal(metrics.messageRows, 15)
})

test('computeLayoutMetrics accounts for running activity and capped panels', () => {
  const metrics = computeLayoutMetrics({
    rows: 20,
    columns: 80,
    hasActivity: true,
    hasError: true,
    hasPanel: true,
    hasSlashSuggestions: false,
    panelRows: 20,
  })

  assert.equal(metrics.bottomRows, 17)
  assert.equal(metrics.messageRows, 1)
})

test('computeLayoutMetrics allows tall panels when the terminal has room', () => {
  const metrics = computeLayoutMetrics({
    rows: 24,
    columns: 120,
    hasActivity: false,
    hasError: false,
    hasPanel: true,
    hasSlashSuggestions: false,
    panelRows: 11,
  })

  assert.equal(metrics.bottomRows, 16)
  assert.equal(metrics.messageRows, 6)
})

test('computeLayoutMetrics never returns negative message rows on tiny terminals', () => {
  const metrics = computeLayoutMetrics({
    rows: 4,
    columns: 20,
    hasActivity: true,
    hasError: true,
    hasPanel: true,
    hasSlashSuggestions: true,
    panelRows: 10,
  })

  assert.equal(metrics.messageRows, 1)
  assert.equal(metrics.rows, 3)
  assert.equal(metrics.columns, 20)
})
