import { readFileSync } from 'node:fs'
import { workflowOverviewFromDetail, workflowOverviewRows } from '../frontends/ink-ui/src/workflowPanel.js'

const detailPath = process.argv[2]
if (!detailPath) {
  console.error('usage: tsx tests/real_workflow_overview_from_detail_smoke.ts <detail.json>')
  process.exit(2)
}
const detail = JSON.parse(readFileSync(detailPath, 'utf8'))
const overview = workflowOverviewFromDetail(detail)
const rows = workflowOverviewRows(overview)
const summary = {
  passed: false,
  name: overview.name,
  description: overview.description,
  phaseCount: overview.phases.length,
  totalAgents: overview.total,
  completedAgents: overview.completed,
  rows,
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
console.log(JSON.stringify({ ...summary, passed: true }, null, 2))
