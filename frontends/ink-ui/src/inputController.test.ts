import test from 'node:test'
import assert from 'node:assert/strict'
import { createPasteStore } from './paste.js'
import { handleInput } from './inputController.js'

test('handleInput submits expanded text when idle', () => {
  const store = createPasteStore()

  const decision = handleInput('hello', '', { return: true }, 'idle', store)

  assert.deepEqual(decision, { value: '', command: { type: 'submit', text: 'hello' } })
})

test('handleInput keeps text when Enter is pressed while running', () => {
  const store = createPasteStore()

  const decision = handleInput('next prompt', '', { return: true }, 'running', store)

  assert.deepEqual(decision, { value: 'next prompt' })
})

test('handleInput sends stop for slash stop and Escape', () => {
  const store = createPasteStore()

  assert.deepEqual(handleInput('/stop', '', { return: true }, 'running', store), {
    value: '',
    command: { type: 'stop' },
  })
  assert.deepEqual(handleInput('draft', '', { escape: true }, 'running', store), {
    value: 'draft',
    command: { type: 'stop' },
  })
})

test('handleInput opens Claude-style local selectors for resume and rewind', () => {
  const store = createPasteStore()

  assert.deepEqual(handleInput('/resume', '', { return: true }, 'idle', store), {
    value: '',
    action: { type: 'open_resume' },
  })
  assert.deepEqual(handleInput('/continue', '', { return: true }, 'idle', store), {
    value: '',
    action: { type: 'open_resume' },
  })
  assert.deepEqual(handleInput('/rewind', '', { return: true }, 'idle', store), {
    value: '',
    action: { type: 'open_rewind' },
  })
  assert.deepEqual(handleInput('/checkpoint', '', { return: true }, 'idle', store), {
    value: '',
    action: { type: 'open_rewind' },
  })
})

test('handleInput sends indexed resume commands without opening a selector', () => {
  const store = createPasteStore()

  assert.deepEqual(handleInput('/resume 2', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'resume_session_index', index: 2 },
  })
  assert.deepEqual(handleInput('/continue 3', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'resume_session_index', index: 3 },
  })
})

test('handleInput folds multiline pasted text and expands on submit', () => {
  const store = createPasteStore()
  const pasted = handleInput('', 'a\nb\nc', {}, 'idle', store)

  assert.equal(pasted.value, '[Copied text #1 +2 lines]')
  assert.deepEqual(handleInput(`${pasted.value} 请检查`, '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'submit', text: 'a\nb\nc 请检查' },
  })
})

test('handleInput turns Ctrl+C into shutdown and exit', () => {
  const store = createPasteStore()

  assert.deepEqual(handleInput('draft', 'c', { ctrl: true }, 'idle', store), {
    value: 'draft',
    command: { type: 'shutdown' },
    exit: true,
  })
})
