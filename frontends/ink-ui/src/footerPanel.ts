export type FooterPanel = {
  type: 'help' | 'status'
  text: string
}

export function statusPanelText(status: string, messageCount: number): string {
  return `status=${status} messages=${messageCount}`
}
