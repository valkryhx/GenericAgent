import test from 'node:test'
import assert from 'node:assert/strict'
import {
  clearLocalCommandOutput,
  commandTextForLocalDecision,
  dismissedLocalCommandOutput,
  localCommandResultOutput,
} from './localCommandTranscript.js'

test('commandTextForLocalDecision returns display text for local actions and commands only', () => {
  assert.equal(commandTextForLocalDecision({ action: { type: 'help' } }), '/help')
  assert.equal(commandTextForLocalDecision({ action: { type: 'status' } }), null)
  assert.equal(commandTextForLocalDecision({ action: { type: 'open_mcp' } }), '/mcp')
  assert.equal(commandTextForLocalDecision({ action: { type: 'open_resume' } }), null)
  assert.equal(commandTextForLocalDecision({ action: { type: 'open_rewind' } }), null)
  assert.equal(commandTextForLocalDecision({ command: { type: 'mcp_reconnect', server: 'demo' } }), '/mcp reconnect demo')
  assert.equal(commandTextForLocalDecision({ command: { type: 'model_switch', selector: '2' } }), '/model 2')
  assert.equal(commandTextForLocalDecision({ command: { type: 'compact', instructions: 'keep decisions' } }), '/compact keep decisions')
  assert.equal(commandTextForLocalDecision({ command: { type: 'compact', instructions: '' } }), '/compact')
  assert.equal(commandTextForLocalDecision({ command: { type: 'workflow_list' } }), '/workflows')
  assert.equal(commandTextForLocalDecision({ command: { type: 'workflow_plan', taskText: '调研 workflow UI', autoApprove: true } }), '/workflow 调研 workflow UI')
  assert.equal(commandTextForLocalDecision({ command: { type: 'workflow_plan', taskText: '调研 workflow UI', autoApprove: true, timeoutSeconds: 120 } }), '/workflow --timeout 120 调研 workflow UI')
  assert.equal(commandTextForLocalDecision({ command: { type: 'workflow_detail', runId: 'wf_demo' } }), '/workflow detail wf_demo')
  assert.equal(commandTextForLocalDecision({ command: { type: 'workflow_resume', runId: 'wf_demo' } }), '/workflow resume wf_demo')
  assert.equal(commandTextForLocalDecision({ command: { type: 'workflow_stop', runId: 'wf_demo', reason: 'user stop' } }), '/workflow stop wf_demo user stop')
  assert.equal(commandTextForLocalDecision({ command: { type: 'resume_session_index', index: 2 } }), null)
  assert.equal(commandTextForLocalDecision({ command: { type: 'rewind', taskId: 2 } }), null)
  assert.equal(commandTextForLocalDecision({ command: { type: 'submit', text: 'hello' } }), null)
  assert.equal(commandTextForLocalDecision({ command: { type: 'skill_invoke', skill: 'imagegen', args: 'cat' } }), null)
})

test('dismissedLocalCommandOutput follows Claude-style dialog messages', () => {
  assert.equal(dismissedLocalCommandOutput('/help'), 'Help dialog dismissed')
  assert.equal(dismissedLocalCommandOutput('/mcp'), 'MCP dialog dismissed')
  assert.equal(dismissedLocalCommandOutput('/model'), 'Model dialog dismissed')
  assert.equal(dismissedLocalCommandOutput('/resume'), 'Resume dialog dismissed')
  assert.equal(dismissedLocalCommandOutput('/rewind'), 'Rewind dialog dismissed')
})

test('localCommandResultOutput formats direct local command results', () => {
  assert.equal(localCommandResultOutput('/status', 'idle', 3), 'Status: idle - 3 messages')
  assert.equal(clearLocalCommandOutput(), 'Display cleared')
})
