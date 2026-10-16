export type InputChromeSection = 'error' | 'hint' | 'input' | 'panel' | 'slashSuggestions' | 'mcpStatus'

export function inputChromeSections({
  hasError,
  hasPanel,
  hasSlashSuggestions,
  hasMcpStatus = false,
}: {
  hasError: boolean
  hasPanel: boolean
  hasSlashSuggestions: boolean
  hasMcpStatus?: boolean
}): InputChromeSection[] {
  // Codex-aligned: composer first, popup (slash/panel) below.
  // Layout::vertical([composer, popup]) in chat_composer.rs.
  return [
    ...(hasError ? ['error' as const] : []),
    'hint',
    'input',
    ...(hasMcpStatus && !hasPanel && !hasSlashSuggestions ? ['mcpStatus' as const] : []),
    ...(hasPanel ? ['panel' as const] : hasSlashSuggestions ? ['slashSuggestions' as const] : []),
  ]
}
