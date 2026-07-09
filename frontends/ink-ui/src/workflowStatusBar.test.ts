import test from 'node:test'
import assert from 'node:assert/strict'
import { workflowStatusBarCommandForKey, workflowStatusBarFromState, workflowStatusBarRows } from './workflowStatusBar.js'
import type { AppState } from './state.js'

function stateWithWorkflows(overrides: Partial<AppState>): AppState {
  return {
    status: 'idle',
    activityLabel: null,
    tokenUsage: null,
    messages: [],
    error: null,
    workflows: [],
    workflowEvents: [],
    workflowDetails: {},
    workflowResults: {},
    ...overrides,
  }
}

test('workflowStatusBarFromState returns null when no live workflow exists', () => {
  const bar = workflowStatusBarFromState(stateWithWorkflows({
    workflows: [
      { runId: 'wf_done', sessionId: 'session', status: 'succeeded', jobs: [{ jobId: 'agent_1', status: 'succeeded' }] },
      { runId: 'wf_failed', sessionId: 'session', status: 'failed', jobs: [{ jobId: 'agent_1', status: 'failed' }] },
    ],
  }))

  assert.equal(bar, null)
})

test('workflowStatusBarFromState selects latest running workflow and summarizes progress tokens', () => {
  const bar = workflowStatusBarFromState(stateWithWorkflows({
    workflows: [
      { runId: 'wf_old', sessionId: 'session', status: 'running', metadata: { workflowName: 'old-workflow' }, jobs: [{ jobId: 'agent_old', status: 'running' }] },
      {
        runId: 'wf_live',
        sessionId: 'session',
        status: 'running',
        metadata: { workflowName: 'read-recent-git-commits' },
        jobs: [
          { jobId: 'agent_1', status: 'succeeded', metadata: { label: 'planner' } },
          { jobId: 'agent_2', status: 'running', metadata: { label: 'implementation' } },
        ],
      },
      { runId: 'wf_done', sessionId: 'session', status: 'succeeded', metadata: { workflowName: 'done-workflow' }, jobs: [{ jobId: 'agent_done', status: 'succeeded' }] },
    ],
    workflowDetails: {
      wf_live: {
        run: { runId: 'wf_live', sessionId: 'session', status: 'running' },
        script: 'return 1',
        events: [],
        draft: null,
        progress: {
          runId: 'wf_live',
          sessionId: 'session',
          status: 'running',
          workflowProgress: [
            { jobId: 'agent_1', label: 'planner', state: 'succeeded', tokenUsage: { totalTokens: 1200 } },
            { jobId: 'agent_2', label: 'implementation', state: 'running', tokenUsage: { totalTokens: 3400 }, lastToolName: 'Edit' },
          ],
        },
      },
    },
  }))

  assert.deepEqual(bar, {
    runId: 'wf_live',
    status: 'running',
    name: 'read-recent-git-commits',
    completedAgents: 1,
    totalAgents: 2,
    activeAgent: 'implementation',
    lastActivity: 'Edit',
    tokenText: '4.6k tok',
  })
})

test('workflowStatusBarFromState uses live workflow_progress without workflow_detail payload', () => {
  const bar = workflowStatusBarFromState(stateWithWorkflows({
    workflows: [
      { runId: 'wf_live_progress', sessionId: 'session', status: 'running', metadata: { workflowName: 'live-progress' }, jobs: [] },
    ],
    workflowDetails: {
      wf_live_progress: {
        run: { runId: 'wf_live_progress', sessionId: 'session', status: 'running', metadata: { workflowName: 'live-progress' } },
        script: '',
        events: [],
        progress: {
          runId: 'wf_live_progress',
          sessionId: 'session',
          status: 'running',
          workflowProgress: [
            { jobId: 'agent_1', label: 'planner', state: 'succeeded', tokenUsage: { totalTokens: 900 } },
            { jobId: 'agent_2', label: 'reviewer', state: 'running', lastToolName: 'Read', tokenUsage: { totalTokens: 1600 } },
          ],
        },
      },
    },
  }))

  assert.deepEqual(bar, {
    runId: 'wf_live_progress',
    status: 'running',
    name: 'live-progress',
    completedAgents: 1,
    totalAgents: 2,
    activeAgent: 'reviewer',
    lastActivity: 'Read',
    tokenText: '2.5k tok',
  })
})

test('workflowStatusBarRows formats running workflow controls and summary', () => {
  const bar = workflowStatusBarFromState(stateWithWorkflows({
    workflows: [{ runId: 'wf_live', sessionId: 'session', status: 'running', metadata: { workflowName: 'live-workflow' }, jobs: [{ jobId: 'a1', status: 'succeeded' }, { jobId: 'a2', status: 'running' }] }],
  }))

  assert.deepEqual(workflowStatusBarRows(bar!), [
    'Enter view · x stop',
    '› ◌ live-workflow  1/2 agents done',
  ])
})

test('workflowStatusBarFromState shows active label from jobs-only fallback', () => {
  const bar = workflowStatusBarFromState(stateWithWorkflows({
    workflows: [{
      runId: 'wf_jobs_only',
      sessionId: 'session',
      status: 'running',
      metadata: { workflowName: 'jobs-only-workflow' },
      jobs: [
        { jobId: 'agent_1', status: 'succeeded', metadata: { label: 'planner' } },
        { jobId: 'agent_2', status: 'running', metadata: { label: 'implementation' } },
      ],
    }],
  }))

  assert.deepEqual(bar, {
    runId: 'wf_jobs_only',
    status: 'running',
    name: 'jobs-only-workflow',
    completedAgents: 1,
    totalAgents: 2,
    activeAgent: 'implementation',
    lastActivity: undefined,
    tokenText: undefined,
  })
})

test('workflowStatusBarRows formats awaiting approval without stop shortcut', () => {
  const bar = workflowStatusBarFromState(stateWithWorkflows({
    workflows: [{ runId: 'wf_review', sessionId: 'session', status: 'awaiting_approval', metadata: { workflowName: 'review-workflow' }, jobs: [] }],
  }))

  assert.deepEqual(workflowStatusBarRows(bar!), [
    'Enter review',
    '› ◌ review-workflow  awaiting approval',
  ])
})

test('workflowStatusBarCommandForKey maps Enter and running-only x controls', () => {
  const running = workflowStatusBarFromState(stateWithWorkflows({
    workflows: [{ runId: 'wf_live', sessionId: 'session', status: 'running', jobs: [] }],
  }))!
  const awaiting = workflowStatusBarFromState(stateWithWorkflows({
    workflows: [{ runId: 'wf_review', sessionId: 'session', status: 'awaiting_approval', jobs: [] }],
  }))!

  assert.deepEqual(workflowStatusBarCommandForKey(running, { return: true }, ''), { type: 'workflow_detail', runId: 'wf_live' })
  assert.deepEqual(workflowStatusBarCommandForKey(running, {}, 'x'), { type: 'workflow_stop', runId: 'wf_live', reason: 'stopped from Ink UI status bar' })
  assert.equal(workflowStatusBarCommandForKey(running, { ctrl: true }, 'x'), null)
  assert.equal(workflowStatusBarCommandForKey(running, { meta: true }, 'x'), null)
  assert.equal(workflowStatusBarCommandForKey(running, { shift: true }, 'x'), null)
  assert.deepEqual(workflowStatusBarCommandForKey(awaiting, { return: true }, ''), { type: 'workflow_detail', runId: 'wf_review' })
  assert.equal(workflowStatusBarCommandForKey(awaiting, {}, 'x'), null)
})
