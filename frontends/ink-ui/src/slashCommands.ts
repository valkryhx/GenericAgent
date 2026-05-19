import type { SkillStatus } from './protocol.js'

export type SlashCommand = {
  name: string
  description: string
  kind?: 'builtin' | 'skill'
  source?: string
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

function skillSlashCommands(skills: readonly SkillStatus[]): SlashCommand[] {
  return skills.map(skill => ({
    name: `/${skill.name}`,
    description: skill.description || 'Skill',
    kind: 'skill',
    source: skill.source,
  }))
}

export function slashSuggestions(input: string, skills: readonly SkillStatus[] = []): SlashCommand[] {
  if (!input.startsWith('/') || input.includes('\n')) return []
  return [
    ...SLASH_COMMANDS.map(command => ({ ...command, kind: 'builtin' as const })),
    ...skillSlashCommands(skills),
  ].filter(command => command.name.startsWith(input))
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

export function slashSelectionAction(
  input: string,
  command: SlashCommand,
  trigger: 'enter' | 'tab',
): { type: 'complete' | 'execute'; value: string } {
  if (trigger === 'enter' && command.kind !== 'skill') {
    return { type: 'execute', value: command.name }
  }
  return { type: 'complete', value: completeSlashCommand(command) }
}

export function formatSlashDescription(description: string, maxLength = 72): string {
  const normalized = description.replace(/\s+/g, ' ').trim()
  if (normalized.length <= maxLength) return normalized
  if (maxLength <= 3) return '.'.repeat(Math.max(0, maxLength))
  return `${normalized.slice(0, maxLength - 3).trimEnd()}...`
}

export function formatSlashSuggestionLine(command: SlashCommand, maxLength = 89): string {
  const prefix = command.kind === 'skill'
    ? `${command.name} [skill${command.source ? `: ${command.source}` : ''}] `
    : `${command.name.padEnd(12)} `
  return `${prefix}${formatSlashDescription(command.description, Math.max(0, maxLength - prefix.length))}`
}

export function shouldCompleteSlashCommand(input: string, command: SlashCommand): boolean {
  return input.trim() !== command.name
}
