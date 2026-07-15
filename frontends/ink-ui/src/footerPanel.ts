export type FooterPanel = {
  type: 'help' | 'status' | 'model'
  text: string
}

export function statusPanelText(status: string, messageCount: number): string {
  return `status=${status} messages=${messageCount}`
}

export function modelSwitchPanelText(message: string): string {
  return message
}

// Rows the panel section reserves for a footer panel. Status renders inline in
// the hint row (0 panel rows); help/model render in the panel section below the
// input as text.split('\n') lines plus a trailing "Esc close" row, matching
// FooterPanelView. Keeping this in sync with panelRows prevents the help panel
// from overlapping the message viewport (its rows were previously unreserved).
export function footerPanelRows(panel: FooterPanel): number {
  if (panel.type === 'status') return 0
  return panel.text.split('\n').length + 1
}
