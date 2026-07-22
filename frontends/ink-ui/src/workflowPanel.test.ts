import test from 'node:test'
import assert from 'node:assert/strict'
import { workflowPanelFromDetail, workflowRawDetailPanelFromDetail, workflowPanelRows, workflowPanelCommandForKey, workflowListRows, workflowListCommandForKey, workflowOverviewFromDetail, workflowOverviewRows, workflowAgentDetailPanelFromOverview, workflowPanelWithRunUpdate } from './workflowPanel.js'
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
test('workflowAgentDetailRows renders selected agent Prompt Activity Outcome sections', () => {
  const panel = workflowPanelFromDetail({
    run: {
      runId: 'wf_agent_detail',
      sessionId: 'session',
      status: 'succeeded',
      jobs: [
        { jobId: 'agent_1', prompt: 'Inspect workflow_planner.py', status: 'succeeded', metadata: { label: 'planner-api', model: 'Opus 4.8 (1M context)' } },
        { jobId: 'agent_2', prompt: 'Inspect real E2E tests', status: 'succeeded', metadata: { label: 'real-e2e-patterns', model: 'Opus 4.8 (1M context)' } },
      ],
    },
    script: 'return 1',
    events: [],
    draft: null,
    progress: {
      runId: 'wf_agent_detail',
      sessionId: 'session',
      status: 'succeeded',
      workflowProgress: [
        {
          jobId: 'agent_1',
          label: 'planner-api',
          phaseTitle: 'Inspect',
          state: 'succeeded',
          toolCalls: ['Read 1', 'Read 2', 'Grep workflow symbols', 'Read final notes'],
          tokenUsage: { totalTokens: 24900 },
          promptPreview: 'Inspect workflow_planner.py preview',
          resultPreview: '已检查 workflow planner public API。',
          lastToolName: 'Grep',
          lastToolSummary: 'searched workflow symbols',
        },
        {
          jobId: 'agent_2',
          label: 'real-e2e-patterns',
          phaseTitle: 'Inspect',
          state: 'succeeded',
          toolCalls: ['Read real smoke tests'],
          tokenUsage: { totalTokens: 45000 },
          promptPreview: 'Inspect real E2E tests preview',
          resultPreview: '已检查 real E2E patterns。',
        },
      ],
    },
  })
  const detailPanel = workflowAgentDetailPanelFromOverview(panel, 0, 0)

  assert.deepEqual(workflowPanelRows(detailPanel).slice(0, 14), [
    'Inspect · 2 agents | planner-api',
    '› ✓ planner-api | ✓ Completed · Opus 4.8 (1M context) · 24.9k tok · 4 tool calls',
    '  ✓ real-e2e-patterns | Prompt',
    '                   |   Inspect workflow_planner.py',
    '                   | Activity · last 3 of 4 tool calls',
    '                   |   Read 2',
    '                   |   Grep workflow symbols',
    '                   |   Read final notes',
    '                   | Outcome',
    '                   |   已检查 workflow planner public API。',
    '↑↓ agent · j/k scroll · esc back',
  ])
})

test('workflowPanelCommandForKey opens agent detail and navigates within it', () => {
  const overviewPanel = workflowPanelFromDetail({
    run: {
      runId: 'wf_agent_keys',
      sessionId: 'session',
      status: 'succeeded',
      jobs: [
        { jobId: 'agent_1', status: 'succeeded', metadata: { label: 'first' } },
        { jobId: 'agent_2', status: 'running', metadata: { label: 'second' } },
      ],
    },
    script: 'return 1',
    events: [],
    draft: { taskText: 'x', classification: {}, plan: { meta: { name: 'x' }, phases: [{ title: 'P', agents: [{ label: 'first' }, { label: 'second' }] }] }, validation: { ok: true } },
    progress: null,
  })

  const opened = workflowPanelCommandForKey(overviewPanel, { return: true }, '')?.panel
  assert.equal(opened?.mode, 'agent_detail')
  assert.equal(opened?.agentIndex, 0)

  const moved = opened ? workflowPanelCommandForKey(opened, { downArrow: true }, '')?.panel : null
  assert.equal(moved?.mode, 'agent_detail')
  if (moved?.mode !== 'agent_detail') throw new Error('expected agent detail panel')
  assert.equal(moved.agentIndex, 1)
  assert.equal(workflowPanelRows(moved).some(row => row.includes('second')), true)

  assert.equal(workflowPanelCommandForKey(moved, {}, 'j')?.panel?.mode, 'agent_detail')
  const scrolled = workflowPanelCommandForKey(moved, {}, 'j')?.panel
  if (scrolled?.mode !== 'agent_detail') throw new Error('expected scrolled agent detail panel')
  assert.equal(scrolled.scrollOffset, 1)
  const unscrolled = workflowPanelCommandForKey({ ...moved, scrollOffset: 1 }, {}, 'k')?.panel
  if (unscrolled?.mode !== 'agent_detail') throw new Error('expected unscrolled agent detail panel')
  assert.equal(unscrolled.scrollOffset, 0)
  const escaped = workflowPanelCommandForKey(moved, { escape: true }, '')?.panel
  assert.equal(escaped?.mode, 'overview')
  if (escaped?.mode !== 'overview') throw new Error('expected overview panel')
  assert.equal(escaped.overview.selectedPhase, 0)
})

test('workflowPanelWithRunUpdate refreshes visible agent detail status and outcome', () => {
  const overviewPanel = workflowPanelFromDetail({
    run: {
      runId: 'wf_agent_update',
      sessionId: 'session',
      status: 'running',
      jobs: [{ jobId: 'agent_1', status: 'running', metadata: { label: 'builder' } }],
    },
    script: 'return 1',
    events: [],
    draft: { taskText: 'x', classification: {}, plan: { meta: { name: 'x' }, phases: [{ title: 'Build', agents: [{ label: 'builder', prompt: 'Build workflow UI' }] }] }, validation: { ok: true } },
    progress: null,
  })
  const detailPanel = workflowAgentDetailPanelFromOverview(overviewPanel, 0, 0, 2)

  const refreshed = workflowPanelWithRunUpdate(detailPanel, {
    runId: 'wf_agent_update',
    sessionId: 'session',
    status: 'succeeded',
    jobs: [{ jobId: 'agent_1', status: 'succeeded', metadata: { label: 'builder' } }],
  })

  assert.equal(refreshed.mode, 'agent_detail')
  if (refreshed.mode !== 'agent_detail') throw new Error('expected agent detail panel')
  assert.equal(refreshed.scrollOffset, 2)
  assert.equal(refreshed.detail.status, 'succeeded')
  assert.equal(refreshed.detail.statusText, 'Completed')
  assert.equal(refreshed.detail.outcome, '(agent did not produce an outcome)')
  assert.equal(workflowPanelRows({ ...refreshed, scrollOffset: 0 }).some(row => row.includes('✓ Completed')), true)
})

test('workflowPanelCommandForKey leaves overview unchanged when selected phase has no agents', () => {
  const panel = workflowPanelFromDetail({
    run: { runId: 'wf_empty_phase', sessionId: 'session', status: 'succeeded', jobs: [] },
    script: 'return 1',
    events: [],
    draft: { taskText: 'x', classification: {}, plan: { meta: { name: 'x' }, phases: [] }, validation: { ok: true } },
    progress: null,
  })

  assert.equal(workflowPanelCommandForKey(panel, { return: true }, ''), null)
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
    'Esc close',
  ])
})

test('workflowPanelCommandForKey maps stop and resume without approve/deny', () => {
  const panel = workflowRawDetailPanelFromDetail({ run: awaitingRun, script: 'return 1', events: [] })

  assert.equal(workflowPanelCommandForKey(panel, { return: true }, '')?.command, undefined)
  assert.equal(workflowPanelCommandForKey(panel, {}, 'd')?.command, undefined)
  assert.deepEqual(workflowPanelCommandForKey({ ...panel, run: { ...awaitingRun, status: 'running' } }, {}, 's')?.command, {
    type: 'workflow_stop',
    runId: 'wf_demo',
    reason: 'stopped from Ink UI',
  })
})

test('workflowPanelCommandForKey maps resume shortcut for completed or interrupted runs', () => {
  const panel = workflowRawDetailPanelFromDetail({ run: { ...awaitingRun, status: 'interrupted' }, script: 'return 1', events: [] })

  assert.deepEqual(workflowPanelCommandForKey(panel, {}, 'r')?.command, {
    type: 'workflow_resume',
    runId: 'wf_demo',
  })
  assert.equal(workflowPanelCommandForKey({ ...panel, run: { ...awaitingRun, status: 'running' } }, {}, 'r'), null)
  assert.equal(workflowPanelCommandForKey({ ...panel, run: { ...awaitingRun, status: 'cancelled' } }, {}, 'r'), null)
  assert.equal(workflowPanelRows(panel).at(-1), 'r resume - s stop - Esc close')
})
