import type { BridgeEvent, ChatMessage, TokenUsage, WorkflowDraftPayload, WorkflowEvent, WorkflowProgressPayload, WorkflowRun } from './protocol.js'

export type AppState = {
  status: 'connecting' | 'idle' | 'running' | 'stopping'
  activityLabel: string | null
  tokenUsage: TokenUsage | null
  messages: ChatMessage[]
  error: string | null
  workflows: WorkflowRun[]
  workflowEvents: WorkflowEvent[]
  workflowDetails: Record<string, { run: WorkflowRun; script: string; events: WorkflowEvent[]; draft?: WorkflowDraftPayload | null; progress?: WorkflowProgressPayload | null }>
  workflowResults: Record<string, Record<string, unknown>>
}

export const initialState: AppState = {
  status: 'connecting',
  activityLabel: null,
  tokenUsage: null,
  messages: [],
  error: null,
  workflows: [],
  workflowEvents: [],
  workflowDetails: {},
  workflowResults: {},
}

export function applyBridgeEvent(state: AppState, event: BridgeEvent): AppState {
  if (event.type === 'ready') {
    return { ...state, status: 'idle', activityLabel: null, tokenUsage: null, error: null }
  }
  if (event.type === 'status') {
    return { ...state, status: event.status, activityLabel: event.status === 'idle' ? null : state.activityLabel, tokenUsage: event.status === 'running' ? null : state.tokenUsage, error: null }
  }
  if (event.type === 'activity') {
    return { ...state, activityLabel: event.label, error: null }
  }
  if (event.type === 'token_usage') {
    return { ...state, tokenUsage: { inputTokens: event.inputTokens, outputTokens: event.outputTokens, totalTokens: event.totalTokens }, error: null }
  }
  if (event.type === 'error') {
    return { ...state, error: event.message }
  }

  if (event.type === 'workflow_draft' || event.type === 'workflow_run') {
    const existing = state.workflows.filter(run => run.runId !== event.run.runId)
    return { ...state, workflows: [...existing, event.run], error: null }
  }
  if (event.type === 'workflow_runs') {
    return { ...state, workflows: event.runs, error: null }
  }
  if (event.type === 'workflow_detail') {
    const existing = state.workflows.filter(run => run.runId !== event.run.runId)
    return {
      ...state,
      workflows: [...existing, event.run],
      workflowDetails: {
        ...state.workflowDetails,
        [event.run.runId]: { run: event.run, script: event.script, events: event.events, draft: event.draft ?? null, progress: event.progress ?? null },
      },
      error: null,
    }
  }
  if (event.type === 'workflow_event') {
    return { ...state, workflowEvents: [...state.workflowEvents, event.event], error: null }
  }
  if (event.type === 'workflow_final') {
    return { ...state, workflowResults: { ...state.workflowResults, [event.runId]: event.result }, error: null }
  }
  if (event.type === 'system') {
    return {
      ...state,
      messages: [
        ...state.messages,
        { id: `s-${state.messages.length}`, role: 'system', text: event.text, done: true },
      ],
      error: null,
    }
  }
  if (event.type === 'local_command_input' || event.type === 'local_command_output') {
    const kind = event.type === 'local_command_input' ? 'input' : 'output'
    const prefix = kind === 'input' ? 'lc-in' : 'lc-out'
    return {
      ...state,
      messages: [
        ...state.messages,
        { id: `${prefix}-${state.messages.length}`, role: 'system', text: event.text, done: true, localCommand: kind },
      ],
      error: null,
    }
  }
  if (event.type === 'clear') {
    return { ...state, messages: [], error: null }
  }
  if (event.type === 'history_replace') {
    return {
      ...state,
      messages: event.messages.map((message, index) => {
        let id = `h-${index}`
        if (message.taskId !== undefined && message.role === 'user') id = `u-${message.taskId}`
        if (message.taskId !== undefined && message.role === 'assistant') id = `a-${message.taskId}`
        return message.taskId === undefined
          ? { id, role: message.role, text: message.text, done: true }
          : { id, role: message.role, text: message.text, done: true, taskId: message.taskId }
      }),
      error: null,
    }
  }
  if (event.type === 'rewind_done') {
    const idx = state.messages.findIndex(message => message.id === `u-${event.taskId}`)
    if (idx === -1) return { ...state, error: null }
    return { ...state, messages: state.messages.slice(0, idx), error: null }
  }
  if (event.type === 'user') {
    return {
      ...state,
      messages: [...state.messages, { id: `u-${event.taskId}`, role: 'user', text: event.text, done: true, taskId: event.taskId }],
      error: null,
    }
  }
  if (event.type === 'assistant_delta') {
    const id = `a-${event.taskId}`
    const idx = state.messages.findIndex(message => message.id === id)
    if (idx === -1) {
      return {
        ...state,
        messages: [...state.messages, { id, role: 'assistant', text: event.text, done: false, taskId: event.taskId }],
      }
    }
    const messages = state.messages.slice()
    messages[idx] = { ...messages[idx], text: messages[idx].text + event.text, done: false }
    return { ...state, messages }
  }
  if (event.type === 'assistant_done') {
    const id = `a-${event.taskId}`
    const idx = state.messages.findIndex(message => message.id === id)
    if (idx === -1) {
      return {
        ...state,
        messages: [...state.messages, { id, role: 'assistant', text: event.text, done: true, taskId: event.taskId }],
      }
    }
    const messages = state.messages.slice()
    messages[idx] = { ...messages[idx], text: event.text, done: true }
    return { ...state, messages }
  }
  return state
}
