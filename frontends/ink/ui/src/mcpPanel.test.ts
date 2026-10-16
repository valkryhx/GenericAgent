import test from 'node:test'
import assert from 'node:assert/strict'
import {
  mcpStatusColor,
  mcpStatusIcon,
  mcpPanelRows,
  mcpStartupStatusRows,
  mcpToolsForServer,
  visibleMcpServerRows,
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
  assert.equal(mcpStatusIcon('connecting'), '◌')
  assert.equal(mcpStatusIcon('disabled'), '○')
  assert.equal(mcpStatusIcon('pending'), '○')
  assert.equal(mcpStatusColor('connected'), 'green')
  assert.equal(mcpStatusColor('failed'), 'red')
  assert.equal(mcpStatusColor('connecting'), 'yellow')
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

test('visibleMcpServerRows keeps selected server visible in a capped panel', () => {
  const panel = panelFromMcpStatus({
    ...statusEvent,
    servers: ['fetch', 'tavily', 'sequential-thinking', 'memory', 'exa'].map((name, index) => ({
      name,
      status: index === 1 ? 'connected' : 'failed',
      transport: index === 1 ? 'http' : 'stdio',
      disabled: false,
      error: index === 1 ? '' : 'boom',
      tool_count: index === 1 ? 5 : 0,
    })),
  })

  const rows = visibleMcpServerRows({ ...panel, selected: 2 }, 3)

  assert.deepEqual(rows.map(row => panel.servers[row.index]?.name), ['tavily', 'sequential-thinking', 'memory'])
  assert.equal(rows.some(row => row.selected && panel.servers[row.index]?.name === 'sequential-thinking'), true)
})

test('mcp server list can display every configured server when the viewport allows it', () => {
  const panel = panelFromMcpStatus({
    ...statusEvent,
    servers: ['fetch', 'tavily', 'exa', 'memory', 'sequential-thinking', 'context7'].map(name => ({
      name,
      status: 'connected',
      transport: 'http',
      disabled: false,
      error: '',
      tool_count: 2,
    })),
  })

  assert.deepEqual(visibleMcpServerRows(panel, panel.servers.length).map(row => panel.servers[row.index]?.name), [
    'fetch', 'tavily', 'exa', 'memory', 'sequential-thinking', 'context7',
  ])
  assert.equal(mcpPanelRows(panel), 10)
})

test('mcp startup status rows show per-server startup progress and final tool totals', () => {
  const pendingRows = mcpStartupStatusRows({
    loading: true,
    servers: [
      { name: 'exa', status: 'connected', tool_count: 3 },
      { name: 'context7', status: 'connecting', tool_count: 0 },
      { name: 'tavily', status: 'pending', tool_count: 0 },
    ],
    tools: Array.from({ length: 3 }, (_, index) => ({
      type: 'function' as const,
      function: { name: `mcp__exa__tool${index}`, description: '', parameters: {} },
    })),
  })
  assert.match(pendingRows[0] ?? '', /MCP connecting in the background.*1\/3.*3 tools/)
  // Startup must not read as a gate: the first turn is answerable while
  // servers are still connecting.
  assert.match(pendingRows[0] ?? '', /you can type now/)
  assert.match(pendingRows.join('\n'), /◌ context7.*connecting/)
  assert.match(pendingRows.join('\n'), /tavily.*pending/)

  const readyRows = mcpStartupStatusRows({
    loading: false,
    servers: [{ name: 'context7', status: 'connected', tool_count: 8 }],
    tools: Array.from({ length: 8 }, (_, index) => ({
      type: 'function' as const,
      function: { name: `mcp__context7__tool${index}`, description: '', parameters: {} },
    })),
  })
  assert.equal(readyRows.length, 1)
  assert.match(readyRows[0] ?? '', /MCP ready.*1 server.*8 tools/)
})

test('mcpPanelRows requests enough height for five servers and selected details', () => {
  const panel = panelFromMcpStatus({
    ...statusEvent,
    servers: ['fetch', 'tavily', 'sequential-thinking', 'memory', 'exa'].map((name, index) => ({
      name,
      status: index === 2 ? 'failed' : 'connected',
      transport: index === 1 ? 'http' : 'stdio',
      disabled: false,
      error: index === 2 ? 'boom' : '',
      tool_count: index === 1 ? 5 : 0,
    })),
    errors: { 'sequential-thinking': 'boom' },
  })

  assert.equal(mcpPanelRows({ ...panel, selected: 2 }), 10)
})
