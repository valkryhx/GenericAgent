export type LayoutMetricInput = {
  rows?: number
  columns?: number
  hasActivity: boolean
  hasError: boolean
  hasPanel: boolean
  hasSlashSuggestions: boolean
  panelRows?: number
  inputRows?: number
}

export type LayoutMetrics = {
  rows: number
  columns: number
  headerRows: number
  bottomRows: number
  messageRows: number
}

export function terminalRows(rows?: number, fallback = 24): number {
  return Math.max(1, Math.floor(rows || fallback))
}

export function terminalColumns(columns?: number, fallback = 80): number {
  return Math.max(1, Math.floor(columns || fallback))
}

export function computeLayoutMetrics(input: LayoutMetricInput): LayoutMetrics {
  const rows = Math.max(1, terminalRows(input.rows) - 1)
  const columns = terminalColumns(input.columns)
  const headerRows = 1
  const inputRows = Math.max(1, Math.floor(input.inputRows ?? 1))
  const baseBottomRows = 3 + inputRows
  const activityRows = 1
  const errorRows = input.hasError ? 1 : 0
  const requestedPanelRows = input.hasPanel || input.hasSlashSuggestions
    ? Math.max(0, input.panelRows ?? (input.hasSlashSuggestions ? 6 : 8))
    : 0
  const availablePanelRows = Math.max(0, rows - headerRows - baseBottomRows - activityRows - errorRows - 1)
  const maxPanelRows = Math.max(0, Math.min(12, availablePanelRows))
  const panelRows = Math.min(requestedPanelRows, maxPanelRows)
  const requestedBottomRows = baseBottomRows + activityRows + errorRows + panelRows
  const bottomRows = Math.min(requestedBottomRows, Math.max(1, rows - headerRows - 1))
  const messageRows = Math.max(1, rows - headerRows - bottomRows)

  return { rows, columns, headerRows, bottomRows, messageRows }
}
