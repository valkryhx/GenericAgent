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
  })
})
