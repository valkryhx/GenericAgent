import test from 'node:test'
import assert from 'node:assert/strict'
import { createInputHistory, nextInput, previousInput, recordInput } from './inputHistory.js'

test('recordInput ignores blanks and consecutive duplicates', () => {
  let history = createInputHistory()

  history = recordInput(history, '   ')
  history = recordInput(history, 'hello')
  history = recordInput(history, 'hello')
  history = recordInput(history, 'world')

  assert.deepEqual(history.entries, ['hello', 'world'])
  assert.equal(history.index, null)
  assert.equal(history.draft, '')
})

test('previousInput and nextInput browse entries while preserving the draft', () => {
  let history = createInputHistory()
  history = recordInput(history, 'first')
  history = recordInput(history, 'second')

  let result = previousInput(history, 'draft')
  assert.equal(result.value, 'second')
  history = result.history

  result = previousInput(history, result.value)
  assert.equal(result.value, 'first')
  history = result.history

  result = previousInput(history, result.value)
  assert.equal(result.value, 'first')
  history = result.history

  result = nextInput(history, result.value)
  assert.equal(result.value, 'second')
  history = result.history

  result = nextInput(history, result.value)
  assert.equal(result.value, 'draft')
  history = result.history

  result = nextInput(history, result.value)
  assert.equal(result.value, 'draft')
  assert.equal(result.history.index, null)
  assert.equal(result.history.draft, '')
})
