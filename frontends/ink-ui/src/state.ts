import type { BridgeEvent, ChatMessage } from './protocol.js'

export type AppState = {
  status: 'connecting' | 'idle' | 'running' | 'stopping'
  messages: ChatMessage[]
  error: string | null
}

export const initialState: AppState = {
  status: 'connecting',
  messages: [],
  error: null,
}

export function applyBridgeEvent(state: AppState, event: BridgeEvent): AppState {
  if (event.type === 'ready') {
    return { ...state, status: 'idle', error: null }
  }
  if (event.type === 'status') {
    return { ...state, status: event.status, error: null }
  }
  if (event.type === 'error') {
    return { ...state, error: event.message }
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
