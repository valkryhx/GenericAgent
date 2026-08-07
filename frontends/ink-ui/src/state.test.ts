import test from 'node:test'
import assert from 'node:assert/strict'
import { applyBridgeEvent, initialState } from './state.js'

test('applyBridgeEvent appends stream deltas and replaces final text', () => {
  let state = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
  state = applyBridgeEvent(state, { type: 'user', taskId: 1, text: 'hi' })
  assert.equal(state.messages[0]?.done, false)
  state = applyBridgeEvent(state, { type: 'assistant_delta', taskId: 1, text: 'he' })
  state = applyBridgeEvent(state, { type: 'assistant_delta', taskId: 1, text: 'llo' })
  state = applyBridgeEvent(state, { type: 'assistant_done', taskId: 1, text: 'hello final' })

  assert.equal(state.status, 'idle')
  assert.equal(state.messages.length, 2)
  assert.equal(state.messages[0]?.done, true)
  assert.deepEqual(state.messages[1], {
    id: 'a-1',
    role: 'assistant',
    text: 'hello final',
    done: true,
    taskId: 1,
  })
})

test('applyBridgeEvent commits overflow stream lines and finalizes only the remaining tail', () => {
  let state = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
  state = applyBridgeEvent(state, { type: 'user', taskId: 2, text: 'q' })
  const many = Array.from({ length: 12 }, (_, i) => `S${i}`).join('\n')
  state = applyBridgeEvent(state, { type: 'assistant_delta', taskId: 2, text: many })

  // First assistant content finalizes the open user so Static order is user → assistant commits.
  assert.equal(state.messages.find(m => m.id === 'u-2')?.done, true)
  const commits = state.messages.filter(m => m.id.startsWith('a-2-c'))
  const live = state.messages.find(m => m.id === 'a-2')
  assert.ok(commits.length >= 1)
  assert.ok(commits.every(m => m.done))
  assert.equal(live?.done, false)
  assert.ok((live?.text.split('\n').length ?? 99) <= 8)
  // Chronological: user before any assistant commit segment in messages array.
  const userIdx = state.messages.findIndex(m => m.id === 'u-2')
  const firstCommitIdx = state.messages.findIndex(m => m.id.startsWith('a-2-c'))
  assert.ok(userIdx >= 0 && firstCommitIdx > userIdx)

  const full = many
  state = applyBridgeEvent(state, { type: 'assistant_done', taskId: 2, text: full })
  const finalLive = state.messages.find(m => m.id === 'a-2')
  assert.equal(finalLive?.done, true)
  // Final body must not re-include already committed lines as a second full dump.
  const allAssistantText = state.messages.filter(m => m.role === 'assistant').map(m => m.text).join('\n')
  for (const line of full.split('\n')) {
    assert.equal(allAssistantText.split('\n').filter(l => l === line).length, 1, `line duplicated: ${line}`)
  }
  assert.equal(state.messages.find(m => m.id === 'u-2')?.done, true)
})


test('applyBridgeEvent assistant_done with summary newline rewrite does not duplicate LLM Running turns', () => {
  let state = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
  state = applyBridgeEvent(state, { type: 'user', taskId: 11, text: '现在几点' })
  // Simulate long incremental stream (tool + two turns) so stream-commit creates a-11-c* segments.
  const parts = [
    '**LLM Running (Turn 1) ...**\n\n',
    '<summary>需要当前时间，读取系统钟</summary>\n',
    '🛠️ Tool: `code_run`  📥 args:\n````text\n{"script":"print(1)"}\n````\n',
    '`````\n[Action] Running python\n[Status] Exit Code: 0\n[Stdout]\n2026-07-14 15:28:09 Tuesday\n`````\n\n',
    '**LLM Running (Turn 2) ...**\n\n',
    '<summary>系统时间为15:28</summary>\n\n',
    '现在是 **2026年7月14日 15:28**。\n',
  ]
  for (const part of parts) {
    state = applyBridgeEvent(state, { type: 'assistant_delta', taskId: 11, text: part })
  }
  const streamed = state.messages.filter(m => m.role === 'assistant').map(m => m.text).join('\n')
  assert.ok(state.messages.some(m => m.id.startsWith('a-11-c')), 'expected stream commits for long turn')
  // agentmain only injects extra newline after </summary> on the final done payload
  const done = streamed.replaceAll('</summary>', '</summary>\n\n')
  state = applyBridgeEvent(state, { type: 'assistant_done', taskId: 11, text: done })
  const all = state.messages.filter(m => m.role === 'assistant').map(m => m.text).join('\n')
  assert.equal(all.split('LLM Running (Turn 1)').length - 1, 1, `Turn 1 duplicated in:\n${all}`)
  assert.equal(all.split('LLM Running (Turn 2)').length - 1, 1, `Turn 2 duplicated in:\n${all}`)
  assert.match(all, /15:28/)
  assert.doesNotMatch(all, /^:28\*\*/m)
})

test('applyBridgeEvent finalizes open user on first assistant_delta so user is not lost mid-stream', () => {
  let state = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
  state = applyBridgeEvent(state, { type: 'user', taskId: 4, text: '现在几点了' })
  assert.equal(state.messages.find(m => m.id === 'u-4')?.done, false)
  state = applyBridgeEvent(state, { type: 'assistant_delta', taskId: 4, text: '稍等' })
  assert.equal(state.messages.find(m => m.id === 'u-4')?.done, true)
  const split = state.messages
  assert.ok(split.find(m => m.id === 'u-4')!.done)
  assert.equal(split.find(m => m.id === 'a-4')?.done, false)
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
  // 成功路径只发 history_replace（不再先 local_command_output），避免 Static 重复。
  state = applyBridgeEvent(state, { type: 'local_command_input', text: '/compact' })
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

test('applyBridgeEvent stores process and workflow common agent snapshots', () => {
  const state = applyBridgeEvent(initialState, {
    type: 'agent_snapshot',
    snapshot: {
      records: [
        {
          executionId: 'process-agent:run:/root/worker',
          engine: 'process',
          recordKind: 'process_agent',
          status: 'running',
          capabilities: { actions: ['read'], features: [] },
        },
        {
          executionId: 'workflow-run:wf_1',
          engine: 'workflow',
          recordKind: 'workflow_run',
          status: 'running',
          runId: 'wf_1',
          capabilities: { actions: ['read'], features: [] },
        },
      ],
      cursors: { process: 4, 'workflow:wf_1': 2 },
      errors: {},
    },
  } as any)

  assert.deepEqual((state as any).agents.map((record: any) => record.engine), ['process', 'workflow'])
  assert.deepEqual((state as any).agentCursors, { process: 4, 'workflow:wf_1': 2 })
})

test('applyBridgeEvent appends common agent events once by eventId', () => {
  const commonEvent = {
    type: 'agent_event',
    event: {
      eventId: 'workflow:wf_1:4',
      engine: 'workflow',
      executionId: 'workflow-child:wf_1:agent_1',
      sourceCursor: 'workflow:wf_1',
      sourceSequence: 4,
      type: 'agent_completed',
      payload: { resultRef: 'agents/agent_1/result.json' },
    },
  } as any
  let state = applyBridgeEvent(initialState, commonEvent)
  state = applyBridgeEvent(state, commonEvent)

  assert.equal((state as any).agentEvents.length, 1)
  assert.equal((state as any).agentEvents[0].eventId, 'workflow:wf_1:4')
})

test('common agent events do not replace workflow detail or progress state', () => {
  let state = applyBridgeEvent(initialState, {
    type: 'workflow_detail',
    run: { runId: 'wf_detail_common', sessionId: 's1', status: 'running' },
    script: 'return 1',
    events: [{ type: 'workflow_started', runId: 'wf_detail_common', sequence: 1, payload: {} }],
    progress: {
      runId: 'wf_detail_common',
      status: 'running',
      workflowProgress: [{ jobId: 'agent_1', state: 'running', label: 'worker' }],
    },
  })
  state = applyBridgeEvent(state, {
    type: 'agent_event',
    event: {
      eventId: 'workflow:wf_detail_common:2',
      engine: 'workflow',
      executionId: 'workflow-child:wf_detail_common:agent_1',
      sourceCursor: 'workflow:wf_detail_common',
      sourceSequence: 2,
      type: 'agent_completed',
    },
  } as any)

  assert.equal(state.workflowDetails.wf_detail_common.script, 'return 1')
  assert.equal(state.workflowDetails.wf_detail_common.progress?.workflowProgress[0]?.label, 'worker')
})
