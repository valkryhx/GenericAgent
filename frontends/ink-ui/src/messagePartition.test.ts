import test from 'node:test'
import assert from 'node:assert/strict'
import { splitStaticAndActiveMessages } from './messagePartition.js'
import type { ChatMessage } from './protocol.js'

test('splitStaticAndActiveMessages defaults completed prompts to terminal scrollback', () => {
  const messages: ChatMessage[] = [
    { id: 'u-1', role: 'user', text: 'old question', done: true, taskId: 1 },
    { id: 'a-1', role: 'assistant', text: 'old answer', done: true, taskId: 1 },
    { id: 'u-2', role: 'user', text: 'new question', done: true, taskId: 2 },
  ]

  const split = splitStaticAndActiveMessages(messages)

  assert.deepEqual(split.staticMessages.map(message => message.id), ['u-1', 'a-1', 'u-2'])
  assert.deepEqual(split.activeMessages, [])
})

test('splitStaticAndActiveMessages can keep the latest task active while the backend is running', () => {
  const messages: ChatMessage[] = [
    { id: 'u-1', role: 'user', text: 'old question', done: true, taskId: 1 },
    { id: 'a-1', role: 'assistant', text: 'old answer', done: true, taskId: 1 },
    { id: 'u-2', role: 'user', text: 'new question', done: true, taskId: 2 },
  ]

  const split = splitStaticAndActiveMessages(messages, { keepLatestTaskActive: true })

  assert.deepEqual(split.staticMessages.map(message => message.id), ['u-1', 'a-1'])
  assert.deepEqual(split.activeMessages.map(message => message.id), ['u-2'])
})

test('splitStaticAndActiveMessages keeps the latest user and streaming assistant together', () => {
  const messages: ChatMessage[] = [
    { id: 'u-1', role: 'user', text: 'old question', done: true, taskId: 1 },
    { id: 'a-1', role: 'assistant', text: 'old answer', done: true, taskId: 1 },
    { id: 'u-2', role: 'user', text: 'new question', done: true, taskId: 2 },
    { id: 'a-2', role: 'assistant', text: 'partial answer', done: false, taskId: 2 },
  ]

  const split = splitStaticAndActiveMessages(messages)

  assert.deepEqual(split.staticMessages.map(message => message.id), ['u-1', 'a-1'])
  assert.deepEqual(split.activeMessages.map(message => message.id), ['u-2', 'a-2'])
})

test('splitStaticAndActiveMessages moves the latest completed task to terminal scrollback by default', () => {
  const messages: ChatMessage[] = [
    { id: 'u-1', role: 'user', text: 'old question', done: true, taskId: 1 },
    { id: 'a-1', role: 'assistant', text: 'old answer', done: true, taskId: 1 },
    { id: 'u-2', role: 'user', text: 'new question', done: true, taskId: 2 },
    { id: 'a-2', role: 'assistant', text: 'final answer', done: true, taskId: 2 },
  ]

  const split = splitStaticAndActiveMessages(messages)

  assert.deepEqual(split.staticMessages.map(message => message.id), ['u-1', 'a-1', 'u-2', 'a-2'])
  assert.deepEqual(split.activeMessages, [])
})

test('splitStaticAndActiveMessages leaves local command transcript in terminal scrollback', () => {
  const messages: ChatMessage[] = [
    { id: 'lc-in-0', role: 'system', text: '/status', done: true, localCommand: 'input' },
    { id: 'lc-out-1', role: 'system', text: 'idle', done: true, localCommand: 'output' },
  ]

  const split = splitStaticAndActiveMessages(messages)

  assert.deepEqual(split.staticMessages.map(message => message.id), ['lc-in-0', 'lc-out-1'])
  assert.deepEqual(split.activeMessages, [])
})
