import test from 'node:test'
import assert from 'node:assert/strict'
import { footerPanelRows, modelSwitchPanelText, statusPanelText, type FooterPanel } from './footerPanel.js'

test('statusPanelText formats frontend status without adding a transcript message', () => {
  const panel: FooterPanel = { type: 'status', text: statusPanelText('idle', 3) }

  assert.deepEqual(panel, { type: 'status', text: 'status=idle messages=3' })
})

test('modelSwitchPanelText formats model selection results for the footer', () => {
  const panel: FooterPanel = { type: 'model', text: modelSwitchPanelText('Set model to NativeOAISession/kimi-native') }

  assert.deepEqual(panel, { type: 'model', text: 'Set model to NativeOAISession/kimi-native' })
})

test('footerPanelRows reserves zero rows for status (renders inline in hint row)', () => {
  assert.equal(footerPanelRows({ type: 'status', text: statusPanelText('idle', 3) }), 0)
})

test('footerPanelRows counts help text lines plus the trailing Esc close row', () => {
  // FooterPanelView renders one Text per text.split('\n') line plus an "Esc close"
  // row. A 3-line panel therefore occupies 4 rows in the panel section; keeping
  // panelRows in sync prevents the help panel overlapping the message viewport.
  const panel: FooterPanel = { type: 'help', text: 'a\nb\nc' }

  assert.equal(footerPanelRows(panel), 4)
})

test('footerPanelRows counts a single-line model confirmation plus Esc close', () => {
  assert.equal(footerPanelRows({ type: 'model', text: 'Set model to X' }), 2)
})
