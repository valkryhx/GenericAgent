export type InputChromeSection = 'error' | 'hint' | 'input' | 'panel' | 'slashSuggestions'

export function inputChromeSections({
  hasError,
  hasPanel,
  hasSlashSuggestions,
}: {
  hasError: boolean
  hasPanel: boolean
  hasSlashSuggestions: boolean
}): InputChromeSection[] {
  // Codex-aligned: composer first, popup (slash/panel) below.
  // Layout::vertical([composer, popup]) in chat_composer.rs.
  return [
    ...(hasError ? ['error' as const] : []),
    'hint',
    'input',
    ...(hasPanel ? ['panel' as const] : hasSlashSuggestions ? ['slashSuggestions' as const] : []),
  ]
}
