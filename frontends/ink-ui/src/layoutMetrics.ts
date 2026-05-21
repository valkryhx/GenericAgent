export type LayoutMetricInput = {
  rows?: number
  columns?: number
  hasActivity: boolean
  hasError: boolean
  hasPanel: boolean
  hasSlashSuggestions: boolean
  panelRows?: number
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
  const rows = terminalRows(input.rows)
  const columns = terminalColumns(input.columns)
  const headerRows = 1
  const baseBottomRows = 4
  const activityRows = input.hasActivity ? 1 : 0
  const errorRows = input.hasError ? 1 : 0
  const requestedPanelRows = input.hasPanel || input.hasSlashSuggestions
    ? Math.max(0, input.panelRows ?? (input.hasSlashSuggestions ? 6 : 8))
    : 0
  const availablePanelRows = Math.max(0, rows - headerRows - baseBottomRows - activityRows - errorRows - 1)
  const maxPanelRows = Math.max(0, Math.min(12, availablePanelRows))
  const panelRows = Math.min(requestedPanelRows, maxPanelRows)
  const bottomRows = baseBottomRows + activityRows + errorRows + panelRows
  const messageRows = Math.max(1, rows - headerRows - bottomRows)

  return { rows, columns, headerRows, bottomRows, messageRows }
}
