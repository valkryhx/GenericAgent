// Ad-hoc real probe (not a CI test): measures the three workflow-UX complaints
// against a real planner + real MCP.
//   1. does /workflow planning block the JSONL command loop?
//   2. do live turn/token counters reach the UI while children run?
//   3. does the bridge return to idle after the terminal workflow event?
// Run: GA_PROBE_PROFILE=<profile> npx tsx frontends/ink-ui/scripts/_probe_workflow_progress.ts
import { spawn } from 'node:child_process'
import path from 'node:path'
import readline from 'node:readline'
import { fileURLToPath } from 'node:url'
import { writeFileSync } from 'node:fs'

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..')
const PYTHON = process.env.PYTHON || 'python'
const BRIDGE = path.join(REPO, 'frontends', 'ink_bridge.py')
const PROFILE = process.env.GA_PROBE_PROFILE || 'cc-deepseek-v4.1-flash-chat'
const TASK = process.env.GA_PROBE_TASK
  || '使用 Tavily 搜索 Python 官方文档中 pathlib.Path.write_text 的行为，然后生成一份介绍该 API 的 HTML 文件并验证文件'
const IDLE_TIMEOUT_MS = Number(process.env.GA_PROBE_IDLE_TIMEOUT_MS || 600_000)
const TOTAL_TIMEOUT_MS = Number(process.env.GA_PROBE_TIMEOUT_MS || 900_000)

const events: Array<Record<string, unknown>> = []
const t0 = Date.now()
const stamps: Record<string, number> = {}
const MARKS_FILE = path.join(REPO, 'temp', '_probe_marks.json')
const mark = (k: string) => {
  if (stamps[k] === undefined) stamps[k] = Date.now() - t0
  try { writeFileSync(MARKS_FILE, JSON.stringify({ stamps, events: events.map(e => String(e.type)) }, null, 2)) } catch {}
}

const child = spawn(PYTHON, [BRIDGE], {
  cwd: REPO,
  env: { ...process.env, GA_WORKFLOW_PLANNER_MODE: 'real', GA_WORKFLOW_LLM_PROFILE: PROFILE, GA_REAL_API_PROFILE: PROFILE },
  stdio: ['pipe', 'pipe', 'pipe'],
})
const send = (obj: Record<string, unknown>) => child.stdin.write(JSON.stringify(obj) + '\n')
child.stderr.on('data', () => {})
const rl = readline.createInterface({ input: child.stdout })
let probeSent = false
let workflowStarted = false
let initialMcpStatusSeen = false
let probeReturned: number | null = null

rl.on('line', line => {
  let ev: Record<string, unknown>
  try { ev = JSON.parse(line) } catch { return }
  events.push(ev)
  const type = String(ev.type)
  if (type === 'ready') { mark('ready'); send({ type: 'mcp_watch_start' }); send({ type: 'mcp_status' }); return }
  if (type === 'mcp_progress' && ev.loading === false) {
    const tools = (ev.tools as Array<{ function?: { name?: string } }> | undefined) || []
    if (!workflowStarted && tools.some(t => t.function?.name === 'mcp__tavily__tavily_search')) {
      workflowStarted = true
      mark('mcp_ready')
      // Kick off the workflow, then immediately fire a second command. If the
      // loop is blocked by planning, the second reply cannot arrive early.
      mark('plan_sent')
      send({ type: 'workflow_plan', taskText: TASK, autoApprove: true })
      probeSent = true
      send({ type: 'mcp_status' })
    }
    return
  }
  if (type === 'mcp_status') {
    if (!probeSent && !initialMcpStatusSeen) { initialMcpStatusSeen = true; return }
    if (probeSent && probeReturned === null) { probeReturned = Date.now() - t0; mark('second_command_reply') }
  }
  if (type === 'activity' || type === 'status') mark(`first_${type}`)
  if (type === 'workflow_final') mark('workflow_final')
  if (type === 'status' && ev.status === 'idle') mark('idle')
})

const waitFor = async (label: string, pred: () => boolean, timeoutMs: number) => {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (pred()) return true
    await new Promise(r => setTimeout(r, 100))
  }
  throw new Error(`${label} timed out`)
}

const summary: Record<string, unknown> = { profile: PROFILE, task: TASK }
try {
  await waitFor('workflow_final', () => events.some(e => e.type === 'workflow_final'), TOTAL_TIMEOUT_MS)
  const live = events.filter(e => e.type === 'workflow_live')
  const liveJobs = live.flatMap(e => (e.jobs as Array<Record<string, unknown>>) || [])
  const maxTurn = liveJobs.reduce((m, j) => Math.max(m, Number(j.turn) || 0), 0)
  const withTokens = liveJobs.filter(j => j.tokenUsage && Number((j.tokenUsage as Record<string, unknown>).total ?? (j.tokenUsage as Record<string, unknown>).input ?? 0) > 0).length
  const progress = events.filter(e => e.type === 'workflow_progress')
  let idle = false
  try { await waitFor('idle', () => events.some(e => e.type === 'status' && e.status === 'idle'), IDLE_TIMEOUT_MS); idle = true } catch {}
  Object.assign(summary, {
    secondCommandReplyAfterPlanMs: (stamps.second_command_reply !== undefined && stamps.plan_sent !== undefined) ? stamps.second_command_reply - stamps.plan_sent : null,
    planSentToFirstActivityMs: stamps.first_activity !== undefined && stamps.plan_sent !== undefined ? stamps.first_activity - stamps.plan_sent : null,
    planSentToFirstStatusMs: stamps.first_status !== undefined && stamps.plan_sent !== undefined ? stamps.first_status - stamps.plan_sent : null,
    workflowLiveEvents: live.length,
    workflowProgressEvents: progress.length,
    maxLiveTurn: maxTurn,
    liveSamplesWithTokens: withTokens,
    returnedToIdle: idle,
    planSentToWorkflowFinalMs: stamps.workflow_final !== undefined && stamps.plan_sent !== undefined ? stamps.workflow_final - stamps.plan_sent : null,
    planSentToIdleMs: stamps.idle !== undefined && stamps.plan_sent !== undefined ? stamps.idle - stamps.plan_sent : null,
    eventTypes: [...new Set(events.map(e => String(e.type)))],
  })
} catch (err) {
  summary.error = String(err).slice(0, 300)
  summary.eventTypes = [...new Set(events.map(e => String(e.type)))]
} finally {
  child.kill()
  console.log(JSON.stringify(summary, null, 2))
}
