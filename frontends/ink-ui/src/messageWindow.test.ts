import test from 'node:test'
import assert from 'node:assert/strict'
import { assistantDisplayText, tailLines, visibleMessages } from './messageWindow.js'
import type { ChatMessage } from './protocol.js'

test('visibleMessages keeps only the latest messages', () => {
  const messages: ChatMessage[] = Array.from({ length: 5 }, (_, i) => ({
    id: String(i),
    role: 'assistant',
    text: String(i),
    done: true,
  }))

  assert.deepEqual(visibleMessages(messages, 2).map(message => message.id), ['3', '4'])
})

test('tailLines keeps the end of long output', () => {
  const text = ['a', 'b', 'c', 'd'].join('\n')

  assert.equal(tailLines(text, 2), '... 2 earlier lines omitted ...\nc\nd')
})

test('assistantDisplayText caps expanded streaming output to a stable window', () => {
  const text = Array.from({ length: 40 }, (_, index) => `line ${index + 1}`).join('\n')

  assert.equal(assistantDisplayText(text, { expanded: true, done: false, maxExpandedLines: 8 }).split('\n').length, 9)
  assert.equal(assistantDisplayText(text, { expanded: true, done: false, maxExpandedLines: 8 }).split('\n')[0], '... 32 earlier lines omitted ...')
})

test('assistantDisplayText keeps collapsed output compact', () => {
  const text = Array.from({ length: 40 }, (_, index) => `line ${index + 1}`).join('\n')

  assert.equal(assistantDisplayText(text, { expanded: false, done: false }).split('\n').length, 19)
})
