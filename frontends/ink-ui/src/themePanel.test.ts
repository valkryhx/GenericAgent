import test from 'node:test'
import assert from 'node:assert/strict'
import { moveThemeSelection, themePanelRows } from './themePanel.js'

test('moveThemeSelection clamps inside available themes', () => {
  assert.equal(moveThemeSelection(0, -1), 0)
  assert.equal(moveThemeSelection(0, 1), 1)
  assert.equal(moveThemeSelection(1, 1), 1)
})

test('themePanelRows accounts for title, themes, and help row', () => {
  assert.equal(themePanelRows(), 4)
})
