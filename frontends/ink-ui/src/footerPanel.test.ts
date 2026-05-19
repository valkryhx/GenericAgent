import test from 'node:test'
import assert from 'node:assert/strict'
import { modelSwitchPanelText, statusPanelText, type FooterPanel } from './footerPanel.js'

test('statusPanelText formats frontend status without adding a transcript message', () => {
  const panel: FooterPanel = { type: 'status', text: statusPanelText('idle', 3) }

  assert.deepEqual(panel, { type: 'status', text: 'status=idle messages=3' })
})

test('modelSwitchPanelText formats model selection results for the footer', () => {
  const panel: FooterPanel = { type: 'model', text: modelSwitchPanelText('Set model to NativeOAISession/kimi-native') }

  assert.deepEqual(panel, { type: 'model', text: 'Set model to NativeOAISession/kimi-native' })
})
