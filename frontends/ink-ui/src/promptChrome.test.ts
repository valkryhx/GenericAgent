import test from 'node:test'
import assert from 'node:assert/strict'
import { inputFrameBorderStyle, inputPrompt, inputPromptLines, inputVisibleRowCount } from './promptChrome.js'

test('inputPrompt uses a Claude-style greater-than marker', () => {
  assert.equal(inputPrompt('hello'), '> hello')
  assert.equal(inputPrompt(''), '> ')
})

test('inputPromptLines keeps multiline input and trailing blank cursor rows visible', () => {
  assert.deepEqual(inputPromptLines('hello\n'), ['> hello', '  '])
  assert.deepEqual(inputPromptLines('hello\nworld'), ['> hello', '  world'])
})

test('inputVisibleRowCount grows for multiline input with a cap', () => {
  assert.equal(inputVisibleRowCount(''), 1)
  assert.equal(inputVisibleRowCount('hello\n'), 2)
  assert.equal(inputVisibleRowCount('1\n2\n3\n4\n5\n6\n7', 4), 4)
})

test('inputFrameBorderStyle draws only horizontal borders with no corner fragments', () => {
  assert.deepEqual(inputFrameBorderStyle, {
    topLeft: '',
    top: '─',
    topRight: '',
    right: '',
    bottomRight: '',
    bottom: '─',
    bottomLeft: '',
    left: '',
  })
})
