import test from 'node:test'
import assert from 'node:assert/strict'
import { workflowPanelFromDetail, workflowRawDetailPanelFromDetail, workflowPanelRows, workflowPanelCommandForKey, workflowListRows, workflowListCommandForKey, workflowOverviewFromDetail, workflowOverviewRows } from './workflowPanel.js'
import type { WorkflowRun } from './protocol.js'

const workflowRuns: WorkflowRun[] = [
  {
    runId: 'wf_done_1',
    sessionId: 'session',
    status: 'succeeded',
    metadata: { workflowName: 'prompt-guided-planner-real-e2e-design' },
    jobs: [{ jobId: 'agent_1', status: 'succeeded' }, { jobId: 'agent_2', status: 'succeeded' }, { jobId: 'agent_3', status: 'succeeded' }, { jobId: 'agent_4', status: 'succeeded' }],
  },
  {
    runId: 'wf_running',
    sessionId: 'session',
    status: 'running',
    metadata: { workflowName: 'read-recent-git-commits' },
    jobs: [{ jobId: 'agent_1', status: 'succeeded' }, { jobId: 'agent_2', status: 'running' }],
  },
]

test('workflowListRows renders Dynamic workflows summary and selected prefix order', () => {
  assert.deepEqual(workflowListRows({ mode: 'list', runs: workflowRuns, selected: 0 }), [
    'Dynamic workflows',
    '1 completed',
    '',
    '› ✓ prompt-guided-planner-real-e2e-design  4 agents',
    '  ◌ read-recent-git-commits  2 agents',
    'Enter view - Up/Down move - Esc close',
  ])
})

test('workflowListCommandForKey moves selection and opens detail on Enter', () => {
  const panel = { mode: 'list' as const, runs: workflowRuns, selected: 0 }

  assert.equal(workflowListCommandForKey(panel, { downArrow: true }, '')?.panel?.selected, 1)
  assert.equal(workflowListCommandForKey({ ...panel, selected: 1 }, { upArrow: true }, '')?.panel?.selected, 0)
  assert.deepEqual(workflowListCommandForKey(panel, { return: true }, '')?.command, {
    type: 'workflow_detail',
    runId: 'wf_done_1',
  })
})

const awaitingRun: WorkflowRun = {
  runId: 'wf_demo',
  sessionId: 'session',
  status: 'awaiting_approval',
  permissionProfile: 'inherit-current-permissions',
  jobs: [],
}

test('workflowOverviewRows groups agents by progress phaseTitle', () => {
  const overview = workflowOverviewFromDetail({
    run: {
      runId: 'wf_overview',
      sessionId: 'session',
      status: 'succeeded',
      jobs: [
        { jobId: 'agent_1', status: 'succeeded', metadata: { label: 'planner-api', model: 'Opus 4.8 (1M context)' } },
        { jobId: 'agent_2', status: 'succeeded', metadata: { label: 'real-e2e-patterns', model: 'Opus 4.8 (1M context)' } },
        { jobId: 'agent_3', status: 'succeeded', metadata: { label: 'summary', model: 'Opus 4.8 (1M context)' } },
      ],
    },
    script: 'return 1',
    events: [],
    draft: {
      taskText: 'Design sufficient real GPT-5.5 E2E coverage for prompt-guided workflow planner',
      classification: { taskType: 'review' },
      plan: {
        meta: { name: 'prompt-guided-planner-real-e2e-design', description: 'Design sufficient real GPT-5.5 E2E coverage for prompt-guided workflow planner' },
        phases: [],
      },
      validation: { ok: true, issues: [] },
    },
    progress: {
      runId: 'wf_overview',
      sessionId: 'session',
      status: 'succeeded',
      workflowProgress: [
        { jobId: 'agent_1', label: 'planner-api', phaseTitle: 'Inspect', state: 'succeeded', tokenUsage: { totalTokens: 24900 }, toolCalls: Array.from({ length: 16 }, () => 'Read') },
        { jobId: 'agent_2', label: 'real-e2e-patterns', phaseTitle: 'Inspect', state: 'succeeded', tokenUsage: { totalTokens: 45000 }, toolCalls: Array.from({ length: 24 }, () => 'Read') },
        { jobId: 'agent_3', label: 'summary', phaseTitle: 'Matrix', state: 'succeeded', tokenUsage: { totalTokens: 7000 }, toolCalls: [] },
      ],
    },
  })

  assert.deepEqual(workflowOverviewRows(overview).slice(0, 8), [
    'prompt-guided-planner-real-e2e-design  3/3 agents · done',
    'Design sufficient real GPT-5.5 E2E coverage for prompt-guided workflow planner',
    'Phases | Inspect · 2 agents',
    '› ✓ Inspect 2/2 | ✓ planner-api  24.9k tok · 16 tools',
    '  ✓ Matrix 1/1 | ✓ real-e2e-patterns  45k tok · 24 tools',
    'Enter agent - Up/Down phase - Esc back',
  ])
})

test('workflowOverviewFromDetail falls back to draft phases when progress phase is missing', () => {
  const overview = workflowOverviewFromDetail({
    run: {
      runId: 'wf_draft_fallback',
      sessionId: 'session',
      status: 'running',
      jobs: [
        { jobId: 'agent_1', status: 'succeeded', metadata: { label: 'context-discovery' } },
        { jobId: 'agent_2', status: 'running', metadata: { label: 'implementation-plan' } },
      ],
    },
    script: 'return 1',
    events: [],
    draft: {
      taskText: '规划 workflow UI',
      classification: { taskType: 'planning' },
      plan: {
        meta: { name: 'workflow-ui-plan', description: '规划 workflow UI' },
        phases: [
          { title: 'Context Discovery', agents: [{ label: 'context-discovery' }] },
          { title: 'Implementation Plan', agents: [{ label: 'implementation-plan' }] },
        ],
      },
      validation: { ok: true, issues: [] },
    },
    progress: {
      runId: 'wf_draft_fallback',
      sessionId: 'session',
      status: 'running',
      workflowProgress: [
        { jobId: 'agent_1', label: 'context-discovery', state: 'succeeded' },
        { jobId: 'agent_2', label: 'implementation-plan', state: 'running' },
      ],
    },
  })

  assert.deepEqual(overview.phases.map(phase => `${phase.title}:${phase.completed}/${phase.total}`), [
    'Context Discovery:1/1',
    'Implementation Plan:0/1',
  ])
})
test('workflowOverviewFromDetail derives awaiting approval agents from draft when jobs and progress are empty', () => {
  const overview = workflowOverviewFromDetail({
    run: {
      runId: 'wf_draft_only',
      sessionId: 'session',
      status: 'awaiting_approval',
      jobs: [],
    },
    script: 'return 1',
    events: [],
    draft: {
      taskText: '规划只读 review workflow',
      classification: { taskType: 'review' },
      plan: {
        meta: { name: 'draft-only-review', description: 'Review draft before approval' },
        phases: [
          { title: 'Review', agents: [{ label: 'contract-review' }] },
          { title: 'Synthesis', agents: [{ label: 'summary', dependsOn: ['contract-review'] }] },
        ],
      },
      validation: { ok: true, issues: [] },
    },
    progress: null,
  })

  assert.deepEqual(overview.phases.map(phase => `${phase.title}:${phase.completed}/${phase.total}:${phase.agents.map(agent => agent.label).join(',')}`), [
    'Review:0/1:contract-review',
    'Synthesis:0/1:summary',
  ])
  assert.deepEqual(workflowOverviewRows(overview).slice(0, 5), [
    'draft-only-review  0/2 agents · awaiting_approval',
    'Review draft before approval',
    'Phases | Review · 1 agent',
    '› ✓ Review 0/1 | · contract-review',
    '  ✓ Synthesis 0/1 | ',
  ])
})
test('workflowPanelFromDetail defaults to overview rows instead of raw script', () => {
  const panel = workflowPanelFromDetail({
    run: {
      runId: 'wf_overview_panel',
      sessionId: 'session',
      status: 'succeeded',
      jobs: [{ jobId: 'agent_1', status: 'succeeded', metadata: { label: 'planner' } }],
    },
    script: 'raw script should not be primary UI',
    events: [],
    draft: {
      taskText: '规划 UI',
      classification: { taskType: 'planning' },
      plan: { meta: { name: 'workflow-ui-plan', description: '规划 UI' }, phases: [{ title: 'Plan', agents: [{ label: 'planner' }] }] },
      validation: { ok: true, issues: [] },
    },
    progress: null,
  })

  assert.equal(panel.mode, 'overview')
  assert.deepEqual(workflowPanelRows(panel).slice(0, 4), [
    'workflow-ui-plan  1/1 agents · done',
    '规划 UI',
    'Phases | Plan · 1 agent',
    '› ✓ Plan 1/1 | ✓ planner',
  ])
  assert.equal(workflowPanelRows(panel).some(row => row.includes('raw script')), false)
})
test('workflowRawDetailPanelFromDetail exposes raw script and approval metadata rows', () => {
  const panel = workflowRawDetailPanelFromDetail({
    run: awaitingRun,
    script: 'export const meta = { name: "demo" }\nreturn { ok: true }',
    events: [],
  })

  assert.deepEqual(workflowPanelRows(panel), [
    'Workflow wf_demo - awaiting_approval',
    'Permission: inherit-current-permissions',
    'Jobs: 0',
    'Script:',
    'export const meta = { name: "demo" }',
    'return { ok: true }',
    'Enter approve - d deny - s stop - Esc close',
  ])
})

test('workflowPanelCommandForKey maps approval keyboard shortcuts', () => {
  const panel = workflowRawDetailPanelFromDetail({ run: awaitingRun, script: 'return 1', events: [] })

  assert.deepEqual(workflowPanelCommandForKey(panel, { return: true }, ''), {
    type: 'workflow_approve',
    runId: 'wf_demo',
  })
  assert.deepEqual(workflowPanelCommandForKey(panel, {}, 'd'), {
    type: 'workflow_deny',
    runId: 'wf_demo',
    reason: 'denied from Ink UI',
  })
  assert.deepEqual(workflowPanelCommandForKey({ ...panel, run: { ...awaitingRun, status: 'running' } }, {}, 's'), {
    type: 'workflow_stop',
    runId: 'wf_demo',
    reason: 'stopped from Ink UI',
  })
})

test('workflowPanelCommandForKey maps resume shortcut for completed or interrupted runs', () => {
  const panel = workflowRawDetailPanelFromDetail({ run: { ...awaitingRun, status: 'interrupted' }, script: 'return 1', events: [] })

  assert.deepEqual(workflowPanelCommandForKey(panel, {}, 'r'), {
    type: 'workflow_resume',
    runId: 'wf_demo',
  })
  assert.equal(workflowPanelCommandForKey({ ...panel, run: { ...awaitingRun, status: 'running' } }, {}, 'r'), null)
  assert.equal(workflowPanelCommandForKey({ ...panel, run: { ...awaitingRun, status: 'cancelled' } }, {}, 'r'), null)
  assert.equal(workflowPanelRows(panel).at(-1), 'Enter approve - r resume - d deny - s stop - Esc close')
})
