import type { BridgeEvent } from './protocol.js'

export function pendingLocalCommandAfterBridgeEvent(
  pendingCommand: string | null,
  event: BridgeEvent,
): string | null {
  if (event.type === 'local_command_output') return null
  return pendingCommand
}
