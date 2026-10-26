export type InputChromeSection = 'error' | 'hint' | 'input' | 'panel' | 'slashSuggestions' | 'mcpStatus'

export function inputChromeSections({
  hasError,
  hasPanel,
  hasSlashSuggestions,
  hasMcpStatus = false,
  hasAboveInputStatus = false,
}: {
  hasError: boolean
  hasPanel: boolean
  hasSlashSuggestions: boolean
  hasMcpStatus?: boolean
  /** Temporary status output (for example `/status`) remains above the composer. */
  hasAboveInputStatus?: boolean
}): InputChromeSection[] {
  // Persistent errors/status stay above the composer. The normal shortcut hint
  // sits directly under it, while transient popups remain at the very bottom.
  return [
    ...(hasError ? ['error' as const] : []),
    ...(hasAboveInputStatus ? ['hint' as const] : []),
    'input',
    ...(hasAboveInputStatus ? [] : ['hint' as const]),
    ...(hasMcpStatus && !hasPanel && !hasSlashSuggestions ? ['mcpStatus' as const] : []),
    ...(hasPanel ? ['panel' as const] : hasSlashSuggestions ? ['slashSuggestions' as const] : []),
  ]
}
