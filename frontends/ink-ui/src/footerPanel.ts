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
