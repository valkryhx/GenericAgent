import type { InputDecision } from './inputController.js'

export function commandTextForLocalDecision(decision: Pick<InputDecision, 'action' | 'command'>): string | null {
  const action = decision.action?.type
  if (action === 'help') return '/help'
  if (action === 'clear') return '/clear'
  if (action === 'open_mcp') return '/mcp'
  if (action === 'open_model') return '/model'

  const command = decision.command
  if (!command) return null
  if (command.type === 'mcp_reconnect') return `/mcp reconnect ${command.server}`
  if (command.type === 'mcp_enable') return `/mcp enable ${command.server}`
  if (command.type === 'mcp_disable') return `/mcp disable ${command.server}`
  if (command.type === 'model_status') return '/model ?'
  if (command.type === 'model_switch') return `/model ${command.selector}`
  if (command.type === 'compact') return `/compact ${command.instructions}`.trimEnd()
  if (command.type === 'workflow_list') return '/workflows'
  if (command.type === 'workflow_detail') return `/workflow detail ${command.runId}`
  if (command.type === 'workflow_approve') return `/workflow approve ${command.runId}`
  if (command.type === 'workflow_resume') return `/workflow resume ${command.runId}`
  if (command.type === 'workflow_deny') return `/workflow deny ${command.runId}${command.reason ? ` ${command.reason}` : ''}`
  if (command.type === 'workflow_stop') return `/workflow stop ${command.runId}${command.reason ? ` ${command.reason}` : ''}`
  if (command.type === 'stop') return '/stop'
  return null
}

export function dismissedLocalCommandOutput(commandText: string): string {
  const commandName = commandText.trim().split(/\s+/, 1)[0]?.replace(/^\//, '') || 'Command'
  const labels: Record<string, string> = {
    help: 'Help',
    mcp: 'MCP',
    model: 'Model',
    llm: 'Model',
    resume: 'Resume',
    continue: 'Resume',
    rewind: 'Rewind',
    checkpoint: 'Rewind',
    workflows: 'Workflows',
    workflow: 'Workflow',
  }
  return `${labels[commandName] ?? commandName} dialog dismissed`
}

export function localCommandResultOutput(commandText: string, status: string, messageCount: number): string {
  if (commandText === '/status') return `Status: ${status} - ${messageCount} messages`
  return 'Command completed'
}

export function clearLocalCommandOutput(): string {
  return 'Display cleared'
}
