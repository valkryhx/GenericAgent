import type { SkillStatus } from './protocol.js'

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

export function skillsPanelText(skills: readonly SkillStatus[]): string {
  if (skills.length === 0) return 'No skills available.'
  const lines = [`Skills (${skills.length}) - context cost`]
  for (const skill of skills) {
    const state = skill.state ?? 'on'
    const model = skill.modelVisible === false ? 'model:hidden' : 'model:on'
    const user = skill.userInvocable === false ? 'user:hidden' : 'user:on'
    const alwaysOn = skill.alwaysOnChars ?? 0
    const onInvoke = skill.onInvokeChars ?? 0
    lines.push(`- ${skill.name} [${state}; ${model}; ${user}; always ${alwaysOn} chars; invoke ${onInvoke} chars]`)
  }
  return lines.join('\n')
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
