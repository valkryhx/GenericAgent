import type { BridgeEvent, McpServerStatus, McpToolStatus } from './protocol.js'

export type McpPanelState = {
  loading: boolean
  selected: number
  configPath: string
  servers: McpServerStatus[]
  tools: McpToolStatus[]
  errors: Record<string, string>
}

export type VisibleMcpServerRow = {
  index: number
  selected: boolean
}

export function loadingMcpPanel(): McpPanelState {
  return { loading: true, selected: 0, configPath: '', servers: [], tools: [], errors: {} }
}

export function panelFromMcpStatus(event: Extract<BridgeEvent, { type: 'mcp_status' }>): McpPanelState {
  return {
    loading: false,
    selected: 0,
    configPath: event.config_path,
    servers: event.servers,
    tools: event.tools,
    errors: event.errors,
  }
}

export function moveMcpSelection(panel: McpPanelState, delta: number): McpPanelState {
  if (panel.servers.length === 0) return panel
  const selected = Math.max(0, Math.min(panel.servers.length - 1, panel.selected + delta))
  return { ...panel, selected }
}

export function visibleMcpServerRows(panel: McpPanelState, maxRows: number): VisibleMcpServerRow[] {
  const size = panel.servers.length
  if (size <= 0) return []
  const limit = Math.max(1, Math.min(size, Math.floor(maxRows)))
  const selected = Math.max(0, Math.min(size - 1, panel.selected))
  let start = selected - Math.floor(limit / 2)
  start = Math.max(0, Math.min(size - limit, start))
  return Array.from({ length: limit }, (_, offset) => {
    const index = start + offset
    return { index, selected: index === selected }
  })
}

export function mcpPanelRows(panel: McpPanelState): number {
  const selected = panel.servers[panel.selected]
  const serverRows = Math.min(panel.servers.length, 5)
  const statusRows = (panel.configPath ? 1 : 0)
    + (panel.loading ? 1 : 0)
    + (!panel.loading && panel.servers.length === 0 ? 1 : 0)
  const selectedRows = selected ? 1 + (panel.errors[selected.name] ? 1 : 0) : 0
  const toolRows = selected ? Math.min(mcpToolsForServer(panel, selected.name).length, 2) : 0
  return 1 + statusRows + serverRows + selectedRows + toolRows + 1
}

export function mcpToolsForServer(panel: McpPanelState, serverName: string): McpToolStatus[] {
  const prefix = `[MCP: ${serverName}/`
  return panel.tools.filter(tool => tool.function.description.startsWith(prefix))
}

export function mcpStatusIcon(status: string): string {
  if (status === 'connected') return '✓'
  if (status === 'failed') return '✕'
  if (status === 'disabled') return '○'
  if (status === 'pending') return '○'
  return '?'
}

export function mcpStatusColor(status: string): string {
  if (status === 'connected') return 'green'
  if (status === 'failed') return 'red'
  if (status === 'disabled') return 'gray'
  return 'yellow'
}
