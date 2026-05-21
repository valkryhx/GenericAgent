import test from 'node:test'
import assert from 'node:assert/strict'
import { inputDivider, inputPrompt } from './promptChrome.js'

test('inputDivider returns a Claude-style horizontal rule', () => {
  assert.equal(inputDivider(8), '────────')
})

test('inputDivider returns exactly the requested one-line width', () => {
  assert.equal(inputDivider(8), '────────')
  assert.equal(inputDivider(1), '─')
  assert.equal(inputDivider(0), '')
})

test('inputPrompt uses a Claude-style greater-than marker', () => {
  assert.equal(inputPrompt('hello'), '> hello')
  assert.equal(inputPrompt(''), '> ')
})
