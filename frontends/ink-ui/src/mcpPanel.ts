import type { BridgeEvent, McpServerStatus, McpToolStatus } from './protocol.js'

export type McpPanelState = {
  loading: boolean
  selected: number
  configPath: string
  servers: McpServerStatus[]
  tools: McpToolStatus[]
  errors: Record<string, string>
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
