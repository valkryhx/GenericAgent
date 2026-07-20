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

test('splitStaticAndActiveMessages keeps open-turn user in live while backend is running (visibility P0-A)', () => {
  // User is !done until turn finalizes — only live, never Static (single channel).
  const messages: ChatMessage[] = [
    { id: 'u-1', role: 'user', text: 'old question', done: true, taskId: 1 },
    { id: 'a-1', role: 'assistant', text: 'old answer', done: true, taskId: 1 },
    { id: 'u-2', role: 'user', text: 'new question', done: false, taskId: 2 },
  ]

  const split = splitStaticAndActiveMessages(messages, { keepLatestTaskActive: true })

  assert.deepEqual(split.staticMessages.map(message => message.id), ['u-1', 'a-1'])
  assert.deepEqual(split.activeMessages.map(message => message.id), ['u-2'])
})

test('splitStaticAndActiveMessages keeps open user + streaming assistant together in live', () => {
  const messages: ChatMessage[] = [
    { id: 'u-1', role: 'user', text: 'old question', done: true, taskId: 1 },
    { id: 'a-1', role: 'assistant', text: 'old answer', done: true, taskId: 1 },
    { id: 'u-2', role: 'user', text: 'new question', done: false, taskId: 2 },
    { id: 'a-2', role: 'assistant', text: 'partial answer', done: false, taskId: 2 },
  ]

  const split = splitStaticAndActiveMessages(messages)

  assert.deepEqual(split.staticMessages.map(message => message.id), ['u-1', 'a-1'])
  assert.deepEqual(split.activeMessages.map(message => message.id), ['u-2', 'a-2'])
})

test('splitStaticAndActiveMessages never puts the same user in both static and active', () => {
  const messages: ChatMessage[] = [
    { id: 'u-2', role: 'user', text: 'new question', done: false, taskId: 2 },
    { id: 'a-2', role: 'assistant', text: 'partial', done: false, taskId: 2 },
  ]
  const split = splitStaticAndActiveMessages(messages, { keepLatestTaskActive: true })
  const staticIds = new Set(split.staticMessages.map(m => m.id))
  for (const m of split.activeMessages) {
    assert.equal(staticIds.has(m.id), false)
  }
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

test('splitStaticAndActiveMessages with keepLatestTaskActive excludes finalized user while streaming', () => {
  // After finalize, user is done and must not re-enter live (no double paint).
  const messages: ChatMessage[] = [
    { id: 'u-1', role: 'user', text: 'old question', done: true, taskId: 1 },
    { id: 'a-1', role: 'assistant', text: 'old answer', done: true, taskId: 1 },
    { id: 'u-2', role: 'user', text: 'new question', done: true, taskId: 2 },
    { id: 'a-2', role: 'assistant', text: 'partial answer', done: false, taskId: 2 },
  ]

  const split = splitStaticAndActiveMessages(messages, { keepLatestTaskActive: true })

  assert.deepEqual(split.staticMessages.map(message => message.id), ['u-1', 'a-1', 'u-2'])
  assert.deepEqual(split.activeMessages.map(message => message.id), ['a-2'])
})

test('mid-run /stop local commands stay live so Static cannot re-print Stop requested', () => {
  // Regression: done local-command rows after an open assistant used to enter Static
  // immediately; assistant_done then inserted finalized rows before them and Ink Static
  // re-emitted the tail → a second "Stop requested" with only one "/stop".
  const messages: ChatMessage[] = [
    { id: 'u-1', role: 'user', text: 'old', done: true, taskId: 1 },
    { id: 'a-1', role: 'assistant', text: 'old answer', done: true, taskId: 1 },
    { id: 'u-2', role: 'user', text: 'hello', done: true, taskId: 2 },
    { id: 'a-2', role: 'assistant', text: 'working...', done: false, taskId: 2 },
    { id: 'lc-in-4', role: 'system', text: '/stop', done: true, localCommand: 'input' },
    { id: 'lc-out-5', role: 'system', text: 'Stop requested', done: true, localCommand: 'output' },
  ]

  const split = splitStaticAndActiveMessages(messages, { keepLatestTaskActive: true })

  assert.deepEqual(split.staticMessages.map(message => message.id), ['u-1', 'a-1', 'u-2'])
  assert.deepEqual(split.activeMessages.map(message => message.id), ['a-2', 'lc-in-4', 'lc-out-5'])
})

test('after turn finalizes, stop local commands commit to static once with the turn', () => {
  const messages: ChatMessage[] = [
    { id: 'u-1', role: 'user', text: 'old', done: true, taskId: 1 },
    { id: 'a-1', role: 'assistant', text: 'old answer', done: true, taskId: 1 },
    { id: 'u-2', role: 'user', text: 'hello', done: true, taskId: 2 },
    { id: 'a-2', role: 'assistant', text: 'working... aborted', done: true, taskId: 2 },
    { id: 'lc-in-4', role: 'system', text: '/stop', done: true, localCommand: 'input' },
    { id: 'lc-out-5', role: 'system', text: 'Stop requested', done: true, localCommand: 'output' },
  ]

  const split = splitStaticAndActiveMessages(messages)

  assert.deepEqual(
    split.staticMessages.map(message => message.id),
    ['u-1', 'a-1', 'u-2', 'a-2', 'lc-in-4', 'lc-out-5'],
  )
  assert.deepEqual(split.activeMessages, [])
})
