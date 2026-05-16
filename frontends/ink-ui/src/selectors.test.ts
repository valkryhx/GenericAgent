import test from 'node:test'
import assert from 'node:assert/strict'
import { handleSelectorInput, rewindOptions } from './selectors.js'
import type { ChatMessage } from './protocol.js'

test('rewindOptions returns user messages with task ids', () => {
  const messages: ChatMessage[] = [
    { id: 'u-1', role: 'user', text: 'first', done: true, taskId: 1 },
    { id: 'a-1', role: 'assistant', text: 'answer', done: true, taskId: 1 },
    { id: 'u-x', role: 'user', text: 'restored', done: true },
  ]

  assert.deepEqual(rewindOptions(messages), [{ taskId: 1, text: 'first' }])
})

test('handleSelectorInput selects resume sessions', () => {
  const decision = handleSelectorInput({
    mode: 'resume',
    selected: 0,
    sessions: [{ id: 's1', mtime: 1, preview: 'hello', rounds: 2 }],
  }, { return: true })

  assert.deepEqual(decision, {
    selector: null,
    command: { type: 'resume_session', id: 's1' },
  })
})

test('handleSelectorInput selects rewind target and returns text for resubmit', () => {
  const decision = handleSelectorInput({
    mode: 'rewind',
    selected: 0,
    options: [{ taskId: 2, text: 'redo this' }],
  }, { return: true })

  assert.deepEqual(decision, {
    selector: null,
    command: { type: 'rewind', taskId: 2 },
    input: 'redo this',
  })
})

test('handleSelectorInput navigates and cancels', () => {
  const selector = {
    mode: 'rewind' as const,
    selected: 1,
    options: [{ taskId: 1, text: 'a' }, { taskId: 2, text: 'b' }],
  }

  assert.equal(handleSelectorInput(selector, { upArrow: true }).selector?.selected, 0)
  assert.equal(handleSelectorInput(selector, { downArrow: true }).selector?.selected, 1)
  assert.deepEqual(handleSelectorInput(selector, { escape: true }), { selector: null })
})
