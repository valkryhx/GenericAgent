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
  return [
    ...(hasError ? ['error' as const] : []),
    ...(hasPanel ? ['panel' as const] : hasSlashSuggestions ? ['slashSuggestions' as const] : []),
    'hint',
    'input',
  ]
}
