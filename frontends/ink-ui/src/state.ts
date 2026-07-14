import type { BridgeEvent, ChatMessage, TokenUsage, WorkflowDraftPayload, WorkflowEvent, WorkflowProgressPayload, WorkflowRun } from './protocol.js'
import {
  commitStreamingAssistantMessages,
  committedAssistantPrefix,
  remainingAssistantTextAfterCommits,
} from './streamCommit.js'

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
    const nextStatus = event.status
    // Finalize open-turn user prompts when the turn returns to idle so they commit to Static once.
    const messages = nextStatus === 'idle'
      ? finalizeOpenUserMessages(state.messages)
      : state.messages
    return {
      ...state,
      status: nextStatus,
      activityLabel: nextStatus === 'idle' ? null : state.activityLabel,
      tokenUsage: nextStatus === 'running' ? null : state.tokenUsage,
      messages,
      error: null,
    }
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
    const detail = state.workflowDetails[event.run.runId]
    return {
      ...state,
      workflows: [...existing, event.run],
      workflowDetails: detail
        ? { ...state.workflowDetails, [event.run.runId]: { ...detail, run: event.run } }
        : state.workflowDetails,
      error: null,
    }
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
  if (event.type === 'workflow_progress') {
    const existingRun = state.workflows.find(run => run.runId === event.progress.runId)
    const existingDetail = state.workflowDetails[event.progress.runId]
    if (!existingRun && !existingDetail) return { ...state, error: null }
    const run = existingDetail?.run ?? existingRun!
    const updatedRun = { ...run, status: event.progress.status }
    const workflows = state.workflows.some(item => item.runId === updatedRun.runId)
      ? state.workflows.map(item => item.runId === updatedRun.runId ? { ...item, status: event.progress.status } : item)
      : [...state.workflows, updatedRun]
    return {
      ...state,
      workflows,
      workflowDetails: {
        ...state.workflowDetails,
        [updatedRun.runId]: {
          run: updatedRun,
          script: existingDetail?.script ?? '',
          events: existingDetail?.events ?? [],
          draft: existingDetail?.draft ?? null,
          progress: event.progress,
        },
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
    // P0-A: open-turn user stays !done so it only paints in live until finalize.
    // Prevents premature Static write that live dock height changes can cover.
    return {
      ...state,
      messages: [...state.messages, { id: `u-${event.taskId}`, role: 'user', text: event.text, done: false, taskId: event.taskId }],
      error: null,
    }
  }
  if (event.type === 'assistant_delta') {
    const id = `a-${event.taskId}`
    const idx = state.messages.findIndex(message => message.id === id)
    let messages: ChatMessage[]
    if (idx === -1) {
      messages = [...state.messages, { id, role: 'assistant', text: event.text, done: false, taskId: event.taskId }]
    } else {
      messages = state.messages.slice()
      messages[idx] = { ...messages[idx], text: messages[idx].text + event.text, done: false }
    }
    // Finalize open user BEFORE any stream-commit segments so Static order is:
    //   user → a-{id}-c0 → a-{id}-c1 → … (never assistant-before-user).
    messages = finalizeOpenUserMessages(messages, event.taskId)
    // Phase 2: commit overflow lines to Static; keep a short live tail (Codex stream tail).
    messages = commitStreamingAssistantMessages(messages, event.taskId)
    return { ...state, messages }
  }
  if (event.type === 'assistant_done') {
    const id = `a-${event.taskId}`
    const idx = state.messages.findIndex(message => message.id === id)
    const alreadyCommitted = committedAssistantPrefix(state.messages, event.taskId)
    const remaining = remainingAssistantTextAfterCommits(event.text, alreadyCommitted)
    let messages: ChatMessage[]
    if (idx === -1) {
      // No live tail left (fully committed mid-stream) or never started — only append remaining once.
      messages = remaining
        ? [...state.messages, { id, role: 'assistant', text: remaining, done: true, taskId: event.taskId }]
        : state.messages.slice()
    } else if (!remaining) {
      // Entire body already in commit segments — drop empty live shell.
      messages = state.messages.filter(message => message.id !== id)
    } else {
      messages = state.messages.slice()
      messages[idx] = { ...messages[idx], text: remaining, done: true }
    }
    // Finalize matching open user with this task so the whole turn commits to Static once.
    messages = finalizeOpenUserMessages(messages, event.taskId)
    return { ...state, messages }
  }
  return state
}

/** Mark open (!done) user prompts as finalized so they can enter Static scrollback once. */
function finalizeOpenUserMessages(messages: ChatMessage[], taskId?: number): ChatMessage[] {
  let changed = false
  const next = messages.map(message => {
    if (message.role !== 'user' || message.done) return message
    if (taskId !== undefined && message.taskId !== taskId) return message
    changed = true
    return { ...message, done: true }
  })
  return changed ? next : messages
}
