import test from 'node:test'
import assert from 'node:assert/strict'
import { handleSelectorInput, newestResumeSessions, rewindOptions, visibleSelectorRows } from './selectors.js'
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

test('visibleSelectorRows keeps the selected resume row visible', () => {
  const selector = {
    mode: 'resume' as const,
    selected: 7,
    sessions: Array.from({ length: 10 }, (_, index) => ({
      id: `s${index}`,
      mtime: index,
      preview: `session ${index}`,
      rounds: index + 1,
    })),
  }

  const rows = visibleSelectorRows(selector, 5)

  assert.deepEqual(rows.map(row => row.index), [5, 6, 7, 8, 9])
  assert.equal(rows.find(row => row.selected)?.index, 7)
})

test('newestResumeSessions sorts sessions by last activity descending', () => {
  const sessions = [
    { id: 'old', mtime: 10, preview: 'old', rounds: 1 },
    { id: 'new', mtime: 30, preview: 'new', rounds: 1 },
    { id: 'middle', mtime: 20, preview: 'middle', rounds: 1 },
  ]

  assert.deepEqual(newestResumeSessions(sessions).map(session => session.id), ['new', 'middle', 'old'])
})
