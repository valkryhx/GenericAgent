import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { readFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import React from 'react'
import { render } from 'ink'
import { App } from '../src/App.js'
import { startBridge, type BridgeClient } from '../src/bridgeClient.js'
import { createCursorParkStdout } from '../src/stdoutCursorPark.js'
import { applyBridgeEvent, initialState, type AppState } from '../src/state.js'
import type { BridgeCommand, BridgeEvent } from '../src/protocol.js'

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..')
const PYTHON = process.env.PYTHON || 'python'
const BRIDGE_SCRIPT = path.join(REPO, 'frontends', 'ink_bridge.py')
const PROFILE = process.env.GA_WORKFLOW_E2E_PROFILE || 'deepseek-v4.1-flash'
const TASK = process.env.GA_WORKFLOW_E2E_TASK || '使用 Tavily 搜索 Python 官方文档中 pathlib.Path.write_text 的行为，然后生成一份介绍该 API 的 HTML 文件并验证文件'
const EXPECTED_MCP_TOOL = process.env.GA_WORKFLOW_E2E_MCP_TOOL || 'mcp__tavily__tavily_search'
const EXPECTED_ARTIFACT_PATTERN = process.env.GA_WORKFLOW_E2E_EXPECTED_ARTIFACT_PATTERN
  ? new RegExp(process.env.GA_WORKFLOW_E2E_EXPECTED_ARTIFACT_PATTERN, 'i')
  : null
const WORKFLOW_START_TIMEOUT_MS = Number(process.env.GA_WORKFLOW_E2E_START_TIMEOUT_MS || 600_000)
const WORKFLOW_TOTAL_TIMEOUT_MS = Number(process.env.GA_WORKFLOW_E2E_TIMEOUT_MS || 600_000)

class CaptureWriteStream extends EventEmitter {
  columns = 100
  rows = 30
  isTTY = true
  write(_chunk: unknown): boolean { return true }
}

class FakeReadStream extends EventEmitter {
  isTTY = true
  setRawMode(): this { return this }
  setEncoding(): this { return this }
  ref(): this { return this }
  unref(): this { return this }
  resume(): this { return this }
  pause(): this { return this }
  private queue: string[] = []
  read(): string | null { return this.queue.shift() ?? null }
  send(text: string): void { this.queue.push(text); this.emit('readable') }
}

const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))
async function waitFor(label: string, predicate: () => boolean, timeoutMs: number): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (predicate()) return
    await delay(100)
  }
  throw new Error(`${label} timed out`)
}
async function typeInput(stdin: FakeReadStream, text: string): Promise<void> {
  for (const char of text) { stdin.send(char); await delay(4) }
  stdin.send(String.fromCharCode(13))
}
function safeError(value: unknown): string {
  return String(value)
    .replace(/Bearer\s+[^\s"']+/gi, 'Bearer [REDACTED]')
    .replace(/sk-[A-Za-z0-9_-]{8,}/g, '[REDACTED_KEY]')
    .replace(/([?&](?:api[_-]?key|token)=)[^&\s]+/gi, '$1[REDACTED]')
    .slice(0, 800)
}

function selectTerminalArtifact(draft, researchLabels) {
  const artifacts = draft?.plan?.executionContract?.artifacts || []
  const agents = (draft?.plan?.phases || []).flatMap(phase => phase.agents || [])
  const dependencies = new Map(agents.map(agent => [String(agent.label || ''), Array.isArray(agent.dependsOn) ? agent.dependsOn.map(String) : []]))
  const ancestorsOf = (label) => {
    const seen = new Set()
    const visit = (current) => {
      for (const dependency of dependencies.get(current) || []) {
        if (seen.has(dependency)) continue
        seen.add(dependency)
        visit(dependency)
      }
    }
    visit(String(label || ''))
    return seen
  }
  const scored = artifacts
    .filter(artifact => artifact?.path && artifact.writer)
    .map(artifact => {
      const ancestors = ancestorsOf(artifact.writer)
      const coversResearch = researchLabels.every(label => ancestors.has(String(label)))
      const terminal = researchLabels.length > 0 && coversResearch
      const outputLike = /\.(html|docx|md|pdf)$/i.test(String(artifact.path))
      return {artifact, ancestors, terminal, outputLike}
    })
  // Prefer the artifact authored by an agent that consumes all research
  // outputs; that is the workflow deliverable, not an intermediate research
  // file. Fall back to the last declared artifact when the graph is linear.
  const terminalCandidates = scored.filter(item => item.terminal)
  const pool = terminalCandidates.length > 0 ? terminalCandidates : scored
  pool.sort((a, b) => (Number(b.outputLike) - Number(a.outputLike)) || (b.ancestors.size - a.ancestors.size))
  return pool[0]?.artifact
}

async function main(): Promise<number> {
  process.env.GA_WORKFLOW_PLANNER_MODE = 'real'
  process.env.GA_WORKFLOW_LLM_PROFILE = PROFILE
  process.env.GA_REAL_API_PROFILE = PROFILE
  delete process.env.GA_INK_MOUSE

  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
  const stdin = new FakeReadStream()
  const cursorPark = createCursorParkStdout(stdout as unknown as NodeJS.WriteStream)
  const events: BridgeEvent[] = []
  const commands: BridgeCommand[] = []
  let state: AppState = initialState
  let realClient: BridgeClient | null = null

  const startBridgeClient = (python: string, bridgeScript: string, onEvent: (event: BridgeEvent) => void, onExit: (code: number | null) => void): BridgeClient => {
    realClient = startBridge(python, bridgeScript, event => {
      events.push(event)
      state = applyBridgeEvent(state, event)
      onEvent(event)
    }, onExit)
    return {
      send(command) { commands.push(command); realClient?.send(command) },
      stop() { realClient?.stop() },
    }
  }
  const instance = render(React.createElement(App, {
    python: PYTHON,
    bridgeScript: BRIDGE_SCRIPT,
    startBridgeClient,
    cursorPark,
  }), {
    stdout: cursorPark.stdout,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    patchConsole: false,
    debug: true,
  })

  const summary: Record<string, unknown> = { passed: false, modelProfile: PROFILE, expectedMcpTool: EXPECTED_MCP_TOOL, task: TASK }
  try {
    await waitFor('bridge ready', () => events.some(event => event.type === 'ready'), 60_000)
    realClient?.send({ type: 'model_status' })
    await waitFor('model list', () => events.some(event => event.type === 'model_status'), 60_000)
    const modelEvent = [...events].reverse().find((event): event is Extract<BridgeEvent, {type: 'model_status'}> => event.type === 'model_status')!
    const model = modelEvent.models.find(item => item.name.toLowerCase().startsWith(`${PROFILE}/`))
    assert.ok(model, `profile ${PROFILE} is absent from GA model list`)
    if (!model.current) {
      await typeInput(stdin, `/model ${model.index}`)
      await waitFor(`${PROFILE} model switch`, () => events.some(event => event.type === 'model_switch_result' && event.ok) && events.some(event => event.type === 'model_status' && event.models.some(item => item.index === model.index && item.current)), 60_000)
    }

    realClient?.send({ type: 'mcp_status' })
    await waitFor(`${EXPECTED_MCP_TOOL} MCP ready`, () => events.some(event => (
      event.type === 'mcp_progress'
      && event.loading === false
      && event.tools.some(tool => tool.function?.name === EXPECTED_MCP_TOOL)
    )), 180_000)

    // Two legitimate activation routes reach the same planner:
    //   * an explicit `/workflow <task>` line becomes the `workflow_plan` bridge command;
    //   * plain task text becomes `submit`, and the host's activation resolver
    //     decides from the task signals (search + artifact + synthesis, ...).
    // The harness must accept either; asserting only on `submit` made an
    // explicit `/workflow` run look like a hang.
    const submitBefore = commands.filter(command => command.type === 'submit').length
    const planBefore = commands.filter(command => command.type === 'workflow_plan').length
    await typeInput(stdin, TASK)
    await waitFor('workflow activation command', () => (
      commands.filter(command => command.type === 'submit').length > submitBefore
      || commands.filter(command => command.type === 'workflow_plan').length > planBefore
    ), 20_000)
    await waitFor('autonomous workflow started or planner failed', () => (
      state.workflows.some(run => run.status === 'running' || run.status === 'succeeded' || run.status === 'failed')
      || events.some(event => event.type === 'error' && event.code.startsWith('workflow_plan'))
    ), WORKFLOW_START_TIMEOUT_MS)
    const runId = state.workflows.at(-1)?.runId
    assert.ok(runId, 'workflow run id was not projected into Ink state')
    await waitFor('workflow final event', () => events.some(event => event.type === 'workflow_final' && event.runId === runId), WORKFLOW_TOTAL_TIMEOUT_MS)

    const run = state.workflows.find(item => item.runId === runId)
    assert.equal(run?.status, 'succeeded', `workflow ended ${run?.status}: ${run?.error || ''}`)
    assert.ok(run?.artifactDir, 'workflow artifact directory missing')
    const draft = JSON.parse(await readFile(path.join(run.artifactDir!, 'workflow-draft.json'), 'utf8')) as {
      plan?: { phases?: Array<{ agents?: Array<Record<string, unknown>> }> ; executionContract?: { artifacts?: Array<{path?: string; writer?: string}> } }
    }
    const agents = (draft.plan?.phases || []).flatMap(phase => phase.agents || [])
    const byLabel = new Map(agents.map(agent => [String(agent.label || ''), agent]))
    const parallelResearch = agents.filter(agent => {
      const tools = Array.isArray(agent.requiredTools) ? agent.requiredTools.map(String) : []
      return String(agent.role || '').toLowerCase() === 'research' && tools.includes(EXPECTED_MCP_TOOL)
    })
    const taskIsLiuGuoliang = /刘国梁|liu\s*guoliang/i.test(TASK)
    const taskNeedsFiveResearchAgents = /5个subagent|五个subagent|5个子代理|五个子代理/i.test(TASK)
    const expectedResearchCount = taskNeedsFiveResearchAgents ? 5 : taskIsLiuGuoliang ? 2 : undefined
    if (expectedResearchCount !== undefined) {
      assert.equal(parallelResearch.length, expectedResearchCount, `expected exactly ${expectedResearchCount} Tavily research agents, got ${parallelResearch.length}`)
      const persistedRun = JSON.parse(await readFile(path.join(run.artifactDir!, 'run.json'), 'utf8')) as {
        jobs?: Array<{jobId?: string; metadata?: {label?: string}}> 
      }
      const jobsByLabel = new Map((persistedRun.jobs || []).map(job => [String(job.metadata?.label || ''), String(job.jobId || '')]))
      let tavilyAgentsWithCalls = 0
      for (const researchAgent of parallelResearch) {
        const label = String(researchAgent.label || '')
        const jobId = jobsByLabel.get(label)
        assert.ok(jobId, `research agent ${label} has no persisted job`)
        const transcript = await readFile(path.join(run.artifactDir!, 'agents', jobId!, 'transcript.jsonl'), 'utf8')
        const calls = transcript.split(/\r?\n/).filter(line => {
          if (!line.trim()) return false
          try {
            const event = JSON.parse(line)
            return event.type === 'tool_call' && event.toolName === 'mcp__tavily__tavily_search'
          } catch { return false }
        })
        assert.ok(calls.length > 0, `research agent ${label} did not actually call Tavily`)
        tavilyAgentsWithCalls += 1
      }
      assert.equal(tavilyAgentsWithCalls, expectedResearchCount)

      const researchLabels = parallelResearch.map(agent => String(agent.label || ''))
      const artifact = selectTerminalArtifact(draft, researchLabels) || draft.plan?.executionContract?.artifacts?.[0]
      assert.ok(artifact?.path && artifact.writer, 'plan has no declared output artifact')
      const writer = byLabel.get(artifact.writer!)
      assert.ok(writer, `artifact writer ${artifact.writer} is absent from plan`)
      const dependencies = new Map(agents.map(agent => [String(agent.label || ''), Array.isArray(agent.dependsOn) ? agent.dependsOn.map(String) : []]))
      const ancestors = new Set<string>()
      const visit = (label: string) => {
        for (const dependency of dependencies.get(label) || []) {
          if (ancestors.has(dependency)) continue
          ancestors.add(dependency)
          visit(dependency)
        }
      }
      visit(artifact.writer!)
      for (const researchAgent of parallelResearch) assert.ok(ancestors.has(String(researchAgent.label)), `output writer does not depend on ${researchAgent.label}`)

      if (taskNeedsFiveResearchAgents) {
        const crossCheckers = agents.filter(agent => {
          const role = String(agent.role || '').toLowerCase()
          const text = `${String(agent.label || '')} ${String(agent.prompt || '')}`
          return /verification|review|synthesis/.test(role) && /交叉验证|核实|真实性|核对|cross.check|verify/i.test(text)
        })
        assert.ok(crossCheckers.length > 0, 'plan has no explicit cross-validation agent')
        const crossCheckAncestors = new Set<string>()
        for (const checker of crossCheckers) {
          const seen = new Set<string>()
          const collect = (label: string) => {
            for (const dependency of dependencies.get(label) || []) {
              if (seen.has(dependency)) continue
              seen.add(dependency)
              crossCheckAncestors.add(dependency)
              collect(dependency)
            }
          }
          collect(String(checker.label || ''))
        }
        for (const researchAgent of parallelResearch) assert.ok(crossCheckAncestors.has(String(researchAgent.label)), `cross-validation does not depend on ${researchAgent.label}`)
      }
    }
    const eventRows = state.workflowEvents.filter(event => event.runId === runId)
    assert.ok(eventRows.some(event => event.type === 'workflow_capability_snapshot'), 'host capability preflight snapshot missing')
    assert.ok(eventRows.some(event => event.type === 'workflow_finished'), 'terminal workflow_finished event missing')
    // The host feeds the workflow result back into the agent loop, which then
    // answers using it, so idle arrives *after* `workflow_final`, not at the
    // same instant. Wait for it instead of asserting on the exact frame.
    await waitFor('ink idle after terminal workflow event', () => state.status === 'idle', 180_000)
    assert.equal(state.status, 'idle', 'Ink UI did not return to idle after terminal workflow event')
    const contract = run?.metadata?.executionContract as { requiredToolEvidence?: Array<{ tool?: string }> ; artifacts?: Array<{ path?: string }> } | undefined
    assert.ok(contract?.requiredToolEvidence?.some(item => item.tool === EXPECTED_MCP_TOOL), `run has no ${EXPECTED_MCP_TOOL} evidence contract`)
    const runArtifacts = (contract?.artifacts || []) as Array<{ path?: string }>
    const outputLike = runArtifacts.filter(item => /\.(html|docx|md|pdf)$/i.test(String(item.path || '')))
    const artifact = (outputLike.length > 0 ? outputLike[outputLike.length - 1] : runArtifacts[runArtifacts.length - 1])?.path
    assert.ok(artifact, 'run has no declared artifact path')
    const workspace = String(run?.metadata?.workspacePath || '')
    assert.ok(workspace, 'run workspacePath missing')
    const resolvedWorkspace = path.resolve(workspace)
    const artifactPortable = artifact.replaceAll('\\', '/')
    assert.equal(path.posix.isAbsolute(artifactPortable), false, 'artifact contract must use a workspace-relative POSIX path')
    assert.equal(path.win32.isAbsolute(artifact), false, 'artifact contract must not use a Windows absolute path')
    const resolvedArtifact = path.resolve(resolvedWorkspace, artifact)
    assert.ok(resolvedArtifact.startsWith(`${resolvedWorkspace}${path.sep}`), 'artifact path escaped workspace')
    assert.equal(String(run?.metadata?.workspacePolicy || ''), 'project-temp-workspace-write-v1', 'workspace policy metadata missing')
    let artifactBytes: number
    if (/\.docx$/i.test(artifact)) {
      const docx = await readFile(resolvedArtifact)
      assert.ok(docx.length > 1000, 'DOCX artifact is empty or too small')
      assert.equal(docx.subarray(0, 4).toString('hex'), '504b0304', 'DOCX artifact is not a ZIP package')
      assert.ok(docx.includes(Buffer.from('[Content_Types].xml')), 'DOCX package lacks content types')
      assert.ok(docx.includes(Buffer.from('word/document.xml')), 'DOCX package lacks the main document part')
      artifactBytes = docx.length
    } else {
      const html = await readFile(resolvedArtifact, 'utf8')
      assert.ok(html.trim().length > 100, 'HTML artifact is empty or too small')
      assert.match(html, /<html[\s>]/i, 'artifact is missing an HTML document root')
      if (EXPECTED_ARTIFACT_PATTERN) assert.match(html, EXPECTED_ARTIFACT_PATTERN, 'artifact does not cover the requested task keywords')
      else if (/刘国梁|liu\s*guoliang/i.test(TASK)) assert.match(html, /刘国梁|Liu\s*Guoliang/i, 'HTML artifact does not cover Liu Guoliang')
      else if (/pathlib|write_text/i.test(TASK)) assert.match(html, /pathlib|write_text/i, 'artifact does not cover the requested API')
      artifactBytes = Buffer.byteLength(html, 'utf8')
    }

    summary.passed = true
    summary.runId = runId
    summary.status = run?.status
    summary.eventTypes = [...new Set(eventRows.map(event => event.type))]
    summary.workspacePath = resolvedWorkspace
    summary.artifactRelativePath = artifactPortable
    summary.resolvedArtifact = resolvedArtifact
    summary.artifact = path.relative(REPO, resolvedArtifact)
    summary.artifactBytes = artifactBytes
    summary.uiReturnedToIdle = state.status === 'idle'
    summary.realTavilyEvidenceContract = true
    summary.researchAgentsWithTavily = expectedResearchCount
  } catch (error) {
    summary.error = String(error).replaceAll(REPO, '<repo>').slice(0, 1200)
    summary.runStatuses = state.workflows.map(run => ({ runId: run.runId, status: run.status, error: run.error }))
    const failedRunId = state.workflows.at(-1)?.runId
    const rejected = state.workflowEvents.find(event => event.runId === failedRunId && event.type === 'workflow_plan_rejected')
    const issues = Array.isArray(rejected?.payload?.issues) ? rejected.payload.issues as Array<{ code?: string }> : []
    summary.validationIssueCodes = issues.map(issue => String(issue.code || 'unknown'))
    summary.events = [...new Set(events.map(event => event.type))]
    summary.bridgeErrors = events.filter(event => event.type === 'error').map(event => ({ code: event.code, message: safeError(event.message) }))
    summary.uiError = safeError(state.error)
    summary.commands = commands.map(command => command.type)
  } finally {
    instance.unmount()
    cursorPark.dispose()
    realClient?.stop()
  }
  console.log(JSON.stringify(summary, null, 2))
  return summary.passed === true ? 0 : 1
}

main().then(code => { process.exitCode = code }).catch(error => {
  console.log(JSON.stringify({ passed: false, error: String(error).slice(0, 1000) }, null, 2))
  process.exitCode = 1
})
