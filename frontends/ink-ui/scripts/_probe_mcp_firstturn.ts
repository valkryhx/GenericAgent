// Ad-hoc real probe (not a CI test): proves the first turn is answered without
// waiting for MCP readiness. Submits immediately after the bridge 'ready'
// event and reports when the first token and the final answer arrive versus
// when MCP discovery finally settles.
// Run: node frontends/ink-ui/node_modules/tsx/dist/cli.mjs frontends/ink-ui/scripts/_probe_mcp_firstturn.ts
import { spawn } from 'node:child_process'
import path from 'node:path'
import readline from 'node:readline'
import { fileURLToPath } from 'node:url'

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..')
const NL = String.fromCharCode(10)
const PROMPT = process.env.GA_PROBE_PROMPT || 'Reply with exactly: FIRST_TURN_OK'
const child = spawn(process.env.PYTHON || 'python', [path.join(REPO, 'frontends', 'ink_bridge.py')], { cwd: REPO, stdio: ['pipe','pipe','pipe'] })
child.stderr.on('data', () => {})
const rl = readline.createInterface({ input: child.stdout })
const t0 = Date.now()
let ready = 0, mcpReady = 0, submitSent = 0, firstDelta = 0, done = 0
rl.on('line', line => {
  let ev: any; try { ev = JSON.parse(line) } catch { return }
  const dt = Date.now() - t0
  if (ev.type === 'ready') {
    ready = dt
    child.stdin.write(JSON.stringify({type:'mcp_watch_start'})+NL)
    submitSent = Date.now() - t0
    child.stdin.write(JSON.stringify({type:'submit', text: PROMPT})+NL)
  }
  if (ev.type === 'mcp_progress' && ev.loading === false && !mcpReady) mcpReady = dt
  if (ev.type === 'assistant_delta' && !firstDelta) firstDelta = dt
  if (ev.type === 'assistant_done' && !done) {
    done = dt
    console.log(JSON.stringify({ readyMs: ready, submitSentMs: submitSent, firstDeltaMs: firstDelta, answerDoneMs: done, mcpReadyMs: mcpReady || null, answerPreview: String(ev.text||'').slice(0,120) }, null, 2))
    child.kill(); process.exit(0)
  }
})
setTimeout(() => { console.log(JSON.stringify({ timeout: true, readyMs: ready, mcpReadyMs: mcpReady, firstDeltaMs: firstDelta })); child.kill(); process.exit(1) }, 180000)
