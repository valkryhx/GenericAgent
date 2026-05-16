import test from 'node:test'
import assert from 'node:assert/strict'
import { applyBridgeEvent, initialState } from './state.js'

test('applyBridgeEvent appends stream deltas and replaces final text', () => {
  let state = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
  state = applyBridgeEvent(state, { type: 'user', taskId: 1, text: 'hi' })
  state = applyBridgeEvent(state, { type: 'assistant_delta', taskId: 1, text: 'he' })
  state = applyBridgeEvent(state, { type: 'assistant_delta', taskId: 1, text: 'llo' })
  state = applyBridgeEvent(state, { type: 'assistant_done', taskId: 1, text: 'hello final' })

  assert.equal(state.status, 'idle')
  assert.equal(state.messages.length, 2)
  assert.deepEqual(state.messages[1], {
    id: 'a-1',
    role: 'assistant',
    text: 'hello final',
    done: true,
    taskId: 1,
  })
})

test('applyBridgeEvent appends system messages and clears display only', () => {
  let state = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
  state = applyBridgeEvent(state, { type: 'system', text: 'hello system' })
  state = applyBridgeEvent(state, { type: 'clear' })

  assert.equal(state.status, 'idle')
  assert.deepEqual(state.messages, [])
})

test('applyBridgeEvent replaces history after resume', () => {
  let state = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
  state = applyBridgeEvent(state, {
    type: 'history_replace',
    messages: [
      { role: 'user', text: 'old question' },
      { role: 'assistant', text: 'old answer' },
    ],
  })

  assert.deepEqual(state.messages, [
    { id: 'h-0', role: 'user', text: 'old question', done: true },
    { id: 'h-1', role: 'assistant', text: 'old answer', done: true },
  ])
})

test('applyBridgeEvent rewinds to before selected user task', () => {
  let state = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
  state = applyBridgeEvent(state, { type: 'user', taskId: 1, text: 'first' })
  state = applyBridgeEvent(state, { type: 'assistant_done', taskId: 1, text: 'answer' })
  state = applyBridgeEvent(state, { type: 'user', taskId: 2, text: 'second' })
  state = applyBridgeEvent(state, { type: 'assistant_done', taskId: 2, text: 'answer 2' })
  state = applyBridgeEvent(state, { type: 'rewind_done', taskId: 2, text: 'second' })

  assert.deepEqual(state.messages.map(message => message.id), ['u-1', 'a-1'])
})
