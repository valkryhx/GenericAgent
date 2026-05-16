export type BridgeCommand =
  | { type: 'submit'; text: string }
  | { type: 'stop' }
  | { type: 'list_resume_sessions' }
  | { type: 'resume_session'; id: string }
  | { type: 'resume_session_index'; index: number }
  | { type: 'rewind'; taskId: number }
  | { type: 'shutdown' }

export type ResumeSession = {
  id: string
  mtime: number
  preview: string
  rounds: number
}

export type HistoryMessage = {
  role: 'user' | 'assistant' | 'system'
  text: string
  taskId?: number
}

export type BridgeEvent =
  | { type: 'ready'; version: number }
  | { type: 'status'; status: 'idle' | 'running' | 'stopping'; taskId?: number }
  | { type: 'user'; taskId: number; text: string }
  | { type: 'assistant_delta'; taskId: number; text: string }
  | { type: 'assistant_done'; taskId: number; text: string }
  | { type: 'system'; text: string }
  | { type: 'clear' }
  | { type: 'resume_sessions'; sessions: ResumeSession[] }
  | { type: 'history_replace'; messages: HistoryMessage[] }
  | { type: 'rewind_done'; taskId: number; text: string }
  | { type: 'error'; code: string; message: string; taskId?: number }

export type ChatMessage = {
  id: string
  role: 'user' | 'assistant' | 'system'
  text: string
  done: boolean
  taskId?: number
}
