export type BridgeCommand =
  | { type: 'submit'; text: string }
  | { type: 'stop' }
  | { type: 'shutdown' }

export type BridgeEvent =
  | { type: 'ready'; version: number }
  | { type: 'status'; status: 'idle' | 'running' | 'stopping'; taskId?: number }
  | { type: 'user'; taskId: number; text: string }
  | { type: 'assistant_delta'; taskId: number; text: string }
  | { type: 'assistant_done'; taskId: number; text: string }
  | { type: 'error'; code: string; message: string; taskId?: number }

export type ChatMessage = {
  id: string
  role: 'user' | 'assistant' | 'system'
  text: string
  done: boolean
}
