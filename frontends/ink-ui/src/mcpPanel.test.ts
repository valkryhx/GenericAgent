import test from 'node:test'
import assert from 'node:assert/strict'
import {
  mcpStatusColor,
  mcpStatusIcon,
  mcpToolsForServer,
  moveMcpSelection,
  panelFromMcpStatus,
} from './mcpPanel.js'

const statusEvent = {
  type: 'mcp_status' as const,
  config_path: 'mcp.json',
  servers: [
    { name: 'demo', status: 'connected', transport: 'stdio', disabled: false, error: '', tool_count: 2 },
    { name: 'bad', status: 'failed', transport: 'stdio', disabled: false, error: 'boom', tool_count: 0 },
  ],
  tools: [
    { type: 'function' as const, function: { name: 'mcp__demo__echo', description: '[MCP: demo/echo] Echo', parameters: {} } },
    { type: 'function' as const, function: { name: 'mcp__bad__noop', description: '[MCP: bad/noop] Noop', parameters: {} } },
  ],
  errors: { bad: 'boom' },
}

test('mcp status helpers map statuses to Claude-style symbols', () => {
  assert.equal(mcpStatusIcon('connected'), '✓')
  assert.equal(mcpStatusIcon('failed'), '✕')
  assert.equal(mcpStatusIcon('disabled'), '○')
  assert.equal(mcpStatusIcon('pending'), '○')
  assert.equal(mcpStatusColor('connected'), 'green')
  assert.equal(mcpStatusColor('failed'), 'red')
  assert.equal(mcpStatusColor('disabled'), 'gray')
  assert.equal(mcpStatusColor('pending'), 'yellow')
})

test('panelFromMcpStatus and moveMcpSelection keep selected server in bounds', () => {
  const panel = panelFromMcpStatus(statusEvent)

  assert.equal(panel.loading, false)
  assert.equal(panel.configPath, 'mcp.json')
  assert.equal(moveMcpSelection(panel, 1).selected, 1)
  assert.equal(moveMcpSelection({ ...panel, selected: 1 }, 1).selected, 1)
  assert.equal(moveMcpSelection(panel, -1).selected, 0)
})

test('mcpToolsForServer filters tools by MCP description prefix', () => {
  const panel = panelFromMcpStatus(statusEvent)

  assert.deepEqual(mcpToolsForServer(panel, 'demo').map(tool => tool.function.name), ['mcp__demo__echo'])
  assert.deepEqual(mcpToolsForServer(panel, 'missing'), [])
})
