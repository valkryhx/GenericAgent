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
  assert.equal(commandTextForLocalDecision({ action: { type: 'open_mcp' } }), '/mcp')
  assert.equal(commandTextForLocalDecision({ command: { type: 'mcp_reconnect', server: 'demo' } }), '/mcp reconnect demo')
  assert.equal(commandTextForLocalDecision({ command: { type: 'model_switch', selector: '2' } }), '/model 2')
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
