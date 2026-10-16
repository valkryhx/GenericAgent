import test from 'node:test'
import assert from 'node:assert/strict'
import { getInkTheme, INK_THEME_NAMES } from './theme.js'

test('default theme preserves existing Ink colors', () => {
  const theme = getInkTheme('default')

  assert.equal(theme.muted, 'gray')
  assert.equal(theme.accent, 'cyan')
  assert.equal(theme.success, 'green')
  assert.equal(theme.warning, 'yellow')
  assert.equal(theme.error, 'red')
  assert.equal(theme.border, 'gray')
  assert.equal(theme.userText, 'black')
  assert.equal(theme.userBackground, '#d7d7d7')
})

test('lightmode theme provides light-terminal friendly colors', () => {
  const theme = getInkTheme('lightmode')

  assert.equal(theme.muted, '#6b7280')
  assert.equal(theme.accent, '#2563eb')
  assert.equal(theme.success, '#047857')
  assert.equal(theme.warning, '#b45309')
  assert.equal(theme.error, '#b91c1c')
  assert.equal(theme.border, '#9ca3af')
  assert.equal(theme.userText, '#111827')
  assert.equal(theme.userBackground, '#e5e7eb')
  assert.equal(theme.code, '#7c3aed')
  assert.equal(theme.link, '#2563eb')
  assert.equal(theme.linkUrl, '#64748b')
})

test('theme names expose default before lightmode for picker order', () => {
  assert.deepEqual(INK_THEME_NAMES, ['default', 'lightmode'])
})
