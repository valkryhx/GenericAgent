import test from 'node:test'
import assert from 'node:assert/strict'
import {
  computeLayoutMetrics,
  terminalCanvasColumns,
  terminalRows,
  transcriptContentColumns,
  transcriptScrollbarColumns,
} from './layoutMetrics.js'

test('terminalRows uses real narrow heights without forcing a tall minimum', () => {
  assert.equal(terminalRows(8), 8)
  assert.equal(terminalRows(1), 1)
  assert.equal(terminalRows(undefined), 24)
})

test('terminalCanvasColumns permanently reserves the physical final column', () => {
  assert.equal(terminalCanvasColumns(120), 119)
  assert.equal(terminalCanvasColumns(80), 79)
  assert.equal(terminalCanvasColumns(40), 39)
  assert.equal(terminalCanvasColumns(1), 1)
})

test('transcript widths derive from the safe canvas', () => {
  assert.equal(transcriptScrollbarColumns(79), 1)
  assert.equal(transcriptContentColumns(79), 76)
})

test('computeLayoutMetrics exposes physical and safe widths separately', () => {
  const metrics = computeLayoutMetrics({
    rows: 24,
    columns: 80,
    hasActivity: false,
    hasError: false,
    hasPanel: false,
    hasSlashSuggestions: false,
    inputRows: 1,
  })

  assert.equal(metrics.terminalColumns, 80)
  assert.equal(metrics.canvasColumns, 79)
})

test('computeLayoutMetrics allows zero header rows for inline scrollback chrome', () => {
  const metrics = computeLayoutMetrics({
    rows: 24,
    columns: 80,
    hasActivity: false,
    hasError: false,
    hasPanel: false,
    hasSlashSuggestions: false,
    headerRows: 0,
  })
  assert.equal(metrics.headerRows, 0)
  assert.equal(metrics.bottomRows, 5)
  assert.equal(metrics.messageRows, 18)
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
  assert.equal(metrics.terminalColumns, 20)
  assert.equal(metrics.canvasColumns, 19)
})
