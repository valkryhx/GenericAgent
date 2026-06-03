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

test('applyBridgeEvent appends local command transcript messages without task ids', () => {
  let state = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
  state = applyBridgeEvent(state, { type: 'local_command_input', text: '/help' })
  state = applyBridgeEvent(state, { type: 'local_command_output', text: 'Help dialog dismissed' })

  assert.deepEqual(state.messages, [
    { id: 'lc-in-0', role: 'system', text: '/help', done: true, localCommand: 'input' },
    { id: 'lc-out-1', role: 'system', text: 'Help dialog dismissed', done: true, localCommand: 'output' },
  ])
  assert.equal(state.messages.some(message => message.taskId !== undefined), false)
})

test('applyBridgeEvent tracks compact activity while running and clears it when idle', () => {
  let state = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
  state = applyBridgeEvent(state, { type: 'status', status: 'running' })
  state = applyBridgeEvent(state, { type: 'activity', label: 'Compacting conversation' })

  assert.equal(state.status, 'running')
  assert.equal(state.activityLabel, 'Compacting conversation')

  state = applyBridgeEvent(state, { type: 'status', status: 'idle' })

  assert.equal(state.status, 'idle')
  assert.equal(state.activityLabel, null)
})

test('applyBridgeEvent keeps completed token usage when idle and clears stale usage on the next run', () => {
  let state = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
  state = applyBridgeEvent(state, { type: 'status', status: 'running' })
  state = applyBridgeEvent(state, { type: 'token_usage', taskId: 1, inputTokens: 11, outputTokens: 17, totalTokens: 28 })

  assert.deepEqual(state.tokenUsage, { inputTokens: 11, outputTokens: 17, totalTokens: 28 })

  state = applyBridgeEvent(state, { type: 'status', status: 'idle' })

  assert.deepEqual(state.tokenUsage, { inputTokens: 11, outputTokens: 17, totalTokens: 28 })

  state = applyBridgeEvent(state, { type: 'status', status: 'running' })

  assert.equal(state.tokenUsage, null)
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

test('applyBridgeEvent compact replacement keeps only compact result rows', () => {
  let state = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
  state = applyBridgeEvent(state, { type: 'local_command_input', text: '/compact' })
  state = applyBridgeEvent(state, { type: 'local_command_output', text: 'Compacted 8 messages into summary context.' })
  state = applyBridgeEvent(state, {
    type: 'history_replace',
    messages: [
      { role: 'system', text: 'Compacted 8 messages into summary context.' },
    ],
  })

  assert.deepEqual(state.messages.map(message => message.text), [
    'Compacted 8 messages into summary context.',
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


test('applyBridgeEvent tracks workflow run events and final results', () => {
  let state = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
  state = applyBridgeEvent(state, {
    type: 'workflow_draft',
    run: { runId: 'wf_1', sessionId: 's1', status: 'awaiting_approval' },
  })
  state = applyBridgeEvent(state, {
    type: 'workflow_event',
    event: { type: 'workflow_approval_requested', runId: 'wf_1', sequence: 1, payload: {} },
  })
  state = applyBridgeEvent(state, {
    type: 'workflow_run',
    run: { runId: 'wf_1', sessionId: 's1', status: 'succeeded', resultRef: 'final-result.json' },
  })
  state = applyBridgeEvent(state, {
    type: 'workflow_final',
    runId: 'wf_1',
    result: { status: 'succeeded', result: { ok: true } },
  })

  assert.equal(state.workflows.length, 1)
  assert.equal(state.workflows[0].status, 'succeeded')
  assert.equal(state.workflowEvents[0].type, 'workflow_approval_requested')
  assert.deepEqual(state.workflowResults.wf_1, { status: 'succeeded', result: { ok: true } })
})

test('applyBridgeEvent stores workflow list and detail payloads', () => {
  let state = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
  state = applyBridgeEvent(state, {
    type: 'workflow_runs',
    runs: [{ runId: 'wf_1', sessionId: 's1', status: 'awaiting_approval' }],
  })
  state = applyBridgeEvent(state, {
    type: 'workflow_detail',
    run: { runId: 'wf_1', sessionId: 's1', status: 'awaiting_approval' },
    script: 'return 1',
    events: [{ type: 'workflow_approval_requested', runId: 'wf_1', sequence: 1, payload: {} }],
  })

  assert.equal(state.workflows.length, 1)
  assert.equal(state.workflowDetails.wf_1.script, 'return 1')
  assert.equal(state.workflowDetails.wf_1.events.length, 1)
})
