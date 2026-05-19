import test from 'node:test'
import assert from 'node:assert/strict'
import { statusPanelText, type FooterPanel } from './footerPanel.js'

test('statusPanelText formats frontend status without adding a transcript message', () => {
  const panel: FooterPanel = { type: 'status', text: statusPanelText('idle', 3) }

  assert.deepEqual(panel, { type: 'status', text: 'status=idle messages=3' })
})
