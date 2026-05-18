import test from 'node:test'
import assert from 'node:assert/strict'
import { moveModelSelection, panelFromModelStatus, shouldApplyModelStatus } from './modelPanel.js'

test('panelFromModelStatus selects current model', () => {
  const panel = panelFromModelStatus({
    type: 'model_status',
    models: [
      { index: 0, name: 'NativeOAISession/gpt-native', current: false },
      { index: 1, name: 'NativeOAISession/kimi-native', current: true },
    ],
  })

  assert.equal(panel.selected, 1)
  assert.equal(panel.models[1].name, 'NativeOAISession/kimi-native')
})

test('moveModelSelection clamps to bounds', () => {
  assert.equal(moveModelSelection(0, -1, 2), 0)
  assert.equal(moveModelSelection(0, 1, 2), 1)
  assert.equal(moveModelSelection(1, 1, 2), 1)
})

test('shouldApplyModelStatus only opens panel when requested or already open', () => {
  assert.equal(shouldApplyModelStatus(false, false), false)
  assert.equal(shouldApplyModelStatus(true, false), true)
  assert.equal(shouldApplyModelStatus(false, true), true)
})
