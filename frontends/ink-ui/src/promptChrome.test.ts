import test from 'node:test'
import assert from 'node:assert/strict'
import { inputDivider, inputPrompt } from './promptChrome.js'

test('inputDivider returns an ASCII horizontal rule', () => {
  assert.equal(inputDivider(8), '--------')
})

test('inputPrompt uses a Claude-style greater-than marker', () => {
  assert.equal(inputPrompt('hello'), '> hello')
  assert.equal(inputPrompt(''), '> ')
})
