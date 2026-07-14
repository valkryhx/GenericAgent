import test from 'node:test'
import assert from 'node:assert/strict'
import { cursorPosition, inputCursorPosition } from './terminalCursor.js'

test('cursorPosition emits a clamped absolute CUP escape sequence', () => {
  assert.equal(cursorPosition(0, 0, 24, 80), '\x1b[1;1H')
  assert.equal(cursorPosition(21, 3, 24, 80), '\x1b[22;4H')
  assert.equal(cursorPosition(23, 79, 24, 80), '\x1b[24;80H')
  assert.equal(cursorPosition(99, 99, 24, 80), '\x1b[24;80H')
})

test('inputCursorPosition parks the native cursor on the visible input caret', () => {
  assert.deepEqual(inputCursorPosition({
    headerRows: 1,
    messageRows: 17,
    activityRows: 1,
    errorRows: 0,
    panelRows: 0,
    hintRows: 1,
    inputBorderTopRows: 1,
    inputPaddingLeftColumns: 1,
    inputGutterColumns: 2,
    inputCursorLine: 0,
    inputCursorColumn: 2,
  }), { row: 21, column: 5 })
})

test('inputCursorPosition ignores panel rows below the composer (Codex popup under input)', () => {
  // panelRows counts only rows above the input frame. Popup-below-composer → pass 0.
  const withoutPanel = inputCursorPosition({
    headerRows: 1,
    messageRows: 12,
    activityRows: 1,
    errorRows: 0,
    panelRows: 0,
    hintRows: 1,
    inputBorderTopRows: 1,
    inputPaddingLeftColumns: 1,
    inputGutterColumns: 2,
    inputCursorLine: 0,
    inputCursorColumn: 0,
  })
  const withPanelRowsPassedAsAbove = inputCursorPosition({
    headerRows: 1,
    messageRows: 12,
    activityRows: 1,
    errorRows: 0,
    panelRows: 6,
    hintRows: 1,
    inputBorderTopRows: 1,
    inputPaddingLeftColumns: 1,
    inputGutterColumns: 2,
    inputCursorLine: 0,
    inputCursorColumn: 0,
  })
  // When callers correctly pass panelRows=0 for below-input panels, cursor stays put.
  assert.equal(withoutPanel.row, 1 + 12 + 1 + 0 + 0 + 1 + 1 + 0)
  // Legacy above-input path still accounts for panelRows so formula remains composable.
  assert.equal(withPanelRowsPassedAsAbove.row, withoutPanel.row + 6)
})
