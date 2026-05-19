import type { InputDecision } from './inputController.js'

export function commandTextForLocalDecision(decision: Pick<InputDecision, 'action' | 'command'>): string | null {
  const action = decision.action?.type
  if (action === 'help') return '/help'
  if (action === 'status') return '/status'
  if (action === 'clear') return '/clear'
  if (action === 'open_mcp') return '/mcp'
  if (action === 'open_model') return '/model'
  if (action === 'open_resume') return '/resume'
  if (action === 'open_rewind') return '/rewind'

  const command = decision.command
  if (!command) return null
  if (command.type === 'mcp_reconnect') return `/mcp reconnect ${command.server}`
  if (command.type === 'mcp_enable') return `/mcp enable ${command.server}`
  if (command.type === 'mcp_disable') return `/mcp disable ${command.server}`
  if (command.type === 'model_status') return '/model ?'
  if (command.type === 'model_switch') return `/model ${command.selector}`
  if (command.type === 'resume_session_index') return `/resume ${command.index}`
  if (command.type === 'rewind') return `/rewind ${command.taskId}`
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
