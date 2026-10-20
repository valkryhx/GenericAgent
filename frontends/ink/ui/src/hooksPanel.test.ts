import test from 'node:test'
import assert from 'node:assert/strict'
import {
  hooksPanelCapturesKey,
  hooksPanelCommandForKey,
  hooksPanelFromStatus,
  hooksPanelLines,
  moveHooksSelection,
} from './hooksPanel.js'

const payload = {
  files: ['D:/repo/.genericagent/settings.json'],
  entries: 3,
  handlers: 3,
  handlerTypes: ['command', 'http', 'mcp_tool'],
  configured: [
    { event: 'Stop', matcher: '', type: 'command', source: 'project', path: 'D:/repo/.genericagent/settings.json', timeout: 10 },
    { event: 'PreToolUse', matcher: 'code_run', type: 'http', source: 'local', path: 'D:/repo/.genericagent/settings.local.json', timeout: 30 },
    { event: 'TeammateIdle', matcher: '', type: 'mcp_tool', source: 'user', path: 'C:/Users/x/.genericagent/hooks.json', timeout: 30 },
  ],
  events: { Stop: 'integrated', WorktreeCreate: 'contract-only' },
  issues: [],
  recent: [
    { hook_run_id: 'h1', event: 'Stop', source: 'project', handler_type: 'command', status: 'success', duration_ms: 42 },
    { hook_run_id: 'h2', event: 'PreToolUse', source: 'local', handler_type: 'http', status: 'error', duration_ms: 1000, error: 'timeout' },
  ],
}

test('hooksPanelFromStatus normalizes missing arrays and starts at first configured handler', () => {
  const state = hooksPanelFromStatus(payload)
  assert.equal(state.selected, 0)
  assert.equal(state.payload.handlers, 3)
  assert.deepEqual(hooksPanelFromStatus({ ...payload, configured: undefined as never }).payload.configured, [])
})

test('hooksPanelLines renders files, handler matrix, integration coverage and audit rows', () => {
  const body = hooksPanelLines(hooksPanelFromStatus(payload)).join('\n')
  assert.match(body, /Hooks  3 handler/)
  assert.match(body, /\.genericagent\/settings\.json/)
  assert.match(body, /Stop\s+command\s+project/)
  assert.match(body, /PreToolUse\s+http\s+local matcher=code_run/)
  assert.match(body, /integrated \(1\): Stop/)
  assert.match(body, /contract-only \(1\): WorktreeCreate/)
  assert.match(body, /success\s+Stop/)
  assert.match(body, /error\s+PreToolUse/)
  assert.match(body, /r refresh/)
})

test('hooksPanelLines renders empty state without crashing', () => {
  const body = hooksPanelLines(hooksPanelFromStatus({
    files: [], entries: 0, handlers: 0, handlerTypes: [], configured: [], events: {}, issues: [], recent: [],
  })).join('\n')
  assert.match(body, /no \.genericagent hook files loaded/)
  assert.match(body, /\(none\)/)
})

test('hook panel selection wraps and refresh maps to hooks_status', () => {
  const state = hooksPanelFromStatus(payload)
  assert.equal(moveHooksSelection(state, 1).selected, 1)
  assert.equal(moveHooksSelection(state, -1).selected, 2)
  assert.deepEqual(hooksPanelCommandForKey(state, { downArrow: true }, ''), {
    panel: { ...state, selected: 1 },
  })
  assert.deepEqual(hooksPanelCommandForKey(state, {}, 'r'), { command: { type: 'hooks_status' } })
  assert.equal(hooksPanelCapturesKey(state, {}, 'r'), true)
  assert.equal(hooksPanelCapturesKey(state, {}, 'x'), false)
})
