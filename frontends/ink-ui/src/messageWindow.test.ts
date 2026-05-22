import test from 'node:test'
import assert from 'node:assert/strict'
import {
  assistantDisplayText,
  clampTranscriptScrollOffset,
  liveTranscriptViewportLines,
  tailLines,
  transcriptLines,
  visibleMessages,
  visibleMessagesForViewport,
  visibleTranscriptLines,
  wrapTranscriptLines,
} from './messageWindow.js'
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

test('visibleMessagesForViewport keeps the latest messages within a row budget', () => {
  const messages: ChatMessage[] = Array.from({ length: 6 }, (_, index) => ({
    id: String(index + 1),
    role: 'user',
    text: `message ${index + 1}`,
    done: true,
  }))

  assert.deepEqual(visibleMessagesForViewport(messages, { maxRows: 3, expandedTools: false }).map(message => message.id), ['4', '5', '6'])
})

test('visibleMessagesForViewport always keeps the active streaming assistant tail', () => {
  const messages: ChatMessage[] = [
    { id: '1', role: 'user', text: 'older', done: true },
    { id: '2', role: 'assistant', text: Array.from({ length: 20 }, (_, index) => `line ${index}`).join('\n'), done: false },
  ]

  const visible = visibleMessagesForViewport(messages, { maxRows: 4, expandedTools: false })
  assert.equal(visible.at(-1)?.id, '2')
  assert.equal(visible.length, 1)
})

test('visibleMessagesForViewport can scroll upward from the sticky bottom', () => {
  const messages: ChatMessage[] = Array.from({ length: 6 }, (_, index) => ({
    id: String(index + 1),
    role: 'user',
    text: `message ${index + 1}`,
    done: true,
  }))

  assert.deepEqual(visibleMessagesForViewport(messages, { maxRows: 3, expandedTools: false, scrollOffset: 2 }).map(message => message.id), ['2', '3', '4'])
})

test('visibleMessagesForViewport clamps large scroll offsets to the oldest window', () => {
  const messages: ChatMessage[] = Array.from({ length: 6 }, (_, index) => ({
    id: String(index + 1),
    role: 'user',
    text: `message ${index + 1}`,
    done: true,
  }))

  assert.deepEqual(visibleMessagesForViewport(messages, { maxRows: 3, expandedTools: false, scrollOffset: 99 }).map(message => message.id), ['1', '2', '3'])
})

test('transcriptLines keeps full assistant output without vertical omission markers', () => {
  const messages: ChatMessage[] = [
    {
      id: 'a-1',
      role: 'assistant',
      text: Array.from({ length: 45 }, (_, index) => `line ${index + 1}`).join('\n'),
      done: true,
    },
  ]

  const lines = transcriptLines(messages, { expandedTools: false })
  const text = lines.map(line => line.text).join('\n')

  assert.equal(text.includes('omitted'), false)
  assert.ok(text.includes('line 1'))
  assert.ok(text.includes('line 45'))
})

test('visibleTranscriptLines scrolls by rendered lines from the sticky bottom', () => {
  const lines = Array.from({ length: 10 }, (_, index) => ({
    id: `line-${index + 1}`,
    text: `line ${index + 1}`,
  }))

  assert.deepEqual(visibleTranscriptLines(lines, { maxRows: 4, scrollOffset: 0 }).lines.map(line => line.text), [
    'line 7',
    'line 8',
    'line 9',
    'line 10',
  ])
  assert.deepEqual(visibleTranscriptLines(lines, { maxRows: 4, scrollOffset: 3 }).lines.map(line => line.text), [
    'line 4',
    'line 5',
    'line 6',
    'line 7',
  ])
  assert.deepEqual(visibleTranscriptLines(lines, { maxRows: 4, scrollOffset: 99 }).lines.map(line => line.text), [
    'line 1',
    'line 2',
    'line 3',
    'line 4',
  ])
})

test('liveTranscriptViewportLines caps streaming transcript rows to the viewport height', () => {
  const lines = [
    { id: '1', text: 'one' },
    { id: '2', text: 'two' },
    { id: '3', text: 'three' },
    { id: '4', text: 'four' },
  ]

  assert.deepEqual(liveTranscriptViewportLines(lines, 2).map(line => line.text), ['three', 'four'])
})

test('liveTranscriptViewportLines keeps completed and streaming transcript in one fixed window', () => {
  const messages: ChatMessage[] = [
    { id: 'u-1', role: 'user', text: 'first prompt', done: true },
    { id: 'a-1', role: 'assistant', text: 'first answer', done: true },
    { id: 'u-2', role: 'user', text: 'second prompt', done: true },
    { id: 'a-2', role: 'assistant', text: 'streaming answer', done: false },
  ]

  const lines = transcriptLines(messages, { expandedTools: false })
  const viewport = liveTranscriptViewportLines(lines, 4).map(line => line.text)

  assert.deepEqual(viewport, ['> second prompt', ' ', '✻ streaming answer', ' '])
})

test('clampTranscriptScrollOffset keeps scrollbar state inside rendered line bounds', () => {
  assert.equal(clampTranscriptScrollOffset(0, 10, 4), 0)
  assert.equal(clampTranscriptScrollOffset(3, 10, 4), 3)
  assert.equal(clampTranscriptScrollOffset(99, 10, 4), 6)
  assert.equal(clampTranscriptScrollOffset(-5, 10, 4), 0)
})

test('wrapTranscriptLines wraps long physical lines without adding ellipsis', () => {
  const lines = [
    {
      id: 'long-0',
      text: 'https://mcp.tavily.com/mcp/?tavilyApiKey=tvly-dev-abcdefghijklmnopqrstuvwxyz',
    },
  ]

  const wrapped = wrapTranscriptLines(lines, 20)

  assert.ok(wrapped.length > 1)
  assert.equal(wrapped.some(line => line.text.includes('...')), false)
  assert.equal(wrapped.map(line => line.text).join(''), lines[0]!.text)
  assert.ok(wrapped.every(line => line.text.length <= 20))
})
