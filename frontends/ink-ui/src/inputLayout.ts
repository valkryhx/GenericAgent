export type InputChromeSection = 'error' | 'hint' | 'topDivider' | 'input' | 'panel' | 'slashSuggestions' | 'bottomDivider'

export function inputChromeSections({
  hasError,
  hasPanel,
  hasSlashSuggestions,
}: {
  hasError: boolean
  hasPanel: boolean
  hasSlashSuggestions: boolean
}): InputChromeSection[] {
  return [
    ...(hasError ? ['error' as const] : []),
    'hint',
    'topDivider',
    'input',
    'bottomDivider',
    ...(hasPanel ? ['panel' as const] : hasSlashSuggestions ? ['slashSuggestions' as const] : []),
  ]
}
