// Ad-hoc real E2E (not a CI test): drives the real ink bridge with the default
// (model-authored) planner and reports the plan shape, the run status and the
// artifact bookkeeping. Proves the /workflow default path executes.
// Run: node frontends/ink-ui/node_modules/tsx/dist/cli.mjs frontends/ink-ui/scripts/_probe_workflow_default_planner.ts
import { spawn } from 'node:child_process'
import path from 'node:path'
import readline from 'node:readline'
import { fileURLToPath } from 'node:url'

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..')
const NL = String.fromCharCode(10)
const TASK = process.env.GA_PROBE_TASK
  || '分别用python写3个demo：1.hello word程序 2. 1-100内的质数 3. html展示你好二字 然后检验结果'
const child = spawn(process.env.PYTHON || 'python', [path.join(REPO, 'frontends', 'ink_bridge.py')], { cwd: REPO, stdio: ['pipe','pipe','pipe'] })
child.stderr.on('data', () => {})
const rl = readline.createInterface({ input: child.stdout })
const t0 = Date.now()
const seen: string[] = []
rl.on('line', line => {
  let ev: any; try { ev = JSON.parse(line) } catch { return }
  if (ev.type === 'ready') {
    child.stdin.write(JSON.stringify({type:'mcp_watch_start'})+NL)
    child.stdin.write(JSON.stringify({type:'workflow_plan', taskText: TASK, autoApprove: true})+NL)
    return
  }
  if (ev.type === 'workflow_run') {
    const md = ev.run?.metadata || {}
    console.log('plannerMode', md.plannerMode, '| taskType', md.workflowTaskType, '| mode', md.mode)
    console.log('workspacePath', md.workspacePath)
  }
  if (ev.type === 'workflow_progress') {
    const rows = (ev.progress?.workflowProgress || []).map((j: any) => j.label + ':' + j.status)
    const key = rows.join(',')
    if (rows.length && seen.at(-1) !== key) { seen.push(key); console.log('progress', key) }
  }
  if (ev.type === 'workflow_final') {
    console.log('FINAL status', ev.result?.status, '| outcome', ev.result?.executionOutcome, '| acceptance', ev.result?.acceptanceStatus)
    console.log('issues', JSON.stringify(ev.result?.workflowIssues || []).slice(0, 500))
    console.log('elapsed', Math.round((Date.now()-t0)/1000)+'s')
    child.kill(); process.exit(0)
  }
  if (ev.type === 'error') console.log('ERROR', ev.code, String(ev.message||'').slice(0,400))
})
setTimeout(() => { console.log('TIMEOUT'); child.kill(); process.exit(1) }, 900000)
