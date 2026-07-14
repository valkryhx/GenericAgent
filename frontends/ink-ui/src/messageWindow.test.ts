import test from 'node:test'
import assert from 'node:assert/strict'
import stringWidth from 'string-width'
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
import { getInkTheme } from './theme.js'

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

test('liveTranscriptViewportLines pins open-turn user lines above a long assistant tail', () => {
  const userLine = { id: 'u-1-0', text: '> 现在几点了', color: 'black', backgroundColor: '#d7d7d7' }
  const userBlank = { id: 'u-1-blank', text: ' ' }
  const assistantLines = Array.from({ length: 20 }, (_, index) => ({
    id: `a-1-${index}`,
    text: index === 0 ? `line ${index}` : `  line ${index}`,
  }))
  const lines = [userLine, userBlank, ...assistantLines]
  const visible = liveTranscriptViewportLines(lines, 6)
  assert.ok(visible.some(line => line.id === 'u-1-0'), 'open user must remain visible in live viewport')
  assert.ok(visible.some(line => line.id.startsWith('a-1-')), 'assistant tail still shown')
  assert.ok(visible.length <= 6)
  // User block stays at the front of the live window (question before answer).
  assert.equal(visible[0]?.id, 'u-1-0')
})

test('transcriptLines keeps [Action]/[Status] left-aligned and equally muted', () => {
  // Stream-commit may place Action on the first line of a segment and Status on a
  // later line. The old "indent every non-first line by 2 spaces" rule made Status
  // look misaligned under Action; both should share the same column and style.
  const messages: ChatMessage[] = [
    {
      id: 'a-1-c0',
      role: 'assistant',
      text: [
        '`````',
        '[Action] Calling MCP tool: mcp__tavily__tavily_search',
        '[Status] MCP success',
        '`````',
      ].join('\n'),
      done: true,
      taskId: 1,
    },
  ]
  const rows = transcriptLines(messages, { theme: getInkTheme('default') })
  const action = rows.find(row => row.text.includes('[Action]'))
  const status = rows.find(row => row.text.includes('[Status]'))
  assert.ok(action)
  assert.ok(status)
  assert.equal(action!.text.startsWith('  '), false, `Action indented: ${JSON.stringify(action!.text)}`)
  assert.equal(status!.text.startsWith('  '), false, `Status indented: ${JSON.stringify(status!.text)}`)
  assert.equal(action!.text.trimStart(), action!.text)
  assert.equal(status!.text.trimStart(), status!.text)
  const actionColor = action!.parts?.find(part => part.text.includes('[Action]'))?.color
  const statusColor = status!.parts?.find(part => part.text.includes('[Status]'))?.color
  assert.equal(actionColor, 'gray')
  assert.equal(statusColor, 'gray')
  assert.equal(actionColor, statusColor)
})

test('transcriptLines strips blockquote markers from [Action]/[Status] lines', () => {
  // Residual fences / tool-summary context can make marked emit blockquotes.
  const messages: ChatMessage[] = [
    {
      id: 'a-9',
      role: 'assistant',
      text: '> [Action] Calling MCP tool: x\n> [Status] MCP success',
      done: true,
      taskId: 9,
    },
  ]
  const rows = transcriptLines(messages, { theme: getInkTheme('default') })
  const action = rows.find(row => row.text.includes('[Action]'))
  const status = rows.find(row => row.text.includes('[Status]'))
  assert.ok(action)
  assert.ok(status)
  assert.equal(action!.text, '[Action] Calling MCP tool: x')
  assert.equal(status!.text, '[Status] MCP success')
  assert.equal(action!.parts?.[0]?.color, 'gray')
  assert.equal(status!.parts?.[0]?.color, 'gray')
})

test('transcriptLines forces [Action]/[Status] to muted gray even when one side was a code fence', () => {
  // Historical bug: open fence → Action rendered as theme.code (cyan); Status outside
  // fence → default foreground. Users saw "calling=blue, success=black".
  const messages: ChatMessage[] = [
    {
      id: 'a-10',
      role: 'assistant',
      text: [
        '```',
        '[Action] Calling MCP tool: mcp__tavily__tavily_search',
        '```',
        '[Status] MCP success',
      ].join('\n'),
      done: true,
      taskId: 10,
    },
  ]
  const theme = getInkTheme('default')
  const rows = transcriptLines(messages, { theme })
  const action = rows.find(row => row.text.includes('[Action]'))
  const status = rows.find(row => row.text.includes('[Status]'))
  assert.ok(action)
  assert.ok(status)
  const actionColor = action!.parts?.find(part => part.text.includes('[Action]'))?.color
  const statusColor = status!.parts?.find(part => part.text.includes('[Status]'))?.color
  assert.equal(actionColor, 'gray')
  assert.equal(statusColor, 'gray')
  assert.notEqual(actionColor, theme.code)
  assert.notEqual(statusColor, theme.code)
  assert.equal(action!.text.startsWith('  '), false)
  assert.equal(status!.text.startsWith('  '), false)
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


test('transcriptLines renders assistant markdown as styled parts', () => {
  const lines = transcriptLines([
    { id: 'a1', role: 'assistant', text: 'Use **bold** and `code`.', done: true },
  ], { expandedTools: false })

  assert.equal(lines[0]!.text, 'Use bold and code.')
  assert.equal(lines[0]!.parts?.some(part => part.text === 'bold' && part.bold), true)
  assert.equal(lines[0]!.parts?.some(part => part.text === 'code' && part.color === 'cyan'), true)
})

test('transcriptLines applies lightmode colors to assistant markdown code and links', () => {
  const lines = transcriptLines([
    { id: 'a1', role: 'assistant', text: 'Use `code` and [docs](https://example.test).', done: true },
  ], { expandedTools: false, theme: getInkTheme('lightmode') })

  assert.equal(lines[0]!.parts?.some(part => part.text === 'code' && part.color === '#7c3aed'), true)
  assert.equal(lines[0]!.parts?.some(part => part.text === 'docs' && part.color === '#2563eb' && part.underline), true)
  assert.equal(lines[0]!.parts?.some(part => part.text === ' (https://example.test)' && part.color === '#64748b'), true)
})

test('wrapTranscriptLines preserves styled parts across wrapped rows', () => {
  const lines = transcriptLines([
    { id: 'a1', role: 'assistant', text: '**abcdef**', done: true },
  ], { expandedTools: false })

  const wrapped = wrapTranscriptLines(lines, 5)

  assert.equal(wrapped.map(line => line.text).join('|'), 'abcde|f| ')
  assert.equal(wrapped[0]!.parts?.some(part => part.bold), true)
  assert.equal(wrapped[1]!.parts?.some(part => part.bold), true)
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

  assert.deepEqual(viewport, ['> second prompt', ' ', 'streaming answer', ' '])
})

test('transcriptLines does not prefix assistant body with activity glyph ✻', () => {
  // ✻ is reserved for formatRunningStatus activity chrome, not message content.
  // Stream commits create many assistant segments; each must not re-stamp ✻.
  const lines = transcriptLines([
    { id: 'a-1-c0', role: 'assistant', text: 'committed prefix', done: true, taskId: 1 },
    { id: 'a-1', role: 'assistant', text: 'live tail', done: false, taskId: 1 },
  ], { expandedTools: false })
  const texts = lines.map(line => line.text)
  assert.equal(texts.some(text => text.includes('✻')), false)
  assert.ok(texts.some(text => text.includes('committed prefix')))
  assert.ok(texts.some(text => text.includes('live tail')))
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

test('wrapTranscriptLines wraps wide characters by terminal display width', () => {
  const wrapped = wrapTranscriptLines([{ id: 'wide-0', text: '中文测试' }], 4)

  assert.deepEqual(wrapped.map(line => line.text), ['中文', '测试'])
})

test('wrapTranscriptLines keeps emoji and combining graphemes intact', () => {
  const wrapped = wrapTranscriptLines([{ id: 'mixed', text: 'ab👨‍💻e\u0301cd' }], 4)

  assert.deepEqual(wrapped.map(line => line.text), ['ab👨‍💻', 'e\u0301cd'])
})

test('wrapTranscriptLines never exceeds the requested display width', () => {
  const wrapped = wrapTranscriptLines([{ id: 'wide', text: '中文👨‍💻abcdef' }], 4)

  assert.equal(wrapped.every(line => stringWidth(line.text) <= 4), true)
})
