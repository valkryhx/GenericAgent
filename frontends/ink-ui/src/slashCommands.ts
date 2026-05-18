export type SlashCommand = {
  name: string
  description: string
}

export const SLASH_COMMANDS: SlashCommand[] = [
  { name: '/help', description: 'Show available commands' },
  { name: '/status', description: 'Show frontend and bridge status' },
  { name: '/mcp', description: 'Show and manage MCP servers' },
  { name: '/model', description: 'Show and switch AI models' },
  { name: '/llm', description: 'Alias for /model' },
  { name: '/clear', description: 'Clear the visible transcript' },
  { name: '/stop', description: 'Stop the current backend task' },
  { name: '/resume', description: 'Pick a previous conversation' },
  { name: '/rewind', description: 'Rewind to a previous user message' },
  { name: '/continue', description: 'Alias for /resume' },
  { name: '/checkpoint', description: 'Alias for /rewind' },
  { name: '/exit', description: 'Exit GenericAgent Ink' },
  { name: '/quit', description: 'Exit GenericAgent Ink' },
]

export function slashSuggestions(input: string): SlashCommand[] {
  if (!input.startsWith('/') || input.includes('\n')) return []
  return SLASH_COMMANDS.filter(command => command.name.startsWith(input))
}

export function moveSlashSelection(selected: number, delta: number, suggestions: readonly SlashCommand[]): number {
  if (suggestions.length === 0) return 0
  return Math.max(0, Math.min(suggestions.length - 1, selected + delta))
}

export function visibleSlashSuggestions(
  suggestions: readonly SlashCommand[],
  selected: number,
  maxVisible = 5,
): { startIndex: number; items: SlashCommand[] } {
  if (suggestions.length <= maxVisible) return { startIndex: 0, items: [...suggestions] }
  const half = Math.floor(maxVisible / 2)
  const startIndex = Math.max(0, Math.min(selected - half, suggestions.length - maxVisible))
  return { startIndex, items: suggestions.slice(startIndex, startIndex + maxVisible) }
}

export function completeSlashCommand(command: SlashCommand): string {
  return `${command.name} `
}

export function shouldCompleteSlashCommand(input: string, command: SlashCommand): boolean {
  return input.trim() !== command.name
}
