import { readFileSync } from 'node:fs'
import { workflowAgentDetailPanelFromOverview, workflowOverviewFromDetail, workflowOverviewRows, workflowPanelFromDetail, workflowPanelRows } from '../frontends/ink-ui/src/workflowPanel.js'

const detailPath = process.argv[2]
if (!detailPath) {
  console.error('usage: tsx tests/real_workflow_overview_from_detail_smoke.ts <detail.json>')
  process.exit(2)
}
const detail = JSON.parse(readFileSync(detailPath, 'utf8'))
const overview = workflowOverviewFromDetail(detail)
const rows = workflowOverviewRows(overview)
const panel = workflowPanelFromDetail(detail)
const agentPanel = overview.total > 0 ? workflowAgentDetailPanelFromOverview(panel, overview.selectedPhase, 0) : null
const agentRows = agentPanel ? workflowPanelRows(agentPanel) : []
const summary = {
  passed: false,
  name: overview.name,
  description: overview.description,
  phaseCount: overview.phases.length,
  totalAgents: overview.total,
  completedAgents: overview.completed,
  rows,
  agentRows,
}
if (!overview.name || overview.name === detail.run?.runId) {
  console.log(JSON.stringify({ ...summary, error: 'overview name did not use draft metadata' }, null, 2))
  process.exit(3)
}
if (overview.phases.length < 1 || overview.total < 1) {
  console.log(JSON.stringify({ ...summary, error: 'overview did not derive phases/agents from real detail' }, null, 2))
  process.exit(4)
}
if (rows.some(row => row.includes(String(detail.script || '').slice(0, 20)))) {
  console.log(JSON.stringify({ ...summary, error: 'overview rows leaked raw script as primary UI' }, null, 2))
  process.exit(5)
}
if (!agentPanel || !agentRows.some(row => row.includes('Prompt')) || !agentRows.some(row => row.includes('Activity')) || !agentRows.some(row => row.includes('Outcome'))) {
  console.log(JSON.stringify({ ...summary, error: 'agent detail rows did not expose Prompt/Activity/Outcome sections' }, null, 2))
  process.exit(6)
}
if (agentRows.some(row => row.includes(String(detail.script || '').slice(0, 20)))) {
  console.log(JSON.stringify({ ...summary, error: 'agent detail rows leaked raw script as primary UI' }, null, 2))
  process.exit(7)
}
console.log(JSON.stringify({ ...summary, passed: true }, null, 2))
