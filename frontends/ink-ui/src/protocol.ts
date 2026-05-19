export type BridgeCommand =
  | { type: 'submit'; text: string }
  | { type: 'stop' }
  | { type: 'list_resume_sessions' }
  | { type: 'resume_session'; id: string }
  | { type: 'resume_session_index'; index: number }
  | { type: 'rewind'; taskId: number }
  | { type: 'mcp_status' }
  | { type: 'mcp_reconnect'; server: string }
  | { type: 'mcp_enable'; server: string }
  | { type: 'mcp_disable'; server: string }
  | { type: 'model_status' }
  | { type: 'model_switch'; selector: string }
  | { type: 'skill_status' }
  | { type: 'skill_invoke'; skill: string; args: string }
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

export type McpServerStatus = {
  name: string
  status: 'connected' | 'failed' | 'disabled' | 'pending' | string
  transport: string
  disabled: boolean
  error: string
  tool_count: number
}

export type McpToolStatus = {
  type: 'function'
  function: {
    name: string
    description: string
    parameters: Record<string, unknown>
  }
}

export type ModelStatus = {
  index: number
  name: string
  current: boolean
}

export type SkillStatus = {
  name: string
  description: string
  source: string
  path: string
}

export type BridgeEvent =
  | { type: 'ready'; version: number }
  | { type: 'status'; status: 'idle' | 'running' | 'stopping'; taskId?: number }
  | { type: 'user'; taskId: number; text: string }
  | { type: 'assistant_delta'; taskId: number; text: string }
  | { type: 'assistant_done'; taskId: number; text: string }
  | { type: 'system'; text: string }
  | { type: 'local_command_input'; text: string }
  | { type: 'local_command_output'; text: string }
  | { type: 'clear' }
  | { type: 'resume_sessions'; sessions: ResumeSession[] }
  | { type: 'history_replace'; messages: HistoryMessage[] }
  | { type: 'rewind_done'; taskId: number; text: string }
  | { type: 'mcp_status'; config_path: string; servers: McpServerStatus[]; tools: McpToolStatus[]; errors: Record<string, string> }
  | { type: 'model_status'; models: ModelStatus[] }
  | { type: 'model_switch_result'; ok: boolean; message: string }
  | { type: 'skill_status'; skills: SkillStatus[] }
  | { type: 'error'; code: string; message: string; taskId?: number }

export type ChatMessage = {
  id: string
  role: 'user' | 'assistant' | 'system'
  text: string
  done: boolean
  taskId?: number
  localCommand?: 'input' | 'output'
}
