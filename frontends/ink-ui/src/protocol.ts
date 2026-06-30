export type BridgeCommand =
  | { type: 'submit'; text: string }
  | { type: 'stop' }
  | { type: 'new_session' }
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
  | { type: 'compact'; instructions: string }
  | { type: 'workflow_draft'; script: string }
  | { type: 'workflow_plan'; taskText: string; context?: Record<string, unknown>; autoApprove?: boolean; args?: unknown; timeoutSeconds?: number }
  | { type: 'workflow_approve'; runId: string; args?: unknown; timeoutSeconds?: number }
  | { type: 'workflow_resume'; runId: string; args?: unknown; timeoutSeconds?: number }
  | { type: 'workflow_deny'; runId: string; reason?: string }
  | { type: 'workflow_list' }
  | { type: 'workflow_detail'; runId: string }
  | { type: 'workflow_stop'; runId: string; reason?: string }
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

export type TokenUsage = {
  inputTokens: number
  outputTokens: number
  totalTokens: number
}


export type WorkflowRunStatus =
  | 'draft'
  | 'awaiting_approval'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'killed'
  | 'interrupted'
  | string

export type WorkflowJobStatus =
  | 'registered'
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'killed'
  | 'cached'
  | 'skipped'
  | 'stale'
  | string

export type WorkflowJob = {
  jobId: string
  prompt?: string
  status: WorkflowJobStatus
  phase?: string | null
  resultRef?: string | null
  error?: string | null
  metadata?: Record<string, unknown>
}

export type WorkflowRun = {
  version?: number
  runId: string
  sessionId: string
  status: WorkflowRunStatus
  artifactDir?: string | null
  permissionProfile?: string
  permissionPolicyVersion?: string
  jobs?: WorkflowJob[]
  resultRef?: string | null
  error?: string | null
  metadata?: Record<string, unknown>
}

export type WorkflowEvent = {
  version?: number
  type: string
  runId: string
  sessionId?: string | null
  jobId?: string | null
  sequence: number
  payload?: Record<string, unknown>
}

export type WorkflowPlanAgent = {
  label?: string
  prompt?: string
  dependsOn?: string[]
  role?: string
  schemaRef?: string
  [key: string]: unknown
}

export type WorkflowPlanPhase = {
  title?: string
  agents?: WorkflowPlanAgent[]
  [key: string]: unknown
}

export type WorkflowPlanPayload = {
  taskType?: string
  meta?: { name?: string; description?: string; [key: string]: unknown }
  phases?: WorkflowPlanPhase[]
  schemas?: Record<string, unknown>
  artifacts?: unknown[]
  constraints?: string[]
  [key: string]: unknown
}

export type WorkflowDraftPayload = {
  taskText: string
  classification: Record<string, unknown>
  plan: WorkflowPlanPayload
  validation: Record<string, unknown>
  script?: string
  context?: Record<string, unknown>
}

export type WorkflowProgressEntry = {
  type?: string
  index?: number
  agentId?: string
  jobId?: string
  label?: string | null
  phase?: string | null
  phaseTitle?: string | null
  state?: WorkflowJobStatus
  resultRef?: string | null
  transcriptRef?: string | null
  lastToolName?: string | null
  lastToolSummary?: string | null
  toolCalls?: string[]
  skillToolCalls?: number
  skillLoadEvents?: unknown[]
  allowedTools?: string[]
  deniedTools?: string[]
  loadedSkills?: string[]
  missingRequiredSkills?: string[]
  capability?: Record<string, unknown>
  capabilities?: Record<string, unknown>
  tokenUsage?: Record<string, unknown>
  promptPreview?: string | null
  resultPreview?: string | null
  error?: string | null
}

export type WorkflowProgressPayload = {
  runId: string
  sessionId?: string | null
  status: WorkflowRunStatus
  workflowProgress: WorkflowProgressEntry[]
}

export type BridgeEvent =
  | { type: 'ready'; version: number }
  | { type: 'status'; status: 'idle' | 'running' | 'stopping'; taskId?: number }
  | { type: 'activity'; label: string | null }
  | ({ type: 'token_usage'; taskId?: number } & TokenUsage)
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
  | { type: 'workflow_draft'; run: WorkflowRun }
  | { type: 'workflow_run'; run: WorkflowRun }
  | { type: 'workflow_runs'; runs: WorkflowRun[] }
  | { type: 'workflow_detail'; run: WorkflowRun; script: string; events: WorkflowEvent[]; draft?: WorkflowDraftPayload | null; progress?: WorkflowProgressPayload | null }
  | { type: 'workflow_event'; event: WorkflowEvent }
  | { type: 'workflow_final'; runId: string; result: Record<string, unknown> }
  | { type: 'error'; code: string; message: string; taskId?: number }

export type ChatMessage = {
  id: string
  role: 'user' | 'assistant' | 'system'
  text: string
  done: boolean
  taskId?: number
  localCommand?: 'input' | 'output'
}
