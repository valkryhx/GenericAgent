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
  if (event.type === 'user') {
    return {
      ...state,
      messages: [...state.messages, { id: `u-${event.taskId}`, role: 'user', text: event.text, done: true }],
      error: null,
    }
  }
  if (event.type === 'assistant_delta') {
    const id = `a-${event.taskId}`
    const idx = state.messages.findIndex(message => message.id === id)
    if (idx === -1) {
      return {
        ...state,
        messages: [...state.messages, { id, role: 'assistant', text: event.text, done: false }],
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
        messages: [...state.messages, { id, role: 'assistant', text: event.text, done: true }],
      }
    }
    const messages = state.messages.slice()
    messages[idx] = { ...messages[idx], text: event.text, done: true }
    return { ...state, messages }
  }
  return state
}
