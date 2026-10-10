// Ad-hoc real probe (not a CI test): times GA ink bridge startup against the
// real MCP config, to answer 'is MCP cold start really 37-82s, and does GA
// block on it?'.
//   readyMs      - bridge handshake done (input box is live from here)
//   perServer    - when each server first reported connected
//   mcpReadyMs   - discovery finished (loading=false)
// Run: node frontends/ink-ui/node_modules/tsx/dist/cli.mjs frontends/ink-ui/scripts/_probe_mcp_startup.ts
import { spawn } from 'node:child_process'
import path from 'node:path'
import readline from 'node:readline'
import { fileURLToPath } from 'node:url'

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..')
const NL = String.fromCharCode(10)
const child = spawn(process.env.PYTHON || 'python', [path.join(REPO, 'frontends', 'ink_bridge.py')], { cwd: REPO, stdio: ['pipe','pipe','pipe'] })
child.stderr.on('data', () => {})
const rl = readline.createInterface({ input: child.stdout })
const t0 = Date.now()
let ready = 0, firstProgress = 0
const perServer: Record<string, number> = {}
rl.on('line', line => {
  let ev: any; try { ev = JSON.parse(line) } catch { return }
  if (ev.type === 'ready') { ready = Date.now() - t0; child.stdin.write(JSON.stringify({type:'mcp_watch_start'})+NL) }
  if (ev.type === 'mcp_progress') {
    const dt = Date.now() - t0
    if (!firstProgress) firstProgress = dt
    for (const s of ev.servers || []) if (s.status === 'connected' && !(s.name in perServer)) perServer[s.name] = dt
    if (ev.loading === false) {
      console.log(JSON.stringify({ readyMs: ready, firstProgressMs: firstProgress, mcpReadyMs: dt, perServer, toolCount: (ev.tools||[]).length }, null, 2))
      child.kill(); process.exit(0)
    }
  }
})
setTimeout(() => { console.log(JSON.stringify({ timeout: true, readyMs: ready, firstProgressMs: firstProgress, perServer })); child.kill(); process.exit(1) }, 180000)
