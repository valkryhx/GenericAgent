import test from 'node:test'
import assert from 'node:assert/strict'
import {
  modelPanelRows,
  moveModelSelection,
  moveReasoningSelection,
  openReasoningPanel,
  panelFromModelStatus,
  shouldApplyModelStatus,
} from './modelPanel.js'

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
  assert.equal(panel.reasoning, null)
})

test('moveModelSelection wraps at both ends', () => {
  assert.equal(moveModelSelection(0, -1, 2), 1)
  assert.equal(moveModelSelection(0, 1, 2), 1)
  assert.equal(moveModelSelection(1, 1, 2), 0)
  assert.equal(moveModelSelection(0, -3, 3), 0)
  assert.equal(moveModelSelection(2, 4, 3), 0)
  assert.equal(moveModelSelection(0, 1, 0), 0)
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

test('reasoning panel follows the selected model and highlights its current effort', () => {
  const panel = panelFromModelStatus({
    type: 'model_status',
    models: [{
      index: 0,
      name: 'gpt-6-luna/gpt-6-luna',
      current: true,
      reasoningEfforts: ['none', 'medium', 'ultra'],
      reasoningEffortKnown: true,
      defaultReasoningEffort: 'medium',
      reasoningEffort: 'ultra',
    }],
  })
  const next = openReasoningPanel(panel, panel.models[0])
  assert.equal(next.reasoning?.selected, 2)
  assert.equal(modelPanelRows(next), 5)
})

test('reasoning selection stops at both boundaries', () => {
  assert.equal(moveReasoningSelection(0, -1, 3), 0)
  assert.equal(moveReasoningSelection(2, 1, 3), 2)
  assert.equal(moveReasoningSelection(0, 1, 3), 1)
  assert.equal(moveReasoningSelection(0, 1, 0), 0)
})
