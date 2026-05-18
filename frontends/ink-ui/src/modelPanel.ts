import type { BridgeEvent, ModelStatus } from './protocol.js'

export type ModelPanelState = {
  models: ModelStatus[]
  selected: number
}

export function panelFromModelStatus(event: Extract<BridgeEvent, { type: 'model_status' }>): ModelPanelState {
  const current = event.models.findIndex(model => model.current)
  return { models: event.models, selected: Math.max(0, current) }
}

export function moveModelSelection(selected: number, delta: number, total: number): number {
  if (total <= 0) return 0
  return Math.max(0, Math.min(total - 1, selected + delta))
}

export function shouldApplyModelStatus(requested: boolean, panelOpen: boolean): boolean {
  return requested || panelOpen
}
