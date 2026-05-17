import test from 'node:test'
import assert from 'node:assert/strict'
import { inputDivider, inputPrompt } from './promptChrome.js'

test('inputDivider returns a Claude-style horizontal rule', () => {
  assert.equal(inputDivider(8), '────────')
})

test('inputPrompt uses a Claude-style greater-than marker', () => {
  assert.equal(inputPrompt('hello'), '> hello')
  assert.equal(inputPrompt(''), '> ')
})
