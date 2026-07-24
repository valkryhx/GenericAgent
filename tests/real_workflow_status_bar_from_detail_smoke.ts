import { readFileSync } from 'node:fs'
import { workflowStatusBarFromState, workflowStatusBarRows } from '../frontends/ink-ui/src/workflowStatusBar.js'
import type { AppState } from '../frontends/ink-ui/src/state.js'

const detailPath = process.argv[2]
if (!detailPath) {
  console.error('usage: tsx tests/real_workflow_status_bar_from_detail_smoke.ts <detail.json>')
  process.exit(2)
}
const detail = JSON.parse(readFileSync(detailPath, 'utf8'))
const state: AppState = {
  status: 'idle',
  activityLabel: null,
  tokenUsage: null,
  messages: [],
  error: null,
  workflows: [detail.run],
  workflowEvents: detail.events ?? [],
  workflowDetails: {
    [detail.run.runId]: {
      run: detail.run,
      script: detail.script,
      events: detail.events ?? [],
      draft: detail.draft ?? null,
      progress: detail.progress ?? null,
    },
  },
  workflowResults: {},
}
const bar = workflowStatusBarFromState(state)
const rows = bar ? workflowStatusBarRows(bar) : []
const summary = {
  passed: false,
  runId: detail.run?.runId,
  status: detail.run?.status,
  bar,
  rows,
}
if (!bar) {
  console.log(JSON.stringify({ ...summary, error: 'status bar did not derive a live workflow summary' }, null, 2))
  process.exit(3)
}
if (detail.run?.status === 'awaiting_approval' && rows.some(row => row.includes('x stop'))) {
  console.log(JSON.stringify({ ...summary, error: 'awaiting approval status bar exposed stop shortcut' }, null, 2))
  process.exit(4)
}
if (!rows.some(row => row.includes('Enter'))) {
  console.log(JSON.stringify({ ...summary, error: 'status bar did not expose Enter shortcut' }, null, 2))
  process.exit(5)
}
if (rows.some(row => row.includes(String(detail.script || '').slice(0, 20)))) {
  console.log(JSON.stringify({ ...summary, error: 'status bar leaked raw script' }, null, 2))
  process.exit(6)
}
console.log(JSON.stringify({ ...summary, passed: true }, null, 2))
