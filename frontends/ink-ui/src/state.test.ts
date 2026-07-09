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

test('applyBridgeEvent stores workflow detail draft and progress payloads', () => {
  let state = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
  state = applyBridgeEvent(state, {
    type: 'workflow_detail',
    run: { runId: 'wf_progress', sessionId: 's1', status: 'succeeded' },
    script: 'return 1',
    events: [],
    draft: {
      taskText: '规划 UI',
      classification: { taskType: 'planning' },
      plan: { meta: { name: 'planned-ui' }, phases: [] },
      validation: { ok: true, issues: [] },
      context: { plannerMode: 'prompt_guided' },
    },
    progress: {
      runId: 'wf_progress',
      sessionId: 's1',
      status: 'succeeded',
      workflowProgress: [
        {
          type: 'workflow_agent',
          index: 1,
          agentId: 'agent_1',
          jobId: 'agent_1',
          label: 'planner',
          phase: 'Plan',
          phaseTitle: 'Plan',
          state: 'succeeded',
          toolCalls: ['Read'],
          tokenUsage: { totalTokens: 12 },
          promptPreview: 'prompt',
          resultPreview: 'done',
        },
      ],
    },
  })

  assert.equal(state.workflowDetails.wf_progress.draft?.taskText, '规划 UI')
  assert.equal(state.workflowDetails.wf_progress.draft?.plan.meta?.name, 'planned-ui')
  assert.equal(state.workflowDetails.wf_progress.progress?.workflowProgress[0].label, 'planner')
  assert.equal(state.workflowDetails.wf_progress.progress?.workflowProgress[0].tokenUsage?.totalTokens, 12)
})

test('applyBridgeEvent stores live workflow progress without requiring detail first', () => {
  let state = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
  state = applyBridgeEvent(state, {
    type: 'workflow_run',
    run: { runId: 'wf_live_progress', sessionId: 's1', status: 'running', metadata: { workflowName: 'live-progress' } },
  })
  state = applyBridgeEvent(state, {
    type: 'workflow_progress',
    progress: {
      runId: 'wf_live_progress',
      sessionId: 's1',
      status: 'running',
      workflowProgress: [
        { jobId: 'agent_1', label: 'planner', state: 'succeeded', tokenUsage: { totalTokens: 1000 } },
        { jobId: 'agent_2', label: 'implementation', state: 'running', lastToolName: 'Edit', tokenUsage: { totalTokens: 2500 } },
      ],
    },
  })

  assert.equal(state.workflowDetails.wf_live_progress.run.runId, 'wf_live_progress')
  assert.equal(state.workflowDetails.wf_live_progress.script, '')
  assert.equal(state.workflowDetails.wf_live_progress.events.length, 0)
  assert.equal(state.workflowDetails.wf_live_progress.progress?.workflowProgress[1].label, 'implementation')
  assert.equal(state.workflows[0].status, 'running')
})

test('applyBridgeEvent merges live workflow progress into existing detail payload', () => {
  let state = applyBridgeEvent(initialState, {
    type: 'workflow_detail',
    run: { runId: 'wf_existing', sessionId: 's1', status: 'running' },
    script: 'return 1',
    events: [{ type: 'workflow_started', runId: 'wf_existing', sequence: 1, payload: {} }],
  })
  state = applyBridgeEvent(state, {
    type: 'workflow_progress',
    progress: {
      runId: 'wf_existing',
      sessionId: 's1',
      status: 'running',
      workflowProgress: [{ jobId: 'agent_1', label: 'planner', state: 'succeeded' }],
    },
  })

  assert.equal(state.workflowDetails.wf_existing.script, 'return 1')
  assert.equal(state.workflowDetails.wf_existing.events.length, 1)
  assert.equal(state.workflowDetails.wf_existing.progress?.workflowProgress[0].state, 'succeeded')
})

test('applyBridgeEvent ignores workflow progress when run is not known yet', () => {
  const state = applyBridgeEvent(initialState, {
    type: 'workflow_progress',
    progress: {
      runId: 'wf_unknown',
      sessionId: 's1',
      status: 'running',
      workflowProgress: [{ jobId: 'agent_1', label: 'planner', state: 'running' }],
    },
  })

  assert.equal(state.workflowDetails.wf_unknown, undefined)
  assert.equal(state.workflows.length, 0)
})

test('applyBridgeEvent updates workflow run status from live workflow progress', () => {
  let state = applyBridgeEvent(initialState, {
    type: 'workflow_run',
    run: { runId: 'wf_status_from_progress', sessionId: 's1', status: 'running', metadata: { workflowName: 'status-progress' } },
  })
  state = applyBridgeEvent(state, {
    type: 'workflow_progress',
    progress: {
      runId: 'wf_status_from_progress',
      sessionId: 's1',
      status: 'failed',
      workflowProgress: [{ jobId: 'agent_1', label: 'planner', state: 'failed' }],
    },
  })

  assert.equal(state.workflows[0].status, 'failed')
  assert.equal(state.workflowDetails.wf_status_from_progress.run.status, 'failed')
})

test('applyBridgeEvent keeps workflow_run payload authoritative after live progress', () => {
  let state = applyBridgeEvent(initialState, {
    type: 'workflow_run',
    run: { runId: 'wf_run_after_progress', sessionId: 's1', status: 'running', metadata: { workflowName: 'run-after-progress' } },
  })
  state = applyBridgeEvent(state, {
    type: 'workflow_progress',
    progress: {
      runId: 'wf_run_after_progress',
      sessionId: 's1',
      status: 'running',
      workflowProgress: [{ jobId: 'agent_1', label: 'planner', state: 'running' }],
    },
  })
  state = applyBridgeEvent(state, {
    type: 'workflow_run',
    run: { runId: 'wf_run_after_progress', sessionId: 's1', status: 'succeeded', metadata: { workflowName: 'run-after-progress' }, resultRef: 'final-result.json' },
  })

  assert.equal(state.workflows[0].status, 'succeeded')
  assert.equal(state.workflowDetails.wf_run_after_progress.run.status, 'succeeded')
  assert.equal(state.workflowDetails.wf_run_after_progress.run.resultRef, 'final-result.json')
  assert.equal(state.workflowDetails.wf_run_after_progress.progress?.workflowProgress[0].state, 'running')
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
