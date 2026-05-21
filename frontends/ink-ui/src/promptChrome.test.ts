import test from 'node:test'
import assert from 'node:assert/strict'
import { inputFrameBorderStyle, inputPrompt } from './promptChrome.js'

test('inputPrompt uses a Claude-style greater-than marker', () => {
  assert.equal(inputPrompt('hello'), '> hello')
  assert.equal(inputPrompt(''), '> ')
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
