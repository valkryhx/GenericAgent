import test from 'node:test'
import assert from 'node:assert/strict'
import { modelPanelRows, moveModelSelection, panelFromModelStatus, shouldApplyModelStatus } from './modelPanel.js'

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

test('modelPanelRows budgets title, model rows, and footer', () => {
  const panel = panelFromModelStatus({
    type: 'model_status',
    models: Array.from({ length: 9 }, (_, index) => ({
      index,
      name: `NativeOAISession/model-${index}`,
      current: index === 3,
    })),
  })

  assert.equal(modelPanelRows(panel), 11)
})
